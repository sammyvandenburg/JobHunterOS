"""Acceptance 1-8: setup, resume, readiness gate, LinkedIn URL, CV immutability, projects, conflicts, optional prefs."""

from __future__ import annotations

from pathlib import Path

import pytest

from jobhunter.cli import main
from jobhunter.services import consent, onboarding
from jobhunter.services import documents as docs
from jobhunter.services import preferences as prefs_svc
from jobhunter.services import profile as profile_svc
from jobhunter.util import sha256_bytes
from tests.conftest import ENTRY_CV, SAMPLE_CV, approve_profile


def test_acc01_fresh_clone_starts_setup(ctx, db, capsys):
    assert main(["--home", str(ctx.paths.home), "setup", "next", "--json"]) == 0
    out = capsys.readouterr().out
    assert "readiness" in out and "finished JobMagnetOS" in out


def test_acc02_resume_without_repeating(ctx, db):
    assert onboarding.next_step(db)["key"] == "readiness"
    onboarding.answer(db, "readiness", "yes")
    assert onboarding.next_step(db)["key"] == "cv"
    docs.add_text_document(db, ctx.paths, "cv", SAMPLE_CV, "cv")
    onboarding.answer(db, "linkedin_url", "https://www.linkedin.com/in/sam-example")
    # "Restart": a new DB connection sees the same state and does not re-ask answered steps.
    from jobhunter.db import connect

    db2 = connect(ctx.paths.db_path)
    nxt = onboarding.next_step(db2)
    assert nxt["key"] == "linkedin_content"
    st = onboarding.status(db2)
    assert {"readiness", "cv", "linkedin_url"} <= set(st["done"])
    db2.close()


def test_acc02b_readiness_no_pauses_and_saves(ctx, db):
    res = onboarding.answer(db, "readiness", "no")
    assert res["paused"] is True and "progress is saved" in res["note"]
    assert not consent.is_granted(db, "readiness")


def test_acc03_live_hunting_requires_readiness_and_approval(ctx, db):
    ok, why = onboarding.live_hunting_allowed(db, demo=False)
    assert not ok and "finished" in why
    consent.grant(db, "readiness", {})
    ok, why = onboarding.live_hunting_allowed(db, demo=False)
    assert not ok and "not approved" in why
    approve_profile(db, ctx)
    ok, _ = onboarding.live_hunting_allowed(db, demo=False)
    assert ok
    # A new CV version re-opens the review gate.
    docs.add_text_document(db, ctx.paths, "cv", SAMPLE_CV + "\nExtra line about Go programming.", "cv-v2")
    ok, why = onboarding.live_hunting_allowed(db, demo=False)
    assert not ok and "review" in why
    # Demo mode is allowed but clearly separate.
    assert onboarding.live_hunting_allowed(db, demo=True)[0]


def test_acc04_linkedin_url_alone_is_not_content(ctx, db):
    consent.grant(db, "readiness", {})
    docs.add_text_document(db, ctx.paths, "cv", SAMPLE_CV, "cv")
    onboarding.answer(db, "linkedin_url", "https://www.linkedin.com/in/sam-example")
    assert onboarding.next_step(db)["key"] == "linkedin_content"
    draft = profile_svc.build_rule_based_draft(db)
    joined = " ".join(f.text for f in draft.facts) + " ".join(e.ref for f in draft.facts for e in f.evidence)
    assert "linkedin.com" not in joined
    with pytest.raises(onboarding.AnswerError):
        onboarding.answer(db, "linkedin_url", "https://example.com/not-linkedin")


def test_acc05_cv_bytes_and_hash_unchanged(ctx, db, tmp_path):
    pdf_like = SAMPLE_CV.encode("utf-8")
    row = docs.add_document(db, ctx.paths, "cv", pdf_like, "cv.md")
    stored = Path(row["stored_path"])
    digest = sha256_bytes(pdf_like)
    assert row["sha256"] == digest and stored.read_bytes() == pdf_like
    approve_profile(db, ctx)  # adds a second text doc; the first stays
    profile_svc.add_user_fact(db, "I also ran a small Proxmox lab.")
    from jobhunter.services import drafts, hunt
    from jobhunter.services.demo import demo_postings

    hunt.import_postings(ctx, db, demo_postings()[:2])
    drafts.prepare(ctx, db, 1)
    assert stored.read_bytes() == pdf_like
    assert sha256_bytes(stored.read_bytes()) == digest
    assert all(r["ok"] for r in docs.verify_integrity(db))


