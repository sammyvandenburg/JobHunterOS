"""Automatic hunting without a terminal.

Two ways, both driven by the same schedule table and the same worker tick:

* inside Hunter: while the dashboard runs, a background thread checks the schedule every minute
  and runs due hunts and digests. Nothing to install; it stops when Hunter is closed.
* when Hunter is closed: an operating-system login task (launchd, systemd user unit, or Windows
  Task Scheduler) that runs ``jobhunter worker run``. Installed only after the person explicitly
  approves it in the dashboard, and removed from the same place.

The worker file lock makes sure only one of them acts at a time. ``status`` tells the truth about
three different things: is the schedule on, is something actually executing it right now (a fresh
heartbeat, not a wish), and when did it last succeed.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import timedelta

from jobhunter.config import AppContext
from jobhunter.db import Database, connect
from jobhunter.services import schedule, worker
from jobhunter.services.locks import FileLock, LockHeld
from jobhunter.util import now, now_iso, parse_iso

IN_APP_TAG = "inside Hunter"  # heartbeat messages written by the in-app scheduler carry this
FRESH_MINUTES = 3  # a heartbeat older than this means nothing is running


class Autorun:
    """In-process scheduler for the dashboard. Safe to start more than once; idle when the schedule is off."""

    def __init__(self, ctx: AppContext, tick_seconds: float | None = None):
        self.ctx = ctx
        self.tick_seconds = tick_seconds or float(os.environ.get("JOBHUNTER_AUTORUN_TICK", "60"))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last: dict = {}

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running or self.ctx.demo:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="hunter-autorun", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        was_running = self.running
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        self._thread = None
        if was_running:
            self._mark_stopped()

    def _mark_stopped(self) -> None:
        """Leave an honest heartbeat behind: overwrite only our own, never a separate worker's."""
        try:
            db = connect(self.ctx.paths.db_path)
            try:
                hb = db.fetchone("SELECT message FROM worker_heartbeat WHERE id = 1")
                if hb and IN_APP_TAG in str(hb.get("message") or ""):
                    worker._beat(db, "stopped", f"automatic hunting {IN_APP_TAG}: Hunter closed")
            finally:
                db.close()
        except Exception:
            pass

    def tick_once(self) -> dict:
        """One scheduler decision, if the schedule is on and no separate worker holds the lock."""
        db = connect(self.ctx.paths.db_path)
        try:
            sched = schedule.get(db)
            if not sched.get("enabled") or sched.get("paused"):
                return {"skipped": "schedule off"}
            lock = FileLock(self.ctx.paths.locks_dir / "worker.lock")
            try:
                lock.acquire()
            except LockHeld:
                return {"skipped": "a separate worker is running"}
            try:
                worker._beat(db, "running", f"automatic hunting {IN_APP_TAG}")
                try:
                    res = worker.tick(self.ctx, db)
                except Exception as exc:
                    worker._beat(db, "idle", f"automatic hunting {IN_APP_TAG}: error: {type(exc).__name__}: {exc}"[:300])
                    raise
                what = "hunt ran" if res.get("ran_hunt") else res["decision"]["reason"]
                if res.get("sent_digest"):
                    what += "; digest sent"
                worker._beat(db, "idle", f"automatic hunting {IN_APP_TAG}: {what}")
                self.last = {"at": now_iso(), **{k: res.get(k) for k in ("ran_hunt", "sent_digest")}}
                return res
            finally:
                lock.release()
        finally:
            db.close()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick_once()
            except Exception as exc:  # keep the thread alive; the next tick tries again
                self.last = {"at": now_iso(), "error": f"{type(exc).__name__}: {exc}"}
            self._stop.wait(self.tick_seconds)


def _ago(delta: timedelta | None) -> str:
    if delta is None:
        return "never"
    s = int(delta.total_seconds())
    if s < 90:
        return "just now"
    if s < 3600:
        return f"{s // 60} min ago"
    if s < 86400:
        return f"{s // 3600} h ago"
    return f"{s // 86400} day(s) ago"


