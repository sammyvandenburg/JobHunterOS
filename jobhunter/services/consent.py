"""Consent records: what the user allowed, when, and whether it was revoked."""

from __future__ import annotations

import json

from jobhunter.db import Database
from jobhunter.util import now_iso

KINDS = {
    "readiness": "You confirmed your CV and LinkedIn profile are finished.",
    "profile_approved": "You approved the profile Hunter built from your materials.",
    "digest_email": "Hunter may email job digests to one confirmed address.",
    "digest_sender": "Hunter may send those digests through the email account you connected.",
    "mailbox_read": "Hunter may read a bounded set of your job-search emails.",
    "model_processing": "Hunter may send minimized job and profile text to Claude for explanations.",
    "github_read": "Hunter may read the public repositories you listed.",
}


def grant(db: Database, kind: str, details: dict | None = None) -> int:
    if kind not in KINDS:
        raise ValueError(f"unknown consent kind: {kind}")
    with db.tx():
        # Close any previous open grant of the same kind so history stays linear.
        db.execute(
            "UPDATE consents SET revoked_at = ? WHERE kind = ? AND granted = 1 AND revoked_at IS NULL",
            (now_iso(), kind),
        )
        return db.insert(
            "consents",
            {"kind": kind, "granted": 1, "details_json": json.dumps(details or {}), "granted_at": now_iso()},
        )


def revoke(db: Database, kind: str) -> int:
    with db.tx():
        return db.update(
            "consents", {"revoked_at": now_iso()}, "kind = ? AND granted = 1 AND revoked_at IS NULL", (kind,)
        )


def active(db: Database, kind: str) -> dict | None:
    return db.fetchone(
        "SELECT * FROM consents WHERE kind = ? AND granted = 1 AND revoked_at IS NULL ORDER BY id DESC LIMIT 1",
        (kind,),
    )


def is_granted(db: Database, kind: str) -> bool:
    return active(db, kind) is not None


def details(db: Database, kind: str) -> dict:
    row = active(db, kind)
    if not row:
        return {}
    try:
        return json.loads(row["details_json"] or "{}")
    except json.JSONDecodeError:
        return {}


def summary(db: Database) -> list[dict]:
    out = []
    for kind, desc in KINDS.items():
        row = active(db, kind)
        out.append(
            {
                "kind": kind,
                "description": desc,
                "granted": row is not None,
                "granted_at": row["granted_at"] if row else None,
                "details": json.loads(row["details_json"]) if row and row["details_json"] else {},
            }
        )
    return out
