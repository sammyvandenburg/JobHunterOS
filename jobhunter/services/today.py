"""Today: the small queue Hunter keeps for the person, and the bounded automation behind it.

Five groups, one action each: Ready to review, Needs your answer, Follow-up due, Something changed, Needs
attention. Automation modes: "on_request" (Hunter prepares only what the person asks for), "prepare_best" (after a
search, Hunter also prepares the best unprepared good matches, within limits), "assisted_apply" (reserved for the
assisted lane; refused until that lane exists in this build). Limits are workload controls, not proven optimal
numbers: at most N new preparations a day and M prepared applications nobody has opened. Preparations are never
applications: nothing here submits anything or writes to an employer."""

from __future__ import annotations

import json
from datetime import timedelta

from jobhunter.config import AppContext
from jobhunter.db import Database
from jobhunter.services import drafts as drafts_svc
from jobhunter.services import hunt, notifications, views, workspace
from jobhunter.services.board import job_key
from jobhunter.util import now, now_iso

MODE_SETTING = "automation.mode"
LIMITS_SETTING = "automation.limits"
MODES = ("on_request", "prepare_best", "assisted_apply")
MODE_LABELS = {"on_request": "On request", "prepare_best": "Prepare my best matches", "assisted_apply": "Assisted Apply"}
DEFAULT_LIMITS = {"preparations_per_day": 3, "unopened_ready": 5, "digests_per_day": 1}
MAX_LIMIT = 20


class TodayError(Exception):
    pass


def settings(db: Database) -> dict:
    mode = db.get_setting(MODE_SETTING) or "on_request"
    limits = {**DEFAULT_LIMITS, **(db.get_setting(LIMITS_SETTING) or {})}
    return {"mode": mode, "mode_label": MODE_LABELS.get(mode, mode), "limits": limits, "assisted_apply_available": assisted_apply_available(db)}


def assisted_apply_available(db: Database) -> bool:
    """The assisted lane is a separate slice; until it registers itself here the mode cannot be chosen."""
    return bool(db.get_setting("capabilities.assisted_apply"))


def set_mode(db: Database, mode: str) -> dict:
    if mode not in MODES:
        raise TodayError(f"Unknown mode '{mode}'. Choose on_request, prepare_best or assisted_apply.")
    if mode == "assisted_apply" and not assisted_apply_available(db):
        raise TodayError("Assisted Apply is not available in this build yet. Choose prepare_best: Hunter prepares, you apply.")
    db.set_setting(MODE_SETTING, mode)
    return settings(db)


def set_limits(db: Database, preparations_per_day: int | None = None, unopened_ready: int | None = None) -> dict:
    cur = {**DEFAULT_LIMITS, **(db.get_setting(LIMITS_SETTING) or {})}
    for key, val in (("preparations_per_day", preparations_per_day), ("unopened_ready", unopened_ready)):
        if val is None:
            continue
        if not (0 <= int(val) <= MAX_LIMIT):
            raise TodayError(f"{key} must be between 0 and {MAX_LIMIT}.")
        cur[key] = int(val)
    cur["digests_per_day"] = 1  # one digest a day is a product rule, not a setting
    db.set_setting(LIMITS_SETTING, cur)
    return settings(db)


# ---------------------------------------------------------------------------
# Bounded auto-preparation: the same request chain the page uses, never a second path
# ---------------------------------------------------------------------------
def _today_start() -> str:
    return now().strftime("%Y-%m-%dT00:00:00")


def prepared_today(db: Database) -> int:
    return db.fetchone("SELECT COUNT(*) AS n FROM requests WHERE kind = 'prepare' AND origin = 'auto' AND requested_at >= ?", (_today_start(),))["n"]


def unopened_ready(db: Database) -> list[dict]:
    """Prepared applications the person has not opened yet (page or command line) and has not applied to."""
    return db.fetchall(
        "SELECT a.job_id, a.opened_at, d.created_at AS prepared_at FROM applications a JOIN drafts d ON d.application_id = a.id AND d.kind = 'package' AND d.status = 'draft' "
        "WHERE a.stage = 'prepared' AND a.applied_at IS NULL AND a.opened_at IS NULL ORDER BY d.created_at DESC"
    )


