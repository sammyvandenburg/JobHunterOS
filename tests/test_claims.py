"""JH-18: candidate claims need candidate evidence. Employer requirements never become achievements."""

from __future__ import annotations

from jobhunter.models import EvidenceRef, Fact, RawPosting
from jobhunter.services import drafts, hunt
from tests.conftest import approve_profile


def _fact(fid, cat, text, kind="professional"):
    return Fact(id=fid, category=cat, text=text, confidence="explicit", experience_kind=kind, evidence=[EvidenceRef(type="document", ref="document:1", quote=text[:60])])


HOMELAB = [_fact("p1", "project", "Homelab Kubernetes cluster (k3s) with GitOps via Argo CD (guided course project)", "guided"), _fact("s1", "skill", "kubernetes", "unknown")]
SENIOR = HOMELAB + [_fact("r1", "role", "Platform Engineer, Contoso — led a team of 4; on-call for the production Kubernetes platform", "professional")]
JOB = "We operate 100 production clusters across 3 regions. You will own the platform."


def test_employer_numbers_and_ownership_words_are_not_candidate_evidence():
    text = "I independently operated 100 production clusters and managed the platform team."
    problems = drafts.validate_text(text, ["p1"], HOMELAB, job_text=JOB)
    joined = " ".join(problems)
    assert "100" in joined and "not in your facts" in joined
    assert "led" in joined or "team" in joined  # leadership claim
    assert "production" in joined
    # The same sentence from someone whose paid role shows it passes.
    assert drafts.validate_text("I led a team of 4 and ran the production Kubernetes platform.", ["r1"], SENIOR, job_text=JOB) == []
    # Guided projects never support production or ownership claims, even when cited.
    assert drafts.validate_text("I owned the production platform.", ["p1"], HOMELAB) != []
    # Plain, honest sentences pass for everyone.
    assert drafts.validate_text("I built a homelab Kubernetes cluster with GitOps via Argo CD as a guided course project.", ["p1"], HOMELAB) == []
    # Numbers that are only the job's identity (title, employer) are fine; numbers from the description are not.
    assert drafts.validate_text("The role is Platform Engineer 2 at Contoso 365.", [], HOMELAB, job_text="Platform Engineer 2 at Contoso 365", identity_text="Platform Engineer 2 Contoso 365") == []
    assert drafts.validate_text("I managed 3 regions.", [], HOMELAB, job_text=JOB) != []


def test_fit_report_uses_need_proof_check_next_and_honest_wording(ctx, db):
    approve_profile(db, ctx)
    jid = hunt.import_postings(ctx, db, [RawPosting(source_id="t", external_id="f1", title="Platform Engineer", employer_name="Northwind", url="https://job-boards.greenhouse.io/n/jobs/1", location_text="Amsterdam, Netherlands", country_codes=["NL"], description_text="What you bring\nExperience running Kubernetes in production\nTerraform and AWS\nPulumi is a plus\n5+ years in infrastructure\nWhat you'll do\nOwn the platform for 100 services\nBe on call", posted_at="2026-10-17T09:00:00+00:00", posted_at_provenance="t", is_employer_hosted=True)])["job_ids"][0]
    pkg = drafts.prepare(ctx, db, jid)
    fit = pkg["fit"]
    assert set(fit) >= {"what_they_need", "your_proof", "what_to_check", "next_step", "fit_line"}
    assert fit["fit_line"].startswith("Tools they name that your CV shows:")
    assert any("Tools they name" in w for w in fit["what_they_need"]) and any("They say:" in w for w in fit["what_they_need"])
    assert any("kubernetes" in m.lower() for m in fit["your_proof"])
    assert any("pulumi" in m.lower() and "not in your profile" in m for m in fit["what_to_check"])
    assert not any("have not used" in m for m in fit["what_to_check"] + fit["next_step"])
    assert fit["next_step"] and all(len(p) < 220 for p in fit["next_step"])
    # Employer scale ("100 services") never shows up as the candidate's achievement in drafted answers.
    for a in pkg["draft"]["answers"]:
        assert "100 services" not in a["answer"]
    assert not pkg["validation_problems"], pkg["validation_problems"]
