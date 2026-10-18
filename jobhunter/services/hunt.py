"""Hunt orchestration: fetch all enabled sources, normalize, deduplicate,
update availability, discover new employers, then match.

One failing source never aborts the others. A partial feed is recorded as
partial and never used to close jobs. Overlapping runs are prevented with a
file lock plus a database row with a heartbeat.
"""

from __future__ import annotations

import json
import os
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import timedelta

from jobhunter.config import HTTP_MAX_CONCURRENCY, AppContext
from jobhunter.db import Database
from jobhunter.models import FetchResult, RawPosting
from jobhunter.services import jobs as jobs_svc
from jobhunter.services import original_lookup
from jobhunter.services.catalog import ensure_source, source_config, source_id_for
from jobhunter.services.http import SafeClient
from jobhunter.services.locks import FileLock, LockHeld
from jobhunter.services.sources import get_adapter
from jobhunter.services.sources.registry import ATS_HOSTS, PROVIDERS
from jobhunter.util import hostname, now, now_iso, parse_iso

STALE_RUN_MINUTES = 20
MAX_NEW_SOURCES_PER_HUNT = 5
REMOTIVE_MIN_INTERVAL_HOURS = 6


class HuntInProgress(Exception):
    pass


def _owner() -> str:
    return f"{hostname()}:{os.getpid()}"


def _reap_stale_runs(db: Database) -> None:
    cutoff = (now() - timedelta(minutes=STALE_RUN_MINUTES)).isoformat()
    db.execute(
        "UPDATE hunt_runs SET status = 'aborted', finished_at = ?, error = 'No heartbeat; the process probably stopped.' "
        "WHERE status = 'running' AND COALESCE(heartbeat_at, started_at) < ?",
        (now_iso(), cutoff),
    )


class HuntNotAllowed(Exception):
    """Live hunting needs readiness and an approved profile. Enforced here, not only in the UI."""


def run_hunt(ctx: AppContext, db: Database, trigger: str = "manual", source_ids: list[str] | None = None, client: SafeClient | None = None, progress=None) -> dict:
    """Run one hunt. Raises HuntInProgress if another run holds the lock, HuntNotAllowed before setup is done."""
    from jobhunter.services import onboarding

    ok, why = onboarding.live_hunting_allowed(db, ctx.demo)
    if not ok:
        raise HuntNotAllowed(why)
    lock = FileLock(ctx.paths.locks_dir / "hunt.lock")
    try:
        lock.acquire()
    except LockHeld as exc:
        raise HuntInProgress("A hunt is already running. Wait for it to finish.") from exc
    try:
        _reap_stale_runs(db)
        if db.fetchone("SELECT id FROM hunt_runs WHERE status = 'running'"):
            raise HuntInProgress("A hunt is already running. Wait for it to finish.")
        run_id = db.insert("hunt_runs", {"trigger": trigger, "status": "running", "started_at": now_iso(), "heartbeat_at": now_iso(), "owner": _owner()})
        try:
            stats = _execute(ctx, db, run_id, source_ids, client, progress)
            status = "completed" if not stats["sources_failed"] and not stats["sources_partial"] else "partial"
            if stats["sources_ok"] == 0 and (stats["sources_failed"] or stats["sources_partial"]):
                status = "failed" if stats["sources_partial"] == 0 else "partial"
            if stats.get("problem"):
                status = "no_sources"  # nothing was checked, so this is never a successful search
            db.update("hunt_runs", {"status": status, "finished_at": now_iso(), "stats_json": json.dumps(stats), "error": stats.get("note")}, "id = ?", (run_id,))
            db.execute("UPDATE schedule SET last_run_at = ? WHERE id = 1", (now_iso(),))
            stats["run_id"] = run_id
            stats["status"] = status
            return stats
        except Exception as exc:
            db.update("hunt_runs", {"status": "failed", "finished_at": now_iso(), "error": f"{type(exc).__name__}: {exc}"}, "id = ?", (run_id,))
            raise
    finally:
        lock.release()


def user_regions(db: Database) -> set[str]:
    """Regions implied by the person's work countries: 'na', 'eu', or both. Empty = unknown."""
    from jobhunter.services import preferences as prefs_svc
    from jobhunter.services.preferences import EUROPE, NORTH_AMERICA

    codes = set(prefs_svc.get(db).work_countries or [])
    out = set()
    if codes & NORTH_AMERICA:
        out.add("na")
    if codes & EUROPE:
        out.add("eu")
    return out


