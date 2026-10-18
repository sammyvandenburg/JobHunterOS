"""Browser smoke test with Playwright (skipped when Playwright or a browser is unavailable)."""

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


def test_browser_smoke(server):
    from playwright.sync_api import Error as PwError
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        try:
            kwargs = {}
            exe = os.environ.get("JOBHUNTER_CHROMIUM")
            if exe:
                kwargs["executable_path"] = exe
            browser = p.chromium.launch(**kwargs)
        except PwError as exc:
            pytest.skip(f"no browser available: {exc}")
        page = browser.new_page()
        page.goto(server + "/")
        assert "Demo mode" in page.content()
        assert page.locator("h1").first.inner_text().strip() == "New jobs"
        page.get_by_role("link", name="Needs attention").first.click()
        assert "attention" in page.url
        page.goto(server + "/")
        page.locator("ul.jobs li h2 a").nth(1).click()  # second job: not pre-saved by the demo seed
        assert "/job/" in page.url
        page.locator("form[action$='/save'] button").first.click()
        assert "Saved." in page.content()
        page.goto(server + "/jobs/saved")
        assert page.locator("ul.jobs li").count() >= 1
        page.goto(server + "/health")
        assert "Sources" in page.content()
        # Keyboard access: the primary action can be reached with Tab.
        page.goto(server + "/")
        page.keyboard.press("Tab")
        assert page.evaluate("document.activeElement.tagName") in {"A", "BUTTON", "INPUT", "SELECT"}
        browser.close()
