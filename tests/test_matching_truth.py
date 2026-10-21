"""Matching truth: exact tools are told from related ones, requirements are read by what the posting means
(essential, nice to have, incidental, negated), the work decides over the title, languages and contract type
are visible checks, work authorization is shown as stated and never inferred, and every explanation names
the evidence kind (paid work, project, course, skills list)."""

from __future__ import annotations

import json

import pytest

from jobhunter.models import RawPosting
from jobhunter.services import hunt, normalize, views
from jobhunter.services import profile as profile_svc
from jobhunter.services.matching import requirements
from jobhunter.services.normalize import classify_relevance
from tests.conftest import approve_profile
from tests.test_matching_matrix import CAREER_CHANGER_CV, FULL_DESC, MID_CV, SENIOR_CV

INFRA_PLAIN = "You run production Kubernetes clusters on AWS with Terraform, Linux, Prometheus, Grafana, Helm and CI/CD pipelines. On-call once a month."


def _seed(ctx, db, ext, **kw):
    base = dict(source_id="t", external_id=ext, title="Platform Engineer", employer_name=f"Employer {ext}", url=f"https://job-boards.greenhouse.io/x/jobs/{ext}", location_text="Rotterdam, Netherlands", country_codes=["NL"], remote_type="hybrid", description_text=INFRA_PLAIN, posted_at="2026-10-19T09:00:00+00:00", posted_at_provenance="t", is_employer_hosted=True)
    base.update(kw)
    return hunt.import_postings(ctx, db, [RawPosting(**base)])["job_ids"][0]


def _m(db, jid):
    row = db.fetchone(
        "SELECT m.match_group, m.gaps_json, m.reasons_json, m.exclusion_reason, m.explanation_text, j.relevance FROM jobs j "
        "LEFT JOIN matches m ON m.job_id = j.id AND m.profile_version_id = (SELECT id FROM profile_versions WHERE status = 'approved' ORDER BY version DESC LIMIT 1) WHERE j.id = ?",
        (jid,),
    )
    row["gaps"] = json.loads(row.get("gaps_json") or "[]")
    row["reasons"] = [r["text"] for r in json.loads(row.get("reasons_json") or "[]")]
    return row


# ---------------------------------------------------------------- exact vs related tools
@pytest.mark.parametrize(
    "cv, asked, have, expect_gap",
    [
        (MID_CV, "Datadog for monitoring and alerting", "Prometheus", "They use Datadog. Your profile shows Prometheus, not Datadog."),
        (SENIOR_CV, "GitOps with Flux", "Argo CD", "They use Flux. Your profile shows Argo CD, not Flux."),
        (MID_CV + "VMware vSphere\n", "Proxmox virtualisation", "VMware", "They use Proxmox. Your profile shows VMware, not Proxmox."),
        (MID_CV, "Infrastructure as code with Pulumi", "Terraform", "They use Pulumi. Your profile shows Terraform, not Pulumi."),
    ],
)
def test_a_related_tool_is_never_counted_as_the_tool_itself(ctx, db, cv, asked, have, expect_gap):
    approve_profile(db, ctx, cv_text=cv, countries="Netherlands")
    jid = _seed(ctx, db, "rel", description_text="You run production Kubernetes on AWS with Terraform and Linux. " + asked + ". On-call once a month.")
    m = _m(db, jid)
    assert m["match_group"] in {"good", "worth_a_shot", "backup"}, m
    assert expect_gap in m["gaps"], m["gaps"]
    assert not any(expect_gap.split(" ")[2].rstrip(".") in r.split("Your CV shows")[-1] for r in m["reasons"] if "Your CV shows" in r), m["reasons"]


# ---------------------------------------------------------------- requirement parsing
REQ_DESC = """About us
We run payments for 2 million users. Two years ago we moved everything off VMware onto the cloud.

What you bring
- Kubernetes in production
- Terraform
- Linux

Nice to have
- Pulumi
- Go (Golang)

No Azure experience required: we are an AWS shop.
"""


def test_requirements_are_read_as_essential_preferred_incidental_or_negated():
    r = requirements.parse(REQ_DESC)
    assert {"kubernetes", "terraform", "linux"} <= r.essential
    assert {"pulumi", "go"} <= r.preferred and not ({"pulumi", "go"} & r.essential)
    assert "azure" in r.negated and "azure" not in r.essential and "azure" not in r.preferred
    assert "vmware" in r.incidental and "vmware" not in r.essential
    assert "aws" in r.essential or "aws" in r.incidental  # "we are an AWS shop" is context, never a negation of AWS


def test_scoring_follows_the_requirement_kinds(ctx, db):
    approve_profile(db, ctx, cv_text=MID_CV, countries="Netherlands")
    jid = _seed(ctx, db, "req", description_text=REQ_DESC)
    m = _m(db, jid)
    assert m["match_group"] == "good", m
    gaps = " ".join(m["gaps"])
    assert "Nice to have" in gaps and "Pulumi" in gaps and "Go" in gaps
    assert "Azure" not in gaps and "VMware" not in gaps
    assert not any("They also ask for" in g and ("Pulumi" in g or "Go" in g) for g in m["gaps"])


