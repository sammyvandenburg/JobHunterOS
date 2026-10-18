"""Find the employer-hosted listing for a job found on an aggregator.

Rules: compare company, role title, and location; title similarity alone is
not enough. Never fabricate a URL. When nothing confirms the original, the
job says "I couldn't confirm the original listing."
"""

from __future__ import annotations

import json
from difflib import SequenceMatcher

from jobhunter.db import Database
from jobhunter.services import dedupe
from jobhunter.services.http import SafeClient
from jobhunter.services.sources import get_adapter
from jobhunter.services.sources.registry import ATS_HOSTS
from jobhunter.util import normalize_company, now_iso

_board_cache: dict[str, list] = {}


def _host(url: str | None) -> str:
    from urllib.parse import urlparse

    try:
        return (urlparse(url or "").hostname or "").lower()
    except ValueError:
        return ""


def resolve(db: Database, job: dict, client: SafeClient | None = None, allow_network: bool = True) -> dict:
    """Try to attach a verified direct URL to the job. Returns a status dict."""
    if job.get("direct_url_status") == "verified" and job.get("direct_url"):
        return {"status": "verified", "url": job["direct_url"], "how": "already verified"}
    # 1. The discovery or apply link is itself an employer-hosted ATS page.
    for key in ("apply_url", "discovery_url"):
        url = job.get(key)
        if url and ATS_HOSTS.get(_host(url)):
            _set(db, job["id"], url, "verified")
            return {"status": "verified", "url": url, "how": f"{key} points at the employer's ATS"}
    # 2. Known employer with a watched ATS board: compare title + location.
    employer = db.fetchone("SELECT * FROM employers WHERE id = ?", (job.get("employer_id"),)) if job.get("employer_id") else None
    if employer and employer.get("ats_kind") in {"greenhouse", "lever", "ashby"} and employer.get("ats_token") and allow_network:
        postings = _board_postings(db, employer, client)
        best = _best_match(job, postings)
        if best:
            _set(db, job["id"], best.url, "verified")
            return {"status": "verified", "url": best.url, "how": "matched on the employer's board by title and location"}
        _set(db, job["id"], None, "unconfirmed")
        return {"status": "unconfirmed", "url": None, "how": "no matching posting on the employer's board"}
    _set(db, job["id"], None, "unconfirmed")
    return {"status": "unconfirmed", "url": None, "how": "no employer board known for this company"}


def _set(db: Database, job_id: int, url: str | None, status: str) -> None:
    db.update("jobs", {"direct_url": url, "direct_url_status": status, "updated_at": now_iso()}, "id = ?", (job_id,))


def _board_postings(db: Database, employer: dict, client: SafeClient | None):
    key = f"{employer['ats_kind']}:{employer['ats_token']}"
    if key in _board_cache:
        return _board_cache[key]
    from jobhunter.services.catalog import source_config

    own = client is None
    client = client or SafeClient()
    try:
        adapter = get_adapter(employer["ats_kind"])
        cfg = source_config(employer["ats_kind"], employer["ats_token"], employer["name"], employer.get("ats_region"))
        result = adapter.fetch(client, key, cfg)
    finally:
        if own:
            client.close()
    postings = result.postings if result.status in {"ok", "partial"} else []
    _board_cache[key] = postings
    return postings


def _loc_tokens(text: str | None) -> set[str]:
    import re

    return {w for w in re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower()).split() if len(w) >= 4 and w not in {"remote", "hybrid", "office", "united", "states", "kingdom"}}


def _best_match(job: dict, postings: list):
    from jobhunter.services.normalize import countries_from_text

    want_title = dedupe.normalize_title(job["title"])
    want_codes = set(json.loads(job.get("country_codes_json") or "[]"))
    want_loc = _loc_tokens(job.get("location_text"))
    want_company = normalize_company(job.get("employer_name"))
    title_matches = []
    for p in postings:
        if want_company and p.employer_name and normalize_company(p.employer_name) != want_company:
            continue
        ratio = SequenceMatcher(None, want_title, dedupe.normalize_title(p.title)).ratio()
        if ratio < 0.9:
            continue
        if job.get("requisition_id") and p.requisition_id and job["requisition_id"] != p.requisition_id:
            continue
        title_matches.append((ratio, p))
    if not title_matches:
        return None
    best, best_score = None, 0.0
    for ratio, p in title_matches:
        codes, _ = countries_from_text(p.location_text)
        codes = set(codes) | {c.upper() for c in p.country_codes}
        if want_codes and codes:
            if not (want_codes & codes):
                continue
        elif want_loc or _loc_tokens(p.location_text):
            # Location known on at least one side but not comparable by country: require shared place words.
            if not (want_loc & _loc_tokens(p.location_text)):
                continue
        elif len(title_matches) != 1:
            # No location on either side: only an unambiguous single posting counts.
            continue
        if ratio > best_score:
            best, best_score = p, ratio
    return best


def clear_cache() -> None:
    _board_cache.clear()
