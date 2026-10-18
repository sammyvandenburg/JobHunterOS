"""Dashboard redesign: favorites, private notes, real metrics, Applied page, filters, direct job links."""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from jobhunter.db import connect
from jobhunter.models import RawPosting
from jobhunter.services import actions, hunt, notes, stats, views
from jobhunter.web.app import create_app
from tests.conftest import approve_profile

H = {"origin": "http://127.0.0.1:8765"}
FETCH = {**H, "X-Requested-With": "fetch", "Accept": "application/json"}


def _seed(ctx, db, ext="j1", **kw):
    base = dict(source_id="t", external_id=ext, title="Platform Engineer", employer_name=f"Employer {ext}", url=f"https://job-boards.greenhouse.io/x/jobs/{ext}", location_text="Amsterdam, Netherlands", country_codes=["NL"], remote_type="hybrid", description_text="Kubernetes, Terraform, AWS, Linux.", posted_at="2026-10-17T09:00:00+00:00", posted_at_provenance="t", is_employer_hosted=True)
    base.update(kw)
    return hunt.import_postings(ctx, db, [RawPosting(**base)])["job_ids"][0]


def _client(ctx):
    return TestClient(create_app(ctx), base_url="http://127.0.0.1:8765")


def _csrf(c, path="/"):
    return re.search(r'name="csrf" value="([0-9a-f]+)"', c.get(path).text).group(1)


def test_favorites_are_independent_of_saved_and_persist(ctx, db):
    approve_profile(db, ctx)
    a, b = _seed(ctx, db, "a"), _seed(ctx, db, "b")
    actions.favorite_job(db, a)
    actions.save_job(db, b)
    assert views.job_detail(db, a)["favorited"] is True and views.job_detail(db, a)["saved"] is False
    assert views.job_detail(db, b)["favorited"] is False and views.job_detail(db, b)["saved"] is True
    assert [i["id"] for i in views.list_jobs(db, view="favorites")["items"]] == [a]
    assert [i["id"] for i in views.list_jobs(db, view="saved")["items"]] == [b]
    t = stats.totals(db)
    assert t["favorites"] == 1 and t["saved"] == 1
    # Toggle off, then on again; the latest event wins and nothing is double counted.
    actions.favorite_job(db, a, False)
    assert stats.totals(db)["favorites"] == 0 and views.list_jobs(db, view="favorites")["total"] == 0
    actions.favorite_job(db, a)
    actions.favorite_job(db, b)
    assert stats.totals(db)["favorites"] == 2
    # Survives a restart (new connection to the same file).
    db.close()
    db2 = connect(ctx.paths.db_path)
    assert views.list_jobs(db2, view="favorites")["total"] == 2
    db2.close()


def test_favorite_toggle_over_fetch_returns_json_and_updates_counts(ctx, db):
    approve_profile(db, ctx)
    jid = _seed(ctx, db)
    c = _client(ctx)
    csrf = _csrf(c)
    r = c.post(f"/do/job/{jid}/favorite", data={"csrf": csrf}, headers=FETCH)
    assert r.status_code == 200 and r.json() == {"job_id": jid, "kind": "favorite", "on": True, "message": "Added to favorites.", "count": 1}
    html = c.get("/jobs/new").text
    assert 'data-kind="favorite"' in html and 'aria-pressed="true"' in html
    assert re.search(r'data-count="favorites">1<', html)
    r = c.post(f"/do/job/{jid}/unfavorite", data={"csrf": csrf}, headers=FETCH)
    assert r.json()["on"] is False and r.json()["count"] == 0
    # Without JS the same form falls back to a redirect, never a JSON blob in the browser.
    r = c.post(f"/do/job/{jid}/save", data={"csrf": csrf}, headers=H, follow_redirects=False)
    assert r.status_code == 303 and "Saved" in r.headers["location"]
    # CSRF still enforced on the JSON path.
    assert c.post(f"/do/job/{jid}/favorite", data={"csrf": "nope"}, headers=FETCH).status_code == 403


