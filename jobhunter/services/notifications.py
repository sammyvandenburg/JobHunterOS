"""Digest composition and delivery with a backend-enforced recipient allowlist.

The only recipient a digest can ever go to is the address stored in the
``digest_email`` consent record. The model, dashboard forms, and source
content have no path to change it. Every delivery attempt is recorded;
ambiguous outcomes are marked ``uncertain`` and never blindly retried.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from jobhunter.config import AppContext, write_private_file
from jobhunter.db import Database
from jobhunter.services import consent, freshness
from jobhunter.services.matching.engine import group_label
from jobhunter.util import now_iso, sha256_text

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class DigestError(Exception):
    pass


def set_recipient(db: Database, address: str) -> dict:
    address = address.strip()
    if not EMAIL_RE.match(address):
        raise DigestError("That does not look like an email address.")
    consent.grant(db, "digest_email", {"address": address, "confirmed": True})
    return {"address": address}


def recipient(db: Database) -> str | None:
    d = consent.details(db, "digest_email")
    return d.get("address") if d.get("confirmed") else None


@dataclass
class Digest:
    subject: str
    text: str
    html: str
    job_ids: list[int]
    content_hash: str


def pending_jobs(db: Database, since: str | None = None, limit: int = 15) -> list[dict]:
    """Recommended, not dismissed, not yet included in a sent digest."""
    rows = db.fetchall(
        "SELECT j.*, m.match_group, m.score, m.explanation_text, m.next_step FROM matches m JOIN jobs j ON j.id = m.job_id "
        "WHERE m.match_group IN ('good','worth_a_shot','backup') AND j.availability != 'closed' "
        "AND j.id NOT IN (SELECT job_id FROM events WHERE kind = 'dismissed' AND reverted_by IS NULL) "
        "AND j.id NOT IN (SELECT CAST(value AS INTEGER) FROM notifications, json_each(notifications.job_ids_json) WHERE notifications.status = 'sent') "
        + ("AND j.first_seen_at >= ? " if since else "")
        + "ORDER BY CASE m.match_group WHEN 'good' THEN 0 WHEN 'worth_a_shot' THEN 1 ELSE 2 END, m.score DESC LIMIT ?",
        ((since, limit) if since else (limit,)),
    )
    return rows


def compose(db: Database, jobs: list[dict], include_closed_note: bool = True) -> Digest:
    lines = ["Hi,", "", f"Hunter found {len(jobs)} job(s) worth a look.", ""]
    html_items = []
    for j in jobs:
        link = j.get("direct_url") if j.get("direct_url_status") == "verified" else (j.get("apply_url") or j.get("discovery_url"))
        b, flabel = freshness.bucket(j)
        lines += [
            f"- {j['title']} at {j.get('employer_name') or 'unknown employer'} ({group_label(j['match_group'])})",
            f"  Location: {j.get('location_text') or 'not stated'}" + (f" · {j['remote_restriction']}" if j.get("remote_restriction") else ""),
            f"  Salary: {j.get('salary_text') or 'Not listed'}",
            f"  {flabel}",
            f"  Why: {j.get('explanation_text') or ''}",
            f"  Link: {link or 'no link available'}",
            "",
        ]
        html_items.append(
            "<li><strong>{t}</strong> at {e} <em>({g})</em><br>{loc}{rr}<br>Salary: {sal}<br>{fl}<br>{why}<br>{link}</li>".format(
                t=_esc(j["title"]),
                e=_esc(j.get("employer_name") or "unknown employer"),
                g=_esc(group_label(j["match_group"])),
                loc=_esc(j.get("location_text") or "not stated"),
                rr=(" · " + _esc(j["remote_restriction"])) if j.get("remote_restriction") else "",
                sal=_esc(j.get("salary_text") or "Not listed"),
                fl=_esc(flabel),
                why=_esc(j.get("explanation_text") or ""),
                link=f'<a href="{_esc(link)}">{_esc(link)}</a>' if link else "no link available",
            )
        )
    lines += ["Open your dashboard on the computer where Hunter runs to save, dismiss, or prepare an application.", "", "Hunter (JobHunterOS)"]
    text = "\n".join(lines)
    html = "<p>Hi,</p><p>Hunter found {} job(s) worth a look.</p><ul>{}</ul><p>Open your dashboard on the computer where Hunter runs to act on these.</p><p>Hunter (JobHunterOS)</p>".format(len(jobs), "".join(html_items))
    ids = [int(j["id"]) for j in jobs]
    return Digest(subject=f"Hunter: {len(jobs)} job(s) worth a look", text=text, html=html, job_ids=ids, content_hash=sha256_text(json.dumps(sorted(ids))))


def _esc(s: str | None) -> str:
    import html

    return html.escape(s or "")


def already_sent(db: Database, content_hash: str) -> bool:
    return db.fetchone("SELECT id FROM notifications WHERE content_hash = ? AND status = 'sent'", (content_hash,)) is not None


def record(db: Database, digest: Digest, channel: str, recipient_addr: str | None, status: str, error: str | None = None, provider_message_id: str | None = None, body_path: str | None = None) -> int:
    return db.insert(
        "notifications",
        {
            "kind": "digest",
            "channel": channel,
            "recipient": recipient_addr,
            "subject": digest.subject,
            "content_hash": digest.content_hash,
            "job_ids_json": json.dumps(digest.job_ids),
            "body_path": body_path,
            "status": status,
            "attempts": 1,
            "provider_message_id": provider_message_id,
            "error": error,
            "created_at": now_iso(),
            "sent_at": now_iso() if status == "sent" else None,
        },
    )


def send_digest(ctx: AppContext, db: Database, jobs: list[dict] | None = None, dry_run: bool = False) -> dict:
    """Send a digest to the confirmed recipient only. Returns the outcome record."""
    addr = recipient(db)
    if not addr:
        raise DigestError("No confirmed digest address. Set one with `jobhunter digest address you@example.com`.")
    jobs = pending_jobs(db) if jobs is None else jobs
    if not jobs:
        return {"status": "skipped", "note": "Nothing new worth emailing."}
    digest = compose(db, jobs)
    if already_sent(db, digest.content_hash):
        return {"status": "skipped", "note": "This exact digest was already sent."}
    path = ctx.paths.exports_dir / "digests" / f"digest-{now_iso().replace(':', '')}.txt"
    write_private_file(path, digest.text.encode("utf-8"))
    if dry_run:
        return {"status": "preview", "subject": digest.subject, "to": addr, "body_path": str(path), "jobs": digest.job_ids}
    from jobhunter.services.email import transport

    try:
        result = transport.send(ctx, db, to_address=addr, subject=digest.subject, text=digest.text, html=digest.html)
    except transport.TransportNotConfigured as exc:
        nid = record(db, digest, "email", addr, "failed", error=str(exc), body_path=str(path))
        return {"status": "failed", "error": str(exc), "notification_id": nid, "body_path": str(path)}
    except transport.TransportUncertain as exc:
        nid = record(db, digest, "email", addr, "uncertain", error=str(exc), body_path=str(path))
        return {"status": "uncertain", "error": str(exc), "notification_id": nid, "note": "The provider did not confirm delivery. Hunter will not resend this digest automatically."}
    except transport.TransportError as exc:
        nid = record(db, digest, "email", addr, "failed", error=str(exc), body_path=str(path))
        return {"status": "failed", "error": str(exc), "notification_id": nid, "body_path": str(path)}
    nid = record(db, digest, "email", addr, "sent", provider_message_id=result.get("message_id"), body_path=str(path))
    db.execute("UPDATE schedule SET last_digest_at = ? WHERE id = 1", (now_iso(),))
    return {"status": "sent", "to": addr, "notification_id": nid, "jobs": digest.job_ids, "transport": result.get("transport")}


def history(db: Database, limit: int = 30) -> list[dict]:
    return db.fetchall("SELECT * FROM notifications ORDER BY id DESC LIMIT ?", (limit,))
