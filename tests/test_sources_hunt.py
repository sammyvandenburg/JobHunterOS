"""Acceptance 13, 14, 16, 17, 18, 23: adapters, dedupe, dates, outages, discovery, original lookup, concurrency."""

from __future__ import annotations

import threading

from jobhunter.services import hunt, original_lookup
from jobhunter.services.http import SafeClient
from jobhunter.services.sources import get_adapter
from tests.conftest import approve_profile, load_fixture


def test_adapters_parse_fixtures(client, seeded_sources):
    gh = get_adapter("greenhouse").fetch(client, "greenhouse:northwind", {"token": "northwind", "employer_name": "Northwind Cloud"})
    assert gh.status == "ok" and len(gh.postings) == 4
    p = next(x for x in gh.postings if x.external_id == "1001")
    assert p.posted_at.startswith("2026-10-10") and p.posted_at_provenance == "greenhouse.first_published" and p.requisition_id == "REQ-77"
    assert "Kubernetes" in p.description_text and "&lt;" not in p.description_text
    lv = get_adapter("lever").fetch(client, "lever:fabrikam", {"site": "fabrikam", "region": "us"})
    assert lv.status == "ok" and lv.postings[0].salary_currency == "EUR" and lv.postings[0].country_codes == ["DE"]
    ab = get_adapter("ashby").fetch(client, "ashby:contoso", {"board": "contoso"})
    assert ab.status == "ok" and len(ab.postings) == 2  # unlisted job skipped
    assert ab.postings[0].salary_text.startswith("€70K")
    rm = get_adapter("remotive").fetch(client, "remotive:devops", {"categories": ["devops"]})
    assert rm.status == "ok" and rm.postings[0].remote_restriction == "Germany" and "Remotive" in rm.notes
    an = get_adapter("arbeitnow").fetch(client, "arbeitnow:eu", {"feed": "eu", "max_pages": 1})
    assert an.status == "ok" and an.complete and an.postings[0].posted_at.startswith("2026-10-1")


def test_lever_eu_base_and_pagination(client, transport):
    transport.add("api.eu.lever.co", "/v0/postings/eusite", load_fixture("lever_sample.json"))
    res = get_adapter("lever").fetch(client, "lever:eusite", {"site": "eusite", "region": "eu"})
    assert res.status == "ok" and any("api.eu.lever.co" in c for c in transport.calls)


def test_acc13_cross_source_dedupe_and_distinct_requisitions(ctx, db, client, seeded_sources):
    approve_profile(db, ctx)
    stats = hunt.run_hunt(ctx, db, client=client)
    titles = db.fetchall("SELECT id, title, employer_name, requisition_id, country_codes_json FROM jobs ORDER BY id")
    northwind = [t for t in titles if t["employer_name"] == "Northwind Cloud" and t["title"] == "Platform Engineer"]
    # Greenhouse REQ-77 (Amsterdam) + Arbeitnow copy -> one job; REQ-78 (Austin) stays distinct.
    assert len(northwind) == 2, northwind
    ams = next(t for t in northwind if "NL" in t["country_codes_json"])
    obs = db.fetchall("SELECT source_id FROM job_observations WHERE job_id = ?", (ams["id"],))
    assert {o["source_id"] for o in obs} == {"greenhouse:northwind", "arbeitnow:eu"}
    # Lever DevOps Engineer (Fabrikam, remote DE) + Remotive copy -> one job.
    fab = [t for t in titles if t["employer_name"] == "Fabrikam Mobility" and t["title"] == "DevOps Engineer"]
    assert len(fab) == 1
    assert stats["postings_fetched"] == 4 + 2 + 2 + 2 + 4
    from jobhunter.services import stats as stats_svc

    assert stats_svc.totals(db)["found"] == db.fetchone("SELECT COUNT(*) AS n FROM jobs")["n"]


