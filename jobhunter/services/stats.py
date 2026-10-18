"""Honest counts. Every number comes from stored records, lifetime by default."""

from __future__ import annotations

from jobhunter.db import Database


def totals(db: Database) -> dict:
    q = db.fetchone
    found = q("SELECT COUNT(*) AS n FROM jobs")["n"]
    relevant = q("SELECT COUNT(*) AS n FROM jobs WHERE relevance IN ('relevant','uncertain')")["n"]
    recommended = q("SELECT COUNT(DISTINCT job_id) AS n FROM recommendations")["n"]
    applied = q("SELECT COUNT(DISTINCT job_id) AS n FROM events WHERE kind = 'applied' AND reverted_by IS NULL")["n"]
    contacted = q("SELECT COUNT(DISTINCT job_id) AS n FROM events WHERE kind = 'contacted' AND reverted_by IS NULL")["n"]
    replies = q(
        "SELECT COUNT(DISTINCT job_id) AS n FROM events WHERE kind = 'reply_received' AND reverted_by IS NULL "
        "AND (json_extract(payload_json, '$.automated') IS NULL OR json_extract(payload_json, '$.automated') = 0)"
    )["n"]
    interviews = q("SELECT COUNT(DISTINCT job_id) AS n FROM events WHERE kind = 'interview' AND reverted_by IS NULL AND json_extract(payload_json, '$.ignored_for_pipeline') IS NULL")["n"]
    offers = q("SELECT COUNT(DISTINCT job_id) AS n FROM events WHERE kind = 'offer' AND reverted_by IS NULL")["n"]
    saved = q(
        "SELECT COUNT(*) AS n FROM (SELECT job_id, kind FROM events e WHERE kind IN ('saved','unsaved') AND reverted_by IS NULL "
        "AND id = (SELECT MAX(id) FROM events WHERE job_id = e.job_id AND kind IN ('saved','unsaved') AND reverted_by IS NULL)) WHERE kind = 'saved'"
    )["n"]
    return {
        "found": found,
        "relevant": relevant,
        "recommended": recommended,
        "saved": saved,
        "applied": applied,
        "contacted": contacted,
        "recruiter_replies": replies,
        "interviews": interviews,
        "offers": offers,
        "hunts": q("SELECT COUNT(*) AS n FROM hunt_runs WHERE trigger IN ('manual','scheduled','demo')")["n"],
        "label": "Lifetime totals",
    }


def summary_line(t: dict) -> str:
    parts = [f"{t['found']} jobs found", f"{t['recommended']} recommended", f"{t['applied']} applications"]
    if t["recruiter_replies"]:
        parts.append(f"{t['recruiter_replies']} recruiter replies")
    if t["interviews"]:
        parts.append(f"{t['interviews']} interview{'s' if t['interviews'] != 1 else ''}")
    return " · ".join(parts)
