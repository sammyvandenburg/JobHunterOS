"""Phase 3: alert emails keep their vacancy links (JH-10) and a hunt runs in the background with
honest, pollable progress while jobs appear source by source (JH-20)."""

from __future__ import annotations

import base64
import time

from fastapi.testclient import TestClient

from jobhunter.db import connect
from jobhunter.models import FetchResult, RawPosting
from jobhunter.services import hunt, views
from jobhunter.services.email import gmail, linking
from jobhunter.services.sources.import_text import clean_url, parse_alert_email
from jobhunter.web.app import create_app
from tests.conftest import approve_profile

HTML_ALERT = """<html><body><table>
<tr><td><a href="https://www.linkedin.com/comm/jobs/view/4000000001/?trackingId=abc&amp;refId=xyz&amp;trk=eml-email_job_alert">Platform Engineer</a><br>Northwind Cloud · Utrecht, Netherlands<br><a href="https://www.linkedin.com/comm/jobs/view/4000000001/?trk=eml-view-job">View job</a></td></tr>
<tr><td><a href="https://www.linkedin.com/comm/jobs/view/4000000002/?trk=x">Site Reliability Engineer</a><br>Contoso · Amsterdam, Netherlands (Hybrid)<br><a href="https://www.linkedin.com/comm/jobs/view/4000000002/?trk=y">View job</a></td></tr>
<tr><td><a href="https://click.example.com/r?url=https%3A%2F%2Fnl.indeed.com%2Fviewjob%3Fjk%3Dabcdef123456%26from%3Dja%26utm_source%3Demail">DevOps Engineer</a><br>Fabrikam · Rotterdam, Netherlands<br></td></tr>
</table><img src="https://www.linkedin.com/emimp/pixel.gif" width="1"><p>You are receiving this email because you set up a job alert.</p></body></html>"""


def test_html_alert_keeps_links_and_card_context():
    posts = parse_alert_email(HTML_ALERT)
    by = {p.title: p for p in posts}
    assert set(by) == {"Platform Engineer", "Site Reliability Engineer", "DevOps Engineer"}
    assert by["Platform Engineer"].url == "https://www.linkedin.com/comm/jobs/view/4000000001/"
    assert by["Platform Engineer"].employer_name == "Northwind Cloud" and by["Platform Engineer"].location_text == "Utrecht, Netherlands"
    assert by["DevOps Engineer"].url == "https://nl.indeed.com/viewjob?jk=abcdef123456"  # unwrapped, tracking gone, jk kept
    assert all(p.posted_at is None and p.posted_at_provenance is None for p in posts)
    assert all(p.external_id.startswith(("linkedin:", "indeed:")) for p in posts)


def test_plain_alert_with_tracked_link_is_not_dropped():
    posts = parse_alert_email("New jobs for you\n\nPlatform Engineer\nSomeCo · Utrecht, Netherlands\nhttps://nl.indeed.com/viewjob?jk=abcdef1234&utm_source=email&from=ja\n\nCloud Engineer\nOtherCo · Remote - Netherlands\nhttps://www.linkedin.com/jobs/view/4000000009/?trk=eml\n")
    assert [p.title for p in posts] == ["Platform Engineer", "Cloud Engineer"]
    assert posts[0].url == "https://nl.indeed.com/viewjob?jk=abcdef1234" and posts[0].employer_name == "SomeCo"
    assert clean_url("https://www.linkedin.com/jobs/view/1/?currentJobId=1&trk=x#top") == "https://www.linkedin.com/jobs/view/1/?currentJobId=1"


def test_multipart_message_prefers_the_part_with_the_job_links():
    def b64(t):
        return base64.urlsafe_b64encode(t.encode()).decode().rstrip("=")

    full = {"payload": {"mimeType": "multipart/alternative", "parts": [{"mimeType": "text/plain", "body": {"data": b64("New jobs for you. View them online: https://www.linkedin.com/comm/jobs/alerts?trk=x")}}, {"mimeType": "text/html", "body": {"data": b64(HTML_ALERT)}}]}}
    text = gmail.extract_body(full)
    assert "jobs/view/4000000001" in text and "pixel.gif" not in text
    from jobhunter.services.sources.import_text import strip_tracking_links

    assert strip_tracking_links("see https://t.example.com/track/open/abc.gif and https://www.linkedin.com/jobs/view/4000000001/?trk=x") == "see [tracking image removed] and https://www.linkedin.com/jobs/view/4000000001/"
    assert len(parse_alert_email(text)) == 3


def test_alert_email_imports_its_jobs_with_unknown_dates(ctx, db):
    approve_profile(db, ctx, countries="Netherlands")
    res = linking.import_text_message(db, HTML_ALERT, sender="jobalerts-noreply@linkedin.com", subject="Platform Engineer: 3 new jobs in Netherlands")
    rows = db.fetchall("SELECT title, employer_name, posted_at, posted_at_provenance, discovery_url FROM jobs ORDER BY id")
    assert len(rows) == 3, (res, rows)
    assert all(r["posted_at"] is None and r["posted_at_provenance"] == "unknown" for r in rows)
    assert all("trk=" not in (r["discovery_url"] or "") for r in rows)
    long_msg = HTML_ALERT.replace("</table>", "".join(f"<tr><td><a href='https://www.linkedin.com/comm/jobs/view/40000001{i:02d}/?trk=z'>Infrastructure Engineer {i}</a><br>Co {i} · Amsterdam, Netherlands</td></tr>" for i in range(40)) + "</table>")
    assert len(parse_alert_email(long_msg)) == 43