def test_acc14_dates_keep_provenance(ctx, db, client, seeded_sources):
    approve_profile(db, ctx)
    hunt.run_hunt(ctx, db, client=client)
    j = db.fetchone("SELECT * FROM jobs WHERE requisition_id = 'REQ-77'")
    assert j["posted_at"].startswith("2026-10-10") and j["posted_at_provenance"] == "greenhouse.first_published"
    assert j["source_updated_at"].startswith("2026-10-15") and j["first_seen_at"] != j["posted_at"]
    # Arbeitnow-only job: the feed's created_at is kept as provenance, not relabelled.
    k = db.fetchone("SELECT * FROM jobs WHERE employer_name = 'Initech Hosting'")
    assert k["posted_at_provenance"] == "arbeitnow.created_at"
    # An imported alert posting has no date; it is 'unknown', not first_seen.
    from jobhunter.services.sources.import_text import parse_alert_email

    posts = parse_alert_email("New jobs for you\n\nPlatform Engineer\nSomeCo · Utrecht, Netherlands\nhttps://www.linkedin.com/comm/jobs/view/4000000001/?trk=x\n")
    assert posts and posts[0].posted_at is None
    res = hunt.import_postings(ctx, db, posts)
    imported = db.fetchone("SELECT posted_at, posted_at_provenance FROM jobs WHERE id = ?", (res["job_ids"][0],))
    assert imported["posted_at"] is None and imported["posted_at_provenance"] == "unknown"


def test_acc16_outage_does_not_close_or_zero(ctx, db, client, seeded_sources):
    approve_profile(db, ctx)
    first = hunt.run_hunt(ctx, db, client=client)
    assert first["status"] == "completed" and first["sources_failed"] == 0
    before_open = db.fetchone("SELECT COUNT(*) AS n FROM jobs WHERE availability = 'open'")["n"]
    seeded_sources.fail_hosts.add("boards-api.greenhouse.io")
    seeded_sources.status["api.ashbyhq.com/posting-api/job-board/contoso"] = 500
    second = hunt.run_hunt(ctx, db, client=client)
    assert second["status"] == "partial" and second["sources_failed"] == 2 and second["sources_ok"] == 2 and second["sources_skipped"] == 1  # Remotive reused (<6h)
    assert db.fetchone("SELECT COUNT(*) AS n FROM jobs WHERE availability = 'closed'")["n"] == 0
    assert db.fetchone("SELECT COUNT(*) AS n FROM jobs WHERE availability = 'open'")["n"] == before_open
    health = {s["id"]: s["plain_status"] for s in hunt.source_health(db)}
    assert "couldn't check" in health["greenhouse:northwind"]
    # A partial Lever page must not close jobs either.
    seeded_sources.fail_hosts.clear()
    seeded_sources.status.clear()
    # Remove a Greenhouse job from the (complete) snapshot: now it is closed with evidence.
    data = load_fixture("greenhouse_sample.json")
    data["jobs"] = [j for j in data["jobs"] if j["id"] != 1004]
    seeded_sources.add("boards-api.greenhouse.io", "/v1/boards/northwind/jobs", data)
    db.execute("UPDATE watch_sources SET last_success_at = NULL WHERE id = 'remotive:devops'")
    hunt.run_hunt(ctx, db, client=client)
    closed = db.fetchone("SELECT availability, availability_note FROM jobs WHERE requisition_id = 'REQ-80'")
    assert closed["availability"] == "closed" and "No longer listed" in closed["availability_note"]


def test_acc17_discovery_introduces_new_employers_and_boards(ctx, db, client, seeded_sources):
    approve_profile(db, ctx)
    seeded_sources.add("boards-api.greenhouse.io", "/v1/boards/newco/jobs", {"jobs": [{"id": 9001, "title": "DevOps Engineer", "absolute_url": "https://job-boards.greenhouse.io/newco/jobs/9001", "location": {"name": "Hamburg, Germany"}, "content": "Kubernetes, Terraform", "updated_at": "2026-10-16T00:00:00Z", "first_published": "2026-10-15T00:00:00Z"}]})
    # Make the Arbeitnow posting link to the employer ATS so promotion can verify it.
    data = load_fixture("arbeitnow_sample.json")
    data["data"][3]["url"] = "https://job-boards.greenhouse.io/newco/jobs/9001"
    seeded_sources.add("www.arbeitnow.com", "/api/job-board-api", data)
    stats = hunt.run_hunt(ctx, db, client=client)
    discovered = db.fetchall("SELECT name, ats_kind, ats_token, origin FROM employers WHERE origin = 'discovered'")
    names = {e["name"] for e in discovered}
    assert {"Initech Hosting", "Newco Robotics", "Umbrella Consulting"} <= names
    newco = next(e for e in discovered if e["name"] == "Newco Robotics")
    assert newco["ats_kind"] == "greenhouse" and newco["ats_token"] == "newco"
    assert db.fetchone("SELECT id FROM watch_sources WHERE id = 'greenhouse:newco'") is not None
    assert stats["employers_new"] >= 3 and stats["sources_added"] >= 1


