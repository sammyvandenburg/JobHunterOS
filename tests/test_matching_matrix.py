"""Matching pipeline matrix (JH-01..06, 11, 19): four candidates x complete, borderline, irrelevant,
incomplete and misleading jobs. Accurate, not merely stricter: credible stretches stay, decoys go,
unresolved locations are separated, and every surface shares one selection."""

from __future__ import annotations

import json

from jobhunter.models import RawPosting
from jobhunter.services import actions, hunt, notifications, stats, views
from jobhunter.services import preferences as prefs_svc
from jobhunter.services import profile as profile_svc
from jobhunter.services.matching import engine
from jobhunter.services.matching.scoring import candidate_skills, job_tools
from tests.conftest import SAMPLE_CV, approve_profile

CAREER_CHANGER_CV = """Robin Switch
Utrecht, Netherlands

Experience
Registered Nurse, City Hospital — Jan 2010 to Aug 2024
- Patient care, shift lead for the night team, medication rounds.
IT Support Technician, Local Clinic — Sep 2024 to Present
- First-line helpdesk; reset passwords; set up laptops.

Projects
Homelab Kubernetes cluster (k3s) with GitOps via Argo CD, following the KubeCraft course.

Skills
Kubernetes (AKS, k3s), Linux, Git
"""

MID_CV = """Casey Mid
Rotterdam, Netherlands

Experience
DevOps Engineer, Fintech BV — Feb 2023 to Present
- Own the Terraform modules and CI/CD pipelines for 30 services on AWS EKS; on-call rotation; Prometheus and Grafana.
Junior Cloud Engineer, Agency NL — Jan 2022 to Jan 2023
- Deployed containers to Kubernetes; wrote Ansible playbooks; Linux servers.

Skills
Kubernetes, Terraform, AWS, Docker, Ansible, Linux, Python, CI/CD, Prometheus, Grafana, Git
"""

SENIOR_CV = """Alex Senior
Amsterdam, Netherlands

Experience
Lead Platform Engineer, ScaleCo — Mar 2020 to Present
- Led a team of 5 engineers; owned the production Kubernetes platform on AWS and GCP; designed the observability stack; on-call escalation.
Site Reliability Engineer, Bank NL — Jan 2017 to Feb 2020
- Ran incident response and reliability for payment systems; Terraform, Linux, Prometheus.

Skills
Kubernetes, Terraform, AWS, GCP, Helm, Argo CD, Linux, Go, Python, Prometheus, Grafana, CI/CD, Git

Certifications
Certified Kubernetes Administrator (CKA), 2021
"""

FULL_DESC = (
    "About the role\nYou join a small platform team that builds and runs the internal developer platform.\n"
    "What you bring\nExperience running Kubernetes in production\nTerraform and AWS\nCI/CD with GitHub Actions\nLinux\n"
    "What you'll do\nOwn the platform\nBe on call\nWe run 40 services for 2 million users."
)


def _job(ctx, db, ext, **kw):
    base = dict(source_id="t", external_id=ext, title="Platform Engineer", employer_name=f"Employer {ext}", url=f"https://job-boards.greenhouse.io/x/jobs/{ext}", location_text="Amsterdam, Netherlands", country_codes=["NL"], description_text=FULL_DESC, posted_at="2026-10-17T09:00:00+00:00", posted_at_provenance="t", is_employer_hosted=True)
    base.update(kw)
    return hunt.import_postings(ctx, db, [RawPosting(**base)])["job_ids"][0]


def _m(db, jid):
    row = db.fetchone(
        "SELECT m.match_group, m.score, m.gaps_json, m.reasons_json, m.exclusion_reason, j.relevance FROM jobs j "
        "LEFT JOIN matches m ON m.job_id = j.id AND m.profile_version_id = (SELECT id FROM profile_versions WHERE status = 'approved' ORDER BY version DESC LIMIT 1) WHERE j.id = ?",
        (jid,),
    )
    row["gaps"] = json.loads(row.get("gaps_json") or "[]")
    row["reasons"] = [r["text"] for r in json.loads(row.get("reasons_json") or "[]")]
    return row


