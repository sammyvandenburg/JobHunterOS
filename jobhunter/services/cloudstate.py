"""State bundle for a Claude-operated cloud run.

The student's Claude account runs Hunter in a fresh cloud session that cannot see the laptop. Claude
carries one JSON file between runs through the student's own connected storage (a private Drive
folder). This module decides exactly what goes into that file and how it merges back:

* inputs (laptop -> cloud): the approved profile revision, preferences, the three consents a hunt and
  a digest need (readiness, profile approval, the confirmed digest address), and the keys of jobs the
  person dismissed or applied to, so the cloud never recommends them again.
* results (both ways): employers, jobs, scheduled hunt summaries, and the notification history
  (delivery keys and content hashes), so a digest is never repeated and the laptop's history is complete.

* the person's work on jobs (both ways): every job they favorited, saved, dismissed, applied to or noted
  travels with its identity and its flags (favorite, saved, not interested, applied date, stage) and the time
  of the last decision, whatever its age, so a restore into an empty database rebuilds it and an old or
  closed job they acted on is never lost. The newer decision wins on each side; equal state changes nothing.

Never in the bundle: original documents or extracted text, the text of notes, drafts, application
details (CVs, packages, contacts), email messages, tokens, or settings beyond the digest transport. A note's
text lives in the page's own database and in the databases that read the page; which page revisions a
database applied is never carried either, so each database replays the page's actions it has not applied.
Jobs are matched by their canonical key, never by row id, so the laptop and the cloud can both create jobs
without colliding.
"""

from __future__ import annotations

import json
import re
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
JOB_WINDOW_DAYS = 21
JOB_COLUMNS = (
    "canonical_key", "title", "employer_name", "employer_confidential", "location_text", "country_codes_json", "remote_type", "remote_restriction",
    "employment_type", "salary_text", "salary_min", "salary_max", "salary_currency", "salary_period", "salary_basis", "sponsorship", "description_text",
    "description_hash", "requisition_id", "posted_at", "posted_at_provenance", "source_updated_at", "first_seen_at", "last_seen_at", "last_checked_at",
    "availability", "availability_note", "discovery_url", "direct_url", "direct_url_status", "apply_url", "relevance", "relevance_reason", "location_confidence",
)
MATCH_COLUMNS = ("score", "match_group", "reasons_json", "gaps_json", "eligibility_json", "exclusion_reason", "explanation_text", "explanation_source", "next_step")
JOB_REFRESH_COLUMNS = ("title", "location_text", "country_codes_json", "remote_type", "remote_restriction", "salary_text", "salary_min", "salary_max", "salary_currency", "salary_period", "description_text", "description_hash", "posted_at", "posted_at_provenance", "source_updated_at", "last_seen_at", "last_checked_at", "availability", "availability_note", "direct_url", "direct_url_status", "apply_url", "relevance", "relevance_reason")
EMPLOYER_COLUMNS = ("name", "canonical_name", "website", "careers_url", "ats_kind", "ats_token", "ats_region", "regions_json", "country_codes_json", "origin", "discovered_via", "first_seen_at")
CONSENTS = ("readiness", "profile_approved", "digest_email")
FORBIDDEN_KEYS = {"facts_json_raw", "source_documents", "job_notes", "note_text", "drafts", "email_messages", "secrets", "token", "refresh_token"}
ACTED_DESCRIPTION_CAP = 3000  # what the dashboard shows for a job the person acted on; a mere snippet for the rest
STATE_EVENT_KINDS = ("favorited", "unfavorited", "saved", "unsaved", "dismissed", "undismissed", "applied", "reply_received", "interview", "offer", "rejected", "withdrawn", "correction")


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
    from jobhunter.services import board

    return {"profile": profile, "preferences": prefs_svc.get(db).model_dump(mode="json"), "preferences_changed_at": _prefs_changed_at(db), "consents": consents, "exclusions": excl, "digest_transport": "claude", "board": board.export_sync_state(db), "cvs": _export_cvs(db)}


def _prefs_changed_at(db: Database) -> dict:
    """When each preference field last changed here (from the confirmed change history)."""
    out: dict = {}
    # The person's own edits only: a restore writing the file's values is not a decision made here.
    for r in db.fetchall("SELECT key, MAX(created_at) AS at FROM preference_changes WHERE reverted_at IS NULL AND COALESCE(reason, '') NOT LIKE 'cloud restore%' GROUP BY key"):
        out[r["key"]] = r["at"]
    return out


PREFS_CONFLICTS_SETTING = "preferences.conflicts"


def _merge_preferences(db: Database, incoming: dict, incoming_at: dict) -> dict:
    """Field by field: the side that changed a field later wins; a field only one side changed keeps that change; a
    field both sides changed since they last agreed is a conflict the person decides (kept as the local value and
    listed under `preferences.conflicts`). Git moved the file; this is the merge."""
    local = prefs_svc.get(db).model_dump(mode="json")
    local_at = _prefs_changed_at(db)
    last_sync = max((db.get_setting("cloud.last_import") or {}).get("at") or "", (db.get_setting("cloud.last_export") or {}).get("at") or "")  # the last exchange with the file
    merged = dict(local)
    conflicts = db.get_setting(PREFS_CONFLICTS_SETTING) or {}
    changed_fields: list[str] = []
    for key in sorted(set(local) | set(incoming)):
        lv, rv = local.get(key), incoming.get(key)
        if lv == rv:
            conflicts.pop(key, None)
            continue
        la, ra = local_at.get(key) or "", (incoming_at or {}).get(key) or ""
        # Timestamps carry whole seconds: an edit in the same second as the last exchange counts as after it,
        # so a doubt becomes a visible conflict rather than a silent overwrite.
        local_changed = bool(la) and la >= last_sync
        remote_changed = bool(ra) and ra >= last_sync
        if local_changed and remote_changed:
            conflicts[key] = {"local": lv, "remote": rv, "local_at": la, "remote_at": ra, "seen_at": now_iso()}
            continue  # keep the local value; the person decides
        if remote_changed or (not local_changed and ra >= la):
            merged[key] = rv
            changed_fields.append(key)
        # else: only this side changed it, or it is newer here: keep local
    if changed_fields:
        prefs_svc.save(db, Preferences.model_validate(merged), reason="cloud restore (field merge)")
    db.set_setting(PREFS_CONFLICTS_SETTING, conflicts)
    return {"changed": changed_fields, "conflicts": sorted(conflicts)}


def _export_cvs(db: Database) -> list[dict]:
    """My CVs as metadata only: stable keys, labels, tags, which is the default, and every version's hash, file name and
    size. Never the file, never its text. A restored database shows the same CVs and records which version an
    application used; the file itself is only on the computer where it was added."""
    from jobhunter.services import documents as docs

    out = []
    for c in docs.list_cvs(db, include_archived=True, with_revisions=True):
        out.append({k: c[k] for k in ("key", "label", "tags", "default", "profile_cv", "archived", "archived_at", "created_at")} | {"revisions": [{k: r[k] for k in ("revision_key", "number", "sha256", "filename", "bytes", "uploaded_at", "note")} for r in c["revisions"]]})
    return out