def candidates(db: Database, exclude: set[int]) -> list[dict]:
    """Good matches, best first, that nobody prepared or asked about: current selection only (so dismissed,
    applied, closed and out-of-window jobs never qualify)."""
    out = []
    for it in views.current_recommended(db):
        if it.get("group") != "good" or it["id"] in exclude:
            continue
        if db.fetchone("SELECT id FROM applications WHERE job_id = ?", (it["id"],)) or db.fetchone("SELECT id FROM requests WHERE job_id = ? AND kind = 'prepare'", (it["id"],)):
            continue
        out.append(it)
    return out


def auto_prepare(ctx: AppContext, db: Database) -> dict:
    """After a search: prepare the best unprepared good matches, within the day's and the queue's limits, through
    the same requests the page makes (origin "auto"). Requested work always goes first (`workspace.run_pending`).
    Returns what happened, including why nothing was prepared."""
    s = settings(db)
    out = {"mode": s["mode"], "prepared": [], "count": 0, "limits": s["limits"], "note": None}
    if s["mode"] not in {"prepare_best", "assisted_apply"}:
        out["note"] = "Automatic preparation is off (mode: on request)."
        return out
    pending = workspace.list_requests(db, "requested")
    if pending:
        out["note"] = f"{len(pending)} request(s) from you still waiting: those go first."
        return out
    done_today = prepared_today(db)
    unopened = unopened_ready(db)
    room_day = max(0, int(s["limits"]["preparations_per_day"]) - done_today)
    room_queue = max(0, int(s["limits"]["unopened_ready"]) - len(unopened))
    room = min(room_day, room_queue)
    out.update({"prepared_today": done_today, "unopened_ready": len(unopened)})
    if room <= 0:
        out["note"] = f"Limit reached: {done_today} prepared today (max {s['limits']['preparations_per_day']}), {len(unopened)} ready and unopened (max {s['limits']['unopened_ready']})." if room_day <= 0 or room_queue <= 0 else None
        return out
    try:
        drafts_svc._require_ready_profile(db)
    except drafts_svc.DraftError as exc:
        out["note"] = str(exc)
        return out
    for it in candidates(db, set())[:room]:
        key = job_key(db.fetchone("SELECT canonical_key FROM jobs WHERE id = ?", (it["id"],))["canonical_key"])
        row = workspace.request(db, it["id"], "prepare", {"origin": "auto"}, job_key=key, origin="auto")
        res = workspace.run_request(ctx, db, row["request_id"])
        out["prepared"].append({"job_id": it["id"], "key": key, "title": it["title"], "employer": it["employer"], "state": res["state"], "message": res.get("message")})
    out["count"] = sum(1 for p in out["prepared"] if p["state"] == "ready")
    if not out["prepared"]:
        out["note"] = "No unprepared good match to prepare."
    return out


def mark_opened(db: Database, job_id: int, when: str | None = None) -> bool:
    row = db.fetchone("SELECT id, opened_at FROM applications WHERE job_id = ?", (job_id,))
    if not row or row.get("opened_at"):
        return False
    db.update("applications", {"opened_at": (when or now_iso())[:25]}, "id = ?", (row["id"],))
    return True


# ---------------------------------------------------------------------------
# Today: five groups, one action each, only from what the data shows
# ---------------------------------------------------------------------------
def _job_bits(db: Database, job_id: int) -> dict:
    j = db.fetchone("SELECT id, canonical_key, title, employer_name FROM jobs WHERE id = ?", (job_id,))
    return {"key": job_key(j["canonical_key"]), "title": j["title"], "employer": j.get("employer_name") or "an employer"} if j else {"key": None, "title": "a job", "employer": "an employer"}


def _item(kind: str, text: str, action_label: str, key: str | None = None, command: str | None = None, **extra) -> dict:
    return {"kind": kind, "text": text, "action": {"label": action_label, "key": key, "command": command}, **extra}


