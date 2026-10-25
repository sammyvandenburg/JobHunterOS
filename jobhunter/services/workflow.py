"""One run contract for every way Hunter runs: this session, the local worker, or a nightly routine.

The order is fixed, and each step is judged from evidence in the database, never from a plan or a promise:

  1. restore           (nightly only) the state from the person's private copy was loaded in this run
  2. read_dashboard    the page's actions were read since Hunter last wrote to the page
  3. hunt              a hunt finished after that read (stored jobs re-matched, new jobs matched)
  4. digest            today's digest was prepared, durably reserved (saved), sent, and recorded
  5. save              the state was saved after the last change
  6. publish_dashboard the page was written after the hunt (and after the digest was recorded)

`plan()` says which steps are done, which comes next, and the exact command for it. Hunter's commands
enforce the order on their own (`hunt run`, `digest prepare` and `board export` refuse while the page is
unread); this module makes the whole sequence visible so a session and a routine follow the same path and
report the same way. Nothing here changes state."""

from __future__ import annotations

from jobhunter.config import AppContext
from jobhunter.db import Database
from jobhunter.util import parse_iso

ORDER = ("restore", "read_dashboard", "hunt", "digest", "save", "publish_dashboard")


def _t(value, seq=None) -> tuple[float, int]:
    """An instant for ordering: (seconds since the epoch, sequence number); (0, 0) when unknown. Timestamps
    carry whole seconds, so two steps in the same second are ordered by the shared counter the dashboard
    reads, writes and hunts take (`board.seq`)."""
    if not value:
        return (0.0, 0)
    if isinstance(value, dict):
        seq = value.get("seq") if seq is None else seq
        value = value.get("at") or value.get("finished_at")
    d = parse_iso(str(value))
    return (d.timestamp(), int(seq or 0)) if d else (0.0, 0)


def _iso(value) -> str | None:
    if isinstance(value, dict):
        value = value.get("at") or value.get("finished_at")
    return str(value) if value else None


def evidence(db: Database) -> dict:
    """The facts every status is judged from."""
    from jobhunter.services import board, hunt, notifications

    last_run = hunt.last_run(db)
    last_digest = db.fetchone("SELECT * FROM notifications WHERE kind = 'digest' AND channel = 'email' ORDER BY id DESC LIMIT 1")
    last_sent = db.fetchone("SELECT sent_at, recipient FROM notifications WHERE kind = 'digest' AND status = 'sent' ORDER BY sent_at DESC LIMIT 1")
    last_cloud = db.fetchone("SELECT * FROM hunt_runs WHERE trigger = 'cloud' AND status != 'running' ORDER BY id DESC LIMIT 1")
    return {
        "dashboard_linked": bool(db.get_setting(board.URL_SETTING)),
        "dashboard_read": db.get_setting("board.last_import"),
        "dashboard_written": db.get_setting("board.last_export"),
        "dashboard_published": board.publish_state(db),
        "dashboard_stale": board.stale_reason(db),
        "restored": db.get_setting("cloud.last_import"),
        "saved": db.get_setting("cloud.last_export"),
        "state_repo": db.get_setting("cloud.repo_dir"),
        "last_hunt": {"id": last_run["id"], "status": last_run["status"], "trigger": last_run.get("trigger"), "started_at": last_run.get("started_at"), "finished_at": last_run.get("finished_at"), "error": last_run.get("error"), "stats": last_run.get("stats") or {}} if last_run else None,
        "email_address_confirmed": bool(notifications.recipient(db)),
        "email_transport": db.get_setting("digest.transport"),
        "last_digest": {k: last_digest.get(k) for k in ("id", "status", "created_at", "claimed_at", "sent_at", "error", "provider_message_id")} if last_digest else None,
        "last_email_sent_at": last_sent["sent_at"] if last_sent else None,
        "sent_today": notifications.sent_today(db) if notifications.recipient(db) else False,
        "last_prepare": db.get_setting("digest.last_prepare"),
        "last_cloud_run": {"status": last_cloud["status"], "finished_at": last_cloud.get("finished_at"), "tag": last_cloud.get("owner")} if last_cloud else None,
    }


