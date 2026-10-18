"""Phase 1 regressions: prepared jobs stay visible, preparation is idempotent, application records
bind the right CV/package and always carry a date, stage transitions respect time and authority,
request parameters are validated (audit items JH-08, JH-09, JH-21, JH-23)."""

from __future__ import annotations

import re
import threading

import pytest
from fastapi.testclient import TestClient

from jobhunter.db import connect
from jobhunter.models import RawPosting
from jobhunter.services import actions, drafts, hunt, onboarding, views
from jobhunter.services import documents as docs
from jobhunter.services import profile as profile_svc
from jobhunter.web.app import create_app
from tests.conftest import SAMPLE_CV, approve_profile

H = {"origin": "http://127.0.0.1:8765"}


def _seed(ctx, db, ext="p1", **kw):
    base = dict(source_id="t", external_id=ext, title="Platform Engineer", employer_name=f"Employer {ext}", url=f"https://job-boards.greenhouse.io/x/jobs/{ext}", location_text="Amsterdam, Netherlands", country_codes=["NL"], description_text="Kubernetes, Terraform, AWS, Linux. We run 100 clusters.", posted_at="2026-10-17T09:00:00+00:00", posted_at_provenance="t", is_employer_hosted=True)
    base.update(kw)
    return hunt.import_postings(ctx, db, [RawPosting(**base)])["job_ids"][0]


def _packages(db, jid):
    return db.fetchall("SELECT id, status, inputs_hash FROM drafts WHERE job_id = ? AND kind = 'package' ORDER BY id", (jid,))


# ---------------------------------------------------------------- JH-08
def test_prepared_job_stays_in_new_jobs_and_is_marked_ready(ctx, db):
    approve_profile(db, ctx)
    jid = _seed(ctx, db)
    assert views.list_jobs(db, view="new")["total"] == 1
    drafts.prepare(ctx, db, jid)
    res = views.list_jobs(db, view="new")
    assert [i["id"] for i in res["items"]] == [jid]
    item = res["items"][0]
    assert item["stage"] == "prepared" and item["stage_label"] == "Ready to apply"
    assert views.list_jobs(db, view="applied")["total"] == 0
    assert views.applied_rows(db) == []
    # Preparing is not applying: no applied event, no applied date.
    app = db.fetchone("SELECT * FROM applications WHERE job_id = ?", (jid,))
    assert app["stage"] == "prepared" and app["applied_at"] is None
    d = views.dashboard(db)
    assert d["totals"]["applied"] == 0 and jid in [j["id"] for j in d["recommended"]]


def test_prepare_is_idempotent_until_inputs_change(ctx, db):
    approve_profile(db, ctx)
    jid = _seed(ctx, db)
    p1 = drafts.prepare(ctx, db, jid)
    p2 = drafts.prepare(ctx, db, jid)
    assert p1["draft_id"] == p2["draft_id"] and p2["reused"] is True
    assert len(_packages(db, jid)) == 1
    assert db.fetchone("SELECT COUNT(*) AS n FROM events WHERE job_id = ? AND kind = 'prepared'", (jid,))["n"] == 1
    # Explicit regenerate makes a new version and marks the old one outdated.
    p3 = drafts.prepare(ctx, db, jid, regenerate=True)
    assert p3["draft_id"] != p1["draft_id"] and p3["reused"] is False
    rows = _packages(db, jid)
    assert [r["status"] for r in rows] == ["outdated", "draft"]
    # A material change to the job (new description) also makes the package outdated on next prepare.
    _seed(ctx, db, description_text="Kubernetes, Terraform, AWS, Linux, GitOps. Now with Prometheus.")
    detail = views.job_detail(db, jid)
    assert detail["package_outdated"] is True
    p4 = drafts.prepare(ctx, db, jid)
    assert p4["draft_id"] != p3["draft_id"]
    assert [r["status"] for r in _packages(db, jid)] == ["outdated", "outdated", "draft"]


