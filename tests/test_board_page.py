"""The dashboard page's own JavaScript, driven in a real Chromium against a simulated artifact runtime
(tests/fake_artifact.py). Simulated owner, not a signed-in claude.ai session: these tests prove the page's
editing behaviour (drafts, saves, revisions, retries, two tabs, leave warnings), not the service."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from jobhunter.services import board
from tests.conftest import approve_profile
from tests.fake_artifact import SHIM, FakeArtifact
from tests.test_board import _key_for, _seed_jobs
from tests.test_browser import _browser

playwright = pytest.importorskip("playwright.sync_api")


@pytest.fixture
def site(ctx, db, tmp_path):
    approve_profile(db, ctx, countries="Netherlands")
    _seed_jobs(ctx, db)
    board.link(db, "https://claude.ai/artifact/5wUUffKaZnmpqGgPnRzPVE")
    board.import_actions(ctx, db, tmp_path / "none", listed=0)
    manifest = board.export(ctx, db, tmp_path / "out")
    fake = FakeArtifact()
    fake.load_export(manifest)
    url = fake.start()
    yield {"fake": fake, "url": url, "nw": _key_for(db, "Northwind Cloud"), "manifest": manifest}
    fake.stop()


def _open(browser, url: str):
    context = browser.new_context(viewport={"width": 1200, "height": 900})
    context.add_init_script(SHIM)
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(url + "/")
    page.wait_for_selector("button.row")
    return context, page, errors


def _open_job(page, key: str):
    page.locator(f"button.row[data-key='{key}']").first.click()
    page.wait_for_selector("textarea#note")


def _wait(fn, timeout=5.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if fn():
            return True
        time.sleep(0.05)
    return False


def test_typed_note_survives_snapshots_and_saves_with_growing_revisions(site):
    from playwright.sync_api import sync_playwright

    fake, nw = site["fake"], site["nw"]
    with sync_playwright() as p:
        browser = _browser(p)
        context, page, errors = _open(browser, site["url"])
        _open_job(page, nw)
        ta = page.locator("textarea#note")
        ta.fill("Ask about on-c")
        ta.press("End")
        # Hunter (or another device) writes meanwhile: the page re-renders, the draft and the caret stay.
        run = fake.doc("meta/run")
        run["at"] = "2026-10-21T10:00:00Z"
        fake.load({"meta/run": run})
        time.sleep(0.5)
        assert ta.input_value() == "Ask about on-c"
        assert page.evaluate("document.activeElement && document.activeElement.id") == "note"
        ta.type("all")
        page.get_by_role("button", name="Save note").click()
        assert _wait(lambda: ((fake.doc(f"actions/{nw}") or {}).get("note") or {}).get("text") == "Ask about on-call")
        doc = fake.doc(f"actions/{nw}")
        assert doc["rev"] == 1 and doc["note"]["rev"] == 1
        status = page.locator(".save[role=status]")
        assert "Saved to your dashboard" in status.inner_text() and "Waiting for Hunter" in status.inner_text()
        # A second edit continues the revision, never resets it.
        ta.fill("Ask about on-call and pay")
        page.get_by_role("button", name="Save note").click()
        assert _wait(lambda: (fake.doc(f"actions/{nw}") or {}).get("rev") == 2)
        doc = fake.doc(f"actions/{nw}")
        assert doc["note"]["rev"] == 2 and doc["note"]["text"] == "Ask about on-call and pay"
        # Hunter applies it (synced_rev 2 on the job): the page says so.
        shard = fake.doc("board/jobs-0")
        shard["jobs"][nw]["state"]["synced_rev"] = 2
        shard["jobs"][nw]["state"]["note"] = "Ask about on-call and pay"
        fake.load({"board/jobs-0": shard})
        assert _wait(lambda: "Synced" in page.locator(".save[role=status]").inner_text())
        assert not errors, errors
        context.close()
        browser.close()


def test_a_failed_save_offers_retry_and_keeps_the_change_on_screen(site):
    from playwright.sync_api import sync_playwright

    fake, nw = site["fake"], site["nw"]
    with sync_playwright() as p:
        browser = _browser(p)
        context, page, errors = _open(browser, site["url"])
        _open_job(page, nw)
        fake.fail_set = True
        page.get_by_role("dialog").get_by_role("button", name="☆ Favorite").click()
        status = page.locator(".save[role=status]")
        assert _wait(lambda: "Couldn't save" in status.inner_text())
        assert fake.doc(f"actions/{nw}") is None or not fake.doc(f"actions/{nw}").get("favorite")
        retry = page.get_by_role("button", name="Retry")
        assert retry.is_visible()
        fake.fail_set = False
        retry.click()
        assert _wait(lambda: (fake.doc(f"actions/{nw}") or {}).get("favorite") is True)
        assert _wait(lambda: "Saved to your dashboard" in status.inner_text())
        assert _wait(lambda: page.get_by_role("dialog").get_by_role("button", name="★ Favorite").count() == 1)
        assert not errors, errors
        context.close()
        browser.close()


def test_two_tabs_never_overwrite_each_other(site):
    from playwright.sync_api import sync_playwright

    fake, nw = site["fake"], site["nw"]
    with sync_playwright() as p:
        browser = _browser(p)
        ca, a, ea = _open(browser, site["url"])
        cb, b, eb = _open(browser, site["url"])
        _open_job(a, nw)
        _open_job(b, nw)
        a.locator("textarea#note").fill("from A")
        b.locator("textarea#note").fill("from B")  # B started editing before A saved
        a.get_by_role("button", name="Save note").click()
        assert _wait(lambda: ((fake.doc(f"actions/{nw}") or {}).get("note") or {}).get("text") == "from A")
        b.get_by_role("button", name="Save note").click()
        assert _wait(lambda: b.locator(".conflict[role=alert]").count() == 1, timeout=12)  # waits out A's short lease
        assert fake.doc(f"actions/{nw}")["note"]["text"] == "from A"  # nothing overwritten
        assert b.locator("textarea#note").input_value() == "from B"
        b.get_by_role("button", name="Keep mine").click()
        assert _wait(lambda: ((fake.doc(f"actions/{nw}") or {}).get("note") or {}).get("text") == "from B")
        assert fake.doc(f"actions/{nw}")["note"]["rev"] == 2 and fake.doc(f"actions/{nw}")["rev"] == 2
        # Tab A sees B's note arrive without losing anything it was not editing.
        assert _wait(lambda: a.locator("textarea#note").input_value() == "from B")
        assert not ea and not eb, (ea, eb)
        ca.close()
        cb.close()
        browser.close()


def test_an_unsaved_note_warns_before_leaving(site):
    from playwright.sync_api import sync_playwright

    nw = site["nw"]
    with sync_playwright() as p:
        browser = _browser(p)
        context, page, errors = _open(browser, site["url"])
        _open_job(page, nw)
        js = "() => { const ev = new Event('beforeunload', { cancelable: true }); window.dispatchEvent(ev); return ev.defaultPrevented; }"
        assert page.evaluate(js) is False
        page.locator("textarea#note").fill("not saved yet")
        assert page.evaluate(js) is True
        page.get_by_role("button", name="Save note").click()
        assert _wait(lambda: page.evaluate(js) is False)
        assert not errors, errors
        context.close()
        browser.close()


def test_page_actions_round_trip_into_hunter(site, ctx, db, tmp_path):
    """What the page wrote is exactly what `board import` reads back: the whole loop, page JavaScript included."""
    from playwright.sync_api import sync_playwright

    fake, nw = site["fake"], site["nw"]
    with sync_playwright() as p:
        browser = _browser(p)
        context, page, errors = _open(browser, site["url"])
        _open_job(page, nw)
        page.get_by_role("dialog").get_by_role("button", name="☆ Favorite").click()
        assert _wait(lambda: (fake.doc(f"actions/{nw}") or {}).get("favorite") is True)
        page.locator("textarea#note").fill("Round trip")
        page.get_by_role("button", name="Save note").click()
        assert _wait(lambda: (fake.doc(f"actions/{nw}") or {}).get("rev") == 2)
        context.close()
        browser.close()
    n = fake.write_actions_dir(tmp_path / "in")
    res = board.import_actions(ctx, db, tmp_path / "in", listed=n)
    assert {a["job_key"]: a["changes"] for a in res["applied"]} == {nw: ["favorite", "note"]}
    manifest = board.export(ctx, db, tmp_path / "out2")
    shard = json.loads(Path(manifest["writes"][1]["file"]).read_text())["jobs"]
    assert shard[nw]["state"]["favorited"] and shard[nw]["state"]["note"] == "Round trip" and shard[nw]["state"]["synced_rev"] == 2


def test_a_note_can_be_edited_again_after_hunter_synced_it(site):
    """Save a note, let Hunter sync it (synced_rev = the action's rev), edit it again: no false conflict."""
    from playwright.sync_api import sync_playwright

    fake, nw = site["fake"], site["nw"]
    with sync_playwright() as p:
        browser = _browser(p)
        context, page, errors = _open(browser, site["url"])
        _open_job(page, nw)
        page.locator("textarea#note").fill("First version")
        page.get_by_role("button", name="Save note").click()
        assert _wait(lambda: (fake.doc(f"actions/{nw}") or {}).get("rev") == 1)
        # Hunter applies it and writes the page: the job's state now carries the note and synced_rev 1.
        shard = fake.doc("board/jobs-0")
        shard["jobs"][nw]["state"]["synced_rev"] = 1
        shard["jobs"][nw]["state"]["note"] = "First version"
        fake.load({"board/jobs-0": shard})
        assert _wait(lambda: "Synced" in page.locator(".save[role=status]").inner_text())
        page.locator("textarea#note").fill("Second version")
        page.get_by_role("button", name="Save note").click()
        assert _wait(lambda: (fake.doc(f"actions/{nw}") or {}).get("rev") == 2), fake.doc(f"actions/{nw}")
        assert page.locator(".conflict[role=alert]").count() == 0
        doc = fake.doc(f"actions/{nw}")
        assert doc["note"]["text"] == "Second version" and doc["note"]["rev"] == 2
        assert not errors, errors
        context.close()
        browser.close()


def test_unsaved_preference_edits_survive_a_snapshot(site):
    from playwright.sync_api import sync_playwright

    fake = site["fake"]
    with sync_playwright() as p:
        browser = _browser(p)
        context, page, errors = _open(browser, site["url"])
        page.get_by_role("button", name="Profile", exact=True).click()
        page.wait_for_selector("input#pf-salary")
        page.locator("input#pf-salary").fill("75000 EUR a year")
        page.locator("input#pf-location").fill("Groningen, Netherlands")
        run = fake.doc("meta/run")
        run["at"] = "2026-10-21T11:00:00Z"
        fake.load({"meta/run": run})
        time.sleep(0.5)
        assert page.locator("input#pf-salary").input_value() == "75000 EUR a year"
        assert page.locator("input#pf-location").input_value() == "Groningen, Netherlands"
        page.locator("#sec-prefs").get_by_role("button", name="Save", exact=True).click()
        assert _wait(lambda: (fake.doc("actions/_prefs") or {}).get("salary") == "75000 EUR a year")
        assert not errors, errors
        context.close()
        browser.close()
