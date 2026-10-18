"""The release journey, end to end, in a real browser on a fresh install:
upload documents -> approve profile -> choose preferences -> find relevant jobs -> favorite and
unfavorite -> save and unsave -> add notes -> prepare twice without duplication -> job still visible ->
mark applied -> restart -> everything still there -> change profile and country -> dashboard and
email digest both move. Synthetic sources only (the fetch is patched), nothing leaves the machine."""

from __future__ import annotations

import os
import re
import socket
import threading
import time

import pytest

from jobhunter.config import AppContext
from jobhunter.db import connect
from jobhunter.models import FetchResult, RawPosting
from jobhunter.services import hunt, notifications
from tests.conftest import LINKEDIN_TEXT, SAMPLE_CV

playwright = pytest.importorskip("playwright.sync_api")

DESC = "About the role\nYou join a small platform team that builds and runs the internal developer platform.\nWhat you bring\nKubernetes in production, Terraform and AWS, CI/CD, Linux.\nWhat you'll do\nOwn the platform. Be on call."
POSTINGS = [
    dict(external_id="nl1", title="Platform Engineer", employer_name="Northwind Cloud", url="https://job-boards.greenhouse.io/northwind/jobs/1", location_text="Amsterdam, Netherlands", country_codes=["NL"]),
    dict(external_id="nl2", title="DevOps Engineer", employer_name="Contoso Payments", url="https://job-boards.greenhouse.io/contoso/jobs/2", location_text="Utrecht, Netherlands", country_codes=["NL"], remote_type="hybrid"),
    dict(external_id="de1", title="Site Reliability Engineer", employer_name="Berlin Robotics", url="https://job-boards.greenhouse.io/berlin/jobs/3", location_text="Berlin, Germany", country_codes=["DE"]),
    dict(external_id="us1", title="Platform Engineer", employer_name="Austin Labs", url="https://job-boards.greenhouse.io/austin/jobs/4", location_text="Remote worldwide", country_codes=[], remote_type="remote", description_text=DESC + "\nApplicants must be based in the US."),
    dict(external_id="x1", title="Passenger Services Platform Agent", employer_name="Airport BV", url="https://job-boards.greenhouse.io/airport/jobs/5", location_text="Amsterdam, Netherlands", country_codes=["NL"], description_text="Help passengers at the platform. Customer service."),
]


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _serve(ctx):
    import uvicorn

    from jobhunter.web.app import create_app

    port = _free_port()
    srv = uvicorn.Server(uvicorn.Config(create_app(ctx), host="127.0.0.1", port=port, log_level="error"))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    for _ in range(50):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    return srv, t, f"http://127.0.0.1:{port}"


def _stop(srv, t):
    srv.should_exit = True
    t.join(timeout=5)


@pytest.fixture
def fake_sources(monkeypatch):
    def fake_fetch(client, s):
        posts = []
        for p in POSTINGS:
            base = dict(source_id=s["id"], remote_type="onsite", description_text=DESC, posted_at="2026-10-17T09:00:00+00:00", posted_at_provenance="t", is_employer_hosted=True)
            base.update(p)
            posts.append(RawPosting(**base))
        time.sleep(0.2)
        return FetchResult(source_id=s["id"], status="ok", postings=posts, complete=True)

    monkeypatch.setattr(hunt, "_fetch_one", fake_fetch)
    monkeypatch.setattr(hunt, "_promote_discovered_employers", lambda *a, **k: 0)
    monkeypatch.setattr(hunt, "_lookup_originals", lambda *a, **k: None)
    monkeypatch.setattr(hunt, "sources_state", lambda db, ids=None: ([{"id": "greenhouse:fake", "kind": "greenhouse", "config_json": "{}", "enabled": 1}], None, None))
    monkeypatch.setattr(hunt, "ensure_sources_initialized", lambda db: {"initialized": False})
    monkeypatch.setattr(hunt, "_record_source_run", _record_without_watch_source_update)


def _record_without_watch_source_update(db, run_id, source, res, stats):
    """The fake source has no watch_sources row; record its jobs and counts without touching that table."""
    from jobhunter.services import jobs as jobs_svc

    new = relevant_new = 0
    with db.tx():
        for p in res.postings:
            job_id, is_new, _changed = jobs_svc.upsert_from_posting(db, p, run_id, True)
            if is_new:
                new += 1
                row = db.fetchone("SELECT relevance FROM jobs WHERE id = ?", (job_id,))
                if row and row["relevance"] in {"relevant", "uncertain"}:
                    relevant_new += 1
    stats["postings_fetched"] += len(res.postings)
    stats["jobs_new"] += new
    stats["jobs_relevant_new"] += relevant_new
    stats["sources_ok"] += 1


