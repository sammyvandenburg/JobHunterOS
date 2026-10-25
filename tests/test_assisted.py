"""Slice C: the Assisted Apply lane against a local form shaped like a Greenhouse job-board form.

Simulated browser test: a real Chromium drives a form served on 127.0.0.1 by this test. It proves the lane's
mechanics (preflight, permission, fill, read-back, one bound approval, one submit, receipt, duplicate and crash
protection). It is not an authenticated end-to-end test on a vendor's live site: no employer receives anything."""

from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from jobhunter.services import actions, assisted, hunt, workspace
from jobhunter.services import drafts as drafts_svc
from jobhunter.services import today as today_svc
from tests.conftest import approve_profile
from tests.test_gate_a import _posting

playwright = pytest.importorskip("playwright.sync_api")

FORM = """<!doctype html><html><head><title>Platform Engineer - Northwind Cloud</title></head><body>
<h1>Apply for this job</h1>
<form method="post" action="/northwind/jobs/nl1/apply" enctype="multipart/form-data" id="application_form">
<label for="first_name">First Name *</label><input id="first_name" name="job_application[first_name]" type="text" required>
<label for="last_name">Last Name *</label><input id="last_name" name="job_application[last_name]" type="text" required>
<label for="email">Email *</label><input id="email" name="job_application[email]" type="email" required>
<label for="phone">Phone</label><input id="phone" name="job_application[phone]" type="tel">
<label for="resume">Resume/CV *</label><input id="resume" name="job_application[resume]" type="file" required>
<label for="cover_letter_text">Cover Letter</label><textarea id="cover_letter_text" name="job_application[cover_letter_text]"></textarea>
<label for="q1">What is your notice period?</label><input id="q1" name="job_application[answers][0]" type="text">
<label for="q2">Why do you want to work here?</label><textarea id="q2" name="job_application[answers][1]"></textarea>
<label for="q3">Do you require visa sponsorship? *</label><select id="q3" name="job_application[answers][2]" required><option value="">Select</option><option>Yes</option><option>No</option></select>
<label for="gdpr"><input id="gdpr" name="job_application[gdpr]" type="checkbox" required> I accept the privacy notice</label>
<button type="submit">Submit application</button>
</form></body></html>"""
THANKS = """<!doctype html><html><head><title>Thank you</title></head><body><h1>Thank you for applying!</h1>
<p>We have received your application. Application ID: APP-%s</p><p>Fields: %s</p></body></html>"""


class _Site:
    """A tiny job board: GET the form, POST the application (recorded, answered with a confirmation page)."""

    def __init__(self, confirm: bool = True):
        self.posts: list[dict] = []
        self.confirm = confirm
        site = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                body = FORM.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n)
                fields = {}
                for m in re.finditer(rb'name="([^"]+)"(?:; filename="([^"]*)")?\r\n(?:Content-Type: [^\r\n]+\r\n)?\r\n(.*?)\r\n--', raw, re.S):
                    name = m.group(1).decode()
                    fields[name] = m.group(2).decode() if m.group(2) else m.group(3).decode(errors="replace")[:200]
                    if m.group(2):
                        fields[name + ".bytes"] = len(m.group(3))
                site.posts.append(fields)
                body = (THANKS % (len(site.posts), json.dumps(sorted(fields)))).encode() if site.confirm else b"<html><body><h1>Error 500</h1><p>Something went wrong.</p></body></html>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self):
        self.server.shutdown()


@pytest.fixture
def site():
    s = _Site()
    yield s
    s.stop()


def _driver():
    try:
        return assisted.BrowserDriver(headless=True)
    except assisted.AssistedError as exc:
        pytest.skip(str(exc))


def _seed(ctx, db, site, host_allowed=True):
    approve_profile(db, ctx, countries="Netherlands")
    jid = hunt.import_postings(ctx, db, [_posting("nl1", "Platform Engineer", url=f"{site.url}/northwind/jobs/nl1", apply_url=f"{site.url}/northwind/jobs/nl1")])["job_ids"][0]
    drafts_svc.prepare(ctx, db, jid)
    workspace.add_questions(db, jid, [{"text": "What is your notice period?"}, {"text": "Why do you want to work here?"}, {"text": "Do you require visa sponsorship?"}])
    workspace.save_answer(db, jid, workspace.answer_id("What is your notice period?"), "One month.")
    workspace.save_answer(db, jid, workspace.answer_id("Why do you want to work here?"), "Because of the platform work.")
    assisted.set_identity(db, "Sam", "Example", "sam@example.invalid", "+31 6 1234 5678")
    if host_allowed:
        assisted.allow_submit_host(db, "127.0.0.1")
    return jid