def _jobs_matrix(ctx, db):
    return {
        "good": _job(ctx, db, "good"),
        "senior": _job(ctx, db, "senior", title="Senior Site Reliability Engineer", description_text="About the role\nYou keep our payment platform reliable and mentor the on-call rotation.\nWhat you bring\n7+ years running production Kubernetes. Terraform, AWS, Linux, Prometheus. You lead incident reviews and own reliability targets."),
        "junior": _job(ctx, db, "junior", title="Junior DevOps Engineer", description_text="Entry level. You will learn Kubernetes, Terraform and Linux with us. Docker, Git."),
        "agent": _job(ctx, db, "agent", title="Passenger Services Platform Agent", description_text="Help passengers at the platform. Customer service, tickets, announcements."),
        "power": _job(ctx, db, "power", title="Power Platform Consultant", description_text="Build Power Apps and Power Automate flows for clients. Dynamics 365."),
        "laravel": _job(ctx, db, "laravel", title="Laravel Developer (B2B platform)", description_text="PHP, Laravel, MySQL, Vue. Build features for our B2B platform."),
        "rf": _job(ctx, db, "rf", title="RF Communication Systems Engineer", description_text="Design RF front ends, antennas, link budgets. MATLAB."),
        "empty": _job(ctx, db, "empty", title="Senior Platform Engineer", description_text=""),
        "us_only": _job(ctx, db, "us_only", location_text="Remote worldwide", country_codes=[], remote_type="remote", description_text=FULL_DESC + "\nApplicants must be based in the US."),
        "lisbon": _job(ctx, db, "lisbon", location_text="Remote", country_codes=[], remote_type="remote", description_text=FULL_DESC + "\nYou will spend two days a week in our Lisbon office."),
        "vague": _job(ctx, db, "vague", location_text="Remote", country_codes=[], remote_type="remote", description_text=FULL_DESC),
        "worldwide": _job(ctx, db, "worldwide", location_text="Remote worldwide", country_codes=[], remote_type="remote", description_text=FULL_DESC),
        "berlin": _job(ctx, db, "berlin", location_text="Berlin, Germany", country_codes=["DE"]),
        "backend": _job(ctx, db, "backend", title="Backend Engineer", description_text="You will own our Kubernetes clusters, Terraform modules, AWS accounts and CI/CD pipelines, with Prometheus monitoring. Linux."),
        "laws": _job(ctx, db, "laws", description_text="We run digital products under local laws. Kubernetes and Terraform on AWS. Linux. CI/CD."),
    }


# ---------------------------------------------------------------- JH-01: experience by what the work was
def test_career_changer_is_not_a_senior_engineer(ctx, db):
    prof = approve_profile(db, ctx, cv_text=CAREER_CHANGER_CV, countries="Netherlands")
    t = prof["targets"]
    assert t["professional_years_total"] > 14 and t["professional_years_relevant"] == 0 and 1.5 <= t["professional_years_adjacent"] <= 2.5
    assert t["level"] in {"entry", "junior"} and "different career" in (t["experience_note"] or "")
    jobs = _jobs_matrix(ctx, db)
    assert _m(db, jobs["senior"])["match_group"] == "excluded" and "not infrastructure work" in _m(db, jobs["senior"])["exclusion_reason"]
    assert _m(db, jobs["empty"])["match_group"] == "excluded"
    g = _m(db, jobs["good"])
    assert g["match_group"] in {"worth_a_shot", "backup"}, g  # a credible stretch, shown with its gaps, never "Good"
    assert _m(db, jobs["junior"])["match_group"] in {"good", "worth_a_shot"}


def test_it_support_with_infrastructure_bullets_counts_as_infrastructure_work(ctx, db):
    prof = approve_profile(db, ctx, cv_text=SAMPLE_CV, countries="Netherlands")
    t = prof["targets"]
    assert 4 <= t["professional_years_relevant"] <= 7 and t["level"] == "mid", t
    assert t["responsibility"] in {"owner", "contributor"}  # managed servers, not people
    roles = [f for f in prof["facts"] if f.category == "role"]
    assert {profile_svc.role_domain(f) for f in roles} >= {"infra_adjacent"}


def test_mid_and_senior_candidates_get_different_defensible_rankings(ctx, db):
    approve_profile(db, ctx, cv_text=MID_CV, countries="Netherlands")
    jobs = _jobs_matrix(ctx, db)
    mid = {k: _m(db, v)["match_group"] for k, v in jobs.items()}
    assert mid["good"] == "good" and mid["senior"] in {"worth_a_shot", "backup"} and mid["junior"] in {"good", "worth_a_shot", "backup"}
    # Senior candidate (a new CV replaces the profile): same jobs, different ranking.
    approve_profile(db, ctx, cv_text=SENIOR_CV, countries="Netherlands")
    engine.run_matching(ctx, db)
    senior = {k: _m(db, v)["match_group"] for k, v in jobs.items()}
    assert senior["senior"] == "good" and senior["good"] in {"good", "worth_a_shot"}
    assert senior["junior"] != "good" and any("below" in g.lower() for g in _m(db, jobs["junior"])["gaps"])
    prof = profile_svc.approved(db)
    assert prof["targets"]["level"] == "senior" and prof["targets"]["responsibility"] == "lead" and prof["targets"]["professional_years_relevant"] >= 9


