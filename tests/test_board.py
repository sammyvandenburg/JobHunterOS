"""The personal dashboard hand-off: Hunter writes the page's documents into a folder, the page's actions
come back as JSON files, and a later, separate Hunter run applies them once. No Claude, no network."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jobhunter.config import AppContext
from jobhunter.db import connect
from jobhunter.models import RawPosting
from jobhunter.services import board, cloudstate, hunt, notes, views
from tests.conftest import JOURNEY_DESC, JOURNEY_POSTINGS, SAMPLE_CV, approve_profile


def _seed_jobs(ctx, db):
    posts = []
    for p in JOURNEY_POSTINGS:
        base = dict(source_id="greenhouse:fake", remote_type="onsite", description_text=JOURNEY_DESC, posted_at="2026-10-17T09:00:00+00:00", posted_at_provenance="t", is_employer_hosted=True)
        base.update(p)
        posts.append(RawPosting(**base))
    return hunt.import_postings(ctx, db, posts, trigger="test")


def _key_for(db, employer: str) -> str:
    row = db.fetchone("SELECT canonical_key FROM jobs WHERE employer_name = ?", (employer,))
    return board.job_key(row["canonical_key"])


def _action(folder: Path, key: str, rev: int, **flags) -> None:
    (folder / "actions").mkdir(parents=True, exist_ok=True)
    doc = {"job_key": key, "favorite": False, "saved": False, "applied_at": None, "dismissed": False, "note": None, "rev": rev, "updated_at": "2026-10-20T07:00:00Z", "updated_by": "tab-test"}
    doc.update(flags)
    (folder / "actions" / f"{key}.json").write_text(json.dumps(doc))


def test_export_writes_private_documents_and_a_manifest(ctx, db, tmp_path):
    approve_profile(db, ctx, countries="Netherlands")
    _seed_jobs(ctx, db)
    manifest = board.export(ctx, db, tmp_path / "out")
    files = {w["doc_id"]: json.loads(Path(w["file"]).read_text()) for w in manifest["writes"]}
    assert set(files) == {"profile", "jobs-0", "run"}
    assert manifest["shards"] == 1 and manifest["jobs"] >= 2 and manifest["recommended"] >= 2
    run = files["run"]
    assert run["schema"] == board.SCHEMA and run["shards"] == 1 and run["recommended"]
    shard = files["jobs-0"]["jobs"]
    employers = {d["employer"] for d in shard.values()}
    assert {"Northwind Cloud", "Contoso Payments"} <= employers
    assert "Berlin Robotics" not in employers and "Austin Labs" not in employers  # not recommended: other countries
    first = shard[run["recommended"][0]]
    assert first["recommended"] and first["group"] in {"good", "worth_a_shot", "backup"} and first["reasons"] and first["links"]
    assert len(first["description"]) <= board.DESCRIPTION_CHARS + 1
    assert first["state"] == {"favorited": False, "saved": False, "dismissed": False, "stage": None, "stage_label": None, "applied_at": None, "note": None, "notes_count": 0, "synced_rev": 0}
    # The profile document carries the summary and preferences, never the CV text or a name.
    profile = files["profile"]
    assert profile["summary"] and profile["countries"] == "the Netherlands" and "facts" not in profile
    everything = "".join(Path(w["file"]).read_text() for w in manifest["writes"])
    name = SAMPLE_CV.strip().splitlines()[0].strip()
    assert name not in everything and "Demo Logistics\n" not in everything and "facts_json" not in everything
    assert manifest["delete_if_present"][0] == "board/jobs-1"
    assert db.get_setting("board.last_export")["jobs"] == manifest["jobs"]


def test_export_needs_an_approved_profile(ctx, db, tmp_path):
    with pytest.raises(board.BoardError):
        board.export(ctx, db, tmp_path / "out")


def test_page_actions_are_applied_once_by_a_separate_run(ctx, db, tmp_path, home):
    approve_profile(db, ctx, countries="Netherlands")
    _seed_jobs(ctx, db)
    nw, contoso = _key_for(db, "Northwind Cloud"), _key_for(db, "Contoso Payments")
    inbox = tmp_path / "in"
    _action(inbox, nw, 3, favorite=True, saved=True, note={"text": "Ask about on-call", "rev": 1})
    _action(inbox, contoso, 2, dismissed=True)
    _action(inbox, "jdeadbeefdeadbeefdead", 1, favorite=True)
    db.close()

    # A separate Hunter run (new connection, same data folder) applies them.
    db2 = connect(AppContext.from_env(home=home).paths.db_path)
    res = board.import_actions(ctx, db2, inbox, listed=3)
    assert res["seen"] == 3 and res["orphans"] == ["jdeadbeefdeadbeefdead"] and res["already_applied"] == 0
    changes = {a["job_key"]: a["changes"] for a in res["applied"]}
    assert changes[nw] == ["favorite", "save", "note"] and changes[contoso] == ["dismiss"]
    fav = views.list_jobs(db2, view="favorites")["items"]
    assert [j["employer"] for j in fav] == ["Northwind Cloud"]
    nw_id = fav[0]["id"]
    assert notes.latest_for_job(db2, nw_id)["body"] == "Ask about on-call"
    assert {j["employer"] for j in views.current_recommended(db2)} == {"Northwind Cloud"}  # Contoso dismissed

    # The same files again: nothing is applied twice.
    again = board.import_actions(ctx, db2, inbox, listed=3)
    assert again["applied"] == [] and again["already_applied"] == 2 and notes.count_for_job(db2, nw_id) == 1

    # A newer revision edits the same note instead of adding a second one, un-dismisses, and records applied.
    _action(inbox, nw, 4, favorite=True, saved=True, note={"text": "Ask about on-call and pay", "rev": 2}, applied_at="2026-10-20T08:00:00.000Z")
    _action(inbox, contoso, 3, dismissed=False)
    res = board.import_actions(ctx, db2, inbox, listed=3)
    changes = {a["job_key"]: a["changes"] for a in res["applied"]}
    assert changes[nw] == ["applied", "note"] and changes[contoso] == ["undismiss"]
    assert notes.count_for_job(db2, nw_id) == 1 and notes.latest_for_job(db2, nw_id)["body"] == "Ask about on-call and pay"
    applied = views.list_jobs(db2, view="applied")["items"]
    assert [j["employer"] for j in applied] == ["Northwind Cloud"] and applied[0]["applied_at"].startswith("2026-10-20T08:00:00")
    assert {j["employer"] for j in views.current_recommended(db2)} == {"Contoso Payments"}  # applied jobs leave the list

    # Un-applying on the page is a correction, reversible like every event.
    _action(inbox, nw, 5, favorite=True, saved=True, note={"text": "Ask about on-call and pay", "rev": 2}, applied_at=None)
    res = board.import_actions(ctx, db2, inbox, listed=3)
    assert {a["job_key"]: a["changes"] for a in res["applied"]}[nw] == ["unapplied"]
    assert views.list_jobs(db2, view="applied")["total"] == 0

    # The next export tells the page what was applied (synced_rev) and shows the state Hunter now holds.
    manifest = board.export(ctx, db2, tmp_path / "out")
    shard = json.loads(Path(manifest["writes"][1]["file"]).read_text())["jobs"]
    assert shard[nw]["state"]["synced_rev"] == 5 and shard[nw]["state"]["favorited"] and shard[nw]["state"]["note"] == "Ask about on-call and pay"
    assert shard[contoso]["state"]["synced_rev"] == 3 and not shard[contoso]["state"]["dismissed"]
    db2.close()


def test_link_validates_and_resets_sync_on_a_new_page(ctx, db):
    with pytest.raises(board.BoardError):
        board.link(db, "https://example.com/not-an-artifact")
    st = board.link(db, "https://claude.ai/artifact/5wUUffKaZnmpqGgPnRzPVE/")
    assert st["linked"] and st["url"] == "https://claude.ai/artifact/5wUUffKaZnmpqGgPnRzPVE"
    db.insert("board_sync", {"job_key": "jabc", "synced_rev": 2, "synced_at": "x"})
    board.link(db, "https://claude.ai/artifact/5wUUffKaZnmpqGgPnRzPVE")  # same page: nothing forgotten
    assert db.fetchone("SELECT COUNT(*) AS n FROM board_sync")["n"] == 1
    board.link(db, "https://claude.ai/code/artifact/0a1b2c3d-4e5f-6789-abcd-ef0123456789")
    assert db.fetchone("SELECT COUNT(*) AS n FROM board_sync")["n"] == 0
    assert board.page_path().exists()


def test_cloud_state_carries_the_dashboard_link_but_never_the_applied_revisions(ctx, db, tmp_path, home, monkeypatch):
    approve_profile(db, ctx, countries="Netherlands")
    _seed_jobs(ctx, db)
    board.link(db, "https://claude.ai/artifact/5wUUffKaZnmpqGgPnRzPVE")
    nw = _key_for(db, "Northwind Cloud")
    _action(tmp_path / "in", nw, 2, favorite=True)
    board.import_actions(ctx, db, tmp_path / "in", listed=1)
    bundle = cloudstate.export_bundle(ctx, db)
    assert bundle["inputs"]["board"] == {"url": "https://claude.ai/artifact/5wUUffKaZnmpqGgPnRzPVE"}
    assert not cloudstate.check_bundle_privacy(bundle)

    # A fresh cloud machine restores the link and the favorite, but acknowledges no revision it did not apply itself.
    other = tmp_path / "cloud-home"
    monkeypatch.setenv("JOBHUNTER_HOME", str(other))
    cctx = AppContext.from_env(home=other)
    cdb = connect(cctx.paths.db_path)
    cloudstate.import_bundle(cctx, cdb, bundle, inputs=True, results=True)
    assert board.status(cdb)["url"] == "https://claude.ai/artifact/5wUUffKaZnmpqGgPnRzPVE"
    assert [j["employer"] for j in views.list_jobs(cdb, view="favorites")["items"]] == ["Northwind Cloud"]
    assert cdb.fetchone("SELECT COUNT(*) AS n FROM board_sync")["n"] == 0
    # An old state file that still carries revisions is accepted, and the revisions are ignored.
    assert board.import_sync_state(cdb, {"url": bundle["inputs"]["board"]["url"], "synced": {nw: 4}}) == {"url": False, "synced_ignored": 1}
    assert cdb.fetchone("SELECT COUNT(*) AS n FROM board_sync")["n"] == 0
    cdb.close()


def test_page_notes_can_be_deleted_and_preferences_edited_from_the_page(ctx, db, tmp_path):
    approve_profile(db, ctx, countries="Netherlands")
    _seed_jobs(ctx, db)
    nw = _key_for(db, "Northwind Cloud")
    inbox = tmp_path / "in"
    _action(inbox, nw, 1, note={"text": "First note", "rev": 1})
    board.import_actions(ctx, db, inbox, listed=None)
    nw_id = db.fetchone("SELECT id FROM jobs WHERE employer_name = 'Northwind Cloud'")["id"]
    assert notes.latest_for_job(db, nw_id)["body"] == "First note"
    # Delete on the page: the note goes, nothing else changes, and a re-run does not bring it back.
    _action(inbox, nw, 2, note={"text": "", "rev": 2, "deleted": True})
    res = board.import_actions(ctx, db, inbox, listed=None)
    assert {a["job_key"]: a["changes"] for a in res["applied"]}[nw] == ["note_deleted"]
    assert notes.latest_for_job(db, nw_id) is None
    assert board.import_actions(ctx, db, inbox, listed=None)["applied"] == []
    # Preferences edited on the page travel through the same parser as setup, once.
    (inbox / "actions").mkdir(exist_ok=True)
    (inbox / "actions" / "_prefs.json").write_text(json.dumps({"job_key": "_prefs", "location": "Utrecht, Netherlands", "countries": "Netherlands, Germany", "salary": "60000 EUR a year", "remote": "remote preferred, hybrid ok, never fully on-site", "rev": 1}))
    res = board.import_actions(ctx, db, inbox, listed=None)
    assert {a["job_key"]: a["changes"] for a in res["applied"]}["_prefs"] == ["location", "countries", "salary", "remote"]
    from jobhunter.services import preferences as prefs_svc

    p = prefs_svc.get(db)
    assert p.current_location == "Utrecht, Netherlands" and set(p.work_countries) == {"NL", "DE"} and p.salary_min == 60000 and p.remote == "preferred" and p.work_modes == ["remote", "hybrid"]
    assert board.import_actions(ctx, db, inbox, listed=None)["applied"] == []
    # A bad edit is reported, not applied.
    (inbox / "actions" / "_prefs.json").write_text(json.dumps({"job_key": "_prefs", "countries": "Narnia", "rev": 2}))
    res = board.import_actions(ctx, db, inbox, listed=None)
    assert res["applied"][0]["changes"][0].startswith("rejected:") and set(prefs_svc.get(db).work_countries) == {"NL", "DE"}
    # The export tells the page which preference edit was applied, and carries the compact profile and status.
    manifest = board.export(ctx, db, tmp_path / "out")
    profile = json.loads(Path(manifest["writes"][0]["file"]).read_text())
    assert profile["prefs_synced_rev"] == 2 and profile["headline"].endswith("Engineer") and profile["prefs"]["work_modes"] == ["remote", "hybrid"]
    run = json.loads(Path(manifest["writes"][-1]["file"]).read_text())
    assert run["hunter"]["automatic_hunting"] == "off" and run["hunter"]["email"]["enabled"] is False and isinstance(run["attention"], list)
    shard = json.loads(Path(manifest["writes"][1]["file"]).read_text())["jobs"]
    first = next(iter(shard.values()))
    assert first["why"] and not first["why"].startswith(("Good match", "Worth a shot")) and "list" in first


def test_dashboard_refinements_stable_reference_counts_and_short_locations(ctx, db, tmp_path, capsys):
    from jobhunter import cli

    approve_profile(db, ctx, countries="Netherlands")
    _seed_jobs(ctx, db)
    hunt.import_postings(ctx, db, [RawPosting(source_id="greenhouse:fake", external_id="far", title="Platform Engineer", employer_name="Far Away BV", url="https://job-boards.greenhouse.io/far/jobs/1", location_text="Remote", remote_type="remote", country_codes=[], description_text=JOURNEY_DESC + " Applicants must be based in the Netherlands.", posted_at="2026-10-19T09:00:00+00:00", posted_at_provenance="t", is_employer_hosted=True)], trigger="test")
    board.link(db, "https://claude.ai/artifact/5wUUffKaZnmpqGgPnRzPVE")
    board.import_actions(ctx, db, tmp_path / "none", listed=0)
    manifest = board.export(ctx, db, tmp_path / "out", limit=1)
    run = json.loads(Path([w for w in manifest["writes"] if w["doc_id"] == "run"][0]["file"]).read_text())
    assert len(run["recommended"]) == 1 and run["counts"]["recommended_total"] >= 2  # loaded vs total, honestly
    shard = {}
    for w in manifest["writes"]:
        if w["collection"] == "board":
            shard.update(json.loads(Path(w["file"]).read_text())["jobs"])
    far = next(d for d in shard.values() if d["employer"] == "Far Away BV")
    assert "the posting says" in far["remote_restriction"] and "the posting says" not in far["location_label"] and len(far["location_label"]) < len(far["remote_restriction"])
    assert far["location_label"].startswith("Remote")
    # The page's "Help me apply" carries the job key; the command line resolves it on any machine.
    key = far["key"]
    assert cli.main(["--json", "jobs", "show", key]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["employer"] == "Far Away BV"
    assert cli.main(["--json", "jobs", "show", "j00000000000000000000"]) == 1 and "No job with key" in json.loads(capsys.readouterr().out)["error"]
