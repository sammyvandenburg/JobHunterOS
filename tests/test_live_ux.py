"""Click -> Hunter starts working -> the result appears. The dashboard and the running Claude Code session behave as
one application: a click stores the request, hands it to the bound session at once, the page shows what Hunter is
doing and then the result. Chromium against the simulated artifact runtime with a stubbed connector: this proves
the page's behaviour and the hand-off files, never the claude.ai service or a real session."""

from __future__ import annotations

import base64

import pytest

from jobhunter.services import board, hunt, logos, workspace
from tests.conftest import approve_profile
from tests.test_board import _key_for
from tests.test_browser import _browser
from tests.test_workspace_page import MCP_STUB, _open, _open_job, _sync, _wait
from tests.test_workspace_page import site as site_fixture

playwright = pytest.importorskip("playwright.sync_api")
site = site_fixture  # the same synthetic dashboard as the workspace tests

SESSION = "session_01LIVE"
FORBIDDEN = ("next run", "Continue in Claude", "Continue application", "Continue this request", "Direct line", "direct line", "Waiting for Hunter", "Sent to Hunter", "request id", "queue", "MCP", "session id", "trigger", "worker", "sync revision")


def _bind(ctx, db, site):
    board.set_channel(db, "session", SESSION, label="Hunter session")
    _sync(ctx, db, site, 0)
    assert site["fake"].doc("meta/run")["hunter"]["channel"] == {**site["fake"].doc("meta/run")["hunter"]["channel"], "kind": "session", "id": SESSION}


def _no_plumbing(text: str) -> None:
    for bad in FORBIDDEN:
        assert bad not in text, bad


def test_research_company_reaches_the_running_session_at_once_and_the_result_appears(site, ctx, db):
    from playwright.sync_api import sync_playwright

    fake, nw = site["fake"], site["nw"]
    _bind(ctx, db, site)
    with sync_playwright() as p:
        browser = _browser(p)
        context, page, errors = _open(browser, site["url"], extra_script=MCP_STUB)
        assert _wait(lambda: "Hunter online" in page.locator("#listCol").inner_text())
        _open_job(page, nw)
        dialog = page.get_by_role("dialog")
        dialog.locator(".help").get_by_role("button", name="Research company").click()
        # Stored durably first, then handed to the bound session immediately with the request id, nothing else.
        assert _wait(lambda: (((fake.doc(f"actions/{nw}") or {}).get("requests") or {}).get("research") or {}).get("id", "").startswith("research:"))
        req_id = fake.doc(f"actions/{nw}")["requests"]["research"]["id"]
        assert _wait(lambda: page.evaluate("window.__mcpCalls.length") == 1)
        assert page.evaluate("window.__mcpCalls") == [{"server": "Claude Code Remote", "tool": "send_message", "input": {"session_id": SESSION, "message": "/jobhunter continue " + req_id, "priority": "next"}}]
        assert _wait(lambda: "Researching…" in dialog.locator(".help").inner_text())
        _no_plumbing(dialog.inner_text())
        assert req_id not in dialog.inner_text()
        # Hunter does the work and writes the job's work document: the page shows the result.
        res, done, _ = _sync(ctx, db, site, 1)
        assert done[0]["request_id"] == req_id and done[0]["state"] == "ready"
        assert _wait(lambda: dialog.locator(".help").get_by_role("button", name="View research").count() == 1)
        assert "Researching…" not in dialog.inner_text()
        dialog.locator(".help").get_by_role("button", name="View research").click()
        assert _wait(lambda: "Company and role brief" in dialog.inner_text() and "The vacancy says" in dialog.inner_text())
        _no_plumbing(dialog.inner_text())
        assert not errors, errors
        context.close()
        browser.close()


def test_without_a_running_session_the_page_says_hunter_is_offline_keeps_the_request_and_offers_retry(site, ctx, db):
    from playwright.sync_api import sync_playwright

    fake, nw = site["fake"], site["nw"]
    with sync_playwright() as p:
        browser = _browser(p)
        context, page, errors = _open(browser, site["url"], extra_script=MCP_STUB)  # the connector exists, no session is bound
        col = page.locator("#listCol")
        assert _wait(lambda: "Hunter is offline" in col.inner_text())
        assert "Reopen /jobhunter" in col.inner_text() and col.get_by_role("button", name="Reconnect Hunter").count() == 1
        _open_job(page, nw)
        dialog = page.get_by_role("dialog")
        dialog.locator(".help").get_by_role("button", name="Research company").click()
        assert _wait(lambda: (((fake.doc(f"actions/{nw}") or {}).get("requests") or {}).get("research") or {}).get("id"))
        assert page.evaluate("window.__mcpCalls") == []  # nothing to reach: no guessing, no fake "sent"
        help_text = dialog.locator(".help").inner_text()
        assert "Hunter is offline" in help_text and dialog.locator(".help").get_by_role("button", name="Retry").count() >= 1
        _no_plumbing(dialog.inner_text())
        # Hunter comes back (its session is bound and the page learns it): Reconnect sends the kept request.
        board.set_channel(db, "session", SESSION)
        _sync(ctx, db, site, 1)  # the request is already done by this run; the page now shows the result instead
        assert _wait(lambda: dialog.locator(".help").get_by_role("button", name="View research").count() == 1)
        assert "Hunter online" in col.inner_text()
        assert not errors, errors
        context.close()
        browser.close()


