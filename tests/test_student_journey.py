"""The student journey on a clean installation, through the same commands Hunter runs for them:
start -> upload documents -> approve profile -> set preferences -> find jobs -> dashboard documents ->
favorite, save, note and "applied" on the dashboard -> close and reopen (every command is its own process
in real life; here its own connection) -> another hunt -> progress still there -> email switched on.
Synthetic documents and sources; nothing leaves the machine."""

from __future__ import annotations

import json
from pathlib import Path

from jobhunter import cli
from jobhunter.services import board
from tests.conftest import LINKEDIN_TEXT, SAMPLE_CV


def _runner(capsys):
    def run(*args):
        rc = cli.main(["--json", *args])
        out = capsys.readouterr().out.strip()
        payload = json.loads(out) if out else {}
        assert rc == 0, (args, payload)
        return payload

    return run


def _page_action(folder: Path, key: str, rev: int, **flags):
    (folder / "actions").mkdir(parents=True, exist_ok=True)
    doc = {"job_key": key, "favorite": False, "saved": False, "applied_at": None, "dismissed": False, "note": None, "rev": rev, "updated_at": "2026-10-20T09:00:00Z", "updated_by": "tab-journey"}
    doc.update(flags)
    (folder / "actions" / f"{key}.json").write_text(json.dumps(doc))