def _import_cvs(db: Database, cvs: list[dict]) -> int:
    """Recreate CV variants and versions by their stable keys, with placeholder originals (no bytes here): the person
    adds the file again on this computer when they want to preview or download it; the hash proves it is the same."""
    from jobhunter.services import documents as docs

    n = 0
    ts = now_iso()
    for c in cvs or []:
        if not isinstance(c, dict) or not re.fullmatch(r"cv[0-9a-f]{10}", str(c.get("key") or "")):
            continue
        with db.tx():
            v = db.fetchone("SELECT * FROM cv_variants WHERE variant_key = ?", (c["key"],))
            if not v:
                vid = db.insert("cv_variants", {"variant_key": c["key"], "label": str(c.get("label") or "My CV")[:60], "tags_json": json.dumps([str(t)[:30] for t in (c.get("tags") or [])][:8]), "is_default": 1 if c.get("default") else 0, "is_profile": 1 if c.get("profile_cv") else 0, "archived_at": c.get("archived_at"), "created_at": c.get("created_at") or ts, "updated_at": ts})
                n += 1
            else:
                vid = v["id"]
                db.update("cv_variants", {"label": str(c.get("label") or v["label"])[:60], "tags_json": json.dumps([str(t)[:30] for t in (c.get("tags") or [])][:8]), "is_default": 1 if c.get("default") else 0, "archived_at": c.get("archived_at"), "updated_at": ts}, "id = ?", (vid,))
            for r in c.get("revisions") or []:
                if not isinstance(r, dict) or not re.fullmatch(r"rev[0-9a-f]{10}", str(r.get("revision_key") or "")):
                    continue
                if db.fetchone("SELECT id FROM cv_revisions WHERE revision_key = ?", (r["revision_key"],)):
                    continue
                existing = db.fetchone("SELECT id FROM source_documents WHERE sha256 = ? AND kind IN ('cv', 'cv_variant') ORDER BY id LIMIT 1", (r.get("sha256") or "",))
                if existing:
                    did = existing["id"]
                else:
                    kind = "cv" if c.get("profile_cv") else "cv_variant"
                    prev = db.fetchone("SELECT id, version FROM source_documents WHERE kind = ? AND superseded_by IS NULL ORDER BY version DESC LIMIT 1", (kind,))
                    did = db.insert("source_documents", {"kind": kind, "label": c.get("label"), "original_filename": r.get("filename"), "media_type": None, "byte_size": int(r.get("bytes") or 0), "sha256": str(r.get("sha256") or ""), "version": (prev["version"] + 1) if prev else int(r.get("number") or 1), "stored_path": str(docs.placeholder_path(db, str(r.get("sha256") or ""), r.get("filename"))), "text_path": None, "extraction_status": "unavailable", "extraction_notes": "The file is not on this computer. Add this CV again to preview or download it.", "source_url": None, "created_at": r.get("uploaded_at") or ts})
                    if prev and kind == "cv":
                        db.update("source_documents", {"superseded_by": did}, "id = ?", (prev["id"],))
                db.insert("cv_revisions", {"revision_key": r["revision_key"], "variant_id": vid, "document_id": did, "number": int(r.get("number") or 1), "note": r.get("note"), "review_json": None, "created_at": r.get("uploaded_at") or ts})
    if cvs and not db.fetchone("SELECT id FROM cv_variants WHERE is_default = 1 AND archived_at IS NULL"):
        first = db.fetchone("SELECT id FROM cv_variants WHERE archived_at IS NULL ORDER BY is_profile DESC, created_at LIMIT 1")
        if first:
            db.update("cv_variants", {"is_default": 1}, "id = ?", (first["id"],))
    return n


def _keys(db: Database, sql: str) -> list[str]:
    return sorted({r["k"] for r in db.fetchall(sql)})


def _acted_jobs(db: Database) -> dict[int, dict]:
    """Every job the person decided something about, with the current flags and the time of the last decision."""
    rows = db.fetchall(
        "SELECT j.id, j.canonical_key, "
        "(SELECT kind FROM events e WHERE e.job_id = j.id AND e.kind IN ('favorited','unfavorited') AND e.reverted_by IS NULL ORDER BY e.id DESC LIMIT 1) AS fav_state, "
        "(SELECT kind FROM events e WHERE e.job_id = j.id AND e.kind IN ('saved','unsaved') AND e.reverted_by IS NULL ORDER BY e.id DESC LIMIT 1) AS save_state, "
        "(SELECT kind FROM events e WHERE e.job_id = j.id AND e.kind IN ('dismissed','undismissed') AND e.reverted_by IS NULL ORDER BY e.id DESC LIMIT 1) AS dismiss_state, "
        "a.stage AS app_stage, a.applied_at AS app_applied_at, a.cv_variant_key, a.cv_choice, a.submitted_cv_choice, a.submitted_cv_revision_key, "
        "(SELECT COUNT(*) FROM job_notes n WHERE n.job_id = j.id AND n.deleted_at IS NULL) AS notes_count, "
        f"(SELECT MAX(occurred_at) FROM events e WHERE e.job_id = j.id AND e.kind IN ({','.join('?' for _ in STATE_EVENT_KINDS)})) AS state_at "
        "FROM jobs j LEFT JOIN applications a ON a.job_id = j.id",
        STATE_EVENT_KINDS,
    )
    out = {}
    for r in rows:
        stage = r.get("app_stage") if r.get("app_stage") != "prepared" else None
        if not (r.get("state_at") or r.get("notes_count") or stage or r.get("cv_variant_key")):
            continue
        out[r["id"]] = {
            "cv": {"variant_key": r.get("cv_variant_key"), "choice": r.get("cv_choice"), "submitted_choice": r.get("submitted_cv_choice"), "submitted_revision_key": r.get("submitted_cv_revision_key")} if (r.get("cv_variant_key") or r.get("submitted_cv_choice")) else None,
            "favorite": r.get("fav_state") == "favorited",
            "saved": r.get("save_state") == "saved",
            "dismissed": r.get("dismiss_state") == "dismissed",
            "applied_at": r.get("app_applied_at") if stage else None,
            "stage": stage,
            "has_note": bool(r.get("notes_count")),
            "at": r.get("state_at") or now_iso(),
        }
    return out


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
    acted = _acted_jobs(db)
    # What the other side cannot re-derive travels: the jobs an email refers to, with their match outcome, and
    # every job the person decided something about, with the decision. Everything else a hunt finds again.
    keys = referenced | excl_keys
    recent = db.fetchall(f"SELECT * FROM jobs WHERE canonical_key IN ({','.join('?' for _ in keys)}) AND (first_seen_at >= ? OR last_seen_at >= ?) ORDER BY id", (*keys, since, since)) if keys else []
    jobs = {j["id"]: j for j in recent}
    if acted:
        for j in db.fetchall(f"SELECT * FROM jobs WHERE id IN ({','.join('?' for _ in acted)}) ORDER BY id", tuple(acted)):
            jobs[j["id"]] = j
    jobs = [jobs[i] for i in sorted(jobs)]
    seen = referenced
    emp_ids = {j["employer_id"] for j in jobs if j.get("employer_id")}
    employers = db.fetchall(f"SELECT * FROM employers WHERE id IN ({','.join('?' for _ in emp_ids)})", tuple(emp_ids)) if emp_ids else []
    emp_by_id = {e["id"]: e["canonical_name"] for e in employers}
    prof = profile_svc.approved(db)
    matches = {m["job_id"]: m for m in db.fetchall("SELECT * FROM matches WHERE profile_version_id = ? AND policy_version = ?", (prof["id"], _policy_version()))} if prof else {}
    if prof:
        from jobhunter.services.matching import engine

        pkey, phash = engine.profile_fingerprint(prof), engine._prefs_hash(db)
        for m in matches.values():
            m["_profile_key"], m["_prefs_hash"] = pkey, phash
    runs = []
    for r in db.fetchall("SELECT * FROM hunt_runs WHERE trigger IN ('scheduled','cloud') AND status != 'running' ORDER BY id DESC LIMIT 20"):
        runs.append({"tag": r.get("owner") if str(r.get("owner") or "").startswith("cloud:") else f"run:{r['started_at']}", "trigger": r["trigger"], "status": r["status"], "started_at": r["started_at"], "finished_at": r.get("finished_at"), "stats": json.loads(r.get("stats_json") or "{}"), "error": r.get("error")})
    return {
        "employers": [{k: e.get(k) for k in EMPLOYER_COLUMNS} for e in employers],
        "jobs": [_job_row(j, emp_by_id, matches.get(j["id"]), snippet=j["canonical_key"] in seen, state=acted.get(j["id"])) for j in jobs],
        "hunt_runs": runs,
        "notifications": notes,
    }


