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
    # Only what the other side cannot re-derive travels: the jobs an email or an exclusion refers to, with their
    # match outcome. Everything else a hunt finds again deterministically. Claude carries this file through its own
    # context, so small is not a nicety; it is what makes the round trip possible at all.
    keys = referenced | excl_keys
    jobs = db.fetchall(f"SELECT * FROM jobs WHERE canonical_key IN ({','.join('?' for _ in keys)}) AND (first_seen_at >= ? OR last_seen_at >= ?) ORDER BY id", (*keys, since, since)) if keys else []
    seen = referenced
    emp_ids = {j["employer_id"] for j in jobs if j.get("employer_id")}
    employers = db.fetchall(f"SELECT * FROM employers WHERE id IN ({','.join('?' for _ in emp_ids)})", tuple(emp_ids)) if emp_ids else []
    emp_by_id = {e["id"]: e["canonical_name"] for e in employers}
    prof = profile_svc.approved(db)
    matches = {m["job_id"]: m for m in db.fetchall("SELECT * FROM matches WHERE profile_version_id = ? AND policy_version = ?", (prof["id"], _policy_version()))} if prof else {}
    runs = []
    for r in db.fetchall("SELECT * FROM hunt_runs WHERE trigger IN ('scheduled','cloud') AND status != 'running' ORDER BY id DESC LIMIT 20"):
        runs.append({"tag": r.get("owner") if str(r.get("owner") or "").startswith("cloud:") else f"run:{r['started_at']}", "trigger": r["trigger"], "status": r["status"], "started_at": r["started_at"], "finished_at": r.get("finished_at"), "stats": json.loads(r.get("stats_json") or "{}"), "error": r.get("error")})
    return {
        "employers": [{k: e.get(k) for k in EMPLOYER_COLUMNS} for e in employers],
        "jobs": [_job_row(j, emp_by_id, matches.get(j["id"]), snippet=j["canonical_key"] in seen) for j in jobs],
        "hunt_runs": runs,
        "notifications": notes,
    }


DESCRIPTION_CAP = 300  # a snippet; the match travels with the job, and the cache key uses the untouched description_hash


def _job_row(j: dict, emp_by_id: dict, match: dict | None, snippet: bool) -> dict:
    row = {k: j.get(k) for k in JOB_COLUMNS}
    text = row.get("description_text") or ""
    row["description_text"] = (text[:DESCRIPTION_CAP] + (" …" if len(text) > DESCRIPTION_CAP else "")) if snippet else None
    row["employer_canonical_name"] = emp_by_id.get(j.get("employer_id"))
    row["match"] = {k: match.get(k) for k in MATCH_COLUMNS} if match else None
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
        matches_added = _import_matches(db, results.get("jobs") or [], key_to_id)
        notes_added = 0
        for n in results.get("notifications") or []:
            if not n.get("delivery_key") or db.fetchone("SELECT id FROM notifications WHERE delivery_key = ?", (n["delivery_key"],)):
                continue
            ids = [key_to_id[k] for k in n.get("job_keys") or [] if k in key_to_id]
            db.insert("notifications", {k: n.get(k) for k in ("kind", "channel", "recipient", "subject", "content_hash", "status", "attempts", "provider_message_id", "error", "created_at", "sent_at", "delivery_key", "claimed_at", "resolved_at", "resolution")} | {"job_ids_json": json.dumps(ids), "body_path": None})
            notes_added += 1
    return {"jobs_added": added, "jobs_refreshed": refreshed, "matches_added": matches_added, "hunt_runs_added": runs_added, "notifications_added": notes_added}


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
        row = db.fetchone("SELECT * FROM jobs WHERE id = ?", (jid,))
        values = {k: m.get(k) for k in MATCH_COLUMNS} | {"job_id": jid, "profile_version_id": prof["id"], "policy_version": engine.POLICY_VERSION, "cache_key": engine.cache_key(row, pkey, phash), "computed_at": now_iso()}
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


def _origin_slug(repo_dir) -> str | None:
    url = _git(repo_dir, "remote", "get-url", "origin", check=False)
    m = re.search(r"github\.com[:/]+([^/]+)/([^/\s]+?)(?:\.git)?/?$", url or "")
    return f"{m.group(1)}/{m.group(2)}".lower() if m else None


def _github_visibility(slug: str) -> str:
    """'public', 'private_or_missing', or 'unknown' (no network). No token: a public repository answers an
    anonymous request, a private one looks like it does not exist. That is exactly the distinction needed."""
    try:
        import httpx

        r = httpx.get(f"https://api.github.com/repos/{slug}", timeout=8, headers={"Accept": "application/vnd.github+json", "User-Agent": "jobhunteros"})
    except Exception:
        return "unknown"
    if r.status_code == 200:
        try:
            return "public" if not r.json().get("private") else "private_or_missing"
        except ValueError:
            return "public"
    return "private_or_missing" if r.status_code in {404, 403} else "unknown"


def check_destination(repo_dir) -> dict:
    """Personal state only ever goes to the student's own private copy: never to a public repository and never
    to KubeCraft's shared one. Raises RepoError otherwise."""
    slug = _origin_slug(repo_dir)
    if slug == UPSTREAM_SLUG:
        raise RepoError("This checkout's origin is KubeCraft's shared repository. Personal state never goes there: use your own private copy of Hunter (see docs/cloud-routine.md).")
    if slug:
        vis = _github_visibility(slug)
        if vis == "public":
            raise RepoError(f"{slug} is a public repository. Personal state only goes to a private one; make it private on GitHub, then save again.")
        return {"origin": slug, "visibility": vis}
    return {"origin": None, "visibility": "local"}


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


