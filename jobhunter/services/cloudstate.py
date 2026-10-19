"""State bundle for a Claude-operated cloud run.

The student's Claude account runs Hunter in a fresh cloud session that cannot see the laptop. Claude
carries one JSON file between runs through the student's own connected storage (a private Drive
folder). This module decides exactly what goes into that file and how it merges back:

* inputs (laptop -> cloud): the approved profile revision, preferences, the three consents a hunt and
  a digest need (readiness, profile approval, the confirmed digest address), and the keys of jobs the
  person dismissed or applied to, so the cloud never recommends them again.
* results (both ways): employers, jobs, scheduled hunt summaries, and the notification history
  (delivery keys and content hashes), so a digest is never repeated and the laptop's history is complete.

Never in the bundle: original documents or extracted text, notes, drafts, favorites, saved flags,
application details, email messages, tokens, or settings beyond the digest transport. Jobs are matched
by their canonical key, never by row id, so the laptop and the cloud can both create jobs without
colliding. Favorites, notes and applications are local events keyed to local ids; a results import adds
or refreshes jobs and never touches them.
"""

from __future__ import annotations

import json
import uuid
from datetime import timedelta

from jobhunter.config import AppContext
from jobhunter.db import Database
from jobhunter.models import Preferences
from jobhunter.services import consent
from jobhunter.services import preferences as prefs_svc
from jobhunter.services import profile as profile_svc
from jobhunter.util import now, now_iso, parse_iso

FORMAT = "jobhunter-state/1"
JOB_WINDOW_DAYS = 45
JOB_COLUMNS = (
    "canonical_key", "title", "employer_name", "employer_confidential", "location_text", "country_codes_json", "remote_type", "remote_restriction",
    "employment_type", "salary_text", "salary_min", "salary_max", "salary_currency", "salary_period", "salary_basis", "sponsorship", "description_text",
    "description_hash", "requisition_id", "posted_at", "posted_at_provenance", "source_updated_at", "first_seen_at", "last_seen_at", "last_checked_at",
    "availability", "availability_note", "discovery_url", "direct_url", "direct_url_status", "apply_url", "relevance", "relevance_reason",
)
JOB_REFRESH_COLUMNS = ("title", "location_text", "country_codes_json", "remote_type", "remote_restriction", "salary_text", "salary_min", "salary_max", "salary_currency", "salary_period", "description_text", "description_hash", "posted_at", "posted_at_provenance", "source_updated_at", "last_seen_at", "last_checked_at", "availability", "availability_note", "direct_url", "direct_url_status", "apply_url", "relevance", "relevance_reason")
EMPLOYER_COLUMNS = ("name", "canonical_name", "website", "careers_url", "ats_kind", "ats_token", "ats_region", "regions_json", "country_codes_json", "origin", "discovered_via", "first_seen_at")
CONSENTS = ("readiness", "profile_approved", "digest_email")
FORBIDDEN_KEYS = {"facts_json_raw", "source_documents", "job_notes", "drafts", "email_messages", "secrets", "token", "refresh_token"}


class BundleError(Exception):
    pass


# ---------------------------------------------------------------- export
def export_bundle(ctx: AppContext, db: Database, include_inputs: bool = True, include_results: bool = True) -> dict:
    out = {"format": FORMAT, "exported_at": now_iso(), "exported_by": "cloud" if db.get_setting("cloud.role") == "cloud" else "laptop", "policy_version": _policy_version()}
    if include_inputs:
        out["inputs"] = _export_inputs(db)
    if include_results:
        out["results"] = _export_results(db)
    return out


def _policy_version() -> str:
    from jobhunter.services.matching.engine import POLICY_VERSION

    return POLICY_VERSION