# ---------------------------------------------------------------- the work decides, not the title
def test_responsibilities_outrank_title_words():
    assert classify_relevance("Software Engineer, Infrastructure", "Own the Kubernetes platform on AWS: Terraform, Helm, Linux hosts, Prometheus alerting, CI/CD pipelines, on-call rotation. Go for internal tooling.")[0] == "relevant"
    assert classify_relevance("Machine Learning Platform Engineer", "Run GPU clusters on Kubernetes with Terraform and Linux; CUDA drivers, Prometheus, on-call; no model training.")[0] in {"relevant", "uncertain"}
    rel, why = classify_relevance("Platform Engineer", "Build React and TypeScript front-end features with REST APIs, unit tests and microservices for our customer app. You ship front-end and back-end code every week with the product team.")
    assert rel != "relevant" and "software" in why.lower()
    assert classify_relevance("Automation Engineer", "PLC programming with Siemens TIA Portal, SCADA systems, industrial control systems and OT networks for a factory.")[0] == "irrelevant"
    assert classify_relevance("Systems Engineer", "SCADA, PLCs and OT networks for a water utility. Windows servers for the control room.")[0] == "irrelevant"
    assert classify_relevance("Automation Engineer", "Automate infrastructure with Terraform and Ansible; Kubernetes, Linux, CI/CD, monitoring.")[0] == "relevant"


# ---------------------------------------------------------------- languages, contract type, work authorization
def test_a_language_requirement_is_a_visible_check_never_a_guess(ctx, db):
    approve_profile(db, ctx, cv_text=MID_CV, countries="Netherlands")
    jid = _seed(ctx, db, "nl", description_text=INFRA_PLAIN + " Fluent Dutch is required for this role.")
    m = _m(db, jid)
    assert m["match_group"] in {"good", "worth_a_shot"}
    assert any("Dutch" in g and "languages" in g for g in m["gaps"]), m["gaps"]
    assert requirements.languages(INFRA_PLAIN + " Fluent Dutch is required. English is our working language.") == [("Dutch", "required"), ("English", "working language")]


def test_a_listed_language_satisfies_the_check(ctx, db):
    approve_profile(db, ctx, cv_text=MID_CV + "\nLanguages\nDutch (native), English (fluent)\n", countries="Netherlands")
    facts = profile_svc.approved(db)["facts"]
    assert any("language" in f.tags and "Dutch" in f.text for f in facts)
    jid = _seed(ctx, db, "nl2", description_text=INFRA_PLAIN + " Fluent Dutch is required for this role.")
    m = _m(db, jid)
    assert not any("Dutch" in g for g in m["gaps"]), m["gaps"]
    assert any("Dutch" in r for r in m["reasons"]), m["reasons"]


def test_contract_type_is_normalised_shown_and_checked(ctx, db):
    assert normalize.normalize(RawPosting(source_id="t", external_id="1", title="DevOps Engineer", employer_name="E", url="https://x.example/1", employment_type="FullTime")).employment_type == "Full-time"
    assert normalize.normalize(RawPosting(source_id="t", external_id="2", title="DevOps Engineer", employer_name="E", url="https://x.example/2", employment_type="Contract")).employment_type == "Contract"
    assert normalize.normalize(RawPosting(source_id="t", external_id="3", title="DevOps Engineer", employer_name="E", url="https://x.example/3", description_text="Freelance / ZZP opdracht voor 6 maanden. " + INFRA_PLAIN)).employment_type == "Contract"
    approve_profile(db, ctx, cv_text=MID_CV, countries="Netherlands")
    jid = _seed(ctx, db, "ct", employment_type="Contract")
    m = _m(db, jid)
    assert m["match_group"] in {"good", "worth_a_shot", "backup"}
    assert any("contract" in g.lower() for g in m["gaps"]), m["gaps"]
    it = views.job_detail(db, jid)
    assert it["employment_type"] == "Contract"


def test_work_authorization_is_shown_as_stated_never_inferred(ctx, db):
    approve_profile(db, ctx, cv_text=MID_CV, countries="Netherlands")
    no = _seed(ctx, db, "auth1", description_text=INFRA_PLAIN + " Unfortunately we cannot offer visa sponsorship for this position.")
    unknown = _seed(ctx, db, "auth2")
    assert views.job_detail(db, no)["authorization"] == "The posting says: no visa sponsorship."
    assert views.job_detail(db, unknown)["authorization"] == "Not stated in the posting."
    # A job in another country is excluded for the country, never with a guess about permits.
    de = _seed(ctx, db, "auth3", location_text="Berlin, Germany", country_codes=["DE"])
    m = _m(db, de)
    assert m["match_group"] == "excluded" and "Germany" in m["exclusion_reason"] and "visa" not in m["exclusion_reason"].lower()
    assert _m(db, no)["match_group"] in {"good", "worth_a_shot", "backup"}  # sponsorship is a fact, not a filter


# ---------------------------------------------------------------- evidence kinds in explanations
def test_explanations_name_the_evidence_kind(ctx, db):
    approve_profile(db, ctx, cv_text=MID_CV, countries="Netherlands")
    facts = profile_svc.approved(db)["facts"]
    role = next(f for f in facts if f.category == "role" and "Fintech" in f.text)
    assert "tool:terraform" in role.tags and "tool:kubernetes" in role.tags  # what the bullets prove, kept with the role
    jid = _seed(ctx, db, "ev", description_text=FULL_DESC)
    m = _m(db, jid)
    text = " ".join(m["reasons"])
    assert "paid work" in text and "Terraform" in text, m["reasons"]
    # A career changer's Kubernetes comes from a project, and the explanation says so.
    db2_facts = None
    approve_profile(db, ctx, cv_text=CAREER_CHANGER_CV, countries="Netherlands")
    jid2 = _seed(ctx, db, "ev2", title="Junior DevOps Engineer", description_text="Entry level. Kubernetes, Linux, Git, GitHub Actions. You learn Terraform with us.")
    m2 = _m(db, jid2)
    assert any("project" in r.lower() and "Kubernetes" in r for r in m2["reasons"]), m2["reasons"]
    assert db2_facts is None