def plan(ctx: AppContext, db: Database, nightly: bool = False) -> dict:
    """The ordered steps with their status (done, next, pending, skipped, blocked) and the exact command."""
    ev = evidence(db)
    steps: list[dict] = []
    linked = ev["dashboard_linked"]
    role_cloud = db.get_setting("cloud.role") == "cloud"
    t_restore = _t(ev["restored"])
    t_read = _t(ev["dashboard_read"])
    t_written = _t(ev["dashboard_written"])
    t_hunt = _t(ev["last_hunt"], (ev["last_hunt"].get("stats") or {}).get("seq")) if ev["last_hunt"] and ev["last_hunt"]["status"] != "running" else (0.0, 0)
    t_saved = _t(ev["saved"])
    last_digest = ev["last_digest"] or {}
    t_digest = _t(last_digest.get("sent_at") or last_digest.get("claimed_at") or last_digest.get("created_at")) if last_digest else (0.0, 0)

    def add(name, status, why, command=None, **extra):
        steps.append({"step": name, "status": status, "why": why, "command": command, **extra})

    # A finished cycle: the page was written after a hunt that followed a read (and the restore). Then this run is
    # complete, and the page being "unread since Hunter wrote" is simply where the next run starts.
    cycle_done = bool(t_read[0]) and t_hunt >= t_read and t_written >= t_hunt and t_read >= t_restore if linked else bool(t_hunt[0]) and t_hunt >= t_restore

    # 1. restore (a nightly run always starts from the private copy; a laptop session never restores)
    if nightly or role_cloud:
        done = bool(t_restore[0])  # a restore starts the cycle; the hunt that follows it never undoes it
        add("restore", "done" if done else "next", "State loaded from the private copy." if done else "Load the state from the private copy first.", "uv run jobhunter cloud load --restore --branch main --json", at=_iso(ev["restored"]))
    else:
        add("restore", "skipped", "This computer holds the state itself.")
    prev_done = all(s["status"] in {"done", "skipped"} for s in steps)

    # 2. read the dashboard's actions
    if not linked:
        add("read_dashboard", "skipped", "No dashboard linked yet.")
    else:
        stale = None if cycle_done else ev["dashboard_stale"]
        done = not stale and bool(t_read[0]) and (not t_restore[0] or t_read >= t_restore)
        add("read_dashboard", "done" if done else ("next" if prev_done else "pending"), "The page's actions were read after Hunter last wrote to it." if done else (stale or "Read the page's actions first (after a restore, every action is read again)."),
            "ArtifactData list collection=actions out_dir=<DIR>/in (follow next_cursor), then: uv run jobhunter board import --in <DIR>/in --listed <total> --json", at=_iso(ev["dashboard_read"]))
    prev_done = all(s["status"] in {"done", "skipped"} for s in steps)

    # 3. hunt
    hunt_status = (ev["last_hunt"] or {}).get("status")
    hunt_ok = bool(t_hunt[0]) and hunt_status in {"completed", "partial"} and (not linked or t_hunt >= t_read) and (not t_restore[0] or t_hunt >= t_restore)
    if hunt_status == "running":
        add("hunt", "pending", "A hunt is running now.", at=None)
    else:
        if hunt_ok:
            why = f"Last hunt {hunt_status} at {ev['last_hunt']['finished_at']}."
        elif ev["last_hunt"] and hunt_status not in {"completed", "partial"}:
            why = f"The last hunt {hunt_status}: {ev['last_hunt'].get('error') or 'no usable result'}. Hunt again."
        else:
            why = "Hunt after the page's actions are read."
        add("hunt", "done" if hunt_ok else ("next" if prev_done else "pending"), why, "uv run jobhunter hunt run" + (" --trigger cloud --json" if nightly else " --json"), at=_iso(ev["last_hunt"]) if hunt_ok else None)
    prev_done = all(s["status"] in {"done", "skipped"} for s in steps)

    # 4. digest: prepare -> save (durable reservation) -> send -> record
    if not ev["email_address_confirmed"]:
        add("digest", "skipped", "No confirmed email address: nothing to send.")
    elif ev["sent_today"]:
        add("digest", "done", f"Today's digest was sent at {ev['last_email_sent_at']}.", at=ev["last_email_sent_at"])
    elif (ev.get("last_prepare") or {}).get("status") == "skipped" and _t(ev["last_prepare"]) >= t_hunt:
        add("digest", "done", "Nothing new to email after this hunt: " + str(ev["last_prepare"].get("note") or ""), at=_iso(ev["last_prepare"]))
    elif last_digest.get("status") == "sending":
        add("digest", "blocked", "A digest is reserved but its outcome was never recorded. Record it (sent, failed or uncertain) before anything else is sent; never send it again blindly.", f"uv run jobhunter digest record {last_digest.get('id')} <sent|failed|uncertain> ...", at=_iso(last_digest.get("sent_at") or last_digest.get("claimed_at") or last_digest.get("created_at")))
    elif last_digest.get("status") == "uncertain" and not last_digest.get("resolved_at"):
        add("digest", "blocked", "An earlier digest may or may not have arrived. The person decides under Needs attention; nothing is resent by itself.", at=_iso(last_digest.get("sent_at") or last_digest.get("claimed_at") or last_digest.get("created_at")))
    else:
        add("digest", "next" if prev_done else "pending", "Prepare today's digest, save the state so the reservation survives, send it through the Gmail connector, record the outcome.",
            "uv run jobhunter digest prepare --json; uv run jobhunter cloud save --json; send; uv run jobhunter digest record <id> sent --message-id <id>")
    prev_done = all(s["status"] in {"done", "skipped"} for s in steps)

    # 5. save the state (whenever a state repository is linked)
    if not ev["state_repo"]:
        add("save", "skipped", "No private copy linked for the state.")
    else:
        latest_change = max(t_hunt[0], t_read[0], t_digest[0])  # a save carries no counter: whole seconds decide
        done = bool(t_saved[0]) and t_saved[0] >= latest_change
        add("save", "done" if done else ("next" if prev_done else "pending"), "State saved after the last change." if done else "Save the state so the next run (and the laptop) knows what happened.", "uv run jobhunter cloud save --json" + (" --branch main" if nightly else ""), at=_iso(ev["saved"]))
    prev_done = all(s["status"] in {"done", "skipped"} for s in steps)

    # 6. publish the projection (the page)
    if not linked:
        add("publish_dashboard", "skipped", "No dashboard linked yet.")
    else:
        written = bool(t_written[0]) and t_written >= t_hunt and t_written >= t_read and (not t_digest[0] or t_written >= t_digest)
        confirmed = written and ev["dashboard_published"]["confirmed"]
        why = "The page shows this run." if confirmed else ("Written to disk, but the write to the page was never confirmed: run the ArtifactData batch, then `uv run jobhunter board published`." if written else "Write the page after the hunt and the digest.")
        add("publish_dashboard", "done" if confirmed else ("next" if prev_done else "pending"), why, "uv run jobhunter board export --out <DIR>/out --json, then the ArtifactData batch from the manifest, then uv run jobhunter board published --json", at=ev["dashboard_published"]["published_at"] if confirmed else None)

    nxt = next((s for s in steps if s["status"] in {"next", "blocked"}), None) or next((s for s in steps if s["status"] == "pending"), None)
    return {"order": list(ORDER), "steps": steps, "next": nxt["step"] if nxt else None, "complete": nxt is None, "next_run_starts_with": "restore" if nightly else ("read_dashboard" if linked else "hunt"), "evidence": ev}


def routine_status(db: Database) -> dict:
    """What can honestly be said about unattended runs, from evidence only. A routine's schedule lives in the
    person's Claude account and is not visible here, so `next_run_at` is never invented."""
    ev = evidence(db)
    last = ev["last_cloud_run"]
    return {
        "email_configured": ev["email_address_confirmed"] and ev["email_transport"] in {"claude", "gmail", "smtp"},
        "last_delivery": {"at": ev["last_email_sent_at"], "status": (ev["last_digest"] or {}).get("status")} if ev["last_digest"] else None,
        "last_nightly_run": last,
        "next_nightly_run": None,
        "note": "The nightly schedule is set in the person's Claude account (routines page); Hunter only sees the runs that happened.",
    }
