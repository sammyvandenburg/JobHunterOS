"""Job repository: upserts with deduplication, observations, views, and counts."""

from __future__ import annotations

import json

from jobhunter.db import Database
from jobhunter.models import RawPosting
from jobhunter.services import dedupe
from jobhunter.services.catalog import upsert_employer
from jobhunter.services.normalize import Normalized, normalize
from jobhunter.services.sources.registry import ATS_HOSTS
from jobhunter.util import normalize_company, now_iso, sha256_text, stable_json

VIEWS = ("new", "saved", "applied", "interviews", "finished", "all", "dismissed", "excluded", "uncertain", "old")


def _employer_id(db: Database, raw: RawPosting, norm: Normalized) -> int | None:
    name = norm.employer_name
    if not name:
        return None
    canonical = normalize_company(name)
    row = db.fetchone("SELECT id FROM employers WHERE canonical_name = ?", (canonical,))
    if row:
        return int(row["id"])
    if raw.employer_confidential:
        return None
    # New employer through the discovery path.
    kind = ATS_HOSTS.get(_host(raw.url)) or ATS_HOSTS.get(_host(raw.apply_url))
    return upsert_employer(
        db,
        name,
        origin="discovered",
        discovered_via=raw.source_id,
        regions=_regions_for(norm.country_codes),
        careers_url=None,
        ats_kind=kind if kind in {"greenhouse", "lever", "ashby"} else None,
    )


def _host(url: str | None) -> str:
    from urllib.parse import urlparse

    try:
        return (urlparse(url or "").hostname or "").lower()
    except ValueError:
        return ""


def _regions_for(codes: list[str]) -> list[str]:
    from jobhunter.services.preferences import EUROPE, NORTH_AMERICA

    out = []
    if set(codes) & NORTH_AMERICA:
        out.append("na")
    if set(codes) & EUROPE:
        out.append("eu")
    return out


