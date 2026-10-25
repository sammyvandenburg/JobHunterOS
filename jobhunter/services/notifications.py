"""Digest composition and delivery with a backend-enforced recipient allowlist.

The only recipient a digest can ever go to is the address stored in the
``digest_email`` consent record. The model, dashboard forms, and source
content have no path to change it. Every delivery attempt is recorded;
ambiguous outcomes are marked ``uncertain`` and never blindly retried.
"""

from __future__ import annotations

import html as html_mod
import json
import re
import secrets
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


FOLLOWUP_EMAIL_SETTING = "followups.email"  # off by default: reminders show on the dashboard; email is a separate choice


def _followup_lines(db: Database) -> list[str]:
    if not db.get_setting(FOLLOWUP_EMAIL_SETTING):
        return []
    from jobhunter.services import drafts as drafts_svc

    due = drafts_svc.due_followups(db)[:5]
    return [f"Follow-up due: {f['title']} at {f['employer_name'] or 'an employer'} (you applied {str(f.get('applied_at') or '')[:10] or 'earlier'})." for f in due]


def _prepared_lines(db: Database) -> list[str]:
    """Applications Hunter prepared on its own that nobody has opened yet: one line each, never a count of
    "applications sent" (nothing was sent)."""
    from jobhunter.services import today as today_svc

    out = []
    for a in today_svc.unopened_ready(db)[:5]:
        r = db.fetchone("SELECT origin FROM requests WHERE job_id = ? AND kind = 'prepare' ORDER BY id DESC LIMIT 1", (a["job_id"],))
        if not r or r.get("origin") != "auto":
            continue
        j = db.fetchone("SELECT title, employer_name FROM jobs WHERE id = ?", (a["job_id"],))
        out.append(f"Ready to review on your dashboard: {j['title']} at {j.get('employer_name') or 'an employer'} (prepared {a['prepared_at'][:10]}; you apply, Hunter never sends).")
    return out


def _changed_lines(db: Database) -> list[str]:
    """Material changes on jobs the person saved, starred, prepared or applied to, seen in the last day."""
    from jobhunter.services import watches

    return [watches.change_line(c) for c in watches.changes(db, days=1)[:5]]


def compose(db: Database, jobs: list[dict], include_closed_note: bool = True) -> Digest:
    reminders = _followup_lines(db)
    prepared = _prepared_lines(db)
    changed = _changed_lines(db)
    opener = f"Hunter found {len(jobs)} job(s) worth a look." if jobs else ("No new jobs today. Your reminders:" if reminders else "No new jobs today.")
    lines = ["Hi,", "", opener, ""]
    html_items = []
    if prepared:
        lines += prepared + [""]
        html_items.append("<p><b>Ready to review</b><br>" + "<br>".join(html_mod.escape(r) for r in prepared) + "</p>")
    if reminders:
        lines += reminders + [""]
        html_items.append("<p><b>Reminders</b><br>" + "<br>".join(html_mod.escape(r) for r in reminders) + "</p>")
    if changed:
        lines += ["Something changed on a job you follow:"] + changed + [""]
        html_items.append("<p><b>Something changed</b><br>" + "<br>".join(html_mod.escape(r) for r in changed) + "</p>")
    for j in jobs:
        link = j.get("direct_url") if j.get("direct_url_status") == "verified" else (j.get("apply_url") or j.get("discovery_url"))
        b, flabel = freshness.bucket(j)
        lines += [
            f"- {j['title']} at {j.get('employer_name') or 'unknown employer'} ({group_label(j['match_group'])})",
            f"  Location: {j.get('location_text') or 'not stated'}" + (f" · {j['remote_restriction']}" if j.get("remote_restriction") else ""),
            f"  Salary: {_salary(j)}",
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
                sal=_esc(_salary(j)),
                fl=_esc(flabel),
                why=_esc(j.get("explanation_text") or ""),
                link=f'<a href="{_esc(link)}">{_esc(link)}</a>' if link else "no link available",
            )
        )
    lines += ["Open your dashboard on the computer where Hunter runs to save, dismiss, or prepare an application.", "", "Hunter, your JobHunterOS assistant by KubeCraft"]
    text = "\n".join(lines)
    html = "<p>Hi,</p><p>{}</p><ul>{}</ul><p>Open your dashboard on the computer where Hunter runs to act on these.</p><p>Hunter, your JobHunterOS assistant by KubeCraft</p>".format(html_mod.escape(opener), "".join(html_items))
    ids = [int(j["id"]) for j in jobs]
    subject = f"JobHunterOS: {len(jobs)} job(s) worth a look" if jobs else ("JobHunterOS: your follow-up reminders" if reminders else (f"JobHunterOS: {len(prepared)} application{'s' if len(prepared) != 1 else ''} ready to review" if prepared else "JobHunterOS: something changed on a job you follow"))
    seed = json.dumps(sorted(ids)) if ids else "reminders|" + now_iso()[:10] + "|" + "|".join(reminders + prepared + changed)
    return Digest(subject=subject, text=text, html=html, job_ids=ids, content_hash=sha256_text(seed))


