"""Gate A (2026-10-25): the audited guarantees, reproduced against the current tree and repaired. Privacy and
identities first: where the state really goes, what exactly is committed, which identities survive a title change,
what a merge keeps, which matches are current, how delivery evidence moves, and that no request runs forever."""

from __future__ import annotations

import json
import os

import pytest

from jobhunter.config import AppContext
from jobhunter.db import connect
from jobhunter.models import RawPosting
from jobhunter.services import actions, board, cloudstate, hunt, notifications, workspace
from jobhunter.services import preferences as prefs_svc
from jobhunter.services.matching import engine
from tests.conftest import approve_profile
from tests.test_cloud_repo import _clone, _git, github  # noqa: F401  (fixture)


def _private(monkeypatch):
    monkeypatch.setattr(cloudstate, "verify_destination_slug", lambda slug, token=None: {"slug": slug, "visibility": "private", "how": "GitHub API with a token", "push_allowed": True, "identity": "student"})
    monkeypatch.setattr(cloudstate, "_slug_of", lambda url: "student/hunter-me" if url else None)


def _posting(ext: str, title: str, **kw) -> RawPosting:
    base = dict(source_id="greenhouse:fake", external_id=ext, title=title, employer_name="Northwind Cloud", url=f"https://job-boards.greenhouse.io/northwind/jobs/{ext}", location_text="Amsterdam, Netherlands", country_codes=["NL"], description_text="Kubernetes, Terraform and AWS. Platform team. On call.", posted_at="2026-10-17T09:00:00+00:00", posted_at_provenance="t", is_employer_hosted=True)
    base.update(kw)
    return RawPosting(**base)


# A01/A02: the push destination is checked, not assumed; the push URL counts, not only the fetch URL.
def test_destination_is_checked_on_the_push_url_and_fails_closed_when_unknown(ctx, db, tmp_path, github, monkeypatch):  # noqa: F811
    approve_profile(db, ctx)
    a = _clone(github, tmp_path / "a")
    _git(a, "remote", "set-url", "--push", "origin", "https://github.com/sammyvandenburg/JobHunterOS.git")  # fetch private, push to KubeCraft's
    with pytest.raises(cloudstate.RepoError, match="KubeCraft's shared repository"):
        cloudstate.save_to_repo(ctx, db, a)
    _git(a, "remote", "set-url", "--push", "origin", str(github))
    calls = []

    def unknown(slug, token=None):
        calls.append(slug)
        return {"slug": slug, "visibility": "unknown", "how": "GitHub answered 403 (rate limit or refusal), which proves nothing", "push_allowed": None, "identity": None}

    monkeypatch.setattr(cloudstate, "verify_destination_slug", unknown)
    monkeypatch.setattr(cloudstate, "_slug_of", lambda url: "student/hunter-me" if url else None)
    with pytest.raises(cloudstate.RepoError, match="Could not establish"):
        cloudstate.save_to_repo(ctx, db, a)
    assert calls == ["student/hunter-me"] and not (a / "state" / "hunter-state.json").exists()
    # The person's own confirmation, recorded with the slug, unblocks it; a token proof would too.
    cloudstate.confirm_private(db, a)
    res = cloudstate.save_to_repo(ctx, db, a)
    assert res["pushed"] and res["destination"]["verified"] and "confirmed private by you" in res["destination"]["how"]
    # With a token the API's own answer decides: private but not pushable is refused.
    monkeypatch.setattr(cloudstate, "verify_destination_slug", lambda slug, token=None: {"slug": slug, "visibility": "private", "how": "GitHub API with a token", "push_allowed": False, "identity": "someone-else"})
    with pytest.raises(cloudstate.RepoError, match="may not push"):
        cloudstate.save_to_repo(ctx, db, a)


def test_verify_destination_reads_the_api_truthfully(monkeypatch):
    monkeypatch.setattr(cloudstate, "_github_repo", lambda slug, token: (200, {"private": True, "permissions": {"push": True}, "owner": {"login": "student"}}, {}))
    v = cloudstate.verify_destination_slug("student/hunter-me", token="t")
    assert v["visibility"] == "private" and v["push_allowed"] and v["identity"] == "student"
    monkeypatch.setattr(cloudstate, "_github_repo", lambda slug, token: (200, {"private": False}, {}))
    assert cloudstate.verify_destination_slug("student/hunter-me", token=None)["visibility"] == "public"
    monkeypatch.setattr(cloudstate, "_github_repo", lambda slug, token: (403, None, {"x-ratelimit-remaining": "0"}))
    assert cloudstate.verify_destination_slug("student/hunter-me", token=None)["visibility"] == "unknown"
    monkeypatch.setattr(cloudstate, "_github_repo", lambda slug, token: (404, None, {}))
    assert cloudstate.verify_destination_slug("student/hunter-me", token=None)["visibility"] == "private_or_missing"
    monkeypatch.setattr(cloudstate, "_github_repo", lambda slug, token: (None, None, {}))
    assert cloudstate.verify_destination_slug("student/hunter-me", token=None)["visibility"] == "unknown"


