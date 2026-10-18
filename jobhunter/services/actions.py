"""User actions and the application pipeline, recorded as events.

Counting rules live here: saving, opening, or drafting never counts as
applying or contacting. "Applied" needs the user's confirmation or clear
email evidence. Corrections are new events that revert old ones; nothing is
deleted, nothing is double-counted.
"""

from __future__ import annotations

import json

from jobhunter.db import Database
from jobhunter.services import feedback
from jobhunter.util import now_iso

STAGES = ["prepared", "applied", "replied", "interview", "offer", "rejected", "withdrawn"]
STAGE_ORDER = {s: i for i, s in enumerate(STAGES)}
FINISHED_STAGES = {"rejected", "withdrawn", "offer"}
EVENT_KINDS = {
    "opened": "Opened the job",
    "saved": "Saved",
    "unsaved": "Removed from saved",
    "favorited": "Added to favorites",
    "unfavorited": "Removed from favorites",
    "dismissed": "Not interested",
    "undismissed": "Interested again",
    "prepared": "Prepared application materials",
    "applied": "Applied",
    "contacted": "Contacted someone",
    "reply_received": "Received a reply",
    "interview": "Interview",
    "rejected": "Rejected",
    "offer": "Offer",
    "withdrawn": "Withdrawn",
    "note": "Note",
    "correction": "Correction",
    "feedback": "Feedback",
}


class ActionError(Exception):
    pass


def _event(db: Database, kind: str, job_id: int | None, actor: str, payload: dict | None = None, occurred_at: str | None = None, application_id: int | None = None, dedupe_key: str | None = None, note: str | None = None) -> int | None:
    if dedupe_key and db.fetchone("SELECT id FROM events WHERE dedupe_key = ?", (dedupe_key,)):
        return None
    return db.insert(
        "events",
        {
            "kind": kind,
            "job_id": job_id,
            "application_id": application_id,
            "actor": actor,
            "payload_json": json.dumps(payload or {}),
            "occurred_at": occurred_at or now_iso(),
            "recorded_at": now_iso(),
            "dedupe_key": dedupe_key,
            "note": note,
        },
    )


def _require_job(db: Database, job_id: int) -> dict:
    row = db.fetchone("SELECT * FROM jobs WHERE id = ?", (job_id,))
    if not row:
        raise ActionError(f"Job {job_id} does not exist.")
    return row


def open_job(db: Database, job_id: int, actor: str = "user") -> dict:
    job = _require_job(db, job_id)
    with db.tx():
        _event(db, "opened", job_id, actor)
    url = job.get("direct_url") if job.get("direct_url_status") == "verified" else None
    return {"job_id": job_id, "url": url or job.get("apply_url") or job.get("discovery_url"), "direct_verified": bool(url)}


def save_job(db: Database, job_id: int, save: bool = True) -> dict:
    _require_job(db, job_id)
    with db.tx():
        _event(db, "saved" if save else "unsaved", job_id, "user")
    return {"job_id": job_id, "saved": save}


def favorite_job(db: Database, job_id: int, on: bool = True) -> dict:
    """Favorites are independent of saved and of notes."""
    _require_job(db, job_id)
    with db.tx():
        _event(db, "favorited" if on else "unfavorited", job_id, "user")
    return {"job_id": job_id, "favorited": on}


def dismiss_job(db: Database, job_id: int, reason_code: str | None = None, note: str | None = None, undo: bool = False) -> dict:
    _require_job(db, job_id)
    with db.tx():
        _event(db, "undismissed" if undo else "dismissed", job_id, "user", {"reason": reason_code, "note": note})
        if not undo:
            feedback.record(db, job_id, "not_interested", reason_code, note)
    return {"job_id": job_id, "dismissed": not undo, "suggestions": feedback.suggestions(db)}


def add_note(db: Database, job_id: int, text: str) -> dict:
    _require_job(db, job_id)
    app = db.fetchone("SELECT id FROM applications WHERE job_id = ?", (job_id,))
    with db.tx():
        _event(db, "note", job_id, "user", {"text": text}, application_id=app["id"] if app else None, note=text)
    return {"job_id": job_id, "noted": True}


def _get_or_create_application(db: Database, job_id: int, stage: str, ts: str) -> dict:
    app = db.fetchone("SELECT * FROM applications WHERE job_id = ?", (job_id,))
    if app:
        return app
    app_id = db.insert("applications", {"job_id": job_id, "stage": stage, "stage_changed_at": ts, "created_at": ts, "updated_at": ts})
    return db.fetchone("SELECT * FROM applications WHERE id = ?", (app_id,))