# ---------------------------------------------------------------- JH-02: titles and words
def test_decoy_titles_are_out_but_unusual_infra_titles_stay_as_uncertain(ctx, db):
    approve_profile(db, ctx, cv_text=MID_CV, countries="Netherlands")
    jobs = _jobs_matrix(ctx, db)
    for k in ("agent", "power", "laravel", "rf"):
        m = _m(db, jobs[k])
        assert m["relevance"] == "irrelevant" and m["match_group"] in {None, "excluded"}, (k, m)
    b = _m(db, jobs["backend"])
    assert b["relevance"] == "uncertain" and b["match_group"] == "uncertain"  # shown under Needs checking, never as a sure match
    assert job_tools("We run digital products under local laws", "") == set()
    assert "git" not in _m(db, jobs["laws"])["reasons"][0].lower() or "aws" in _m(db, jobs["laws"])["reasons"][0].lower()
    e = _m(db, jobs["empty"])
    assert e["match_group"] != "good" and any("almost no text" in g for g in e["gaps"])


def test_skills_canonical_for_parser_and_structured_paths_and_aks_is_not_broad_azure(ctx, db):
    from jobhunter.models import EvidenceRef, Fact

    parsed = approve_profile(db, ctx, cv_text=CAREER_CHANGER_CV, countries="Netherlands")
    keys = set(candidate_skills(parsed["facts"]))
    assert {"kubernetes", "aks", "linux", "git"} <= keys and "azure" not in keys
    # The same phrase as one Claude-style fact behaves the same way.
    phrase = [Fact(id="s1", category="skill", text="Kubernetes (AKS, k3s)", confidence="explicit", evidence=[EvidenceRef(type="document", ref="document:1", quote="Kubernetes (AKS, k3s)")])]
    assert set(candidate_skills(phrase)) == {"kubernetes", "aks"}
    jid = _job(ctx, db, "az", description_text="Kubernetes on Azure. Terraform. Linux. Git. CI/CD.")
    m = _m(db, jid)
    assert any("Azure" in g and "AKS" in g and ("related" in g or "not Azure" in g) for g in m["gaps"]), m["gaps"]


# ---------------------------------------------------------------- JH-04: location
def test_location_rules_for_a_netherlands_hunt(ctx, db):
    approve_profile(db, ctx, cv_text=MID_CV, countries="Netherlands")
    jobs = _jobs_matrix(ctx, db)
    us = _m(db, jobs["us_only"])
    assert us["match_group"] == "excluded" and "US" in us["exclusion_reason"] and "Netherlands" in us["exclusion_reason"]
    assert _m(db, jobs["berlin"])["match_group"] == "excluded"
    for k in ("lisbon", "vague"):
        m = _m(db, jobs[k])
        assert m["match_group"] == "uncertain", (k, m)
    assert _m(db, jobs["worldwide"])["match_group"] in {"good", "worth_a_shot"}
    new_ids = {i["id"] for i in views.list_jobs(db, view="new", per_page=50)["items"]}
    assert jobs["good"] in new_ids and jobs["worldwide"] in new_ids
    assert not ({jobs["us_only"], jobs["lisbon"], jobs["vague"], jobs["berlin"]} & new_ids)
    check_ids = {i["id"] for i in views.list_jobs(db, view="uncertain", per_page=50)["items"]}
    assert {jobs["lisbon"], jobs["vague"]} <= check_ids
    assert views.list_jobs(db, view="uncertain")["items"][0]["group_label"] == "Needs checking"


def test_work_modes_are_expressible(ctx, db):
    approve_profile(db, ctx, cv_text=MID_CV, countries="Netherlands")
    onsite = _job(ctx, db, "o1", location_text="Amsterdam, Netherlands", remote_type="onsite")
    hybrid = _job(ctx, db, "h1", location_text="Amsterdam, Netherlands", remote_type="hybrid")
    remote = _job(ctx, db, "r1", location_text="Remote - Netherlands", remote_type="remote")
    from jobhunter.services.onboarding import parse_work_modes

    assert parse_work_modes("remote preferred, hybrid is fine, never fully on-site") == ("preferred", ["remote", "hybrid"])
    assert parse_work_modes("remote only") == ("required", ["remote"])
    assert parse_work_modes("don't mind") == ("no_preference", ["remote", "hybrid", "onsite"])
    p = prefs_svc.get(db)
    p.remote, p.work_modes = "preferred", ["remote", "hybrid"]
    prefs_svc.save(db, p, reason="test")
    engine.run_matching(ctx, db)
    assert _m(db, onsite)["match_group"] == "excluded" and "office" in _m(db, onsite)["exclusion_reason"]
    assert _m(db, hybrid)["match_group"] != "excluded" and _m(db, remote)["match_group"] != "excluded"