def test_notes_add_edit_delete_and_survive_rediscovery(ctx, db):
    approve_profile(db, ctx)
    jid = _seed(ctx, db)
    with pytest.raises(notes.NoteError):
        notes.add(db, jid, "   ")
    n1 = notes.add(db, jid, "Really interesting company, especially the Kubernetes stack.")
    n2 = notes.add(db, jid, "Ask about on-call.")
    assert notes.count_for_job(db, jid) == 2
    assert [n["id"] for n in notes.list_for_job(db, jid)] == [n2["id"], n1["id"]]
    upd = notes.update(db, n1["id"], "Really interesting company. Ask about the platform team size.")
    assert upd["updated_at"] >= upd["created_at"] and "team size" in notes.get(db, n1["id"])["body"]
    # The same vacancy is found again with changed text: same job, notes untouched.
    again = _seed(ctx, db, description_text="Kubernetes, Terraform, AWS, Linux, GitOps. Updated text.")
    assert again == jid
    assert notes.count_for_job(db, jid) == 2 and "team size" in notes.get(db, n1["id"])["body"]
    detail = views.job_detail(db, jid)
    assert detail["notes_count"] == 2 and detail["notes"][0]["body"] == "Ask about on-call."
    assert "Updated text" in detail["description"]
    # Delete is soft and reversible in the database, invisible in the product.
    notes.delete(db, n2["id"])
    assert notes.count_for_job(db, jid) == 1
    with pytest.raises(notes.NoteError):
        notes.get(db, n2["id"])
    assert db.fetchone("SELECT deleted_at FROM job_notes WHERE id = ?", (n2["id"],))["deleted_at"]
    with pytest.raises(notes.NoteError):
        notes.update(db, n2["id"], "zombie")
    with pytest.raises(notes.NoteError):
        notes.add(db, jid, "x" * 10_001)
    # Deleting everything leaves the job itself alone.
    notes.delete(db, n1["id"])
    assert views.job_detail(db, jid)["id"] == jid and views.list_jobs(db, view="new")["total"] == 1


