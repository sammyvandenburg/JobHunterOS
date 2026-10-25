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
    "followup": "Follow-up reminder",
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


def save_job(db: Database, job_id: int, save: bool = True, occurred_at: str | None = None) -> dict:
    """`occurred_at` keeps the original time when a decision made elsewhere (the page, a cloud run) is replayed here."""
    _require_job(db, job_id)
    with db.tx():
        _event(db, "saved" if save else "unsaved", job_id, "user", occurred_at=occurred_at)
    return {"job_id": job_id, "saved": save}


def favorite_job(db: Database, job_id: int, on: bool = True, occurred_at: str | None = None) -> dict:
    """Favorites are independent of saved and of notes."""
    _require_job(db, job_id)
    with db.tx():
        _event(db, "favorited" if on else "unfavorited", job_id, "user", occurred_at=occurred_at)
    return {"job_id": job_id, "favorited": on}


def dismiss_job(db: Database, job_id: int, reason_code: str | None = None, note: str | None = None, undo: bool = False, occurred_at: str | None = None) -> dict:
    _require_job(db, job_id)
    with db.tx():
        _event(db, "undismissed" if undo else "dismissed", job_id, "user", {"reason": reason_code, "note": note}, occurred_at=occurred_at)
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


def _bind_submission(db: Database, app: dict, ts: str, cv_document_id: int | None = None, cv_choice: str | None = None, evidence: dict | None = None, destination: str | None = None) -> None:
    """Attach the CV and package that were current when the person applied, plus a snapshot of what was prepared.
    Never overwritten later. `cv_choice`: "prepared" (the version in the package), a CV variant key ("another
    CV": its current version), or "unknown" ("not sure": no version is invented)."""
    from jobhunter.services import documents as docs

    values = {}
    if not app.get("submitted_cv_choice"):
        choice = cv_choice or "prepared"
        if choice == "unknown":
            values.update({"submitted_cv_choice": "unknown", "submitted_cv_revision_key": None})
        elif isinstance(choice, str) and choice.startswith("rev"):
            # The exact version the person saw at confirmation: that one, or nothing. Never a quiet substitute.
            r = docs.cv_revision(db, choice)
            if r and not r.get("archived_at_variant"):
                v = db.fetchone("SELECT archived_at FROM cv_variants WHERE id = ?", (r["variant_id"],))
                if v and v.get("archived_at"):
                    raise ActionError(f"The CV version you chose ({r['label']}, version {r['number']}) is archived. Bring it back or choose another CV.")
                values.update({"submitted_cv_choice": "exact", "submitted_cv_revision_key": r["revision_key"]})
                cv_document_id = cv_document_id or r["document_id"]
            else:
                raise ActionError("That CV version is not known here. Choose the CV again.")
        elif choice == "prepared":
            sel = db.fetchone("SELECT cv_variant_key FROM applications WHERE id = ?", (app["id"],))
            pkg = db.fetchone("SELECT content_json FROM drafts WHERE job_id = ? AND kind = 'package' ORDER BY id DESC LIMIT 1", (app["job_id"],))
            rev = ((json.loads(pkg["content_json"] or "{}").get("cv") or {}).get("revision_key")) if pkg else None
            if not rev:
                try:
                    v = docs.get_cv(db, sel["cv_variant_key"], with_revisions=False) if sel and sel.get("cv_variant_key") else docs.default_cv(db)
                    rev = v["revision_key"] if v else None
                except docs.DocumentError:
                    rev = None
            values.update({"submitted_cv_choice": "prepared" if rev else "unknown", "submitted_cv_revision_key": rev})
            if rev:
                r = docs.cv_revision(db, rev)
                if r:
                    cv_document_id = cv_document_id or r["document_id"]
        else:
            try:
                v = docs.get_cv(db, choice, with_revisions=False)
                values.update({"submitted_cv_choice": "other", "submitted_cv_revision_key": v["revision_key"]})
                cv_document_id = cv_document_id or v["document_id"]
            except docs.DocumentError:
                values.update({"submitted_cv_choice": "unknown", "submitted_cv_revision_key": None})
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
    if not app.get("submission_json"):
        values["submission_json"] = json.dumps(_submission_snapshot(db, app, values, ts, evidence=evidence, destination=destination))
        values["submitted_destination"] = destination or (db.fetchone("SELECT COALESCE(apply_url, direct_url, discovery_url) AS d FROM jobs WHERE id = ?", (app["job_id"],)) or {}).get("d")
    if values:
        values["updated_at"] = ts
        db.update("applications", values, "id = ?", (app["id"],))


