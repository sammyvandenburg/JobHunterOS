"""Slice E: material-change detection on the jobs the person follows, company watches, the digest and Today lines,
and the restrained weekly observation. Synthetic data only."""

from __future__ import annotations

from jobhunter.models import RawPosting
from jobhunter.services import actions, board, hunt, notifications, watches
from jobhunter.services import drafts as drafts_svc
from jobhunter.services import jobs as jobs_svc
from jobhunter.services import today as today_svc
from jobhunter.util import now_iso
from tests.conftest import approve_profile
from tests.test_gate_a import _posting


def _fields(db, jid):
    return [(r["field"], r["old_value"], r["new_value"]) for r in db.fetchall("SELECT * FROM job_changes WHERE job_id = ? ORDER BY id", (jid,))]


def test_material_changes_are_recorded_by_a_refresh_never_by_a_failed_check(ctx, db):
    approve_profile(db, ctx, countries="Netherlands")
    base = dict(salary_text="€70,000 - €80,000", apply_url="https://job-boards.greenhouse.io/northwind/jobs/nl1/apply")
    jid = hunt.import_postings(ctx, db, [_posting("nl1", "Platform Engineer", **base)])["job_ids"][0]
    assert _fields(db, jid) == []
    hunt.import_postings(ctx, db, [_posting("nl1", "Platform Engineer", **base)])  # the same again: nothing changed
    assert _fields(db, jid) == []
    hunt.import_postings(ctx, db, [_posting("nl1", "Senior Platform Engineer", salary_text="€80,000 - €90,000", location_text="Rotterdam, Netherlands", description_text="Kubernetes, Terraform and AWS. Platform team. On call. Apply before 15 November 2026.", apply_url=base["apply_url"])])
    got = {f: (o, n) for f, o, n in _fields(db, jid)}
    assert got["salary"] == ("€70,000 - €80,000", "€80,000 - €90,000") and got["location"] == ("Amsterdam, Netherlands", "Rotterdam, Netherlands") and got["title"] == ("Platform Engineer", "Senior Platform Engineer")
    assert got["description"][0] != got["description"][1] and got["closing_date"] == (None, "15 November 2026")
    # An aggregator copy without pay: not a change (a missing value is not a changed value); a failed check: nothing.
    agg = RawPosting(source_id="remotive:devops", external_id="r-1", title="Senior Platform Engineer", employer_name="Northwind Cloud", url="https://remotive.com/remote-jobs/1", location_text="Rotterdam, Netherlands", country_codes=["NL"], description_text="Kubernetes.", posted_at="2026-10-18T09:00:00+00:00", posted_at_provenance="t", is_employer_hosted=False)
    n_before = len(_fields(db, jid))
    hunt.import_postings(ctx, db, [agg])
    assert len(_fields(db, jid)) == n_before
    jobs_svc.mark_unverified(db, "greenhouse:fake", "Could not check greenhouse:fake", now_iso())
    assert len(_fields(db, jid)) == n_before
    # A complete employer snapshot without the job: "no longer listed" (with its evidence), then listed again.
    db.execute("UPDATE job_observations SET last_observed_at = '2026-09-01T00:00:00+00:00' WHERE job_id = ? AND source_id != 'greenhouse:fake'", (jid,))
    assert jobs_svc.mark_missing_as_closed(db, "greenhouse:fake", set(), now_iso()) == 1
    assert _fields(db, jid)[-1] == ("availability", "open", "closed")
    hunt.import_postings(ctx, db, [_posting("nl1", "Senior Platform Engineer", **base)])
    assert ("availability", "closed", "open") in _fields(db, jid)
    assert all(r["source_id"] for r in db.fetchall("SELECT source_id FROM job_changes WHERE job_id = ?", (jid,)))