def test_notes_web_flow(ctx, db):
    approve_profile(db, ctx)
    jid = _seed(ctx, db)
    c = _client(ctx)
    csrf = _csrf(c, f"/job/{jid}")
    r = c.post(f"/do/job/{jid}/notes", data={"csrf": csrf, "body": "First note"}, headers=H, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/job/{jid}?msg=Note%20saved.#notes"
    nid = notes.list_for_job(db, jid)[0]["id"]
    r = c.post(f"/do/note/{nid}/update", data={"csrf": csrf, "job_id": jid, "body": "First note, edited"}, headers=FETCH)
    assert r.status_code == 200 and r.json()["note_id"] == nid and r.json()["when"]
    html = c.get(f"/job/{jid}").text
    assert "First note, edited" in html and "My notes" in html and 'data-note-form="edit"' in html
    r = c.post(f"/do/note/{nid}/update", data={"csrf": csrf, "job_id": jid, "body": ""}, headers=FETCH)
    assert r.status_code == 400 and "empty" in r.json()["error"]
    r = c.post(f"/do/note/{nid}/delete", data={"csrf": csrf, "job_id": jid}, headers=H, follow_redirects=False)
    assert r.status_code == 303 and "deleted" in r.headers["location"]
    assert notes.count_for_job(db, jid) == 0
    assert c.post(f"/do/note/{nid}/delete", data={"csrf": "bad", "job_id": jid}, headers=H).status_code == 403
    # A note on a job that does not exist is refused, not silently stored.
    r = c.post("/do/job/999/notes", data={"csrf": csrf, "body": "ghost"}, headers=FETCH)
    assert r.status_code == 400


def test_dashboard_metrics_and_recommendations_come_from_real_data(ctx, db):
    approve_profile(db, ctx)
    ids = [_seed(ctx, db, f"r{i}", title="Platform Engineer" if i < 7 else "Solutions Architect (pre-sales)") for i in range(8)]
    actions.favorite_job(db, ids[0])
    actions.favorite_job(db, ids[1])
    actions.mark_applied(db, ids[2], applied_at="2026-10-15T10:00:00+00:00")
    actions.dismiss_job(db, ids[3], reason_code="not_for_me")
    d = views.dashboard(db)
    assert d["totals"]["found"] == 8 and d["totals"]["favorites"] == 2 and d["totals"]["applied"] == 1
    rec_ids = [j["id"] for j in d["recommended"]]
    assert len(rec_ids) <= 5 and ids[2] not in rec_ids and ids[3] not in rec_ids and ids[7] not in rec_ids
    assert d["status"] == "Idle" and d["last_run"] and d["prefs"]["location"] == "Rotterdam, Netherlands"
    for j in d["recommended"]:
        assert j["reasons"] and j["group_label"] in {"Good match", "Worth a shot", "Backup option"}
        assert "%" not in j["group_label"]
    c = _client(ctx)
    html = c.get("/").text
    assert "Your job hunt, all in one place." in html
    assert re.search(r'href="/jobs/all"[^<]*<span class="label">.*?Jobs found</span><span class="value">8</span>', html, re.S)
    assert re.search(r'data-count="favorites">2<', html)
    assert re.search(r'Applied</span><span class="value">1</span>', html)
    assert "Recommended for you" in html and "Recent activity" in html and "Your preferences" in html
    assert "Rotterdam, Netherlands" in html and "Netherlands" in html
    # Activity shows what the person did, with links to the jobs.
    assert f'href="/job/{ids[2]}"' in html
    # Status is JSON for the worker/skill too.
    s = c.get("/api/status").json()
    assert s["totals"]["found"] == 8


def test_applied_page_lists_company_title_date_status_notes_and_link(ctx, db):
    approve_profile(db, ctx)
    a = _seed(ctx, db, "ap1", employer_name="Northwind Cloud")
    b = _seed(ctx, db, "ap2", employer_name="Contoso Infra", title="Site Reliability Engineer")
    _seed(ctx, db, "ap3")
    actions.mark_applied(db, a, applied_at="2026-10-12T10:00:00+00:00")
    actions.mark_applied(db, b, applied_at="2026-10-14T10:00:00+00:00")
    actions.set_stage(db, b, "interview", actor="user")
    notes.add(db, a, "Spoke to the recruiter on Friday.")
    rows = views.applied_rows(db)
    assert [r["id"] for r in rows] == [b, a]  # newest application first
    assert rows[1]["note"]["body"].startswith("Spoke to") and rows[0]["stage"] == "interview"
    c = _client(ctx)
    html = c.get("/jobs/applied").text
    assert "Northwind Cloud" in html and "Contoso Infra" in html and "Site Reliability Engineer" in html
    assert "12 Oct" in html and "14 Oct" in html and "Spoke to the recruiter" in html
    assert html.count('href="/go/') >= 2 and "Employer ap3" not in html
    # Empty state is honest.
    html = c.get("/jobs/finished").text
    assert "Nothing finished yet" in html or "No finished" in html


def test_new_jobs_filters_sort_and_pagination(ctx, db):
    approve_profile(db, ctx)
    nl_remote = _seed(ctx, db, "f1", remote_type="remote", posted_at="2026-10-17T20:00:00+00:00")
    nl_onsite = _seed(ctx, db, "f2", remote_type="onsite", posted_at="2026-10-05T09:00:00+00:00", title="DevOps Engineer")
    de_hybrid = _seed(ctx, db, "f3", location_text="Berlin, Germany", country_codes=["DE"], remote_type="hybrid", description_text="Kubernetes, Terraform, AWS, Linux, Prometheus.", posted_at="2026-10-16T09:00:00+00:00")
    actions.favorite_job(db, nl_onsite)
    actions.save_job(db, de_hybrid)
    ids = lambda **kw: {i["id"] for i in views.list_jobs(db, view="new", per_page=50, **kw)["items"]}  # noqa: E731
    assert ids() == {nl_remote, nl_onsite, de_hybrid}
    assert ids(remote="remote") == {nl_remote}
    assert ids(country="de") == {de_hybrid}
    assert ids(fav_only=True) == {nl_onsite}
    assert ids(saved_only=True) == {de_hybrid}
    assert ids(query="prometheus") == {de_hybrid}
    assert ids(query="DevOps") == {nl_onsite}
    assert ids(query="Employer f1") == {nl_remote}
    # Age filters are relative to now; the 2026-10-05 job falls outside 7 days as long as "now" is after Oct 12 2026.
    assert nl_onsite not in ids(age="7") and nl_remote in ids(age="7")
    newest = [i["id"] for i in views.list_jobs(db, view="new", sort="newest")["items"]]
    assert newest[0] == nl_remote and newest[-1] == nl_onsite
    page1 = views.list_jobs(db, view="new", per_page=2, page=1)
    page2 = views.list_jobs(db, view="new", per_page=2, page=2)
    assert page1["total"] == 3 and len(page1["items"]) == 2 and len(page2["items"]) == 1 and page1["pages"] == 2
    assert page1["filters"]["sort"] == "best"
    # Freshness: a job posted within 48 hours is highlighted.
    fresh = {i["id"]: i["freshness"] for i in views.list_jobs(db, view="new")["items"]}
    assert fresh[nl_onsite] != "fresh"
    c = _client(ctx)
    html = c.get("/jobs/new?remote=remote&sort=newest").text
    assert "Employer f1" in html and "Employer f2" not in html
    assert 'name="q"' in html and 'name="remote"' in html and 'name="age"' in html and 'name="sort"' in html and 'name="fav"' in html
    html = c.get("/jobs/new?q=Prometheus").text
    assert "Employer f3" in html and "Employer f1" not in html
    html = c.get("/jobs/new?q=nothingmatchesthis").text
    assert "No jobs match" in html or "Nothing matches" in html or "No new jobs" in html


def test_job_detail_route_direct_link_and_go(ctx, db):
    approve_profile(db, ctx)
    jid = _seed(ctx, db, "d1", salary_text="€70,000 - €85,000", description_text="Kubernetes, Terraform, AWS, Linux. On-call once a month.")
    c = _client(ctx)
    r = c.get(f"/job/{jid}")
    assert r.status_code == 200
    html = r.text
    assert "Platform Engineer" in html and "Employer d1" in html and "70,000" in html
    for needle in ("Why this fits you", "Things to consider", "Full job description", "My notes", "Open application", "Help me apply", "I applied", "Not interested"):
        assert needle in html, needle
    assert f'href="/go/{jid}"' in html and "On-call once a month." in html
    assert c.get("/job/4242").status_code == 404
    r = c.get(f"/go/{jid}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "https://job-boards.greenhouse.io/x/jobs/d1"
    assert any(e["kind"] == "opened" for e in views.job_detail(db, jid)["history"])
    # Unknown list views 404 instead of showing an empty, misleading page.
    assert c.get("/jobs/whatever").status_code == 404
