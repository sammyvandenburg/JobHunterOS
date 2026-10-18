"""Regression tests for three audited defects: the launchd plist template, scheduled-run
accounting when a hunt is skipped, and the file secret store with a wrong passphrase."""

from __future__ import annotations

import plistlib
import shlex
import sys
from datetime import timedelta

import pytest

from jobhunter.config import AppContext
from jobhunter.services import datamgmt, hunt, schedule, worker
from jobhunter.services import secrets as secrets_mod
from jobhunter.services.secrets import SecretStore, SecretStoreLocked
from jobhunter.util import now

STATS = {"run_id": 1, "status": "completed", "jobs_new": 0, "jobs_relevant_new": 0, "sources_failed": 0}


# ---------------------------------------------------------------------------
# 1. Service templates: the launchd plist must be valid XML with a real argv list.
# ---------------------------------------------------------------------------
def test_launchd_plist_is_valid_and_keeps_spaces_in_paths(tmp_path, monkeypatch):
    spaced_home = tmp_path / "job hunter home"
    monkeypatch.setenv("JOBHUNTER_HOME", str(spaced_home))
    ctx = AppContext.from_env(demo=False, home=spaced_home)
    monkeypatch.setattr(worker, "_which", lambda name: "/Applications/My Tools/uv")

    t = worker.service_templates(ctx)
    for key in ("platform", "command", "command_argv", "systemd", "launchd", "launchd_instructions", "windows", "cron"):
        assert key in t, key

    # The plist is pure XML (instructions live outside it) and parses.
    assert t["launchd"].lstrip().startswith("<?xml")
    plist = plistlib.loads(t["launchd"].encode("utf-8"))
    args = plist["ProgramArguments"]
    assert isinstance(args, list)
    assert args[0] == "/Applications/My Tools/uv"
    assert "worker" in args and "run" in args
    assert "worker run" in " ".join(args)
    assert plist["EnvironmentVariables"]["JOBHUNTER_HOME"] == str(spaced_home)
    assert plist["WorkingDirectory"] == args[args.index("--project") + 1]
    assert "LaunchAgents" in t["launchd_instructions"]

    # The one-line command and the cron/systemd lines quote paths with spaces.
    assert t["command_argv"] == args
    assert t["command"] == shlex.join(args)
    assert shlex.split(t["command"]) == args
    assert shlex.quote(str(spaced_home)) in t["cron"]
    assert t["command"] in t["cron"] and t["command"] in t["systemd"]
    assert f'"{spaced_home}"' in t["windows"] or f"'{spaced_home}'" in t["windows"]


def test_service_templates_fall_back_to_python_without_uv(ctx, monkeypatch):
    monkeypatch.setattr(worker, "_which", lambda name: None)
    t = worker.service_templates(ctx)
    assert t["command_argv"] == [sys.executable, "-m", "jobhunter", "worker", "run"]
    plist = plistlib.loads(t["launchd"].encode("utf-8"))
    assert plist["ProgramArguments"] == t["command_argv"]


def test_schedule_install_prints_instructions_before_plist(ctx, home, capsys, monkeypatch):
    from jobhunter import cli

    assert cli.main(["--home", str(home), "schedule", "install"]) == 0
    out = capsys.readouterr().out
    assert out.index("LaunchAgents") < out.index("<?xml")
    assert "<!--" not in out.split("<?xml", 1)[1].split("</plist>", 1)[0]


# ---------------------------------------------------------------------------
# 2. Worker tick: a skipped hunt (another one is running) is not a run.
# ---------------------------------------------------------------------------
def _make_due(db) -> str:
    schedule.set_timezone(db, "UTC")
    schedule.set_frequency(db, "every_2h")
    schedule.enable(db)
    past = (now() - timedelta(minutes=10)).isoformat()
    db.execute("UPDATE schedule SET next_run_at = ? WHERE id = 1", (past,))
    return past


