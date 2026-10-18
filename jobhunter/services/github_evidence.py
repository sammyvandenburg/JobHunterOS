"""Read-only GitHub evidence for projects the user names explicitly.

Only public repository metadata and the README are read, through the public
REST API without credentials. Nothing is cloned or executed. A fork or a
tutorial repository is flagged so ownership is asked, not assumed.
"""

from __future__ import annotations

import re

from jobhunter.services.http import FetchError, SafeClient, validate_url

API = "https://api.github.com"
REPO_RE = re.compile(r"^(?:https?://github\.com/)?([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$")
MAX_README_BYTES = 20_000
MAX_REPOS = 10


def parse_repo(ref: str) -> tuple[str, str] | None:
    m = REPO_RE.match(ref.strip())
    if not m:
        return None
    return m.group(1), m.group(2)


def inspect_repos(refs: list[str], client: SafeClient | None = None) -> dict:
    """Return structured, minimal evidence for up to MAX_REPOS repositories."""
    own = client is None
    client = client or SafeClient()
    results: list[dict] = []
    errors: list[str] = []
    try:
        for ref in refs[:MAX_REPOS]:
            parsed = parse_repo(ref)
            if not parsed:
                errors.append(f"Not a GitHub repository reference: {ref}")
                continue
            owner, name = parsed
            try:
                meta = client.get(validate_url(f"{API}/repos/{owner}/{name}"), headers={"Accept": "application/vnd.github+json"}).json()
            except FetchError as exc:
                errors.append(f"Could not read {owner}/{name}: {exc}")
                continue
            readme = ""
            try:
                r = client.get(
                    validate_url(f"{API}/repos/{owner}/{name}/readme"),
                    headers={"Accept": "application/vnd.github.raw+json"},
                )
                readme = r.text[:MAX_README_BYTES]
            except FetchError:
                readme = ""
            langs = {}
            try:
                langs = client.get(validate_url(f"{API}/repos/{owner}/{name}/languages")).json()
            except FetchError:
                pass
            results.append(
                {
                    "full_name": meta.get("full_name", f"{owner}/{name}"),
                    "html_url": meta.get("html_url"),
                    "description": meta.get("description"),
                    "fork": bool(meta.get("fork")),
                    "archived": bool(meta.get("archived")),
                    "stargazers": meta.get("stargazers_count"),
                    "pushed_at": meta.get("pushed_at"),
                    "created_at": meta.get("created_at"),
                    "languages": list(langs.keys())[:8],
                    "topics": (meta.get("topics") or [])[:10],
                    "readme_excerpt": readme[:4000],
                    "ownership": "unknown",  # the user confirms: own | team | fork_tutorial | contribution
                    "flags": _flags(meta, readme),
                }
            )
    finally:
        if own:
            client.close()
    return {"repositories": results, "errors": errors}


def _flags(meta: dict, readme: str) -> list[str]:
    flags = []
    if meta.get("fork"):
        flags.append("fork: this repository is a fork; authorship must be confirmed")
    low = (readme or "").lower()
    if any(w in low for w in ("tutorial", "course", "bootcamp", "follow along", "exercise", "udemy", "kubecraft")):
        flags.append("guided: README mentions a course or tutorial; this is a guided project unless you say otherwise")
    if (meta.get("stargazers_count") or 0) > 500 and not meta.get("fork"):
        flags.append("popular: large public project; your contribution scope must be stated by you")
    return flags