def mark_prepared(db: Database, job_id: int, cv_document_id: int | None = None) -> dict:
    """A package was made. This is not applying: the stage stays 'prepared' and no date is set."""
    _require_job(db, job_id)
    ts = now_iso()
    with db.tx():
        app = _get_or_create_application(db, job_id, "prepared", ts)
        _event(db, "prepared", job_id, "user", application_id=app["id"])
    return {"application_id": app["id"], "stage": app["stage"]}


def _bind_submission(db: Database, app: dict, ts: str, cv_document_id: int | None = None) -> None:
    """Attach the CV and package that were current when the person applied. Never overwritten later."""
    values = {}
    if not app.get("package_draft_id"):
        pkg = db.fetchone("SELECT id, content_json FROM drafts WHERE job_id = ? AND kind = 'package' ORDER BY id DESC LIMIT 1", (app["job_id"],))
        if pkg:
            values["package_draft_id"] = pkg["id"]
            if not app.get("cv_document_id") and not cv_document_id:
                cv = (json.loads(pkg["content_json"] or "{}").get("cv") or {}).get("document_id")
                if cv:
                    values["cv_document_id"] = cv
    if not app.get("cv_document_id") and cv_document_id:
        values["cv_document_id"] = cv_document_id
    if not app.get("cv_document_id") and "cv_document_id" not in values:
        cur = db.fetchone("SELECT id FROM source_documents WHERE kind = 'cv' ORDER BY version DESC, id DESC LIMIT 1")
        if cur:
            values["cv_document_id"] = cur["id"]
    if values:
        values["updated_at"] = ts
        db.update("applications", values, "id = ?", (app["id"],))


def mark_applied(db: Database, job_id: int, applied_at: str | None = None, source: str = "user", evidence: dict | None = None, cv_document_id: int | None = None, dedupe_key: str | None = None) -> dict:
    """Record an application. Manual and email confirmations are deduplicated per job."""
    _require_job(db, job_id)
    ts = now_iso()
    when = applied_at or ts
    with db.tx():
        app = _get_or_create_application(db, job_id, "applied", ts)
        already = db.fetchone("SELECT id, occurred_at FROM events WHERE job_id = ? AND kind = 'applied' AND reverted_by IS NULL", (job_id,))
        if already:
            # Same application confirmed twice (e.g. manual + receipt email): keep the earliest time, no second event.
            earliest = min(already["occurred_at"], when)
            db.update("events", {"occurred_at": earliest}, "id = ?", (already["id"],))
            db.update("applications", {"applied_at": earliest, "updated_at": ts}, "id = ?", (app["id"],))
            return {"application_id": app["id"], "stage": app["stage"], "duplicate": True}
        _event(db, "applied", job_id, source, {"evidence": evidence or {}}, occurred_at=when, application_id=app["id"], dedupe_key=dedupe_key)
        values = {"applied_at": when, "applied_source": source, "updated_at": ts}
        if STAGE_ORDER[app["stage"]] < STAGE_ORDER["applied"]:
            values.update({"stage": "applied", "stage_changed_at": ts})
        db.update("applications", values, "id = ?", (app["id"],))
        _bind_submission(db, app, ts, cv_document_id)
    return {"application_id": app["id"], "stage": "applied", "duplicate": False}


