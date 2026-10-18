"""Automatic hunting without a terminal.

Two ways, both driven by the same schedule table and the same worker tick:

* inside Hunter: while the dashboard runs, a background thread checks the schedule every minute
  and runs due hunts and digests. Nothing to install; it stops when Hunter is closed.
* when Hunter is closed: an operating-system login task (launchd, systemd user unit, or Windows
  Task Scheduler) that runs ``jobhunter worker run``. Installed only after the person explicitly
  approves it in the dashboard, and removed from the same place.

The worker file lock makes sure only one of them acts at a time.
"""

from __future__ import annotations

import os
import threading

from jobhunter.config import AppContext
from jobhunter.db import connect
from jobhunter.services import schedule, worker
from jobhunter.services.locks import FileLock, LockHeld
from jobhunter.util import now_iso


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
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        self._thread = None

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
                worker._beat(db, "running", "automatic hunting inside Hunter")
                res = worker.tick(self.ctx, db)
                worker._beat(db, "idle", "automatic hunting inside Hunter: " + ("hunt ran" if res.get("ran_hunt") else res["decision"]["reason"]))
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


def status(ctx: AppContext, db, autorun: Autorun | None = None) -> dict:
    """Plain words for the dashboard: is automatic hunting on, and what is doing it?"""
    sched = schedule.describe(db)
    svc = worker.service_status(ctx)
    worker_alive = str(sched.get("worker", "")).startswith("running")
    on = bool(sched.get("enabled")) and not sched.get("paused")
    if not on:
        label, detail = "off", "Hunter only looks when you press Find jobs now."
    elif worker_alive and "inside Hunter" in str((sched.get("worker_heartbeat") or {}).get("message") or ""):
        label, detail = "running", "Running inside Hunter. It stops when you close Hunter."
    elif worker_alive:
        label, detail = "running", "Running in the background, also when Hunter is closed."
    elif svc.get("installed"):
        label, detail = "installed, waiting", "The background task is installed; it starts when you log in."
    elif autorun and autorun.running:
        label, detail = "running", "Running inside Hunter. It stops when you close Hunter."
    else:
        label, detail = "on, but nothing is running it", "Open Hunter (or install the background task) so the schedule can run."
    return {"on": on, "label": label, "detail": detail, "service": svc, "next_run_at": sched.get("next_run_at"), "frequency_label": sched.get("frequency_label"), "notify_label": sched.get("notify_label"), "worker": sched.get("worker")}