def test_the_registry_names_what_hunter_may_do_per_site(ctx, db):
    approve_profile(db, ctx, countries="Netherlands")
    gh = hunt.import_postings(ctx, db, [_posting("gh", "Platform Engineer", apply_url="https://job-boards.greenhouse.io/northwind/jobs/1/apply")])["job_ids"][0]
    li = hunt.import_postings(ctx, db, [_posting("li", "DevOps Engineer", url="https://www.linkedin.com/jobs/view/123", apply_url="https://www.linkedin.com/jobs/view/123", is_employer_hosted=False, source_id="import:text")])["job_ids"][0]
    un = hunt.import_postings(ctx, db, [_posting("un", "SRE", url="https://careers.example.org/jobs/9", apply_url="https://careers.example.org/jobs/9/apply", is_employer_hosted=False, source_id="import:text")])["job_ids"][0]
    caps = {j: assisted.capability_for(db, db.fetchone("SELECT * FROM jobs WHERE id = ?", (j,))) for j in (gh, li, un)}
    assert caps[gh]["id"] == "assisted_fill" and caps[gh]["verified"] == "simulated" and caps[gh]["needs_local_browser"]
    assert caps[li]["id"] == "manual_only" and "terms" in caps[li]["note"]
    assert caps[un]["id"] == "prepare_only" and not caps[un]["needs_local_browser"]
    # reviewed_submit only for a host the person named; nothing is submit-capable by default.
    assisted.allow_submit_host(db, "job-boards.greenhouse.io")
    assert assisted.capability_for(db, db.fetchone("SELECT * FROM jobs WHERE id = ?", (gh,)))["id"] == "reviewed_submit"
    assisted.allow_submit_host(db, "job-boards.greenhouse.io", allow=False)
    assert assisted.capability_for(db, db.fetchone("SELECT * FROM jobs WHERE id = ?", (gh,)))["id"] == "assisted_fill"
    # The route on the page carries the capability; a cloud session is told it has no browser of the person's.
    key = __import__("jobhunter.services.board", fromlist=["job_key"]).job_key(db.fetchone("SELECT canonical_key FROM jobs WHERE id = ?", (gh,))["canonical_key"])
    drafts_svc.prepare(ctx, db, gh)
    assert workspace.work_doc(db, gh, key)["package"]["route"]["capability"]["id"] == "assisted_fill"
    pf = assisted.preflight(ctx, db, gh)
    assert not pf["ok"] and any("Confirm the name" in b for b in pf["blockers"])
    db.set_setting("cloud.role", "cloud")
    assert any("cloud session" in b for b in assisted.preflight(ctx, db, gh)["blockers"])
    assert not today_svc.assisted_apply_available(db)


