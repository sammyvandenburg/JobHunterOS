"""Honest coverage: what the last hunt really checked and where the jobs it found are.

Counts come from the run's own records (source runs, observations, job countries), never from the size of
the catalogue. "Checked" means a source answered; "couldn't check" means it did not; the share of relevant
jobs inside the person's countries says how much of what Hunter can reach is useful to them."""

from __future__ import annotations

import json

from jobhunter.db import Database
from jobhunter.services import preferences as prefs_svc
from jobhunter.services.normalize import country_names


def report(db: Database, run_id: int | None = None) -> dict:
    run = db.fetchone("SELECT * FROM hunt_runs WHERE id = ?", (run_id,)) if run_id else db.fetchone("SELECT * FROM hunt_runs WHERE status != 'running' ORDER BY id DESC LIMIT 1")
    if not run:
        return {"run": None, "note": "No hunt has run yet, so there is nothing to measure."}
    srows = db.fetchall("SELECT s.source_id, s.status, s.fetched_count, s.error, w.kind, w.origin, e.regions_json FROM source_runs s LEFT JOIN watch_sources w ON w.id = s.source_id LEFT JOIN employers e ON e.id = w.employer_id WHERE s.hunt_run_id = ?", (run["id"],))
    sources = {"checked": 0, "ok": 0, "partial": 0, "failed": 0, "skipped": 0, "by_region": {}, "by_kind": {}, "couldnt_check": []}
    for r in srows:
        st = r["status"]
        sources[st if st in sources else "failed"] += 1
        if st in {"ok", "partial"}:
            sources["checked"] += 1
        region = ",".join(sorted(json.loads(r.get("regions_json") or "[]"))) or "unknown"
        sources["by_region"].setdefault(region, {"ok": 0, "partial": 0, "failed": 0, "skipped": 0})
        sources["by_region"][region][st if st in sources["by_region"][region] else "failed"] += 1
        sources["by_kind"].setdefault(r.get("kind") or "?", {"ok": 0, "partial": 0, "failed": 0, "skipped": 0})
        sources["by_kind"][r.get("kind") or "?"][st if st in {"ok", "partial", "failed", "skipped"} else "failed"] += 1
        if st == "failed":
            sources["couldnt_check"].append({"source": r["source_id"], "error": r.get("error")})
    # Jobs first seen in this run, by country, and how many of the relevant ones are inside the person's countries.
    jobs = db.fetchall(
        "SELECT DISTINCT j.id, j.country_codes_json, j.relevance, j.location_confidence, j.remote_type FROM jobs j JOIN job_observations o ON o.job_id = j.id WHERE o.hunt_run_id = ?",
        (run["id"],),
    )
    work = set(prefs_svc.get(db).work_countries or [])
    by_country: dict[str, int] = {}
    found = relevant = inside = outside = unknown = worldwide = 0
    for j in jobs:
        found += 1
        codes = json.loads(j.get("country_codes_json") or "[]")
        for c in codes:
            by_country[c] = by_country.get(c, 0) + 1
        if j.get("relevance") not in {"relevant", "uncertain"}:
            continue
        relevant += 1
        if j.get("location_confidence") == "worldwide":
            worldwide += 1
        elif not codes:
            unknown += 1
        elif work and set(codes) & work:
            inside += 1
        elif work:
            outside += 1
    share = round(100.0 * (inside + worldwide) / relevant) if relevant else None
    top = sorted(by_country.items(), key=lambda kv: -kv[1])[:8]
    if sources["checked"] == 0:
        note = "No source could be checked in this hunt, so nothing can be said about coverage."
    elif relevant == 0:
        note = f"{sources['checked']} sources answered but none of the {found} jobs found was infrastructure work."
    else:
        note = f"{sources['checked']} of {len(srows)} sources answered. Of {relevant} relevant jobs, {inside + worldwide} ({share}%) are open to you in {country_names(sorted(work)) if work else 'your countries'}; {outside} are elsewhere; {unknown} do not say where."
    return {
        "run": {"id": run["id"], "status": run["status"], "finished_at": run.get("finished_at"), "trigger": run.get("trigger")},
        "sources": sources,
        "jobs": {"found": found, "relevant": relevant, "inside_your_countries": inside, "worldwide": worldwide, "outside": outside, "location_unknown": unknown, "share_open_to_you_percent": share, "by_country": [{"country": c, "name": country_names([c]), "jobs": n} for c, n in top]},
        "your_countries": sorted(work),
        "note": note,
    }
