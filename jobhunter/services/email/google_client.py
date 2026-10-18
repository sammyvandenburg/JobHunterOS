"""One KubeCraft-managed Google OAuth client for every student.

Students never create a Google Cloud project. Hunter needs the client id and the
"desktop app" client secret of KubeCraft's OAuth client. Google documents that a
desktop-app client secret is not a confidential value (an installed app cannot keep
secrets; PKCE and the per-user consent screen protect the flow), but it is still
never committed to this repository. Hunter finds it, in this order:

1. environment: JOBHUNTER_GOOGLE_CLIENT_ID and JOBHUNTER_GOOGLE_CLIENT_SECRET
2. a file in the private data folder: ~/.jobhunteros/google_client.json
   (either {"client_id": ..., "client_secret": ...} or the JSON Google lets you download)
3. KubeCraft's published copy at CLIENT_CONFIG_URL (HTTPS), cached into that file

Google endpoints can be pointed elsewhere with environment variables; tests use that
to run a fake Google on localhost. Nothing here sends mail or stores tokens.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
from pathlib import Path

from jobhunter.config import Paths, write_private_file

CLIENT_CONFIG_URL = "https://jobhunter.kubecraft.nl/google-oauth-client.json"
ENV_ID = "JOBHUNTER_GOOGLE_CLIENT_ID"
ENV_SECRET = "JOBHUNTER_GOOGLE_CLIENT_SECRET"
ENV_URL = "JOBHUNTER_GOOGLE_CLIENT_URL"
DEFAULTS = {
    "auth": "https://accounts.google.com/o/oauth2/v2/auth",
    "token": "https://oauth2.googleapis.com/token",
    "revoke": "https://oauth2.googleapis.com/revoke",
    "api": "https://gmail.googleapis.com/gmail/v1/users/me",
}


class ClientConfigError(Exception):
    pass


def endpoints() -> dict:
    """Google URLs, overridable for tests (JOBHUNTER_GOOGLE_AUTH_URL, _TOKEN_URL, _REVOKE_URL, JOBHUNTER_GMAIL_API)."""
    return {
        "auth": os.environ.get("JOBHUNTER_GOOGLE_AUTH_URL", DEFAULTS["auth"]),
        "token": os.environ.get("JOBHUNTER_GOOGLE_TOKEN_URL", DEFAULTS["token"]),
        "revoke": os.environ.get("JOBHUNTER_GOOGLE_REVOKE_URL", DEFAULTS["revoke"]),
        "api": os.environ.get("JOBHUNTER_GMAIL_API", DEFAULTS["api"]),
    }


def client_file(paths: Paths) -> Path:
    return paths.home / "google_client.json"


def _parse(data: dict) -> dict | None:
    inner = data.get("installed") or data.get("web") or data
    cid, secret = inner.get("client_id"), inner.get("client_secret")
    if cid and secret:
        return {"client_id": str(cid).strip(), "client_secret": str(secret).strip()}
    return None


def resolve_client(paths: Paths, fetch: bool = True) -> dict:
    """The OAuth client to use, with where it came from. Raises ClientConfigError when none is available."""
    cid, secret = os.environ.get(ENV_ID), os.environ.get(ENV_SECRET)
    if cid and secret:
        return {"client_id": cid.strip(), "client_secret": secret.strip(), "source": "environment"}
    f = client_file(paths)
    if f.exists():
        try:
            parsed = _parse(json.loads(f.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            parsed = None
        if parsed:
            return {**parsed, "source": "file"}
    if fetch:
        url = os.environ.get(ENV_URL, CLIENT_CONFIG_URL)
        fetched = _fetch(url)
        if fetched:
            write_private_file(f, json.dumps({**fetched, "source_url": url}, indent=1).encode("utf-8"))
            return {**fetched, "source": "kubecraft"}
    raise ClientConfigError(
        "Google sign-in is not available yet: Hunter could not get KubeCraft's Google sign-in details. "
        "Try again in a minute. If it keeps failing, KubeCraft has not published them yet (see docs/email.md)."
    )


def _fetch(url: str) -> dict | None:
    if not url.lower().startswith("https://"):
        return None
    try:
        import httpx

        r = httpx.get(url, timeout=10, follow_redirects=False, headers={"Accept": "application/json"})
        if r.status_code != 200:
            return None
        return _parse(r.json())
    except Exception:
        return None


def describe(paths: Paths) -> dict:
    """For the Connections page: is sign-in possible, and where did the client come from? Never raises."""
    try:
        c = resolve_client(paths, fetch=False)
    except ClientConfigError:
        try:
            c = resolve_client(paths, fetch=True)
        except ClientConfigError as exc:
            return {"available": False, "source": None, "client_id_hint": None, "problem": str(exc), "config_url": os.environ.get(ENV_URL, CLIENT_CONFIG_URL)}
    cid = c["client_id"]
    return {"available": True, "source": c["source"], "client_id_hint": ("…" + cid[-30:]) if len(cid) > 30 else cid, "problem": None, "config_url": os.environ.get(ENV_URL, CLIENT_CONFIG_URL)}


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).decode("ascii").rstrip("=")
    return verifier, challenge