# ---------------------------------------------------------------- JH-03/05: corrections and invalidation
def test_profile_correction_supersedes_facts_and_refreshes_matches_without_a_scrape(ctx, db):
    approve_profile(db, ctx, cv_text=MID_CV, countries="Netherlands")
    jid = _job(ctx, db, "c1")
    before = _m(db, jid)
    assert any("AWS" in r for r in before["reasons"])
    v1 = profile_svc.approved(db)
    res = profile_svc.correct(db, "I do not have AWS experience")
    assert res["approved"] and "aws" in [t.lower() for t in res["retracted"]]
    v2 = profile_svc.approved(db)
    assert v2["id"] != v1["id"] and v2["version"] > v1["version"]
    assert all(f.status == "rejected" for f in v2["facts"] if f.category == "skill" and f.text == "aws")
    engine.run_matching(ctx, db)
    after = _m(db, jid)
    assert not any("AWS" in r.split("Your CV shows")[-1] for r in after["reasons"]) and any("AWS" in g for g in after["gaps"]), (after["reasons"], after["gaps"])
    # Adding a skill works too, and is evidenced by the person's words.
    res2 = profile_svc.correct(db, "I also use Pulumi at work")
    assert "pulumi" in res2["added"]
    # Country change: no re-import, matches refresh.
    p = prefs_svc.get(db)
    p.work_countries = ["DE"]
    prefs_svc.save(db, p, reason="test")
    engine.run_matching(ctx, db)
    assert _m(db, jid)["match_group"] == "excluded" and "Germany" in _m(db, jid)["exclusion_reason"]


def test_reclassified_job_leaves_the_lists_and_changed_fields_update_the_job(ctx, db):
    approve_profile(db, ctx, cv_text=MID_CV, countries="Netherlands")
    jid = _job(ctx, db, "u1")
    assert jid in {i["id"] for i in views.list_jobs(db, view="new")["items"]}
    db.execute("UPDATE jobs SET relevance = 'irrelevant', relevance_reason = 'Reclassified for the test.' WHERE id = ?", (jid,))
    engine.run_matching(ctx, db)
    assert jid not in {i["id"] for i in views.list_jobs(db, view="new")["items"]}
    assert _m(db, jid)["match_group"] == "excluded"
    # Same text, different provider fields: the job row follows the observation.
    j2 = _job(ctx, db, "u2", apply_url="https://apply.example/1")
    again = _job(ctx, db, "u2", apply_url="https://apply.example/2", country_codes=["NL", "BE"], remote_type="hybrid")
    assert again == j2
    row = db.fetchone("SELECT apply_url, remote_type, country_codes_json FROM jobs WHERE id = ?", (j2,))
    assert row["apply_url"] == "https://apply.example/2" and row["remote_type"] == "hybrid" and "BE" in row["country_codes_json"]
    # A change far into a long description is still a change.
    long_desc = FULL_DESC + ("\nfiller line about the team culture." * 300)
    j3 = _job(ctx, db, "u3", description_text=long_desc + "\nMust have Pulumi.")
    _job(ctx, db, "u3", description_text=long_desc + "\nMust have Crossplane.")
    assert "Crossplane" in db.fetchone("SELECT description_text FROM jobs WHERE id = ?", (j3,))["description_text"]


# ---------------------------------------------------------------- JH-06: dedupe
def test_dedupe_keeps_distinct_jobs_apart_and_merges_tracking_variants(ctx, db):
    approve_profile(db, ctx, cv_text=MID_CV, countries="Netherlands")
    a = _job(ctx, db, "d1", url="https://careers.example.com/jobs?id=100", is_employer_hosted=True)
    b = _job(ctx, db, "d2", url="https://careers.example.com/jobs?id=200", is_employer_hosted=True)
    assert a != b
    c = _job(ctx, db, "d3", url="https://careers.example.com/jobs?id=100&utm_source=linkedin&utm_medium=email", employer_name="Employer d1", is_employer_hosted=True)
    assert c == a
    ams = _job(ctx, db, "d4", employer_name="Multi Office BV", location_text="Amsterdam, Netherlands", url="https://multi.example/a")
    rot = _job(ctx, db, "d5", employer_name="Multi Office BV", location_text="Rotterdam, Netherlands", url="https://multi.example/b")
    assert ams != rot
    repost = _job(ctx, db, "d6", employer_name="Multi Office BV", location_text="Amsterdam, Netherlands", url="https://multi.example/a?utm_campaign=x")
    assert repost == ams