def _export_inputs(db: Database) -> dict:
    prof = profile_svc.approved(db)
    if not prof:
        raise BundleError("There is no approved profile yet, so there is nothing a cloud run could match against.")
    profile = {k: prof.get(k) for k in ("version", "summary_text", "built_by", "created_at", "approved_at")}
    profile["facts"] = json.loads(prof.get("facts_json") or "[]")
    profile["targets"] = json.loads(prof.get("targets_json") or "{}")
    consents = {k: consent.details(db, k) for k in CONSENTS if consent.is_granted(db, k)}
    excl = {"dismissed": _keys(db, "SELECT j.canonical_key AS k FROM jobs j WHERE (SELECT kind FROM events e WHERE e.job_id = j.id AND e.kind IN ('dismissed','undismissed') AND e.reverted_by IS NULL ORDER BY e.id DESC LIMIT 1) = 'dismissed'"),
            "applied": _keys(db, "SELECT j.canonical_key AS k FROM jobs j JOIN applications a ON a.job_id = j.id WHERE a.stage != 'prepared'")}
    return {"profile": profile, "preferences": prefs_svc.get(db).model_dump(mode="json"), "consents": consents, "exclusions": excl, "digest_transport": "claude"}


def _keys(db: Database, sql: str) -> list[str]:
    return sorted({r["k"] for r in db.fetchall(sql)})


def _export_results(db: Database) -> dict:
    since = (now() - timedelta(days=JOB_WINDOW_DAYS)).isoformat()
    referenced = set()
    notes = []
    for n in db.fetchall("SELECT * FROM notifications WHERE channel = 'email' ORDER BY id DESC LIMIT 200"):
        ids = json.loads(n.get("job_ids_json") or "[]")
        keys = [r["canonical_key"] for r in db.fetchall(f"SELECT canonical_key FROM jobs WHERE id IN ({','.join('?' for _ in ids)})", tuple(ids))] if ids else []
        referenced.update(keys)
        notes.append({k: n.get(k) for k in ("kind", "channel", "recipient", "subject", "content_hash", "status", "attempts", "provider_message_id", "error", "created_at", "sent_at", "delivery_key", "claimed_at", "resolved_at", "resolution")} | {"job_keys": keys})
    excl_keys = set(_export_inputs_exclusion_keys(db))
    jobs = db.fetchall("SELECT * FROM jobs WHERE first_seen_at >= ? OR last_seen_at >= ? ORDER BY id", (since, since))
    seen = {j["canonical_key"] for j in jobs}
    extra = (referenced | excl_keys) - seen
    if extra:
        jobs += db.fetchall(f"SELECT * FROM jobs WHERE canonical_key IN ({','.join('?' for _ in extra)})", tuple(extra))
    emp_ids = {j["employer_id"] for j in jobs if j.get("employer_id")}
    employers = db.fetchall(f"SELECT * FROM employers WHERE id IN ({','.join('?' for _ in emp_ids)})", tuple(emp_ids)) if emp_ids else []
    emp_by_id = {e["id"]: e["canonical_name"] for e in employers}
    runs = []
    for r in db.fetchall("SELECT * FROM hunt_runs WHERE trigger IN ('scheduled','cloud') AND status != 'running' ORDER BY id DESC LIMIT 20"):
        runs.append({"tag": r.get("owner") if str(r.get("owner") or "").startswith("cloud:") else f"run:{r['started_at']}", "trigger": r["trigger"], "status": r["status"], "started_at": r["started_at"], "finished_at": r.get("finished_at"), "stats": json.loads(r.get("stats_json") or "{}"), "error": r.get("error")})
    return {
        "employers": [{k: e.get(k) for k in EMPLOYER_COLUMNS} for e in employers],
        "jobs": [{k: j.get(k) for k in JOB_COLUMNS} | {"employer_canonical_name": emp_by_id.get(j.get("employer_id"))} for j in jobs],
        "hunt_runs": runs,
        "notifications": notes,
    }


def _export_inputs_exclusion_keys(db: Database) -> list[str]:
    try:
        ex = _export_inputs(db)["exclusions"]
        return ex["dismissed"] + ex["applied"]
    except BundleError:
        return []