DESCRIPTION_CAP = 300  # a snippet; the match travels with the job, and the cache key uses the untouched description_hash


def _job_row(j: dict, emp_by_id: dict, match: dict | None, snippet: bool, state: dict | None = None) -> dict:
    row = {k: j.get(k) for k in JOB_COLUMNS}
    text = row.get("description_text") or ""
    cap = ACTED_DESCRIPTION_CAP if state else DESCRIPTION_CAP
    row["description_text"] = (text[:cap] + (" …" if len(text) > cap else "")) if (snippet or state) else None
    row["employer_canonical_name"] = emp_by_id.get(j.get("employer_id"))
    row["match"] = ({k: match.get(k) for k in MATCH_COLUMNS} | {"fingerprint": {"profile": match.get("_profile_key"), "prefs": match.get("_prefs_hash"), "policy": match.get("policy_version"), "job": match.get("cache_key")}, "computed_at": match.get("computed_at")}) if match else None
    if state:
        row["state"] = state
    return row


def write_bundle(path, bundle: dict) -> int:
    """JSON, gzipped when the name ends in .gz (the Drive round trip carries base64, so small matters)."""
    import gzip
    from pathlib import Path as _P

    data = json.dumps(bundle, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    path = _P(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix == ".gz":
        with gzip.open(path, "wb", compresslevel=9) as fh:
            fh.write(data)
    else:
        path.write_bytes(data)
    return path.stat().st_size


def read_bundle(path) -> dict:
    import gzip
    from pathlib import Path as _P

    raw = _P(path).read_bytes()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return json.loads(raw.decode("utf-8"))


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
    if not inputs and (bundle.get("inputs") or {}).get("board"):
        # Laptop side: learn which dashboard actions the cloud run already applied, so they are not applied twice.
        from jobhunter.services import board

        summary["board"] = board.import_sync_state(db, bundle["inputs"]["board"])
    if results or inputs:
        from jobhunter.services.matching import engine

        summary["matching"] = engine.rematch_quietly(ctx, db)
    db.set_setting("cloud.last_import", {"at": now_iso(), "exported_at": bundle.get("exported_at"), "exported_by": bundle.get("exported_by"), "inputs": inputs, "results": results})
    return summary


def _import_inputs(db: Database, inputs: dict) -> dict:
    prof = inputs["profile"]
    cur = profile_svc.approved(db)
    changed = {"profile": False, "preferences": False, "consents": []}
    # The newer approval wins; an older snapshot never replaces a profile approved later on this side.
    if not cur or ((prof.get("approved_at") or "") > (cur.get("approved_at") or "") and (cur.get("approved_at"), cur.get("version")) != (prof.get("approved_at"), prof.get("version"))):
        prev = db.fetchone("SELECT MAX(version) AS v FROM profile_versions")
        with db.tx():
            db.execute("UPDATE profile_versions SET status = 'superseded' WHERE status IN ('approved', 'draft')")
            db.insert("profile_versions", {"version": max(int(prof.get("version") or 1), int(prev["v"] or 0) + 1), "status": "approved", "summary_text": prof.get("summary_text"), "facts_json": json.dumps(prof.get("facts") or []), "targets_json": json.dumps(prof.get("targets") or {}), "document_ids_json": "[]", "built_by": prof.get("built_by") or "rules", "created_at": prof.get("created_at") or now_iso(), "approved_at": prof.get("approved_at") or now_iso()})
        changed["profile"] = True
    new_prefs = Preferences.model_validate(inputs.get("preferences") or {})
    if new_prefs.model_dump(mode="json") != prefs_svc.get(db).model_dump(mode="json"):
        if db.get_setting("preferences") is None:
            prefs_svc.save(db, new_prefs, reason="cloud restore")  # an empty database simply takes the file
            changed["preferences"] = True
        else:
            merge = _merge_preferences(db, new_prefs.model_dump(mode="json"), inputs.get("preferences_changed_at") or {})
            changed["preferences"] = bool(merge["changed"])
            changed["preference_conflicts"] = merge["conflicts"]
    for kind, details in (inputs.get("consents") or {}).items():
        if kind in CONSENTS and (not consent.is_granted(db, kind) or consent.details(db, kind) != details):
            consent.grant(db, kind, details)
            changed["consents"].append(kind)
    if inputs.get("digest_transport") == "claude":
        db.set_setting("digest.transport", "claude")
    if inputs.get("board"):
        from jobhunter.services import board

        changed["board"] = board.import_sync_state(db, inputs["board"])
    if isinstance(inputs.get("cvs"), list):
        changed["cvs"] = _import_cvs(db, inputs["cvs"])
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
        matches_added = _import_matches(db, results.get("jobs") or [], key_to_id)
        states_applied = _import_states(db, results.get("jobs") or [], key_to_id)
        notes_added = 0
        for n in results.get("notifications") or []:
            if not n.get("delivery_key"):
                continue
            existing = db.fetchone("SELECT * FROM notifications WHERE delivery_key = ?", (n["delivery_key"],))
            if existing:
                _merge_notification(db, existing, n)
                continue
            ids = [key_to_id[k] for k in n.get("job_keys") or [] if k in key_to_id]
            db.insert("notifications", {k: n.get(k) for k in ("kind", "channel", "recipient", "subject", "content_hash", "status", "attempts", "provider_message_id", "error", "created_at", "sent_at", "delivery_key", "claimed_at", "resolved_at", "resolution")} | {"job_ids_json": json.dumps(ids), "body_path": None})
            notes_added += 1
    return {"jobs_added": added, "jobs_refreshed": refreshed, "matches_added": matches_added, "states_applied": states_applied, "hunt_runs_added": runs_added, "notifications_added": notes_added}


NOTIFICATION_RANK = {"queued": 0, "sending": 1, "failed": 2, "uncertain": 3, "sent": 4}


def _merge_notification(db: Database, existing: dict, incoming: dict) -> bool:
    """Lifecycle evidence only moves forward: sent never becomes sending again, a provider message id is never erased,
    and a resolution recorded anywhere is kept. Whatever side knows more wins, field by field."""
    values: dict = {}
    cur_rank = NOTIFICATION_RANK.get(existing.get("status") or "", -1)
    new_rank = NOTIFICATION_RANK.get(incoming.get("status") or "", -1)
    if new_rank > cur_rank:
        values["status"] = incoming["status"]
        if incoming.get("error"):
            values["error"] = incoming["error"]
    if incoming.get("provider_message_id") and not existing.get("provider_message_id"):
        values["provider_message_id"] = incoming["provider_message_id"]
    if incoming.get("sent_at") and not existing.get("sent_at"):
        values["sent_at"] = incoming["sent_at"]
    if incoming.get("resolved_at") and not existing.get("resolved_at"):
        values["resolved_at"] = incoming["resolved_at"]
        values["resolution"] = incoming.get("resolution")
    if int(incoming.get("attempts") or 0) > int(existing.get("attempts") or 0):
        values["attempts"] = int(incoming["attempts"])
    if values:
        db.update("notifications", values, "id = ?", (existing["id"],))
    return bool(values)


def _state_at(db: Database, job_id: int) -> str:
    row = db.fetchone(f"SELECT MAX(occurred_at) AS t FROM events WHERE job_id = ? AND kind IN ({','.join('?' for _ in STATE_EVENT_KINDS)})", (job_id, *STATE_EVENT_KINDS))
    return (row or {}).get("t") or ""


def _import_states(db: Database, jobs: list[dict], key_to_id: dict[str, int]) -> int:
    """Rebuild the person's decisions on each job that travelled with a `state`: the newer side wins, equal
    state changes nothing, and every change is an ordinary reversible event stamped with the original time.
    Notes are not rebuilt here (their text never travels): the page is read again for them."""
    from jobhunter.services import actions

    n = 0
    for j in jobs:
        st = j.get("state")
        jid = key_to_id.get(j["canonical_key"])
        if not isinstance(st, dict) or not jid:
            continue
        cv_changed = _import_cv_state(db, jid, st.get("cv"))  # before "applied": the recorded CV version binds first
        when = str(st.get("at") or "")[:32]
        if when and when <= _state_at(db, jid):
            n += cv_changed
            continue  # this database decided later (or at the same time): keep its own decision
        row = db.fetchone(
            "SELECT (SELECT kind FROM events e WHERE e.job_id = j.id AND e.kind IN ('favorited','unfavorited') AND e.reverted_by IS NULL ORDER BY e.id DESC LIMIT 1) AS fav, "
            "(SELECT kind FROM events e WHERE e.job_id = j.id AND e.kind IN ('saved','unsaved') AND e.reverted_by IS NULL ORDER BY e.id DESC LIMIT 1) AS sav, "
            "(SELECT kind FROM events e WHERE e.job_id = j.id AND e.kind IN ('dismissed','undismissed') AND e.reverted_by IS NULL ORDER BY e.id DESC LIMIT 1) AS dis, "
            "a.stage AS stage FROM jobs j LEFT JOIN applications a ON a.job_id = j.id WHERE j.id = ?",
            (jid,),
        )
        changed = False
        if bool(st.get("favorite")) != (row.get("fav") == "favorited"):
            actions.favorite_job(db, jid, on=bool(st.get("favorite")), occurred_at=when or None)
            changed = True
        if bool(st.get("saved")) != (row.get("sav") == "saved"):
            actions.save_job(db, jid, save=bool(st.get("saved")), occurred_at=when or None)
            changed = True
        if bool(st.get("dismissed")) != (row.get("dis") == "dismissed"):
            actions.dismiss_job(db, jid, reason_code="restored", note="Decided on your other computer or in a cloud run.", undo=not st.get("dismissed"), occurred_at=when or None)
            changed = True
        stage = st.get("stage") if st.get("stage") in actions.STAGE_ORDER else None
        cur_stage = row.get("stage") if row.get("stage") != "prepared" else None
        if stage and stage != cur_stage:
            if cur_stage is None:
                applied_at = str(st.get("applied_at") or when or "")[:32] or None
                actions.mark_applied(db, jid, applied_at=applied_at, source="user", evidence={"via": "restore"})
            if stage != "applied":
                actions.set_stage(db, jid, stage, actor="user", occurred_at=when or None, payload={"via": "restore"}, allow_backwards=True)
            changed = True
        elif not stage and cur_stage:
            for ev in db.fetchall("SELECT id FROM events WHERE job_id = ? AND application_id IS NOT NULL AND reverted_by IS NULL AND kind IN ('applied','reply_received','interview','offer','rejected','withdrawn') ORDER BY id", (jid,)):
                actions.correct(db, ev["id"], note="Undone on your other computer or in a cloud run.")
            changed = True
        n += changed or cv_changed
    return n


def _import_cv_state(db: Database, jid: int, cv: dict | None) -> bool:
    """The CV chosen for this job and the version recorded as sent. A recorded submission is history: it is set once
    and never overwritten by a later restore."""
    if not isinstance(cv, dict):
        return False
    app = db.fetchone("SELECT * FROM applications WHERE job_id = ?", (jid,))
    values: dict = {}
    vk = cv.get("variant_key")
    if vk and re.fullmatch(r"cv[0-9a-f]{10}", str(vk)) and db.fetchone("SELECT id FROM cv_variants WHERE variant_key = ?", (vk,)) and (not app or app.get("cv_variant_key") != vk):
        values.update({"cv_variant_key": vk, "cv_choice": cv.get("choice") if cv.get("choice") in {"default", "suggested", "student"} else "student"})
    sc, sr = cv.get("submitted_choice"), cv.get("submitted_revision_key")
    if sc in {"prepared", "other", "unknown"} and (not app or not app.get("submitted_cv_choice")):
        if sr and not db.fetchone("SELECT id FROM cv_revisions WHERE revision_key = ?", (sr,)):
            sr = None
        values.update({"submitted_cv_choice": sc if sr or sc == "unknown" else "unknown", "submitted_cv_revision_key": sr})
    if not values:
        return False
    ts = now_iso()
    with db.tx():
        if not app:
            db.insert("applications", {"job_id": jid, "stage": "prepared", "stage_changed_at": ts, "created_at": ts, "updated_at": ts, **values})
        else:
            db.update("applications", {**values, "updated_at": ts}, "id = ?", (app["id"],))
    return True


def _import_matches(db: Database, jobs: list[dict], key_to_id: dict[str, int]) -> int:
    """The cloud's match outcome for the same profile and policy, stored under the key the local engine would compute,
    so the laptop shows the cloud's groups without rescoring from a snippet. A later profile change rescores as usual."""
    from jobhunter.services.matching import engine

    prof = profile_svc.approved(db)
    if not prof:
        return 0
    pkey, phash = engine.profile_fingerprint(prof), engine._prefs_hash(db)
    n = 0
    for j in jobs:
        m = j.get("match")
        jid = key_to_id.get(j["canonical_key"])
        if not m or not jid or not m.get("match_group"):
            continue
        fp = m.get("fingerprint") or {}
        # Only a match computed for this exact profile, these preferences and this policy is current here; anything
        # else is an old opinion and is left out rather than relabelled.
        if fp and (fp.get("profile") != pkey or fp.get("prefs") != phash or fp.get("policy") != engine.POLICY_VERSION):
            continue
        row = db.fetchone("SELECT * FROM jobs WHERE id = ?", (jid,))
        key = engine.cache_key(row, pkey, phash)
        if fp and fp.get("job") and fp["job"] != key:
            continue  # the job's material fields differ from what was scored
        values = {k: m.get(k) for k in MATCH_COLUMNS} | {"job_id": jid, "profile_version_id": prof["id"], "policy_version": engine.POLICY_VERSION, "cache_key": key, "computed_at": m.get("computed_at") or now_iso()}
        values["explanation_source"] = values.get("explanation_source") or "rules"
        existing = db.fetchone("SELECT id FROM matches WHERE job_id = ? AND profile_version_id = ? AND policy_version = ?", (jid, prof["id"], engine.POLICY_VERSION))
        if existing:
            db.update("matches", values, "id = ?", (existing["id"],))
        else:
            db.insert("matches", values)
        n += 1
    return n


def _apply_exclusions(db: Database, excl: dict) -> int:
    """On the cloud: the person's dismissed and applied jobs stay out of the digest. Recorded as system dismissals."""
    from jobhunter.services import actions

    n = 0
    keys = set(excl.get("dismissed") or []) | set(excl.get("applied") or [])
    for key in sorted(keys):
        row = db.fetchone("SELECT j.id, (SELECT kind FROM events e WHERE e.job_id = j.id AND e.kind IN ('dismissed','undismissed') AND e.reverted_by IS NULL ORDER BY e.id DESC LIMIT 1) AS st, a.stage AS stage FROM jobs j LEFT JOIN applications a ON a.job_id = j.id WHERE canonical_key = ?", (key,))
        if row and row.get("st") != "dismissed" and (row.get("stage") or "prepared") == "prepared":
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
    return {"role": db.get_setting("cloud.role") or "laptop", "state_repo": db.get_setting(REPO_SETTING), "state_branch": db.get_setting(BRANCH_SETTING), "last_import": last, "last_export": exported, "digest_transport": db.get_setting("digest.transport")}


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


# ---------------------------------------------------------------- a private git repository as the state store
STATE_DIR = "state"  # inside the student's own private copy of Hunter; upstream never touches it
STATE_FILE = "hunter-state.json"
SEEN_FILE = "seen_jobs.json"
REPO_SETTING = "cloud.state_repo"
BRANCH_SETTING = "cloud.state_branch"
UPSTREAM_URL = "https://github.com/sammyvandenburg/JobHunterOS.git"
UPSTREAM_SLUG = "sammyvandenburg/jobhunteros"


class RepoError(BundleError):
    pass


def _git(repo_dir, *args: str, check: bool = True) -> str:
    import subprocess

    res = subprocess.run(["git", "-C", str(repo_dir), *args], capture_output=True, text=True, timeout=120)
    if check and res.returncode != 0:
        raise RepoError(f"git {' '.join(args)} failed: {(res.stderr or res.stdout).strip()[:300]}")
    return (res.stdout or "").strip()


def _has_remote(repo_dir) -> bool:
    return bool(_git(repo_dir, "remote", check=False))


def link_repo(db: Database, repo_dir) -> dict:
    """Remember the student's own private state repository (a git clone on this machine)."""
    from pathlib import Path as _P

    d = _P(repo_dir).expanduser().resolve()
    if not (d / ".git").exists():
        raise RepoError(f"{d} is not a git clone. Clone your private state repository there first.")
    db.set_setting(REPO_SETTING, str(d))
    return {"state_repo": str(d), "remote": _git(d, "remote", "get-url", "origin", check=False) or None}


def software_repo():
    """The checkout this code runs from, when it is a git clone: the student's own private copy of Hunter."""
    from pathlib import Path as _P

    root = _P(__file__).resolve().parents[2]
    return root if (root / ".git").exists() else None


def repo_dir(db: Database, override=None):
    """Where the state lives: an explicit path, the linked path, or (the default) this very checkout."""
    from pathlib import Path as _P

    d = override or db.get_setting(REPO_SETTING) or software_repo()
    if not d:
        raise RepoError("No state repository. Run Hunter from a git clone of your own private copy, or `jobhunter cloud link --repo <clone>`.")
    return _P(d)


def state_dir(repo):
    from pathlib import Path as _P

    d = _P(repo) / STATE_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def _slug_of(url: str | None) -> str | None:
    m = re.search(r"github\.com[:/]+([^/]+)/([^/\s]+?)(?:\.git)?/?$", url or "")
    return f"{m.group(1)}/{m.group(2)}".lower() if m else None


def _origin_slug(repo_dir) -> str | None:
    return _slug_of(_git(repo_dir, "remote", "get-url", "origin", check=False))


def _push_url(repo_dir, branch: str | None = None) -> str:
    """Where a push really goes: a push URL, a pushRemote or a pushDefault can differ from the fetch URL."""
    remote = "origin"
    if branch:
        remote = _git(repo_dir, "config", "--get", f"branch.{branch}.pushRemote", check=False) or _git(repo_dir, "config", "--get", "remote.pushDefault", check=False) or "origin"
    return _git(repo_dir, "remote", "get-url", "--push", remote, check=False)


def _github_token() -> str | None:
    import os

    return os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or None


def _github_repo(slug: str, token: str | None) -> tuple[int | None, dict | None, dict]:
    """(status, body, headers) for GET /repos/{slug}; (None, None, {}) when the network did not answer."""
    try:
        import httpx

        headers = {"Accept": "application/vnd.github+json", "User-Agent": "jobhunteros"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        r = httpx.get(f"https://api.github.com/repos/{slug}", timeout=8, headers=headers)
    except Exception:
        return None, None, {}
    try:
        body = r.json() if r.content else None
    except ValueError:
        body = None
    return r.status_code, body if isinstance(body, dict) else None, {k.lower(): v for k, v in r.headers.items()}


def _github_visibility(slug: str) -> str:
    """Kept for callers: 'public', 'private' (positively, with a token), 'private_or_missing' (anonymous 404) or 'unknown'."""
    return verify_destination_slug(slug)["visibility"]


TRUST_SETTING = "cloud.destination_trust"  # the person's own confirmation that a destination is private, when no token can prove it


def verify_destination_slug(slug: str, token: str | None = None) -> dict:
    """What can be positively established about a GitHub destination. With a token: the repository's own `private`
    flag and whether this identity may push. Without one: an anonymous 404 only says "not visible anonymously";
    that is proof of privacy only together with a push that succeeded (the repository exists and we may write).
    Rate limits, network errors and 403s prove nothing."""
    token = token if token is not None else _github_token()
    status, body, headers = _github_repo(slug, token)
    if status is None:
        return {"slug": slug, "visibility": "unknown", "how": "no answer from api.github.com", "push_allowed": None, "identity": None}
    if token and status == 200 and body is not None:
        private = bool(body.get("private"))
        perms = body.get("permissions") or {}
        return {"slug": slug, "visibility": "private" if private else "public", "how": "GitHub API with a token", "push_allowed": bool(perms.get("push")) if perms else None, "identity": body.get("owner", {}).get("login")}
    if status == 200 and body is not None:
        return {"slug": slug, "visibility": "public" if not body.get("private") else "private_or_missing", "how": "anonymous GitHub API", "push_allowed": None, "identity": None}
    if status in {403, 429} or headers.get("x-ratelimit-remaining") == "0":
        return {"slug": slug, "visibility": "unknown", "how": f"GitHub answered {status} (rate limit or refusal), which proves nothing", "push_allowed": None, "identity": None}
    if status == 404:
        return {"slug": slug, "visibility": "private_or_missing", "how": "anonymous GitHub API: not visible without a token", "push_allowed": None, "identity": None}
    return {"slug": slug, "visibility": "unknown", "how": f"GitHub answered {status}", "push_allowed": None, "identity": None}


def check_destination(repo_dir, db: Database | None = None, branch: str | None = None) -> dict:
    """Personal state only ever goes to the student's own private copy: never to a public repository, never to
    KubeCraft's shared one, and never to a destination whose privacy could not be established. The push URL is
    what is checked (it can differ from the fetch URL). Raises RepoError otherwise.

    Positive proof is one of: the GitHub API with a token says private and pushable; an anonymous 404 for the
    push destination plus an earlier push that succeeded to that same destination (recorded by `save_to_repo`);
    or the person's own recorded confirmation for that slug (`cloud link --confirm-private`)."""
    push_url = _push_url(repo_dir, branch)
    fetch_slug = _origin_slug(repo_dir)
    slug = _slug_of(push_url) or fetch_slug
    if slug == UPSTREAM_SLUG or fetch_slug == UPSTREAM_SLUG:
        raise RepoError("This checkout pushes to KubeCraft's shared repository. Personal state never goes there: use your own private copy of Hunter (see docs/cloud-routine.md).")
    if not slug:
        if push_url and not push_url.startswith(("file://", "/")) and "github.com" not in push_url:
            return {"origin": None, "push_url": push_url, "visibility": "unverified-host", "how": "not GitHub; no check possible", "verified": False}
        return {"origin": None, "push_url": push_url or None, "visibility": "local", "verified": True}
    v = verify_destination_slug(slug)
    out = {"origin": fetch_slug, "push_slug": slug, "push_url": push_url, **v, "verified": False}
    if fetch_slug and fetch_slug != slug:
        out["note"] = f"The push destination ({slug}) differs from the fetch origin ({fetch_slug}); the push destination was checked."
    if v["visibility"] == "public":
        raise RepoError(f"{slug} is a public repository. Personal state only goes to a private one; make it private on GitHub, then save again.")
    if v["visibility"] == "private":
        if v.get("push_allowed") is False:
            raise RepoError(f"{slug} is private but this identity ({v.get('identity') or 'token'}) may not push to it.")
        out["verified"] = True
        return out
    trusted = (db.get_setting(TRUST_SETTING) or {}) if db is not None else {}
    if v["visibility"] == "private_or_missing":
        pushed_before = (db.get_setting("cloud.last_export") or {}).get("pushed_to") == slug if db is not None else False
        if pushed_before:
            out.update({"verified": True, "how": v["how"] + " + an earlier push to this destination succeeded"})
            return out
    if trusted.get(slug):
        out.update({"verified": True, "how": f"confirmed private by you on {trusted[slug].get('at', '')[:10]}"})
        return out
    raise RepoError(f"Could not establish that {slug} is private ({v['how']}). Nothing was pushed. Set GITHUB_TOKEN (or GH_TOKEN) so Hunter can check, or confirm it yourself once: `jobhunter cloud link <repo> --confirm-private`.")


def confirm_private(db: Database, repo_dir) -> dict:
    """The person's own statement that the push destination is private, recorded with the slug and the time."""
    slug = _slug_of(_push_url(repo_dir)) or _origin_slug(repo_dir)
    if not slug:
        raise RepoError("This checkout has no GitHub destination to confirm.")
    if slug == UPSTREAM_SLUG:
        raise RepoError("That is KubeCraft's shared repository; it can never hold personal state.")
    trusted = db.get_setting(TRUST_SETTING) or {}
    trusted[slug] = {"at": now_iso(), "by": "owner"}
    db.set_setting(TRUST_SETTING, trusted)
    return {"confirmed_private": slug, "at": trusted[slug]["at"]}


def state_branch(db: Database, override: str | None = None, repo_dir=None) -> str:
    """The branch that keeps the state. A cloud session may sit on a session branch; the state still goes to this one."""
    b = override or db.get_setting(BRANCH_SETTING)
    if b:
        return b
    cur = _git(repo_dir, "rev-parse", "--abbrev-ref", "HEAD", check=False) if repo_dir else ""
    return cur if cur and cur != "HEAD" else "main"


def _same_state(old: dict, new: dict) -> bool:
    """Equal apart from the export stamp, so a save with nothing new makes no commit."""
    volatile = {"exported_at", "exported_by"}
    return {k: v for k, v in old.items() if k not in volatile} == {k: v for k, v in new.items() if k not in volatile}


STATE_FILES = (f"{STATE_DIR}/{STATE_FILE}", f"{STATE_DIR}/{SEEN_FILE}")  # the only paths Hunter ever commits


def _refuse_symlink(path: Path) -> None:
    import os

    if os.path.islink(path):
        raise RepoError(f"{path} is a symbolic link. Hunter writes its state only to ordinary files; remove the link first.")
    parent = path.parent
    if os.path.islink(parent):
        raise RepoError(f"{parent} is a symbolic link. The state folder must be an ordinary folder inside the copy.")


def save_to_repo(ctx: AppContext, db: Database, repo=None, branch: str | None = None) -> dict:
    """Export the bundle into the state repository, commit exactly the two state files, push (pull --rebase first when a
    remote exists). The person's own index and working changes are left alone (`git commit --only` on the two paths),
    and the outgoing commit is checked to touch nothing else before it is pushed. The file is pretty JSON so a diff on
    GitHub stays readable; `seen_jobs.json` is the human-readable list of emailed jobs."""
    d = repo_dir(db, repo)
    br = state_branch(db, branch, d)
    dest = check_destination(d, db, br) if _has_remote(d) else {"origin": None, "visibility": "local", "verified": True}
    pull_ok = None
    if _has_remote(d):
        pull_ok = _git_ok(d, "pull", "--rebase", "--quiet", "origin", br)
    bundle = export_bundle(ctx, db)
    leaks = check_bundle_privacy(bundle)
    if leaks:
        raise BundleError(f"Refusing to save: forbidden content {leaks}")
    sd = state_dir(d)
    previous = sd / STATE_FILE
    for f in (sd / STATE_FILE, sd / SEEN_FILE):
        _refuse_symlink(f)
    if previous.exists():
        try:
            if _same_state(json.loads(previous.read_text(encoding="utf-8")), bundle) and not _git(d, "status", "--porcelain", "--", *STATE_FILES):
                return {"saved": False, "note": "Nothing changed since the last save.", "path": str(previous), "branch": br}
        except (ValueError, OSError):
            pass
    (sd / STATE_FILE).write_text(json.dumps(bundle, ensure_ascii=False, indent=1), encoding="utf-8")
    seen = {}
    for n in bundle.get("results", {}).get("notifications", []):
        for k in n.get("job_keys", []):
            if n.get("status") in {"sent", "uncertain", "sending"}:
                seen[k] = {"emailed_at": n.get("sent_at") or n.get("created_at"), "status": n.get("status")}
    (sd / SEEN_FILE).write_text(json.dumps(seen, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    if not _git(d, "status", "--porcelain", "--", *STATE_FILES):
        return {"saved": False, "note": "Nothing changed since the last save.", "path": str(sd)}
    from jobhunter.util import hostname

    before = _git(d, "rev-parse", "HEAD", check=False)
    # Only the two state files, whatever else is staged or lying around (a CV next to the code, a stray file in state/):
    # `add` names them exactly and `commit --only` takes just those paths, leaving the person's own index as it was.
    _git(d, "add", "--", *STATE_FILES)
    _git(d, "-c", "user.name=Hunter", "-c", "user.email=hunter@jobhunteros.local", "commit", "-q", "--only", "-m", f"Hunter state {bundle['exported_at']} ({bundle['exported_by']} on {hostname()})", "--", *STATE_FILES)
    touched = [ln for ln in _git(d, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD").splitlines() if ln]
    if set(touched) - set(STATE_FILES):
        _git(d, "reset", "-q", "--soft", before or "HEAD~1")
        raise RepoError(f"The state commit would have carried {sorted(set(touched) - set(STATE_FILES))}. Nothing was committed or pushed.")
    pushed = None
    if _has_remote(d):
        last = ""
        for _ in range(3):
            try:
                _git(d, "push", "--quiet", "origin", f"HEAD:refs/heads/{br}")
                pushed = True
                break
            except RepoError as exc:
                last = str(exc)
                _git(d, "pull", "--rebase", "--quiet", "origin", br, check=False)
        if not pushed:
            raise RepoError(f"Could not push the state to {br}: {last}")
    db.set_setting(BRANCH_SETTING, br)
    db.set_setting("cloud.last_export", {"at": bundle["exported_at"], "path": str(sd / STATE_FILE), "pushed": pushed, "pushed_to": dest.get("push_slug") if pushed else None, "branch": br, "pull_ok": pull_ok})
    return {"saved": True, "pushed": pushed, "pull_ok": pull_ok, "branch": br, "destination": dest, "commit": _git(d, "rev-parse", "--short", "HEAD"), "files": list(STATE_FILES), "path": str(sd / STATE_FILE), "jobs": len(bundle["results"]["jobs"]), "notifications": len(bundle["results"]["notifications"])}


def load_from_repo(ctx: AppContext, db: Database, repo=None, restore: bool = False, branch: str | None = None) -> dict:
    """Pull the state branch and merge its file: `restore=True` on a fresh cloud machine (inputs too),
    `restore=False` on the laptop (results only; notes, favorites, applications are never touched)."""
    d = repo_dir(db, repo)
    br = state_branch(db, branch, d)
    pull_ok = None
    if _has_remote(d):
        pull_ok = _git_ok(d, "pull", "--rebase", "--quiet", "origin", br)
        if not pull_ok:
            # Whatever is on disk may be old. A restore must say so, never pass it off as current.
            return {"loaded": False, "current": False, "note": f"Could not pull the state branch {br} from the remote, so the copy on disk may be out of date. Nothing was restored. Check the network or the repository and try again.", "branch": br}
    db.set_setting(BRANCH_SETTING, br)
    f = d / STATE_DIR / STATE_FILE
    if not f.exists():
        return {"loaded": False, "note": "There is no state/hunter-state.json yet. Save from the computer that has the profile first (`jobhunter cloud save`)."}
    bundle = json.loads(f.read_text(encoding="utf-8"))
    res = import_bundle(ctx, db, bundle, inputs=restore, results=True)
    return {"loaded": True, "current": pull_ok is not False, "pull_ok": pull_ok, "branch": br, "commit": _git(d, "rev-parse", "--short", "HEAD", check=False), "exported_at": bundle.get("exported_at"), "exported_by": bundle.get("exported_by"), **res}


BACKUP_PREFIX = "hunter-update-backup-"
UPSTREAM_FILE = ".jobhunter/upstream.json"
PROTECTED_FILE = ".jobhunter/protected"
DEFAULT_PROTECTED = (STATE_DIR + "/", ".env", UPSTREAM_FILE, PROTECTED_FILE)


def _git_ok(d, *args: str) -> bool:
    import subprocess

    return subprocess.run(["git", "-C", str(d), *args], capture_output=True, text=True).returncode == 0


def _backup_tag_name(d) -> str:
    base = BACKUP_PREFIX + now_iso().replace(":", "").replace("+", "")
    tag, n = base, 1
    while _git_ok(d, "rev-parse", "-q", "--verify", f"refs/tags/{tag}"):
        n += 1
        tag = f"{base}-{n}"
    return tag


def _protected_paths(d) -> list[str]:
    """Paths an update never touches: the state, local secrets, the update bookkeeping, and whatever the
    student lists in .jobhunter/protected (one path per line)."""
    out = list(DEFAULT_PROTECTED)
    f = d / PROTECTED_FILE
    if f.exists():
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and line not in out:
                out.append(line)
    return out


def _dirty_outside_state(d) -> list[str]:
    out = []
    for line in _git(d, "status", "--porcelain", "--untracked-files=no").splitlines():
        path = line[3:].strip()
        if path and not path.startswith(STATE_DIR + "/"):
            out.append(path)
    return out


def _template_base(d, branch: str) -> str | None:
    """The upstream commit this copy's files started from. Recorded by an earlier update in
    .jobhunter/upstream.json; otherwise found by matching the copy's first commit tree against upstream's
    history (GitHub's "Use this template" copies the files exactly, so the trees are identical)."""
    f = d / UPSTREAM_FILE
    if f.exists():
        try:
            commit = json.loads(f.read_text(encoding="utf-8")).get("commit")
            if commit and _git_ok(d, "cat-file", "-e", f"{commit}^{{commit}}") and _git_ok(d, "merge-base", "--is-ancestor", commit, f"upstream/{branch}"):
                return commit
        except (ValueError, OSError, RepoError):
            pass
    roots = _git(d, "rev-list", "--max-parents=0", "HEAD").split()
    trees = {_git(d, "rev-parse", f"{r}^{{tree}}") for r in roots}
    for line in _git(d, "log", "--format=%H %T", "-n", "2000", f"upstream/{branch}").splitlines():
        sha, tree = line.split()
        if tree in trees:
            return sha
    return None


def update_from_upstream(repo=None, upstream_url: str = UPSTREAM_URL, branch: str = "main") -> dict:
    """Bring KubeCraft's newer Hunter into the student's own copy.

    A copy that shares history with upstream (a clone or fork) merges. A copy made from the template has its own
    history: upstream's changes since the version the copy started from are applied as a patch, with the state,
    local secrets and any protected path left alone. Either way: refused while the copy has unsaved changes
    outside state/, a backup tag is set first (`rollback_update` returns to it), and a change that does not
    apply cleanly changes nothing. Never `--allow-unrelated-histories`."""
    d = repo or software_repo()
    if not d:
        raise RepoError("Hunter is not running from a git clone, so there is nothing to update.")
    dirty = _dirty_outside_state(d)
    if dirty:
        return {"updated": False, "note": "Your copy has unsaved changes outside state/: " + ", ".join(dirty[:5]) + ". Commit or discard them first, then update again.", "changes": dirty}
    remotes = _git(d, "remote").split()
    if "upstream" not in remotes:
        _git(d, "remote", "add", "upstream", upstream_url)
    _git(d, "fetch", "--quiet", "upstream", branch)
    before = _git(d, "rev-parse", "HEAD")
    target = _git(d, "rev-parse", f"upstream/{branch}")
    shared = _git(d, "merge-base", "HEAD", f"upstream/{branch}", check=False)
    tag = _backup_tag_name(d)
    import subprocess

    if shared:
        if shared == target:
            return {"updated": False, "note": "Up to date.", "method": "merge", "from": before[:7], "to": target[:7]}
        _git(d, "tag", tag, before)
        res = subprocess.run(["git", "-C", str(d), "-c", "user.name=Hunter", "-c", "user.email=hunter@jobhunteros.local", "merge", "--no-edit", f"upstream/{branch}"], capture_output=True, text=True, timeout=120)
        if res.returncode != 0:
            subprocess.run(["git", "-C", str(d), "merge", "--abort"], capture_output=True, text=True)
            _git(d, "tag", "-d", tag, check=False)
            return {"updated": False, "method": "merge", "note": "KubeCraft's update does not merge cleanly with your copy. Nothing was changed. Ask for help with: " + (res.stdout or res.stderr).strip()[:200]}
        method = "merge"
    else:
        base = _template_base(d, branch)
        if not base:
            return {"updated": False, "method": "patch", "note": "Hunter cannot tell which KubeCraft version your copy started from, so it will not guess. Ask KubeCraft for help (they can add .jobhunter/upstream.json with the starting commit)."}
        if base == target:
            return {"updated": False, "note": "Up to date.", "method": "patch", "from": base[:7], "to": target[:7]}
        excludes = [f":(exclude){p.rstrip('/')}" for p in _protected_paths(d)]
        diff = subprocess.run(["git", "-C", str(d), "diff", "--binary", base, target, "--", ".", *excludes], capture_output=True, text=True, timeout=120)
        if diff.returncode != 0:
            raise RepoError("Could not compute KubeCraft's changes: " + diff.stderr.strip()[:200])
        _git(d, "tag", tag, before)
        if diff.stdout.strip():
            applied = subprocess.run(["git", "-C", str(d), "apply", "--3way", "--index"], input=diff.stdout, capture_output=True, text=True, timeout=120)
            if applied.returncode != 0:
                subprocess.run(["git", "-C", str(d), "reset", "--hard", "-q", tag], capture_output=True, text=True)
                _git(d, "tag", "-d", tag, check=False)
                return {"updated": False, "method": "patch", "note": "KubeCraft's update does not apply cleanly to your copy (a file you changed was changed upstream too). Nothing was changed. Ask for help with: " + (applied.stderr or applied.stdout).strip()[:200]}
        f = d / UPSTREAM_FILE
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps({"commit": target, "from": base, "updated_at": now_iso(), "upstream": upstream_url}, indent=1) + "\n", encoding="utf-8")
        _git(d, "add", "-f", "--", UPSTREAM_FILE)
        _git(d, "-c", "user.name=Hunter", "-c", "user.email=hunter@jobhunteros.local", "commit", "-q", "-m", f"Hunter update to KubeCraft {target[:7]}")
        method = "patch"
    after = _git(d, "rev-parse", "HEAD")
    changed = _git(d, "diff", "--name-only", before, after).split() if before != after else []
    touched_state = [x for x in changed if x.startswith(STATE_DIR + "/")]
    return {"updated": before != after, "method": method, "from": before[:7], "to": after[:7], "upstream": target[:7], "files_changed": len(changed), "state_touched": touched_state, "backup_tag": tag, "note": "Up to date." if before == after else f"Hunter is updated (backup tag {tag}; `jobhunter cloud update --rollback` returns to it). Run `uv sync` once, then restart the dashboard if it is open."}


def rollback_update(repo=None) -> dict:
    """Return to the tree before the last update (its backup tag). Refused while there are unsaved changes."""
    d = repo or software_repo()
    if not d:
        raise RepoError("Hunter is not running from a git clone, so there is nothing to roll back.")
    tags = sorted(t for t in _git(d, "tag", "--list", BACKUP_PREFIX + "*").split() if t)
    if not tags:
        return {"rolled_back": False, "note": "No update to roll back: no backup tag exists."}
    dirty = _git(d, "status", "--porcelain", "--untracked-files=no").strip()
    if dirty:
        return {"rolled_back": False, "note": "Your copy has unsaved changes. Save or discard them first (`jobhunter cloud save` for the state), then roll back."}
    tag = tags[-1]
    _git(d, "reset", "--hard", "-q", tag)
    _git(d, "tag", "-d", tag, check=False)
    return {"rolled_back": True, "to": tag, "head": _git(d, "rev-parse", "--short", "HEAD"), "note": "Back to the version before the last update. Run `uv sync` once."}