# A02: exactly two files, whatever is staged or lying around; the person's index is untouched.
def test_only_the_two_state_files_are_ever_committed(ctx, db, tmp_path, github, monkeypatch):  # noqa: F811
    approve_profile(db, ctx)
    _private(monkeypatch)
    a = _clone(github, tmp_path / "a")
    _git(a, "remote", "set-url", "origin", str(github))
    (a / "my-cv.pdf").write_bytes(b"%PDF-1.4 dummy")
    _git(a, "add", "my-cv.pdf")  # the person staged a CV by mistake
    (a / "state").mkdir(exist_ok=True)
    (a / "state" / "unexpected.json").write_text("{}")  # a stray file inside state/
    (a / "notes.txt").write_text("working change")
    res = cloudstate.save_to_repo(ctx, db, a)
    assert res["pushed"] and res["files"] == ["state/hunter-state.json", "state/seen_jobs.json"]
    committed = _git(a, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD").splitlines()
    assert sorted(committed) == ["state/hunter-state.json", "state/seen_jobs.json"]
    assert "my-cv.pdf" in _git(a, "diff", "--cached", "--name-only")  # still staged for the person, never committed
    assert "notes.txt" in _git(a, "status", "--porcelain")
    peek = _clone(github, tmp_path / "peek")
    assert sorted(p.name for p in (peek / "state").iterdir()) == ["hunter-state.json", "seen_jobs.json"]
    assert not (peek / "my-cv.pdf").exists()
    # A symbolic link where a state file should be is refused, not followed.
    (a / "state" / "hunter-state.json").unlink()
    os.symlink(a / "my-cv.pdf", a / "state" / "hunter-state.json")
    actions.favorite_job(db, hunt.import_postings(ctx, db, [_posting("x1", "Platform Engineer")])["job_ids"][0])
    with pytest.raises(cloudstate.RepoError, match="symbolic link"):
        cloudstate.save_to_repo(ctx, db, a)


# A03: identities survive a changed title; contacts and requests have portable ids.
def test_job_identity_survives_a_title_change_and_contacts_have_portable_keys(ctx, db, tmp_path):
    approve_profile(db, ctx)
    jid = hunt.import_postings(ctx, db, [_posting("nl1", "Platform Engineer")])["job_ids"][0]
    key_before = board.job_key(db.fetchone("SELECT canonical_key FROM jobs WHERE id = ?", (jid,))["canonical_key"])
    actions.favorite_job(db, jid)
    hunt.import_postings(ctx, db, [_posting("nl1", "Senior Platform Engineer (Amsterdam)", location_text="Utrecht, Netherlands")])
    row = db.fetchone("SELECT id, title, canonical_key FROM jobs WHERE id = ?", (jid,))
    assert row["title"] == "Senior Platform Engineer (Amsterdam)" and board.job_key(row["canonical_key"]) == key_before
    assert db.fetchone("SELECT COUNT(*) AS n FROM jobs")["n"] == 1
    c = workspace.add_contact(db, jid, "connection", "Sam Friend", route="https://www.linkedin.com/in/sam-friend")
    assert c["key"].startswith("ct") and len(c["key"]) == 12
    # The page may name the contact by its key (any machine) or by the row id (this machine only).
    workspace.mark_contacted(db, jid, c["key"], when="2026-10-25T10:00:00Z")
    assert workspace.list_contacts(db, jid)[0]["status"] == "contacted"
    # Old rows without a key get one on first use, once.
    db.execute("UPDATE contacts SET contact_key = NULL")
    k1 = workspace.list_contacts(db, jid)[0]["key"]
    assert k1 and workspace.list_contacts(db, jid)[0]["key"] == k1
    r1 = workspace.request(db, jid, "contact", {})
    r2 = workspace.request(db, jid, "contact", {})
    assert r1["request_id"] != r2["request_id"] and len(r1["request_id"].rsplit(":", 1)[1]) == 10


# A04: a failed pull is not a current restore; preferences merge field by field; a newer approval wins.
def test_failed_pull_is_not_a_restore_and_preferences_merge_without_losing_either_side(ctx, db, tmp_path, github, monkeypatch):  # noqa: F811
    approve_profile(db, ctx, countries="Netherlands")
    _private(monkeypatch)
    a = _clone(github, tmp_path / "a")
    _git(a, "remote", "set-url", "origin", str(github))
    cloudstate.save_to_repo(ctx, db, a)
    # Side B (a cloud run) restores, then changes the salary; side A (the laptop) changes the location meanwhile.
    ctx_b = AppContext.from_env(demo=False, home=tmp_path / "b")
    db_b = connect(ctx_b.paths.db_path)
    b = _clone(github, tmp_path / "b-clone")
    assert cloudstate.load_from_repo(ctx_b, db_b, b, restore=True)["loaded"]
    pb = prefs_svc.get(db_b)
    pb.salary_min, pb.salary_currency, pb.salary_period = 65000, "EUR", "year"
    prefs_svc.save(db_b, pb, reason="cloud edit")
    cloudstate.save_to_repo(ctx_b, db_b, b)
    pa = prefs_svc.get(db)
    pa.current_location = "Groningen, Netherlands"
    prefs_svc.save(db, pa, reason="laptop edit")
    res = cloudstate.load_from_repo(ctx, db, a, restore=True)
    assert res["loaded"] and res["current"]
    merged = prefs_svc.get(db)
    assert merged.salary_min == 65000 and merged.current_location == "Groningen, Netherlands"  # both non-conflicting edits survive
    assert res["inputs"].get("preference_conflicts") == []
    # The same field changed on both sides after they last agreed: kept local, listed as a conflict for the person.
    pa = prefs_svc.get(db)
    pa.salary_min = 70000
    prefs_svc.save(db, pa, reason="laptop edit 2")
    cloudstate.save_to_repo(ctx, db, a)
    cloudstate.load_from_repo(ctx_b, db_b, b, restore=True)
    pb = prefs_svc.get(db_b)
    pb.salary_min = 60000
    prefs_svc.save(db_b, pb, reason="cloud edit 2")
    cloudstate.save_to_repo(ctx_b, db_b, b)
    pa = prefs_svc.get(db)
    pa.salary_min = 72000
    prefs_svc.save(db, pa, reason="laptop edit 3")
    res = cloudstate.load_from_repo(ctx, db, a, restore=True)
    assert prefs_svc.get(db).salary_min == 72000 and res["inputs"]["preference_conflicts"] == ["salary_min"]
    assert db.get_setting(cloudstate.PREFS_CONFLICTS_SETTING)["salary_min"]["remote"] == 60000
    # A pull that fails is reported as not restored, never as current.
    monkeypatch.setattr(cloudstate, "_git_ok", lambda d, *a: False)
    res = cloudstate.load_from_repo(ctx, db, a, restore=True)
    assert res["loaded"] is False and res["current"] is False and "may be out of date" in res["note"]
    db_b.close()


# A05: an imported match carries the fingerprints that produced it; a mismatch is dropped, never relabelled.
def test_imported_matches_keep_their_fingerprints_and_stale_ones_are_dropped(ctx, db, tmp_path):
    approve_profile(db, ctx, countries="Netherlands")
    jid = hunt.import_postings(ctx, db, [_posting("nl1", "Platform Engineer")])["job_ids"][0]
    actions.favorite_job(db, jid)  # an acted-on job travels with its match outcome
    bundle = cloudstate.export_bundle(ctx, db)
    m = next(j["match"] for j in bundle["results"]["jobs"] if j.get("match"))
    assert m["fingerprint"]["policy"] == engine.POLICY_VERSION and m["fingerprint"]["profile"] and m["fingerprint"]["prefs"]
    # Same profile and preferences elsewhere: the match is accepted with its original time.
    ctx2 = AppContext.from_env(demo=False, home=tmp_path / "same")
    db2 = connect(ctx2.paths.db_path)
    res = cloudstate.import_bundle(ctx2, db2, bundle, inputs=True, results=True)
    assert res["results"]["matches_added"] == 1
    assert db2.fetchone("SELECT computed_at FROM matches")["computed_at"] == m["computed_at"]
    db2.close()
    # Different preferences on the receiving side: the old opinion is not current there and is left out.
    ctx3 = AppContext.from_env(demo=False, home=tmp_path / "other")
    db3 = connect(ctx3.paths.db_path)
    stale = json.loads(json.dumps(bundle))
    stale["inputs"]["preferences"]["work_countries"] = ["DE"]
    res = cloudstate.import_bundle(ctx3, db3, stale, inputs=True, results=True)
    assert res["results"]["matches_added"] == 0  # the old opinion was not imported
    local = db3.fetchone("SELECT computed_at, cache_key FROM matches")  # what is there was computed here, for these preferences
    assert local is None or local["cache_key"] != m["fingerprint"]["job"]  # scored here for these preferences, not the old key
    db3.close()


# A06: delivery evidence only moves forward.
def test_notification_lifecycle_merges_forward_and_never_authorises_a_second_send(ctx, db, tmp_path):
    approve_profile(db, ctx, countries="Netherlands")
    notifications.set_recipient(db, "student@example.com")
    db.set_setting("digest.transport", "claude")
    hunt.import_postings(ctx, db, [_posting("nl1", "Platform Engineer")])
    prep = notifications.prepare_handoff(db=db, ctx=ctx) if False else notifications.prepare_handoff(ctx, db)
    assert prep["status"] == "ready"
    snapshot = cloudstate.export_bundle(ctx, db)  # taken while the digest is 'sending'
    notifications.record_handoff(db, prep["notification_id"], "sent", message_id="gm-123")
    sent_row = db.fetchone("SELECT * FROM notifications WHERE id = ?", (prep["notification_id"],))
    assert sent_row["status"] == "sent" and sent_row["provider_message_id"] == "gm-123"
    cloudstate.import_bundle(ctx, db, snapshot, inputs=False, results=True)
    after = db.fetchone("SELECT * FROM notifications WHERE id = ?", (prep["notification_id"],))
    assert after["status"] == "sent" and after["provider_message_id"] == "gm-123" and after["sent_at"] == sent_row["sent_at"]
    assert notifications.sent_today(db)
    assert notifications.prepare_handoff(ctx, db)["status"] == "skipped"
    # The other direction: a side that only knew 'sending' learns 'sent' with the id.
    ctx2 = AppContext.from_env(demo=False, home=tmp_path / "other")
    db2 = connect(ctx2.paths.db_path)
    cloudstate.import_bundle(ctx2, db2, snapshot, inputs=True, results=True)
    assert db2.fetchone("SELECT status FROM notifications")["status"] == "sending"
    cloudstate.import_bundle(ctx2, db2, cloudstate.export_bundle(ctx, db), inputs=False, results=True)
    row2 = db2.fetchone("SELECT * FROM notifications")
    assert row2["status"] == "sent" and row2["provider_message_id"] == "gm-123" and db2.fetchone("SELECT COUNT(*) AS n FROM notifications")["n"] == 1
    db2.close()


# A11: requests are claimed atomically with a lease; a dead run leaves them recoverable, never running forever.
def test_requests_are_claimed_once_and_recover_after_a_crash(ctx, db, tmp_path):
    approve_profile(db, ctx, countries="Netherlands")
    jid = hunt.import_postings(ctx, db, [_posting("nl1", "Platform Engineer")])["job_ids"][0]
    r = workspace.request(db, jid, "prepare", {})
    assert workspace.claim_request(db, r["request_id"], "run-a")["claimed_by"] == "run-a"
    assert workspace.claim_request(db, r["request_id"], "run-b") is None  # held by run-a
    assert workspace.run_request(ctx, db, r["request_id"], holder="run-b")["state"] == "running"  # run-b does not take it over
    # run-a dies: the lease expires; recovery puts it back, bounded by attempts.
    db.update("requests", {"lease_until": "2000-01-01T00:00:00+00:00"}, "request_id = ?", (r["request_id"],))
    assert workspace.recover_stale(db) == [{"request_id": r["request_id"], "state": "requested"}]
    done = workspace.run_pending(ctx, db)
    assert done[0]["state"] == "ready" and db.fetchone("SELECT attempts, claimed_by FROM requests")["claimed_by"] is None
    # Three interruptions end in a truthful failure.
    r2 = workspace.request(db, jid, "research", {})
    for _ in range(3):
        workspace.claim_request(db, r2["request_id"], "x")
        db.update("requests", {"lease_until": "2000-01-01T00:00:00+00:00"}, "request_id = ?", (r2["request_id"],))
        workspace.recover_stale(db)
    assert db.fetchone("SELECT state, message FROM requests WHERE request_id = ?", (r2["request_id"],))["state"] == "failed"
    # A bug inside a request is a recorded failure, not a request stuck in running.
    r3 = workspace.request(db, jid, "letter", {})
    import jobhunter.services.workspace as ws

    orig = ws.write_optional_text
    ws.write_optional_text = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    try:
        out = workspace.run_request(ctx, db, r3["request_id"])
    finally:
        ws.write_optional_text = orig
    assert out["state"] == "failed" and "Something went wrong" in out["message"]


# ---------------------------------------------------------------------------
# User edits and submissions (A07-A09, A17-A18)
# ---------------------------------------------------------------------------
from jobhunter.services import documents as docs  # noqa: E402
from jobhunter.services import drafts as drafts_svc  # noqa: E402
from jobhunter.services import interviewos  # noqa: E402
from tests.test_cv_variants import PLATFORM_CV  # noqa: E402


def _prepared(ctx, db):
    approve_profile(db, ctx, countries="Netherlands")
    jid = hunt.import_postings(ctx, db, [_posting("nl1", "Platform Engineer")])["job_ids"][0]
    drafts_svc.prepare(ctx, db, jid)
    return jid


def test_user_texts_survive_a_cv_change_and_a_regeneration_without_saving_again(ctx, db):
    jid = _prepared(ctx, db)
    pkg = drafts_svc.current_package(db, jid)
    first = next(a for a in pkg["draft"]["answers"] if a["answer"])
    aid = workspace.answer_id(first["question"])
    workspace.save_answer(db, jid, aid, "My own wording.")
    workspace.add_questions(db, jid, [{"text": "Why us? (150 words)", "limit": 150, "limit_unit": "words"}])
    workspace.save_answer(db, jid, workspace.answer_id("Why us? (150 words)"), "Because of the platform team.")
    workspace.save_optional_text(db, jid, "letter", "My letter.")
    plat = docs.add_cv_variant(db, ctx.paths, PLATFORM_CV.encode(), "p.txt", "Platform — English")
    pkg2 = drafts_svc.prepare(ctx, db, jid, cv_variant_key=plat["key"])  # the CV changed: a new package version
    assert pkg2["version"] == 2 and "user_answers" not in pkg2
    pkg3 = drafts_svc.prepare(ctx, db, jid, regenerate=True)
    assert pkg3["version"] == 3
    w = workspace.work_doc(db, jid, "jkey")
    by_id = {a["id"]: a for a in w["package"]["answers"]}
    assert by_id[aid]["user"]["text"] == "My own wording."
    q = by_id[workspace.answer_id("Why us? (150 words)")]
    assert q["user"]["text"] == "Because of the platform team." and q["limit"] == 150 and q["limit_unit"] == "words" and q["custom"]
    assert w["package"]["optional"]["letter"]["user_text"]["text"] == "My letter."
    assert w["package"]["answers"][0]["custom"] and all(a["example"] for a in w["package"]["answers"] if not a["custom"])  # the form's questions first; Hunter's are examples
    # Cleared is not the same as never answered.
    workspace.save_answer(db, jid, aid, "")
    u = workspace.user_text(db, jid, "answer", aid)
    assert u["text"] == "" and u["rev"] == 2


def test_the_cv_confirmed_is_the_exact_version_shown_never_a_substitute(ctx, db):
    jid = _prepared(ctx, db)
    plat = docs.add_cv_variant(db, ctx.paths, PLATFORM_CV.encode(), "p.txt", "Platform — English")
    rev1 = plat["revision_key"]
    docs.add_cv_revision(db, ctx.paths, plat["key"], (PLATFORM_CV + "\nv2\n").encode(), "p2.txt")
    actions.mark_applied(db, jid, cv_choice=rev1)  # the version the person saw, even though a newer one exists
    app = db.fetchone("SELECT * FROM applications WHERE job_id = ?", (jid,))
    assert app["submitted_cv_choice"] == "exact" and app["submitted_cv_revision_key"] == rev1
    snap = json.loads(app["submission_json"])
    assert snap["cv"]["sha256"] == plat["sha256"] and snap["cv"]["number"] == 1 and snap["destination"].startswith("https://") and snap["evidence"]["source"] == "user"
    assert snap["answers_state"] == "not_recorded"
    # An archived or unknown version is refused, never swapped for the default.
    other = hunt.import_postings(ctx, db, [_posting("nl2", "DevOps Engineer")])["job_ids"][0]
    docs.archive_cv(db, plat["key"])
    with pytest.raises(actions.ActionError, match="archived"):
        actions.mark_applied(db, other, cv_choice=rev1)
    with pytest.raises(actions.ActionError, match="not known"):
        actions.mark_applied(db, other, cv_choice="rev0000000000")
    assert db.fetchone("SELECT applied_at FROM applications WHERE job_id = ?", (other,)) is None or db.fetchone("SELECT applied_at FROM applications WHERE job_id = ?", (other,))["applied_at"] is None


def test_submission_snapshot_is_immutable_and_the_handoff_uses_it_alone(ctx, db):
    jid = _prepared(ctx, db)
    pkg = drafts_svc.current_package(db, jid)
    aid = workspace.answer_id(next(a for a in pkg["draft"]["answers"] if a["answer"])["question"])
    workspace.save_answer(db, jid, aid, "Sent wording.")
    actions.mark_applied(db, jid, cv_choice="prepared", evidence={"receipt": "none"})
    snap = json.loads(db.fetchone("SELECT submission_json FROM applications WHERE job_id = ?", (jid,))["submission_json"])
    assert snap["answers_state"] == "confirmed" and list(snap["answers"].values())[0]["text"] == "Sent wording."
    workspace.save_answer(db, jid, aid, "Edited after applying.")
    docs.set_default_cv(db, docs.add_cv_variant(db, ctx.paths, PLATFORM_CV.encode(), "p.txt", "Platform — English")["key"])
    db.execute("UPDATE jobs SET description_text = 'changed later', description_hash = 'h9' WHERE id = ?", (jid,))
    actions.mark_applied(db, jid, cv_choice="unknown")  # a second confirmation changes nothing
    snap2 = json.loads(db.fetchone("SELECT submission_json FROM applications WHERE job_id = ?", (jid,))["submission_json"])
    assert snap2 == snap
    actions.set_stage(db, jid, "interview")
    text = (ctx.paths.handoffs_dir / __import__("pathlib").Path(interviewos.export(ctx, db, jid)["path"]).name).read_text()
    assert "Sent wording." in text and "Edited after applying." not in text and "My CV, version 1" in text
    # Applied without any saved answer: the hand-off says so instead of listing later drafts.
    other = hunt.import_postings(ctx, db, [_posting("nl2", "DevOps Engineer")])["job_ids"][0]
    drafts_svc.prepare(ctx, db, other)
    actions.mark_applied(db, other, cv_choice="unknown")
    workspace.save_answer(db, other, aid, "Written after applying.")
    t2 = __import__("pathlib").Path(interviewos.export(ctx, db, other)["path"]).read_text()
    assert "No answers were recorded at the time you applied" in t2 and "Written after applying." not in t2 and "Unknown: no CV version" in t2


def test_export_covers_the_workspace_and_a_sent_cv_can_still_be_deleted_with_a_record(ctx, db):
    from jobhunter.services import datamgmt

    jid = _prepared(ctx, db)
    plat = docs.add_cv_variant(db, ctx.paths, PLATFORM_CV.encode(), "p.txt", "Platform — English")
    workspace.save_answer(db, jid, workspace.answer_id(drafts_svc.current_package(db, jid)["draft"]["answers"][0]["question"]), "Mine.")
    actions.mark_applied(db, jid, cv_choice=plat["key"])
    out = datamgmt.export_all(ctx, db)
    assert out["tables"]["user_texts"] == 1 and out["tables"]["cv_variants"] == 2 and out["tables"]["cv_revisions"] == 2 and "requests" in out["tables"]
    readme = (__import__("pathlib").Path(out["path"]) / "README.txt").read_text()
    assert "metadata with the hash" in readme and "documents/" in readme
    with pytest.raises(docs.CVError, match="--force"):
        docs.delete_cv(db, plat["key"])
    path = __import__("pathlib").Path(docs.cv_file(db, plat["key"])["path"])
    docs.delete_cv(db, plat["key"], force=True)
    assert not path.exists() and docs.cv_revision(db, plat["revision_key"]) is None
    tomb = json.loads(db.fetchone("SELECT submitted_cv_tombstone_json FROM applications WHERE job_id = ?", (jid,))["submitted_cv_tombstone_json"])
    assert tomb["sha256"] == plat["sha256"] and tomb["label"] == "Platform — English"
    actions.set_stage(db, jid, "interview")
    assert "you deleted this CV" in __import__("pathlib").Path(interviewos.export(ctx, db, jid)["path"]).read_text()


# ---------------------------------------------------------------------------
# Truth, sources and operational status (A12-A16, A19-A20)
# ---------------------------------------------------------------------------
def test_sourced_research_outlives_timestamp_refreshes_and_rule_changes(ctx, db, monkeypatch):
    jid = _prepared(ctx, db)
    job = db.fetchone("SELECT * FROM jobs WHERE id = ?", (jid,))
    base = workspace.ensure_research(db, job)
    assert base["level"] == "posting" and base["created_by"] == "rules"
    ok = workspace.submit_research(db, jid, {"vacancy_says": ["Kubernetes in production."], "company_describes": ["Northwind runs a hosted platform for logistics firms."], "interpretation": ["A small platform team."], "sources": [{"url": "https://job-boards.greenhouse.io/northwind/jobs/nl1", "label": "Posting", "checked_at": "2026-10-25T09:00:00Z"}, {"url": "https://northwind.example/about", "label": "About Northwind", "checked_at": "2026-10-25T09:05:00Z"}]})
    assert ok["accepted"]
    db.execute("UPDATE jobs SET last_checked_at = '2026-10-26T06:00:00+00:00', last_seen_at = '2026-10-26T06:00:00+00:00' WHERE id = ?", (jid,))
    monkeypatch.setattr(workspace, "RESEARCH_POLICY", "9999.99-z")
    job = db.fetchone("SELECT * FROM jobs WHERE id = ?", (jid,))
    kept = workspace.ensure_research(db, job)
    assert kept["created_by"] == "claude" and kept["level"] == "researched" and kept["version"] == ok["version"]
    # The posting text itself changes: a new version, with the sourced company lines carried forward and dated.
    db.execute("UPDATE jobs SET description_text = description_text || '\nNow also Pulumi.', description_hash = 'h-new' WHERE id = ?", (jid,))
    job = db.fetchone("SELECT * FROM jobs WHERE id = ?", (jid,))
    nxt = workspace.ensure_research(db, job)
    assert nxt["version"] == ok["version"] + 1 and nxt["company_describes"] == ["Northwind runs a hosted platform for logistics firms."]
    assert any(s["url"] == "https://northwind.example/about" for s in nxt["sources"]) and nxt["carried_from"]["version"] == ok["version"]


def test_one_evidence_evaluation_essentials_first_negations_out_years_not_inflated(ctx, db):
    approve_profile(db, ctx, countries="Netherlands")
    desc = "Requirements\nKubernetes in production and Terraform.\nNo Azure experience required.\n10+ years of experience.\nNice to have\nPulumi.\nAbout us\nWe moved off Jenkins years ago."
    jid = hunt.import_postings(ctx, db, [_posting("nl1", "Platform Engineer", description_text=desc)])["job_ids"][0]
    prof = __import__("jobhunter.services.profile", fromlist=["approved"]).approved(db)
    facts = [f for f in prof["facts"] if f.status != "rejected"]
    case = workspace.strongest_case(db, db.fetchone("SELECT * FROM jobs WHERE id = ?", (jid,)), facts, "", years_relevant=prof["targets"].get("professional_years_relevant"))
    names = [i["requirement"] for i in case["items"]]
    assert names[0].lower().startswith("kubernetes") and "Azure" in case["negated"] and not any("Azure" in n or "Jenkins" in n for n in names)
    years = next(i for i in case["items"] if "years" in i["requirement"])
    assert not years["profile_supports"] and "your profile shows about" in years["gap"]
    plus = next(i for i in case["items"] if i.get("optional"))
    assert "Pulumi" in plus["requirement"] and plus["gap"] is None


def test_question_classes_limits_and_no_invented_questionnaire(ctx, db):
    cq = drafts_svc.classify_question
    assert cq("How do you manage incidents at 3 am?") == "open"
    assert cq("Which programming languages do you use?") == "open"
    assert cq("Describe how you would trace a slow request.") == "open"
    assert cq("When could you start?") == "declaration" and cq("What is your notice period?") == "declaration" and cq("Salary expectations?") == "declaration"
    assert cq("Do you require visa sponsorship?") == "personal" and cq("Are you legally authorized to work in the Netherlands?") == "personal" and cq("What is your age?") == "personal"
    jid = _prepared(ctx, db)
    workspace.add_questions(db, jid, [{"text": "Tell us about a migration you ran. (200 words)", "limit": 200, "limit_unit": "words"}, {"text": "What is your notice period?", "limit": 100}])
    rows = workspace.question_rows(db, jid)
    assert rows[0]["limit_unit"] == "words" and rows[0]["class"] == "open" and not rows[0]["needs_user"]
    assert rows[1]["limit_unit"] == "characters" and rows[1]["class"] == "declaration" and rows[1]["needs_user"]
    w = workspace.work_doc(db, jid, "k")
    checklist = next(c for c in w["package"]["checklist"] if c["id"] == "answers")
    assert "The form's questions" in checklist["text"] and "0 of 2" in checklist["text"]  # examples are not counted


def test_an_aggregator_refresh_never_erases_verified_employer_content(ctx, db):
    approve_profile(db, ctx, countries="Netherlands")
    jid = hunt.import_postings(ctx, db, [_posting("nl1", "Platform Engineer", salary_text="€70,000 - €80,000", apply_url="https://job-boards.greenhouse.io/northwind/jobs/nl1/apply")])["job_ids"][0]
    before = db.fetchone("SELECT * FROM jobs WHERE id = ?", (jid,))
    assert before["direct_url_status"] == "verified" and before["salary_text"]
    # The same observation from the employer board, now without a salary line: nothing is emptied.
    hunt.import_postings(ctx, db, [_posting("nl1", "Platform Engineer", salary_text=None, apply_url="https://job-boards.greenhouse.io/northwind/jobs/nl1/apply")])
    after = db.fetchone("SELECT * FROM jobs WHERE id = ?", (jid,))
    assert after["salary_text"] == before["salary_text"]
    # An aggregator copy of the same job (matched as a duplicate) brings a shorter description and no links: the
    # employer's content stays; only the observation is recorded.
    agg = RawPosting(source_id="remotive:devops", external_id="r-77", title="Platform Engineer", employer_name="Northwind Cloud", url="https://remotive.com/remote-jobs/77", location_text="Netherlands", country_codes=["NL"], description_text="Kubernetes.", posted_at="2026-10-18T09:00:00+00:00", posted_at_provenance="t", is_employer_hosted=False)
    hunt.import_postings(ctx, db, [agg])
    again = db.fetchone("SELECT * FROM jobs WHERE id = ?", (jid,))
    assert again["description_text"] == before["description_text"] and again["direct_url"] == before["direct_url"] and again["apply_url"] == before["apply_url"]
    assert db.fetchone("SELECT COUNT(*) AS n FROM job_observations WHERE job_id = ?", (jid,))["n"] == 2
    # And a later aggregator refresh of its own observation may not rewrite the employer's fields either.
    agg2 = agg.model_copy(update={"title": "Platform Engineer (Remote)", "description_text": "Kubernetes and more.", "salary_text": None})
    hunt.import_postings(ctx, db, [agg2])
    final = db.fetchone("SELECT * FROM jobs WHERE id = ?", (jid,))
    assert final["title"] == before["title"] and final["description_text"] == before["description_text"]


def test_refresh_history_conditional_requests_and_retry_after(ctx, db, monkeypatch):
    from jobhunter.services import http as http_svc

    assert http_svc.parse_retry_after("120") == 120.0 and http_svc.parse_retry_after(None) is None and http_svc.parse_retry_after("nonsense") is None
    seen: list[dict] = []

    class FakeResp:
        def __init__(self, status, headers, content=b"{}"):
            self.status, self.headers, self.content, self.final_url = status, headers, content, "https://x"

    class FakeClient:
        def get(self, url, params=None, headers=None, allow_http=False):
            seen.append(dict(headers or {}))
            return FakeResp(200, {"etag": '"abc"', "last-modified": "Sat, 25 Oct 2026 06:00:00 GMT"}, b"{}")

    sc = hunt._SourceClient(FakeClient(), {"etag": '"old"', "last_modified": "Fri, 24 Oct 2026 06:00:00 GMT"})
    sc.get("https://x")
    sc.get("https://x/page2")
    assert seen[0]["If-None-Match"] == '"old"' and seen[0]["If-Modified-Since"].startswith("Fri") and "If-None-Match" not in seen[1]
    assert sc.last_response["etag"] == '"abc"' and sc.last_response["bytes"] == 2
    # A 304 is an answer ("unchanged"), not a failure, and never closes anything; a Retry-After is remembered.
    from jobhunter.services.sources.base import SourceAdapter

    r304 = SourceAdapter.failed("greenhouse:x", "not modified", 304)
    assert r304.status == "skipped" and r304.complete and r304.http_status == 304
    r429 = SourceAdapter.failed("greenhouse:x", "HTTP 429", 429, retry_after=3600)
    assert r429.retry_after == 3600
    approve_profile(db, ctx, countries="Netherlands")
    from jobhunter.services import catalog

    with db.tx():
        catalog.ensure_source(db, "greenhouse:northwind", "greenhouse", {"token": "northwind", "employer_name": "Northwind Cloud"}, "catalogue", None)
    run_id = db.insert("hunt_runs", {"trigger": "test", "status": "running", "started_at": "2026-10-25T06:00:00+00:00"})
    src = db.fetchone("SELECT * FROM watch_sources WHERE id = 'greenhouse:northwind'")
    hunt._record_source_run(db, run_id, src, r429, {"sources_ok": 0, "sources_failed": 0, "sources_partial": 0, "sources_skipped": 0, "postings_fetched": 0, "jobs_new": 0, "jobs_relevant_new": 0, "jobs_updated": 0, "jobs_closed": 0, "failures": []}, last_response={})
    src = db.fetchone("SELECT * FROM watch_sources WHERE id = 'greenhouse:northwind'")
    assert src["next_allowed_at"] and src["next_allowed_at"] > "2026-10-25"
    res = hunt._fetch_one(http_svc.SafeClient(transport=None, min_interval_per_host=0.0, retries=0) if False else object(), src)
    assert res.status == "skipped" and "asked Hunter to wait" in res.notes
    ok = __import__("jobhunter.models", fromlist=["FetchResult"]).FetchResult(source_id="greenhouse:northwind", status="ok", postings=[], refresh={"etag": '"abc"', "last_modified": "Sat", "bytes": 10})
    hunt._record_source_run(db, run_id, src, ok, {"sources_ok": 0, "sources_failed": 0, "sources_partial": 0, "sources_skipped": 0, "postings_fetched": 0, "jobs_new": 0, "jobs_relevant_new": 0, "jobs_updated": 0, "jobs_closed": 0, "failures": []}, last_response=ok.refresh)
    src = db.fetchone("SELECT * FROM watch_sources WHERE id = 'greenhouse:northwind'")
    assert src["etag"] == '"abc"' and src["last_bytes"] == 10 and src["next_allowed_at"] is None and src["last_fetch_at"]


def test_publication_is_confirmed_separately_and_status_keeps_its_distinctions(ctx, db, tmp_path):
    from jobhunter.services import workflow

    approve_profile(db, ctx, countries="Netherlands")
    hunt.import_postings(ctx, db, [_posting("nl1", "Platform Engineer")])
    board.link(db, "https://claude.ai/artifact/5wUUffKaZnmpqGgPnRzPVE")
    board.import_actions(ctx, db, tmp_path / "none", listed=0)
    hunt.run_hunt(ctx, db, source_ids=[]) if False else None
    manifest = board.export(ctx, db, tmp_path / "out")
    plan = workflow.plan(ctx, db)
    step = next(s for s in plan["steps"] if s["step"] == "publish_dashboard")
    assert step["status"] != "done" and "never confirmed" in step["why"]
    board.published(db, manifest)
    plan = workflow.plan(ctx, db)
    step = next(s for s in plan["steps"] if s["step"] == "publish_dashboard")
    assert step["status"] == "done" and step["at"]
    st = board._hunter_status(db)
    assert st["dashboard_url"].startswith("https://claude.ai/artifact/") and st["publish"]["confirmed"] and st["work"] == {"queued": 0, "running": 0, "needs_input": 0, "ready": 0, "failed": 0}
    assert st["email"]["configured"] is False and st["email"]["delivery_active"] is False and st["routine"]["verified"] is False
    notifications.set_recipient(db, "student@example.com")
    db.set_setting("digest.transport", "claude")
    st = board._hunter_status(db)
    assert st["email"]["configured"] is True and st["email"]["delivery_active"] is False  # configured is not delivered
    # A follow-up-only digest goes out when the person opted in, even with no new jobs.
    jid = db.fetchone("SELECT id FROM jobs")["id"]
    actions.mark_applied(db, jid, applied_at="2026-10-01T09:00:00+00:00", cv_choice="unknown")
    workspace.set_followup(db, jid, due_at="2026-10-02T09:00:00+00:00")
    db.execute("UPDATE jobs SET posted_at = '2025-01-01T00:00:00+00:00'")  # nothing new to email
    board.import_actions(ctx, db, tmp_path / "none", listed=0)  # the page was written above; read it again first
    assert notifications.prepare_handoff(ctx, db)["status"] == "skipped"
    db.set_setting(notifications.FOLLOWUP_EMAIL_SETTING, True)
    prep = notifications.prepare_handoff(ctx, db)
    assert prep["status"] == "ready" and prep["subject"] == "JobHunterOS: your follow-up reminders" and "Follow-up due" in prep["text"] and prep["jobs"] == []