def _enabled_sources(db: Database, source_ids: list[str] | None) -> list[dict]:
    if source_ids:
        marks = ",".join("?" for _ in source_ids)
        return db.fetchall(f"SELECT * FROM watch_sources WHERE id IN ({marks})", tuple(source_ids))
    rows = db.fetchall(
        "SELECT w.*, e.regions_json FROM watch_sources w LEFT JOIN employers e ON e.id = w.employer_id WHERE w.enabled = 1 ORDER BY w.id"
    )
    regions = user_regions(db)
    if not regions:
        return rows
    kept = []
    for r in rows:
        # Feeds are region-tagged by kind; employer boards by the employer's hiring regions.
        if r["kind"] == "arbeitnow" and "eu" not in regions:
            continue
        emp_regions = set(json.loads(r.get("regions_json") or "[]"))
        if emp_regions and not (emp_regions & regions):
            continue
        kept.append(r)
    return kept


def _should_skip(source: dict) -> str | None:
    if source["kind"] == "remotive" and source.get("last_success_at"):
        last = parse_iso(source["last_success_at"])
        if last and (now() - last) < timedelta(hours=REMOTIVE_MIN_INTERVAL_HOURS):
            return f"Remotive asks for few requests per day; last read {int((now() - last).total_seconds() // 3600)}h ago, reusing it."
    return None


def ensure_sources_initialized(db: Database) -> dict:
    """First use: load the starter catalogue once. Idempotent, and it never re-enables a source the person turned off."""
    if db.get_setting("catalog.synced_at"):
        return {"initialized": False}
    if db.fetchone("SELECT 1 FROM watch_sources LIMIT 1"):
        # The person (or a test) set up sources by hand before the first search: respect that choice.
        return {"initialized": False, "note": "sources already exist; run `jobhunter sources sync-catalog` to add the starter catalogue"}
    from jobhunter.services import catalog

    res = catalog.sync_catalog(db)
    return {"initialized": True, **res}


def sources_state(db: Database, source_ids: list[str] | None = None) -> tuple[list[dict], str | None, str | None]:
    """(sources to check, problem code, plain message). Distinguishes 'nothing set up' from 'you turned them off'."""
    sources = _enabled_sources(db, source_ids)
    if sources:
        return sources, None, None
    total = db.fetchone("SELECT COUNT(*) AS n FROM watch_sources")["n"]
    enabled = db.fetchone("SELECT COUNT(*) AS n FROM watch_sources WHERE enabled = 1")["n"]
    if total == 0:
        return [], "no_sources", "There are no job sources yet. Add an employer board under Search health, or run `jobhunter sources sync-catalog`."
    if enabled == 0:
        return [], "all_disabled", "All job sources are switched off. Turn some on under Search health and search again."
    return [], "none_for_countries", "None of the enabled job sources cover your countries. Add an employer board for your country, or check your countries under Profile & settings."


def _execute(ctx: AppContext, db: Database, run_id: int, source_ids: list[str] | None, client: SafeClient | None, progress) -> dict:
    ensure_sources_initialized(db)
    sources, problem, note = sources_state(db, source_ids)
    if problem:
        return {"sources_total": 0, "sources_ok": 0, "sources_partial": 0, "sources_failed": 0, "sources_skipped": 0, "postings_fetched": 0, "jobs_new": 0, "jobs_updated": 0, "jobs_relevant_new": 0, "jobs_closed": 0, "employers_new": 0, "sources_added": 0, "failures": [], "problem": problem, "note": note}
    own_client = client is None
    client = client or SafeClient()
    stats = {
        "sources_total": len(sources),
        "sources_ok": 0,
        "sources_partial": 0,
        "sources_failed": 0,
        "sources_skipped": 0,
        "postings_fetched": 0,
        "jobs_new": 0,
        "jobs_updated": 0,
        "jobs_relevant_new": 0,
        "jobs_closed": 0,
        "employers_new": 0,
        "sources_added": 0,
        "failures": [],
    }
    results: dict[str, FetchResult] = {}
    try:
        with ThreadPoolExecutor(max_workers=HTTP_MAX_CONCURRENCY) as pool:
            futures = {}
            for s in sources:
                skip = _should_skip(s)
                if skip:
                    results[s["id"]] = FetchResult(source_id=s["id"], status="skipped", error=skip, complete=False)
                    continue
                futures[pool.submit(_fetch_one, client, s)] = s
            for fut in as_completed(futures):
                s = futures[fut]
                try:
                    results[s["id"]] = fut.result()
                except Exception as exc:  # adapter bug: still isolate the failure
                    results[s["id"]] = FetchResult(source_id=s["id"], status="failed", error=f"{type(exc).__name__}: {exc}", complete=False)
                db.update("hunt_runs", {"heartbeat_at": now_iso()}, "id = ?", (run_id,))
                if progress:
                    progress(s["id"], results[s["id"]].status)
        employers_before = db.fetchone("SELECT COUNT(*) AS n FROM employers")["n"]
        for s in sources:
            res = results.get(s["id"])
            if not res:
                continue
            _record_source_run(db, run_id, s, res, stats)
        stats["employers_new"] = db.fetchone("SELECT COUNT(*) AS n FROM employers")["n"] - employers_before
        stats["sources_added"] = _promote_discovered_employers(db, client, limit=MAX_NEW_SOURCES_PER_HUNT)
        _lookup_originals(db, client, run_id)
    finally:
        original_lookup.clear_cache()
        if own_client:
            client.close()
    # Matching runs after discovery; failures there must not lose fetched jobs.
    try:
        from jobhunter.services.matching import engine

        stats["matching"] = engine.run_matching(ctx, db, hunt_run_id=run_id)
    except Exception as exc:
        stats["matching"] = {"status": "failed", "error": f"{type(exc).__name__}: {exc}", "trace": traceback.format_exc()[-800:]}
    return stats


