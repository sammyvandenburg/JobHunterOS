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
def test_destination_is_checked_on_the_push_url_and_fails_closed_when_unknown(ctx, db, tmp_path, github, monkeypatch):
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
def test_only_the_two_state_files_are_ever_committed(ctx, db, tmp_path, github, monkeypatch):
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
def test_failed_pull_is_not_a_restore_and_preferences_merge_without_losing_either_side(ctx, db, tmp_path, github, monkeypatch):
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