def test_today_and_the_digest_show_changes_on_followed_jobs_only_and_company_watches(ctx, db, tmp_path):
    approve_profile(db, ctx, countries="Netherlands")
    a = hunt.import_postings(ctx, db, [_posting("nl1", "Platform Engineer", salary_text="€70,000")])["job_ids"][0]
    hunt.import_postings(ctx, db, [_posting("nl2", "DevOps Engineer", salary_text="€60,000")])
    actions.save_job(db, a)
    hunt.import_postings(ctx, db, [_posting("nl1", "Platform Engineer", salary_text="€75,000"), _posting("nl2", "DevOps Engineer", salary_text="€65,000")])
    ch = watches.changes(db)
    assert [c["job_id"] for c in ch] == [a] and ch[0]["field"] == "salary" and "changed from €70,000 to €75,000" in watches.change_line(ch[0])
    assert len(watches.changes(db, only_watched=False)) == 2
    t = today_svc.today(ctx, db)
    items = [i for g in t["groups"] if g["id"] == "changed" for i in g["items"] if i["kind"] == "watch_change"]
    assert len(items) == 1 and items[0]["action"]["label"] == "Open" and items[0]["action"]["key"].startswith("j") and f"watches ack {a}" in items[0]["action"]["command"]
    # The digest names it under "Something changed" even with no new jobs; acknowledging clears Today.
    notifications.set_recipient(db, "student@example.com")
    db.set_setting("digest.transport", "claude")
    board.link(db, "https://claude.ai/artifact/5wUUffKaZnmpqGgPnRzPVE")
    board.import_actions(ctx, db, tmp_path / "none", listed=0)
    db.execute("UPDATE jobs SET posted_at = '2025-01-01T00:00:00+00:00'")
    prep = notifications.prepare_handoff(ctx, db)
    assert prep["status"] == "ready" and prep["subject"] == "JobHunterOS: something changed on a job you follow" and "Something changed on a job you follow:" in prep["text"] and "€75,000" in prep["text"] and "DevOps" not in prep["text"]
    assert watches.acknowledge(db, a)["acknowledged"] == 1 and watches.changes(db) == []
    assert not [i for g in today_svc.today(ctx, db)["groups"] for i in g["items"] if i["kind"] == "watch_change"]
    # A company watch: a new relevant job there shows on Today; an irrelevant one does not.
    watches.watch_company(db, "Contoso Payments")
    c = hunt.import_postings(ctx, db, [_posting("c1", "Site Reliability Engineer", employer_name="Contoso Payments", url="https://job-boards.greenhouse.io/contoso/jobs/c1")])["job_ids"][0]
    hunt.import_postings(ctx, db, [_posting("c2", "Sales Manager", employer_name="Contoso Payments", url="https://job-boards.greenhouse.io/contoso/jobs/c2", description_text="Sell payments products.")])
    new = watches.new_at_watched_companies(db)
    assert [n["job_id"] for n in new] == [c]
    assert any(i["kind"] == "company_new" and "Site Reliability Engineer at Contoso Payments" in i["text"] for g in today_svc.today(ctx, db)["groups"] for i in g["items"])
    s = watches.summary(db)
    assert s["companies"] == ["Contoso Payments"] and any(r["job_id"] == a for r in s["watched_jobs"]) and "never as closed" in s["note"]
    watches.watch_company(db, "Contoso Payments", on=False)
    assert watches.summary(db)["companies"] == []


def test_the_weekly_observation_is_one_fact_with_a_small_sample_guard(ctx, db):
    approve_profile(db, ctx, countries="Netherlands")
    ids = hunt.import_postings(ctx, db, [_posting(f"nl{i}", "Platform Engineer", employer_name=f"Employer {i}", url=f"https://job-boards.greenhouse.io/e{i}/jobs/{i}") for i in range(1, 10)])["job_ids"]
    assert watches.observation_due(db)
    obs = watches.weekly_observation(ctx, db, mark=False)
    assert obs["kind"] is None and obs["text"] is None  # nothing to say yet, nothing invented
    for jid in ids[:2]:
        drafts_svc.prepare(ctx, db, jid)
        actions.mark_applied(db, jid, cv_choice="unknown")
    obs = watches.weekly_observation(ctx, db, mark=False)
    assert obs["kind"] == "early" and "too few to conclude" in obs["text"]
    for jid in ids[2:5]:
        drafts_svc.prepare(ctx, db, jid)
        actions.mark_applied(db, jid, cv_choice="unknown")
    actions.set_stage(db, ids[0], "interview")
    obs = watches.weekly_observation(ctx, db, mark=False)
    assert obs["kind"] == "replies" and "5 applications, 1 with a reply" in obs["text"] and "not a verdict" in obs["text"]
    # Unopened prepared packs outrank the reply fact.
    for jid in ids[5:8]:
        drafts_svc.prepare(ctx, db, jid)
    assert len(today_svc.unopened_ready(db)) == 3
    obs = watches.weekly_observation(ctx, db, mark=False)
    assert obs["kind"] == "unopened" and "3 prepared applications are waiting unopened" in obs["text"]
    # Today carries it once a week; after it is counted, not again until next week.
    t = today_svc.today(ctx, db)
    assert t["weekly"] and t["weekly"]["text"] == obs["text"]
    watches.weekly_observation(ctx, db, mark=True)
    assert not watches.observation_due(db) and today_svc.today(ctx, db)["weekly"] is None