def test_simultaneous_prepare_clicks_make_one_package(ctx, db):
    approve_profile(db, ctx)
    jid = _seed(ctx, db)
    db.close()
    errors = []

    def go():
        d = connect(ctx.paths.db_path)
        try:
            drafts.prepare(ctx, d, jid)
        except Exception as exc:  # pragma: no cover - surfaced by the assertion below
            errors.append(repr(exc))
        finally:
            d.close()

    threads = [threading.Thread(target=go) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    d = connect(ctx.paths.db_path)
    try:
        assert errors == []
        assert len(_packages(d, jid)) == 1
        assert d.fetchone("SELECT COUNT(*) AS n FROM events WHERE job_id = ? AND kind = 'prepared'", (jid,))["n"] == 1
    finally:
        d.close()


# ---------------------------------------------------------------- JH-09
def test_every_applied_transition_records_a_date_and_binds_cv_and_package(ctx, db):
    approve_profile(db, ctx)
    jid = _seed(ctx, db)
    pkg = drafts.prepare(ctx, db, jid)
    # Through the generic stage path (dashboard "Update" dropdown).
    actions.set_stage(db, jid, "applied", actor="user", occurred_at="2026-10-15T09:00:00+00:00")
    app = db.fetchone("SELECT * FROM applications WHERE job_id = ?", (jid,))
    assert app["applied_at"] == "2026-10-15T09:00:00+00:00" and app["applied_source"] == "user"
    assert app["cv_document_id"] == pkg["cv"]["document_id"] and app["package_draft_id"] == pkg["draft_id"]
    # Follow-up scheduling accepts it (it used to say "not applied").
    assert drafts.schedule_followup(db, jid)["due_at"]
    # Replacement CV later: the application keeps what was actually submitted.
    docs.add_text_document(db, ctx.paths, "cv", SAMPLE_CV + "\nNew line: Prometheus and Grafana at work.", "cv-v2")
    with pytest.raises(drafts.DraftError, match="check your profile"):
        drafts.prepare(ctx, db, jid)  # profile needs review: no silent mixing of new CV with old facts
    assert onboarding.ensure_profile_draft(db)  # what the upload handler does: a fresh draft from the new file
    profile_svc.approve(db)
    new_cv = docs.current_document(db, "cv")
    assert new_cv["id"] != pkg["cv"]["document_id"]
    app2 = db.fetchone("SELECT * FROM applications WHERE job_id = ?", (jid,))
    assert app2["cv_document_id"] == pkg["cv"]["document_id"] and app2["package_draft_id"] == pkg["draft_id"]
    # A second job prepared now uses the new CV.
    j2 = _seed(ctx, db, "p2")
    pkg2 = drafts.prepare(ctx, db, j2)
    assert pkg2["cv"]["document_id"] == new_cv["id"]


def test_backend_refuses_hunting_and_preparing_without_an_approved_profile(ctx, db):
    jid = _seed(ctx, db)
    with pytest.raises(drafts.DraftError):
        drafts.prepare(ctx, db, jid)
    with pytest.raises(hunt.HuntNotAllowed):
        hunt.run_hunt(ctx, db)
    assert db.fetchone("SELECT COUNT(*) AS n FROM hunt_runs WHERE trigger = 'manual'")["n"] == 0


# ---------------------------------------------------------------- JH-21
def test_older_email_event_never_overrides_newer_user_outcome(ctx, db):
    approve_profile(db, ctx)
    jid = _seed(ctx, db)
    actions.mark_applied(db, jid, applied_at="2026-10-01T09:00:00+00:00")
    actions.set_stage(db, jid, "offer", actor="user", occurred_at="2026-10-14T09:00:00+00:00")
    res = actions.set_stage(db, jid, "rejected", actor="email", occurred_at="2026-10-10T09:00:00+00:00", dedupe_key="mail-1")
    assert res["changed"] is False and "offer" in res["note"]
    assert db.fetchone("SELECT stage FROM applications WHERE job_id = ?", (jid,))["stage"] == "offer"
    # A newer email rejection after an offer is still not applied automatically; the person decides.
    res = actions.set_stage(db, jid, "rejected", actor="email", occurred_at="2026-10-16T09:00:00+00:00", dedupe_key="mail-2")
    assert res["changed"] is False
    # The person can always set it.
    res = actions.set_stage(db, jid, "rejected", actor="user", occurred_at="2026-10-16T09:00:00+00:00")
    assert res["changed"] is True


# ---------------------------------------------------------------- JH-23
def test_bad_query_parameters_never_500_and_pager_links_are_encoded(ctx, db):
    approve_profile(db, ctx)
    for i in range(25):
        _seed(ctx, db, f"q{i}", employer_name=f"A&B {i}")
    c = TestClient(create_app(ctx), base_url="http://127.0.0.1:8765")
    assert c.get("/jobs/new?page=not-a-number").status_code == 200
    assert c.get("/jobs/new?page=-4").status_code == 200
    assert c.get("/jobs/new?page=99999").status_code == 200
    html = c.get("/jobs/new?q=A%26B+1&sort=newest").text
    assert "A&amp;B 1" in html
    m = re.search(r'href="\?([^"]+)&amp;page=2"', c.get("/jobs/new?q=A%26B&sort=newest").text)
    assert m and "q=A%26B" in m.group(1)
    assert c.get("/job/not-a-number").status_code in {404, 422}