def last_scheduled_hunt(db: Database) -> dict | None:
    row = db.fetchone("SELECT id, status, started_at, finished_at, stats_json, error FROM hunt_runs WHERE trigger = 'scheduled' ORDER BY id DESC LIMIT 1")
    if not row:
        return None
    stats = json.loads(row.get("stats_json") or "{}")
    ok = db.fetchone("SELECT finished_at FROM hunt_runs WHERE trigger = 'scheduled' AND status IN ('completed', 'partial') ORDER BY id DESC LIMIT 1")
    return {"id": row["id"], "at": row.get("finished_at") or row.get("started_at"), "status": row["status"], "jobs_new": int(stats.get("jobs_new") or 0), "error": row.get("error"), "last_ok_at": ok["finished_at"] if ok else None}


def last_email(db: Database) -> dict | None:
    row = db.fetchone("SELECT id, status, created_at, sent_at, error FROM notifications WHERE kind = 'digest' ORDER BY id DESC LIMIT 1")
    if not row:
        return None
    ok = db.fetchone("SELECT sent_at FROM notifications WHERE kind = 'digest' AND status = 'sent' ORDER BY id DESC LIMIT 1")
    return {"id": row["id"], "at": row.get("sent_at") or row.get("created_at"), "status": row["status"], "error": row.get("error"), "last_ok_at": ok["sent_at"] if ok else None}


def status(ctx: AppContext, db: Database, autorun: Autorun | None = None) -> dict:
    """Plain words for the dashboard, from evidence: the schedule row, the heartbeat's age and author,
    the login task's real state, and the last scheduled hunt and email."""
    sched = schedule.describe(db)
    hb = sched.get("worker_heartbeat") or {}
    beat_at = parse_iso(hb.get("last_beat_at"))
    age = (now() - beat_at) if beat_at else None
    fresh = bool(age is not None and age < timedelta(minutes=FRESH_MINUTES) and hb.get("status") != "stopped")
    in_app_beat = IN_APP_TAG in str(hb.get("message") or "")
    svc = worker.service_status(ctx)
    on = bool(sched.get("enabled")) and not sched.get("paused")
    thread_alive = bool(autorun and autorun.running)
    thread_error = (autorun.last or {}).get("error") if autorun else None
    thread_last = parse_iso((autorun.last or {}).get("at")) if autorun else None
    runner = None
    if fresh and not in_app_beat:
        runner = "worker"
    elif thread_alive:
        runner = "hunter"
    if not on:
        label, detail = "off", "Hunter only looks when you press Find jobs now."
    elif runner == "worker":
        label, detail = "running", f"Running in the background (last check {_ago(age)}), also when Hunter is closed."
    elif runner == "hunter" and thread_error:
        label, detail = "running, last check failed", f"Running inside Hunter, but the last check failed: {thread_error}. Hunter tries again every minute."
    elif runner == "hunter":
        checked = f" (last check {_ago(now() - thread_last)})" if thread_last else ""
        label, detail = "running", f"Running inside Hunter{checked}. It stops when you close Hunter."
    elif svc.get("installed") and svc.get("active"):
        label, detail = "installed, but not responding", f"The background task says it is running, but Hunter has not heard from it ({_ago(age)}). Turn automatic hunting off and on again, or look at {ctx.paths.logs_dir}."
    elif svc.get("installed"):
        label, detail = "installed, waiting", "The background task is installed; it starts when you log in to this computer."
    else:
        label, detail = "on, but nothing is running it", "Open Hunter (the schedule runs inside it), or turn on 'Keep hunting when Hunter is closed'."
    return {
        "on": on,
        "label": label,
        "detail": detail,
        "runner": runner,
        "service": svc,
        "heartbeat_fresh": fresh,
        "heartbeat_age": _ago(age),
        "next_run_at": sched.get("next_run_at"),
        "frequency_label": sched.get("frequency_label"),
        "notify_label": sched.get("notify_label"),
        "digest_time": sched.get("digest_time"),
        "worker": sched.get("worker"),
        "last_hunt": last_scheduled_hunt(db),
        "last_email": last_email(db),
    }