def _fetch_one(client: SafeClient, source: dict) -> FetchResult:
    adapter = get_adapter(source["kind"])
    cfg = json.loads(source["config_json"] or "{}")
    if source["kind"] == "adzuna":
        from jobhunter.services.secrets import SecretStore  # keys may live in the secret store

        try:
            store = SecretStore(AppContext.from_env().paths)
            cfg.setdefault("app_id", store.get("adzuna.app_id"))
            cfg.setdefault("app_key", store.get("adzuna.app_key"))
        except Exception:
            pass
    return adapter.fetch(client, source["id"], cfg)


def _record_source_run(db: Database, run_id: int, source: dict, res: FetchResult, stats: dict) -> None:
    ts = now_iso()
    employer_hosted = PROVIDERS.get(source["kind"], {}).get("employer_hosted", False)
    fetched = len(res.postings)
    new = relevant_new = updated = 0
    seen_ext: set[str] = set()
    with db.tx():
        for p in res.postings:
            if not p.title or not p.external_id:
                continue
            try:
                job_id, is_new, changed = jobs_svc.upsert_from_posting(db, p, run_id, employer_hosted)
            except Exception:
                continue
            seen_ext.add(p.external_id)
            if is_new:
                new += 1
                row = db.fetchone("SELECT relevance FROM jobs WHERE id = ?", (job_id,))
                if row and row["relevance"] in {"relevant", "uncertain"}:
                    relevant_new += 1
            elif changed:
                updated += 1
        closed = 0
        if res.status == "ok" and res.complete and employer_hosted:
            closed = jobs_svc.mark_missing_as_closed(db, source["id"], seen_ext, ts)
        elif res.status == "failed":
            jobs_svc.mark_unverified(db, source["id"], f"Could not check {source['id']} on {ts[:10]}", ts)
        db.insert(
            "source_runs",
            {
                "hunt_run_id": run_id,
                "source_id": source["id"],
                "status": res.status,
                "started_at": ts,
                "finished_at": now_iso(),
                "fetched_count": fetched,
                "relevant_count": relevant_new,
                "new_count": new,
                "http_status": res.http_status,
                "error": res.error,
                "notes": res.notes if res.complete else ((res.notes or "") + " Incomplete snapshot: not used to close jobs.").strip(),
            },
        )
        values = {"last_run_at": ts, "last_status": res.status, "last_error": res.error}
        if res.status in {"ok", "partial"}:
            values["last_success_at"] = ts
            values["consecutive_failures"] = 0
        elif res.status == "failed":
            values["consecutive_failures"] = int(source.get("consecutive_failures") or 0) + 1
        db.update("watch_sources", values, "id = ?", (source["id"],))
    stats["postings_fetched"] += fetched
    stats["jobs_new"] += new
    stats["jobs_updated"] += updated
    stats["jobs_relevant_new"] += relevant_new
    stats["jobs_closed"] += closed
    key = {"ok": "sources_ok", "partial": "sources_partial", "failed": "sources_failed", "skipped": "sources_skipped"}[res.status]
    stats[key] += 1
    if res.status in {"failed", "partial"} and res.error:
        stats["failures"].append({"source": source["id"], "status": res.status, "error": res.error})