def test_acc18_original_lookup_never_guesses(ctx, db, client, seeded_sources):
    approve_profile(db, ctx)
    hunt.run_hunt(ctx, db, client=client)
    # Umbrella Consulting (Remotive only, unknown board) -> unconfirmed, no URL invented.
    j = db.fetchone("SELECT * FROM jobs WHERE employer_name = 'Umbrella Consulting'")
    res = original_lookup.resolve(db, j, client)
    assert res["status"] == "unconfirmed" and res["url"] is None
    assert db.fetchone("SELECT direct_url, direct_url_status FROM jobs WHERE id = ?", (j["id"],))["direct_url"] is None
    # Arbeitnow copy of Northwind's Platform Engineer merged with the Greenhouse one -> verified employer link.
    k = db.fetchone("SELECT * FROM jobs WHERE requisition_id = 'REQ-77'")
    assert k["direct_url_status"] == "verified" and "greenhouse.io/northwind" in k["direct_url"]
    # Title similarity alone is not enough: a Northwind job with a different location stays unconfirmed.
    from jobhunter.models import RawPosting

    imp = hunt.import_postings(ctx, db, [RawPosting(source_id="import:pasted", external_id="pe-tokyo", title="Platform Engineer", employer_name="Northwind Cloud", url="", location_text="Tokyo, Japan", description_text="Kubernetes")])
    job = db.fetchone("SELECT * FROM jobs WHERE id = ?", (imp["job_ids"][0],))
    assert job["id"] not in (k["id"],)
    res2 = original_lookup.resolve(db, job, client)
    assert res2["status"] == "unconfirmed"


def test_acc23_concurrent_hunts_do_not_duplicate(ctx, db, client, seeded_sources):
    approve_profile(db, ctx)
    results = {}

    def run(name):
        from jobhunter.db import connect

        d = connect(ctx.paths.db_path)
        try:
            results[name] = hunt.run_hunt(ctx, d, client=SafeClient(transport=seeded_sources, min_interval_per_host=0.0, retries=0))
        except hunt.HuntInProgress as exc:
            results[name] = str(exc)
        finally:
            d.close()

    threads = [threading.Thread(target=run, args=(i,)) for i in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    ok = [r for r in results.values() if isinstance(r, dict)]
    assert len(ok) >= 1 and len(ok) + sum(1 for r in results.values() if isinstance(r, str)) == 3
    hunt.run_hunt(ctx, db, client=client)  # a second sequential run adds nothing new
    counts = db.fetchall("SELECT canonical_key, COUNT(*) AS n FROM jobs GROUP BY canonical_key HAVING n > 1")
    assert counts == []
    assert db.fetchone("SELECT COUNT(*) AS n FROM job_observations WHERE source_id = 'greenhouse:northwind'")["n"] == 4


def test_safe_http_blocks_private_targets(client):
    import pytest

    from jobhunter.services.http import UnsafeURL, validate_url

    for bad in ["http://127.0.0.1/x", "https://localhost/x", "https://169.254.169.254/latest/meta-data", "file:///etc/passwd", "https://user:pw@example.com/", "https://example.com:8443/x", "ftp://example.com/x"]:
        with pytest.raises(UnsafeURL):
            validate_url(bad)
    assert validate_url("https://example.com/jobs") == "https://example.com/jobs"


def test_import_text_detects_ats_links():
    from jobhunter.services.sources.import_text import detect_ats_link

    assert detect_ats_link("https://job-boards.greenhouse.io/gitlab/jobs/123")["token"] == "gitlab"
    assert detect_ats_link("https://jobs.lever.co/palantir/abc-def")["site"] == "palantir"
    assert detect_ats_link("https://jobs.ashbyhq.com/ashby/uuid")["board"] == "ashby"
    assert detect_ats_link("https://www.linkedin.com/jobs/view/123") is None
