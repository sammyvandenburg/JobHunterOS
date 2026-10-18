"""Imports from user-supplied content: pasted vacancies and job-alert emails.

This is the supported path for LinkedIn and Indeed. Nothing here fetches those
sites. An alert email is parsed into postings with unknown posting dates; the
discovery link is kept with tracking parameters removed (never the whole link).
HTML alerts keep their anchors, so "View job" buttons still yield the vacancy
link and the job card around it. Imported content is untrusted text: it is
stored as description, never interpreted as instructions.
"""

from __future__ import annotations

import html as html_lib
import re
from urllib.parse import parse_qsl, unquote, urlencode, urlparse, urlunparse

from jobhunter.models import RawPosting
from jobhunter.services.dedupe import TRACKING_PARAMS as _SHARED_TRACKING
from jobhunter.util import strip_html

LINKEDIN_VIEW = re.compile(r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/(?:comm/)?jobs/view/(?:[^/\s?]*-)?(\d{6,})", re.I)
INDEED_JK = re.compile(r"https?://(?:[a-z]{2,3}\.)?indeed\.com/[^\s]*?(?:jk=|/viewjob\?[^\s]*jk=)([a-f0-9]{8,})", re.I)
INDEED_RC = re.compile(r"https?://(?:[a-z]{2,3}\.)?indeed\.com/rc/clk[^\s]*jk=([a-f0-9]{8,})", re.I)
URL_RE = re.compile(r"https?://[^\s<>\"')\]]+", re.I)
TRACKING_PARAMS = set(_SHARED_TRACKING) | {"trackingid", "refid", "midtoken", "midsig", "trkemail", "otptoken", "lipi", "licu", "eid", "alid", "ihl", "from", "tk", "li", "trk", "rid", "mcid", "cid", "vjk", "advn", "sjdu", "tag", "xkcb", "xpse", "xfps"}
REDIRECT_PARAMS = ("url", "u", "redirect", "redirect_url", "target", "destination", "dest", "link", "redir", "r", "q")
BOILERPLATE = re.compile(r"^(view job|view jobs?|apply( now)?|easy apply|see (all|more)( jobs)?|save( job)?|dismiss|not interested|unsubscribe|manage (your )?(alerts?|preferences)|help|privacy|terms|you are receiving|this email was|©|jobs? you may be interested in|new jobs? (for you|match)|based on your|recommended for you|more jobs|show more)\b", re.I)
ANCHOR_RE = re.compile(r"<a\b[^>]*?href\s*=\s*([\"'])(.*?)\1[^>]*>(.*?)</a>", re.I | re.S)
PIXEL_RE = re.compile(r"https?://\S*(?:pixel|beacon|/open\b|/o/|\.gif|track/open|imp\.)\S*", re.I)


def html_to_text_with_links(raw: str) -> str:
    """Like strip_html, but each link becomes 'anchor text' then its URL on the next line, so job
    links survive and stay next to the card they belong to."""

    def repl(m: re.Match) -> str:
        url = html_lib.unescape(m.group(2)).strip()
        inner = m.group(3)
        if not url.lower().startswith(("http://", "https://")):
            return inner
        return f"\n{inner}\n{url}\n"

    return strip_html(ANCHOR_RE.sub(repl, raw))


def looks_like_html(text: str) -> bool:
    return bool(re.search(r"<\s*(html|body|table|div|a|p|br|td|span)\b", text or "", re.I))


def unwrap_redirect(url: str, depth: int = 0) -> str:
    """click.example.com/r?url=https%3A%2F%2Fwww.linkedin.com%2F... -> the real link (two levels at most)."""
    if depth > 2:
        return url
    try:
        p = urlparse(url)
    except ValueError:
        return url
    for key, value in parse_qsl(p.query, keep_blank_values=False):
        if key.lower() in REDIRECT_PARAMS:
            inner = unquote(value)
            if inner.lower().startswith(("http://", "https://")) and (LINKEDIN_VIEW.search(inner) or INDEED_JK.search(inner) or INDEED_RC.search(inner) or depth == 0):
                return unwrap_redirect(inner, depth + 1)
    return url


def clean_url(url: str) -> str:
    """Drop tracking parameters and fragments, keep everything that names the vacancy (jk=, id=, currentJobId=)."""
    url = unwrap_redirect((url or "").strip().rstrip(".,;)"))
    try:
        p = urlparse(url)
    except ValueError:
        return url
    kept = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=False) if k.lower() not in TRACKING_PARAMS]
    return urlunparse((p.scheme, p.netloc, p.path, "", urlencode(kept), ""))