def _promote_discovered_employers(db: Database, client: SafeClient, limit: int) -> int:
    """Discovered employers whose job links reveal a public ATS board become watched sources
    after one successful read. Bounded per hunt."""
    added = 0
    rows = db.fetchall(
        "SELECT j.id, j.employer_id, j.employer_name, j.discovery_url, j.apply_url, j.direct_url FROM jobs j JOIN employers e ON e.id = j.employer_id "
        "WHERE e.origin = 'discovered' AND e.ats_token IS NULL AND j.relevance IN ('relevant','uncertain') ORDER BY j.first_seen_at DESC LIMIT 200"
    )
    tried: set[str] = set()
    for r in rows:
        if added >= limit:
            break
        for url in (r["direct_url"], r["apply_url"], r["discovery_url"]):
            from jobhunter.services.sources.import_text import detect_ats_link

            info = detect_ats_link(url or "")
            if not info:
                continue
            token = info.get("token") or info.get("site") or info.get("board")
            sid = source_id_for(info["kind"], token)
            if sid in tried or db.fetchone("SELECT id FROM watch_sources WHERE id = ?", (sid,)):
                break
            tried.add(sid)
            cfg = source_config(info["kind"], token, r["employer_name"] or token, info.get("region"))
            res = get_adapter(info["kind"]).fetch(client, sid, cfg)
            if res.status in {"ok", "partial"} and res.postings:
                with db.tx():
                    db.update("employers", {"ats_kind": info["kind"], "ats_token": token, "ats_region": info.get("region"), "last_checked_at": now_iso(), "last_check_status": "ok"}, "id = ?", (r["employer_id"],))
                    if ensure_source(db, sid, info["kind"], cfg, "discovered", r["employer_id"]):
                        added += 1
            break
    return added


def _lookup_originals(db: Database, client: SafeClient, run_id: int) -> None:
    rows = db.fetchall(
        "SELECT * FROM jobs WHERE direct_url_status IN ('none') AND relevance IN ('relevant','uncertain') "
        "AND id IN (SELECT job_id FROM job_observations WHERE hunt_run_id = ?) LIMIT 40",
        (run_id,),
    )
    for job in rows:
        try:
            original_lookup.resolve(db, job, client)
        except Exception:
            continue


def last_run(db: Database) -> dict | None:
    row = db.fetchone("SELECT * FROM hunt_runs ORDER BY id DESC LIMIT 1")
    if row and row.get("stats_json"):
        row["stats"] = json.loads(row["stats_json"])
    return row


def last_successful_run(db: Database) -> dict | None:
    return db.fetchone("SELECT * FROM hunt_runs WHERE status IN ('completed','partial') ORDER BY id DESC LIMIT 1")


def source_health(db: Database) -> list[dict]:
    rows = db.fetchall("SELECT * FROM watch_sources ORDER BY enabled DESC, kind, id")
    out = []
    for r in rows:
        info = PROVIDERS.get(r["kind"], {})
        out.append(
            {
                **r,
                "provider": info.get("name", r["kind"]),
                "employer_hosted": info.get("employer_hosted", False),
                "plain_status": _plain_status(r),
            }
        )
    return out


def _plain_status(r: dict) -> str:
    if not r.get("enabled"):
        return "Off"
    if r.get("last_status") == "ok":
        return "Working"
    if r.get("last_status") == "partial":
        return "Partly working: " + (r.get("last_error") or "some pages could not be read")
    if r.get("last_status") == "failed":
        return "I couldn't check this source. Your other searches still worked."
    if r.get("last_status") == "skipped":
        return "Skipped this time: " + (r.get("last_error") or "")
    return "Not checked yet"


def import_postings(ctx: AppContext, db: Database, postings: list[RawPosting], trigger: str = "import") -> dict:
    """Store user-imported postings (pasted or from alert emails) and run matching."""
    run_id = db.insert("hunt_runs", {"trigger": trigger, "status": "running", "started_at": now_iso(), "owner": _owner()})
    new = 0
    ids = []
    with db.tx():
        for p in postings:
            job_id, is_new, _ = jobs_svc.upsert_from_posting(db, p, run_id, employer_hosted=bool(ATS_HOSTS.get(_host(p.url))))
            ids.append(job_id)
            new += int(is_new)
        db.update("hunt_runs", {"status": "completed", "finished_at": now_iso(), "stats_json": json.dumps({"imported": len(postings), "jobs_new": new})}, "id = ?", (run_id,))
    try:
        from jobhunter.services.matching import engine

        matching = engine.run_matching(ctx, db, hunt_run_id=run_id, job_ids=ids)
    except Exception as exc:
        matching = {"status": "failed", "error": str(exc)}
    return {"run_id": run_id, "imported": len(postings), "jobs_new": new, "job_ids": ids, "matching": matching}


def _host(url: str | None) -> str:
    from urllib.parse import urlparse

    try:
        return (urlparse(url or "").hostname or "").lower()
    except ValueError:
        return ""
