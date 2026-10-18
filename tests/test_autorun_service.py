"""Automatic hunting: the in-app scheduler, its status words, and the optional login task
(launchd / systemd user unit / Windows task) that is only ever installed after explicit approval."""

from __future__ import annotations

import re
import subprocess
from types import SimpleNamespace
from urllib.parse import unquote

from fastapi.testclient import TestClient

from jobhunter.services import autorun, schedule, worker
from jobhunter.services.locks import FileLock
from jobhunter.web.app import create_app

H = {"origin": "http://127.0.0.1:8765"}


class Runner:
    """Stands in for subprocess.run: records every command, answers what the test says."""

    def __init__(self, answers=None):
        self.calls: list[list[str]] = []
        self.answers = answers or {}

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        code, out = self.answers.get(argv[0], (0, ""))
        if callable(code):
            code, out = code(argv)
        return SimpleNamespace(returncode=code, stdout=out, stderr="")


def _fake_os(monkeypatch, tmp_path, system):
    monkeypatch.setattr(worker.platform, "system", lambda: system)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(worker, "_which", lambda name: f"/usr/bin/{name}")
    if system == "Darwin":
        monkeypatch.setattr(worker.os, "getuid", lambda: 501, raising=False)


def test_install_and_remove_linux_user_service(ctx, monkeypatch, tmp_path):
    _fake_os(monkeypatch, tmp_path, "Linux")
    r = Runner({"systemctl": (lambda argv: (0, "inactive") if "is-active" in argv else (0, ""), "")})
    assert worker.service_status(ctx, r)["installed"] is False
    res = worker.install_service(ctx, r)
    assert res["installed"] is True and "background search service" in res["note"]
    unit = tmp_path / "home" / ".config" / "systemd" / "user" / "jobhunter-worker.service"
    text = unit.read_text()
    assert unit.exists() and "worker run" in text and f"JOBHUNTER_HOME={ctx.paths.home}" in text and not text.startswith("#")
    assert r.calls == [["systemctl", "--user", "daemon-reload"], ["systemctl", "--user", "enable", "--now", "jobhunter-worker"]]
    st = worker.service_status(ctx, r)
    assert st["installed"] is True and st["active"] is False and st["supported"] is True
    res = worker.uninstall_service(ctx, r)
    assert res["file_removed"] is True and not unit.exists()
    assert ["systemctl", "--user", "disable", "--now", "jobhunter-worker"] in r.calls and r.calls[-1] == ["systemctl", "--user", "daemon-reload"]


def test_install_fails_in_plain_words_when_the_os_refuses(ctx, monkeypatch, tmp_path):
    _fake_os(monkeypatch, tmp_path, "Linux")
    r = Runner({"systemctl": (1, "Failed to connect to bus")})
    res = worker.install_service(ctx, r)
    assert res["installed"] is False and res["note"].startswith("Linux did not accept the service.") and "bus" in res["note"]
    monkeypatch.setattr(worker, "_which", lambda name: None)
    res = worker.install_service(ctx, Runner())
    assert res["installed"] is False and "Keep Hunter open" in res["note"]
    assert worker.service_status(ctx, Runner())["supported"] is False


def test_install_and_remove_macos_login_task(ctx, monkeypatch, tmp_path):
    _fake_os(monkeypatch, tmp_path, "Darwin")
    r = Runner()
    res = worker.install_service(ctx, r)
    plist = tmp_path / "home" / "Library" / "LaunchAgents" / "com.kubecraft.jobhunter.worker.plist"
    assert res["installed"] is True and plist.exists() and plist.read_text().startswith("<?xml")
    assert r.calls == [["launchctl", "bootstrap", "gui/501", str(plist)]]
    # Already loaded: launchd says so, Hunter kickstarts instead of failing.
    r2 = Runner({"launchctl": (lambda argv: (37, "Bootstrap failed: 37: Operation already in progress") if argv[1] == "bootstrap" else (0, ""), "")})
    assert worker.install_service(ctx, r2)["installed"] is True and r2.calls[-1][:2] == ["launchctl", "kickstart"]
    assert worker.service_status(ctx, Runner())["active"] is True
    res = worker.uninstall_service(ctx, r)
    assert res["file_removed"] is True and not plist.exists() and r.calls[-1] == ["launchctl", "bootout", "gui/501/com.kubecraft.jobhunter.worker"]


def test_install_and_remove_windows_task(ctx, monkeypatch, tmp_path):
    _fake_os(monkeypatch, tmp_path, "Windows")
    r = Runner({"schtasks": (lambda argv: (0, "Status: Running") if "/Query" in argv else (0, ""), "")})
    res = worker.install_service(ctx, r)
    assert res["installed"] is True and r.calls[0][:3] == ["schtasks", "/Create", "/TN"] and "ONLOGON" in r.calls[0] and r.calls[1][:2] == ["schtasks", "/Run"]
    assert "worker run" in r.calls[0][r.calls[0].index("/TR") + 1]
    assert worker.service_status(ctx, r)["installed"] is True
    worker.uninstall_service(ctx, r)
    assert r.calls[-1] == ["schtasks", "/Delete", "/TN", "JobHunterOS worker", "/F"]


