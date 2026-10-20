"""Four candidates against the same realistic vacancy set: a beginner, a career changer, a mid-level and a
senior engineer. Checks that adjacent roles are classified by the work, that explanations name the
requirement and the evidence, that missing evidence and missing experience are told apart, and that
country and work-arrangement restrictions are exact for everyone."""

from __future__ import annotations

import json

import pytest

from jobhunter.models import RawPosting
from jobhunter.services import hunt, views
from jobhunter.services import preferences as prefs_svc
from jobhunter.services.normalize import classify_relevance
from tests.conftest import approve_profile
from tests.test_matching_matrix import CAREER_CHANGER_CV, MID_CV, SENIOR_CV

BEGINNER_CV = """Jamie Start
Rotterdam, Netherlands

Education
KubeCraft DevOps programme, 2026 (course)

Projects
Homelab: three-node k3s cluster with Argo CD and Prometheus, following the KubeCraft course.
Deployed a small web app with Docker and GitHub Actions.

Skills
Kubernetes, Docker, Linux, Git, GitHub Actions
"""

INFRA = (
    "About the role\nYou run our production platform.\nWhat you bring\nKubernetes in production, Terraform, AWS, Linux, "
    "Prometheus and Grafana, CI/CD with GitHub Actions. On-call once a month.\nWhat you'll do\nOwn the clusters, automate everything."
)
SOFTWARE = (
    "About the role\nYou build features for our customer app.\nWhat you bring\nJava, Spring Boot, TypeScript, React, REST APIs, unit tests, "
    "microservices. You ship frontend and backend code every week."
)
SECOPS = "You run the SOC: triage alerts, threat hunting, incident response, SIEM tuning. Linux and AWS knowledge welcome. Kubernetes a plus."

VACANCIES = [
    ("junior", dict(title="Junior DevOps Engineer", description_text="Entry level. You learn Kubernetes, Terraform and Linux with us. Docker, Git, GitHub Actions.")),
    ("mid", dict(title="Platform Engineer", description_text=INFRA)),
    ("senior", dict(title="Senior Site Reliability Engineer", description_text="7+ years running production Kubernetes. " + INFRA)),
    ("head", dict(title="Head of Platform Engineering", description_text="You lead three teams, own the budget and the roadmap. " + INFRA)),
    ("secops", dict(title="Security Operations Engineer", description_text=SECOPS)),
    ("swe_platform_team", dict(title="Software Engineer - Platform", description_text=SOFTWARE)),
    ("platform_title_software_work", dict(title="Platform Engineer", description_text=SOFTWARE)),
    ("us_only", dict(title="Platform Engineer", location_text="Remote", country_codes=[], remote_type="remote", description_text=INFRA + "\nApplicants must be based in the US.")),
    ("berlin_hybrid", dict(title="Platform Engineer", location_text="Berlin, Germany", country_codes=["DE"], remote_type="hybrid", description_text=INFRA)),
    ("office_only", dict(title="DevOps Engineer", remote_type="onsite", description_text=INFRA)),
]


def _seed(ctx, db):
    posts = []
    for ext, kw in VACANCIES:
        base = dict(source_id="t", external_id=ext, employer_name=f"Employer {ext}", url=f"https://job-boards.greenhouse.io/x/jobs/{ext}", location_text="Amsterdam, Netherlands", country_codes=["NL"], remote_type="hybrid", posted_at="2026-10-17T09:00:00+00:00", posted_at_provenance="t", is_employer_hosted=True)
        base.update(kw)
        posts.append(RawPosting(**base))
    ids = hunt.import_postings(ctx, db, posts)["job_ids"]
    return dict(zip([v[0] for v in VACANCIES], ids, strict=True))


def _match(db, jid):
    row = db.fetchone(
        "SELECT m.match_group, m.score, m.gaps_json, m.reasons_json, m.exclusion_reason, m.explanation_text, j.relevance, j.relevance_reason FROM jobs j "
        "LEFT JOIN matches m ON m.job_id = j.id AND m.profile_version_id = (SELECT id FROM profile_versions WHERE status = 'approved' ORDER BY version DESC LIMIT 1) WHERE j.id = ?",
        (jid,),
    )
    row["gaps"] = json.loads(row.get("gaps_json") or "[]")
    row["reasons"] = [r["text"] for r in json.loads(row.get("reasons_json") or "[]")]
    return row


def test_adjacent_roles_are_classified_by_the_work():
    assert classify_relevance("Head of Platform Engineering", INFRA)[0] == "uncertain"  # a stretch, decided by leadership evidence
    assert classify_relevance("Engineering Manager, Infrastructure", INFRA)[0] == "uncertain"
    assert classify_relevance("Head of Marketing", "Own the brand and the campaigns.")[0] == "irrelevant"
    assert classify_relevance("Lead Platform Engineer", INFRA)[0] == "relevant"
    assert classify_relevance("Security Operations Engineer", SECOPS)[0] == "uncertain"
    assert classify_relevance("Security Operations Engineer", "Triage alerts in the SIEM. Write reports.")[0] == "irrelevant"
    assert classify_relevance("DevSecOps Engineer", INFRA)[0] == "relevant"
    assert classify_relevance("Software Engineer - Platform", SOFTWARE)[0] == "irrelevant"
    assert classify_relevance("Software Engineer - Platform", INFRA)[0] == "uncertain"
    rel, why = classify_relevance("Platform Engineer", SOFTWARE)
    assert rel == "uncertain" and "software development" in why
    assert classify_relevance("Platform Engineer", INFRA)[0] == "relevant"