def test_write_cover_letter_dispatches_at_once_and_keeps_the_students_own_letter(site, ctx, db):
    from playwright.sync_api import sync_playwright

    fake, nw, jid = site["fake"], site["nw"], site["jid"]
    workspace.run_request(ctx, db, workspace.request(db, jid, "prepare", {}, request_id="prepare:seed")["request_id"])
    workspace.save_optional_text(db, jid, "letter", "Dear team, my own words about running clusters.")
    _bind(ctx, db, site)
    with sync_playwright() as p:
        browser = _browser(p)
        context, page, errors = _open(browser, site["url"], extra_script=MCP_STUB)
        _open_job(page, nw)
        dialog = page.get_by_role("dialog")
        dialog.locator(".help").get_by_role("button", name="Write cover letter").click()
        assert _wait(lambda: (((fake.doc(f"actions/{nw}") or {}).get("requests") or {}).get("letter") or {}).get("id", "").startswith("letter:"))
        assert _wait(lambda: page.evaluate("window.__mcpCalls.length") == 1)
        assert page.evaluate("window.__mcpCalls")[0]["tool"] == "send_message"
        assert _wait(lambda: "Drafting…" in dialog.locator(".help").inner_text())
        res, done, _ = _sync(ctx, db, site, 1)
        assert done[0]["kind"] == "letter" and done[0]["state"] == "ready"
        assert workspace.user_text(db, jid, "letter", "letter")["text"] == "Dear team, my own words about running clusters."  # never overwritten
        assert _wait(lambda: dialog.locator(".help").get_by_role("button", name="Review cover letter").count() == 1)
        dialog.locator(".help").get_by_role("button", name="Review cover letter").click()
        assert _wait(lambda: page.locator("#txt-letter").count() == 1)
        assert page.locator("#txt-letter").input_value() == "Dear team, my own words about running clusters."
        assert "Facts used in Hunter’s draft" in dialog.inner_text()  # Hunter's draft is there, underneath, not on top
        _no_plumbing(dialog.inner_text())
        assert not errors, errors
        context.close()
        browser.close()


def test_favorite_is_immediate_and_says_saved_and_nothing_about_hunter(site, ctx, db):
    from playwright.sync_api import sync_playwright

    fake, nw = site["fake"], site["nw"]
    with sync_playwright() as p:
        browser = _browser(p)
        context, page, errors = _open(browser, site["url"])
        _open_job(page, nw)
        dialog = page.get_by_role("dialog")
        dialog.get_by_role("button", name="☆ Favorite").click()
        assert _wait(lambda: (fake.doc(f"actions/{nw}") or {}).get("favorite") is True)
        status = dialog.locator(".save[role=status]")
        assert _wait(lambda: status.inner_text().strip() == "Saved")
        assert dialog.get_by_role("button", name="★ Favorite").count() == 1
        assert "★" in page.locator(f"button.row[data-key='{nw}']").inner_text()
        _no_plumbing(dialog.inner_text())
        assert "pending" not in dialog.inner_text().lower() and "Hunter" not in status.inner_text()
        assert not errors, errors
        context.close()
        browser.close()


def test_get_application_help_goes_working_then_review_application_never_continue(site, ctx, db):
    from playwright.sync_api import sync_playwright

    fake, nw = site["fake"], site["nw"]
    _bind(ctx, db, site)
    with sync_playwright() as p:
        browser = _browser(p)
        context, page, errors = _open(browser, site["url"], extra_script=MCP_STUB)
        _open_job(page, nw)
        dialog = page.get_by_role("dialog")
        help_box = dialog.locator(".help")
        assert help_box.get_by_role("button", name="Get application help").count() == 1
        help_box.get_by_role("button", name="Get application help").click()
        assert _wait(lambda: (((fake.doc(f"actions/{nw}") or {}).get("requests") or {}).get("prepare") or {}).get("id"))
        assert _wait(lambda: page.evaluate("window.__mcpCalls.length") == 1)
        assert _wait(lambda: help_box.get_by_role("button", name="Working…").count() == 1 and help_box.get_by_role("button", name="Working…").is_disabled())
        assert "Preparing…" in help_box.inner_text()
        assert "Continue application" not in dialog.inner_text()
        _sync(ctx, db, site, 1)
        assert _wait(lambda: help_box.get_by_role("button", name="Review application").count() == 1)
        assert "Continue application" not in dialog.inner_text() and "Working…" not in dialog.inner_text()
        assert "Ready" in page.locator(f"button.row[data-key='{nw}']").inner_text()
        help_box.get_by_role("button", name="Review application").click()
        text = dialog.inner_text()
        assert _wait(lambda: "Back to job" in dialog.inner_text())
        text = dialog.inner_text()
        for section in ("Your CV", "Your strongest case", "Application questions", "Optional", "Ready to apply?"):
            assert section in text, section
        assert "Application —" in text and "Continue" not in text
        _no_plumbing(text)
        assert not errors, errors
        context.close()
        browser.close()


