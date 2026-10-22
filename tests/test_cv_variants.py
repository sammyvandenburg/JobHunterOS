"""My CVs: one factual profile, several presentations. The existing CV becomes the default variant without any new
onboarding; a second variant never replaces it or changes the profile; versions are immutable revisions; identical
bytes are never stored twice; the original bytes come back unchanged in a fresh process."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from jobhunter.db import connect
from jobhunter.services import documents as docs
from jobhunter.services import profile as profile_svc
from tests.conftest import SAMPLE_CV, approve_profile

PLATFORM_CV = SAMPLE_CV.replace("Linux support engineer moving into platform work", "Platform-focused engineer; Kubernetes and GitOps first") + "\nCertified Kubernetes Security Specialist (CKS), 2026\n"


def test_existing_cv_becomes_the_default_variant_without_new_onboarding(ctx, db):
    approve_profile(db, ctx)
    cvs = docs.list_cvs(db)
    assert len(cvs) == 1
    only = cvs[0]
    assert only["default"] and only["profile_cv"] and only["version"] == 1 and only["label"] == docs.PROFILE_LABEL
    assert only["file_available"] and only["readable"] and only["key"].startswith("cv") and only["revision_key"].startswith("rev")
    assert docs.list_cvs(db) == cvs  # idempotent
    assert db.get_setting("profile.needs_review") is None


def test_a_second_variant_never_replaces_the_default_or_touches_the_profile(ctx, db):
    approve_profile(db, ctx)
    before = profile_svc.approved(db)
    first = docs.default_cv(db)
    v = docs.add_cv_variant(db, ctx.paths, PLATFORM_CV.encode(), "platform-en.txt", "Platform — English", tags=["platform", "english"])
    assert v["label"] == "Platform — English" and v["tags"] == ["platform", "english"] and v["version"] == 1 and not v["default"] and not v["profile_cv"]
    assert docs.default_cv(db)["key"] == first["key"]
    assert profile_svc.approved(db)["id"] == before["id"] and db.get_setting("profile.needs_review") is None
    assert docs.current_document(db, "cv")["id"] == first["document_id"]  # the profile's CV is untouched
    # The new presentation claims a certification the approved profile does not show: a review item, not a rewrite.
    assert any(r["kind"] == "certification" and "CKS" in r["claim"] for r in v["review"])
    # Both files are the original bytes.
    assert docs.cv_file(db, v["key"])["data"] == PLATFORM_CV.encode()
    assert docs.cv_file(db, first["key"])["data"] == SAMPLE_CV.encode()
    # Same bytes again: no duplicate, by content, whatever the name.
    with pytest.raises(docs.CVError, match="same file"):
        docs.add_cv_variant(db, ctx.paths, PLATFORM_CV.encode(), "copy.txt", "Another name")
    with pytest.raises(docs.CVError, match="already have"):
        docs.add_cv_variant(db, ctx.paths, b"x" * 300, "x.txt", "platform — english")


def test_a_new_version_is_an_immutable_revision_and_make_default_is_explicit(ctx, db):
    approve_profile(db, ctx)
    v = docs.add_cv_variant(db, ctx.paths, PLATFORM_CV.encode(), "platform-en.txt", "Platform — English")
    same = docs.add_cv_revision(db, ctx.paths, v["key"], PLATFORM_CV.encode(), "platform-en.txt")
    assert same["duplicate"] and same["version"] == 1
    newer = PLATFORM_CV + "\nAdded Argo Rollouts project.\n"
    v2 = docs.add_cv_revision(db, ctx.paths, v["key"], newer.encode(), "platform-en-v2.txt", note="Added rollouts")
    assert v2["version"] == 2 and [r["number"] for r in v2["revisions"]] == [1, 2]
    assert docs.cv_file(db, v2["revisions"][0]["revision_key"])["data"] == PLATFORM_CV.encode()  # revision 1 unchanged
    assert docs.cv_file(db, v2["revisions"][1]["revision_key"])["data"] == newer.encode()
    assert not docs.get_cv(db, v["key"])["default"]
    docs.set_default_cv(db, v["key"])
    assert docs.default_cv(db)["key"] == v["key"] and sum(c["default"] for c in docs.list_cvs(db)) == 1
    # A new version of the profile CV keeps the existing rule: the profile is reviewed, nothing else changes.
    prof = next(c for c in docs.list_cvs(db) if c["profile_cv"])
    r = docs.add_cv_revision(db, ctx.paths, prof["key"], (SAMPLE_CV + "\nGit, GitHub Actions\n").encode(), "cv2.txt")
    assert r["version"] == 2 and db.get_setting("profile.needs_review")
    assert docs.current_document(db, "cv")["id"] == r["document_id"]


def test_archive_hides_without_rewriting_and_delete_is_refused_for_sent_versions(ctx, db):
    approve_profile(db, ctx)
    v = docs.add_cv_variant(db, ctx.paths, PLATFORM_CV.encode(), "platform-en.txt", "Platform — English", make_default=True)
    a = docs.archive_cv(db, v["key"])
    assert a["archived"] and not a["default"]
    assert [c["key"] for c in docs.list_cvs(db)] != [v["key"]] and v["key"] in [c["key"] for c in docs.list_cvs(db, include_archived=True)]
    assert docs.default_cv(db)["profile_cv"]  # the default moved to a CV that is still visible
    assert docs.cv_file(db, v["key"])["data"] == PLATFORM_CV.encode()  # nothing rewritten
    docs.archive_cv(db, v["key"], archive=False)
    assert not docs.get_cv(db, v["key"])["archived"]
    with pytest.raises(docs.CVError, match="Archive it instead"):
        docs.delete_cv(db, docs.default_cv(db)["key"])
    path = Path(docs.cv_file(db, v["key"])["path"])
    docs.delete_cv(db, v["key"])
    assert not path.exists() and all(c["key"] != v["key"] for c in docs.list_cvs(db, include_archived=True))


def test_original_bytes_are_served_unchanged_in_a_fresh_process(ctx, db, home):
    approve_profile(db, ctx)
    v = docs.add_cv_variant(db, ctx.paths, PLATFORM_CV.encode(), "platform-en.txt", "Platform — English")
    db.close()
    code = f"""