def upsert_from_posting(db: Database, raw: RawPosting, hunt_run_id: int | None, employer_hosted: bool) -> tuple[int, bool, bool]:
    """Insert or update a job from one observation. Returns (job_id, is_new_job, changed)."""
    norm = normalize(raw)
    ts = now_iso()
    raw_hash = sha256_text(
        stable_json(
            {
                "t": raw.title, "d": norm.description_hash, "l": raw.location_text, "s": raw.salary_text, "u": raw.url, "au": raw.apply_url,
                "cc": norm.country_codes, "rt": norm.remote_type, "rr": norm.remote_restriction, "lc": norm.location_confidence, "req": raw.requisition_id,
                "pa": raw.posted_at, "su": raw.source_updated_at, "et": raw.employment_type, "sal": [norm.salary_min, norm.salary_max, norm.salary_currency, norm.salary_period],
                "sp": norm.sponsorship, "rel": norm.relevance, "emp": raw.employer_name,
            }
        )
    )
    obs = db.fetchone("SELECT * FROM job_observations WHERE source_id = ? AND external_id = ?", (raw.source_id, raw.external_id))
    direct_url = raw.url if (employer_hosted or raw.is_employer_hosted) else None
    if not direct_url and ATS_HOSTS.get(_host(raw.url)):
        direct_url = raw.url
    direct_status = "verified" if direct_url else "none"
    employer_id = _employer_id(db, raw, norm)
    codes_json = json.dumps(norm.country_codes)

    if obs:
        job = db.fetchone("SELECT * FROM jobs WHERE id = ?", (obs["job_id"],))
        changed = obs["raw_hash"] != raw_hash
        db.update(
            "job_observations",
            {"last_observed_at": ts, "raw_hash": raw_hash, "title": raw.title, "location_text": raw.location_text, "url": raw.url, "posted_at_raw": raw.posted_at, "updated_at_raw": raw.source_updated_at, "hunt_run_id": hunt_run_id},
            "id = ?",
            (obs["id"],),
        )
        values = {"last_seen_at": ts, "last_checked_at": ts, "updated_at": ts}
        if _may_reopen(job, raw, employer_hosted):
            values.update({"availability": "open", "availability_note": None})
        if changed:
            values.update(_job_values(norm, raw, employer_id, codes_json, direct_url, direct_status, keep_first_seen=job))
        db.update("jobs", values, "id = ?", (job["id"],))
        return int(job["id"]), False, changed

    # Look for an existing job this observation duplicates.
    candidates = db.fetchall(
        "SELECT * FROM jobs WHERE canonical_key = ? OR (employer_name IS NOT NULL AND lower(employer_name) = lower(?))",
        (dedupe.canonical_key(norm.employer_name, raw.title, norm.country_codes, norm.remote_type, raw.requisition_id), norm.employer_name or ""),
    )
    match = None
    for cand in candidates:
        if dedupe.same_job(cand, norm.employer_name, raw.title, norm.country_codes, norm.remote_type, raw.requisition_id, [raw.url, raw.apply_url], norm.description_text, location_text=raw.location_text):
            match = cand
            break
    if match:
        job_id = int(match["id"])
        values = {"last_seen_at": ts, "last_checked_at": ts, "updated_at": ts}
        if _may_reopen(match, raw, employer_hosted):
            values.update({"availability": "open", "availability_note": None})
        # Prefer employer-hosted details over aggregator copies; keep the earliest first_seen.
        if (employer_hosted or raw.is_employer_hosted) and match.get("direct_url_status") != "verified":
            values.update(_job_values(norm, raw, employer_id, codes_json, direct_url, direct_status, keep_first_seen=match))
        elif not match.get("description_text") and norm.description_text:
            values["description_text"] = norm.description_text
        if not match.get("posted_at") and raw.posted_at:
            values["posted_at"] = raw.posted_at
            values["posted_at_provenance"] = raw.posted_at_provenance
        db.update("jobs", values, "id = ?", (job_id,))
        is_new = False
    else:
        values = _job_values(norm, raw, employer_id, codes_json, direct_url, direct_status, keep_first_seen=None)
        values.update({"first_seen_at": ts, "last_seen_at": ts, "last_checked_at": ts, "created_at": ts, "updated_at": ts, "availability": "open"})
        job_id = db.insert("jobs", values)
        is_new = True
    db.insert(
        "job_observations",
        {
            "job_id": job_id,
            "source_id": raw.source_id,
            "external_id": raw.external_id,
            "url": raw.url,
            "title": raw.title,
            "location_text": raw.location_text,
            "posted_at_raw": raw.posted_at,
            "updated_at_raw": raw.source_updated_at,
            "raw_hash": raw_hash,
            "first_observed_at": ts,
            "last_observed_at": ts,
            "hunt_run_id": hunt_run_id,
        },
    )
    return job_id, is_new, True


def _may_reopen(job: dict, raw: RawPosting, employer_hosted: bool) -> bool:
    """An employer board (or the source that closed it) may reopen a closed job; a stale aggregator copy may not."""
    if job.get("availability") != "closed":
        return True
    if employer_hosted or raw.is_employer_hosted:
        return True
    note = job.get("availability_note") or ""
    return raw.source_id in note


def _job_values(norm: Normalized, raw: RawPosting, employer_id: int | None, codes_json: str, direct_url: str | None, direct_status: str, keep_first_seen: dict | None) -> dict:
    return {
        "location_confidence": norm.location_confidence,
        "canonical_key": dedupe.canonical_key(norm.employer_name, raw.title, norm.country_codes, norm.remote_type, raw.requisition_id),
        "title": raw.title,
        "employer_id": employer_id,
        "employer_name": norm.employer_name,
        "employer_confidential": 1 if raw.employer_confidential else 0,
        "location_text": norm.location_text,
        "country_codes_json": codes_json,
        "remote_type": norm.remote_type,
        "remote_restriction": norm.remote_restriction,
        "employment_type": norm.employment_type,
        "salary_text": norm.salary_text,
        "salary_min": norm.salary_min,
        "salary_max": norm.salary_max,
        "salary_currency": norm.salary_currency,
        "salary_period": norm.salary_period,
        "salary_basis": norm.salary_basis,
        "sponsorship": norm.sponsorship,
        "description_text": norm.description_text,
        "description_hash": norm.description_hash,
        "requisition_id": raw.requisition_id,
        "posted_at": raw.posted_at,
        "posted_at_provenance": raw.posted_at_provenance or "unknown",
        "source_updated_at": raw.source_updated_at,
        "discovery_url": raw.url or None,
        "direct_url": direct_url,
        "direct_url_status": direct_status,
        "apply_url": raw.apply_url or (raw.url if direct_url else None),
        "relevance": norm.relevance,
        "relevance_reason": norm.relevance_reason,
    }


