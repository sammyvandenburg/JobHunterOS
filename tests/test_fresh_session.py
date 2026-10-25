"""The brief's closing check: the selected CV, the answers in the person's wording, the notes and the confirmed
application survive unchanged into a fresh session: a new Hunter process on the same computer, a new browser
session on the same page, and a restore into an empty database (where only the CV file itself is missing,
by design, until it is added again)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from jobhunter.config import AppContext
from jobhunter.db import connect
from jobhunter.services import actions, board, cloudstate, workspace
from jobhunter.services import documents as docs
from tests.conftest import approve_profile
from tests.test_board import _action, _key_for, _seed_jobs
from tests.test_cv_variants import PLATFORM_CV


def _journey(ctx, db, tmp_path):
    approve_profile(db, ctx, countries="Netherlands")
    _seed_jobs(ctx, db)
    plat = docs.add_cv_variant(db, ctx.paths, PLATFORM_CV.encode(), "platform-en.txt", "Platform — English", tags=["platform"])
    board.link(db, "https://claude.ai/artifact/5wUUffKaZnmpqGgPnRzPVE")
    board.import_actions(ctx, db, tmp_path / "none", listed=0)
    nw = _key_for(db, "Northwind Cloud")
    jid = db.fetchone("SELECT id FROM jobs WHERE employer_name = 'Northwind Cloud'")["id"]
    inbox = tmp_path / "in1"
    _action(inbox, nw, 1, requests={"prepare": {"id": "prepare:1", "at": "2026-10-22T08:00:00Z", "params": {}}}, cv_variant=plat["key"], cv_choice="student", note={"text": "Ask about the on-call rota.", "rev": 1})
    board.import_actions(ctx, db, inbox, listed=1)
    workspace.run_pending(ctx, db)
    manifest = board.export(ctx, db, tmp_path / "out1")
    work = json.loads(Path(next(w["file"] for w in manifest["writes"] if w["doc_id"] == nw)).read_text())
    aid = next(a["id"] for a in work["package"]["answers"] if not a["needs_user"])
    inbox2 = tmp_path / "in2"
    _action(inbox2, nw, 2, cv_variant=plat["key"], cv_choice="student", note={"text": "Ask about the on-call rota.", "rev": 1},
            answers={aid: {"text": "My own wording, kept.", "rev": 1, "updated_at": "2026-10-22T08:10:00Z"}},
            applied_at="2026-10-22T09:00:00Z", applied_cv="prepared", followup={"action": "set", "due_at": "2026-10-29T09:00:00Z", "at": "2026-10-22T09:01:00Z"})
    board.import_actions(ctx, db, inbox2, listed=1)
    manifest = board.export(ctx, db, tmp_path / "out2")
    return {"key": nw, "jid": jid, "plat": plat, "aid": aid, "manifest": manifest}


def _snapshot(db, jid, key, aid):
    w = workspace.work_doc(db, jid, key)
    app = db.fetchone("SELECT * FROM applications WHERE job_id = ?", (jid,))
    return {
        "cv": (w["cv"]["selected"]["key"], w["cv"]["selected"]["version"], w["cv"]["selected"]["choice"]),
        "answer": next(a for a in w["package"]["answers"] if a["id"] == aid)["user"]["text"],
        "note": db.fetchone("SELECT body FROM job_notes WHERE job_id = ? AND deleted_at IS NULL", (jid,))["body"],
        "applied": (app["applied_at"][:19], app["submitted_cv_choice"], app["submitted_cv_revision_key"]),
        "followup": workspace.current_followup(db, jid)["due"],
        "state": w["state"],
    }


def test_the_work_survives_a_fresh_hunter_process_unchanged(ctx, db, tmp_path, home):
    j = _journey(ctx, db, tmp_path)
    before = _snapshot(db, j["jid"], j["key"], j["aid"])
    assert before["cv"] == (j["plat"]["key"], 1, "student") and before["answer"] == "My own wording, kept." and before["state"] == "applied"
    assert before["applied"][1] == "prepared" and before["applied"][2] == j["plat"]["revision_key"] and before["followup"] == "2026-10-29"
    db.close()
    code = f"""