def _submission_snapshot(db: Database, app: dict, values: dict, ts: str, evidence: dict | None = None, destination: str | None = None) -> dict:
    """What was prepared when the person said "I applied": the vacancy as stored, the destination, the package
    version, the answers they confirmed (their wording, never Hunter's drafts), the messages confirmed sent, the
    CV version with its hash, and the evidence. Kept as history, never recomputed; three states stay distinct:
    unknown (not sure), not recorded (nothing was saved), explicitly empty (the person cleared it)."""
    from jobhunter.services import documents as docs
    from jobhunter.services import workspace

    job = db.fetchone("SELECT title, employer_name, description_hash, apply_url, direct_url, discovery_url, location_text, salary_text, posted_at, last_checked_at FROM jobs WHERE id = ?", (app["job_id"],)) or {}
    pkg_id = values.get("package_draft_id") or app.get("package_draft_id")
    pkg = db.fetchone("SELECT id, content_json, created_at FROM drafts WHERE id = ?", (pkg_id,)) if pkg_id else None
    content = json.loads(pkg["content_json"] or "{}") if pkg else {}
    answers = {}
    for r in workspace.user_texts(db, app["job_id"], "answer"):
        answers[r["question"] or r["key"]] = {"text": r.get("text") or "", "saved_at": r.get("updated_at"), "state": "explicitly_empty" if not (r.get("text") or "") else "confirmed"}
    texts = {k: {"text": t["text"], "saved_at": t.get("updated_at")} for k in ("intro", "letter") if (t := workspace.user_text(db, app["job_id"], k, k)) and t.get("text")}
    sent = [{"draft_id": d["id"], "kind": d["kind"], "sent_confirmed_at": d["sent_confirmed_at"]} for d in db.fetchall("SELECT id, kind, sent_confirmed_at FROM drafts WHERE job_id = ? AND status = 'sent_confirmed'", (app["job_id"],))]
    rev_key = values.get("submitted_cv_revision_key") or app.get("submitted_cv_revision_key")
    rev = docs.cv_revision(db, rev_key) if rev_key else None
    return {
        "at": ts,
        "vacancy": job,
        "destination": destination or job.get("apply_url") or job.get("direct_url") or job.get("discovery_url"),
        "package": {"draft_id": pkg_id, "version": content.get("version"), "created_at": pkg["created_at"] if pkg else None, "inputs_hash": content.get("inputs_hash")} if pkg else None,
        "answers": answers,
        "answers_state": "confirmed" if any(a["state"] == "confirmed" for a in answers.values()) else ("explicitly_empty" if answers else "not_recorded"),
        "texts": texts,
        "messages_sent": sent,
        "cv": {"choice": values.get("submitted_cv_choice") or app.get("submitted_cv_choice") or "unknown", "revision_key": rev_key, "sha256": rev["sha256"] if rev else None, "filename": rev["original_filename"] if rev else None, "label": rev["label"] if rev else None, "number": rev["number"] if rev else None},
        "evidence": evidence or {"source": "user"},
    }


def mark_applied(db: Database, job_id: int, applied_at: str | None = None, source: str = "user", evidence: dict | None = None, cv_document_id: int | None = None, dedupe_key: str | None = None, cv_choice: str | None = None) -> dict:
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
            if cv_choice and not app.get("submitted_cv_choice"):
                _bind_submission(db, app, ts, cv_document_id, cv_choice)  # a late answer to "which CV?" still binds once
            return {"application_id": app["id"], "stage": app["stage"], "duplicate": True}
        _event(db, "applied", job_id, source, {"evidence": evidence or {}}, occurred_at=when, application_id=app["id"], dedupe_key=dedupe_key)
        values = {"applied_at": when, "applied_source": source, "updated_at": ts}
        if STAGE_ORDER[app["stage"]] < STAGE_ORDER["applied"]:
            values.update({"stage": "applied", "stage_changed_at": ts})
        db.update("applications", values, "id = ?", (app["id"],))
        _bind_submission(db, app, ts, cv_document_id, cv_choice, evidence={"source": source, **(evidence or {})})
    return {"application_id": app["id"], "stage": "applied", "duplicate": False}


def set_stage(db: Database, job_id: int, stage: str, actor: str = "user", occurred_at: str | None = None, payload: dict | None = None, dedupe_key: str | None = None, allow_backwards: bool = False, cv_choice: str | None = None) -> dict:
    """Move the pipeline. Automated/email events never move a confirmed pipeline backwards."""
    if stage not in STAGES:
        raise ActionError(f"Unknown stage '{stage}'.")
    _require_job(db, job_id)
    ts = now_iso()
    kind = {"applied": "applied", "replied": "reply_received", "interview": "interview", "offer": "offer", "rejected": "rejected", "withdrawn": "withdrawn", "prepared": "prepared"}[stage]
    if stage == "applied" and actor == "user":
        # One path for "I applied": records the date, the CV and the package.
        return mark_applied(db, job_id, applied_at=occurred_at, source="user", evidence=payload, dedupe_key=dedupe_key, cv_choice=cv_choice)
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