def test_the_nightly_run_still_finds_jobs_and_writes_the_board_without_any_session(ctx, db, fake_sources, tmp_path):
    """Nightly Hunter is a different thing from the interactive one: it needs no bound session and the page copy
    never depends on it."""
    approve_profile(db, ctx, countries="Netherlands")
    assert db.get_setting(board.CHANNEL_SETTING) is None
    stats = hunt.run_hunt(ctx, db)
    assert stats["status"] in {"completed", "partial"} and stats["jobs_relevant_new"] >= 1
    assert isinstance(stats.get("logos"), dict)  # marks are looked up by Hunter during the search, never by the browser
    board.link(db, "https://claude.ai/artifact/5wUUffKaZnmpqGgPnRzPVE")
    board.import_actions(ctx, db, tmp_path / "none", listed=0)
    manifest = board.export(ctx, db, tmp_path / "out")
    assert any(m["doc_id"] == "logos" for m in manifest["writes"]) and (tmp_path / "out" / "meta" / "logos.json").exists()
    run = __import__("json").loads((tmp_path / "out" / "meta" / "run.json").read_text())
    assert run["hunter"]["channel"] is None and run["recommended"]


def test_a_letter_request_without_a_preparation_prepares_first(ctx, db):
    from tests.test_board import _seed_jobs

    approve_profile(db, ctx, countries="Netherlands")
    _seed_jobs(ctx, db)
    jid = db.fetchone("SELECT id FROM jobs WHERE employer_name = 'Northwind Cloud'")["id"]
    assert workspace.work_doc(db, jid, _key_for(db, "Northwind Cloud")) is None
    done = workspace.run_request(ctx, db, workspace.request(db, jid, "letter", {}, request_id="letter:seed")["request_id"])
    assert done["state"] == "ready"
    w = workspace.work_doc(db, jid, _key_for(db, "Northwind Cloud"))
    assert w["package"] and w["package"]["optional"]["letter"]["latest"]["text"] and w["brief"]["vacancy_says"]


class _FakeClient:
    def __init__(self, responses: dict):
        self.responses = responses
        self.calls: list[str] = []

    def get(self, url, **kw):
        self.calls.append(url)
        r = self.responses.get(url)
        if r is None:
            raise RuntimeError("refused")
        return r


class _Resp:
    def __init__(self, status, headers, content):
        self.status, self.headers, self.content = status, headers, content


def test_logos_are_fetched_once_from_verified_employer_domains_and_cached_as_small_data_uris(ctx, db):
    from tests.test_board import _seed_jobs

    approve_profile(db, ctx, countries="Netherlands")
    _seed_jobs(ctx, db)
    db.execute("UPDATE jobs SET direct_url = 'https://northwind.example/careers/1', direct_url_status = 'verified' WHERE employer_name = 'Northwind Cloud'")
    db.execute("UPDATE jobs SET direct_url = 'https://boards.greenhouse.io/acme/jobs/1', direct_url_status = 'verified' WHERE employer_name != 'Northwind Cloud'")
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40
    client = _FakeClient({"https://northwind.example/favicon.ico": _Resp(200, {"content-type": "image/png"}, png)})
    stats = logos.refresh(db, client, limit=5)
    assert stats["checked"] == 1 and stats["ok"] == 1 and client.calls == ["https://northwind.example/favicon.ico"]  # an ATS host is never an employer
    doc = logos.doc(db)
    assert doc["logos"]["northwind.example"] == "data:image/png;base64," + base64.b64encode(png).decode()
    # Fresh marks are not fetched again; a refusal is remembered, not retried every run.
    assert logos.refresh(db, client, limit=5)["checked"] == 0 and len(client.calls) == 1
    db.execute("UPDATE employer_logos SET checked_at = '2020-01-01T00:00:00+00:00'")
    bad = _FakeClient({"https://northwind.example/favicon.ico": _Resp(200, {"content-type": "text/html"}, b"<html>")})
    assert logos.refresh(db, bad, limit=5)["ok"] == 0 and logos.doc(db)["logos"] == {}
    assert logos.refresh(db, bad, limit=5)["checked"] == 0
