"""The application workspace on the real dashboard page, driven in Chromium against the simulated artifact
runtime: Prepare application -> a recorded request -> Hunter's work -> the same page shows the preparation; edited
answers, the CV choice, "I applied" with which CV, follow-ups, contacts, My CVs, the live line to the running Hunter
session, and the same page at phone and desktop widths. Simulated runtime: this proves the page's JavaScript
and the hand-off files, never the claude.ai service."""

from __future__ import annotations

import time

import pytest

from jobhunter.services import board, workspace
from jobhunter.services import documents as docs
from tests.conftest import approve_profile
from tests.fake_artifact import SHIM, FakeArtifact
from tests.test_board import _key_for, _seed_jobs
from tests.test_browser import _browser
from tests.test_cv_variants import PLATFORM_CV

playwright = pytest.importorskip("playwright.sync_api")

MCP_STUB = """
(() => {
  const inner = window.claude.use;
  window.__mcpCalls = [];
  const mcp = { callTool: async (server, tool, input) => { window.__mcpCalls.push({ server, tool, input }); if (window.__mcpFail) { const e = new Error("no"); e.code = window.__mcpFail; throw e; } return { payload: { ok: true } }; } };
  window.claude.use = async (name) => (name === "mcp" ? mcp : inner(name));
})();
"""


@pytest.fixture
def site(ctx, db, tmp_path):
    approve_profile(db, ctx, countries="Netherlands")
    _seed_jobs(ctx, db)
    docs.add_cv_variant(db, ctx.paths, PLATFORM_CV.encode(), "platform-en.txt", "Platform — English", tags=["platform", "english"])
    board.link(db, "https://claude.ai/artifact/5wUUffKaZnmpqGgPnRzPVE")
    board.import_actions(ctx, db, tmp_path / "none", listed=0)
    manifest = board.export(ctx, db, tmp_path / "out")
    fake = FakeArtifact()
    fake.load_export(manifest)
    url = fake.start()
    yield {"fake": fake, "url": url, "nw": _key_for(db, "Northwind Cloud"), "jid": db.fetchone("SELECT id FROM jobs WHERE employer_name = 'Northwind Cloud'")["id"], "tmp": tmp_path}
    fake.stop()


def _open(browser, url: str, width=1200, extra_script: str | None = None):
    context = browser.new_context(viewport={"width": width, "height": 900})
    context.add_init_script(SHIM)
    if extra_script:
        context.add_init_script(extra_script)
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(url + "/")
    page.wait_for_selector("button.row")
    return context, page, errors


def _open_job(page, key: str):
    page.locator(f"button.row[data-key='{key}']").first.click()
    page.wait_for_selector("textarea#note")


