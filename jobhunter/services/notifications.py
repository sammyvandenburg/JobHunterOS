"""Digest composition and delivery with a backend-enforced recipient allowlist.

The only recipient a digest can ever go to is the address stored in the
``digest_email`` consent record. The model, dashboard forms, and source
content have no path to change it. Every delivery attempt is recorded;
ambiguous outcomes are marked ``uncertain`` and never blindly retried.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import timedelta

from jobhunter.config import AppContext, write_private_file
from jobhunter.db import Database
from jobhunter.services import consent, freshness
from jobhunter.services.matching.engine import group_label
from jobhunter.util import now, now_iso, parse_iso, sha256_text

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class DigestError(Exception):
    pass


def set_recipient(db: Database, address: str) -> dict:
    address = address.strip()
    if not EMAIL_RE.match(address):
        raise DigestError("That does not look like an email address.")
    consent.grant(db, "digest_email", {"address": address, "confirmed": True})
    db.delete_setting("digest.suggested_recipient")
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
    """What the dashboard recommends right now, minus jobs already in a sent (or possibly sent) digest.
    Same selection as New jobs: current profile, policy, preferences, countries, freshness, not dismissed,
    not applied. One row per job."""
    from jobhunter.services import views

    already = {
        int(r["job_id"])
        for r in db.fetchall("SELECT CAST(value AS INTEGER) AS job_id FROM notifications, json_each(notifications.job_ids_json) WHERE notifications.status IN ('sent','uncertain','sending')")
    }
    items = views.current_recommended(db, exclude_ids=already)
    if since:
        items = [i for i in items if (i.get("first_seen_at") or "") >= since]
    out = []
    for i in items[:limit]:
        row = db.fetchone("SELECT * FROM jobs WHERE id = ?", (i["id"],))
        row.update({"match_group": i.get("group"), "score": i.get("score"), "explanation_text": i.get("explanation"), "next_step": i.get("next_step")})
        out.append(row)
    return out


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


STALE_SENDING_MINUTES = 15


def delivery_key(recipient_addr: str, content_hash: str) -> str:
    return sha256_text(f"{recipient_addr.strip().lower()}|{content_hash}")


def already_sent(db: Database, content_hash: str) -> bool:
    return db.fetchone("SELECT id FROM notifications WHERE content_hash = ? AND status = 'sent'", (content_hash,)) is not None


def _is_stale_sending(row: dict) -> bool:
    if row.get("status") != "sending":
        return False
    claimed = parse_iso(row.get("claimed_at") or row.get("created_at"))
    return bool(claimed) and (now() - claimed) > timedelta(minutes=STALE_SENDING_MINUTES)


def unresolved(db: Database) -> list[dict]:
    """Digests whose delivery is unknown: the provider did not confirm, or Hunter stopped mid-send."""
    rows = db.fetchall("SELECT * FROM notifications WHERE channel = 'email' AND resolved_at IS NULL AND status IN ('uncertain','sending') ORDER BY id DESC")
    return [r for r in rows if r["status"] == "uncertain" or _is_stale_sending(r)]


def resolve(db: Database, notification_id: int, outcome: str) -> dict:
    """The person tells Hunter what happened: 'arrived' (count it as sent) or 'not_arrived' (free it for a retry)."""
    row = db.fetchone("SELECT * FROM notifications WHERE id = ?", (notification_id,))
    if not row:
        raise DigestError(f"Digest {notification_id} not found.")
    if outcome not in {"arrived", "not_arrived"}:
        raise DigestError("Outcome must be 'arrived' or 'not_arrived'.")
    with db.tx():
        db.update("notifications", {"status": "sent" if outcome == "arrived" else "failed", "resolved_at": now_iso(), "resolution": outcome, "sent_at": row.get("sent_at") or (now_iso() if outcome == "arrived" else None)}, "id = ?", (notification_id,))
    return {"notification_id": notification_id, "status": "sent" if outcome == "arrived" else "failed", "note": "Marked as arrived." if outcome == "arrived" else "Okay. You can send it again; those jobs are back in the next digest."}


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
            "delivery_key": delivery_key(recipient_addr, digest.content_hash) if recipient_addr else None,
            "claimed_at": now_iso(),
        },
    )


def _claim(db: Database, digest: Digest, addr: str, body_path: str, force: bool) -> tuple[int | None, dict | None]:
    """Reserve the right to send this exact digest. Returns (notification_id, None) or (None, why_not)."""
    key = delivery_key(addr, digest.content_hash)
    with db.tx():
        row = db.fetchone("SELECT * FROM notifications WHERE delivery_key = ?", (key,))
        if row:
            st = row["status"]
            if st == "sent":
                return None, {"status": "skipped", "note": "This exact digest was already sent.", "notification_id": row["id"]}
            if st == "sending" and not _is_stale_sending(row):
                return None, {"status": "in_progress", "note": "This digest is being sent right now.", "notification_id": row["id"]}
            if (st == "uncertain" or st == "sending") and not force:
                return None, {"status": "blocked", "notification_id": row["id"], "note": "An earlier try may already have delivered this email. Hunter will not send it again by itself. Check your inbox; under Needs attention you can say it arrived, or send it again."}
            db.update("notifications", {"status": "sending", "claimed_at": now_iso(), "attempts": int(row.get("attempts") or 0) + 1, "error": None, "resolved_at": None, "resolution": None, "body_path": body_path, "recipient": addr}, "id = ?", (row["id"],))
            return row["id"], None
        try:
            nid = db.insert(
                "notifications",
                {"kind": "digest", "channel": "email", "recipient": addr, "subject": digest.subject, "content_hash": digest.content_hash, "job_ids_json": json.dumps(digest.job_ids), "body_path": body_path, "status": "sending", "attempts": 1, "created_at": now_iso(), "claimed_at": now_iso(), "delivery_key": key},
            )
        except sqlite3.IntegrityError:
            return None, {"status": "in_progress", "note": "This digest is being sent right now."}
        return nid, None


def send_digest(ctx: AppContext, db: Database, jobs: list[dict] | None = None, dry_run: bool = False, force: bool = False, notification_id: int | None = None) -> dict:
    """Send a digest to the confirmed recipient only, at most once per content.

    Outbox rules: the send is claimed in the database before anything leaves the machine; two callers cannot
    claim the same digest; a timeout or crash leaves the row 'uncertain' or stale 'sending', and nothing
    resends those automatically. `force=True` is the person's explicit "send it again".
    """
    addr = recipient(db)
    if not addr:
        raise DigestError("No confirmed digest address. Set one with `jobhunter digest address you@example.com`.")
    if notification_id is not None:
        row = db.fetchone("SELECT * FROM notifications WHERE id = ?", (notification_id,))
        if not row:
            raise DigestError(f"Digest {notification_id} not found.")
        ids = json.loads(row["job_ids_json"] or "[]")
        jobs = _jobs_by_ids(db, ids)
        if not jobs:
            raise DigestError("Those jobs are gone, so there is nothing to send again.")
    jobs = pending_jobs(db) if jobs is None else jobs
    if not jobs:
        return {"status": "skipped", "note": "Nothing new worth emailing."}
    digest = compose(db, jobs)
    path = ctx.paths.exports_dir / "digests" / f"digest-{now_iso().replace(':', '')}.txt"
    write_private_file(path, digest.text.encode("utf-8"))
    if dry_run:
        return {"status": "preview", "subject": digest.subject, "to": addr, "body_path": str(path), "jobs": digest.job_ids}
    nid, why_not = _claim(db, digest, addr, str(path), force)
    if why_not:
        return why_not
    from jobhunter.services.email import transport

    def finish(status: str, error: str | None = None, message_id: str | None = None) -> None:
        with db.tx():
            db.update("notifications", {"status": status, "error": error, "provider_message_id": message_id, "sent_at": now_iso() if status == "sent" else None}, "id = ?", (nid,))

    try:
        result = transport.send(ctx, db, to_address=addr, subject=digest.subject, text=digest.text, html=digest.html)
    except transport.TransportNotConfigured as exc:
        finish("failed", str(exc))
        return {"status": "failed", "error": str(exc), "notification_id": nid, "body_path": str(path)}
    except transport.TransportUncertain as exc:
        finish("uncertain", str(exc))
        return {"status": "uncertain", "error": str(exc), "notification_id": nid, "note": "The email provider did not confirm delivery. Hunter will not send this digest again by itself. Check your inbox, then say under Needs attention whether it arrived."}
    except transport.TransportError as exc:
        finish("failed", str(exc))
        return {"status": "failed", "error": str(exc), "notification_id": nid, "body_path": str(path)}
    except BaseException as exc:  # crash mid-send: leave the claim so a later run treats it as uncertain
        finish("uncertain", f"interrupted: {type(exc).__name__}")
        raise
    finish("sent", None, result.get("message_id"))
    db.execute("UPDATE schedule SET last_digest_at = ? WHERE id = 1", (now_iso(),))
    return {"status": "sent", "to": addr, "notification_id": nid, "jobs": digest.job_ids, "transport": result.get("transport")}


def _jobs_by_ids(db: Database, ids: list[int]) -> list[dict]:
    if not ids:
        return []
    marks = ",".join("?" for _ in ids)
    return db.fetchall(
        f"SELECT j.*, m.match_group, m.score, m.explanation_text, m.next_step FROM jobs j LEFT JOIN matches m ON m.job_id = j.id "
        f"WHERE j.id IN ({marks}) GROUP BY j.id ORDER BY j.id",
        tuple(ids),
    )


def history(db: Database, limit: int = 30) -> list[dict]:
    return db.fetchall("SELECT * FROM notifications ORDER BY id DESC LIMIT ?", (limit,))


def send_test(ctx: AppContext, db: Database) -> dict:
    """A short email to the confirmed address so the person can see that sending works.
    Goes through the same transport, allowlist and outbox as real digests."""
    addr = recipient(db)
    if not addr:
        raise DigestError("Confirm the address first.")
    stamp = now_iso()
    text = (
        "Hi,\n\nThis is a test from Hunter, your KubeCraft job-hunt companion.\n"
        "If you can read this, your job emails will arrive here.\n\n"
        f"Sent {stamp[:16].replace('T', ' ')} UTC. Nothing else was sent and no job was applied to.\n"
    )
    html = "<p>Hi,</p><p>This is a test from <strong>Hunter</strong>, your KubeCraft job-hunt companion.<br>If you can read this, your job emails will arrive here.</p><p style=\"color:#666\">Nothing else was sent and no job was applied to.</p>"
    digest = Digest(subject="Hunter test: your job emails work", text=text, html=html, job_ids=[], content_hash=sha256_text(f"test|{stamp}"))
    nid, why_not = _claim(db, digest, addr, "", False)
    if why_not:
        return why_not
    db.update("notifications", {"kind": "test"}, "id = ?", (nid,))
    from jobhunter.services.email import transport

    def finish(status: str, error: str | None = None, message_id: str | None = None) -> None:
        with db.tx():
            db.update("notifications", {"status": status, "error": error, "provider_message_id": message_id, "sent_at": now_iso() if status == "sent" else None}, "id = ?", (nid,))

    try:
        result = transport.send(ctx, db, to_address=addr, subject=digest.subject, text=digest.text, html=digest.html)
    except transport.TransportUncertain as exc:
        finish("uncertain", str(exc))
        return {"status": "uncertain", "error": str(exc), "notification_id": nid, "note": "Google did not confirm the test email. Check your inbox in a minute."}
    except transport.TransportError as exc:
        finish("failed", str(exc))
        return {"status": "failed", "error": str(exc), "notification_id": nid}
    finish("sent", None, result.get("message_id"))
    return {"status": "sent", "to": addr, "notification_id": nid, "note": f"Test email sent to {addr}. Check your inbox (and spam) in a minute."}


def confirm_suggested_recipient(db: Database) -> dict:
    """The person agrees to receive digests at the Gmail address they just connected."""
    addr = db.get_setting("digest.suggested_recipient")
    if not addr:
        raise DigestError("There is no suggested address. Type the address instead.")
    res = set_recipient(db, addr)
    db.delete_setting("digest.suggested_recipient")
    return res