def test_fill_read_back_one_bound_approval_one_submit_and_a_receipt(ctx, db, site):
    jid = _seed(ctx, db, site)
    pf = assisted.preflight(ctx, db, jid)
    assert pf["ok"], pf["blockers"]
    assert pf["capability"]["id"] == "reviewed_submit" and pf["cv"]["sha256"] and pf["identity"]["email"] == "sam@example.invalid"
    assert today_svc.assisted_apply_available(db)  # the mode can now be chosen; it still never submits by itself
    run = assisted.permit(ctx, db, jid)
    assert run["state"] == "permitted" and run["permission"]["upload_cv"]["revision_key"].startswith("rev")
    if True:
        d = _driver()
        try:
            run = assisted.fill(ctx, db, run["run_key"], d)
            rb = run["readback"]
            by = {f["label"]: f["value"] for f in rb["fields"]}
            assert run["state"] == "filled" and by["First Name *"] == "Sam" and by["Last Name *"] == "Example" and by["Email *"] == "sam@example.invalid" and by["Phone"] == "+31 6 1234 5678"
            assert by["Resume/CV *"] == pf["cv"]["filename"] and by["What is your notice period?"] == "One month." and by["Why do you want to work here?"] == "Because of the platform work."
            assert by["Cover Letter"] == "" and by["Do you require visa sponsorship? *"] == ""  # no letter of theirs; a personal choice only they make
            assert {b["label"] for b in rb["blanks"]} >= {"Cover Letter", "Do you require visa sponsorship? *"} and rb["consents"][0]["label"].startswith("I accept the privacy notice")
            assert rb["sources"]["What is your notice period?"] == "your answer" and rb["sources"]["First Name *"] == "your identity"
            rv = assisted.review(db, run["run_key"])
            assert not rv["can_submit"] and "required field is still empty" in rv["why_not"]  # the sponsorship choice is theirs, and required
            d.page.locator("#q3").select_option("No")  # the person chooses on the form; Hunter reads the form again
            run = assisted.reread(db, run["run_key"], d)
            rv = assisted.review(db, run["run_key"])
            assert rv["can_submit"] and any(ln.startswith("- File: ") and "version 1" in ln for ln in rv["lines"]) and any("Will be ticked only with your approval" in ln for ln in rv["lines"])
            assert site.posts == []  # nothing left the machine yet
            # The approval binds to this read-back: a stale hash, a wrong code and an expired code all refuse.
            with pytest.raises(assisted.AssistedError, match="not the one on the page"):
                assisted.approve(db, run["run_key"], "deadbeef")
            ap = assisted.approve(db, run["run_key"], rv["readback_hash"])
            assert len(ap["code"]) == 8 and db.fetchone("SELECT approval_hash FROM assisted_runs WHERE run_key = ?", (run["run_key"],))["approval_hash"] != ap["code"]
            with pytest.raises(assisted.AssistedError, match="does not match"):
                assisted.submit(ctx, db, run["run_key"], "WRONG000", d)
            assert site.posts == []
            db.execute("UPDATE assisted_runs SET approval_expires_at = '2020-01-01T00:00:00' WHERE run_key = ?", (run["run_key"],))
            with pytest.raises(assisted.AssistedError, match="expired"):
                assisted.submit(ctx, db, run["run_key"], ap["code"], d)
            assert assisted.current_run(db, jid)["state"] == "filled"
            ap = assisted.approve(db, run["run_key"], rv["readback_hash"])
            # A form that changed after approval is never submitted.
            d.page.locator("#q1").fill("Two months.")
            with pytest.raises(assisted.AssistedError, match="changed since you approved"):
                assisted.submit(ctx, db, run["run_key"], ap["code"], d)
            d.page.locator("#q1").fill("One month.")
            res = assisted.submit(ctx, db, run["run_key"], ap["code"], d)
            assert res["state"] == "submitted" and res["receipt"]["confirmation_id"] == "APP-1" and res["receipt"]["looks_submitted"] and res["receipt"]["url"].endswith("/apply")
            assert len(site.posts) == 1 and site.posts[0]["job_application[first_name]"] == "Sam" and site.posts[0]["job_application[resume]"] == pf["cv"]["filename"] and site.posts[0]["job_application[resume].bytes"] == pf["cv"]["bytes"]
            assert site.posts[0]["job_application[gdpr]"] == "on" and site.posts[0]["job_application[cover_letter_text]"] == ""
            # Recorded as applied with the exact CV version, the destination and the receipt as evidence; once.
            app = db.fetchone("SELECT * FROM applications WHERE job_id = ?", (jid,))
            assert app["applied_at"] and app["submitted_cv_choice"] == "exact" and app["submitted_cv_revision_key"] == pf["cv"]["revision_key"] and app["submitted_destination"].startswith(site.url)
            snap = json.loads(app["submission_json"])
            assert snap["evidence"]["source"] == "assisted" and snap["evidence"]["confirmation_id"] == "APP-1" and snap["answers_state"] == "confirmed"
            with pytest.raises(assisted.AssistedError, match="Nothing is sent twice"):
                assisted.submit(ctx, db, run["run_key"], ap["code"], d)
            assert "already" in " ".join(assisted.preflight(ctx, db, jid)["blockers"])
            with pytest.raises(assisted.AssistedError):
                assisted.permit(ctx, db, jid)
            assert len(site.posts) == 1
        finally:
            d.close()


def test_assisted_fill_sites_never_get_a_click_and_the_person_reports_their_own_submit(ctx, db, site):
    jid = _seed(ctx, db, site, host_allowed=False)
    # 127.0.0.1 is not a known ATS and not allowed: prepare_only. Pretend the host is a known fill-only board for the lane.
    assert assisted.capability_for(db, db.fetchone("SELECT * FROM jobs WHERE id = ?", (jid,)))["id"] == "prepare_only"
    db.set_setting(assisted.SUBMIT_HOSTS_SETTING, ["127.0.0.1"])
    assisted.FORM_SUPPORT["fixture"] = "simulated"
    run = assisted.permit(ctx, db, jid)
    db.update("assisted_runs", {"capability": "assisted_fill"}, "run_key = ?", (run["run_key"],))
    if True:
        d = _driver()
        try:
            run = assisted.fill(ctx, db, run["run_key"], d)
            rv = assisted.review(db, run["run_key"])
            assert not rv["can_submit"] and "press submit on the site yourself" in rv["why_not"]
            with pytest.raises(assisted.AssistedError, match="filling only"):
                assisted.approve(db, run["run_key"], rv["readback_hash"])
            assert site.posts == []
            # The person submits on the site (here: the test clicks for them), then says so.
            d.page.locator("#gdpr").check()
            d.page.locator("#q3").select_option("No")
            with d.page.expect_navigation():
                d.page.locator("button[type=submit]").click()
            res = assisted.user_submitted(ctx, db, run["run_key"], d)
            assert res["state"] == "user_submitted" and res["receipt"]["looks_submitted"] and "You said you pressed submit. The page agrees." == res["receipt"]["evidence"]
            app = db.fetchone("SELECT * FROM applications WHERE job_id = ?", (jid,))
            assert app["applied_at"] and app["applied_source"] == "user" and json.loads(app["submission_json"])["evidence"]["source"] == "user"
        finally:
            d.close()


