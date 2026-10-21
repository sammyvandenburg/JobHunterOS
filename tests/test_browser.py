"""Browser smoke test with Playwright (skipped when Playwright or a browser is unavailable).

Set JOBHUNTER_CHROMIUM=/path/to/chrome when the Playwright-managed browser is missing.
"""

from __future__ import annotations

import os
import socket
import threading
import time

import pytest

from jobhunter.config import AppContext
from jobhunter.db import connect

playwright = pytest.importorskip("playwright.sync_api")


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def server(home):
    import uvicorn

    from jobhunter.services import demo
    from jobhunter.web.app import create_app

    ctx = AppContext.from_env(demo=True, home=home)
    d = connect(ctx.paths.db_path)
    demo.seed(ctx, d)
    d.close()
    port = _free_port()
    config = uvicorn.Config(create_app(ctx), host="127.0.0.1", port=port, log_level="error")
    srv = uvicorn.Server(config)
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    for _ in range(50):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    t.join(timeout=5)


def _browser(p):
    from playwright.sync_api import Error as PwError

    exe = os.environ.get("JOBHUNTER_CHROMIUM")
    candidates = [exe] if exe else [None]
    bundled = os.path.join(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", ""), "chromium")
    if not exe and os.environ.get("PLAYWRIGHT_BROWSERS_PATH") and os.access(bundled, os.X_OK) and os.path.isfile(bundled):
        candidates.append(bundled)  # a pre-installed Chromium next to the Playwright-managed ones (cloud containers)
    last: Exception | None = None
    for cand in candidates:
        try:
            return p.chromium.launch(**({"executable_path": cand} if cand else {}))
        except PwError as exc:
            last = exc
    pytest.skip(f"no browser available: {last}")


def test_browser_smoke(server):
    from playwright.sync_api import expect, sync_playwright

    with sync_playwright() as p:
        browser = _browser(p)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(server + "/")
        assert "Demo mode" in page.content()
        assert page.locator("h1").first.inner_text().strip() == "Your job hunt, all in one place."
        # Sidebar navigation and the four metric cards exist and link somewhere real.
        nav = page.locator("nav.nav")
        for label in ("Dashboard", "New jobs", "Favorites", "Saved", "Applied", "Finished", "Needs attention", "Profile & settings"):
            expect(nav.get_by_role("link", name=label).first).to_be_visible()
        assert page.locator("a.metric").count() == 4
        page.locator("a.metric").nth(1).click()
        assert page.url.endswith("/jobs/new")
        assert page.locator("h1").first.inner_text().strip() == "New jobs"
        # Favorite toggles without a page reload, updates the sidebar count, and is independent of Saved.
        # Pick a card the demo seed did not pre-save, so "independent of Saved" is a real check.
        first = page.locator("ul.jobs li.job").filter(has=page.locator("button.bm[aria-pressed='false']")).first
        href = first.locator(".title a").get_attribute("href")
        fav = first.locator("button.fav")
        assert fav.get_attribute("aria-pressed") == "false"
        fav.click()
        expect(fav).to_have_attribute("aria-pressed", "true")
        expect(page.locator("#toast")).to_contain_text("Added to favorites.")
        expect(nav.locator("[data-count='favorites']")).to_have_text("1")
        # Off again without a reload, then on again, then reload: the state and the count must follow.
        fav.click()
        expect(fav).to_have_attribute("aria-pressed", "false")
        expect(page.locator("#toast")).to_contain_text("Removed from favorites.")
        expect(nav.locator("[data-count='favorites']")).to_have_text("0")
        fav.click()
        expect(fav).to_have_attribute("aria-pressed", "true")
        page.reload()
        first = page.locator("ul.jobs li.job").filter(has=page.locator("a[href='" + href + "']")).first
        assert first.locator("button.fav").get_attribute("aria-pressed") == "true"
        assert first.locator("button.bm").get_attribute("aria-pressed") == "false"
        bm = first.locator("button.bm")
        bm.click()
        expect(bm).to_have_attribute("aria-pressed", "true")
        bm.click()
        expect(bm).to_have_attribute("aria-pressed", "false")
        page.goto(server + "/jobs/favorites")
        assert page.locator("ul.jobs li.job").count() == 1
        assert page.locator("ul.jobs li.job .title a").first.get_attribute("href") == href
        page.goto(server + "/jobs/saved")
        assert href not in [a.get_attribute("href") for a in page.locator("ul.jobs li.job .title a").all()]
        # Job detail: direct link, Open application link, notes add/edit/delete.
        page.goto(server + "/jobs/new")
        page.locator("ul.jobs li.job .title a").nth(1).click()
        assert "/job/" in page.url
        job_url = page.url
        open_link = page.get_by_role("link", name="Open application").first
        assert open_link.get_attribute("href").startswith("/go/")
        page.locator("form[data-note-form='new'] textarea").fill("Ask about on-call rota.")
        page.locator("form[data-note-form='new'] button").click()
        page.wait_for_url(lambda u: "msg=" in u)
        assert page.locator(".notes .note").count() == 1
        assert "Ask about on-call rota." in page.locator(".notes .note textarea").first.input_value()
        page.locator(".notes .note textarea").first.fill("Ask about on-call rota and budget.")
        page.locator(".notes .note form[data-note-form='edit'] button").first.click()
        expect(page.locator(".notes .note .save-state").first).to_contain_text("Saved")
        page.goto(job_url)
        assert "on-call rota and budget" in page.locator(".notes .note textarea").first.input_value()
        page.once("dialog", lambda d: d.accept())
        page.locator(".notes .note form[action$='/delete'] button").first.click()
        page.wait_for_url(lambda u: "deleted" in u)
        assert page.locator(".notes .note").count() == 0
        # Applied page and health page render.
        page.goto(server + "/jobs/applied")
        assert page.locator("h1").first.inner_text().strip() == "Applied"
        assert page.locator("table.table tbody tr").count() >= 1
        page.goto(server + "/health")
        assert "Sources" in page.content()
        # Keyboard access: the first Tab lands on an interactive element.
        page.goto(server + "/")
        page.keyboard.press("Tab")
        assert page.evaluate("document.activeElement.tagName") in {"A", "BUTTON", "INPUT", "SELECT"}
        # Mobile layout: the sidebar is hidden behind a menu button and the page does not scroll sideways.
        page.set_viewport_size({"width": 390, "height": 800})
        for path in ("/", "/jobs/new", job_url.replace(server, ""), "/jobs/applied", "/profile", "/connections", "/health"):
            page.goto(server + path)
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1"), path
        page.goto(server + "/")
        assert page.locator("[data-toggle-sidebar]").first.is_visible()
        assert errors == [], errors
        browser.close()
