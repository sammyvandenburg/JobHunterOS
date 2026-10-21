"""Regression cases from the 2026-10-21 audit: personal work (favorites, saves, notes, applied, not interested)
must survive a cloud restore into an empty database, a restart, and a replay of the page's actions. The state
file carries the flags and the job identities, never a note's text; each database acknowledges a page
revision only after it applied the values itself; a listing that did not complete is never an empty read."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jobhunter.config import AppContext
from jobhunter.db import connect
from jobhunter.services import actions, board, cloudstate, hunt, notes, views
from tests.conftest import approve_profile
from tests.test_board import _action, _key_for, _seed_jobs

URL = "https://claude.ai/artifact/5wUUffKaZnmpqGgPnRzPVE"


def _fresh(tmp_path, name):
    home = tmp_path / name
    ctx = AppContext.from_env(demo=False, home=home)
    return ctx, connect(ctx.paths.db_path)


def _id(db, employer: str) -> int:
    return db.fetchone("SELECT id FROM jobs WHERE employer_name = ?", (employer,))["id"]


def _shard(manifest: dict) -> dict:
    out = {}
    for w in manifest["writes"]:
        if w["collection"] == "board":
            out.update(json.loads(Path(w["file"]).read_text())["jobs"])
    return out


def test_favorite_save_and_note_survive_a_restore_into_an_empty_database(ctx, db, tmp_path):
    # The audit case: favorite + save + note at revision 1 -> cloud export -> empty database -> action import -> export.
    approve_profile(db, ctx, countries="Netherlands")
    _seed_jobs(ctx, db)
    board.link(db, URL)
    nw, contoso = _key_for(db, "Northwind Cloud"), _key_for(db, "Contoso Payments")
    inbox = tmp_path / "in"
    _action(inbox, nw, 1, favorite=True, saved=True, note={"text": "Ask about on-call", "rev": 1})
    res = board.import_actions(ctx, db, inbox, listed=1)
    assert {a["job_key"]: a["changes"] for a in res["applied"]}[nw] == ["favorite", "save", "note"]
    # Work done on this computer without the page: applied and not interested.
    actions.mark_applied(db, _id(db, "Contoso Payments"), applied_at="2026-10-19T10:00:00+00:00")
    third = db.fetchone("SELECT id, employer_name FROM jobs WHERE employer_name NOT IN ('Northwind Cloud', 'Contoso Payments') ORDER BY id LIMIT 1")
    actions.dismiss_job(db, third["id"])

    bundle = cloudstate.export_bundle(ctx, db)
    text = json.dumps(bundle)
    assert "Ask about on-call" not in text and not cloudstate.check_bundle_privacy(bundle)
    keys = {j["canonical_key"] for j in bundle["results"]["jobs"]}
    assert len(keys) >= 3  # the acted-on jobs travel even though no email ever mentioned them
    assert "synced" not in bundle["inputs"]["board"]  # revisions are per database, never carried

    cctx, cdb = _fresh(tmp_path, "cloud-home")
    cloudstate.import_bundle(cctx, cdb, bundle, inputs=True, results=True)
    assert board.status(cdb)["url"] == URL
    assert [j["employer"] for j in views.list_jobs(cdb, view="favorites")["items"]] == ["Northwind Cloud"]
    assert [j["employer"] for j in views.list_jobs(cdb, view="saved")["items"]] == ["Northwind Cloud"]
    assert [j["employer"] for j in views.list_jobs(cdb, view="applied")["items"]] == ["Contoso Payments"]
    assert [j["employer"] for j in views.list_jobs(cdb, view="dismissed")["items"]] == [third["employer_name"]]
    assert views.list_jobs(cdb, view="applied")["items"][0]["applied_at"].startswith("2026-10-19T10:00:00")
    # Nothing is acknowledged that this database did not apply itself: the note still has to come from the page.
    assert cdb.fetchone("SELECT COUNT(*) AS n FROM board_sync")["n"] == 0
    assert board.stale_reason(cdb)  # a restored database reads the page before it hunts or writes
    res = board.import_actions(cctx, cdb, inbox, listed=1)
    assert {a["job_key"]: a["changes"] for a in res["applied"]} == {nw: ["note"]}
    nw_id = _id(cdb, "Northwind Cloud")
    assert notes.count_for_job(cdb, nw_id) == 1 and notes.latest_for_job(cdb, nw_id)["body"] == "Ask about on-call"
    # Replay is safe, and the page is told the truth.
    assert board.import_actions(cctx, cdb, inbox, listed=1)["applied"] == []
    shard = _shard(board.export(cctx, cdb, tmp_path / "out"))
    assert shard[nw]["state"]["favorited"] and shard[nw]["state"]["saved"] and shard[nw]["state"]["note"] == "Ask about on-call"
    assert shard[nw]["state"]["synced_rev"] == 1
    assert shard[contoso]["state"]["stage"] == "applied" and shard[contoso]["state"]["applied_at"].startswith("2026-10-19")
    cdb.close()


def test_undo_on_the_page_survives_a_restart_and_a_restore(ctx, db, tmp_path, home):
    approve_profile(db, ctx, countries="Netherlands")
    _seed_jobs(ctx, db)
    board.link(db, URL)
    nw = _key_for(db, "Northwind Cloud")
    inbox = tmp_path / "in"
    _action(inbox, nw, 1, favorite=True, saved=True, dismissed=True, note={"text": "Maybe", "rev": 1})
    board.import_actions(ctx, db, inbox, listed=1)
    _action(inbox, nw, 2, favorite=False, saved=False, dismissed=False, note={"text": "", "rev": 2, "deleted": True})
    res = board.import_actions(ctx, db, inbox, listed=1)
    assert {a["job_key"]: a["changes"] for a in res["applied"]}[nw] == ["unfavorite", "unsave", "undismiss", "note_deleted"]
    db.close()
    # Restart: a new process on the same folder.
    db2 = connect(AppContext.from_env(home=home).paths.db_path)
    nw_id = _id(db2, "Northwind Cloud")
    assert views.list_jobs(db2, view="favorites")["total"] == 0 and views.list_jobs(db2, view="saved")["total"] == 0
    assert views.list_jobs(db2, view="dismissed")["total"] == 0 and notes.count_for_job(db2, nw_id) == 0
    bundle = cloudstate.export_bundle(ctx, db2)
    db2.close()
    assert "Maybe" not in json.dumps(bundle)
    # Restore into an empty database: still undone, and the page's revision 2 replays as a no-op.
    cctx, cdb = _fresh(tmp_path, "cloud-home")
    cloudstate.import_bundle(cctx, cdb, bundle, inputs=True, results=True)
    assert views.list_jobs(cdb, view="favorites")["total"] == 0 and views.list_jobs(cdb, view="dismissed")["total"] == 0
    res = board.import_actions(cctx, cdb, inbox, listed=1)
    assert {a["job_key"]: a["changes"] for a in res["applied"]} == {nw: []}
    shard = _shard(board.export(cctx, cdb, tmp_path / "out"))
    assert shard[nw]["state"]["synced_rev"] == 2 and not shard[nw]["state"]["favorited"] and shard[nw]["state"]["note"] is None
    cdb.close()


def test_the_laptop_learns_cloud_side_work_without_losing_its_own(ctx, db, tmp_path):
    approve_profile(db, ctx, countries="Netherlands")
    _seed_jobs(ctx, db)
    board.link(db, URL)
    nw_id, contoso = _id(db, "Northwind Cloud"), _key_for(db, "Contoso Payments")
    actions.favorite_job(db, nw_id, occurred_at="2026-10-18T09:00:00+00:00")
    # A cloud run restores the laptop's state, then sees a new page action: Contoso saved with a note.
    cctx, cdb = _fresh(tmp_path, "cloud-home")
    cloudstate.import_bundle(cctx, cdb, cloudstate.export_bundle(ctx, db), inputs=True, results=True)
    assert [j["employer"] for j in views.list_jobs(cdb, view="favorites")["items"]] == ["Northwind Cloud"]
    _seed_jobs(cctx, cdb)  # the cloud's own hunt finds the same vacancies
    inbox = tmp_path / "in"
    _action(inbox, contoso, 2, saved=True, note={"text": "Call Sam first", "rev": 1})
    board.import_actions(cctx, cdb, inbox, listed=1)
    back = cloudstate.export_bundle(cctx, cdb)
    cdb.close()
    assert "Call Sam first" not in json.dumps(back)
    # Meanwhile the laptop unfavorited Northwind (newer than anything the cloud knows).
    actions.favorite_job(db, nw_id, on=False)
    cloudstate.import_bundle(ctx, db, back, inputs=False, results=True)
    assert views.list_jobs(db, view="favorites")["total"] == 0  # the laptop's newer decision stands
    assert [j["employer"] for j in views.list_jobs(db, view="saved")["items"]] == ["Contoso Payments"]
    # The note text never travelled through the state file: the laptop reads the page itself, once.
    assert db.fetchone("SELECT COUNT(*) AS n FROM board_sync WHERE job_key = ?", (contoso,))["n"] == 0
    res = board.import_actions(ctx, db, inbox, listed=1)
    assert {a["job_key"]: a["changes"] for a in res["applied"]} == {contoso: ["note"]}
    assert notes.latest_for_job(db, _id(db, "Contoso Payments"))["body"] == "Call Sam first"


def test_an_incomplete_or_unreadable_listing_is_never_an_empty_read(ctx, db, tmp_path):
    approve_profile(db, ctx, countries="Netherlands")
    _seed_jobs(ctx, db)
    board.link(db, URL)
    nw = _key_for(db, "Northwind Cloud")
    # Missing folder without evidence of an empty listing: refused, and the page stays unread.
    with pytest.raises(board.BoardError, match="does not exist"):
        board.import_actions(ctx, db, tmp_path / "missing", listed=1)
    assert board.stale_reason(db) and db.get_setting("board.last_import") is None
    # A file that is not JSON: refused by name; nothing is applied or recorded.
    inbox = tmp_path / "in"
    _action(inbox, nw, 1, favorite=True)
    (inbox / "actions" / "broken.json").write_text("{not json")
    with pytest.raises(board.BoardError, match="broken.json"):
        board.import_actions(ctx, db, inbox, listed=2)
    assert views.list_jobs(db, view="favorites")["total"] == 0 and db.get_setting("board.last_import") is None
    (inbox / "actions" / "broken.json").unlink()
    # The listing said two documents, the folder holds one: the pagination did not complete.
    with pytest.raises(board.BoardError, match="2"):
        board.import_actions(ctx, db, inbox, listed=2)
    assert db.get_setting("board.last_import") is None
    # The count matches: read, applied, recorded with its evidence.
    res = board.import_actions(ctx, db, inbox, listed=1)
    assert res["applied"] and db.get_setting("board.last_import")["listed"] == 1 and board.stale_reason(db) is None
    # An empty collection is a real read only when the listing said so.
    board.export(ctx, db, tmp_path / "out")
    assert board.stale_reason(db)
    with pytest.raises(board.BoardError):
        board.import_actions(ctx, db, tmp_path / "empty")
    assert board.stale_reason(db)
    res = board.import_actions(ctx, db, tmp_path / "empty", listed=0)
    assert res["seen"] == 0 and board.stale_reason(db) is None


def test_a_read_without_listing_evidence_is_refused_and_orphans_stay_visible_until_their_job_arrives(ctx, db, tmp_path):
    approve_profile(db, ctx, countries="Netherlands")
    board.link(db, URL)
    inbox = tmp_path / "in"
    inbox.mkdir()
    (inbox / "actions").mkdir()
    # An existing but empty folder with no evidence of an empty listing is not a read.
    with pytest.raises(board.BoardError, match="listing"):
        board.import_actions(ctx, db, inbox)
    assert board.stale_reason(db)
    # An action for a job this database does not hold yet: kept, visible, and applied the moment the job arrives.
    from jobhunter.models import RawPosting

    posting = RawPosting(source_id="greenhouse:fake", external_id="far", title="Platform Engineer", employer_name="Far Away BV", url="https://job-boards.greenhouse.io/far/jobs/1", location_text="Amsterdam, Netherlands", country_codes=["NL"], remote_type="hybrid", description_text="You run Kubernetes on AWS with Terraform and Linux. On-call.", posted_at="2026-10-19T09:00:00+00:00", posted_at_provenance="t", is_employer_hosted=True)
    jid0 = hunt.import_postings(ctx, db, [posting], trigger="test")["job_ids"][0]
    far = board.job_key(db.fetchone("SELECT canonical_key FROM jobs WHERE id = ?", (jid0,))["canonical_key"])
    db.execute("PRAGMA foreign_keys=OFF")
    for table in ("matches", "job_observations", "recommendations", "events"):
        db.execute(f"DELETE FROM {table} WHERE job_id = ?", (jid0,))
    db.execute("DELETE FROM jobs WHERE id = ?", (jid0,))  # as if another machine had found it, not this one
    db.execute("PRAGMA foreign_keys=ON")
    _action(inbox, far, 1, favorite=True, note={"text": "Looks great", "rev": 1})
    res = board.import_actions(ctx, db, inbox, listed=1)
    assert res["orphans"] == [far] and board.stale_reason(db) is None
    st = board.status(db)
    assert st["unresolved_actions"] == 1 and "wait for their jobs" in st["unresolved_note"]
    assert db.fetchone("SELECT COUNT(*) AS n FROM board_sync WHERE job_key = ?", (far,))["n"] == 0  # never acknowledged
    _seed_jobs(ctx, db)
    hunt.import_postings(ctx, db, [posting], trigger="test")  # the hunt finds it here too
    row = db.fetchone("SELECT id, canonical_key FROM jobs WHERE employer_name = 'Far Away BV'")
    assert board.job_key(row["canonical_key"]) == far
    applied = board.apply_unresolved(db)
    assert applied == [far]
    assert views.job_detail(db, row["id"])["favorited"] and notes.latest_for_job(db, row["id"])["body"] == "Looks great"
    assert db.fetchone("SELECT synced_rev FROM board_sync WHERE job_key = ?", (far,))["synced_rev"] == 1
    assert board.status(db)["unresolved_actions"] == 0
    # Export carries the applied state; a replay of the same file is a no-op.
    shard = _shard(board.export(ctx, db, tmp_path / "out"))
    assert shard[far]["state"]["favorited"] and shard[far]["state"]["note"] == "Looks great"
    assert board.import_actions(ctx, db, inbox, listed=1)["applied"] == []