def test_tick_does_not_mark_ran_when_hunt_in_progress(ctx, db, monkeypatch):
    past = _make_due(db)
    before = schedule.get(db)

    def busy(*a, **k):
        raise hunt.HuntInProgress("busy")

    monkeypatch.setattr(hunt, "run_hunt", busy)
    out = worker.tick(ctx, db)
    assert out["ran_hunt"] is False
    assert out["hunt"] == {"skipped": "busy"}
    after = schedule.get(db)
    assert after["last_run_at"] == before["last_run_at"]
    assert after["next_run_at"] == past  # still due: the worker tries again next tick

    monkeypatch.setattr(hunt, "run_hunt", lambda c, d, trigger="scheduled", **k: dict(STATS))
    out2 = worker.tick(ctx, db)
    assert out2["ran_hunt"] is True and out2["hunt"]["status"] == "completed"
    done = schedule.get(db)
    assert done["last_run_at"] is not None and done["last_run_at"] != before["last_run_at"]
    assert done["next_run_at"] != past
    assert worker.tick(ctx, db)["decision"]["reason"] == "not due yet"


def test_tick_marks_ran_when_hunt_fails_inside_run(ctx, db, monkeypatch):
    past = _make_due(db)

    def boom(*a, **k):
        raise RuntimeError("source blew up")

    monkeypatch.setattr(hunt, "run_hunt", boom)
    with pytest.raises(RuntimeError):
        worker.tick(ctx, db)
    s = schedule.get(db)
    assert s["last_run_at"] is not None and s["next_run_at"] != past


# ---------------------------------------------------------------------------
# 3. Secret store (file backend): a wrong passphrase must never destroy the file.
# ---------------------------------------------------------------------------
@pytest.fixture
def file_backend(home, monkeypatch):
    monkeypatch.setenv(secrets_mod.ENV_BACKEND, "file")
    monkeypatch.setenv(secrets_mod.ENV_PASSPHRASE, "right")
    return home


def test_wrong_passphrase_locks_store_and_preserves_file(ctx, file_backend, monkeypatch):
    SecretStore(ctx.paths).set("smtp.password", "token-123")
    original = ctx.paths.secrets_file.read_bytes()
    assert original.startswith(b"ENC1\n")

    monkeypatch.setenv(secrets_mod.ENV_PASSPHRASE, "wrong")
    store = SecretStore(ctx.paths)
    with pytest.raises(SecretStoreLocked):
        store.get("smtp.password")
    with pytest.raises(SecretStoreLocked):
        store.set("smtp.password", "new-value")
    assert ctx.paths.secrets_file.read_bytes() == original
    with pytest.raises(SecretStoreLocked):
        store.delete("smtp.password")
    assert ctx.paths.secrets_file.read_bytes() == original
    with pytest.raises(SecretStoreLocked):
        store.list_keys()
    desc = store.describe()
    assert desc["readable"] is False
    assert "passphrase" in desc["problem"].lower() and "reset-secrets" in desc["problem"]
    assert "Nothing was changed" in desc["problem"]

    monkeypatch.setenv(secrets_mod.ENV_PASSPHRASE, "right")
    good = SecretStore(ctx.paths)
    assert good.get("smtp.password") == "token-123"
    assert good.describe()["readable"] is True
    assert "problem" not in good.describe()


def test_missing_passphrase_on_encrypted_file_is_locked_not_empty(ctx, file_backend, monkeypatch):
    SecretStore(ctx.paths).set("gmail.token", "abc")
    original = ctx.paths.secrets_file.read_bytes()
    monkeypatch.delenv(secrets_mod.ENV_PASSPHRASE)
    store = SecretStore(ctx.paths)
    with pytest.raises(SecretStoreLocked):
        store.set("gmail.token", "overwritten")
    assert ctx.paths.secrets_file.read_bytes() == original
    assert store.describe()["readable"] is False


def test_corrupt_file_raises_and_is_preserved(ctx, file_backend):
    ctx.paths.secrets_file.parent.mkdir(parents=True, exist_ok=True)
    garbage = b"\x00\xffnot a secrets file\n\x01"
    ctx.paths.secrets_file.write_bytes(garbage)
    store = SecretStore(ctx.paths)
    with pytest.raises(SecretStoreLocked):
        store.get("x")
    with pytest.raises(SecretStoreLocked):
        store.set("x", "y")
    assert ctx.paths.secrets_file.read_bytes() == garbage
    assert store.describe()["readable"] is False