def _salary(j: dict) -> str:
    from jobhunter.services.normalize import salary_label

    return salary_label(j.get("salary_min"), j.get("salary_max"), j.get("salary_currency"), j.get("salary_period"), j.get("salary_basis"), j.get("salary_text")) or "Not listed"


def _esc(s: str | None) -> str:
    import html

    return html.escape(s or "")


STALE_SENDING_MINUTES = 15
AUTO_RETRY_AFTER_MINUTES = 30  # a failed scheduled digest is tried again after this
AUTO_RETRY_MAX_ATTEMPTS = 3  # and at most this many times in total


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


def failed_digest(db: Database) -> dict | None:
    """The most recent digest that failed and nobody has dealt with yet (last 48 hours)."""
    since = (now() - timedelta(hours=48)).isoformat()
    return db.fetchone("SELECT * FROM notifications WHERE kind = 'digest' AND status = 'failed' AND resolved_at IS NULL AND created_at >= ? ORDER BY id DESC LIMIT 1", (since,))


def retry_plan(db: Database, row: dict) -> dict:
    """Will Hunter try this failed digest again by itself? Only when sending can work and the attempt budget is left."""
    from jobhunter.services.email import gmail, transport

    attempts = int(row.get("attempts") or 0)
    if db.get_setting("digest.transport") == "claude":
        return {"will_retry": False, "reason": "Claude sends the emails in this setup and tries again on its next run"}
    if attempts >= AUTO_RETRY_MAX_ATTEMPTS:
        return {"will_retry": False, "reason": f"Hunter already tried {attempts} times and will not try again by itself"}
    if not transport.describe(db)["configured"]:
        return {"will_retry": False, "reason": "Gmail is not connected, so Hunter cannot try again by itself"}
    g = gmail.describe(db)["send"]
    if g["connected"] and g.get("last_error"):
        return {"will_retry": False, "reason": "The Gmail connection stopped working; connect Gmail again first"}
    last_try = parse_iso(row.get("claimed_at") or row.get("created_at"))
    wait = max(0, AUTO_RETRY_AFTER_MINUTES - int(((now() - last_try).total_seconds() // 60) if last_try else 0))
    return {"will_retry": True, "reason": f"Hunter will try again by itself in about {wait} minutes" if wait else "Hunter will try again by itself within a minute", "due": wait == 0}


def retry_due(db: Database) -> dict | None:
    """A failed digest the scheduler may try again right now."""
    row = failed_digest(db)
    if not row:
        return None
    plan = retry_plan(db, row)
    return row if plan["will_retry"] and plan.get("due") else None


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
    with db.tx():
        # Earlier failed digests are covered now: their jobs were still pending and are in this one.
        db.execute("UPDATE notifications SET resolved_at = ?, resolution = 'superseded' WHERE kind = 'digest' AND status = 'failed' AND resolved_at IS NULL AND id != ?", (now_iso(), nid))
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


def prepare_handoff(ctx: AppContext, db: Database) -> dict:
    """Compose the next digest and claim it in the outbox, for Claude to send through its own Gmail connector.
    Python never sends in this mode and never sees a token. The recipient in the result is the only address
    Claude may use; `record_handoff` closes the row with what really happened."""
    addr = recipient(db)
    if not addr:
        raise DigestError("No confirmed digest address. Confirm one first (`jobhunter digest address you@example.com`).")
    if sent_today(db):
        return _prepared(db, {"status": "skipped", "note": "Already emailed today. One digest a day; the rest waits for tomorrow."})
    from jobhunter.services import board

    stale = board.stale_reason(db)
    if stale:
        # Never email a job the person marked applied or not interested on their dashboard.
        return {"status": "blocked", "note": stale}
    from jobhunter.services import board as _board

    _board.apply_unresolved(db)  # a page action whose job arrived with this hunt counts before anything is emailed
    jobs = pending_jobs(db)
    if not jobs and not _followup_lines(db) and not _prepared_lines(db) and not _changed_lines(db):
        return _prepared(db, {"status": "skipped", "note": "Nothing new worth emailing."})
    digest = compose(db, jobs)
    path = ctx.paths.exports_dir / "digests" / f"digest-{now_iso().replace(':', '')}.txt"
    write_private_file(path, digest.text.encode("utf-8"))
    nid, why_not = _claim(db, digest, addr, str(path), False)
    if why_not:
        return why_not
    db.update("notifications", {"channel": "email", "error": None}, "id = ?", (nid,))
    return {"status": "ready", "notification_id": nid, "to": addr, "subject": digest.subject, "text": digest.text, "html": digest.html, "content_hash": digest.content_hash, "jobs": digest.job_ids, "rules": ["Send to exactly this address, nothing else, nobody in cc or bcc.", "Send the subject, text and html as given.", "Then run `jobhunter digest record <notification_id> sent --message-id <id>`; on an error `failed --error \"...\"`; if you do not know whether it went out, `uncertain`."]}


def _prepared(db: Database, result: dict) -> dict:
    """Evidence that a digest was considered after the last hunt, so the run contract can tell 'nothing to send' from 'not tried'."""
    db.set_setting("digest.last_prepare", {"at": now_iso(), "status": result.get("status"), "note": result.get("note")})
    return result


def sent_today(db: Database) -> bool:
    """At most one digest per local day, judged from the email history itself (it travels with the cloud state)."""
    from zoneinfo import ZoneInfo

    row = db.fetchone("SELECT MAX(sent_at) AS t FROM notifications WHERE kind = 'digest' AND status = 'sent'")
    last = parse_iso(row["t"]) if row and row["t"] else None
    if not last:
        return False
    sched = db.fetchone("SELECT timezone FROM schedule WHERE id = 1") or {}
    prefs = db.get_setting("preferences") or {}
    try:
        tz = ZoneInfo(sched.get("timezone") or prefs.get("timezone") or "UTC")
    except Exception:
        tz = ZoneInfo("UTC")
    return last.astimezone(tz).date() == now().astimezone(tz).date()


def record_handoff(db: Database, notification_id: int, outcome: str, message_id: str | None = None, error: str | None = None) -> dict:
    """What happened to a digest Claude sent (or did not). Only a claimed ('sending') row can be recorded."""
    if outcome not in {"sent", "failed", "uncertain"}:
        raise DigestError("Outcome must be sent, failed, or uncertain.")
    row = db.fetchone("SELECT * FROM notifications WHERE id = ?", (notification_id,))
    if not row:
        raise DigestError(f"Digest {notification_id} not found.")
    if row["status"] != "sending":
        raise DigestError(f"Digest {notification_id} is already recorded as {row['status']}; nothing changed.")
    with db.tx():
        db.update("notifications", {"status": outcome, "error": error if outcome != "sent" else None, "provider_message_id": message_id, "sent_at": now_iso() if outcome == "sent" else None}, "id = ?", (notification_id,))
        if outcome == "sent":
            db.execute("UPDATE notifications SET resolved_at = ?, resolution = 'superseded' WHERE kind = 'digest' AND status = 'failed' AND resolved_at IS NULL AND id != ?", (now_iso(), notification_id))
            db.execute("UPDATE schedule SET last_digest_at = ? WHERE id = 1", (now_iso(),))
    return {"notification_id": notification_id, "status": outcome, "note": {"sent": "Recorded as sent.", "failed": "Recorded as failed. Hunter will offer it again under Needs attention.", "uncertain": "Recorded as uncertain. It will not be sent again by itself; the person decides under Needs attention."}[outcome]}


def send_test(ctx: AppContext, db: Database) -> dict:
    """A short email to the confirmed address so the person can see that sending works.
    Goes through the same transport, allowlist and outbox as real digests."""
    addr = recipient(db)
    if not addr:
        raise DigestError("Confirm the address first.")
    stamp = now_iso()
    text = (
        "Hi,\n\nThis is a test from Hunter, your JobHunterOS assistant by KubeCraft.\n"
        "If you can read this, your job emails will arrive here.\n\n"
        f"Sent {stamp[:16].replace('T', ' ')} UTC. Nothing else was sent and no job was applied to.\n"
    )
    html = "<p>Hi,</p><p>This is a test from <strong>Hunter</strong>, your KubeCraft job-hunt companion.<br>If you can read this, your job emails will arrive here.</p><p style=\"color:#666\">Nothing else was sent and no job was applied to.</p>"
    # Every press is its own test email: the outbox key must not collide for two presses in the same second.
    digest = Digest(subject="Hunter test: your job emails work", text=text, html=html, job_ids=[], content_hash=sha256_text(f"test|{stamp}|{secrets.token_hex(8)}"))
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