import sys
from jobhunter.config import AppContext
from jobhunter.db import connect
from jobhunter.services import documents as docs
ctx = AppContext.from_env(demo=False)
db = connect(ctx.paths.db_path)
f = docs.cv_file(db, {v['key']!r})
sys.stdout.buffer.write(f['data'])
"""
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, env={**__import__('os').environ, "JOBHUNTER_HOME": str(home)}, cwd=str(Path(__file__).resolve().parents[1]))
    assert out.returncode == 0, out.stderr.decode()
    assert out.stdout == PLATFORM_CV.encode()
    # Without the file (a fresh cloud session), the answer is the smallest honest action, never a guess.
    db2 = connect(ctx.paths.db_path)
    Path(docs.cv_file(db2, v["key"])["path"]).unlink()
    with pytest.raises(docs.CVError, match="Add this CV again"):
        docs.cv_file(db2, v["key"])
    assert docs.get_cv(db2, v["key"])["file_available"] is False
    db2.close()


def test_hunter_suggests_another_cv_with_one_reason_and_only_when_tags_fit(ctx, db):
    approve_profile(db, ctx)
    assert docs.suggest_cv(db, {"title": "Platform Engineer", "description_text": ""}) is None  # one CV: nothing to suggest
    v = docs.add_cv_variant(db, ctx.paths, PLATFORM_CV.encode(), "platform-en.txt", "Platform — English", tags=["platform", "english"])
    s = docs.suggest_cv(db, {"title": "Senior Platform Engineer", "description_text": "Kubernetes."})
    assert s and s["variant_key"] == v["key"] and "platform" in s["reason"]
    assert docs.suggest_cv(db, {"title": "DevOps Engineer", "description_text": "Fluent Dutch required."}) is None


def _two_jobs(ctx, db):
    from tests.test_board import _seed_jobs

    _seed_jobs(ctx, db)
    a = db.fetchone("SELECT id FROM jobs WHERE employer_name = 'Northwind Cloud'")["id"]
    b = db.fetchone("SELECT id FROM jobs WHERE employer_name = 'Contoso Payments'")["id"]
    return a, b


def test_cv_selection_is_per_job_and_the_package_records_the_exact_version(ctx, db):
    from jobhunter.services import drafts

    approve_profile(db, ctx, countries="Netherlands")
    a, b = _two_jobs(ctx, db)
    plat = docs.add_cv_variant(db, ctx.paths, PLATFORM_CV.encode(), "platform-en.txt", "Platform — English", tags=["platform"])
    pa = drafts.prepare(ctx, db, a)
    assert pa["cv"]["choice"] == "default" and pa["cv"]["label"] == docs.PROFILE_LABEL and pa["cv"]["revision_key"]
    assert pa["cv_suggestion"] and pa["cv_suggestion"]["variant_key"] == plat["key"]  # Northwind: "Platform Engineer"
    pb = drafts.prepare(ctx, db, b, cv_variant_key=plat["key"])
    assert pb["cv"]["variant_key"] == plat["key"] and pb["cv"]["choice"] == "student" and pb["cv"]["version"] == 1
    # Job A is untouched by B's choice, and re-preparing A reuses the same package.
    again = drafts.prepare(ctx, db, a)
    assert again["reused"] and again["cv"]["variant_key"] == pa["cv"]["variant_key"]
    # Changing the default later never changes B's explicit choice; A (default) gets a new version on next prepare.
    docs.set_default_cv(db, plat["key"])
    assert drafts.selected_cv(db, b)["key"] == plat["key"]
    pa2 = drafts.prepare(ctx, db, a)
    assert not pa2["reused"] and pa2["cv"]["variant_key"] == plat["key"] and pa2["version"] == 2
    # A new revision of the selected CV marks B's package outdated: the package names the exact version.
    docs.add_cv_revision(db, ctx.paths, plat["key"], (PLATFORM_CV + "\nnew line\n").encode(), "v2.txt")
    job_b = db.fetchone("SELECT * FROM jobs WHERE id = ?", (b,))
    assert drafts.package_is_outdated(db, job_b, drafts.current_package(db, b))


def test_i_applied_records_which_version_was_sent_and_never_invents_one(ctx, db):
    from jobhunter.services import actions, drafts

    approve_profile(db, ctx, countries="Netherlands")
    a, b = _two_jobs(ctx, db)
    plat = docs.add_cv_variant(db, ctx.paths, PLATFORM_CV.encode(), "platform-en.txt", "Platform — English")
    pkg = drafts.prepare(ctx, db, a)
    drafts.save_user_answer(db, pkg["draft_id"], "Why are you interested in this role?", "Because I like platforms.")
    actions.mark_applied(db, a, cv_choice="prepared")
    app = db.fetchone("SELECT * FROM applications WHERE job_id = ?", (a,))
    assert app["submitted_cv_choice"] == "prepared" and app["submitted_cv_revision_key"] == pkg["cv"]["revision_key"]
    snap = __import__("json").loads(app["submission_json"])
    assert snap["answers"]["Why are you interested in this role?"]["text"] == "Because I like platforms." and snap["package"]["draft_id"] == pkg["draft_id"]
    assert snap["vacancy"]["title"] and snap["cv"]["revision_key"] == pkg["cv"]["revision_key"]
    # A later version of that CV does not change what was recorded as sent.
    prof = next(c for c in docs.list_cvs(db) if c["profile_cv"])
    docs.add_cv_revision(db, ctx.paths, prof["key"], (SAMPLE_CV + "\nmore\n").encode(), "cv2.txt")
    assert db.fetchone("SELECT submitted_cv_revision_key FROM applications WHERE job_id = ?", (a,))["submitted_cv_revision_key"] == pkg["cv"]["revision_key"]
    # "Another CV" binds that CV's current version; "Not sure" binds nothing.
    actions.mark_applied(db, b, cv_choice=plat["key"])
    app_b = db.fetchone("SELECT * FROM applications WHERE job_id = ?", (b,))
    assert app_b["submitted_cv_choice"] == "other" and app_b["submitted_cv_revision_key"] == plat["revision_key"]
    c = db.fetchone("SELECT id FROM jobs WHERE id NOT IN (?, ?) LIMIT 1", (a, b))["id"]
    actions.mark_applied(db, c, cv_choice="unknown")
    app_c = db.fetchone("SELECT * FROM applications WHERE job_id = ?", (c,))
    assert app_c["submitted_cv_choice"] == "unknown" and app_c["submitted_cv_revision_key"] is None
    # Confirming twice never changes the recorded version.
    actions.mark_applied(db, a, cv_choice=plat["key"])
    assert db.fetchone("SELECT submitted_cv_revision_key FROM applications WHERE job_id = ?", (a,))["submitted_cv_revision_key"] == pkg["cv"]["revision_key"]