def test_acc06_guided_projects_never_professional(ctx, db):
    consent.grant(db, "readiness", {})
    docs.add_text_document(db, ctx.paths, "cv", SAMPLE_CV, "cv")
    draft = profile_svc.build_rule_based_draft(db)
    projects = [f for f in draft.facts if f.category == "project"]
    assert projects and all(f.experience_kind in {"personal", "guided"} for f in projects)
    capstone = next(f for f in projects if "capstone" in f.text.lower())
    assert capstone.experience_kind == "guided"
    # Even a submitted draft claiming a project is professional gets downgraded without user evidence.
    capstone.experience_kind = "professional"
    checked, warnings = profile_svc.validate_draft(db, draft)
    assert next(f for f in checked.facts if "capstone" in f.text.lower()).experience_kind == "personal"
    assert any("only you can confirm" in w for w in warnings)
    # Certifications: an in-progress cert is a course, not a certification.
    certs = [f for f in draft.facts if f.category == "certification"]
    courses = [f for f in draft.facts if f.category == "course"]
    assert any("CKA" in f.text for f in certs)
    assert any("AWS" in f.text and "in progress" in f.text for f in courses)


def test_acc07_conflicts_and_overlaps(ctx, db):
    consent.grant(db, "readiness", {})
    docs.add_text_document(db, ctx.paths, "cv", SAMPLE_CV, "cv")
    # LinkedIn with different years triggers a clarification question, never silent reconciliation.
    from tests.conftest import LINKEDIN_TEXT

    docs.add_text_document(db, ctx.paths, "linkedin", LINKEDIN_TEXT.replace("Jan 2022 – Present", "Jan 2019 – Present"), "linkedin")
    draft = profile_svc.build_rule_based_draft(db)
    assert any(q.key == "cv_linkedin_dates" for q in draft.questions)
    # Overlapping roles (Mar 2020-Dec 2021 and Jun 2021-Dec 2021) count once: about 6.x years, not 7+.
    years = draft.professional_years_relevant
    assert years is not None and 6.0 <= years <= 7.0, years
    assert profile_svc.merged_months([("2020-03", "2021-12"), ("2021-06", "2021-12")]) == 22


def test_acc08_salary_and_remote_can_stay_unset(ctx, db):
    approve_profile(db, ctx)
    onboarding.answer(db, "salary_min", "skip")
    onboarding.answer(db, "remote", "skip")
    p = prefs_svc.get(db)
    assert p.salary_min is None and p.salary_strength == "unset" and p.remote == "no_preference"
    assert onboarding.next_step(db)["key"] in {"hunt_frequency", "github", "notify_mode"}


def test_profile_submit_rejects_unsupported_quote(ctx, db):
    consent.grant(db, "readiness", {})
    docs.add_text_document(db, ctx.paths, "cv", ENTRY_CV, "cv")
    from jobhunter.models import EvidenceRef, Fact, ProfileDraft

    draft = ProfileDraft(
        summary_text="x",
        facts=[
            Fact(id="r1", category="role", text="Senior SRE at BigCo (2015 to 2020)", experience_kind="professional", start="2015-01", end="2020-01", evidence=[EvidenceRef(type="document", ref="document:1", quote="Senior SRE at BigCo")]),
            Fact(id="c1", category="certification", text="AWS Certified Solutions Architect", evidence=[]),
        ],
    )
    checked, warnings = profile_svc.validate_draft(db, draft)
    assert all(f.status == "needs_clarification" for f in checked.facts)
    assert checked.professional_years_relevant == 0.0 or checked.professional_years_relevant is not None
    assert any("not found" in w for w in warnings)


def test_preferences_history_and_revert(ctx, db):
    p = prefs_svc.get(db)
    p.remote = "preferred"
    prefs_svc.save(db, p, reason="t1")
    p.remote = "required"
    prefs_svc.save(db, p, reason="t2")
    assert prefs_svc.get(db).remote == "required"
    assert prefs_svc.revert_last(db, "remote")
    assert prefs_svc.get(db).remote == "preferred"