# ---------------------------------------------------------------- JH-11/19: one selection everywhere
def test_dashboard_cli_digest_and_recommended_count_agree(ctx, db):
    approve_profile(db, ctx, cv_text=MID_CV, countries="Netherlands")
    jobs = _jobs_matrix(ctx, db)
    actions.mark_applied(db, jobs["good"])
    actions.dismiss_job(db, jobs["junior"], reason_code="other")
    actions.dismiss_job(db, jobs["junior"], undo=True)
    notifications.set_recipient(db, "me@example.com")
    dash = {i["id"] for i in views.list_jobs(db, view="new", per_page=100)["items"]}
    digest = [r["id"] for r in notifications.pending_jobs(db, limit=100)]
    assert set(digest) <= dash and len(digest) == len(set(digest))
    assert jobs["good"] not in digest and jobs["junior"] in digest and jobs["us_only"] not in digest and jobs["lisbon"] not in digest
    t = stats.totals(db)
    assert t["recommended"] == len(dash) and t["applied"] == 1
    d = views.dashboard(db)
    assert d["totals"]["recommended"] == len(dash) and {j["id"] for j in d["recommended"]} <= dash
    # A new profile version makes the digest and dashboard move together.
    profile_svc.correct(db, "I do not have Kubernetes experience")
    engine.run_matching(ctx, db)
    dash2 = {i["id"] for i in views.list_jobs(db, view="new", per_page=100)["items"]}
    digest2 = {r["id"] for r in notifications.pending_jobs(db, limit=100)}
    assert digest2 <= dash2 and stats.totals(db)["recommended"] == len(dash2)


# ---------------------------------------------------------------- obvious matching errors seen on real postings
def test_software_roles_on_platform_teams_and_ml_platform_jobs_are_never_good_matches(ctx, db):
    approve_profile(db, ctx, cv_text=MID_CV, countries="Netherlands")
    infra = FULL_DESC + "\nKubernetes, Terraform, AWS, Linux, Prometheus, Grafana, CI/CD, Docker, Ansible."
    jobs = {
        "fullstack": _job(ctx, db, "fs", title="Full-Stack Engineer (Backend Leaning) - Platform Engineering", description_text=infra),
        "swe_cloud": _job(ctx, db, "swc", title="Software Engineer, Cloud", description_text=infra),
        "ml_platform": _job(ctx, db, "mlp", title="Machine Learning Platform Engineer I", description_text=infra),
        "platform": _job(ctx, db, "pe", title="Platform Engineer - Developer Experience", description_text=infra),
        "devops_team": _job(ctx, db, "dv", title="DevOps Engineer (Payments team)", description_text=infra),
        "no_overlap": _job(ctx, db, "nov", title="Platform Engineer", description_text="About the role\nYou run our Windows fleet with SCCM and PowerShell, Citrix and Exchange. Five years with Microsoft products."),
    }
    fs = _m(db, jobs["fullstack"])
    assert fs["relevance"] == "uncertain" and fs["match_group"] == "uncertain", fs
    swc = _m(db, jobs["swe_cloud"])
    assert swc["relevance"] == "uncertain" and swc["match_group"] != "good", swc
    ml = _m(db, jobs["ml_platform"])
    assert ml["relevance"] == "uncertain" and ml["match_group"] != "good" and "machine-learning" in ml["reasons"][0] + str(ml), ml
    for k in ("platform", "devops_team"):
        m = _m(db, jobs[k])
        assert m["relevance"] == "relevant" and m["match_group"] == "good", (k, m)
        assert m["reasons"][0].startswith("They ask for") and "Your CV shows" in m["reasons"][0], m["reasons"]
        assert any("direction you chose" in r for r in m["reasons"]), m["reasons"]
    weak = _m(db, jobs["no_overlap"])
    assert weak["relevance"] == "relevant" and weak["match_group"] != "good", weak
    # The one selection every surface uses agrees: software titles are not among the Good matches.
    good = {i["id"] for i in views.list_jobs(db, view="new", group="good", per_page=50)["items"]}
    assert jobs["platform"] in good and jobs["fullstack"] not in good and jobs["ml_platform"] not in good