def test_missing_tool_is_reported_not_raised(ctx, monkeypatch, tmp_path):
    _fake_os(monkeypatch, tmp_path, "Linux")

    def missing(argv, **kw):
        raise FileNotFoundError(argv[0])

    assert "not installed" in worker.install_service(ctx, missing)["note"]

    def slow(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 60)

    assert "timed out" in worker.install_service(ctx, slow)["note"]


def test_autorun_status_words_and_tick_rules(ctx, db, monkeypatch, tmp_path):
    _fake_os(monkeypatch, tmp_path, "Linux")
    monkeypatch.setattr(worker, "service_status", lambda c, runner=None: {"platform": "Linux", "installed": False, "active": None, "path": None, "supported": True})
    a = autorun.Autorun(ctx, tick_seconds=3600)
    assert autorun.status(ctx, db, a)["label"] == "off"
    assert a.tick_once() == {"skipped": "schedule off"}
    schedule.set_frequency(db, "every_2h")
    schedule.enable(db)
    assert autorun.status(ctx, db, a)["label"] == "on, but nothing is running it"
    a.start()
    assert a.running and autorun.status(ctx, db, a)["label"] == "running"
    a.start()  # idempotent
    a.stop()
    assert not a.running
    # A heartbeat from a separate worker process counts as running too.
    worker._beat(db, "running", "worker loop")
    assert autorun.status(ctx, db, None)["detail"].startswith("Running in the background")
    db.execute("DELETE FROM worker_heartbeat")
    monkeypatch.setattr(worker, "service_status", lambda c, runner=None: {"platform": "Linux", "installed": True, "active": False, "path": "/x", "supported": True})
    assert autorun.status(ctx, db, None)["label"] == "installed, waiting"
    # Another worker holds the lock: the in-app tick steps aside instead of running a second hunt.
    lock = FileLock(ctx.paths.locks_dir / "worker.lock")
    lock.acquire()
    try:
        assert a.tick_once() == {"skipped": "a separate worker is running"}
    finally:
        lock.release()
    res = a.tick_once()
    assert res["ran_hunt"] is False and res["decision"]["reason"] in {"next run planned", "not due yet"}
    schedule.disable(db)
    assert a.tick_once() == {"skipped": "schedule off"}


def test_dashboard_turns_automatic_hunting_on_and_off_with_approval(ctx, db, monkeypatch):
    installed = {"on": False}
    calls: list[str] = []
    monkeypatch.setattr(worker, "install_service", lambda c, runner=None: (calls.append("install"), installed.update(on=True), {"installed": True, "note": "Task installed (fake)."})[-1])
    monkeypatch.setattr(worker, "uninstall_service", lambda c, runner=None: (calls.append("remove"), installed.update(on=False), {"removed": True, "note": "Task removed (fake)."})[-1])
    monkeypatch.setattr(worker, "service_status", lambda c, runner=None: {"platform": "Linux", "installed": installed["on"], "active": installed["on"], "path": "/fake", "supported": True})
    c = TestClient(create_app(ctx), base_url="http://127.0.0.1:8765")
    csrf = re.search(r'name="csrf" value="([0-9a-f]+)"', c.get("/connections").text).group(1)
    assert not c.app.state.autorun.running

    def post(**data):
        r = c.post("/do/schedule", data={"csrf": csrf, **data}, headers=H, follow_redirects=False)
        return unquote(r.headers["location"])

    # No tick in the box: nothing changes.
    assert "Tick the box" in post(action="automatic_on")
    assert not schedule.get(db)["enabled"] and not c.app.state.autorun.running and calls == []
    # Approved, inside Hunter only.
    assert "Automatic hunting is on" in post(action="automatic_on", approve="1")
    assert schedule.get(db)["enabled"] and schedule.get(db)["frequency"] == "daily" and c.app.state.autorun.running and calls == []
    page = c.get("/connections").text
    assert "Automatic hunting: running" in page and "Turn off automatic hunting" in page
    # The login task needs its own tick.
    assert "Tick the box" in post(action="service_install")
    assert calls == []
    assert "fake" in post(action="service_install", approve="1")
    assert calls == ["install"] and "Background task: installed" in c.get("/connections").text
    # Off removes both.
    loc = post(action="automatic_off")
    assert "Automatic hunting is off" in loc and "Task removed" in loc
    assert not schedule.get(db)["enabled"] and not c.app.state.autorun.running and calls == ["install", "remove"]
    assert "Automatic hunting: off" in c.get("/connections").text
    # Approve + persist in one go.
    assert "Task installed" in post(action="automatic_on", approve="1", persist="1")
    assert calls == ["install", "remove", "install"] and c.app.state.autorun.running
    c.app.state.autorun.stop()
