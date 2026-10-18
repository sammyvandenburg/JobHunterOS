"""Explicit feedback: dismissals, reasons, and suggested (never automatic) preference changes."""

from __future__ import annotations

from jobhunter.db import Database
from jobhunter.util import now_iso

SUGGEST_AFTER = 3


def record(db: Database, job_id: int, kind: str, reason_code: str | None = None, note: str | None = None) -> int:
    return db.insert("feedback", {"job_id": job_id, "kind": kind, "reason_code": reason_code, "note": note, "created_at": now_iso()})


def is_dismissed(db: Database, job_id: int) -> bool:
    row = db.fetchone(
        "SELECT kind FROM events WHERE job_id = ? AND kind IN ('dismissed','undismissed') AND reverted_by IS NULL ORDER BY id DESC LIMIT 1",
        (job_id,),
    )
    return bool(row and row["kind"] == "dismissed")


def active_flags(db: Database) -> dict:
    """Preference flags derived only from confirmed preference changes."""
    prefs = db.get_setting("preferences") or {}
    extras = prefs.get("extras") or {}
    flags = {}
    fc = extras.get("fewer_consultancy")
    if fc and fc.get("value"):
        flags["fewer_consultancy"] = fc.get("strength", "nice")
    return flags


def suggestions(db: Database) -> list[dict]:
    """Patterns in dismissals that deserve one explicit question. Nothing is applied here."""
    out = []
    extras = (db.get_setting("preferences") or {}).get("extras") or {}
    dismissed_reason = db.fetchall("SELECT reason_code, COUNT(*) AS n FROM feedback WHERE kind = 'not_interested' AND reason_code IS NOT NULL GROUP BY reason_code")
    for row in dismissed_reason:
        if row["n"] >= SUGGEST_AFTER:
            code = row["reason_code"]
            if code == "consultancy" and not extras.get("fewer_consultancy"):
                out.append({"key": "fewer_consultancy", "question": "You said 'not interested' to several consultancy jobs. Should I show fewer consultancy jobs?", "count": row["n"]})
            elif code == "too_senior" and not extras.get("avoid_senior"):
                out.append({"key": "avoid_senior", "question": "You dismissed several jobs as too senior. Should I stop showing senior stretches?", "count": row["n"]})
            elif code == "location" and not extras.get("location_strict"):
                out.append({"key": "location_strict", "question": "Several dismissals were about location. Should I treat your work countries as a must?", "count": row["n"]})
    return out


def summary(db: Database) -> dict:
    return {
        "dismissed": db.fetchone("SELECT COUNT(*) AS n FROM feedback WHERE kind = 'not_interested'")["n"],
        "by_reason": db.fetchall("SELECT reason_code, COUNT(*) AS n FROM feedback WHERE kind = 'not_interested' GROUP BY reason_code"),
        "suggestions": suggestions(db),
    }