def job_link(url: str) -> tuple[str, str] | None:
    """(platform, external id) for a LinkedIn or Indeed vacancy link, else None."""
    u = unwrap_redirect(url)
    li = LINKEDIN_VIEW.search(u)
    if li:
        return "linkedin", li.group(1)
    ind = INDEED_JK.search(u) or INDEED_RC.search(u)
    if ind:
        return "indeed", ind.group(1)
    return None


def count_job_links(text: str) -> int:
    return len({job_link(m.group(0)) for m in URL_RE.finditer(text or "")} - {None})


def strip_tracking_links(text: str) -> str:
    """For stored email text: tracking pixels go, every other link stays with its tracking parameters removed."""

    def repl(m: re.Match) -> str:
        u = m.group(0)
        if PIXEL_RE.fullmatch(u) and not job_link(u):
            return "[tracking image removed]"
        return clean_url(u)

    return URL_RE.sub(repl, text or "")


def parse_alert_email(text: str, source_id: str = "import:alert") -> list[RawPosting]:
    """Extract postings from a LinkedIn or Indeed job-alert email body (plain text or HTML).

    Several vacancies per email are normal; the same vacancy linked twice (title and "View job")
    is one posting, built from the better context. Posting dates stay unknown: an alert's delivery
    time is not when the job was posted.
    """
    body = html_to_text_with_links(text) if looks_like_html(text) else (text or "")
    lines = [ln.strip() for ln in body.splitlines()]
    found: dict[str, dict] = {}
    for idx, line in enumerate(lines):
        for m in URL_RE.finditer(line):
            info = job_link(m.group(0))
            if not info:
                continue
            platform, ext = info
            key = f"{platform}:{ext}"
            title, company, location = _context(lines, idx, line)
            score = (2 if title and not BOILERPLATE.match(title) else 0) + (1 if company else 0) + (1 if location else 0)
            prev = found.get(key)
            if prev is None or score > prev["score"]:
                found[key] = {"score": score, "platform": platform, "ext": ext, "url": clean_url(m.group(0)), "title": title, "company": company, "location": location}
    postings: list[RawPosting] = []
    for key, f in found.items():
        title = f["title"]
        if not title or BOILERPLATE.match(title):
            continue
        postings.append(
            RawPosting(
                source_id=source_id,
                external_id=key,
                title=title,
                employer_name=f["company"],
                url=f["url"],
                location_text=f["location"],
                description_text="",
                posted_at=None,
                posted_at_provenance=None,
                raw={"platform": f["platform"], "imported_from": "alert email"},
            )
        )
    return postings


def _split_company_location(text: str) -> tuple[str | None, str | None]:
    for sep in (" · ", "·", " | ", " – ", " — ", " - "):
        if sep in text:
            parts = [p.strip() for p in text.split(sep) if p.strip()]
            if len(parts) >= 2:
                return parts[0], parts[1]
    return None, None


def _context(lines: list[str], idx: int, line: str) -> tuple[str | None, str | None, str | None]:
    """Find title / company / location in the job card around a link (lines before and after)."""

    def usable(ln: str) -> bool:
        return bool(ln) and not URL_RE.search(ln) and not BOILERPLATE.match(ln) and len(ln) <= 160

    before = [ln for ln in lines[max(0, idx - 4) : idx] if usable(ln)]
    after = [ln for ln in lines[idx + 1 : idx + 4] if usable(ln)]
    inline = URL_RE.sub("", line).strip(" -:|·")
    candidates = ([inline] if usable(inline) else []) + list(reversed(before)) + after
    if not candidates:
        return None, None, None
    title = company = location = None
    placeish = re.compile(r"\b(netherlands|germany|remote|hybrid|on-?site|united|kingdom|france|spain|belgium|austria|switzerland|ireland|poland|portugal|italy|sweden|denmark|norway|finland|canada|usa|[A-Z]{2})\b")
    for c in candidates:
        if "·" in c:  # "Company · Location" is how alert cards are laid out
            comp, loc = _split_company_location(c)
            if company is None:
                company, location = comp, loc
            continue
        comp, loc = _split_company_location(c)
        if title is not None and company is None and comp and (placeish.search(loc or "") or len(c) < 60):
            company, location = comp, loc
            continue
        if title is None:
            title = c
    if title is None:
        title = candidates[0]
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
    body = html_to_text_with_links(text) if looks_like_html(text) else text
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
