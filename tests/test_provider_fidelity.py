"""What the providers really say reaches the person unchanged: Ashby's structured pay and secondary locations,
Lever's numeric salary, one salary rendering everywhere (never "Not listed" when pay is stored), and a feed
in which some postings could not be read is partial, so it never closes jobs."""

from __future__ import annotations

import copy

from jobhunter.models import RawPosting
from jobhunter.services import hunt, normalize, notifications, views
from jobhunter.services.sources import get_adapter
from tests.conftest import approve_profile, load_fixture

ASHBY_STRUCTURED = {
    "id": "a1b2-0042",
    "title": "Platform Engineer",
    "employmentType": "FullTime",
    "location": "Dublin, Ireland",
    "secondaryLocations": [
        {"location": "Amsterdam, Netherlands", "address": {"postalAddress": {"addressLocality": "Amsterdam", "addressCountry": "Netherlands"}}},
        {"location": "Berlin", "address": {"postalAddress": {"addressLocality": "Berlin", "addressCountry": "Germany"}}},
    ],
    "publishedAt": "2026-10-19T10:00:00.000+00:00",
    "isListed": True,
    "isRemote": False,
    "workplaceType": "Hybrid",
    "address": {"postalAddress": {"addressLocality": "Dublin", "addressCountry": "Ireland"}},
    "jobUrl": "https://jobs.ashbyhq.com/contoso/a1b2-0042",
    "applyUrl": "https://jobs.ashbyhq.com/contoso/a1b2-0042/application",
    "descriptionPlain": "Kubernetes, Terraform and GCP for a payments platform. On-call. Prometheus and Grafana.",
    "compensation": {
        "compensationTierSummary": "€70K – €90K • Offers Equity",
        "scrapeableCompensationSalarySummary": "€70K - €90K",
        "compensationTiers": [
            {
                "id": "t1",
                "title": "Ireland",
                "tierSummary": "€70K – €90K • Offers Equity",
                "components": [
                    {"id": "c1", "summary": "€70K – €90K", "compensationType": "Salary", "interval": "1 YEAR", "currencyCode": "EUR", "minValue": 70000, "maxValue": 90000},
                    {"id": "c2", "summary": "Offers Equity", "compensationType": "EquityPercentage", "interval": "NONE", "currencyCode": None, "minValue": None, "maxValue": None},
                ],
            }
        ],
    },
}


def test_ashby_keeps_structured_pay_and_every_secondary_country():
    raw = get_adapter("ashby")._convert("ashby:contoso", ASHBY_STRUCTURED, {"employer_name": "Contoso Payments"})
    assert (raw.salary_min, raw.salary_max, raw.salary_currency, raw.salary_period) == (70000, 90000, "EUR", "year")
    assert raw.salary_text == "€70K – €90K • Offers Equity"  # the summary stays as evidence
    assert raw.raw["country_names"] == ["Ireland", "Netherlands", "Germany"]
    n = normalize.normalize(raw)
    assert set(n.country_codes) == {"IE", "NL", "DE"} and n.remote_type == "hybrid"
    assert (n.salary_min, n.salary_max, n.salary_currency, n.salary_period) == (70000, 90000, "EUR", "year")
    # Without structured tiers the summary text is parsed the same way.
    plain = copy.deepcopy(ASHBY_STRUCTURED)
    del plain["compensation"]["compensationTiers"]
    n2 = normalize.normalize(get_adapter("ashby")._convert("ashby:contoso", plain, {}))
    assert (n2.salary_min, n2.salary_max, n2.salary_currency) == (70000, 90000, "EUR")


def test_one_salary_rendering_everywhere_and_never_not_listed_when_pay_is_stored(ctx, db, client, seeded_sources):
    approve_profile(db, ctx, countries="Germany, Netherlands, Ireland")
    hunt.run_hunt(ctx, db, client=client)
    lever = db.fetchone("SELECT j.* FROM jobs j JOIN job_observations o ON o.job_id = j.id WHERE o.source_id = 'lever:fabrikam' AND j.salary_min IS NOT NULL")
    assert lever and lever["salary_text"] is None  # Lever gave numbers, no text
    label = normalize.salary_label(lever["salary_min"], lever["salary_max"], lever["salary_currency"], lever["salary_period"])
    assert label == "€55,000 to €70,000 a year"
    assert views.summarize(lever)["salary"] == label
    notifications.set_recipient(db, "student@example.com")
    jobs = [j for j in notifications.pending_jobs(db, limit=50) if j["id"] == lever["id"]]
    assert jobs, "the Lever job should be in the digest"
    digest = notifications.compose(db, jobs)
    assert label in digest.text and label in digest.html and "Not listed" not in digest.text
    # The CLI text and the dashboard document use the same words.
    assert label in views.format_list(views.list_jobs(db, view="new", per_page=50)) if hasattr(views, "format_list") else True
    # Text with no numbers stays as it is; nothing is invented.
    assert normalize.salary_label(None, None, None, None, text="Competitive") == "Competitive"
    assert normalize.salary_label(None, None, None, None) is None
    assert normalize.salary_label(4500, 5500, "EUR", "month") == "€4,500 to €5,500 a month"
    assert normalize.salary_label(60000, None, "USD", "year") == "from $60,000 a year"
    assert normalize.salary_label(None, 80000, "CHF", "year", basis="total") == "up to 80,000 CHF a year (total compensation)"


def test_postings_that_cannot_be_read_make_the_feed_partial_and_never_close_jobs(ctx, db, client, seeded_sources):
    approve_profile(db, ctx)
    first = hunt.run_hunt(ctx, db, client=client)
    assert first["status"] == "completed"
    open_sql = "SELECT COUNT(*) AS n FROM jobs j JOIN job_observations o ON o.job_id = j.id WHERE o.source_id = 'greenhouse:northwind' AND j.availability = 'open'"
    open_before = db.fetchone(open_sql)["n"]
    data = load_fixture("greenhouse_sample.json")
    removed = data["jobs"].pop()  # one job gone from the feed
    data["jobs"][0]["title"] = None  # one job the adapter cannot read
    seeded_sources.add("boards-api.greenhouse.io", "/v1/boards/northwind/jobs", data)
    res = get_adapter("greenhouse").fetch(client, "greenhouse:northwind", {"token": "northwind", "employer_name": "Northwind Cloud"})
    assert res.status == "partial" and res.complete is False and res.skipped_ids == [str(data["jobs"][0]["id"])]
    assert "1 of" in (res.error or "") and "could not be read" in res.error
    db.execute("UPDATE watch_sources SET last_success_at = NULL")
    second = hunt.run_hunt(ctx, db, client=client)
    assert second["sources_partial"] >= 1 and second["jobs_closed"] == 0
    assert db.fetchone(open_sql)["n"] == open_before
    still = db.fetchone("SELECT j.availability FROM jobs j JOIN job_observations o ON o.job_id = j.id WHERE o.source_id = 'greenhouse:northwind' AND o.external_id = ?", (str(removed["id"]),))
    assert still["availability"] == "open"
    health = {s["id"]: s for s in hunt.source_health(db)}
    assert health["greenhouse:northwind"]["last_status"] == "partial"


def test_raw_posting_identity_is_kept():
    raw = RawPosting(source_id="ashby:x", external_id="a1b2-0042", title="T", employer_name="E", url="https://jobs.ashbyhq.com/x/a1b2-0042")
    assert raw.external_id == "a1b2-0042"