def today(ctx: AppContext, db: Database) -> dict:
    ready: list[dict] = []
    for a in db.fetchall(
        "SELECT a.job_id, a.opened_at, d.created_at AS prepared_at, r.origin FROM applications a JOIN drafts d ON d.application_id = a.id AND d.kind = 'package' AND d.status = 'draft' "
        "LEFT JOIN requests r ON r.job_id = a.job_id AND r.kind = 'prepare' WHERE a.stage = 'prepared' AND a.applied_at IS NULL ORDER BY a.opened_at IS NOT NULL, d.created_at DESC"
    ):
        b = _job_bits(db, a["job_id"])
        rd = workspace.readiness(db, a["job_id"])
        who = "Hunter prepared" if a.get("origin") == "auto" else "Prepared"
        ready.append(_item("ready", f"{b['title']} at {b['employer']}: {rd['label'].lower()}. {who} {a['prepared_at'][:10]}.", "Open", key=b["key"], readiness=rd["state"], opened=bool(a.get("opened_at")), prepared_at=a["prepared_at"]))
    needs: list[dict] = []
    for r in db.fetchall("SELECT r.request_id, r.kind, r.message, r.job_id FROM requests r WHERE r.state = 'needs_input' ORDER BY r.id DESC LIMIT 10"):
        b = _job_bits(db, r["job_id"]) if r.get("job_id") else {"key": None, "title": "", "employer": ""}
        needs.append(_item("needs_input", f"{b['title']}: {r['message']}" if b["title"] else str(r["message"]), "Answer", key=b["key"], command=f"jobhunter work run {r['request_id']}"))
    for a in db.fetchall("SELECT a.job_id FROM applications a WHERE a.stage = 'prepared' AND a.applied_at IS NULL ORDER BY a.id DESC LIMIT 20"):
        mine = {t["key"] for t in workspace.user_texts(db, a["job_id"], "answer") if (t.get("text") or "").strip()}
        open_qs = [q for q in workspace.question_rows(db, a["job_id"]) if q["needs_user"] and q["id"] not in mine]
        if open_qs:
            b = _job_bits(db, a["job_id"])
            needs.append(_item("question", f"{b['title']} at {b['employer']}: {len(open_qs)} question{'s' if len(open_qs) != 1 else ''} from the form only you can answer ({open_qs[0]['text'][:60]}{'…' if len(open_qs[0]['text']) > 60 else ''}).", "Answer", key=b["key"]))
    from jobhunter.services.email import linking

    queue = linking.review_queue(db)
    for s in queue["suggested"][:5]:
        words = {"receipt": "an application receipt", "rejection": "a rejection", "interview": "an interview invitation", "offer": "an offer", "recruiter": "a recruiter's message"}.get(s["kind"], "a status change")
        b = _job_bits(db, s["job_id"]) if s.get("job_id") else {"key": None, "title": None, "employer": None}
        text = f"An email from {s['from'] or 'someone'} looks like {words}" + (f" for {s['job']}" if s.get("job") else ", but Hunter cannot tell which job") + ". Is it?"
        needs.append(_item("email_review", text, "Confirm or dismiss", key=b["key"], command=f"jobhunter email confirm {s['email_id']} {s['job_id'] or '<job>'}  /  jobhunter email reject {s['email_id']}", email_id=s["email_id"]))
    conflicts = db.get_setting("preferences.conflicts") or {}
    if conflicts:
        needs.append(_item("preference_conflict", f"Your preferences were changed on two computers: {', '.join(list(conflicts)[:3])}. Which is right?", "Review", command="jobhunter prefs show --json"))
    followups: list[dict] = []
    for f in drafts_svc.due_followups(db)[:10]:
        b = _job_bits(db, f["job_id"])
        followups.append(_item("followup", f"{b['title']} at {b['employer']}: follow-up was due {f['due_at'][:10]}.", "Open", key=b["key"]))
    changed: list[dict] = []
    for a in db.fetchall("SELECT a.job_id FROM applications a WHERE a.applied_at IS NULL ORDER BY a.id DESC LIMIT 30"):
        job = db.fetchone("SELECT * FROM jobs WHERE id = ?", (a["job_id"],))
        pkg = drafts_svc.current_package(db, a["job_id"])
        if not job or not pkg:
            continue
        inputs = pkg.get("inputs") or {}
        b = _job_bits(db, a["job_id"])
        if inputs.get("job") and inputs["job"] != job.get("description_hash"):
            changed.append(_item("job_changed", f"{b['title']} at {b['employer']}: the posting changed since Hunter prepared it. Your wording is kept.", "Prepare again", key=b["key"], command=f"jobhunter work request {a['job_id']} prepare"))
        elif job.get("availability") in {"closed", "gone", "missing"}:
            changed.append(_item("job_gone", f"{b['title']} at {b['employer']}: no longer listed where Hunter found it (last checked {(job.get('last_checked_at') or '')[:10]}). Not the same as closed.", "Open", key=b["key"]))
    for a in queue["applied_by_itself"][:5]:
        changed.append(_item("email_applied", f"{a['job']}: marked {a['stage']} from an email ({a['subject'] or a['from']}, {(a['received_at'] or '')[:10]}). Undo if that is wrong.", "Undo", key=_job_bits(db, a["job_id"])["key"], command=f"jobhunter email unlink {a['email_id']}", email_id=a["email_id"]))
    attention: list[dict] = []
    last = hunt.last_run(db)
    if last and last.get("status") in {"failed", "no_sources", "aborted"}:
        attention.append(_item("hunt_failed", "The last search stopped before it finished. Your results are saved.", "Search again", command="jobhunter hunt run"))
    stats = (last or {}).get("stats") or {}
    if last and stats.get("sources_failed"):
        attention.append(_item("sources_failed", f"{stats['sources_failed']} site{'s' if stats['sources_failed'] != 1 else ''} couldn't be checked in the last search (not closed, not empty: unchecked).", "See sources", command="jobhunter sources health"))
    for r in db.fetchall("SELECT r.request_id, r.kind, r.message, r.job_id FROM requests r WHERE r.state = 'failed' ORDER BY r.id DESC LIMIT 5"):
        b = _job_bits(db, r["job_id"]) if r.get("job_id") else {"key": None, "title": "", "employer": ""}
        attention.append(_item("request_failed", f"Hunter could not {r['kind'].replace('_', ' ')}{(' for ' + b['title']) if b['title'] else ''}: {r['message']}", "Retry", key=b["key"], command=f"jobhunter work request {r['job_id']} {r['kind']}" if r.get("job_id") else None))
    for r in db.fetchall("SELECT run_key, job_id, error FROM assisted_runs WHERE state = 'uncertain' ORDER BY id DESC LIMIT 3"):
        b = _job_bits(db, r["job_id"])
        attention.append(_item("assisted_uncertain", f"{b['title']} at {b['employer']}: Hunter pressed submit but could not confirm it went through. Check the site or your inbox.", "Resolve", key=b["key"], command=f"jobhunter assisted resolve {r['job_id']} submitted|not_submitted"))
    if notifications.failed_digest(db):
        attention.append(_item("digest_failed", "The last job email could not be sent.", "Retry", command="jobhunter digest send"))
    if notifications.unresolved(db):
        attention.append(_item("digest_uncertain", "A job email may not have arrived. Check your inbox and tell Hunter.", "Review", command="jobhunter digest status"))
    from jobhunter.services import board

    pub = board.publish_state(db)
    if pub.get("exported_at") and not pub.get("confirmed"):
        attention.append(_item("not_published", "The dashboard was written on this computer but the write to the page was never confirmed.", "Sync", command="jobhunter board export"))
    stale = board.stale_reason(db)
    if stale:
        attention.append(_item("page_unread", "Your dashboard's actions have not been read since Hunter last wrote to it.", "Sync", command="jobhunter board import"))
    s = settings(db)
    auto = {"mode": s["mode"], "mode_label": s["mode_label"], "limits": s["limits"], "prepared_today": prepared_today(db), "unopened_ready": len(unopened_ready(db))}
    groups = [
        {"id": "ready", "title": "Ready to review", "items": ready[:10]},
        {"id": "needs_answer", "title": "Needs your answer", "items": needs[:10]},
        {"id": "followup", "title": "Follow-up due", "items": followups},
        {"id": "changed", "title": "Something changed", "items": changed[:10]},
        {"id": "attention", "title": "Needs attention", "items": attention[:8]},
    ]
    total = sum(len(g["items"]) for g in groups)
    return {"schema": 1, "at": now_iso(), "groups": groups, "total": total, "automation": auto, "empty_text": "Nothing waiting for you today." if not total else None, "since": (now() - timedelta(days=1)).isoformat()}


def text_view(t: dict) -> str:
    lines = [f"Today ({t['at'][:16]}) · mode: {t['automation']['mode_label']}"]
    for g in t["groups"]:
        if not g["items"]:
            continue
        lines.append(f"\n{g['title']} ({len(g['items'])})")
        for it in g["items"]:
            act = it["action"]
            how = f" → {act['label']}" + (f" ({act['command']})" if act.get("command") else "")
            lines.append(f"- {it['text']}{how}")
    if not t["total"]:
        lines.append(t["empty_text"])
    return "\n".join(lines)


def to_json(t: dict) -> str:
    return json.dumps(t, indent=2)