def test_an_unclear_answer_is_uncertain_and_a_dead_process_never_clicks_twice(ctx, db):
    s = _Site(confirm=False)
    try:
        jid = _seed(ctx, db, s)
        run = assisted.permit(ctx, db, jid)
        if True:
            d = _driver()
            try:
                run = assisted.fill(ctx, db, run["run_key"], d)
                d.page.locator("#q3").select_option("No")
                run = assisted.reread(db, run["run_key"], d)
                ap = assisted.approve(db, run["run_key"], run["readback_hash"])
                res = assisted.submit(ctx, db, run["run_key"], ap["code"], d)
                assert res["state"] == "uncertain" and "does not clearly say" in res["error"] and len(s.posts) == 1
                assert db.fetchone("SELECT applied_at FROM applications WHERE job_id = ?", (jid,))["applied_at"] is None  # not inflated
                assert any("uncertain" in b for b in assisted.preflight(ctx, db, jid)["blockers"])
                t = today_svc.today(ctx, db)
                assert any(i["kind"] == "assisted_uncertain" for g in t["groups"] if g["id"] == "attention" for i in g["items"])
                # The person checked their inbox: it went through. Recorded with their word as the evidence.
                out = assisted.resolve(ctx, db, jid, "submitted")
                assert out["state"] == "submitted" and db.fetchone("SELECT applied_source FROM applications WHERE job_id = ?", (jid,))["applied_source"] == "user"
            finally:
                d.close()
        # A crash mid-submit: the lease expires, the run reads uncertain, and no new attempt starts until resolved.
        other = hunt.import_postings(ctx, db, [_posting("nl2", "DevOps Engineer", url=f"{s.url}/northwind/jobs/nl2", apply_url=f"{s.url}/northwind/jobs/nl2")])["job_ids"][0]
        drafts_svc.prepare(ctx, db, other)
        run2 = assisted.permit(ctx, db, other)
        db.update("assisted_runs", {"state": "submitting", "lease_until": "2020-01-01T00:00:00"}, "run_key = ?", (run2["run_key"],))
        assert assisted.status(db, other)["run"]["state"] == "uncertain"
        with pytest.raises(assisted.AssistedError, match="uncertain"):
            assisted.permit(ctx, db, other)
        assisted.resolve(ctx, db, other, "not_submitted")
        assert assisted.current_run(db, other)["state"] == "failed" and assisted.permit(ctx, db, other)["state"] == "permitted"
        assert db.fetchone("SELECT COUNT(*) AS n FROM applications WHERE job_id = ? AND applied_at IS NOT NULL", (other,))["n"] == 0
    finally:
        s.stop()


def test_the_interactive_run_stops_on_anything_but_yes_and_the_code(ctx, db, site, monkeypatch):
    jid = _seed(ctx, db, site)
    drivers: list = []

    class Recording(assisted.BrowserDriver):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            drivers.append(self)

    monkeypatch.setattr(assisted, "BrowserDriver", Recording)
    said: list[str] = []

    def asker(script):
        it = iter(script)

        def ask(prompt):
            ans = next(it)
            if ans == "READY":
                drivers[-1].page.locator("#q3").select_option("No")  # the person makes the required choice on the form
            return ans

        return ask

    try:
        res = assisted.run_interactive(ctx, db, jid, headless=True, ask=asker(["READY", "no"]), say=said.append)
    except assisted.AssistedError as exc:
        pytest.skip(str(exc))
    assert res["state"] == "cancelled" and site.posts == [] and any("Destination:" in s for s in said)
    assert db.fetchone("SELECT applied_at FROM applications WHERE job_id = ?", (jid,))["applied_at"] is None
    res = assisted.run_interactive(ctx, db, jid, headless=True, ask=asker(["READY", "YES", ""]), say=said.append)
    assert res["state"] == "cancelled" and site.posts == [] and any("one-time code" in s for s in said)
    actions.dismiss_job(db, jid)  # unrelated: the lane leaves other records alone
