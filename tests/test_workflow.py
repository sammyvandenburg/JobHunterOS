"""One run contract for the session, the worker and the nightly routine: the same order, judged from evidence."""

from __future__ import annotations

from jobhunter.config import AppContext
from jobhunter.db import connect
from jobhunter.services import board, cloudstate, hunt, notifications, workflow
from tests.conftest import approve_profile
from tests.test_board import _seed_jobs

URL = "https://claude.ai/artifact/5wUUffKaZnmpqGgPnRzPVE"


def _status(plan: dict) -> dict:
    return {s["step"]: s["status"] for s in plan["steps"]}


def test_the_order_is_read_hunt_digest_save_publish_and_judged_from_evidence(ctx, db, tmp_path, fake_sources):
    approve_profile(db, ctx, countries="Netherlands")
    notifications.set_recipient(db, "student@example.com")
    db.set_setting("digest.transport", "claude")
    board.link(db, URL)
    p = workflow.plan(ctx, db)
    assert p["order"] == list(workflow.ORDER)
    assert _status(p) == {"restore": "skipped", "read_dashboard": "next", "hunt": "pending", "digest": "pending", "save": "skipped", "publish_dashboard": "pending"}
    assert p["next"] == "read_dashboard" and "board import" in p["steps"][1]["command"]
    board.import_actions(ctx, db, tmp_path / "none", listed=0)
    assert workflow.plan(ctx, db)["next"] == "hunt"
    hunt.run_hunt(ctx, db)
    p = workflow.plan(ctx, db)
    assert _status(p)["hunt"] == "done" and p["next"] == "digest"
    # Digest: prepared and reserved, then recorded. Until it is recorded the contract is blocked, never "send again".
    h = notifications.prepare_handoff(ctx, db)
    assert h["status"] == "ready"
    p = workflow.plan(ctx, db)
    assert _status(p)["digest"] == "blocked" and "record" in p["steps"][3]["command"]
    notifications.record_handoff(db, h["notification_id"], "sent", message_id="gmail-1")
    p = workflow.plan(ctx, db)
    assert _status(p)["digest"] == "done" and p["next"] == "publish_dashboard"
    board.export(ctx, db, tmp_path / "out")
    p = workflow.plan(ctx, db)
    assert p["complete"] and p["next"] is None
    assert p["next_run_starts_with"] == "read_dashboard"
    # The local worker hunts without reading the page (it cannot): that run is not complete until the page is read.
    hunt.run_hunt(ctx, db, trigger="scheduled", allow_stale_board=True)
    p = workflow.plan(ctx, db)
    assert p["next"] == "read_dashboard" and _status(p)["hunt"] == "done" and _status(p)["publish_dashboard"] == "pending"


def test_a_reserved_digest_survives_a_dead_run_and_is_never_resent_blindly(ctx, db, tmp_path, fake_sources):
    approve_profile(db, ctx, countries="Netherlands")
    notifications.set_recipient(db, "student@example.com")
    db.set_setting("digest.transport", "claude")
    _seed_jobs(ctx, db)
    # Run one prepares the digest and saves the state (the durable reservation), then dies before sending or recording.
    h = notifications.prepare_handoff(ctx, db)
    assert h["status"] == "ready"
    bundle = cloudstate.export_bundle(ctx, db)
    assert any(n["status"] == "sending" and n["delivery_key"] for n in bundle["results"]["notifications"])
    # Run two, next day, fresh machine: restores and must not send the same digest again on its own.
    home = tmp_path / "cloud2"
    c2 = AppContext.from_env(demo=False, home=home)
    d2 = connect(c2.paths.db_path)
    cloudstate.import_bundle(c2, d2, bundle, inputs=True, results=True)
    d2.execute("UPDATE notifications SET claimed_at = '2026-10-19T05:00:00+00:00', created_at = '2026-10-19T05:00:00+00:00'")
    _seed_jobs(c2, d2)  # the night's hunt finds the same vacancies again
    p = workflow.plan(c2, d2, nightly=True)
    assert _status(p)["digest"] == "blocked" and "never send it again blindly" in p["steps"][3]["why"]
    again = notifications.prepare_handoff(c2, d2)
    assert again["status"] == "skipped" and "Nothing new" in again["note"]  # the reserved jobs count as emailed, so no second email
    assert notifications.unresolved(d2)  # and the unknown delivery is visible under Needs attention
    # The person (or Claude, after checking the inbox) records what happened; only then can that digest go out again.
    nid = p["evidence"]["last_digest"]["id"]
    notifications.record_handoff(d2, nid, "failed", error="session ended before sending")
    assert notifications.prepare_handoff(c2, d2)["status"] == "ready"
    d2.close()


def test_a_restored_database_reads_the_page_before_it_hunts(ctx, db, tmp_path, fake_sources):
    approve_profile(db, ctx, countries="Netherlands")
    board.link(db, URL)
    bundle = cloudstate.export_bundle(ctx, db)
    home = tmp_path / "cloud"
    c2 = AppContext.from_env(demo=False, home=home)
    d2 = connect(c2.paths.db_path)
    cloudstate.import_bundle(c2, d2, bundle, inputs=True, results=True)
    p = workflow.plan(c2, d2, nightly=True)
    assert _status(p)["restore"] == "done" and p["next"] == "read_dashboard"
    import pytest

    with pytest.raises(hunt.HuntNotAllowed):
        hunt.run_hunt(c2, d2, trigger="cloud")
    board.import_actions(c2, d2, tmp_path / "none", listed=0)
    assert workflow.plan(c2, d2, nightly=True)["next"] == "hunt"
    hunt.run_hunt(c2, d2, trigger="cloud")
    st = workflow.routine_status(d2)
    assert st["last_nightly_run"]["status"] in {"completed", "partial"} and st["next_nightly_run"] is None and not st["email_configured"]
    d2.close()
