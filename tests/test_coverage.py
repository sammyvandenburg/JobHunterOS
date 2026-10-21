"""Coverage is measured from the hunt's own records: sources that answered, sources that could not be
checked, and where the relevant jobs are relative to the person's countries. Never from the catalogue size."""

from __future__ import annotations

from jobhunter.services import coverage, hunt
from tests.conftest import approve_profile


def test_coverage_counts_what_was_checked_and_where_the_jobs_are(ctx, db, client, seeded_sources):
    assert coverage.report(db)["run"] is None
    approve_profile(db, ctx, countries="Netherlands")
    hunt.run_hunt(ctx, db, client=client)
    rep = coverage.report(db)
    assert rep["run"]["status"] == "completed" and rep["sources"]["checked"] == rep["sources"]["ok"] + rep["sources"]["partial"] >= 4
    assert rep["sources"]["couldnt_check"] == [] and rep["jobs"]["found"] >= 5 and rep["jobs"]["relevant"] >= 3
    countries = {c["country"] for c in rep["jobs"]["by_country"]}
    assert "NL" in countries and rep["your_countries"] == ["NL"]
    assert rep["jobs"]["inside_your_countries"] >= 1 and rep["jobs"]["outside"] >= 1
    assert rep["jobs"]["share_open_to_you_percent"] is not None and "sources answered" in rep["note"] and "the Netherlands" in rep["note"]
    # A source that could not be checked is named as such, and the share is computed from what did answer.
    seeded_sources.fail_hosts.add("api.ashbyhq.com")
    db.execute("UPDATE watch_sources SET last_success_at = NULL")
    hunt.run_hunt(ctx, db, client=client)
    rep2 = coverage.report(db)
    assert rep2["sources"]["failed"] == 1 and rep2["sources"]["couldnt_check"][0]["source"] == "ashby:contoso"
    assert rep2["sources"]["by_kind"]["ashby"]["failed"] == 1