def _wait(fn, timeout=6.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if fn():
            return True
        time.sleep(0.05)
    return False


def _sync(ctx, db, site, n_round: int):
    """One Hunter run: read the page's actions, do the requests, write the page (what `/jobhunter` does)."""
    fake, tmp = site["fake"], site["tmp"]
    n = fake.write_actions_dir(tmp / f"in{n_round}")
    res = board.import_actions(ctx, db, tmp / f"in{n_round}", listed=n)
    done = workspace.run_pending(ctx, db)
    manifest = board.export(ctx, db, tmp / f"out{n_round}")
    fake.load_export(manifest)
    return res, done, manifest


def test_prepare_application_from_the_page_reaches_hunter_and_comes_back_to_the_same_page(site, ctx, db):
    from playwright.sync_api import sync_playwright

    fake, nw = site["fake"], site["nw"]
    with sync_playwright() as p:
        browser = _browser(p)
        context, page, errors = _open(browser, site["url"])
        _open_job(page, nw)
        dialog = page.get_by_role("dialog")
        assert dialog.get_by_role("button", name="Get application help").count() == 1
        assert dialog.get_by_role("link", name="Open application").count() >= 1  # always there
        dialog.get_by_role("button", name="Get application help").first.click()
        assert _wait(lambda: (((fake.doc(f"actions/{nw}") or {}).get("requests") or {}).get("prepare") or {}).get("id", "").startswith("prepare:"))
        req_id = fake.doc(f"actions/{nw}")["requests"]["prepare"]["id"]
        # No running Hunter session behind this page: the request is kept, the page says Hunter is offline, with Retry.
        assert _wait(lambda: "Hunter is offline" in dialog.inner_text())
        assert dialog.get_by_role("button", name="Retry").count() >= 1
        text = dialog.inner_text()
        assert "Continue" not in text and "next run" not in text and req_id not in text
        # Hunter runs (read, do, write): the same open page shows the preparation.
        res, done, _ = _sync(ctx, db, site, 1)
        assert res["applied"][0]["changes"] == ["request:prepare"] and done[0]["request_id"] == req_id and done[0]["state"] == "ready"
        assert _wait(lambda: dialog.get_by_role("button", name="Review application").count() == 1)
        assert "Hunter is offline" not in dialog.locator(".help").inner_text() and dialog.get_by_role("button", name="Retry").count() == 0
        dialog.get_by_role("button", name="Review application").first.click()  # the focused application workspace
        assert _wait(lambda: "Your strongest case" in dialog.inner_text())
        dialog.get_by_text("Company and role brief").click()
        assert _wait(lambda: "The vacancy says" in dialog.inner_text()), dialog.inner_text()
        text = dialog.inner_text()
        assert "The vacancy says" in text and "Sources" in text and "Application questions" in text
        assert dialog.locator(".ev .e").count() >= 1  # the strongest case: evidence lines, not a table or a paragraph
        assert "Prepared " in text and "version 1" in text
        row_text = page.locator(f"button.row[data-key='{nw}']").inner_text()
        assert "In progress" not in row_text and "Ready" in row_text
        # The CV picker: default preselected, Hunter's one-line suggestion for the platform CV.
        assert "Hunter suggests “Platform — English”" in text and page.locator("#cv-select").input_value() != ""
        # Edit an answer (Hunter's examples are folded; open them); a snapshot from Hunter meanwhile keeps the draft and the caret.
        dialog.get_by_text("Example answers from Hunter").click()
        ta = dialog.locator("textarea[id^=ans-]").first
        assert _wait(lambda: ta.is_visible())
        aid = ta.get_attribute("id")[4:]
        ta.fill("Because the platform team runs things I")
        run = fake.doc("meta/run")
        run["at"] = "2026-10-22T10:00:00Z"
        fake.load({"meta/run": run})
        time.sleep(0.5)
        assert ta.input_value() == "Because the platform team runs things I" and page.evaluate("document.activeElement && document.activeElement.id") == "ans-" + aid
        ta.type(" care about.")
        ta.locator("xpath=following-sibling::div[1]").get_by_role("button", name="Save", exact=True).click()
        assert _wait(lambda: (((fake.doc(f"actions/{nw}") or {}).get("answers") or {}).get(aid) or {}).get("text") == "Because the platform team runs things I care about.")
        assert fake.doc(f"actions/{nw}")["answers"][aid]["rev"] == 1
        # Paste questions from the form: Hunter drafts answers to the real questions, never an invented questionnaire.
        dialog.get_by_role("button", name="Add the questions").click()
        dialog.locator("#newq").fill("Why do you want to work here? (500)\nWhat is your notice period?")
        dialog.get_by_role("button", name="Draft these answers").click()
        assert _wait(lambda: len((((fake.doc(f"actions/{nw}") or {}).get("requests") or {}).get("prepare") or {}).get("params", {}).get("questions", [])) == 2)
        qs = fake.doc(f"actions/{nw}")["requests"]["prepare"]["params"]["questions"]
        assert qs[0] == {"text": "Why do you want to work here?", "limit": 500}
        res, done, _ = _sync(ctx, db, site, 2)
        assert "answer" in res["applied"][0]["changes"] and done[0]["state"] == "ready"
        assert _wait(lambda: "What is your notice period?" in dialog.inner_text())
        assert "only you can answer" in dialog.inner_text() or "your words" in dialog.inner_text()
        # Hunter kept the person's wording through the second preparation.
        w = fake.doc(f"work/{nw}")
        mine = next(a for a in w["package"]["answers"] if a["id"] == aid)
        assert mine["user"]["text"] == "Because the platform team runs things I care about."
        assert not errors, errors
        context.close()
        browser.close()


def test_i_applied_records_the_cv_and_the_follow_up_has_a_changeable_date(site, ctx, db):
    from playwright.sync_api import sync_playwright

    fake, nw, jid = site["fake"], site["nw"], site["jid"]
    workspace.run_request(ctx, db, workspace.request(db, jid, "prepare", {}, request_id="prepare:seed")["request_id"])
    _sync(ctx, db, site, 0)
    with sync_playwright() as p:
        browser = _browser(p)
        context, page, errors = _open(browser, site["url"])
        _open_job(page, nw)
        dialog = page.get_by_role("dialog")
        assert _wait(lambda: "Review application" in dialog.inner_text())
        # Opening the job and copying never count as applied (nothing is written by looking).
        assert fake.doc(f"actions/{nw}") is None
        dialog.get_by_role("button", name="I applied").first.click()
        form = dialog.locator("form.applied")
        assert "The one prepared here: My CV (version 1)" in form.inner_text() and form.locator("input[name=cv][value=prepared]").is_checked()
        form.locator("#applied-when").fill("2026-10-20")
        form.get_by_role("button", name="Record it").click()
        assert _wait(lambda: (fake.doc(f"actions/{nw}") or {}).get("applied_cv") == "prepared")
        doc = fake.doc(f"actions/{nw}")
        assert doc["applied_at"].startswith("2026-10-20")
        assert _wait(lambda: "Applied 20 Oct 2026" in dialog.inner_text())
        assert dialog.get_by_role("button", name="View application").count() >= 1
        # Remind me to follow up, with a date the person can change.
        dialog.locator("#fu-when").fill("2026-10-27")
        dialog.get_by_role("button", name="Remind me").click()
        assert _wait(lambda: ((fake.doc(f"actions/{nw}") or {}).get("followup") or {}).get("action") == "set")
        assert fake.doc(f"actions/{nw}")["followup"]["due_at"].startswith("2026-10-27")
        res, _, _ = _sync(ctx, db, site, 1)
        assert "applied" in res["applied"][0]["changes"] and "followup:set" in res["applied"][0]["changes"]
        app = db.fetchone("SELECT * FROM applications WHERE job_id = ?", (jid,))
        assert app["submitted_cv_choice"] == "prepared" and app["submitted_cv_revision_key"]
        assert workspace.current_followup(db, jid)["due"] == "2026-10-27"
        assert _wait(lambda: "CV sent: My CV (version 1)" in dialog.inner_text() and "Follow-up reminder: 27 Oct 2026" in dialog.inner_text())
        dialog.get_by_role("button", name="Snooze 3 days").click()
        assert _wait(lambda: ((fake.doc(f"actions/{nw}") or {}).get("followup") or {}).get("action") == "snooze")
        _sync(ctx, db, site, 2)
        assert workspace.current_followup(db, jid)["due"] == "2026-10-30"
        dialog.get_by_role("button", name="Stop reminding").click()
        assert _wait(lambda: ((fake.doc(f"actions/{nw}") or {}).get("followup") or {}).get("action") == "stop")
        _sync(ctx, db, site, 3)
        assert workspace.current_followup(db, jid) is None
        assert _wait(lambda: "No reminder set." in dialog.inner_text())
        assert not errors, errors
        context.close()
        browser.close()


def test_contacts_are_found_on_request_named_by_the_person_and_only_confirmed_by_them(site, ctx, db):
    from playwright.sync_api import sync_playwright

    fake, nw, jid = site["fake"], site["nw"], site["jid"]
    workspace.run_request(ctx, db, workspace.request(db, jid, "prepare", {}, request_id="prepare:seed")["request_id"])
    _sync(ctx, db, site, 0)
    with sync_playwright() as p:
        browser = _browser(p)
        context, page, errors = _open(browser, site["url"])
        _open_job(page, nw)
        dialog = page.get_by_role("dialog")
        dialog.get_by_role("button", name="Review application").first.click()  # people live in the application workspace
        dialog.get_by_role("button", name="Find contacts").first.click()
        assert _wait(lambda: (((fake.doc(f"actions/{nw}") or {}).get("requests") or {}).get("contact") or {}).get("id"))
        res, done, _ = _sync(ctx, db, site, 1)
        assert done[0]["state"] == "needs_input"
        assert _wait(lambda: "couldn't confirm a contact" in dialog.inner_text())
        dialog.get_by_role("button", name="Add someone you know").click()
        dialog.locator("input[name=name]").fill("Sam Friend")
        dialog.locator("input[name=role]").fill("SRE")
        dialog.locator("input[name=route]").fill("https://www.linkedin.com/in/sam-friend")
        dialog.get_by_role("button", name="Keep this contact").click()
        assert _wait(lambda: ((fake.doc(f"actions/{nw}") or {}).get("contacts_new") or [{}])[0].get("name") == "Sam Friend")
        assert _wait(lambda: "Your connection" in dialog.inner_text())
        res, _, _ = _sync(ctx, db, site, 2)
        assert "contact" in res["applied"][0]["changes"]
        cs = workspace.list_contacts(db, jid)
        assert len(cs) == 1 and cs[0]["type"] == "connection" and cs[0]["route"] == "https://www.linkedin.com/in/sam-friend"
        assert _wait(lambda: dialog.get_by_role("button", name="I contacted them").count() == 1)
        dialog.get_by_role("button", name="Draft a short message").click()
        assert _wait(lambda: (((fake.doc(f"actions/{nw}") or {}).get("requests") or {}).get("message") or {}).get("params", {}).get("contact") in (cs[0]["key"], cs[0]["id"]))
        _, done, _ = _sync(ctx, db, site, 3)
        assert done[0]["state"] == "ready"
        assert _wait(lambda: "Hi Sam Friend," in dialog.inner_text())
        assert not [e for e in __import__("jobhunter.services.actions", fromlist=["history"]).history(db, jid) if e["kind"] == "contacted"]
        dialog.get_by_role("button", name="I contacted them").click()
        assert _wait(lambda: cs[0]["key"] in ((fake.doc(f"actions/{nw}") or {}).get("contacted") or {}))  # the portable key, not the row id
        res, _, _ = _sync(ctx, db, site, 4)
        assert "contacted" in res["applied"][0]["changes"] and workspace.list_contacts(db, jid)[0]["status"] == "contacted"
        assert _wait(lambda: "You contacted them" in dialog.inner_text())
        assert not errors, errors
        context.close()
        browser.close()


def test_my_cvs_under_profile_edits_reach_hunter_and_files_stay_with_hunter(site, ctx, db):
    from playwright.sync_api import sync_playwright

    fake = site["fake"]
    with sync_playwright() as p:
        browser = _browser(p)
        context, page, errors = _open(browser, site["url"])
        page.get_by_role("button", name="Profile", exact=True).click()
        box = page.locator("#sec-cvs")
        text = box.inner_text()
        assert "My CV" in text and "Platform — English" in text and "default" in text and "version 1" in text and "your profile was built from this" in text
        plat = next(c for c in fake.doc("meta/profile")["cvs"] if c["label"] == "Platform — English")
        box.get_by_role("button", name="Make default").click()
        assert _wait(lambda: (((fake.doc("actions/_cvs") or {}).get("variants") or {}).get(plat["key"]) or {}).get("default") is True)
        page.locator(f"#cvt-{plat['key']}").fill("platform, english, kubernetes")
        page.locator(f"#cvt-{plat['key']}").locator("xpath=ancestor::div[contains(@class,'cvrow')]").get_by_role("button", name="Save name").click()
        assert _wait(lambda: (((fake.doc("actions/_cvs") or {}).get("variants") or {}).get(plat["key"]) or {}).get("tags") == ["platform", "english", "kubernetes"])
        assert "Saved" in box.inner_text() and "next run" not in box.inner_text()
        box.get_by_role("button", name="Preview / download").first.click()
        assert _wait(lambda: (((fake.doc("actions/_cvs") or {}).get("requests") or {}).get("cv_file") or {}).get("id", "").startswith("cv_file:"))
        assert _wait(lambda: "the file stays with Hunter" in box.inner_text())  # no session behind this page: honest, never silent
        res, done, _ = _sync(ctx, db, site, 1)
        assert any(c.startswith("cv:") and c.endswith(":default") for c in res["applied"][0]["changes"]) and "request:cv_file" in res["applied"][0]["changes"]
        assert docs.default_cv(db)["label"] == "Platform — English" and docs.get_cv(db, plat["key"])["tags"] == ["platform", "english", "kubernetes"]
        assert done[0]["kind"] == "cv_file" and done[0]["state"] == "ready" and "unchanged" in done[0]["message"]
        assert _wait(lambda: "Hunter has the file, unchanged" in box.inner_text())
        assert not errors, errors
        context.close()
        browser.close()


def test_a_nightly_routine_as_the_stand_in_gets_the_request_and_a_refusal_shows_hunter_offline_with_retry(site, ctx, db):
    from playwright.sync_api import sync_playwright

    fake, nw = site["fake"], site["nw"]
    board.set_channel(db, "routine", "trig_01TEST", label="Hunter routine")
    _sync(ctx, db, site, 0)
    assert fake.doc("meta/run")["hunter"]["channel"]["id"] == "trig_01TEST"
    with sync_playwright() as p:
        browser = _browser(p)
        context, page, errors = _open(browser, site["url"], extra_script=MCP_STUB)
        _open_job(page, nw)
        dialog = page.get_by_role("dialog")
        dialog.get_by_role("button", name="Get application help").first.click()
        assert _wait(lambda: "Preparing…" in dialog.inner_text())
        calls = page.evaluate("window.__mcpCalls")
        req_id = fake.doc(f"actions/{nw}")["requests"]["prepare"]["id"]
        assert calls == [{"server": "Claude Code Remote", "tool": "fire_trigger", "input": {"trigger_id": "trig_01TEST", "text": "/jobhunter continue " + req_id}}]
        text = dialog.inner_text()
        assert "Continue" not in text and "next run" not in text and req_id not in text
        # The connector refuses: the request is still saved, the page says Hunter is offline and offers Retry.
        page.evaluate("window.__mcpFail = 'server_not_connected'")
        dialog.get_by_role("button", name="Find contacts").first.click()
        assert _wait(lambda: "Hunter is offline" in dialog.locator(".help").inner_text())
        assert (fake.doc(f"actions/{nw}")["requests"]["contact"]["id"]).startswith("contact:")
        assert "server_not_connected" not in dialog.inner_text() and "Continue" not in dialog.inner_text()
        page.evaluate("window.__mcpFail = null")
        dialog.locator(".help").get_by_role("button", name="Hunter is offline — Retry").click()
        assert _wait(lambda: len(page.evaluate("window.__mcpCalls")) == 3 and "Finding contacts…" in dialog.inner_text())  # the refused attempt counts too
        assert page.evaluate("window.__mcpCalls")[2]["input"]["text"] == "/jobhunter continue " + fake.doc(f"actions/{nw}")["requests"]["contact"]["id"]
        assert not errors, errors
        context.close()
        browser.close()


def test_the_same_page_works_at_phone_and_desktop_widths(site, ctx, db):
    from playwright.sync_api import sync_playwright

    nw, jid = site["nw"], site["jid"]
    workspace.run_request(ctx, db, workspace.request(db, jid, "prepare", {}, request_id="prepare:seed")["request_id"])
    _sync(ctx, db, site, 0)
    with sync_playwright() as p:
        browser = _browser(p)
        for width in (390, 1280):
            context, page, errors = _open(browser, site["url"], width=width)
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
            _open_job(page, nw)
            dialog = page.get_by_role("dialog")
            box = dialog.bounding_box()
            assert box["width"] <= width + 1 and (width > 860 or box["width"] >= width - 1)
            assert dialog.get_by_role("button", name="Review application").first.is_visible()
            assert dialog.get_by_role("link", name="Open application").first.is_visible()
            assert dialog.locator(".secnav").count() == 0  # no tabs inside the job
            dialog.get_by_role("button", name="Review application").first.click()
            assert _wait(lambda d=dialog: "Your strongest case" in d.inner_text())
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
            dialog.get_by_role("button", name="Back to job").click()
            assert _wait(lambda d=dialog: "Job overview" in d.inner_text())
            assert not errors, (width, errors)
            context.close()
        browser.close()


def test_today_shows_what_hunter_prepared_and_opening_it_counts_as_seen(site, ctx, db):
    """Simulated runtime: the Today block on the real page, one action per item, and the opened marker."""
    from playwright.sync_api import sync_playwright

    from jobhunter.services import today as today_svc

    fake = site["fake"]
    today_svc.set_mode(db, "prepare_best")
    today_svc.set_limits(db, preparations_per_day=1)
    pid = today_svc.auto_prepare(ctx, db)["prepared"][0]["job_id"]
    key = board.job_key(db.fetchone("SELECT canonical_key FROM jobs WHERE id = ?", (pid,))["canonical_key"])
    fake.load_export(board.export(ctx, db, site["tmp"] / "out2", require_actions=False))
    with sync_playwright() as p:
        browser = _browser(p)
        context, page, errors = _open(browser, site["url"])
        sec = page.locator("#listCol")
        assert _wait(lambda: "needs your attention" in sec.inner_text().lower() and "Hunter prepared" in sec.inner_text())
        text = sec.inner_text()
        assert ("ready for review" in text or "ready to submit" in text) and "Hunter prepared 1 today" in text
        assert sec.get_by_role("button", name="Open").count() == 1
        sec.get_by_role("button", name="Open").click()
        dialog = page.get_by_role("dialog")
        assert _wait(lambda: "by Hunter after a search" in dialog.inner_text())
        assert "Ready to submit" in dialog.inner_text() or "Ready for review" in dialog.inner_text()
        assert _wait(lambda: bool((fake.doc(f"actions/{key}") or {}).get("opened_at")))
        res, _, _ = _sync(ctx, db, site, 7)
        assert "opened" in res["applied"][0]["changes"] and today_svc.unopened_ready(db) == []
        assert not errors, errors
        context.close()
        browser.close()