def save_to_repo(ctx: AppContext, db: Database, repo=None, branch: str | None = None) -> dict:
    """Export the bundle into the state repository, commit, push (pull --rebase first when a remote exists).
    The file is pretty JSON so a diff on GitHub stays readable; `seen_jobs.json` is the human-readable
    list of emailed jobs, the one thing the original tiny Hunter kept."""
    d = repo_dir(db, repo)
    br = state_branch(db, branch, d)
    dest = check_destination(d) if _has_remote(d) else {"origin": None, "visibility": "local"}
    if _has_remote(d):
        _git(d, "pull", "--rebase", "--quiet", "origin", br, check=False)
    bundle = export_bundle(ctx, db)
    leaks = check_bundle_privacy(bundle)
    if leaks:
        raise BundleError(f"Refusing to save: forbidden content {leaks}")
    sd = state_dir(d)
    previous = sd / STATE_FILE
    if previous.exists():
        try:
            if _same_state(json.loads(previous.read_text(encoding="utf-8")), bundle) and not _git(d, "status", "--porcelain", "--", STATE_DIR):
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
    # Only the state directory is ever staged: a stray CV or database next to the code never gets committed.
    _git(d, "add", "--", STATE_DIR)
    if not _git(d, "status", "--porcelain", "--", STATE_DIR):
        return {"saved": False, "note": "Nothing changed since the last save.", "path": str(sd)}
    from jobhunter.util import hostname

    _git(d, "-c", "user.name=Hunter", "-c", "user.email=hunter@jobhunteros.local", "commit", "-q", "-m", f"Hunter state {bundle['exported_at']} ({bundle['exported_by']} on {hostname()})")
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
    db.set_setting("cloud.last_export", {"at": bundle["exported_at"], "path": str(sd / STATE_FILE), "pushed": pushed, "branch": br})
    return {"saved": True, "pushed": pushed, "branch": br, "destination": dest, "commit": _git(d, "rev-parse", "--short", "HEAD"), "path": str(sd / STATE_FILE), "jobs": len(bundle["results"]["jobs"]), "notifications": len(bundle["results"]["notifications"])}


def load_from_repo(ctx: AppContext, db: Database, repo=None, restore: bool = False, branch: str | None = None) -> dict:
    """Pull the state branch and merge its file: `restore=True` on a fresh cloud machine (inputs too),
    `restore=False` on the laptop (results only; notes, favorites, applications are never touched)."""
    d = repo_dir(db, repo)
    br = state_branch(db, branch, d)
    if _has_remote(d):
        _git(d, "pull", "--rebase", "--quiet", "origin", br, check=False)
    db.set_setting(BRANCH_SETTING, br)
    f = d / STATE_DIR / STATE_FILE
    if not f.exists():
        return {"loaded": False, "note": "There is no state/hunter-state.json yet. Save from the computer that has the profile first (`jobhunter cloud save`)."}
    bundle = json.loads(f.read_text(encoding="utf-8"))
    res = import_bundle(ctx, db, bundle, inputs=restore, results=True)
    return {"loaded": True, "branch": br, "commit": _git(d, "rev-parse", "--short", "HEAD", check=False), "exported_at": bundle.get("exported_at"), "exported_by": bundle.get("exported_by"), **res}


def update_from_upstream(repo=None, upstream_url: str = UPSTREAM_URL, branch: str = "main") -> dict:
    """Bring KubeCraft's newer Hunter into the student's own copy. Upstream never touches `state/`, so a
    merge cannot conflict with personal state; a conflict in code is reported, never resolved blindly."""
    d = repo or software_repo()
    if not d:
        raise RepoError("Hunter is not running from a git clone, so there is nothing to update.")
    remotes = _git(d, "remote").split()
    if "upstream" not in remotes:
        _git(d, "remote", "add", "upstream", upstream_url)
    _git(d, "fetch", "--quiet", "upstream", branch)
    before = _git(d, "rev-parse", "--short", "HEAD")
    import subprocess

    res = subprocess.run(["git", "-C", str(d), "-c", "user.name=Hunter", "-c", "user.email=hunter@jobhunteros.local", "merge", "--no-edit", f"upstream/{branch}"], capture_output=True, text=True, timeout=120)
    if res.returncode != 0:
        subprocess.run(["git", "-C", str(d), "merge", "--abort"], capture_output=True, text=True)
        return {"updated": False, "note": "KubeCraft's update does not merge cleanly with your copy. Nothing was changed. Ask for help with: " + (res.stdout or res.stderr).strip()[:200]}
    after = _git(d, "rev-parse", "--short", "HEAD")
    changed = _git(d, "diff", "--name-only", before, after).split() if before != after else []
    touched_state = [f for f in changed if f.startswith(STATE_DIR + "/")]
    return {"updated": before != after, "from": before, "to": after, "files_changed": len(changed), "state_touched": touched_state, "note": "Up to date." if before == after else "Hunter is updated. Run `uv sync` once, then restart the dashboard if it is open."}
