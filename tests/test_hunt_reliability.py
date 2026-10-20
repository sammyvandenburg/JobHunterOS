"""Hunt reliability: a failed matching stage is a failed hunt, a dead run never blocks the next one, an
overlapping hunt is refused, a slow source cannot hold the whole search, and the first matches are
visible before the last source has answered. Synthetic sources only."""

from __future__ import annotations

import threading
import time

import pytest

from jobhunter.models import FetchResult, RawPosting
from jobhunter.services import hunt, views
from jobhunter.services.matching import engine
from jobhunter.util import hostname, now_iso
from tests.conftest import (
    JOURNEY_DESC,
    JOURNEY_POSTINGS,
    _record_without_watch_source_update,
    approve_profile,
)


def _sources(monkeypatch, n: int, delay: float = 0.0, slow: dict | None = None):
    """n synthetic sources; each returns the journey postings with its own ids. `slow` maps a source id to seconds."""
    slow = slow or {}

    def fake_fetch(client, s):
        time.sleep(slow.get(s["id"], delay))
        posts = []
        for p in JOURNEY_POSTINGS:
            base = dict(source_id=s["id"], remote_type="onsite", description_text=JOURNEY_DESC, posted_at="2026-10-17T09:00:00+00:00", posted_at_provenance="t", is_employer_hosted=True)
            base.update(p)
            base["external_id"] = f"{s['id']}-{p['external_id']}"
            base["url"] = p["url"] + "?s=" + s["id"]
            base["employer_name"] = f"{p['employer_name']} {s['id'][-1]}"  # distinct employers per source: distinct jobs
            posts.append(RawPosting(**base))
        return FetchResult(source_id=s["id"], status="ok", postings=posts, complete=True)

    rows = [{"id": f"greenhouse:fake{i}", "kind": "greenhouse", "config_json": "{}", "enabled": 1} for i in range(n)]
    monkeypatch.setattr(hunt, "_fetch_one", fake_fetch)
    monkeypatch.setattr(hunt, "_promote_discovered_employers", lambda *a, **k: 0)
    monkeypatch.setattr(hunt, "_lookup_originals", lambda *a, **k: None)
    monkeypatch.setattr(hunt, "sources_state", lambda db, ids=None: (rows, None, None))
    monkeypatch.setattr(hunt, "ensure_sources_initialized", lambda db: {"initialized": False})
    monkeypatch.setattr(hunt, "_record_source_run", _record_without_watch_source_update)
    return rows


def test_a_failed_matching_stage_is_a_failed_hunt_with_its_jobs_kept(ctx, db, monkeypatch):
    approve_profile(db, ctx, countries="Netherlands")
    _sources(monkeypatch, 2)

    def boom(*a, **k):
        raise RuntimeError("scoring blew up")

    monkeypatch.setattr(engine, "run_matching", boom)
    stats = hunt.run_hunt(ctx, db, trigger="manual")
    assert stats["status"] == "failed", stats
    assert stats["jobs_new"] >= 4 and stats["matching"]["status"] == "failed"
    row = hunt.last_run(db)
    assert row["status"] == "failed" and "compar" in (row["error"] or "").lower()
    assert hunt.last_successful_run(db) is None
    # The jobs are kept, and the dashboard says why there are no recommendations.
    assert db.fetchone("SELECT COUNT(*) AS n FROM jobs")["n"] >= 4
    assert "could not be compared" in hunt.progress(db, stats["run_id"])["message"].lower() or "stopped" in hunt.progress(db, stats["run_id"])["message"].lower()


def test_a_dead_run_never_blocks_the_next_hunt(ctx, db, monkeypatch):
    approve_profile(db, ctx, countries="Netherlands")
    _sources(monkeypatch, 1)
    # A run left behind by a process that no longer exists, with a heartbeat from a moment ago.
    dead = db.insert("hunt_runs", {"trigger": "manual", "status": "running", "started_at": now_iso(), "heartbeat_at": now_iso(), "owner": f"{hostname()}:999999"})
    stats = hunt.run_hunt(ctx, db, trigger="manual")
    assert stats["status"] in {"completed", "partial"}
    old = db.fetchone("SELECT status, error FROM hunt_runs WHERE id = ?", (dead,))
    assert old["status"] == "aborted" and old["error"]
    # The same from the dashboard path, which starts hunts in a thread.
    dead2 = db.insert("hunt_runs", {"trigger": "manual", "status": "running", "started_at": now_iso(), "heartbeat_at": now_iso(), "owner": f"{hostname()}:999998"})
    res = hunt.start_background(ctx, trigger="manual")
    assert res["status"] == "queued"
    for _ in range(100):
        p = hunt.progress(db, res["run_id"])
        if not p["running"]:
            break
        time.sleep(0.1)
    assert db.fetchone("SELECT status FROM hunt_runs WHERE id = ?", (dead2,))["status"] == "aborted"
    assert hunt.progress(db, res["run_id"])["status"] in {"completed", "partial"}


def test_an_overlapping_hunt_is_refused_while_the_first_one_is_alive(ctx, db, monkeypatch):
    approve_profile(db, ctx, countries="Netherlands")
    _sources(monkeypatch, 2, delay=0.8)
    from jobhunter.db import connect

    out = {}

    def first():
        d = connect(ctx.paths.db_path)
        try:
            out["first"] = hunt.run_hunt(ctx, d, trigger="manual")
        finally:
            d.close()

    t = threading.Thread(target=first)
    t.start()
    time.sleep(0.3)
    with pytest.raises(hunt.HuntInProgress):
        hunt.run_hunt(ctx, db, trigger="manual")
    with pytest.raises(hunt.HuntInProgress):
        hunt.start_background(ctx, trigger="manual")
    t.join(timeout=30)
    assert out["first"]["status"] in {"completed", "partial"}
    assert db.fetchone("SELECT COUNT(*) AS n FROM hunt_runs WHERE status IN ('running','queued')")["n"] == 0


def test_a_slow_source_does_not_hold_the_whole_search(ctx, db, monkeypatch):
    approve_profile(db, ctx, countries="Netherlands")
    _sources(monkeypatch, 3, slow={"greenhouse:fake1": 5.0})
    monkeypatch.setattr(hunt, "SOURCE_STAGE_BUDGET_SECONDS", 1.5)
    t0 = time.time()
    stats = hunt.run_hunt(ctx, db, trigger="manual")
    assert time.time() - t0 < 4.5
    assert stats["status"] == "partial" and stats["sources_ok"] == 2 and stats["sources_failed"] == 1
    assert any("time" in f["error"].lower() for f in stats["failures"])
    assert stats["jobs_new"] >= 4  # the fast sources' jobs are kept


def test_first_matches_are_visible_before_the_last_source_answers(ctx, db, monkeypatch):
    approve_profile(db, ctx, countries="Netherlands")
    _sources(monkeypatch, 3, delay=0.4)
    seen: list[int] = []

    def on_progress(sid, status):
        seen.append(len(views.current_recommended(db)))

    stats = hunt.run_hunt(ctx, db, trigger="manual", progress=on_progress)
    assert stats["status"] in {"completed", "partial"}
    # After the first source answered there were already recommendations, before the others finished.
    assert seen and seen[0] > 0 and seen[0] < seen[-1]
    assert stats["stages"]["sources"] >= 0 and "matching" in stats["stages"]
    p = hunt.progress(db, stats["run_id"])
    assert p["stage"] == "ready" and p["jobs_relevant_new"] > 0
