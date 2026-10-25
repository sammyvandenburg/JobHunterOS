"""Export, backup, and delete-my-data."""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

from jobhunter.config import AppContext, write_private_file
from jobhunter.db import Database
from jobhunter.util import now_iso

EXPORT_TABLES = [
    "source_documents", "profile_versions", "profile_questions", "preference_changes", "employers", "watch_sources",
    "hunt_runs", "source_runs", "jobs", "job_observations", "matches", "recommendations", "applications", "events",
    "drafts", "contacts", "followups", "email_accounts", "email_messages", "notifications", "consents", "feedback",
    "schedule", "settings", "handoffs", "job_notes",
    "cv_variants", "cv_revisions", "requests", "research", "user_texts", "board_sync",
]


def export_all(ctx: AppContext, db: Database) -> dict:
    out_dir = ctx.paths.exports_dir / f"export-{now_iso().replace(':', '')}"
    out_dir.mkdir(parents=True, exist_ok=True)
    counts = {}
    for table in EXPORT_TABLES:
        rows = db.fetchall(f"SELECT * FROM {table}")
        counts[table] = len(rows)
        write_private_file(out_dir / f"{table}.json", json.dumps(rows, indent=1, default=str).encode("utf-8"))
    docs_dir = out_dir / "documents"
    docs_dir.mkdir(exist_ok=True)
    for row in db.fetchall("SELECT stored_path FROM source_documents"):
        p = Path(row["stored_path"])
        if p.exists():
            shutil.copy2(p, docs_dir / p.name)
    write_private_file(out_dir / "README.txt", (
        b"JobHunterOS export.\n\n"
        b"JSON per table: your profile, preferences, jobs, matches, applications (with the submission snapshots), your own answers and letters (user_texts), "
        b"CV names and versions (cv_variants, cv_revisions: metadata with the hash of every version), research, contacts, follow-ups, notes, requests.\n"
        b"documents/: the original files themselves (CVs, LinkedIn PDF), byte-identical, named by their hash. A version whose file was not on this computer is listed in cv_revisions but has no file here.\n"
        b"Secrets (tokens, passwords) are not exported.\n"))
    return {"path": str(out_dir), "tables": counts, "note": "Secrets (tokens, passwords) are not included."}


def backup(ctx: AppContext, db: Database) -> dict:
    ctx.paths.backups_dir.mkdir(parents=True, exist_ok=True)
    target = ctx.paths.backups_dir / f"jobhunter-{now_iso().replace(':', '')}.sqlite3"
    dest = sqlite3.connect(str(target))
    try:
        db.conn.backup(dest)
    finally:
        dest.close()
    return {"path": str(target), "note": "Documents live beside the database in the documents folder; copy that folder too for a full backup."}


def delete_all(ctx: AppContext, db: Database, confirm: str) -> dict:
    """Delete local personal data. Secrets are removed first so tokens cannot be reused."""
    if confirm != "DELETE":
        raise ValueError("Type DELETE to confirm.")
    from jobhunter.services.secrets import SecretStore, SecretStoreLocked

    removed = {"secrets": 0}
    problems: list[str] = []
    try:
        store = SecretStore(ctx.paths)
        for key in store.list_keys():
            store.delete(key)
            removed["secrets"] += 1
    except SecretStoreLocked:
        problems.append(
            "The secret store could not be read (wrong passphrase or damaged file), so stored tokens could not be removed one by one. "
            "The secrets file itself was deleted with the rest. If an operating-system keyring was in use, check it for 'jobhunteros' entries."
        )
    except Exception as exc:
        problems.append(f"Stored tokens could not be removed one by one ({type(exc).__name__}). The secrets file itself was deleted with the rest.")
    db.close()
    root = ctx.paths.root
    for sub in ("documents", "derived", "exports", "cache", "logs", "locks"):
        p = root / sub
        if p.exists():
            shutil.rmtree(p, ignore_errors=True)
            removed[sub] = 1
    for f in root.glob("jobhunter.sqlite3*"):
        f.unlink(missing_ok=True)
        removed["database"] = 1
    for f in (ctx.paths.secrets_file, ctx.paths.dashboard_secret_file):
        if f.exists():
            f.unlink()
    backups = list(ctx.paths.backups_dir.glob("*")) if ctx.paths.backups_dir.exists() else []
    return {
        "deleted": removed,
        "problems": problems,
        "backups_kept": [str(b) for b in backups],
        "limits": [
            "Backups in the backups folder were kept on purpose; delete them yourself if you want them gone.",
            "Copies you exported or emailed, and data held by Google or your mail provider, are outside Hunter's control.",
            "If a Gmail connection existed, remove Hunter at https://myaccount.google.com/permissions too.",
        ],
    }