import json
from jobhunter.config import AppContext
from jobhunter.db import connect
from jobhunter.services import workspace
ctx = AppContext.from_env(demo=False)
db = connect(ctx.paths.db_path)
w = workspace.work_doc(db, {j['jid']}, {j['key']!r})
app = db.fetchone("SELECT * FROM applications WHERE job_id = ?", ({j['jid']},))
print(json.dumps({{"cv": [w["cv"]["selected"]["key"], w["cv"]["selected"]["version"], w["cv"]["selected"]["choice"]], "answer": next(a for a in w["package"]["answers"] if a["id"] == {j['aid']!r})["user"]["text"], "note": db.fetchone("SELECT body FROM job_notes WHERE job_id = ? AND deleted_at IS NULL", ({j['jid']},))["body"], "applied": [app["applied_at"][:19], app["submitted_cv_choice"], app["submitted_cv_revision_key"]], "followup": workspace.current_followup(db, {j['jid']})["due"], "state": w["state"]}}))
"""
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env={**os.environ, "JOBHUNTER_HOME": str(home)}, cwd=str(Path(__file__).resolve().parents[1]))
    assert out.returncode == 0, out.stderr
    fresh = json.loads(out.stdout)
    assert fresh == {k: (list(v) if isinstance(v, tuple) else v) for k, v in before.items()}


def test_the_work_survives_a_restore_into_an_empty_database_except_the_file_itself(ctx, db, tmp_path):
    j = _journey(ctx, db, tmp_path)
    bundle = cloudstate.export_bundle(ctx, db)
    assert cloudstate.check_bundle_privacy(bundle) == [] and "My own wording" not in json.dumps(bundle) and "on-call rota" not in json.dumps(bundle)
    ctx2 = AppContext.from_env(demo=False, home=tmp_path / "empty")
    db2 = connect(ctx2.paths.db_path)
    cloudstate.import_bundle(ctx2, db2, bundle, inputs=True, results=True)
    jid2 = db2.fetchone("SELECT id FROM jobs WHERE employer_name = 'Northwind Cloud'")["id"]
    app2 = db2.fetchone("SELECT * FROM applications WHERE job_id = ?", (jid2,))
    assert app2["applied_at"].startswith("2026-10-22T09:00") and app2["submitted_cv_choice"] == "prepared" and app2["submitted_cv_revision_key"] == j["plat"]["revision_key"]
    assert app2["cv_variant_key"] == j["plat"]["key"] and docs.get_cv(db2, j["plat"]["key"])["label"] == "Platform — English"
    # The note and the answers come back from the page (its database keeps them), not from Git.
    board.link(db2, "https://claude.ai/artifact/5wUUffKaZnmpqGgPnRzPVE")
    board.import_actions(ctx2, db2, tmp_path / "in2", listed=1)
    assert db2.fetchone("SELECT body FROM job_notes WHERE job_id = ? AND deleted_at IS NULL", (jid2,))["body"] == "Ask about the on-call rota."
    assert db2.get_setting(workspace.PENDING_SETTING)[j["key"]]["answers"][j["aid"]]["text"] == "My own wording, kept."  # kept until the package exists here
    workspace.run_request(ctx2, db2, workspace.request(db2, jid2, "prepare", {}, request_id="prepare:restored")["request_id"])
    w2 = workspace.work_doc(db2, jid2, j["key"])  # what the next export writes: the kept answer has landed
    assert not db2.get_setting(workspace.PENDING_SETTING)
    assert next(a for a in w2["package"]["answers"] if a["id"] == j["aid"])["user"]["text"] == "My own wording, kept."
    assert w2["application"]["submitted_cv"] == {"choice": "prepared", "label": "Platform — English", "version": 1, "filename": "platform-en.txt"}
    with pytest.raises(docs.CVError, match="Add this CV again"):
        docs.cv_file(db2, j["plat"]["key"])
    assert not actions.history(db2, jid2) or db2.fetchone("SELECT COUNT(*) AS n FROM events WHERE job_id = ? AND kind = 'applied' AND reverted_by IS NULL", (jid2,))["n"] == 1
    db2.close()


def test_the_page_shows_the_same_work_in_a_new_browser_session(ctx, db, tmp_path):
    pytest.importorskip("playwright.sync_api")
    from playwright.sync_api import sync_playwright

    from tests.fake_artifact import SHIM, FakeArtifact
    from tests.test_browser import _browser

    j = _journey(ctx, db, tmp_path)
    fake = FakeArtifact()
    fake.load_export(j["manifest"])
    for f in sorted((tmp_path / "in2" / "actions").glob("*.json")):
        fake.load({"actions/" + f.stem: json.loads(f.read_text())})
    url = fake.start()
    try:
        with sync_playwright() as p:
            browser = _browser(p)
            seen = []
            for _ in range(2):  # two separate browser sessions: nothing lives in the browser itself
                context = browser.new_context(viewport={"width": 1200, "height": 900})
                context.add_init_script(SHIM)
                page = context.new_page()
                errors: list[str] = []
                page.on("pageerror", lambda e, errors=errors: errors.append(str(e)))
                page.goto(url + "/")
                page.wait_for_selector("button.row")
                page.get_by_role("button", name="Applied").first.click()
                page.locator(f"button.row[data-key='{j['key']}']").first.click()
                page.wait_for_selector("textarea#note")
                dialog = page.get_by_role("dialog")
                text = dialog.inner_text()
                seen.append({"note": page.locator("textarea#note").input_value(), "answer": page.locator(f"#ans-{j['aid']}").input_value(), "cv": page.locator("#cv-select").evaluate("s => s.options[s.selectedIndex].text"), "applied": "Applied 22 Oct 2026" in text, "sent": "CV sent: Platform — English (version 1)" in text, "followup": "Follow-up reminder: 29 Oct 2026" in text, "main": dialog.get_by_role("button", name="View application").count() >= 1})
                assert not errors, errors
                context.close()
            browser.close()
    finally:
        fake.stop()
    assert seen[0] == seen[1] == {"note": "Ask about the on-call rota.", "answer": "My own wording, kept.", "cv": "Platform — English (version 1)", "applied": True, "sent": True, "followup": True, "main": True}
