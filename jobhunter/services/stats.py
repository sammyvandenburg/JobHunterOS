"""Honest counts. Every number comes from stored records, lifetime by default."""

from __future__ import annotations

from jobhunter.db import Database


def totals(db: Database) -> dict:
    q = db.fetchone
    found = q("SELECT COUNT(*) AS n FROM jobs")["n"]
    relevant = q("SELECT COUNT(*) AS n FROM jobs WHERE relevance IN ('relevant','uncertain')")["n"]
    from jobhunter.services import views  # local import: views imports stats lazily too

    # Recommended = jobs recommended right now (the exact set the Recommended card and New jobs open),
    # not every job that was ever scored into a group.
    recommended = views.current_recommended_count(db)
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
    favorites = q(
        "SELECT COUNT(*) AS n FROM (SELECT job_id, kind FROM events e WHERE kind IN ('favorited','unfavorited') AND reverted_by IS NULL "
        "AND id = (SELECT MAX(id) FROM events WHERE job_id = e.job_id AND kind IN ('favorited','unfavorited') AND reverted_by IS NULL)) WHERE kind = 'favorited'"
    )["n"]
    return {
        "found": found,
        "relevant": relevant,
        "recommended": recommended,
        "favorites": favorites,
        "saved": saved,
        "applied": applied,
        "contacted": contacted,
        "recruiter_replies": replies,
        "interviews": interviews,
        "offers": offers,
        "hunts": q("SELECT COUNT(*) AS n FROM hunt_runs WHERE trigger IN ('manual','scheduled','demo')")["n"],
        "label": "Lifetime totals, except Recommended, which is what is on your list right now",
    }


def summary_line(t: dict) -> str:
    parts = [f"{t['recommended']} jobs worth a look", f"{t['applied']} applied"]
    if t["recruiter_replies"]:
        parts.append(f"{t['recruiter_replies']} repl{'y' if t['recruiter_replies'] == 1 else 'ies'}")
    if t["interviews"]:
        parts.append(f"{t['interviews']} interview{'s' if t['interviews'] != 1 else ''}")
    parts.append(f"{t['found']} found in total")
    return " · ".join(parts)