def test_fresh_install_journey_in_a_browser(home, fake_sources, tmp_path):
    from playwright.sync_api import Error as PwError
    from playwright.sync_api import expect, sync_playwright

    ctx = AppContext.from_env(home=home)
    srv, t, base = _serve(ctx)
    cv_file = tmp_path / "cv.txt"
    cv_file.write_text(SAMPLE_CV)
    li_file = tmp_path / "linkedin.txt"
    li_file.write_text(LINKEDIN_TEXT)
    with sync_playwright() as p:
        try:
            kwargs = {}
            exe = os.environ.get("JOBHUNTER_CHROMIUM")
            if exe:
                kwargs["executable_path"] = exe
            browser = p.chromium.launch(**kwargs)
        except PwError as exc:
            _stop(srv, t)
            pytest.skip(f"no browser available: {exc}")
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))

        # 1. Fresh install: readiness, then both documents in one step.
        page.goto(base + "/")
        assert "Let's get you set up" in page.content()
        page.get_by_role("button", name="Yes, both are finished").click()
        page.locator("input[name=cv]").set_input_files(str(cv_file))
        page.locator("input[name=linkedin]").set_input_files(str(li_file))
        page.get_by_role("button", name="Upload").click()
        # 2. Approve the profile that was built from the files.
        page.wait_for_url(re.compile(r"/profile"))
        assert "Here is what I understand about you" in page.content()
        assert "general IT" in page.content() or "infrastructure or systems work" in page.content()
        page.get_by_role("button", name="Yes, this looks right").click()
        # 3. Preferences in one go.
        page.wait_for_url(re.compile(r"/profile"))
        page.locator("input[name=location]").first.fill("Rotterdam, Netherlands")
        page.locator("input[name=countries]").first.fill("Netherlands")
        page.locator("input[name=salary]").first.fill("skip")
        page.locator("select[name=remote]").select_option("nice to have")
        page.locator("select[name=frequency]").select_option("only when asked")
        page.get_by_role("button", name="Save and finish").click()
        page.wait_for_url(re.compile(r"/\?msg=|/$"))
        assert "All set" in page.content()
        # 4. Find jobs now: background search with live progress, then relevant, country-correct jobs.
        page.get_by_role("button", name="Find jobs now").first.click()
        page.wait_for_url(re.compile(r"run=\d+"))
        assert page.locator("[data-hunt-poll]").count() == 1
        page.wait_for_url(lambda u: "run=" not in u, timeout=30000)
        page.goto(base + "/jobs/new")
        companies = page.locator("ul.jobs li.job .company").all_inner_texts()
        assert "Northwind Cloud" in companies and "Contoso Payments" in companies
        assert "Berlin Robotics" not in companies and "Austin Labs" not in companies and "Airport BV" not in companies
        assert all("%" not in x for x in page.locator("ul.jobs li.job .chip").all_inner_texts())
        page.goto(base + "/jobs/excluded")
        assert "Austin Labs" in page.content() and "Berlin Robotics" in page.content()
        # 5. Favorite on, off, on; save on, off; both survive a reload.
        page.goto(base + "/jobs/new")
        card = page.locator("ul.jobs li.job").filter(has_text="Northwind Cloud").first
        href = card.locator(".title a").get_attribute("href")
        fav, bm = card.locator("button.fav"), card.locator("button.bm")
        fav.click()
        expect(fav).to_have_attribute("aria-pressed", "true")
        fav.click()
        expect(fav).to_have_attribute("aria-pressed", "false")
        fav.click()
        expect(fav).to_have_attribute("aria-pressed", "true")
        bm.click()
        expect(bm).to_have_attribute("aria-pressed", "true")
        bm.click()
        expect(bm).to_have_attribute("aria-pressed", "false")
        page.reload()
        card = page.locator("ul.jobs li.job").filter(has=page.locator(f"a[href='{href}']")).first
        assert card.locator("button.fav").get_attribute("aria-pressed") == "true" and card.locator("button.bm").get_attribute("aria-pressed") == "false"
        # 6. Notes, then Help me apply twice: one package, the job still on the list.
        page.goto(base + href)
        page.locator("form[data-note-form='new'] textarea").fill("Nice stack. Ask about on-call.")
        page.locator("form[data-note-form='new'] button").click()
        page.wait_for_url(re.compile(r"msg="))
        assert page.locator(".notes .note").count() == 1
        page.locator("aside form[action$='/prepare'] button").first.click()
        page.wait_for_url(re.compile(r"msg="))
        assert "What they need" in page.content() and "Your proof" in page.content() and "Ready to apply" in page.content()
        page.locator("aside form[action$='/prepare'] button").first.click()
        page.wait_for_url(re.compile(r"msg="))
        assert "same help" in page.content()
        d = connect(ctx.paths.db_path)
        job_id = int(href.rsplit("/", 1)[1])
        assert d.fetchone("SELECT COUNT(*) AS n FROM drafts WHERE job_id = ? AND kind = 'package'", (job_id,))["n"] == 1
        d.close()
        # Edit a draft answer in my own words and save it.
        ta = page.locator("form[data-answer-form] textarea").first
        ta.fill("I like that you run Kubernetes on AWS. I manage Ubuntu servers and built a k3s homelab.")
        page.locator("form[data-answer-form] button:not([type=button])").first.click()
        expect(page.locator("form[data-answer-form] .save-state").first).to_contain_text("saved")
        page.goto(base + "/jobs/new")
        assert href in [a.get_attribute("href") for a in page.locator("ul.jobs li.job .title a").all()]
        assert "Ready to apply" in page.content()
        # 7. I applied, with a date; it moves to Applied with the date and the CV version.
        page.goto(base + href)
        page.locator("aside input[name=when]").first.fill("2026-10-16")
        page.once("dialog", lambda dlg: dlg.accept())
        page.locator("aside form[action$='/applied'] button").click()
        page.wait_for_url(re.compile(r"msg="))
        page.goto(base + "/jobs/applied")
        assert "Northwind Cloud" in page.content() and "16 Oct" in page.content()
        d = connect(ctx.paths.db_path)
        app = d.fetchone("SELECT applied_at, cv_document_id, package_draft_id FROM applications WHERE job_id = ?", (job_id,))
        assert app["applied_at"].startswith("2026-10-16") and app["cv_document_id"] and app["package_draft_id"]
        d.close()
        # 8. Restart the server: everything is still there.
        _stop(srv, t)
        srv, t, base = _serve(ctx)
        page.goto(base + "/jobs/favorites")
        assert "Northwind Cloud" in page.content()
        page.goto(base + href)
        assert "Nice stack. Ask about on-call." in page.locator(".notes .note textarea").first.input_value()
        assert "I like that you run Kubernetes" in page.locator("form[data-answer-form] textarea").first.input_value()
        page.goto(base + "/jobs/applied")
        assert "Northwind Cloud" in page.content()
        page.goto(base + "/")
        assert page.locator("a.metric").nth(3).inner_text().strip().lower().startswith("applied")
        # 9. Change the profile and the country: dashboard and digest move together, no new search.
        d = connect(ctx.paths.db_path)
        notifications.set_recipient(d, "me@example.com")
        before_digest = {r["id"] for r in notifications.pending_jobs(d, limit=50)}
        d.close()
        page.goto(base + "/profile")
        page.locator("form[action='/do/profile/correct'] input[name=text]").fill("I do not have AWS experience")
        page.get_by_role("button", name="Fix it").click()
        page.wait_for_url(re.compile(r"msg="))
        assert "Removed: aws" in page.content()
        page.goto(base + "/jobs/new")
        page.locator("ul.jobs li.job .title a").first.click()
        considered = page.locator("#why").inner_text()
        # AWS is gone from the profile; the CV still names EKS, so Hunter says "related, not the same".
        assert "AWS" in considered and ("not in your profile" in considered or "related but not the same" in considered), considered
        page.goto(base + "/profile")
        page.locator("#prefs-form input[name=countries]").fill("Germany")
        page.locator("#prefs-form button.primary").click()
        page.wait_for_url(re.compile(r"msg="))
        assert "checked again" in page.content()
        page.goto(base + "/jobs/new")
        companies = page.locator("ul.jobs li.job .company").all_inner_texts()
        assert "Berlin Robotics" in companies and "Contoso Payments" not in companies
        d = connect(ctx.paths.db_path)
        after_digest = {r["id"] for r in notifications.pending_jobs(d, limit=50)}
        dash = {int(a.get_attribute("href").rsplit("/", 1)[1]) for a in page.locator("ul.jobs li.job .title a").all()}
        assert after_digest <= dash and after_digest != before_digest
        d.close()
        assert errors == [], errors
        browser.close()
    _stop(srv, t)
