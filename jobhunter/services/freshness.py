"""Freshness buckets. Fit outranks freshness; this only labels and nudges."""

from __future__ import annotations

from datetime import datetime

from jobhunter.util import days_ago

WINDOW_DAYS = 14


def bucket(job: dict, ref: datetime | None = None) -> tuple[str, str]:
    """Return (bucket, label). Buckets: fresh | recent | older | old | unknown."""
    posted = job.get("posted_at") if job.get("posted_at_provenance") not in (None, "unknown") else None
    age = days_ago(posted, ref) if posted else None
    if age is None:
        seen = days_ago(job.get("first_seen_at"), ref)
        seen_txt = f"first seen by Hunter {int(seen)} day(s) ago" if seen is not None else "first seen by Hunter recently"
        return "unknown", f"No posting date ({seen_txt})"
    if age <= 2:
        return "fresh", "Posted in the last 2 days"
    if age <= 7:
        return "recent", f"Posted {int(age)} days ago"
    if age <= WINDOW_DAYS:
        return "older", f"Posted {int(age)} days ago"
    return "old", f"Posted {int(age)} days ago (older than 2 weeks)"


def freshness_bonus(b: str) -> float:
    return {"fresh": 4.0, "recent": 2.5, "older": 1.0, "unknown": 0.0, "old": -1.0}[b]


def verification_label(job: dict) -> str:
    av = job.get("availability")
    if av == "closed":
        return "No longer listed: " + (job.get("availability_note") or "")
    if av == "unverified":
        return "Could not check it recently: " + (job.get("availability_note") or "the site could not be reached")
    checked = job.get("last_checked_at") or job.get("last_seen_at")
    return f"Still listed on {(checked or '')[:10]}"
