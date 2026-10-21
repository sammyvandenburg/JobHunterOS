"""Hunt orchestration: fetch all enabled sources, normalize, deduplicate,
update availability, discover new employers, then match.

One failing source never aborts the others. A partial feed is recorded as
partial and never used to close jobs. Overlapping runs are prevented with a
file lock plus a database row with a heartbeat.
"""

from __future__ import annotations

import json
import os
import time
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

STALE_RUN_MINUTES = 5  # only for a run whose process cannot be checked (another machine): no heartbeat this long = dead
QUEUED_STALE_MINUTES = 2  # a 'queued' row whose thread never started
SOURCE_STAGE_BUDGET_SECONDS = 480.0  # a source that has not answered by then is "couldn't check", the search moves on
MAX_NEW_SOURCES_PER_HUNT = 5
REMOTIVE_MIN_INTERVAL_HOURS = 6
MATCHING_FAILED_NOTE = "Jobs were found and saved, but comparing them with your profile failed: {err}. Search again, or run `jobhunter match run`."


class HuntInProgress(Exception):
    pass


def _owner() -> str:
    return f"{hostname()}:{os.getpid()}"


def _owner_alive(owner: str | None) -> bool | None:
    """True/False when the owner ran on this machine and we can ask the OS; None when we cannot tell."""
    if not owner or ":" not in owner:
        return None
    host, _, pid = owner.rpartition(":")
    if host != hostname():
        return None
    try:
        pid_n = int(pid)
    except ValueError:
        return None
    if pid_n == os.getpid():
        return True
    try:
        os.kill(pid_n, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None
    return True


def _reap_stale_runs(db: Database, lock_held: bool = False, keep_id: int | None = None) -> int:
    """Mark dead runs as aborted at once, so nobody waits for an arbitrary timeout.

    A run is dead when its process is gone (asked from the OS on the same machine), when we hold the
    exclusive hunt lock and the run claims this machine (then nothing else can be hunting here), or, for a
    run from another machine, when its heartbeat is older than STALE_RUN_MINUTES. A 'queued' row whose
    thread never started is dead after QUEUED_STALE_MINUTES."""
    reaped = 0
    cutoff = (now() - timedelta(minutes=STALE_RUN_MINUTES)).isoformat()
    queued_cutoff = (now() - timedelta(minutes=QUEUED_STALE_MINUTES)).isoformat()
    for r in db.fetchall("SELECT id, status, owner, heartbeat_at, started_at FROM hunt_runs WHERE status IN ('running','queued')"):
        if keep_id is not None and r["id"] == keep_id:
            continue
        alive = _owner_alive(r.get("owner"))
        same_host = bool(r.get("owner")) and r["owner"].rpartition(":")[0] == hostname()
        beat = r.get("heartbeat_at") or r.get("started_at") or ""
        if alive is False:
            why = "The process that ran this search is gone."
        elif lock_held and same_host and alive is not True:
            why = "No other search can be running on this computer; this one was left behind."
        elif alive is None and beat < cutoff:
            why = "No heartbeat for a while; the process probably stopped."
        elif r["status"] == "queued" and beat < queued_cutoff and alive is not True:
            why = "The search never started."
        else:
            continue
        db.update("hunt_runs", {"status": "aborted", "finished_at": now_iso(), "error": why}, "id = ?", (r["id"],))
        reaped += 1
    return reaped


class HuntNotAllowed(Exception):
    """Live hunting needs readiness and an approved profile. Enforced here, not only in the UI."""


class DashboardNotRead(HuntNotAllowed):
    """A dashboard is linked and its actions have not been read since the last sync: an applied or dismissed
    job could otherwise be recommended again."""


def board_stale(db: Database) -> str | None:
    """Why the dashboard's actions must be read first, or None when they are current (or no dashboard)."""
    from jobhunter.services import board

    return board.stale_reason(db)


def run_hunt(ctx: AppContext, db: Database, trigger: str = "manual", source_ids: list[str] | None = None, client: SafeClient | None = None, progress=None, run_id: int | None = None, allow_stale_board: bool = False, deadline_seconds: float | None = None) -> dict:
    """Run one hunt. Raises HuntInProgress if another live run holds the lock, HuntNotAllowed before setup is
    done, DashboardNotRead when a linked dashboard's actions were not read since the last sync (unless
    `allow_stale_board`, for callers that cannot read the page, such as the local dashboard and the worker).

    `run_id` is set when `start_background` already created the row (status 'queued') so the caller has
    an id to poll before any work starts.
    """
    from jobhunter.services import onboarding

    ok, why = onboarding.live_hunting_allowed(db, ctx.demo)
    if not ok:
        if run_id:
            db.update("hunt_runs", {"status": "failed", "finished_at": now_iso(), "error": why}, "id = ?", (run_id,))
        raise HuntNotAllowed(why)
    stale = None if allow_stale_board else board_stale(db)
    if stale:
        if run_id:
            db.update("hunt_runs", {"status": "failed", "finished_at": now_iso(), "error": stale}, "id = ?", (run_id,))
        raise DashboardNotRead(stale)
    lock = FileLock(ctx.paths.locks_dir / "hunt.lock")
    try:
        lock.acquire()
    except LockHeld as exc:
        if run_id:
            db.update("hunt_runs", {"status": "aborted", "finished_at": now_iso(), "error": "Another search was already running."}, "id = ?", (run_id,))
        raise HuntInProgress("A hunt is already running. Wait for it to finish.") from exc
    try:
        _reap_stale_runs(db, lock_held=True, keep_id=run_id)
        other = db.fetchone("SELECT id FROM hunt_runs WHERE status IN ('running','queued') AND id != ?", (run_id or -1,))
        if other:
            if run_id:
                db.update("hunt_runs", {"status": "aborted", "finished_at": now_iso(), "error": "Another search was already running."}, "id = ?", (run_id,))
            raise HuntInProgress("A hunt is already running. Wait for it to finish.")
        if run_id:
            db.update("hunt_runs", {"status": "running", "started_at": now_iso(), "heartbeat_at": now_iso(), "owner": _owner()}, "id = ?", (run_id,))
        else:
            run_id = db.insert("hunt_runs", {"trigger": trigger, "status": "running", "started_at": now_iso(), "heartbeat_at": now_iso(), "owner": _owner()})
        try:
            stats = _execute(ctx, db, run_id, source_ids, client, progress, deadline_seconds=deadline_seconds)
            from jobhunter.services import board as _board

            stats["seq"] = _board._next_seq(db)  # orders this hunt against dashboard reads and writes in the same second
            stats["unresolved_applied"] = len(_board.apply_unresolved(db))  # page actions for jobs this hunt just found
            status = "completed" if not stats["sources_failed"] and not stats["sources_partial"] else "partial"
            if stats.get("deadline_hit") and status == "completed":
                status = "partial"
            if stats["sources_ok"] == 0 and (stats["sources_failed"] or stats["sources_partial"]):
                status = "failed" if stats["sources_partial"] == 0 else "partial"
            if stats.get("problem"):
                status = "no_sources"  # nothing was checked, so this is never a successful search
            note = stats.get("note")
            if (stats.get("matching") or {}).get("status") == "failed":
                # Jobs without a comparison are not a result the person can use: never call this a success.
                status = "failed"
                note = MATCHING_FAILED_NOTE.format(err=stats["matching"].get("error", "unknown error"))
                stats["note"] = note
            db.update("hunt_runs", {"status": status, "finished_at": now_iso(), "stats_json": json.dumps(stats), "error": note}, "id = ?", (run_id,))
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


STAGE_WORDS = {"starting": "Starting", "comparing stored jobs": "Comparing the jobs already found", "checking sources": "Checking job sites", "discovering employers": "Looking for new employers", "comparing jobs": "Comparing jobs with your profile", "finding original links": "Finding the original listings", "ready": "Ready"}


def _progress_update(db: Database, run_id: int, prog: dict) -> None:
    db.update("hunt_runs", {"heartbeat_at": now_iso(), "progress_json": json.dumps(prog)}, "id = ?", (run_id,))


def progress(db: Database, run_id: int) -> dict | None:
    """Plain, pollable state of one hunt: stage, sources done, new jobs so far."""
    row = db.fetchone("SELECT id, trigger, status, started_at, finished_at, stats_json, progress_json, error FROM hunt_runs WHERE id = ?", (run_id,))
    if not row:
        return None
    prog = json.loads(row.get("progress_json") or "{}")
    st = json.loads(row.get("stats_json") or "{}")
    stage = prog.get("stage") or ("ready" if row["status"] not in {"running", "queued"} else "starting")
    done = int(prog.get("sources_done", 0))
    total = int(prog.get("sources_total", st.get("sources_total", 0) or 0))
    new = int(prog.get("jobs_new", st.get("jobs_new", 0) or 0))
    relevant = int(prog.get("jobs_relevant_new", st.get("jobs_relevant_new", 0) or 0))
    if row["status"] in {"running", "queued"}:
        if stage == "checking sources" and total:
            message = f"Checking job sites: {done} of {total} done. {new} new job{'' if new == 1 else 's'} so far."
        else:
            message = STAGE_WORDS.get(stage, stage) + "…"
    elif row["status"] == "no_sources":
        message = row.get("error") or "No job sources to check."
    elif row["status"] in {"failed", "aborted"}:
        message = "The search stopped: " + (row.get("error") or "unknown problem.")
    else:
        failed = int(st.get("sources_failed", 0) or 0)
        message = f"Done: {new} new job{'' if new == 1 else 's'}, {relevant} relevant" + (f". {failed} site(s) could not be checked." if failed else ".")
    matching = st.get("matching") or prog.get("matching") or {}
    return {"run_id": row["id"], "trigger": row["trigger"], "status": row["status"], "stage": stage, "stage_label": STAGE_WORDS.get(stage, stage), "sources_done": done, "sources_total": total, "jobs_new": new, "jobs_relevant_new": relevant, "message": message, "started_at": row["started_at"], "finished_at": row["finished_at"], "error": row.get("error"), "running": row["status"] in {"running", "queued"}, "matching": {k: matching.get(k) for k in ("status", "presented", "error")} if matching else None, "stages": st.get("stages") or prog.get("stages")}


def running_run(db: Database) -> dict | None:
    row = db.fetchone("SELECT id FROM hunt_runs WHERE status IN ('running','queued') ORDER BY id DESC LIMIT 1")
    return progress(db, row["id"]) if row else None


def start_detached(ctx: AppContext, trigger: str = "manual", source_ids: list[str] | None = None, deadline_seconds: float | None = None) -> dict:
    """Start `jobhunter hunt run` as its own process (new session, output to a log file) and return at once, so the
    caller can poll `hunt status`, sync the dashboard when the first results are in, and finish later. The child
    process owns the run; this process does not wait for it."""
    import subprocess
    import sys

    from jobhunter.db import connect
    from jobhunter.services import onboarding

    db = connect(ctx.paths.db_path)
    try:
        ok, why = onboarding.live_hunting_allowed(db, ctx.demo)
        if not ok:
            raise HuntNotAllowed(why)
        _reap_stale_runs(db)
        if db.fetchone("SELECT id FROM hunt_runs WHERE status IN ('running','queued')"):
            raise HuntInProgress("A search is already running. Wait for it to finish.")
    finally:
        db.close()
    ctx.paths.logs_dir.mkdir(parents=True, exist_ok=True)
    log = ctx.paths.logs_dir / f"hunt-{now_iso().replace(':', '')}.log"
    cmd = [sys.executable, "-m", "jobhunter", "--json", "hunt", "run", "--trigger", trigger, "--without-dashboard"]
    if deadline_seconds:
        cmd += ["--deadline", str(int(deadline_seconds))]
    for s in source_ids or []:
        cmd += ["--source", s]
    with open(log, "ab") as fh:
        proc = subprocess.Popen(cmd, stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True, cwd=str(ctx.paths.root))
    return {"started": True, "pid": proc.pid, "log": str(log), "note": "The search runs on its own. `jobhunter hunt status --json` follows it; the dashboard can be synced as soon as the first matches are in."}


def start_background(ctx: AppContext, trigger: str = "manual") -> dict:
    """Start a hunt in a background thread and return its run id at once. The page can poll
    `progress(db, run_id)` while jobs appear source by source. Honest early answers: not allowed,
    or already running, are raised here, before any thread starts."""
    import threading

    from jobhunter.db import connect
    from jobhunter.services import onboarding

    db = connect(ctx.paths.db_path)
    try:
        ok, why = onboarding.live_hunting_allowed(db, ctx.demo)
        if not ok:
            raise HuntNotAllowed(why)
        _reap_stale_runs(db)
        if db.fetchone("SELECT id FROM hunt_runs WHERE status IN ('running','queued')"):
            raise HuntInProgress("A search is already running. Wait for it to finish.")
        run_id = db.insert("hunt_runs", {"trigger": trigger, "status": "queued", "started_at": now_iso(), "heartbeat_at": now_iso(), "owner": _owner(), "progress_json": json.dumps({"stage": "starting"})})
    finally:
        db.close()

    def _bg() -> None:
        d = connect(ctx.paths.db_path)
        try:
            # The local dashboard cannot read the artifact page, so a stale dashboard is reported, not blocking.
            run_hunt(ctx, d, trigger=trigger, run_id=run_id, allow_stale_board=True)
        except Exception:
            pass  # run_hunt recorded the failure on the row
        finally:
            d.close()

    threading.Thread(target=_bg, name=f"hunt-{run_id}", daemon=True).start()
    return {"run_id": run_id, "status": "queued"}


def _execute(ctx: AppContext, db: Database, run_id: int, source_ids: list[str] | None, client: SafeClient | None, progress, deadline_seconds: float | None = None) -> dict:
    ensure_sources_initialized(db)
    t_start = time.monotonic()
    deadline_at = (t_start + float(deadline_seconds)) if deadline_seconds else None

    def past_deadline() -> bool:
        return deadline_at is not None and time.monotonic() >= deadline_at

    sources, problem, note = sources_state(db, source_ids)
    if problem:
        _progress_update(db, run_id, {"stage": "ready", "sources_total": 0, "sources_done": 0, "jobs_new": 0, "jobs_relevant_new": 0})
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
    from jobhunter.services.matching import engine

    # Stored jobs first: what is already in the database is compared before any site is asked, so the
    # page has a shortlist within seconds even when the sites are slow.
    prog = {"stage": "comparing stored jobs", "sources_total": len(sources), "sources_done": 0, "jobs_new": 0, "jobs_relevant_new": 0}
    _progress_update(db, run_id, prog)
    try:
        engine.rematch_quietly(ctx, db)
    except Exception:
        pass  # the full pass at the end reports a real failure
    prog["stage"] = "checking sources"
    _progress_update(db, run_id, prog)
    employers_before = db.fetchone("SELECT COUNT(*) AS n FROM employers")["n"]

    timings: dict[str, float] = {}
    t_sources = time.monotonic()
    early_errors: list[str] = []

    def step(source: dict, res: FetchResult) -> None:
        # Progressive persistence: each source's jobs are stored as soon as that source answers,
        # so the dashboard fills up during the search instead of at the end.
        new_ids = _record_source_run(db, run_id, source, res, stats) or []
        if new_ids:
            # Early matches: compare this source's new jobs right away, so the best ones are visible
            # while slower sources are still answering. The full pass at the end catches the rest.
            try:
                engine.run_matching(ctx, db, hunt_run_id=run_id, job_ids=new_ids, use_model=False)
            except Exception as exc:
                early_errors.append(f"{type(exc).__name__}: {exc}")
        prog.update({"sources_done": prog["sources_done"] + 1, "jobs_new": stats["jobs_new"], "jobs_relevant_new": stats["jobs_relevant_new"]})
        _progress_update(db, run_id, prog)
        if progress:
            progress(source["id"], res.status)

    try:
        pool = ThreadPoolExecutor(max_workers=HTTP_MAX_CONCURRENCY)
        futures = {}
        try:
            for s in sources:
                skip = _should_skip(s)
                if skip:
                    step(s, FetchResult(source_id=s["id"], status="skipped", error=skip, complete=False))
                    continue
                futures[pool.submit(_fetch_one, client, s)] = s
            pending = dict(futures)
            budget = SOURCE_STAGE_BUDGET_SECONDS if deadline_at is None else max(0.1, min(SOURCE_STAGE_BUDGET_SECONDS, deadline_at - time.monotonic()))
            try:
                for fut in as_completed(futures, timeout=budget):
                    s = pending.pop(fut)
                    try:
                        res = fut.result()
                    except Exception as exc:  # adapter bug: still isolate the failure
                        res = FetchResult(source_id=s["id"], status="failed", error=f"{type(exc).__name__}: {exc}", complete=False)
                    step(s, res)
            except TimeoutError:
                # The budget is up: what answered is kept, the rest is "couldn't check", never "no jobs".
                minutes = int(SOURCE_STAGE_BUDGET_SECONDS // 60) or 1
                hit = past_deadline()
                stats["deadline_hit"] = stats.get("deadline_hit", False) or hit
                why = f"Did not answer before the deadline ({int(deadline_seconds or 0)} s for the whole search); skipped this time." if hit else f"Did not answer within {minutes} minute(s); skipped this time."
                for fut, s in list(pending.items()):
                    fut.cancel()
                    step(s, FetchResult(source_id=s["id"], status="failed", error=why, complete=False))
        finally:
            # Never wait for a hung download: the threads finish on their own and their results are dropped.
            pool.shutdown(wait=False, cancel_futures=True)
        timings["sources"] = round(time.monotonic() - t_sources, 1)
        prog["stage"] = "discovering employers"
        _progress_update(db, run_id, prog)
        stats["employers_new"] = db.fetchone("SELECT COUNT(*) AS n FROM employers")["n"] - employers_before
        skipped_stages: list[str] = []
        if past_deadline():
            stats["deadline_hit"] = True
            stats["sources_added"] = 0
            skipped_stages.append("employer discovery")
        else:
            try:
                stats["sources_added"] = _promote_discovered_employers(db, client, limit=MAX_NEW_SOURCES_PER_HUNT)
            except Exception as exc:
                stats["sources_added"] = 0
                stats.setdefault("warnings", []).append(f"employer discovery: {type(exc).__name__}: {exc}")
        # Matching runs before the original-link lookups so the best matches get looked up first.
        prog["stage"] = "comparing jobs"
        _progress_update(db, run_id, prog)
        t_match = time.monotonic()
        try:
            stats["matching"] = engine.run_matching(ctx, db, hunt_run_id=run_id)
            if early_errors:
                stats["matching"]["early_errors"] = early_errors[:3]
        except Exception as exc:
            stats["matching"] = {"status": "failed", "error": f"{type(exc).__name__}: {exc}", "trace": traceback.format_exc()[-800:]}
        timings["matching"] = round(time.monotonic() - t_match, 1)
        prog["matching"] = {k: stats["matching"].get(k) for k in ("status", "presented", "error")}
        prog["stage"] = "finding original links"
        _progress_update(db, run_id, prog)
        t_links = time.monotonic()
        if past_deadline():
            stats["deadline_hit"] = True
            skipped_stages.append("original-link lookup")
        else:
            _lookup_originals(db, client, run_id, heartbeat=lambda: _progress_update(db, run_id, prog))
        timings["links"] = round(time.monotonic() - t_links, 1)
        if stats.get("deadline_hit"):
            total = round(time.monotonic() - t_start)
            stats["note"] = ((stats.get("note") or "") + f" Stopped at the deadline after {total} s" + (": " + " and ".join(skipped_stages) + " skipped this time" if skipped_stages else "") + ". What was found is matched; the rest waits for the next search.").strip()
    finally:
        original_lookup.clear_cache()
        if own_client:
            client.close()
    stats["stages"] = timings
    prog["stage"] = "ready"
    prog["stages"] = timings
    _progress_update(db, run_id, prog)
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


def _record_source_run(db: Database, run_id: int, source: dict, res: FetchResult, stats: dict) -> list[int]:
    """Store one source's answer. Returns the ids of the new relevant jobs, for early matching."""
    ts = now_iso()
    employer_hosted = PROVIDERS.get(source["kind"], {}).get("employer_hosted", False)
    fetched = len(res.postings)
    new = relevant_new = updated = 0
    seen_ext: set[str] = set()
    new_relevant_ids: list[int] = []
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
                    new_relevant_ids.append(job_id)
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
    return new_relevant_ids


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


def _lookup_originals(db: Database, client: SafeClient, run_id: int, heartbeat=None) -> None:
    """Resolve the employer's own listing for this run's jobs, best matches first (bounded per run)."""
    rows = db.fetchall(
        "SELECT j.* FROM jobs j LEFT JOIN matches m ON m.job_id = j.id AND m.profile_version_id = (SELECT id FROM profile_versions WHERE status = 'approved' ORDER BY version DESC LIMIT 1) "
        "WHERE j.direct_url_status IN ('none') AND j.relevance IN ('relevant','uncertain') "
        "AND j.id IN (SELECT job_id FROM job_observations WHERE hunt_run_id = ?) "
        "ORDER BY CASE m.match_group WHEN 'good' THEN 0 WHEN 'worth_a_shot' THEN 1 WHEN 'backup' THEN 2 WHEN 'uncertain' THEN 3 ELSE 4 END, m.score DESC, j.id LIMIT 40",
        (run_id,),
    )
    for i, job in enumerate(rows):
        if heartbeat and i % 5 == 0:
            heartbeat()
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
