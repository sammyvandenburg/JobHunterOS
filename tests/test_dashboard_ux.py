"""The redesigned dashboard: a navigation rail, the job list beside the selected job, Today first, one primary
action, no tabs inside a job, keyboard and phone widths. Driven in a real Chromium against the simulated artifact
runtime (tests/fake_artifact.py): it proves the page's own behaviour, not the claude.ai service."""

from __future__ import annotations

import time

import pytest

from jobhunter.services import actions, board, hunt, workspace
from jobhunter.services import drafts as drafts_svc
from tests.conftest import approve_profile
from tests.fake_artifact import SHIM, FakeArtifact
from tests.test_board import _key_for, _seed_jobs
from tests.test_browser import _browser
from tests.test_gate_a import _posting

playwright = pytest.importorskip("playwright.sync_api")


@pytest.fixture
def site(ctx, db, tmp_path):
    approve_profile(db, ctx, countries="Netherlands")
    _seed_jobs(ctx, db)
    hunt.import_postings(ctx, db, [
        _posting("el1", "Infrastructure Engineer", employer_name="ElevenLabs", url="https://elevenlabs.io/careers/1", location_text="London, UK; Dublin, IE; Warsaw, PL; Lisbon, PT", remote_type="remote", description_text="ElevenLabs builds voice AI.\n\nKubernetes on AWS with Terraform and Argo CD. Prometheus and Grafana. Python tooling.", posted_at="2026-10-13T09:00:00+00:00"),
        _posting("fb1", "Site Reliability Engineer", employer_name="Fabrikam Pay", url="https://job-boards.greenhouse.io/fabrikam/jobs/1", location_text="Amsterdam, Netherlands", remote_type="hybrid", description_text="SRE for payments. Kubernetes, Python, Grafana, Terraform.", posted_at="2026-10-20T09:00:00+00:00"),
        _posting("gx1", "Cloud Engineer", employer_name="Globex", url="https://job-boards.greenhouse.io/globex/jobs/1", location_text="Rotterdam, Netherlands", description_text="Cloud platform work on AWS with Terraform and Kubernetes.", posted_at="2026-10-21T09:00:00+00:00"),
        _posting("it1", "DevOps Engineer", employer_name="Initech", url="https://job-boards.greenhouse.io/initech/jobs/1", location_text="Eindhoven, Netherlands", description_text="CI/CD, Kubernetes, Ansible and Linux.", posted_at="2026-10-22T09:00:00+00:00"),
    ])
    nw = db.fetchone("SELECT id FROM jobs WHERE employer_name = 'Northwind Cloud'")["id"]
    con = db.fetchone("SELECT id FROM jobs WHERE employer_name = 'Contoso Payments'")["id"]
    drafts_svc.prepare(ctx, db, nw)
    drafts_svc.prepare(ctx, db, con)
    actions.mark_applied(db, con, applied_at="2026-10-20T10:00:00+00:00", cv_choice="prepared")
    workspace.set_followup(db, con, due_at="2026-10-27T09:00:00+00:00")
    board.link(db, "https://claude.ai/artifact/5wUUffKaZnmpqGgPnRzPVE")
    board.import_actions(ctx, db, tmp_path / "none", listed=0)
    manifest = board.export(ctx, db, tmp_path / "out")
    fake = FakeArtifact()
    fake.load_export(manifest)
    url = fake.start()
    yield {"fake": fake, "url": url, "nw": _key_for(db, "Northwind Cloud"), "el": _key_for(db, "ElevenLabs"), "con": _key_for(db, "Contoso Payments")}
    fake.stop()


def _open(browser, url, width=1400, height=900):
    context = browser.new_context(viewport={"width": width, "height": height})
    context.add_init_script(SHIM)
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e, errors=errors: errors.append(str(e)))
    page.goto(url + "/")
    page.wait_for_selector("button.row")
    return context, page, errors