def set_stage(db: Database, job_id: int, stage: str, actor: str = "user", occurred_at: str | None = None, payload: dict | None = None, dedupe_key: str | None = None, allow_backwards: bool = False) -> dict:
    """Move the pipeline. Automated/email events never move a confirmed pipeline backwards."""
    if stage not in STAGES:
        raise ActionError(f"Unknown stage '{stage}'.")
    _require_job(db, job_id)
    ts = now_iso()
    kind = {"applied": "applied", "replied": "reply_received", "interview": "interview", "offer": "offer", "rejected": "rejected", "withdrawn": "withdrawn", "prepared": "prepared"}[stage]
    if stage == "applied" and actor == "user":
        # One path for "I applied": records the date, the CV and the package.
        return mark_applied(db, job_id, applied_at=occurred_at, source="user", evidence=payload, dedupe_key=dedupe_key)
    with db.tx():
        app = _get_or_create_application(db, job_id, stage, ts)
        current = app["stage"]
        moving_back = STAGE_ORDER[stage] < STAGE_ORDER[current] and stage not in FINISHED_STAGES
        if moving_back and not (allow_backwards and actor == "user"):
            _event(db, kind, job_id, actor, {**(payload or {}), "ignored_for_pipeline": True, "reason": f"pipeline already at {current}"}, occurred_at, app["id"], dedupe_key)
            return {"application_id": app["id"], "stage": current, "changed": False, "note": f"Kept stage '{current}'; a later-recorded '{stage}' does not move it back."}
        if actor != "user":
            # Automated or email evidence never overrides what the person decided.
            if current in FINISHED_STAGES:
                _event(db, kind, job_id, actor, {**(payload or {}), "ignored_for_pipeline": True, "reason": f"pipeline finished at {current}"}, occurred_at, app["id"], dedupe_key)
                return {"application_id": app["id"], "stage": current, "changed": False, "note": f"Kept stage '{current}': you set that yourself, so an email does not change it. You can change it any time."}
            last_user = db.fetchone(
                "SELECT occurred_at FROM events WHERE application_id = ? AND actor = 'user' AND reverted_by IS NULL AND kind IN ('applied','reply_received','interview','offer','rejected','withdrawn') ORDER BY occurred_at DESC LIMIT 1",
                (app["id"],),
            )
            if last_user and occurred_at and occurred_at < last_user["occurred_at"]:
                _event(db, kind, job_id, actor, {**(payload or {}), "ignored_for_pipeline": True, "reason": f"older than your own update at {last_user['occurred_at']}"}, occurred_at, app["id"], dedupe_key)
                return {"application_id": app["id"], "stage": current, "changed": False, "note": f"Kept stage '{current}': this email is older than your own update."}
        eid = _event(db, kind, job_id, actor, payload or {}, occurred_at, app["id"], dedupe_key)
        if eid is None:
            return {"application_id": app["id"], "stage": current, "changed": False, "note": "Duplicate event ignored."}
        values = {"stage": stage, "stage_changed_at": ts, "updated_at": ts}
        if stage == "applied" and not app.get("applied_at"):
            values.update({"applied_at": occurred_at or ts, "applied_source": actor})
        db.update("applications", values, "id = ?", (app["id"],))
        if stage == "applied":
            _bind_submission(db, app, ts)
        if stage in FINISHED_STAGES or stage == "replied":
            db.execute("UPDATE followups SET status = 'cancelled', resolved_at = ?, reason = ? WHERE application_id = ? AND status = 'pending'", (ts, f"stage changed to {stage}", app["id"]))
    return {"application_id": app["id"], "stage": stage, "changed": True}


def mark_contacted(db: Database, job_id: int, who: str, channel: str, occurred_at: str | None = None) -> dict:
    _require_job(db, job_id)
    app = db.fetchone("SELECT id FROM applications WHERE job_id = ?", (job_id,))
    with db.tx():
        _event(db, "contacted", job_id, "user", {"who": who, "channel": channel}, occurred_at, app["id"] if app else None)
    return {"job_id": job_id, "contacted": True}


def correct(db: Database, event_id: int, note: str | None = None) -> dict:
    """Revert one event (undo). The reverting event is recorded; counts update accordingly."""
    ev = db.fetchone("SELECT * FROM events WHERE id = ?", (event_id,))
    if not ev:
        raise ActionError(f"Event {event_id} not found.")
    if ev["reverted_by"]:
        return {"event_id": event_id, "already_reverted": True}
    ts = now_iso()
    with db.tx():
        cid = _event(db, "correction", ev["job_id"], "user", {"reverts": event_id, "kind": ev["kind"]}, application_id=ev["application_id"], note=note)
        db.update("events", {"reverted_by": cid}, "id = ?", (event_id,))
        if ev["application_id"] and ev["kind"] in {"applied", "reply_received", "interview", "offer", "rejected", "withdrawn"}:
            _recompute_stage(db, ev["application_id"], ts)
    return {"event_id": event_id, "reverted": True}


def _recompute_stage(db: Database, application_id: int, ts: str) -> None:
    kinds = {"applied": "applied", "reply_received": "replied", "interview": "interview", "offer": "offer", "rejected": "rejected", "withdrawn": "withdrawn", "prepared": "prepared"}
    rows = db.fetchall(
        "SELECT kind, payload_json FROM events WHERE application_id = ? AND reverted_by IS NULL AND kind IN ('prepared','applied','reply_received','interview','offer','rejected','withdrawn') ORDER BY occurred_at",
        (application_id,),
    )
    stage = "prepared"
    for r in rows:
        payload = json.loads(r["payload_json"] or "{}")
        if payload.get("ignored_for_pipeline"):
            continue
        s = kinds[r["kind"]]
        if STAGE_ORDER[s] >= STAGE_ORDER[stage] or s in FINISHED_STAGES:
            stage = s
    applied = db.fetchone("SELECT MIN(occurred_at) AS t FROM events WHERE application_id = ? AND kind = 'applied' AND reverted_by IS NULL", (application_id,))
    db.update("applications", {"stage": stage, "stage_changed_at": ts, "applied_at": applied["t"] if applied else None, "updated_at": ts}, "id = ?", (application_id,))


def history(db: Database, job_id: int) -> list[dict]:
    rows = db.fetchall("SELECT * FROM events WHERE job_id = ? ORDER BY occurred_at, id", (job_id,))
    for r in rows:
        r["label"] = EVENT_KINDS.get(r["kind"], r["kind"])
        r["payload"] = json.loads(r["payload_json"] or "{}")
    return rows