def mark_missing_as_closed(db: Database, source_id: str, seen_external_ids: set[str], ts: str) -> int:
    """For a complete employer-board snapshot, jobs no longer listed are closed (with evidence)."""
    rows = db.fetchall(
        "SELECT o.id AS obs_id, o.external_id, o.job_id FROM job_observations o JOIN jobs j ON j.id = o.job_id "
        "WHERE o.source_id = ? AND j.availability != 'closed'",
        (source_id,),
    )
    n = 0
    for r in rows:
        if r["external_id"] in seen_external_ids:
            continue
        # Only close if no other source still lists it.
        others = db.fetchone(
            "SELECT COUNT(*) AS c FROM job_observations WHERE job_id = ? AND source_id != ? AND datetime(last_observed_at) >= datetime(?, '-7 days')",
            (r["job_id"], source_id, ts),
        )
        if others and others["c"]:
            continue
        db.update(
            "jobs",
            {"availability": "closed", "availability_note": f"No longer listed on {source_id} on {ts[:10]}", "last_checked_at": ts, "updated_at": ts},
            "id = ?",
            (r["job_id"],),
        )
        n += 1
    return n


def mark_unverified(db: Database, source_id: str, note: str, ts: str) -> int:
    """A failed source check means 'couldn't verify', never 'closed'."""
    return db.update(
        "jobs",
        {"availability_note": note, "updated_at": ts},
        "id IN (SELECT job_id FROM job_observations WHERE source_id = ?) AND availability = 'open' AND datetime(last_checked_at) < datetime(?, '-20 days')",
        (source_id, ts),
    ) + db.update(
        "jobs",
        {"availability": "unverified"},
        "id IN (SELECT job_id FROM job_observations WHERE source_id = ?) AND availability = 'open' AND datetime(last_checked_at) < datetime(?, '-20 days')",
        (source_id, ts),
    )


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------
def get_job(db: Database, job_id: int) -> dict | None:
    row = db.fetchone("SELECT * FROM jobs WHERE id = ?", (job_id,))
    if row:
        row["country_codes"] = json.loads(row.get("country_codes_json") or "[]")
        row["observations"] = db.fetchall("SELECT * FROM job_observations WHERE job_id = ? ORDER BY first_observed_at", (job_id,))
    return row


def job_state(db: Database, job_id: int) -> dict:
    """Latest user action state for a job (saved/dismissed) and application stage."""
    saved = db.fetchone(
        "SELECT kind FROM events WHERE job_id = ? AND kind IN ('saved','unsaved') AND reverted_by IS NULL ORDER BY id DESC LIMIT 1", (job_id,)
    )
    dismissed = db.fetchone(
        "SELECT kind, payload_json FROM events WHERE job_id = ? AND kind IN ('dismissed','undismissed') AND reverted_by IS NULL ORDER BY id DESC LIMIT 1", (job_id,)
    )
    fav = db.fetchone(
        "SELECT kind FROM events WHERE job_id = ? AND kind IN ('favorited','unfavorited') AND reverted_by IS NULL ORDER BY id DESC LIMIT 1", (job_id,)
    )
    app = db.fetchone("SELECT * FROM applications WHERE job_id = ?", (job_id,))
    return {
        "favorited": bool(fav and fav["kind"] == "favorited"),
        "saved": bool(saved and saved["kind"] == "saved"),
        "dismissed": bool(dismissed and dismissed["kind"] == "dismissed"),
        "application": app,
        "stage": app["stage"] if app else None,
    }