# ---------------------------------------------------------------- import
def import_bundle(ctx: AppContext, db: Database, bundle: dict, inputs: bool, results: bool) -> dict:
    """Merge a bundle. `inputs` is for the cloud side (profile, preferences, consents, exclusions);
    `results` for both sides (jobs, employers, run summaries, notification history). Local events are never touched."""
    if bundle.get("format") != FORMAT:
        raise BundleError(f"Not a Hunter state file (format {bundle.get('format')!r}).")
    summary: dict = {"inputs": None, "results": None}
    if inputs:
        if "inputs" not in bundle:
            raise BundleError("This state file has no inputs section.")
        summary["inputs"] = _import_inputs(db, bundle["inputs"])
    if results and bundle.get("results"):
        summary["results"] = _import_results(db, bundle["results"])
    if inputs and bundle.get("inputs", {}).get("exclusions"):
        summary["exclusions_applied"] = _apply_exclusions(db, bundle["inputs"]["exclusions"])
    if results or inputs:
        from jobhunter.services.matching import engine

        summary["matching"] = engine.rematch_quietly(ctx, db)
    db.set_setting("cloud.last_import", {"at": now_iso(), "exported_at": bundle.get("exported_at"), "exported_by": bundle.get("exported_by"), "inputs": inputs, "results": results})
    return summary


def _import_inputs(db: Database, inputs: dict) -> dict:
    prof = inputs["profile"]
    cur = profile_svc.approved(db)
    changed = {"profile": False, "preferences": False, "consents": []}
    if not cur or (cur.get("approved_at"), cur.get("version")) != (prof.get("approved_at"), prof.get("version")):
        prev = db.fetchone("SELECT MAX(version) AS v FROM profile_versions")
        with db.tx():
            db.execute("UPDATE profile_versions SET status = 'superseded' WHERE status IN ('approved', 'draft')")
            db.insert("profile_versions", {"version": max(int(prof.get("version") or 1), int(prev["v"] or 0) + 1), "status": "approved", "summary_text": prof.get("summary_text"), "facts_json": json.dumps(prof.get("facts") or []), "targets_json": json.dumps(prof.get("targets") or {}), "document_ids_json": "[]", "built_by": prof.get("built_by") or "rules", "created_at": prof.get("created_at") or now_iso(), "approved_at": prof.get("approved_at") or now_iso()})
        changed["profile"] = True
    new_prefs = Preferences.model_validate(inputs.get("preferences") or {})
    if new_prefs.model_dump(mode="json") != prefs_svc.get(db).model_dump(mode="json"):
        prefs_svc.save(db, new_prefs, reason="cloud restore")
        changed["preferences"] = True
    for kind, details in (inputs.get("consents") or {}).items():
        if kind in CONSENTS and (not consent.is_granted(db, kind) or consent.details(db, kind) != details):
            consent.grant(db, kind, details)
            changed["consents"].append(kind)
    if inputs.get("digest_transport") == "claude":
        db.set_setting("digest.transport", "claude")
    db.set_setting("cloud.role", "cloud")
    return changed