@pytest.mark.parametrize(
    "name, cv, expect",
    [
        ("beginner", BEGINNER_CV, {"junior": {"good", "worth_a_shot"}, "mid": {"worth_a_shot", "backup"}, "senior": {"excluded"}}),
        ("career_changer", CAREER_CHANGER_CV, {"junior": {"good", "worth_a_shot"}, "mid": {"worth_a_shot", "backup"}, "senior": {"excluded"}}),
        ("mid", MID_CV, {"junior": {"worth_a_shot", "backup", "good"}, "mid": {"good"}, "senior": {"good", "worth_a_shot"}}),
        ("senior", SENIOR_CV, {"mid": {"good", "worth_a_shot"}, "senior": {"good"}}),
    ],
)
def test_each_candidate_gets_a_defensible_result_on_the_same_vacancies(ctx, db, name, cv, expect):
    approve_profile(db, ctx, cv_text=cv, countries="Netherlands")
    prefs = prefs_svc.get(db)
    prefs.remote = "preferred"
    prefs.work_modes = ["remote", "hybrid"]  # never fully in the office
    prefs_svc.save(db, prefs, reason="test")
    jobs = _seed(ctx, db)
    for key, groups in expect.items():
        m = _match(db, jobs[key])
        assert m["match_group"] in groups, (name, key, m["match_group"], m["exclusion_reason"], m["gaps"])
    # Everyone: a software job on a platform team, US-only remote, Germany and office-only are never recommended.
    for key in ("swe_platform_team", "us_only", "berlin_hybrid", "office_only"):
        m = _match(db, jobs[key])
        assert m["match_group"] in {None, "excluded"}, (name, key, m)
    # Head of Platform: a credible stretch only for the senior engineer who led a team; everyone else sees why not.
    head = _match(db, jobs["head"])
    if name == "senior":
        assert head["match_group"] in {"good", "worth_a_shot", "backup"}, head
    else:
        assert head["match_group"] in {None, "excluded"} and "leading a team" in (head["exclusion_reason"] or ""), (name, head)
    assert "US" in _match(db, jobs["us_only"])["exclusion_reason"] or "United States" in _match(db, jobs["us_only"])["exclusion_reason"]
    assert "Germany" in _match(db, jobs["berlin_hybrid"])["exclusion_reason"]
    assert "office" in _match(db, jobs["office_only"])["exclusion_reason"].lower()
    # Security operations and "platform title, software work" are shown as needs-checking at most, never Good.
    # Ambiguous work (security operations, an infrastructure title with a software description) is never
    # recommended as if sure: it is "needs checking" when the fit is there, otherwise left out with the reason.
    rec_ids = {i["id"] for i in views.current_recommended(db)}
    checking = {i["id"] for i in views.list_jobs(db, view="uncertain", per_page=50)["items"]}
    for key in ("secops", "platform_title_software_work"):
        m = _match(db, jobs[key])
        assert m["relevance"] == "uncertain" and m["match_group"] in {"uncertain", "excluded"}, (name, key, m)
        assert jobs[key] not in rec_ids and (jobs[key] in checking or m["match_group"] == "excluded")
    # Explanations name the requirement and the evidence for a recommended job.
    rec = {i["id"]: i for i in views.current_recommended(db)}
    assert rec, name
    for it in rec.values():
        expl = (it["explanation"] or "") + " " + " ".join(r.get("text", "") for r in it["reasons"])
        assert any(w in expl for w in ("They ask for", "direction you chose", "infrastructure work", "Check what the work")), (name, it["title"], expl)


def test_missing_evidence_is_told_apart_from_missing_experience(ctx, db):
    # The CV mentions Terraform, but the approved profile (for example one Claude refined) does not carry it as a skill.
    approve_profile(db, ctx, cv_text=MID_CV, countries="Netherlands")
    from jobhunter.services import profile as profile_svc

    row = profile_svc.approved(db)
    kept = [f for f in row["facts"] if not (f.category == "skill" and "terraform" in f.text.lower())]
    assert len(kept) < len(row["facts"])
    db.update("profile_versions", {"facts_json": json.dumps([f.model_dump(mode="json") for f in kept])}, "id = ?", (row["id"],))
    assert not any(f.category == "skill" and "terraform" in f.text.lower() for f in profile_svc.approved(db)["facts"])
    jobs = _seed(ctx, db)
    m = _match(db, jobs["mid"])
    gaps = " ".join(m["gaps"])
    assert "Terraform" in gaps and "not in your approved profile yet" in gaps, m["gaps"]
    assert "Your CV mentions" in gaps
    m2 = _match(db, jobs["secops"])  # SIEM is not in the CV at all
    assert m2["relevance"] == "uncertain"