def _wait(fn, timeout=5.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if fn():
            return True
        time.sleep(0.05)
    return False


def test_today_first_four_destinations_and_scannable_rows(site):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = _browser(p)
        context, page, errors = _open(browser, site["url"])
        nav = page.get_by_role("navigation", name="Main")
        assert [b.inner_text().strip().split("\n")[0] for b in nav.get_by_role("button").all()] == ["Today", "Jobs", "Applications", "Profile", "Settings"]
        assert page.locator(".brand").inner_text().replace("\n", " ") .find("JobHunterOS") >= 0 and "BY KUBECRAFT" in page.locator(".brand").inner_text().upper()
        assert "Top picks for you" in page.locator("#listCol").inner_text()
        picks = page.locator("#listCol section[aria-label='Top picks'] button.row")
        assert 3 <= picks.count() <= 5
        employers = [r.locator(".s").first.inner_text().split(" · ")[0] for r in picks.all()]
        assert len(set(employers[:3])) == 3  # different employers first, best first
        row = page.locator(f"button.row[data-key='{site['el']}']").first
        text = row.inner_text()
        assert "Infrastructure Engineer" in text and "ElevenLabs" in text and "London, UK + 3 locations" in text and "days ago" in text and ("Good match" in text or "Worth a shot" in text)
        assert 2 <= row.locator(".tag").count() <= 4 and row.locator(".tag").first.inner_text() == "Infrastructure"
        assert row.locator(".logo").inner_text().strip() == "EL" or row.locator(".logo img").count() == 1  # initials, or the employer's own favicon
        assert "Why:" not in text and len(text) < 400  # no matching paragraphs in the list
        assert page.locator(".ricon").count() == 0  # role icons belong to the detail and the pipeline, not to the compact rows
        assert page.locator(".secnav").count() == 0
        assert not errors, errors
        context.close()
        browser.close()


def test_the_job_detail_shows_what_matters_without_scrolling_and_has_no_tabs(site):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = _browser(p)
        context, page, errors = _open(browser, site["url"])
        page.locator(f"button.row[data-key='{site['el']}']").first.click()
        dialog = page.get_by_role("dialog")
        page.wait_for_selector("textarea#note")
        # Header: role icon, title, employer, match badge; Favorite, Save, Close together at the top.
        assert dialog.locator(".ricon[aria-label='Infrastructure Engineer role']").count() == 1
        head = dialog.locator(".dhead")
        for name in ("☆ Favorite", "Save for later", "Close"):
            assert head.get_by_role("button", name=name).count() == 1
        assert dialog.get_by_role("button", name="☆ Favorite").count() == 1 and dialog.get_by_role("button", name="Save for later").count() == 1  # never repeated lower down
        assert dialog.get_by_role("button", name="Not interested?").count() == 0 and dialog.locator(".secnav").count() == 0
        # Primary and secondary actions visible without scrolling.
        for loc in (dialog.get_by_role("button", name="Prepare my application"), dialog.get_by_role("link", name="Open application")):
            box = loc.first.bounding_box()
            assert box and box["y"] + box["height"] <= 900, box
        assert dialog.get_by_role("button", name="Prepare my application").count() == 1
        text = dialog.inner_text()
        assert "Get help with this application" in text and "Job overview" in text and "Why this could be a good fit" in text and "Job description" in text and "Your application" in text and "My notes" in text
        assert "View all locations" in text and "Dublin" not in text.split("View all locations")[0]  # the long list stays folded
        assert "Hunter reads this on its next run" not in text and "Press Help me apply" not in text and "Continue in Claude" not in text
        # The description is collapsed by default.
        assert "Kubernetes on AWS" not in text
        dialog.get_by_role("button", name="Show full description").click()
        assert _wait(lambda: "Kubernetes on AWS" in dialog.inner_text())
        # Favorite is real and persistent; the row shows it.
        dialog.get_by_role("button", name="☆ Favorite").click()
        assert _wait(lambda: (site["fake"].doc(f"actions/{site['el']}") or {}).get("favorite") is True)
        assert _wait(lambda: "★" in page.locator(f"button.row[data-key='{site['el']}']").first.inner_text())
        assert not errors, errors
        context.close()
        browser.close()


def test_keyboard_opens_and_closes_a_job_and_focus_returns_to_the_row(site):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = _browser(p)
        context, page, errors = _open(browser, site["url"])
        row = page.locator(f"button.row[data-key='{site['el']}']").first
        row.focus()
        page.keyboard.press("Enter")
        page.wait_for_selector("textarea#note")
        assert page.evaluate("document.activeElement && document.activeElement.closest('.dhead') !== null")
        page.keyboard.press("Escape")
        assert _wait(lambda: page.get_by_role("dialog").count() == 0)
        assert page.evaluate(f"document.activeElement && document.activeElement.getAttribute('data-key') === '{site['el']}'")
        # Escape inside the workspace goes back to the job first.
        page.locator(f"button.row[data-key='{site['nw']}']").first.click()
        page.get_by_role("dialog").get_by_role("button", name="Continue application").first.click()
        assert _wait(lambda: "Your strongest case" in page.get_by_role("dialog").inner_text())
        page.keyboard.press("Escape")
        assert _wait(lambda: "Job overview" in page.get_by_role("dialog").inner_text())
        assert not errors, errors
        context.close()
        browser.close()


def test_on_a_phone_the_list_is_the_screen_and_a_job_opens_full_screen(site):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = _browser(p)
        context, page, errors = _open(browser, site["url"], width=390, height=844)
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
        assert page.locator("#detailCol").is_hidden()
        page.get_by_role("button", name="Jobs", exact=True).click()
        page.get_by_label("Which jobs").select_option("all")
        assert _wait(lambda: page.locator("#listCol").evaluate("el => el.scrollHeight > el.clientHeight + 100"))  # the column scrolls, never the page
        page.locator("#listCol").evaluate("el => el.scrollTop = 100")
        page.locator(f"button.row[data-key='{site['el']}']").first.click()
        dialog = page.get_by_role("dialog")
        page.wait_for_selector("textarea#note")
        box = dialog.bounding_box()
        assert box["width"] >= 389 and box["x"] <= 1
        assert dialog.get_by_role("button", name="Prepare my application").first.is_visible()
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
        dialog.get_by_role("button", name="Close").click()
        assert _wait(lambda: page.locator("#detailCol").is_hidden())
        assert page.locator("#listCol").evaluate("el => el.scrollTop") == 100  # back returns to the same place
        # The rail sits at the bottom with the four destinations.
        assert [b.inner_text().strip().split("\n")[0] for b in page.get_by_role("navigation", name="Main").get_by_role("button").all()][:4] == ["Today", "Jobs", "Applications", "Profile"]
        assert not errors, errors
        context.close()
        browser.close()


def test_applications_is_a_simple_pipeline_and_profile_holds_the_settings(site):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = _browser(p)
        context, page, errors = _open(browser, site["url"])
        page.get_by_role("button", name="Applications").click()
        col = page.locator("#listCol")
        assert _wait(lambda: "Applied" in col.inner_text())
        text = col.inner_text()
        assert "Contoso Payments · DevOps Engineer" in text and "Applied 20 Oct 2026" in text and "CV: My CV v1" in text and "Follow-up 27 Oct 2026" in text and "Follow up" in text
        assert "Northwind Cloud · Platform Engineer" in text and "Prepared" in text and ("ready" in text.lower() or "preparing" in text.lower())
        assert "conversion" not in text.lower() and page.locator("canvas, svg.chart").count() == 0
        page.locator(f".approw[data-key='{site['con']}']").click()
        assert _wait(lambda: "Applied 20 Oct 2026" in page.get_by_role("dialog").inner_text())
        page.get_by_role("button", name="Profile", exact=True).click()
        assert _wait(lambda: "approved candidate profile" in col.inner_text().lower())
        text = col.inner_text().lower()
        assert "preferences" in text and "my cvs" in text and "connected services and hunter settings" in text and "automation" in text
        page.get_by_role("button", name="Settings").click()
        assert _wait(lambda: page.locator("#sec-settings").is_visible())
        assert not errors, errors
        context.close()
        browser.close()
