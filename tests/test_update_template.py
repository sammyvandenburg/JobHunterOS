"""Updating a copy that was created from the template (GitHub's "Use this template" makes a new history), without
`--allow-unrelated-histories`: upstream's changes since the version the copy started from are applied as a patch,
protected paths are never touched, a backup tag allows a rollback, a conflict changes nothing, and a copy with
shared history still merges as before."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from jobhunter.services import cloudstate


def _git(d, *args, check=True) -> str:
    res = subprocess.run(["git", "-C", str(d), *args], capture_output=True, text=True)
    if check and res.returncode != 0:
        raise AssertionError(res.stderr)
    return res.stdout.strip()


def _commit(d, msg):
    _git(d, "add", "-A")
    _git(d, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", msg)


def _upstream(tmp_path) -> Path:
    up = tmp_path / "upstream"
    up.mkdir()
    _git(up, "init", "-q", "-b", "main")
    (up / "hunter.py").write_text("print('v1')\n")
    (up / "docs").mkdir()
    (up / "docs" / "guide.md").write_text("v1 guide\n")
    (up / "state").mkdir()
    (up / "state" / "README.md").write_text("private state lives here\n")
    _commit(up, "v1")
    return up


def _template_copy(up, where: Path) -> Path:
    """What 'Use this template' produces: the same files, a brand-new history with one commit."""
    where.mkdir()
    _git(where, "init", "-q", "-b", "main")
    for p in up.rglob("*"):
        if ".git" in p.parts or p.is_dir():
            continue
        dest = where / p.relative_to(up)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(p.read_bytes())
    _commit(where, "Initial commit")
    return where


def test_a_template_created_copy_updates_by_patch_and_can_roll_back(tmp_path):
    up = _upstream(tmp_path)
    copy = _template_copy(up, tmp_path / "copy")
    assert _git(copy, "merge-base", "HEAD", "HEAD") and _git(copy, "rev-list", "--count", "HEAD") == "1"
    # The student's private things: state, a local env file, and a path they protect themselves.
    (copy / "state" / "hunter-state.json").write_text('{"mine": true}\n')
    (copy / ".env").write_text("SECRET=1\n")
    (copy / ".jobhunter").mkdir()
    (copy / ".jobhunter" / "protected").write_text("docs/my-notes.md\n")
    (copy / "docs" / "my-notes.md").write_text("my notes\n")
    _commit(copy, "my state and notes")
    # KubeCraft ships v2: code changes, a new file, a doc change, and (wrongly) a change under state/ and docs/my-notes.md.
    (up / "hunter.py").write_text("print('v2')\n")
    (up / "new_module.py").write_text("NEW = 2\n")
    (up / "docs" / "guide.md").write_text("v2 guide\n")
    (up / "state" / "README.md").write_text("changed upstream, must not reach the copy\n")
    (up / "docs" / "my-notes.md").write_text("upstream notes\n")
    _commit(up, "v2")
    res = cloudstate.update_from_upstream(copy, upstream_url=str(up))
    assert res["updated"] and res["method"] == "patch" and res["state_touched"] == [], res
    assert (copy / "hunter.py").read_text() == "print('v2')\n" and (copy / "new_module.py").read_text() == "NEW = 2\n" and (copy / "docs" / "guide.md").read_text() == "v2 guide\n"
    assert (copy / "state" / "README.md").read_text() == "private state lives here\n"
    assert (copy / "state" / "hunter-state.json").read_text() == '{"mine": true}\n' and (copy / ".env").read_text() == "SECRET=1\n"
    assert (copy / "docs" / "my-notes.md").read_text() == "my notes\n"
    assert _git(copy, "status", "--porcelain") == ""
    recorded = json.loads((copy / ".jobhunter" / "upstream.json").read_text())
    assert recorded["commit"] == _git(up, "rev-parse", "main")
    assert res["backup_tag"].startswith("hunter-update-backup-") and res["backup_tag"] in _git(copy, "tag", "--list")
    # Nothing new: no second commit, no second tag.
    tags_before = _git(copy, "tag", "--list")
    again = cloudstate.update_from_upstream(copy, upstream_url=str(up))
    assert again["updated"] is False and "Up to date" in again["note"] and _git(copy, "tag", "--list") == tags_before
    # Rollback: back to the tree before the update, the private files untouched.
    back = cloudstate.rollback_update(copy)
    assert back["rolled_back"] and (copy / "hunter.py").read_text() == "print('v1')\n" and not (copy / "new_module.py").exists()
    assert (copy / "state" / "hunter-state.json").read_text() == '{"mine": true}\n'
    # Updating again after the rollback works (the recorded base is gone with the rollback; the tree match finds it).
    res2 = cloudstate.update_from_upstream(copy, upstream_url=str(up))
    assert res2["updated"] and (copy / "hunter.py").read_text() == "print('v2')\n"
    # A conflicting local edit: reported, nothing changed, tree clean.
    (copy / "hunter.py").write_text("print('student edit')\n")
    _commit(copy, "local edit")
    (up / "hunter.py").write_text("print('v3')\n")
    _commit(up, "v3")
    res3 = cloudstate.update_from_upstream(copy, upstream_url=str(up))
    assert res3["updated"] is False and "does not apply cleanly" in res3["note"] and _git(copy, "status", "--porcelain") == ""
    assert (copy / "hunter.py").read_text() == "print('student edit')\n"
    # Unsaved changes outside state/: refused before anything happens.
    (copy / "hunter.py").write_text("print('uncommitted')\n")
    res4 = cloudstate.update_from_upstream(copy, upstream_url=str(up))
    assert res4["updated"] is False and "unsaved changes" in res4["note"]


def test_a_copy_with_shared_history_still_merges(tmp_path):
    up = _upstream(tmp_path)
    copy = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(up), str(copy)], check=True)
    (up / "hunter.py").write_text("print('v2')\n")
    _commit(up, "v2")
    res = cloudstate.update_from_upstream(copy, upstream_url=str(up))
    assert res["updated"] and res["method"] == "merge" and (copy / "hunter.py").read_text() == "print('v2')\n"