def test_clean_install_student_journey(home, fake_sources, tmp_path, capsys):
    run = _runner(capsys)
    cv = tmp_path / "cv.txt"
    cv.write_text(SAMPLE_CV)
    li = tmp_path / "linkedin.txt"
    li.write_text(LINKEDIN_TEXT)

    # Start Hunter: a clean install asks the readiness question first and recommends nothing.
    first = run("next")
    assert first["setup_next"]["key"] == "readiness"
    run("setup", "answer", "readiness", "yes")
    assert run("setup", "next")["next"]["key"] == "documents"

    # Upload both documents: the profile is built from them.
    run("docs", "add", "--kind", "cv", str(cv))
    run("docs", "add", "--kind", "linkedin", str(li))
    assert run("setup", "next")["next"]["key"] == "profile_approve"
    shown = run("profile", "show")
    assert shown.get("summary") or shown.get("summary_text")
    questions = run("profile", "questions")
    questions = questions if isinstance(questions, list) else questions.get("questions", [])
    if any(q["key"] == "target_direction" for q in questions):
        run("profile", "answer", "target_direction", "platform, devops")
    run("setup", "answer", "profile_approve", "yes")

    # Preferences in one go, then setup is finished.
    assert run("setup", "next")["next"]["key"] == "basics"
    run("setup", "basics", "--location", "Rotterdam, Netherlands", "--countries", "Netherlands", "--salary", "skip", "--remote", "nice to have", "--frequency", "only when asked", "--notify", "dashboard")
    assert run("next")["setup_next"] is None

    # Find jobs: only the two Dutch postings are recommended; the German, US-only and non-IT ones are not.
    hunt1 = run("hunt", "run")
    assert hunt1["status"] in {"ok", "partial", "completed"} and hunt1["jobs_new"] >= 4
    rec = run("jobs", "list")["items"]
    assert {j["employer"] for j in rec} == {"Northwind Cloud", "Contoso Payments"}
    assert all(j["group"] in {"good", "worth_a_shot", "backup"} and "%" not in (j["explanation"] or "") for j in rec)

    # Open the dashboard: Hunter publishes the page once (Claude does the publish) and links it; the export is what the page shows.
    assert run("board", "status")["linked"] is False
    page = run("board", "page")
    assert Path(page["page"]).exists() and page["capabilities"]["db"]["rules"][0]["read"] == "owner"
    run("board", "link", "https://claude.ai/artifact/testpage0001")
    out = tmp_path / "out"
    # The page's actions are always read first; before the first write there are none.
    assert run("board", "status")["actions_stale"]
    empty = tmp_path / "empty"
    empty.mkdir()
    assert run("board", "import", "--in", str(empty), "--listed", "0")["seen"] == 0
    manifest = run("board", "export", "--out", str(out))
    shard = json.loads(Path(manifest["writes"][1]["file"]).read_text())["jobs"]
    by_emp = {d["employer"]: d for d in shard.values()}
    assert manifest["recommended"] == 2 and set(by_emp) == {"Northwind Cloud", "Contoso Payments"}
    northwind, contoso = by_emp["Northwind Cloud"]["key"], by_emp["Contoso Payments"]["key"]
    profile_doc = json.loads(Path(manifest["writes"][0]["file"]).read_text())
    assert profile_doc["countries"] == "the Netherlands" and profile_doc["location"] == "Rotterdam, Netherlands"

    # On the dashboard: favorite and save Northwind, add a note, mark Contoso applied (what the page writes).
    inbox = tmp_path / "in"
    _page_action(inbox, northwind, 1, favorite=True, saved=True, note={"text": "Great team page. Ask about on-call.", "rev": 1})
    _page_action(inbox, contoso, 1, applied_at="2026-10-20T09:30:00.000Z")

    # Close and reopen: a new Hunter run reads the page's actions and applies them once.
    applied = run("board", "import", "--in", str(inbox), "--listed", "2")
    assert {a["job_key"]: a["changes"] for a in applied["applied"]} == {northwind: ["favorite", "save", "note"], contoso: ["applied"]}
    assert run("board", "import", "--in", str(inbox), "--listed", "2")["already_applied"] == 2
    favs = run("jobs", "list", "--view", "favorites")["items"]
    assert [j["employer"] for j in favs] == ["Northwind Cloud"] and favs[0]["saved"] and favs[0]["notes_count"] == 1
    assert [j["employer"] for j in run("jobs", "list", "--view", "applied")["items"]] == ["Contoso Payments"]
    assert [j["employer"] for j in run("jobs", "list")["items"]] == ["Northwind Cloud"]  # applied jobs leave the list

    # Run another hunt: the same postings again. Nothing is duplicated and the progress is still there.
    hunt2 = run("hunt", "run")
    assert hunt2["jobs_new"] == 0
    favs = run("jobs", "list", "--view", "favorites")["items"]
    assert [j["employer"] for j in favs] == ["Northwind Cloud"] and favs[0]["notes_count"] == 1
    assert [j["employer"] for j in run("jobs", "list", "--view", "applied")["items"]] == ["Contoso Payments"]
    # Enable email: one confirmed address, Claude's Gmail connector as the transport, and the first digest is ready
    # for Claude to send; it contains the recommended job and not the one already applied to.
    run("digest", "address", "student@example.invalid")
    run("digest", "transport", "claude")
    prepared = run("digest", "prepare")
    assert prepared["status"] == "ready" and prepared["to"] == "student@example.invalid"
    assert "Northwind Cloud" in prepared["text"] and "Contoso Payments" not in prepared["text"]
    run("digest", "record", str(prepared["notification_id"]), "failed", "--error", "test: not sent")

    # Write the page, and check that progress and the applied state travel with it.
    manifest = run("board", "export", "--out", str(out))
    shard = json.loads(Path(manifest["writes"][1]["file"]).read_text())["jobs"]
    assert shard[northwind]["state"]["favorited"] and shard[northwind]["state"]["note"].startswith("Great team") and shard[northwind]["state"]["synced_rev"] == 1
    assert shard[contoso]["state"]["stage"] == "applied" and not shard[contoso]["recommended"] and shard[contoso]["list"] == "acted"
    run_doc = json.loads(Path(manifest["writes"][-1]["file"]).read_text())
    assert run_doc["counts"] == {"recommended": 1, "jobs": 2, "favorites": 1, "saved": 1, "dismissed": 0, "applied": 1, "old": 0, "uncertain": 0, "recommended_total": 1}
    assert run_doc["sync"]["actions_read_at"] and run_doc["sync"]["actions_seen"] == 2

    # After Hunter wrote the page, nothing recommends or emails again until the page's actions are read:
    # the student may have pressed Applied in between.
    assert run("digest", "prepare")["status"] == "blocked"
    assert cli.main(["--json", "hunt", "run"]) == 1 and "read" in json.loads(capsys.readouterr().out)["error"].lower()
    assert cli.main(["--json", "board", "export", "--out", str(out)]) == 1 and capsys.readouterr()
    run("board", "import", "--in", str(inbox), "--listed", "2")
    assert run("board", "status")["actions_stale"] is None
    run("digest", "record", str(run("digest", "prepare")["notification_id"]), "failed", "--error", "test: not sent")

    # The cloud state file (for nightly runs) carries the dashboard link and nothing private.
    bundle_path = tmp_path / "state.json"
    run("cloud", "export", "--out", str(bundle_path))
    bundle = json.loads(bundle_path.read_text())
    assert bundle["inputs"]["board"] == {"url": "https://claude.ai/artifact/testpage0001"}  # no revisions: each database acknowledges its own
    text = bundle_path.read_text()
    assert "Great team page" not in text and "Sam Example" not in text
    states = {j["employer_name"]: j["state"] for j in bundle["results"]["jobs"] if j.get("state")}
    assert states["Northwind Cloud"]["favorite"] and states["Northwind Cloud"]["saved"] and states["Northwind Cloud"]["has_note"]
    assert states["Contoso Payments"]["stage"] == "applied"
    assert run("board", "status")["actions_applied"] == 2 and board.SCHEMA in Path(manifest["writes"][0]["file"]).read_text()
