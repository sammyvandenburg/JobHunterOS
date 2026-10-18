"""Email setup in a real browser: Connect Gmail -> confirm the address -> test email arrives -> choose daily email ->
enable automatic hunting -> restart Hunter -> the scheduled digest arrives -> disconnect. Fake Google on localhost."""

from __future__ import annotations

import re
import time
from datetime import timedelta

import pytest

from jobhunter.config import AppContext
from jobhunter.db import connect
from jobhunter.services import worker
from jobhunter.util import now
from tests.conftest import approve_profile, launch_browser, serve_app, stop_app

playwright = pytest.importorskip("playwright.sync_api")


def _wait(pred, timeout=20.0, what="condition"):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for {what}")


def test_email_setup_journey_in_a_browser(home, google, fake_sources, monkeypatch):
    from playwright.sync_api import Error as PwError
    from playwright.sync_api import expect, sync_playwright

    # The login task itself is faked (the real one would call launchctl/systemctl on this machine);
    # the dashboard runs in this process, so the fake is what it calls.
    monkeypatch.setenv("JOBHUNTER_AUTORUN_TICK", "0.3")
    task = {"installed": False}
    monkeypatch.setattr(worker, "install_service", lambda c, runner=None: (task.update(installed=True), {"installed": True, "note": "Hunter now runs a background search service while you are logged in."})[-1])
    monkeypatch.setattr(worker, "uninstall_service", lambda c, runner=None: (task.update(installed=False), {"removed": True, "note": "The background task is gone."})[-1])
    monkeypatch.setattr(worker, "service_status", lambda c, runner=None: {"platform": "Linux", "installed": task["installed"], "active": task["installed"], "path": "/fake/jobhunter-worker.service", "supported": True})

    ctx = AppContext.from_env(home=home)
    d = connect(ctx.paths.db_path)
    approve_profile(d, ctx)
    d.close()
    srv, t, base = serve_app(ctx)
    with sync_playwright() as p:
        try:
            browser = launch_browser(p)
        except PwError as exc:
            stop_app(srv, t)
            pytest.skip(f"no browser available: {exc}")
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("dialog", lambda dlg: dlg.accept())

        # 1. Connect Gmail: one click, Google's screen (faked), straight back into Hunter.
        page.goto(base + "/connections")
        chips = page.locator(".status-chips")
        expect(chips).to_contain_text("Gmail not connected")
        page.get_by_role("button", name="Connect Gmail", exact=True).click()
        page.wait_for_url(re.compile(r"/connections\?msg="))
        assert "Gmail connected as student@gmail.example" in page.content()
        expect(chips).to_contain_text("Connected: student@gmail.example")
        assert google.auth_requests[0]["scope"] == "https://www.googleapis.com/auth/gmail.send"
        # 2. Confirm the detected address, then the test email.
        page.get_by_role("button", name="Yes, send them there").click()
        page.wait_for_url(re.compile(r"msg="))
        expect(chips).to_contain_text("Sending enabled")
        page.get_by_role("button", name="Send test email").click()
        page.wait_for_url(re.compile(r"msg="))
        assert "Test email sent to student@gmail.example" in page.content()
        _wait(lambda: len(google.sent) == 1, what="test email")
        assert google.sent[0]["subject"] == "Hunter test: your job emails work" and google.sent[0]["to"] == "student@gmail.example"
        # 3. Daily, with email; then allow automatic hunting.
        page.locator("input[name=plan][value=daily]").check()
        page.locator("input[name=time_of_day]").fill("07:30")
        page.locator("input[name=email]").check()
        page.locator("form.prefs-form button.primary").click()
        page.wait_for_url(re.compile(r"msg="))
        assert "look every day at 07:30" in page.content() and "one email a day at 08:30" in page.content()
        expect(chips).to_contain_text("Automatic hunting: running")
        # Keeping it running when Hunter is closed needs the explicit tick.
        page.locator("input[name=approve]").check()
        page.get_by_role("button", name="Keep hunting when Hunter is closed").click()
        page.wait_for_url(re.compile(r"msg="))
        assert task["installed"] is True and "Background task: installed and running" in page.content()
        # 4. Restart Hunter: still connected, still automatic, no questions asked again.
        stop_app(srv, t)
        srv, t, base = serve_app(ctx)
        page.goto(base + "/connections")
        chips = page.locator(".status-chips")
        expect(chips).to_contain_text("Connected: student@gmail.example")
        expect(chips).to_contain_text("Sending enabled")
        expect(chips).to_contain_text("Automatic hunting: running")
        assert "Background task: installed" in page.content()
        # 5. The scheduled run comes due: the in-app scheduler hunts (synthetic source) and emails the digest once.
        d = connect(ctx.paths.db_path)
        d.execute("UPDATE schedule SET next_run_at = ?, digest_time = '00:00', last_digest_at = NULL WHERE id = 1", ((now() - timedelta(minutes=1)).isoformat(),))
        d.close()
        _wait(lambda: len(google.sent) == 2, timeout=40, what="scheduled digest")
        digest = google.sent[1]
        assert digest["to"] == "student@gmail.example" and "Northwind Cloud" in digest["text"]
        time.sleep(1.0)  # a few more ticks: nothing is sent twice
        assert len(google.sent) == 2
        page.goto(base + "/connections")
        history = page.locator("#email")
        expect(history).to_contain_text("Hunter test: your job emails work")
        d = connect(ctx.paths.db_path)
        rows = d.fetchall("SELECT kind, status FROM notifications ORDER BY id")
        d.close()
        assert rows == [{"kind": "test", "status": "sent"}, {"kind": "digest", "status": "sent"}]
        # 6. Disconnect from the dashboard: permission gone at Google, history kept, button back.
        page.get_by_role("button", name="Disconnect Gmail").click()
        page.wait_for_url(re.compile(r"msg="))
        assert len(google.revoked) == 1
        expect(page.locator(".status-chips")).to_contain_text("Gmail not connected")
        assert page.get_by_role("button", name="Connect Gmail", exact=True).count() == 1
        page.get_by_role("button", name="Turn off automatic hunting").click()
        page.wait_for_url(re.compile(r"msg="))
        expect(page.locator(".status-chips")).to_contain_text("Automatic hunting: off")
        assert task["installed"] is False and "Background task" not in page.content()
        assert errors == [], errors
        browser.close()
    stop_app(srv, t)
