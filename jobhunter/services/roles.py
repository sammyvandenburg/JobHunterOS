"""Role families and company marks for the dashboard: recognition aids, never matching input.

A vacancy title maps to one of twenty role families (DevOps, SRE, Platform, ...) so the page can show a
consistent icon next to it. The mapping reads the title the way `normalize` does (the same word families), but
it changes nothing about relevance or scoring: an icon is decoration with a label, not evidence. Unknown or
ambiguous titles get the neutral "Engineering" family.

A company mark is the employer's own favicon from its verified domain, or the employer's initials. Hunter never
guesses a logo from a search result and never shows one company's mark for another."""

from __future__ import annotations

import re
from urllib.parse import urlparse

from jobhunter.services.sources.registry import ATS_HOSTS

# Order matters: the first family whose pattern matches wins. Specific words before general ones.
FAMILIES: list[tuple[str, str, str]] = [
    ("manager", "Engineering Manager", r"\b(engineering manager|head of|director|vp\b|chief|team manager|people manager)"),
    ("consultant", "Technical Consultant", r"\b(consultant|consulting)\b"),
    ("devsecops", "DevSecOps Engineer", r"\b(devsecops|secops|security (devops|platform|automation))\b"),
    ("security", "Security Engineer", r"\b(security|infosec|appsec|cyber|penetration|pentest)\b"),
    ("sre", "Site Reliability Engineer", r"\b(site reliability|sre|system reliability|reliability engineer|production engineer)\b"),
    ("kubernetes", "Kubernetes Engineer", r"\b(kubernetes|k8s|openshift)\b"),
    ("cloud_architect", "Cloud/Platform Architect", r"\b(cloud|platform|infrastructure|devops) architect\b"),
    ("architect", "Solutions Architect", r"\b(solutions? architect|enterprise architect|technical architect|architect)\b"),
    ("ml", "Machine Learning Engineer", r"\b(machine learning|ml engineer|mlops|ai engineer|deep learning)\b"),
    ("datascience", "Data Scientist", r"\b(data scientist|data science|data analyst|analytics engineer)\b"),
    ("data", "Data Engineer", r"\b(data engineer|dataops|data platform|etl|big data)\b"),
    ("devops", "DevOps Engineer", r"\b(devops|dev ops|cloudops|cloud ops|automation engineer|release engineer|build engineer|deployment engineer|ci/cd)\b"),
    ("platform", "Platform Engineer", r"\bplatform\b"),
    ("infrastructure", "Infrastructure Engineer", r"\b(infrastructure|infra|datacenter|data center|storage engineer|virtuali[sz]ation|hosting|noc|hpc)\b"),
    ("network", "Network Engineer", r"\b(network|networking)\b"),
    ("cloud", "Cloud Engineer", r"\bcloud\b"),
    ("systems", "Systems Engineer", r"\b(systems? engineer|systems? administrator|sysadmin|linux|unix|it operations|operations engineer|technical operations|site operations)\b"),
    ("frontend", "Frontend Engineer", r"\b(frontend|front-end|front end|react|angular|vue|ui engineer|web developer)\b"),
    ("backend", "Backend Engineer", r"\b(backend|back-end|back end|api engineer|server-side)\b"),
    ("software", "Software Engineer", r"\b(software|developer|programmer|full ?stack|java|python|\.net|node)\b"),
]
FAMILY_LABELS = {fid: label for fid, label, _ in FAMILIES}
FAMILY_LABELS["engineering"] = "Engineering"
SHORT_TAGS = {"manager": "Management", "consultant": "Consulting", "devsecops": "DevSecOps", "security": "Security", "sre": "SRE", "kubernetes": "Kubernetes", "cloud_architect": "Architecture", "architect": "Architecture", "ml": "ML", "datascience": "Data science", "data": "Data", "devops": "DevOps", "platform": "Platform", "infrastructure": "Infrastructure", "network": "Network", "cloud": "Cloud", "systems": "Systems", "frontend": "Frontend", "backend": "Backend", "software": "Software", "engineering": "Engineering"}
_COMPILED = [(fid, re.compile(rx, re.I)) for fid, _, rx in FAMILIES]
_PUBLIC_SUFFIX_2 = {"co", "com", "org", "net", "ac", "gov", "edu", "or", "ne"}
_BOARD_HOSTS = ("linkedin.com", "indeed.com", "remotive.com", "arbeitnow.com", "glassdoor.com", "adzuna.com", "monster.com", "stepstone.de", "welcometothejungle.com")


def role_family(title: str | None) -> dict:
    """{"id", "label", "tag"} for a title. Deterministic; the icon never feeds the matching."""
    t = (title or "").strip()
    for fid, rx in _COMPILED:
        if rx.search(t):
            return {"id": fid, "label": FAMILY_LABELS[fid], "tag": SHORT_TAGS[fid]}
    return {"id": "engineering", "label": "Engineering", "tag": "Engineering"}


def _registrable(host: str) -> str:
    parts = host.lower().strip(".").split(".")
    if len(parts) >= 3 and parts[-2] in _PUBLIC_SUFFIX_2 and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def initials(name: str | None) -> str:
    words = [w for w in re.split(r"[^A-Za-z0-9]+", name or "") if w and w.lower() not in {"the", "bv", "b.v", "gmbh", "ltd", "inc", "llc", "ag", "sa", "nv", "plc", "co"}]
    if not words:
        return "?"
    if len(words) == 1:
        return words[0][:2].upper()
    return (words[0][0] + words[1][0]).upper()


def company_mark(employer_name: str | None, direct_url: str | None, direct_url_status: str | None, employer_confidential: bool = False) -> dict:
    """The employer's verified domain (for its own favicon) or nothing; the initials always. A listing on an
    applicant-tracking host (greenhouse, lever, ...) or a job board is not the employer's domain."""
    domain = None
    if direct_url and direct_url_status == "verified" and not employer_confidential:
        try:
            host = (urlparse(direct_url).hostname or "").lower()
        except ValueError:
            host = ""
        if host and host not in ATS_HOSTS and not any(host == b or host.endswith("." + b) for b in _BOARD_HOSTS):
            domain = _registrable(host)
    return {"domain": domain, "initials": "?" if employer_confidential else initials(employer_name)}


def short_location(location_text: str | None, remote_label: str | None, remote_restriction: str | None) -> tuple[str, list[str]]:
    """One short line for a card ("Amsterdam, NL · Hybrid" or "Berlin + 9 locations · Remote") and the full list
    of places for the detail view. Never a 15-location paragraph inline."""
    raw = (location_text or "").strip()
    places = [p.strip() for p in re.split(r"\s*(?:;|\||/| or |\n)\s*", raw) if p.strip()] if raw else []
    if len(places) == 1 and places[0].count(",") >= 3:
        places = [p.strip() for p in places[0].split(",") if p.strip()]
    first = places[0] if places else ""
    if first and first.lower() in {"remote", "remote worldwide", "anywhere"}:
        first = ""
    label = first
    if len(places) > 1:
        label = f"{first or 'Multiple'} + {len(places) - 1} location{'s' if len(places) - 1 != 1 else ''}"
    rule = (remote_restriction or "").strip()
    rule = re.sub(r"\s*\(the posting says:.*?\)", "", rule)
    rule = re.sub(r"\.\s*(Check (before you apply|the posting)\.?)$", ".", rule).rstrip(".")
    mode = rule if rule and len(rule) <= 40 else (remote_label or "")
    parts = [label] if label else []
    if mode and mode.lower() not in label.lower():
        parts.append(mode)
    return " · ".join(parts) or (raw or "Location not stated"), places