def _import_results(db: Database, results: dict) -> dict:
    added = refreshed = 0
    with db.tx():
        emp_ids: dict[str, int] = {}
        for e in results.get("employers") or []:
            row = db.fetchone("SELECT id FROM employers WHERE canonical_name = ?", (e["canonical_name"],))
            if row:
                emp_ids[e["canonical_name"]] = row["id"]
            else:
                emp_ids[e["canonical_name"]] = db.insert("employers", {k: e.get(k) for k in EMPLOYER_COLUMNS} | {"first_seen_at": e.get("first_seen_at") or now_iso()})
        key_to_id: dict[str, int] = {}
        for j in results.get("jobs") or []:
            local = db.fetchone("SELECT id, last_seen_at FROM jobs WHERE canonical_key = ?", (j["canonical_key"],))
            values = {k: j.get(k) for k in JOB_COLUMNS}
            values["employer_id"] = emp_ids.get(j.get("employer_canonical_name"))
            if local:
                key_to_id[j["canonical_key"]] = local["id"]
                if (j.get("last_seen_at") or "") > (local.get("last_seen_at") or ""):
                    db.update("jobs", {k: values[k] for k in JOB_REFRESH_COLUMNS} | {"employer_id": values["employer_id"], "updated_at": now_iso()}, "id = ?", (local["id"],))
                    refreshed += 1
            else:
                values["created_at"] = now_iso()
                values["updated_at"] = now_iso()
                key_to_id[j["canonical_key"]] = db.insert("jobs", values)
                added += 1
        runs_added = 0
        for r in results.get("hunt_runs") or []:
            if db.fetchone("SELECT id FROM hunt_runs WHERE owner = ?", (r["tag"],)):
                continue
            db.insert("hunt_runs", {"trigger": "cloud", "status": r["status"], "started_at": r["started_at"], "finished_at": r.get("finished_at"), "owner": r["tag"], "stats_json": json.dumps(r.get("stats") or {}), "error": r.get("error")})
            runs_added += 1
        notes_added = 0
        for n in results.get("notifications") or []:
            if not n.get("delivery_key") or db.fetchone("SELECT id FROM notifications WHERE delivery_key = ?", (n["delivery_key"],)):
                continue
            ids = [key_to_id[k] for k in n.get("job_keys") or [] if k in key_to_id]
            db.insert("notifications", {k: n.get(k) for k in ("kind", "channel", "recipient", "subject", "content_hash", "status", "attempts", "provider_message_id", "error", "created_at", "sent_at", "delivery_key", "claimed_at", "resolved_at", "resolution")} | {"job_ids_json": json.dumps(ids), "body_path": None})
            notes_added += 1
    return {"jobs_added": added, "jobs_refreshed": refreshed, "hunt_runs_added": runs_added, "notifications_added": notes_added}


def _apply_exclusions(db: Database, excl: dict) -> int:
    """On the cloud: the person's dismissed and applied jobs stay out of the digest. Recorded as system dismissals."""
    from jobhunter.services import actions

    n = 0
    keys = set(excl.get("dismissed") or []) | set(excl.get("applied") or [])
    for key in sorted(keys):
        row = db.fetchone("SELECT j.id, (SELECT kind FROM events e WHERE e.job_id = j.id AND e.kind IN ('dismissed','undismissed') AND e.reverted_by IS NULL ORDER BY e.id DESC LIMIT 1) AS st FROM jobs j WHERE canonical_key = ?", (key,))
        if row and row.get("st") != "dismissed":
            actions.dismiss_job(db, row["id"], reason_code="cloud_restore", note="Excluded by the person on their own computer.")
            n += 1
    return n


# ---------------------------------------------------------------- bookkeeping
def new_run_tag() -> str:
    return f"cloud:{uuid.uuid4().hex[:12]}"


def tag_latest_run(db: Database, tag: str) -> None:
    row = db.fetchone("SELECT id FROM hunt_runs ORDER BY id DESC LIMIT 1")
    if row:
        db.update("hunt_runs", {"owner": tag, "trigger": "cloud"}, "id = ?", (row["id"],))


def status(db: Database) -> dict:
    last = db.get_setting("cloud.last_import") or {}
    exported = db.get_setting("cloud.last_export") or {}
    return {"role": db.get_setting("cloud.role") or "laptop", "last_import": last, "last_export": exported, "digest_transport": db.get_setting("digest.transport")}


def check_bundle_privacy(bundle: dict) -> list[str]:
    """Names of things that must never be in a bundle; empty when clean. Used by tests and `cloud export --check`."""
    text = json.dumps(bundle)
    return [k for k in FORBIDDEN_KEYS if f'"{k}"' in text]


def ago(ts: str | None) -> str:
    t = parse_iso(ts)
    if not t:
        return "never"
    s = int((now() - t).total_seconds())
    return "just now" if s < 90 else (f"{s // 60} min ago" if s < 3600 else f"{s // 3600} h ago")