def test_missing_file_starts_empty_and_save_is_atomic(ctx, file_backend):
    store = SecretStore(ctx.paths)
    assert store.get("nothing") is None and store.list_keys() == []
    store.set("a", "1")
    store.set("b", "2")
    assert sorted(store.list_keys()) == ["a", "b"]
    store.delete("a")
    assert store.list_keys() == ["b"]
    leftovers = [p for p in ctx.paths.secrets_file.parent.iterdir() if p.name.startswith(ctx.paths.secrets_file.name) and p != ctx.paths.secrets_file]
    assert leftovers == []
    if sys.platform != "win32":
        assert ctx.paths.secrets_file.stat().st_mode & 0o777 == 0o600


def test_reset_file_store_renames_never_deletes(ctx, file_backend, monkeypatch):
    SecretStore(ctx.paths).set("smtp.password", "token-123")
    original = ctx.paths.secrets_file.read_bytes()
    monkeypatch.setenv(secrets_mod.ENV_PASSPHRASE, "wrong")
    res = secrets_mod.reset_file_store(ctx.paths)
    assert res["status"] == "reset"
    kept = ctx.paths.secrets_file.parent / res["kept"].rsplit("/", 1)[-1]
    assert kept.exists() and ".unreadable-" in kept.name
    assert kept.read_bytes() == original
    assert not ctx.paths.secrets_file.exists()
    # A fresh store works again with the new passphrase and nothing else lingers.
    store = SecretStore(ctx.paths)
    assert store.get("smtp.password") is None
    store.set("smtp.password", "fresh")
    assert store.get("smtp.password") == "fresh"
    # Nothing to reset: say so, do not invent a rename.
    ctx.paths.secrets_file.unlink()
    assert secrets_mod.reset_file_store(ctx.paths)["status"] == "nothing_to_do"


def test_reset_file_store_refuses_readable_file_unless_forced(ctx, file_backend):
    SecretStore(ctx.paths).set("smtp.password", "token-123")
    res = secrets_mod.reset_file_store(ctx.paths)
    assert res["status"] == "readable" and ctx.paths.secrets_file.exists()
    res = secrets_mod.reset_file_store(ctx.paths, force=True)
    assert res["status"] == "reset" and not ctx.paths.secrets_file.exists()


def test_cli_data_reset_secrets(ctx, file_backend, home, capsys, monkeypatch):
    import json

    from jobhunter import cli

    SecretStore(ctx.paths).set("smtp.password", "token-123")
    monkeypatch.setenv(secrets_mod.ENV_PASSPHRASE, "wrong")
    assert cli.main(["--home", str(home), "--json", "data", "reset-secrets"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "reset" and ".unreadable-" in payload["kept"]
    assert not ctx.paths.secrets_file.exists()


def test_delete_all_reports_locked_store_and_still_deletes(ctx, db, file_backend, monkeypatch):
    SecretStore(ctx.paths).set("smtp.password", "token-123")
    (ctx.paths.documents_dir).mkdir(parents=True, exist_ok=True)
    (ctx.paths.documents_dir / "cv.txt").write_text("cv")
    monkeypatch.setenv(secrets_mod.ENV_PASSPHRASE, "wrong")
    res = datamgmt.delete_all(ctx, db, "DELETE")
    assert res["deleted"]["secrets"] == 0
    assert any("secret store" in p.lower() for p in res["problems"])
    assert not ctx.paths.documents_dir.exists()
    assert not ctx.paths.db_path.exists()
    assert not ctx.paths.secrets_file.exists()


def test_connections_page_shows_secret_store_problem(ctx, db, file_backend, monkeypatch):
    from fastapi.testclient import TestClient

    from jobhunter.web.app import create_app

    SecretStore(ctx.paths).set("smtp.password", "token-123")
    db.close()
    monkeypatch.setenv(secrets_mod.ENV_PASSPHRASE, "wrong")
    client = TestClient(create_app(ctx), base_url="http://127.0.0.1:8765")
    r = client.get("/connections")
    assert r.status_code == 200
    assert "cannot read its secret store" in r.text
