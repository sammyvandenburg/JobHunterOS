"""Imports from user-supplied content: pasted vacancies and job-alert emails.

This is the supported path for LinkedIn and Indeed. Nothing here fetches those
sites. An alert email is parsed into postings with unknown posting dates; the
discovery link is kept with tracking parameters removed. Imported content is
untrusted text: it is stored as description, never interpreted as instructions.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse, urlunparse

from jobhunter.models import RawPosting
from jobhunter.util import strip_html

LINKEDIN_VIEW = re.compile(r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/(?:comm/)?jobs/view/(?:[^/\s?]*-)?(\d{6,})", re.I)
INDEED_JK = re.compile(r"https?://(?:[a-z]{2,3}\.)?indeed\.com/[^\s]*?(?:jk=|/viewjob\?[^\s]*jk=)([a-f0-9]{8,})", re.I)
INDEED_RC = re.compile(r"https?://(?:[a-z]{2,3}\.)?indeed\.com/rc/clk[^\s]*jk=([a-f0-9]{8,})", re.I)
URL_RE = re.compile(r"https?://[^\s<>\"')\]]+", re.I)
TRACKING_PARAMS = {"utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term", "trk", "trackingId", "refId", "lipi", "midToken", "midSig", "trkEmail", "otpToken", "eid", "li", "from", "tk", "alid", "ihl"}


def clean_url(url: str) -> str:
    try:
        p = urlparse(url)
    except ValueError:
        return url
    q = parse_qs(p.query, keep_blank_values=False)
    kept = {k: v for k, v in q.items() if k not in TRACKING_PARAMS}
    query = "&".join(f"{k}={v[0]}" for k, v in kept.items())
    return urlunparse((p.scheme, p.netloc, p.path, "", query, ""))


def parse_alert_email(text: str, source_id: str = "import:alert") -> list[RawPosting]:
    """Extract postings from a LinkedIn or Indeed job-alert email body (plain text or HTML)."""
    body = strip_html(text) if "<" in text and ">" in text else text
    lines = [ln.strip() for ln in body.splitlines()]
    postings: dict[str, RawPosting] = {}
    for idx, line in enumerate(lines):
        for m in URL_RE.finditer(line):
            url = m.group(0).rstrip(".,;")
            li = LINKEDIN_VIEW.search(url)
            ind = INDEED_JK.search(url) or INDEED_RC.search(url)
            if not li and not ind:
                continue
            platform = "linkedin" if li else "indeed"
            ext = li.group(1) if li else ind.group(1)  # type: ignore[union-attr]
            key = f"{platform}:{ext}"
            if key in postings:
                continue
            title, company, location = _context(lines, idx, line)
            if not title:
                continue
            postings[key] = RawPosting(
                source_id=source_id,
                external_id=key,
                title=title,
                employer_name=company,
                url=clean_url(url),
                location_text=location,
                description_text="",
                posted_at=None,
                posted_at_provenance=None,
                raw={"platform": platform, "imported_from": "alert email"},
            )
    return list(postings.values())


def _context(lines: list[str], idx: int, line: str) -> tuple[str | None, str | None, str | None]:
    """Find title / company / location in the lines around a job link."""
    window = [ln for ln in lines[max(0, idx - 4) : idx + 1] if ln and not URL_RE.fullmatch(ln)]
    stripped = URL_RE.sub("", line).strip(" -:|")
    candidates = [ln for ln in window if not ln.lower().startswith(("view job", "apply", "see all", "easy apply"))]
    if stripped and stripped not in candidates:
        candidates.append(stripped)
    candidates = [c for c in candidates if c]
    if not candidates:
        return None, None, None
    # Common layouts: "Title\nCompany · Location" or "Title - Company - Location"
    title = candidates[0]
    company = location = None
    for c in candidates[1:]:
        if "·" in c:
            parts = [p.strip() for p in c.split("·")]
            company, location = parts[0], (parts[1] if len(parts) > 1 else None)
            break
    if company is None and " - " in title:
        parts = [p.strip() for p in title.split(" - ")]
        title = parts[0]
        company = parts[1] if len(parts) > 1 else None
        location = parts[2] if len(parts) > 2 else None
    if len(title) > 140:
        return None, None, None
    return title, company, location


def posting_from_text(
    text: str,
    title: str | None,
    company: str | None,
    url: str | None,
    location: str | None = None,
    source_id: str = "import:pasted",
) -> RawPosting:
    """Build a posting from text the user pasted. The URL is stored, not fetched."""
    body = strip_html(text) if "<" in text and ">" in text else text
    if not title:
        first = next((ln.strip() for ln in body.splitlines() if ln.strip()), "")
        title = first[:120] or "Untitled vacancy"
    ext = re.sub(r"[^a-z0-9]+", "-", f"{company or 'unknown'}-{title}".lower())[:80]
    return RawPosting(
        source_id=source_id,
        external_id=ext,
        title=title.strip(),
        employer_name=company,
        url=clean_url(url) if url else "",
        location_text=location,
        description_text=body,
        raw={"imported_from": "pasted text"},
    )


def detect_ats_link(url: str) -> dict | None:
    """Recognize public ATS job URLs so Hunter can do an allowed source lookup."""
    try:
        p = urlparse(url)
    except ValueError:
        return None
    host = (p.hostname or "").lower()
    parts = [x for x in p.path.split("/") if x]
    if host in {"boards.greenhouse.io", "job-boards.greenhouse.io", "job-boards.eu.greenhouse.io", "boards.eu.greenhouse.io"}:
        # /{token}/jobs/{id}
        if len(parts) >= 3 and parts[1] == "jobs":
            return {"kind": "greenhouse", "token": parts[0], "job_id": parts[2], "eu": ".eu." in host}
    if host in {"jobs.lever.co", "jobs.eu.lever.co"}:
        if len(parts) >= 2:
            return {"kind": "lever", "site": parts[0], "job_id": parts[1].split("?")[0], "region": "eu" if "eu" in host else "us"}
    if host == "jobs.ashbyhq.com":
        if len(parts) >= 2:
            return {"kind": "ashby", "board": parts[0], "job_id": parts[1]}
    return None
