"""Conservative cross-source deduplication.

Two observations become one job only when the evidence is strong: identical
canonical links, or the same employer + same title + compatible location (and
no conflicting requisition ids). Different requisition ids or materially
different locations stay separate. Source observations are always preserved.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse, urlunparse

from jobhunter.util import normalize_company

_TITLE_NOISE = re.compile(r"\((?:m/w/d|f/m/d|m/f/d|w/m/d|all genders|he/she/they|d/f/m|m/f/x)\)|\b(?:m/w/d|f/m/d|all genders)\b|[\-–—|:]+\s*(?:remote|hybrid|onsite).*$", re.I)


def normalize_title(title: str) -> str:
    t = _TITLE_NOISE.sub(" ", title or "")
    t = re.sub(r"[^a-z0-9 ]+", " ", t.lower())
    t = re.sub(r"\s+", " ", t).strip()
    return t


def canonical_url(url: str | None) -> str | None:
    if not url:
        return None
    try:
        p = urlparse(url.strip())
    except ValueError:
        return None
    host = (p.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = re.sub(r"/+$", "", p.path or "")
    path = re.sub(r"/apply$|/application$", "", path)
    return urlunparse(("https", host, path, "", "", ""))


def location_bucket(country_codes: list[str], remote_type: str) -> str:
    if remote_type == "remote" and not country_codes:
        return "remote"
    codes = sorted(set(country_codes))
    return ",".join(codes) if codes else "unknown"


def canonical_key(employer_name: str | None, title: str, country_codes: list[str], remote_type: str, requisition_id: str | None) -> str:
    return "|".join(
        [normalize_company(employer_name), normalize_title(title), location_bucket(country_codes, remote_type), (requisition_id or "").strip().lower()]
    )


def locations_compatible(a_codes: list[str], b_codes: list[str], a_remote: str, b_remote: str) -> bool:
    if not a_codes or not b_codes:
        return a_remote == b_remote or "remote" in (a_remote, b_remote)
    return bool(set(a_codes) & set(b_codes))


def _shingles(text: str, n: int = 4) -> set[str]:
    words = re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower()).split()
    return {" ".join(words[i : i + n]) for i in range(max(0, len(words) - n + 1))}


def description_similarity(a: str, b: str) -> float:
    sa, sb = _shingles(a[:6000]), _shingles(b[:6000])
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def same_job(existing: dict, employer_name: str | None, title: str, codes: list[str], remote_type: str, requisition_id: str | None, urls: list[str | None], description: str) -> bool:
    """Decide whether a new observation is the same vacancy as an existing job."""
    ex_req = (existing.get("requisition_id") or "").strip().lower()
    new_req = (requisition_id or "").strip().lower()
    if ex_req and new_req and ex_req != new_req:
        return False
    ex_urls = {canonical_url(existing.get(k)) for k in ("direct_url", "apply_url", "discovery_url")} - {None}
    if any(canonical_url(u) in ex_urls for u in urls if u):
        return True
    if normalize_company(existing.get("employer_name")) != normalize_company(employer_name) or not normalize_company(employer_name):
        return False
    if normalize_title(existing.get("title") or "") != normalize_title(title):
        return False
    import json

    ex_codes = json.loads(existing.get("country_codes_json") or "[]")
    if not locations_compatible(ex_codes, codes, existing.get("remote_type") or "unknown", remote_type):
        return False
    if ex_req and new_req and ex_req == new_req:
        return True
    # Same employer, same title, compatible location: confirm with description similarity when both exist.
    ex_desc = existing.get("description_text") or ""
    if ex_desc and description:
        return description_similarity(ex_desc, description) >= 0.5 or len(description) < 200 or len(ex_desc) < 200
    return True
