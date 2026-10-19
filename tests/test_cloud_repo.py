"""The student's own private git repository as the state store: two fresh cloud machines and the laptop share one
small file through clone, pull and push. No file passes through Claude's context, no re-emailing, no lost
notes or favorites, and a push race resolves by rebase. A local bare repository stands in for GitHub."""

from __future__ import annotations

import json
import subprocess

import pytest

from jobhunter.config import AppContext
from jobhunter.db import connect
from jobhunter.services import actions, cloudstate, hunt, notes, notifications, views
from tests.conftest import approve_profile


def _git(d, *a):
    return subprocess.run(["git", "-C", str(d), *a], capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def github(tmp_path):
    """A bare repository with one initial commit, like a freshly created private repo with a README."""
    bare = tmp_path / "github.git"
    subprocess.run(["git", "init", "--bare", "-q", "-b", "main", str(bare)], check=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", "-q", str(bare), str(seed)], check=True)
    (seed / "README.md").write_text("# Hunter state\n")
    _git(seed, "add", "README.md")
    _git(seed, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init")
    _git(seed, "push", "-q", "origin", "main")
    return bare


def _clone(github, where):
    subprocess.run(["git", "clone", "-q", str(github), str(where)], check=True)
    return where


def _fresh(tmp_path, name):
    ctx = AppContext.from_env(demo=False, home=tmp_path / name)
    return ctx, connect(ctx.paths.db_path)


def test_github_state_round_trip_two_cloud_runs_and_the_laptop(ctx, db, tmp_path, github, fake_sources):
    # Laptop: profile approved, address confirmed, a dismissed job; link the clone and save.
    approve_profile(db, ctx)
    notifications.set_recipient(db, "student@example.com")
    db.set_setting("digest.transport", "claude")
    dismissed = hunt.import_postings(ctx, db, [__import__("jobhunter.models", fromlist=["RawPosting"]).RawPosting(source_id="t", external_id="nl1", title="Platform Engineer", employer_name="Northwind Cloud", url="https://job-boards.greenhouse.io/northwind/jobs/1", location_text="Amsterdam, Netherlands", country_codes=["NL"], description_text="Kubernetes, Terraform, AWS, Linux. Platform team on call.", posted_at="2026-10-17T09:00:00+00:00", posted_at_provenance="t", is_employer_hosted=True)])["job_ids"][0]
    actions.dismiss_job(db, dismissed, reason_code="not_for_me")
    laptop_clone = _clone(github, tmp_path / "laptop-clone")
    assert cloudstate.link_repo(db, laptop_clone)["remote"]
    res = cloudstate.save_to_repo(ctx, db)
    assert res["saved"] and res["pushed"] and res["jobs"] == 1  # the dismissed job travels as an exclusion
    state = json.loads((laptop_clone / "state" / "hunter-state.json").read_text())
    assert "Ask about" not in json.dumps(state) and state["inputs"]["consents"]["digest_email"]["address"] == "student@example.com"
    assert json.loads((laptop_clone / "state" / "seen_jobs.json").read_text()) == {}

    # Cloud run one: fresh machine, fresh clone, restore, hunt, email once, save.
    c1, d1 = _fresh(tmp_path, "cloud1")
    clone1 = _clone(github, tmp_path / "clone1")
    loaded = cloudstate.load_from_repo(c1, d1, clone1, restore=True)
    assert loaded["loaded"] and loaded["inputs"]["profile"] and loaded["exclusions_applied"] == 1
    hunt.run_hunt(c1, d1, trigger="cloud")
    cloudstate.tag_latest_run(d1, cloudstate.new_run_tag())
    h1 = notifications.prepare_handoff(c1, d1)
    assert h1["status"] == "ready" and "Northwind Cloud" not in h1["text"] and "Berlin Robotics" in h1["text"]
    notifications.record_handoff(d1, h1["notification_id"], "sent", message_id="gmail-1")
    saved1 = cloudstate.save_to_repo(c1, d1, clone1)
    assert saved1["saved"] and saved1["pushed"] and saved1["notifications"] == 1
    d1.close()
    seen = json.loads(subprocess.run(["git", "-C", str(_clone(github, tmp_path / "peek")), "show", "HEAD:state/seen_jobs.json"], capture_output=True, text=True, check=True).stdout)
    assert len(seen) == len(h1["jobs"]) and all(v["status"] == "sent" for v in seen.values())

    # Cloud run two: another fresh machine; restores run one's state; sends nothing.
    c2, d2 = _fresh(tmp_path, "cloud2")
    clone2 = _clone(github, tmp_path / "clone2")
    loaded2 = cloudstate.load_from_repo(c2, d2, clone2, restore=True)
    assert loaded2["results"]["notifications_added"] == 1 and loaded2["results"]["jobs_added"] >= 1
    hunt.run_hunt(c2, d2, trigger="cloud")
    h2 = notifications.prepare_handoff(c2, d2)
    assert h2["status"] == "skipped"
    assert cloudstate.save_to_repo(c2, d2, clone2)["saved"] in {True, False}
    d2.close()

    # Laptop: pull and merge; notes, favorites and the dismissal are untouched; the emailed job is held back.
    kept = dismissed
    notes.add(db, kept, "Local note stays local.")
    merged = cloudstate.load_from_repo(ctx, db)
    assert merged["loaded"] and merged["results"]["notifications_added"] == 1
    assert views.job_detail(db, kept)["dismissed"] is True and [n["body"] for n in notes.list_for_job(db, kept)] == ["Local note stays local."]
    berlin = db.fetchone("SELECT id FROM jobs WHERE employer_name = 'Berlin Robotics'")
    assert berlin and berlin["id"] not in {j["id"] for j in notifications.pending_jobs(db, limit=50)}
    assert views.job_detail(db, berlin["id"])["group"] in {"good", "worth_a_shot", "backup"}
    # A dashboard action on the cloud job, then a save; the next cloud run restores it and sends nothing new.
    actions.favorite_job(db, berlin["id"])
    cloudstate.save_to_repo(ctx, db)
    c3, d3 = _fresh(tmp_path, "cloud3")
    clone3 = _clone(github, tmp_path / "clone3")
    cloudstate.load_from_repo(c3, d3, clone3, restore=True)
    hunt.run_hunt(c3, d3, trigger="cloud")
    assert notifications.prepare_handoff(c3, d3)["status"] == "skipped"
    cloudstate.save_to_repo(c3, d3, clone3)
    d3.close()
    cloudstate.load_from_repo(ctx, db)
    assert views.job_detail(db, berlin["id"])["favorited"] is True
    assert db.fetchone("SELECT COUNT(*) AS n FROM notifications")["n"] == 1
    assert "Favorite" not in (laptop_clone / "state" / "hunter-state.json").read_text() and "favorited" not in (laptop_clone / "state" / "hunter-state.json").read_text()


def test_push_race_is_resolved_by_rebase_and_errors_are_plain(ctx, db, tmp_path, github):
    approve_profile(db, ctx)
    a = _clone(github, tmp_path / "a")
    b = _clone(github, tmp_path / "b")
    cloudstate.link_repo(db, a)
    cloudstate.save_to_repo(ctx, db)
    # Someone else (the cloud) pushes in between.
    _git(b, "pull", "-q", "--rebase")
    (b / "note.txt").write_text("cloud was here\n")
    _git(b, "add", "note.txt")
    _git(b, "-c", "user.name=c", "-c", "user.email=c@c", "commit", "-q", "-m", "cloud")
    _git(b, "push", "-q")
    db.set_setting("preferences", {**(db.get_setting("preferences") or {}), "current_location": "Utrecht, Netherlands"})
    res = cloudstate.save_to_repo(ctx, db)
    assert res["saved"] and res["pushed"]
    assert (a / "note.txt").exists()  # rebased onto the other side's commit
    # Nothing changed: no empty commit.
    assert cloudstate.save_to_repo(ctx, db)["saved"] is False
    with pytest.raises(cloudstate.RepoError):
        cloudstate.link_repo(db, tmp_path / "not-a-clone")
    # Nothing linked means "this checkout": the student's own private copy of Hunter is the state repository.
    assert cloudstate.repo_dir(connect(AppContext.from_env(demo=False, home=tmp_path / "nolink").paths.db_path)) == cloudstate.software_repo()


def test_single_private_copy_keeps_code_and_state_and_updates_cleanly(ctx, db, tmp_path, github):
    """One repository per student: Hunter's code plus state/. KubeCraft's updates merge in without touching state."""
    # "KubeCraft" publishes the tool; the student has a private copy with an origin of their own.
    upstream = tmp_path / "upstream.git"
    subprocess.run(["git", "init", "--bare", "-q", "-b", "main", str(upstream)], check=True)
    kube = _clone(upstream, tmp_path / "kube")
    (kube / "hunter.py").write_text("print('v1')\n")
    (kube / "state").mkdir()
    (kube / "state" / "README.md").write_text("private state lives here\n")
    _git(kube, "add", "-A")
    _git(kube, "-c", "user.name=k", "-c", "user.email=k@k", "commit", "-q", "-m", "v1")
    _git(kube, "push", "-q", "origin", "main")
    student = _clone(upstream, tmp_path / "student")
    _git(student, "remote", "set-url", "origin", str(github))  # their own private repository
    _git(student, "push", "-q", "-f", "origin", "main")
    approve_profile(db, ctx)
    notifications.set_recipient(db, "student@example.com")
    res = cloudstate.save_to_repo(ctx, db, student)
    assert res["saved"] and res["pushed"]
    committed = _git(student, "show", "--stat", "--name-only", "--format=", "HEAD").split()
    assert committed == ["state/hunter-state.json", "state/seen_jobs.json"]
    # A stray private file next to the code is never staged by a save.
    (student / "my-cv.pdf").write_bytes(b"%PDF-1.4 secret")
    db.set_setting("preferences", {**(db.get_setting("preferences") or {}), "current_location": "Utrecht, Netherlands"})
    cloudstate.save_to_repo(ctx, db, student)
    assert "my-cv.pdf" not in _git(student, "ls-files")
    # KubeCraft ships v2; the student's copy updates without touching their state.
    (kube / "hunter.py").write_text("print('v2')\n")
    _git(kube, "add", "-A")
    _git(kube, "-c", "user.name=k", "-c", "user.email=k@k", "commit", "-q", "-m", "v2")
    _git(kube, "push", "-q", "origin", "main")
    up = cloudstate.update_from_upstream(student, upstream_url=str(upstream))
    assert up["updated"] and up["state_touched"] == [] and (student / "hunter.py").read_text() == "print('v2')\n"
    assert "Utrecht" in (student / "state" / "hunter-state.json").read_text()
    assert cloudstate.update_from_upstream(student, upstream_url=str(upstream))["updated"] is False
    # A conflicting upstream change is reported, not forced.
    (student / "hunter.py").write_text("print('student edit')\n")
    _git(student, "add", "-A")
    _git(student, "-c", "user.name=s", "-c", "user.email=s@s", "commit", "-q", "-m", "local edit")
    (kube / "hunter.py").write_text("print('v3')\n")
    _git(kube, "add", "-A")
    _git(kube, "-c", "user.name=k", "-c", "user.email=k@k", "commit", "-q", "-m", "v3")
    _git(kube, "push", "-q", "origin", "main")
    up = cloudstate.update_from_upstream(student, upstream_url=str(upstream))
    assert up["updated"] is False and "does not merge cleanly" in up["note"] and _git(student, "status", "--porcelain") == ""
    # The default location is the checkout itself: no link, no second repository.
    assert cloudstate.software_repo() is not None and cloudstate.repo_dir(db) == cloudstate.software_repo()


def test_state_never_goes_to_a_public_or_shared_repository(ctx, db, tmp_path, github, monkeypatch):
    approve_profile(db, ctx)
    a = _clone(github, tmp_path / "a")
    # Origin that is KubeCraft's shared repository: refused before anything is written.
    _git(a, "remote", "set-url", "origin", "https://github.com/sammyvandenburg/JobHunterOS.git")
    with pytest.raises(cloudstate.RepoError, match="KubeCraft's shared repository"):
        cloudstate.save_to_repo(ctx, db, a)
    assert not (a / "state" / "hunter-state.json").exists()
    # A public repository on GitHub: refused.
    _git(a, "remote", "set-url", "origin", "git@github.com:someone/my-hunter.git")
    monkeypatch.setattr(cloudstate, "_github_visibility", lambda slug: "public")
    with pytest.raises(cloudstate.RepoError, match="public repository"):
        cloudstate.save_to_repo(ctx, db, a)
    # A private one (anonymous GitHub answers 404): allowed, and the state goes to the named branch even
    # when the checkout sits on a session branch.
    monkeypatch.setattr(cloudstate, "_github_visibility", lambda slug: "private_or_missing")
    _git(a, "remote", "set-url", "origin", str(github))
    monkeypatch.setattr(cloudstate, "_origin_slug", lambda d: "someone/my-hunter")
    _git(a, "checkout", "-q", "-b", "claude/session-xyz")
    res = cloudstate.save_to_repo(ctx, db, a, branch="main")
    assert res["pushed"] and res["branch"] == "main" and res["destination"]["visibility"] == "private_or_missing"
    peek = _clone(github, tmp_path / "peek")
    assert (peek / "state" / "hunter-state.json").exists() and _git(peek, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    # The next load on another machine reads that branch regardless of its own checkout branch.
    c, d = _fresh(tmp_path, "cloud")
    other = _clone(github, tmp_path / "other")
    _git(other, "checkout", "-q", "-b", "claude/another-session")
    monkeypatch.setattr(cloudstate, "_origin_slug", lambda d: "someone/my-hunter")
    loaded = cloudstate.load_from_repo(c, d, other, restore=True, branch="main")
    assert loaded["loaded"] and loaded["branch"] == "main" and loaded["inputs"]["profile"]
    d.close()
