"""Only one thing runs a scheduled hunt. The in-app scheduler and a real worker process share one file lock;
shutdown leaves an honest heartbeat; a restart catches up one missed run and skips old ones; a hunt that died
mid-way is reaped, not waited for."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from datetime import timedelta
from pathlib import Path

from jobhunter.db import connect
from jobhunter.services import autorun, hunt, schedule, worker
from jobhunter.services.locks import FileLock
from jobhunter.util import now
from tests.conftest import approve_profile

REPO = Path(__file__).resolve().parents[1]


def _enable(db, frequency="every_2h"):
    schedule.set_frequency(db, frequency)
    schedule.enable(db)


def _wait(pred, timeout=15.0, what="condition"):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for {what}")


def test_in_app_scheduler_steps_aside_for_a_real_worker_process(ctx, db):
    _enable(db)
    env = {**os.environ, "JOBHUNTER_HOME": str(ctx.paths.home), "JOBHUNTER_SECRETS_BACKEND": "file"}
    proc = subprocess.Popen([sys.executable, "-m", "jobhunter", "worker", "run"], cwd=str(REPO), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        _wait(lambda: (db.fetchone("SELECT status FROM worker_heartbeat WHERE id = 1") or {}).get("status") in {"idle", "running"}, what="worker heartbeat")
        a = autorun.Autorun(ctx, tick_seconds=3600)
        assert a.tick_once() == {"skipped": "a separate worker is running"}
        st = autorun.status(ctx, db, a)
        assert st["runner"] == "worker" and st["label"] == "running" and "background" in st["detail"]
        # A second worker process refuses to start while the first holds the lock.
        second = subprocess.run([sys.executable, "-m", "jobhunter", "worker", "run"], cwd=str(REPO), env=env, capture_output=True, text=True, timeout=60)
        assert second.returncode == 2 and "already running" in second.stdout
    finally:
        proc.terminate()
        out, _ = proc.communicate(timeout=20)
    assert proc.returncode == 0, out
    hb = db.fetchone("SELECT status, message FROM worker_heartbeat WHERE id = 1")
    assert hb["status"] == "stopped" and hb["message"] == "worker stopped"
    # The worker is gone: the in-app scheduler takes over on its next tick.
    res = autorun.Autorun(ctx, tick_seconds=3600).tick_once()
    assert "skipped" not in res and res["decision"]["reason"] in {"next run planned", "not due yet"}
    assert autorun.status(ctx, db)["label"] == "on, but nothing is running it"


def test_only_one_hunt_when_both_are_due_at_the_same_time(ctx, db, fake_sources):
    approve_profile(db, ctx)
    _enable(db)
    db.execute("UPDATE schedule SET next_run_at = ? WHERE id = 1", ((now() - timedelta(minutes=1)).isoformat(),))
    holding = threading.Event()
    results: dict = {}

    def worker_like():
        d = connect(ctx.paths.db_path)
        lock = FileLock(ctx.paths.locks_dir / "worker.lock")
        lock.acquire()
        holding.set()
        try:
            results["worker"] = worker.tick(ctx, d)
        finally:
            lock.release()
            d.close()

    t = threading.Thread(target=worker_like)
    t.start()
    holding.wait(5)
    a = autorun.Autorun(ctx, tick_seconds=3600)
    results["autorun"] = a.tick_once()
    t.join(30)
    assert results["autorun"] == {"skipped": "a separate worker is running"}
    assert results["worker"]["ran_hunt"] is True
    assert db.fetchone("SELECT COUNT(*) AS n FROM hunt_runs WHERE trigger = 'scheduled'")["n"] == 1
    # The slot is used up; the in-app scheduler does not run it again.
    after = a.tick_once()
    assert after["ran_hunt"] is False and after["decision"]["reason"] == "not due yet"
    assert db.fetchone("SELECT COUNT(*) AS n FROM hunt_runs")["n"] == 1


def test_restart_catches_up_one_missed_run_and_skips_old_ones(ctx, db, fake_sources):
    approve_profile(db, ctx)
    _enable(db)
    a = autorun.Autorun(ctx, tick_seconds=3600)
    db.execute("UPDATE schedule SET next_run_at = ? WHERE id = 1", ((now() - timedelta(hours=2)).isoformat(),))
    res = a.tick_once()
    assert res["ran_hunt"] is True and res["decision"]["reason"].startswith("catching up one missed run")
    assert a.tick_once()["ran_hunt"] is False
    assert db.fetchone("SELECT COUNT(*) AS n FROM hunt_runs")["n"] == 1
    db.execute("UPDATE schedule SET next_run_at = ? WHERE id = 1", ((now() - timedelta(hours=48)).isoformat(),))
    res = a.tick_once()
    assert res["ran_hunt"] is False and "too old; skipped" in res["decision"]["reason"]
    assert schedule.get(db)["next_run_at"] > now().isoformat()
    assert db.fetchone("SELECT COUNT(*) AS n FROM hunt_runs")["n"] == 1


def test_shutdown_and_restart_of_the_in_app_scheduler(ctx, db):
    _enable(db)
    a = autorun.Autorun(ctx, tick_seconds=3600)
    a.start()
    _wait(lambda: autorun.IN_APP_TAG in str((db.fetchone("SELECT message FROM worker_heartbeat WHERE id = 1") or {}).get("message") or ""), what="in-app heartbeat")
    assert autorun.status(ctx, db, a)["label"] == "running"
    t0 = time.time()
    a.stop()
    assert time.time() - t0 < 5 and not a.running
    hb = db.fetchone("SELECT status, message FROM worker_heartbeat WHERE id = 1")
    assert hb["status"] == "stopped" and "Hunter closed" in hb["message"]
    assert autorun.status(ctx, db, a)["label"] == "on, but nothing is running it"
    b = autorun.Autorun(ctx, tick_seconds=3600)
    b.start()
    _wait(lambda: (db.fetchone("SELECT status FROM worker_heartbeat WHERE id = 1") or {}).get("status") == "idle", what="restart heartbeat")
    assert autorun.status(ctx, db, b)["label"] == "running"
    b.stop()


def test_a_hunt_that_died_mid_way_is_reaped_not_waited_for(ctx, db, fake_sources):
    approve_profile(db, ctx)
    _enable(db)
    a = autorun.Autorun(ctx, tick_seconds=3600)
    stale = (now() - timedelta(minutes=hunt.STALE_RUN_MINUTES + 5)).isoformat()
    with db.tx():
        dead = db.insert("hunt_runs", {"trigger": "scheduled", "status": "running", "started_at": stale, "heartbeat_at": stale, "owner": "old-host:1"})
    db.execute("UPDATE schedule SET next_run_at = ? WHERE id = 1", ((now() - timedelta(minutes=1)).isoformat(),))
    res = a.tick_once()
    assert res["ran_hunt"] is True
    assert db.fetchone("SELECT status FROM hunt_runs WHERE id = ?", (dead,))["status"] == "aborted"
    assert db.fetchone("SELECT COUNT(*) AS n FROM hunt_runs WHERE status = 'completed'")["n"] == 1
    # A hunt that is really running right now (fresh heartbeat) is respected: the slot stays due for the next tick.
    with db.tx():
        live = db.insert("hunt_runs", {"trigger": "manual", "status": "running", "started_at": now().isoformat(), "heartbeat_at": now().isoformat(), "owner": "this-host:2"})
    before = schedule.get(db)["next_run_at"]
    db.execute("UPDATE schedule SET next_run_at = ? WHERE id = 1", ((now() - timedelta(minutes=1)).isoformat(),))
    res = a.tick_once()
    assert res["ran_hunt"] is False and "skipped" in res.get("hunt", {})
    assert schedule.get(db)["next_run_at"] != before and db.fetchone("SELECT status FROM hunt_runs WHERE id = ?", (live,))["status"] == "running"