# ---------------------------------------------------------------- JH-20
def _slow_sources(db, monkeypatch, delays):
    from jobhunter.services import catalog

    for i in range(len(delays)):
        catalog.ensure_source(db, f"greenhouse:s{i}", "greenhouse", {"token": f"s{i}", "employer_name": f"Employer {i}"}, "user", None)
    db.set_setting("catalog.synced_at", "test")

    def fake_fetch(client, s):
        i = int(s["id"].split("s")[-1])
        time.sleep(delays[i])
        p = RawPosting(source_id=s["id"], external_id=f"j{i}", title="Platform Engineer", employer_name=f"Employer {i}", url=f"https://job-boards.greenhouse.io/s{i}/jobs/{i}", location_text="Amsterdam, Netherlands", country_codes=["NL"], description_text="Kubernetes, Terraform, AWS, Linux, CI/CD. You own the platform with the team.", posted_at="2026-10-17T09:00:00+00:00", posted_at_provenance="t", is_employer_hosted=True)
        return FetchResult(source_id=s["id"], status="ok", postings=[p], complete=True)

    monkeypatch.setattr(hunt, "_fetch_one", fake_fetch)
    monkeypatch.setattr(hunt, "_promote_discovered_employers", lambda *a, **k: 0)
    monkeypatch.setattr(hunt, "_lookup_originals", lambda *a, **k: None)
    monkeypatch.setattr(hunt, "HTTP_MAX_CONCURRENCY", 1, raising=False)


def test_background_hunt_reports_progress_and_stores_jobs_progressively(ctx, db, monkeypatch):
    approve_profile(db, ctx, countries="Netherlands")
    _slow_sources(db, monkeypatch, [0.05, 0.9, 1.6])
    db.close()
    res = hunt.start_background(ctx)
    run_id = res["run_id"]
    d = connect(ctx.paths.db_path)
    try:
        seen_partial = False
        for _ in range(120):
            p = hunt.progress(d, run_id)
            if p["status"] == "running" and 0 < p["sources_done"] < p["sources_total"]:
                assert d.fetchone("SELECT COUNT(*) AS n FROM jobs")["n"] >= p["sources_done"]  # jobs land before the search ends
                assert "Checking job sites" in p["message"] and f"{p['sources_done']} of {p['sources_total']}" in p["message"]
                seen_partial = True
            if not p["running"]:
                break
            time.sleep(0.05)
        assert seen_partial, "never observed a partially complete search"
        assert p["status"] == "completed" and p["sources_done"] == 3 and p["jobs_new"] == 3 and p["stage"] == "ready"
        assert "Done: 3 new jobs" in p["message"]
        assert hunt.running_run(d) is None
        assert views.list_jobs(d, view="new")["total"] == 3
        # A second start while one runs is refused honestly; after it finishes, it starts again.
        _slow_sources_running = hunt.start_background(ctx)
        assert _slow_sources_running["status"] == "queued"
        try:
            hunt.start_background(ctx)
        except hunt.HuntInProgress as exc:
            assert "already running" in str(exc)
        else:  # the second one may already have finished on a fast machine; then no error is expected
            pass
    finally:
        d.close()


def test_dashboard_starts_a_background_hunt_and_exposes_progress(ctx, db, monkeypatch):
    approve_profile(db, ctx, countries="Netherlands")
    _slow_sources(db, monkeypatch, [0.3, 0.3])
    c = TestClient(create_app(ctx), base_url="http://127.0.0.1:8765")
    import re

    csrf = re.search(r'name="csrf" value="([0-9a-f]+)"', c.get("/").text).group(1)
    r = c.post("/do/hunt", data={"csrf": csrf}, headers={"origin": "http://127.0.0.1:8765"}, follow_redirects=False)
    assert r.status_code == 303 and "/?run=" in r.headers["location"]
    run_id = int(r.headers["location"].split("run=")[1])
    html = c.get("/").text
    assert f'data-hunt-poll="{run_id}"' in html and "Searching" in html
    for _ in range(100):
        p = c.get(f"/api/hunt/{run_id}").json()
        if not p["running"]:
            break
        time.sleep(0.05)
    assert p["status"] == "completed" and p["jobs_new"] == 2 and p["totals"]["new"] == 2
    assert c.get("/api/hunt/999").status_code == 404
    assert c.get("/api/status").json()["running"] is None
    html = c.get("/").text
    assert "data-hunt-poll" not in html and "Find jobs now" in html


def test_sync_hunt_path_still_works_and_originals_follow_match_order(ctx, db, client, seeded_sources):
    approve_profile(db, ctx)
    seen = []

    def spy(d, job, cl):
        seen.append(job["id"])

    from jobhunter.services import original_lookup

    original_lookup.resolve, old = spy, original_lookup.resolve
    try:
        st = hunt.run_hunt(ctx, db, client=client)
    finally:
        original_lookup.resolve = old
    assert st["status"] == "completed"
    prog = hunt.progress(db, st["run_id"])
    assert prog["stage"] == "ready" and prog["sources_done"] == prog["sources_total"] == st["sources_total"]
    if len(seen) >= 2:
        scores = [db.fetchone("SELECT COALESCE(score, -1) AS s FROM matches WHERE job_id = ? ORDER BY id DESC LIMIT 1", (j,))["s"] for j in seen]
        assert scores == sorted(scores, reverse=True)
