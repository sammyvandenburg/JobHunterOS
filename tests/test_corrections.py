"""Profile corrections in the person's own words: Hunter proposes a structured change, the person confirms,
a new approved revision is written, and the old matches no longer count."""

from __future__ import annotations

from jobhunter.services import profile as profile_svc
from jobhunter.services import views
from jobhunter.services.matching import engine
from tests.conftest import approve_profile
from tests.test_matching_matrix import MID_CV, _job, _m


def test_a_mixed_sentence_removes_only_what_was_denied(ctx, db):
    approve_profile(db, ctx, cv_text=MID_CV, countries="Netherlands")
    jid = _job(ctx, db, "c1")
    assert _m(db, jid)["match_group"] in {"good", "worth_a_shot", "backup"}
    v1 = profile_svc.approved(db)
    plan = profile_svc.plan_correction(db, "I do not use AWS, but I do use Kubernetes and Terraform")
    assert plan["remove"] == ["AWS"] and plan["keep"] == ["Kubernetes", "Terraform"] and plan["add"] == []
    assert plan["confirmation"] == "Remove AWS. Keep Kubernetes and Terraform. Correct?"
    assert plan["needs_confirmation"] and profile_svc.approved(db)["id"] == v1["id"]  # nothing changed yet
    res = profile_svc.apply_correction(db, plan)
    v2 = profile_svc.approved(db)
    assert res["approved"] and v2["id"] != v1["id"] and v2["version"] == v1["version"] + 1
    skills = {f.text.lower(): f.status for f in v2["facts"] if f.category == "skill"}
    assert skills.get("aws") == "rejected" and skills.get("kubernetes") != "rejected" and skills.get("terraform") != "rejected"
    # The old matches belong to the old revision: nothing is recommended on stale grounds until a rematch.
    assert views.current_recommended(db) == []
    engine.run_matching(ctx, db)
    after = _m(db, jid)
    assert after["match_group"] in {"good", "worth_a_shot", "backup"} and any("AWS" in g for g in after["gaps"])


def test_plain_sentences_still_work_and_unknown_words_become_a_note(ctx, db):
    approve_profile(db, ctx, cv_text=MID_CV, countries="Netherlands")
    plan = profile_svc.plan_correction(db, "I don't have AWS experience")
    assert plan["remove"] == ["AWS"] and plan["confirmation"] == "Remove AWS. Correct?"
    plan = profile_svc.plan_correction(db, "I also use Pulumi at work")
    assert plan["add"] == ["Pulumi"] and plan["confirmation"] == "Add Pulumi. Correct?"
    plan = profile_svc.plan_correction(db, "My notice period is one month")
    assert plan["remove"] == [] and plan["add"] == [] and plan["note"] and "Correct?" in plan["confirmation"]
    res = profile_svc.apply_correction(db, plan)
    assert any(f.category == "other" and "notice period" in f.text for f in profile_svc.approved(db)["facts"]) and res["approved"]


def test_the_command_line_asks_before_it_changes_anything(ctx, db, capsys):
    import json

    from jobhunter import cli

    approve_profile(db, ctx, cv_text=MID_CV, countries="Netherlands")
    v1 = profile_svc.approved(db)["id"]
    assert cli.main(["--json", "profile", "correct", "I do not use AWS, but I do use Kubernetes and Terraform"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["needs_confirmation"] and out["confirmation"].startswith("Remove AWS. Keep Kubernetes and Terraform.")
    assert profile_svc.approved(db)["id"] == v1
    assert cli.main(["--json", "profile", "correct", "--yes", "I do not use AWS, but I do use Kubernetes and Terraform"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["approved"] and out["retracted"] == ["aws"] and profile_svc.approved(db)["id"] != v1
