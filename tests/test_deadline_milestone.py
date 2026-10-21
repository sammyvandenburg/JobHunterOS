"""A hunt ends when it is told to: a whole-command deadline stops the search at the next safe point with an honest
status, and a fetch that never answers cannot keep the process alive. While a hunt runs, the dashboard document
carries its progress, so a page written mid-hunt shows the first results and says the search is still on."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from jobhunter.services import board, hunt
from tests.conftest import approve_profile


def test_a_slow_source_cannot_hold_the_hunt_past_its_deadline(ctx, db, client, seeded_sources, monkeypatch):
    approve_profile(db, ctx, countries="Netherlands")
    import httpx

    original = seeded_sources.handle_request

    def slow(request):
        if request.url.host == "api.lever.co":
            time.sleep(6)
        return original(request)

    monkeypatch.setattr(seeded_sources, "handle_request", slow)
    monkeypatch.setattr(hunt, "SOURCE_STAGE_BUDGET_SECONDS", 60.0)
    t0 = time.monotonic()
    stats = hunt.run_hunt(ctx, db, client=client, deadline_seconds=2.0)
    took = time.monotonic() - t0
    assert took < 5.0, took
    assert stats["status"] in {"partial", "completed"} and stats["deadline_hit"] is True
    lever = next(f for f in stats["failures"] if f["source"] == "lever:fabrikam")
    assert "deadline" in lever["error"].lower() or "did not answer" in lever["error"].lower()
    run = hunt.last_run(db)
    assert run["status"] in {"partial", "completed"} and "deadline" in (run.get("error") or run["stats"].get("note") or "").lower()
    assert isinstance(httpx.ConnectError, type)


def test_the_dashboard_document_carries_a_running_hunt(ctx, db, tmp_path):
    from tests.test_board import _seed_jobs

    approve_profile(db, ctx, countries="Netherlands")
    _seed_jobs(ctx, db)
    board.link(db, "https://claude.ai/artifact/5wUUffKaZnmpqGgPnRzPVE")
    board.import_actions(ctx, db, tmp_path / "none", listed=0)
    manifest = board.export(ctx, db, tmp_path / "out")
    run_doc = json.loads(Path([w for w in manifest["writes"] if w["doc_id"] == "run"][0]["file"]).read_text())
    assert run_doc["hunt_in_progress"] is None
    rid = db.insert("hunt_runs", {"trigger": "manual", "status": "running", "started_at": "2026-10-21T08:00:00+00:00", "heartbeat_at": "2026-10-21T08:00:30+00:00", "owner": "x", "progress_json": json.dumps({"stage": "checking sources", "sources_done": 12, "sources_total": 72, "jobs_new": 9, "jobs_relevant_new": 3})})
    board.import_actions(ctx, db, tmp_path / "none", listed=0)  # the page is read before every write, mid-hunt too
    manifest = board.export(ctx, db, tmp_path / "out2")
    run_doc = json.loads(Path([w for w in manifest["writes"] if w["doc_id"] == "run"][0]["file"]).read_text())
    prog = run_doc["hunt_in_progress"]
    assert prog["run_id"] == rid and prog["sources_done"] == 12 and prog["sources_total"] == 72 and prog["jobs_relevant_new"] == 3 and "12 of 72" in prog["message"]
    assert run_doc["hunter"]["last_run"]["status"] == "running"
    from jobhunter.services import workflow

    board.import_actions(ctx, db, tmp_path / "none", listed=0)
    plan = workflow.plan(ctx, db)
    assert plan["next"] == "hunt" and not plan["complete"]  # a running hunt is never "all done"


def test_hunt_start_returns_at_once_and_status_follows_the_run(ctx, db, home, monkeypatch):
    approve_profile(db, ctx, countries="Netherlands")
    db.close()
    env = {**os.environ, "JOBHUNTER_HOME": str(home), "JOBHUNTER_SECRETS_BACKEND": "file"}
    t0 = time.monotonic()
    out = subprocess.run([sys.executable, "-m", "jobhunter", "--json", "hunt", "start", "--source", "nothing:here"], capture_output=True, text=True, env=env, timeout=60)
    assert out.returncode == 0, out.stderr
    started = json.loads(out.stdout)
    assert started["started"] and started["pid"] and started["log"] and time.monotonic() - t0 < 20
    for _ in range(100):
        st = json.loads(subprocess.run([sys.executable, "-m", "jobhunter", "--json", "hunt", "status"], capture_output=True, text=True, env=env, timeout=60).stdout)
        if st and not st.get("running"):
            break
        time.sleep(0.2)
    assert st and st["status"] in {"no_sources", "failed", "completed", "partial"} and not st["running"]
