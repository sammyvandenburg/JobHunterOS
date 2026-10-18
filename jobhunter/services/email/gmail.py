"""Gmail integration: read-only sync and separately authorized sending.

Scopes (https://developers.google.com/workspace/gmail/api/auth/scopes):
  read: https://www.googleapis.com/auth/gmail.readonly  (restricted scope)
  send: https://www.googleapis.com/auth/gmail.send      (sensitive scope)

What the token really grants: a readonly token can read the whole mailbox.
Hunter's label/query filter is applied on Hunter's side; it does not narrow
the token. Hunter never archives, deletes, labels, or marks messages read,
and never asks for a mailbox password.

OAuth client: one KubeCraft-managed "Desktop app" OAuth client (see
google_client.py); students never create a Google Cloud project. The dashboard
runs the standard Google sign-in with PKCE and a loopback redirect back to
Hunter. In a project whose consent screen is in Testing status, Google expires
refresh tokens after 7 days and limits test users; a published app with the
gmail.send scope needs Google's brand verification. See docs/email.md.
"""

from __future__ import annotations

import base64
import json
import re
import secrets as _secrets
from datetime import timedelta
from email.message import EmailMessage
from email.utils import make_msgid

from jobhunter.config import AppContext
from jobhunter.db import Database
from jobhunter.services import consent
from jobhunter.services.email import classify, google_client, linking
from jobhunter.services.secrets import SecretStore
from jobhunter.util import now_iso, parse_iso

SCOPE_READ = "https://www.googleapis.com/auth/gmail.readonly"
SCOPE_SEND = "https://www.googleapis.com/auth/gmail.send"
PENDING_KEY = "gmail.oauth_pending"
PENDING_MAX_AGE_S = 600


def _api() -> str:
    return google_client.endpoints()["api"]


def _token_url() -> str:
    return google_client.endpoints()["token"]


def _revoke_url() -> str:
    return google_client.endpoints()["revoke"]


# Backwards-compatible names (tests and older code read them).
API = google_client.DEFAULTS["api"]
TOKEN_URL = google_client.DEFAULTS["token"]
REVOKE_URL = google_client.DEFAULTS["revoke"]
DEFAULT_QUERY = (
    'newer_than:{days}d (subject:(application OR applied OR interview OR "thank you for applying" OR "your application" OR recruiter OR "job alert" OR opportunity) '
    "OR from:(linkedin.com OR indeed.com OR greenhouse.io OR lever.co OR ashbyhq.com OR workable.com OR smartrecruiters.com OR myworkday.com OR icims.com))"
)
DEFAULT_SINCE_DAYS = 30
MAX_MESSAGES_PER_SYNC = 200
MAX_BODY_CHARS = 4000


class GmailError(Exception):
    pass


# ---------------------------------------------------------------------------
# OAuth
# ---------------------------------------------------------------------------
def _scopes_for(purpose: str) -> list[str]:
    return [SCOPE_READ] if purpose == "read" else [SCOPE_SEND]


def _check_granted(purpose: str, granted: set[str]) -> None:
    if purpose == "read" and SCOPE_SEND in granted:
        raise GmailError("The read connection must not include send permission.")
    if purpose == "send" and SCOPE_READ in granted:
        raise GmailError("The send connection must not include read permission.")
    if purpose == "send" and SCOPE_SEND not in granted:
        raise GmailError("Google did not grant permission to send email. Try again and tick the box that allows sending.")
    if purpose == "read" and SCOPE_READ not in granted:
        raise GmailError("Google did not grant read-only access. Try again and tick the box for reading mail.")


def _store_connection(ctx: AppContext, db: Database, purpose: str, address: str, granted: set[str], token_json: str) -> dict:
    """One place that records a connection: token in the secret store, account row, the right consent."""
    store = SecretStore(ctx.paths)
    secret_ref = f"gmail.{purpose}.token"
    store.set(secret_ref, token_json)
    with db.tx():
        db.execute("UPDATE email_accounts SET status = 'disconnected', disconnected_at = ? WHERE provider = 'gmail' AND purpose = ? AND status = 'connected'", (now_iso(), purpose))
        acct_id = db.insert(
            "email_accounts",
            {
                "provider": "gmail",
                "address": address,
                "purpose": purpose,
                "scopes_json": json.dumps(sorted(granted)),
                "secret_ref": secret_ref,
                "status": "connected",
                "query": DEFAULT_QUERY if purpose == "read" else None,
                "since_days": DEFAULT_SINCE_DAYS if purpose == "read" else None,
                "connected_at": now_iso(),
            },
        )
        # Reading the mailbox and sending through an account are separate permissions. Neither one is the
        # recipient decision ("digest_email"), which only `notifications.set_recipient` may change.
        consent.grant(db, "mailbox_read" if purpose == "read" else "digest_sender", {"provider": "gmail", "address": address, "scopes": sorted(granted)})
        if purpose == "send":
            db.set_setting("digest.transport", "gmail")
            from jobhunter.services import notifications

            if not notifications.recipient(db) and address and address != "unknown":
                db.set_setting("digest.suggested_recipient", address)
    return {"account_id": acct_id, "address": address, "scopes": sorted(granted), "note": _scope_note(purpose)}


def begin_connect(ctx: AppContext, db: Database, purpose: str, redirect_uri: str) -> dict:
    """Step 1 of the dashboard flow: the Google sign-in URL for one purpose (send or read), with PKCE and
    a one-time state that the callback must return. Sending and reading are always separate connections."""
    if purpose not in {"send", "read"}:
        raise GmailError("Unknown connection type.")
    client = google_client.resolve_client(ctx.paths)
    verifier, challenge = google_client.pkce_pair()
    state = _secrets.token_urlsafe(24)
    db.set_setting(PENDING_KEY, {"state": state, "verifier": verifier, "purpose": purpose, "redirect_uri": redirect_uri, "created_at": now_iso()})
    from urllib.parse import urlencode

    params = {
        "client_id": client["client_id"],
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(_scopes_for(purpose)),
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "false",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return {"url": f"{google_client.endpoints()['auth']}?{urlencode(params)}", "state": state, "purpose": purpose}


def finish_connect(ctx: AppContext, db: Database, state: str | None, code: str | None, error: str | None = None) -> dict:
    """Step 2: Google sent the person back. Check the state, trade the code for tokens, verify the granted
    scope is exactly the one asked for, find the account's address, store everything."""
    pending = db.get_setting(PENDING_KEY) or {}
    db.delete_setting(PENDING_KEY)
    if error:
        raise GmailError("Google sign-in was cancelled or refused." if error == "access_denied" else f"Google sign-in failed: {error}")
    if not pending or not state or state != pending.get("state"):
        raise GmailError("This sign-in link is not the one Hunter started. Press Connect Gmail again.")
    started = parse_iso(pending.get("created_at"))
    from jobhunter.util import now as _now

    if not started or (_now() - started) > timedelta(seconds=PENDING_MAX_AGE_S):
        raise GmailError("That sign-in took too long. Press Connect Gmail again.")
    if not code:
        raise GmailError("Google did not send a sign-in code. Press Connect Gmail again.")
    purpose = pending["purpose"]
    client = google_client.resolve_client(ctx.paths)
    import httpx

    try:
        r = httpx.post(
            _token_url(),
            data={"code": code, "client_id": client["client_id"], "client_secret": client["client_secret"], "redirect_uri": pending["redirect_uri"], "grant_type": "authorization_code", "code_verifier": pending["verifier"]},
            timeout=30,
        )
    except httpx.HTTPError as exc:
        raise GmailError(f"Could not reach Google to finish the sign-in ({type(exc).__name__}). Check your internet connection and try again.") from exc
    if r.status_code != 200:
        raise GmailError("Google refused the sign-in code. Press Connect Gmail again.")
    tok = r.json()
    if not tok.get("refresh_token"):
        raise GmailError("Google did not give Hunter a long-lived permission. Remove Hunter at https://myaccount.google.com/permissions and connect again.")
    granted = set((tok.get("scope") or " ".join(_scopes_for(purpose))).split())
    _check_granted(purpose, granted)
    token_json = json.dumps(_token_record(tok, client, granted))
    address = _profile_address(_Creds(tok["access_token"])) or "unknown"
    return {**_store_connection(ctx, db, purpose, address, granted, token_json), "purpose": purpose, "suggested_recipient": db.get_setting("digest.suggested_recipient")}


def _token_record(tok: dict, client: dict, granted: set[str]) -> dict:
    from jobhunter.util import now as _now

    expiry = (_now() + timedelta(seconds=int(tok.get("expires_in") or 3600))).isoformat()
    return {"token": tok.get("access_token"), "refresh_token": tok.get("refresh_token"), "token_uri": _token_url(), "client_id": client["client_id"], "client_secret": client["client_secret"], "scopes": sorted(granted), "expiry": expiry}


class _Creds:
    """The one thing the API wrapper needs from a credential: a bearer token."""

    def __init__(self, token: str):
        self.token = token


def connect(ctx: AppContext, db: Database, purpose: str, client_secret_path: str | None = None, open_browser: bool = True) -> dict:
    """Terminal flow (`jobhunter email connect ...`): Google's installed-app sign-in with a local redirect.
    Uses KubeCraft's client unless a client secret file is given."""
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as exc:
        raise GmailError("google-auth-oauthlib is not installed") from exc
    scopes = _scopes_for(purpose)
    if client_secret_path:
        flow = InstalledAppFlow.from_client_secrets_file(client_secret_path, scopes=scopes)
        client = None
    else:
        client = google_client.resolve_client(ctx.paths)
        flow = InstalledAppFlow.from_client_config({"installed": {"client_id": client["client_id"], "client_secret": client["client_secret"], "auth_uri": google_client.endpoints()["auth"], "token_uri": _token_url(), "redirect_uris": ["http://127.0.0.1"]}}, scopes=scopes)
    creds = flow.run_local_server(port=0, open_browser=open_browser, prompt="consent", access_type="offline")
    granted = set(creds.scopes or scopes)
    _check_granted(purpose, granted)
    address = _profile_address(creds) or "unknown"
    return {**_store_connection(ctx, db, purpose, address, granted, creds.to_json()), "purpose": purpose}


def _scope_note(purpose: str) -> str:
    if purpose == "read":
        return "Google granted read-only access to the whole mailbox. Hunter only fetches messages matching your job-search query, but the token itself is not limited to those."
    return "Google granted permission to send email as you. Hunter only uses it for digests to your confirmed address."


def _profile_address(creds) -> str | None:
    try:
        import httpx

        r = httpx.get(f"{_api()}/profile", headers={"Authorization": f"Bearer {creds.token}"}, timeout=20)
        if r.status_code == 200:
            return r.json().get("emailAddress")
    except Exception:
        return None
    return None


EXPIRED_MESSAGE = "Your Gmail connection stopped working (Google sign-in expired). Press Connect Gmail again."


def _credentials(ctx: AppContext, acct: dict, db: Database | None = None) -> _Creds:
    """A valid bearer token for this account, refreshed with Google when needed. A refresh that Google
    refuses (expired or revoked) is recorded on the account so the dashboard can say "connect again"."""
    store = SecretStore(ctx.paths)
    raw = store.get(acct["secret_ref"])
    if not raw:
        raise GmailError("The Gmail token is missing. Connect again.")
    data = json.loads(raw)
    from jobhunter.util import now as _now

    expiry = parse_iso(data.get("expiry"))
    if data.get("token") and expiry and (expiry - _now()) > timedelta(seconds=60):
        return _Creds(data["token"])
    if not data.get("refresh_token"):
        _note_error(db, acct, EXPIRED_MESSAGE)
        raise GmailError(EXPIRED_MESSAGE)
    import httpx

    try:
        r = httpx.post(data.get("token_uri") or _token_url(), data={"client_id": data.get("client_id"), "client_secret": data.get("client_secret"), "refresh_token": data["refresh_token"], "grant_type": "refresh_token"}, timeout=30)
    except httpx.HTTPError as exc:
        raise GmailError(f"Could not reach Google to refresh the sign-in ({type(exc).__name__}). Try again later.") from exc
    if r.status_code != 200:
        _note_error(db, acct, EXPIRED_MESSAGE)
        raise GmailError(EXPIRED_MESSAGE + " (If KubeCraft's Google app is still in testing, sign-ins expire after 7 days.)")
    tok = r.json()
    data["token"] = tok.get("access_token")
    data["expiry"] = (_now() + timedelta(seconds=int(tok.get("expires_in") or 3600))).isoformat()
    store.set(acct["secret_ref"], json.dumps(data))
    _note_error(db, acct, None)
    return _Creds(data["token"])


def _note_error(db: Database | None, acct: dict, message: str | None) -> None:
    if db is None or not acct.get("id"):
        return
    try:
        db.update("email_accounts", {"last_error": message}, "id = ?", (acct["id"],))
    except Exception:
        pass


def disconnect(ctx: AppContext, db: Database, purpose: str) -> dict:
    acct = db.fetchone("SELECT * FROM email_accounts WHERE provider = 'gmail' AND purpose = ? AND status = 'connected'", (purpose,))
    if not acct:
        return {"disconnected": False, "note": "Nothing was connected."}
    store = SecretStore(ctx.paths)
    revoked = False
    raw = store.get(acct["secret_ref"])
    if raw:
        try:
            import httpx

            token = json.loads(raw).get("refresh_token") or json.loads(raw).get("token")
            if token:
                r = httpx.post(_revoke_url(), params={"token": token}, timeout=20)
                revoked = r.status_code == 200
        except Exception:
            revoked = False
    store.delete(acct["secret_ref"])
    with db.tx():
        db.update("email_accounts", {"status": "disconnected", "disconnected_at": now_iso()}, "id = ?", (acct["id"],))
        consent.revoke(db, "mailbox_read" if purpose == "read" else "digest_sender")
        if purpose == "send" and db.get_setting("digest.transport") == "gmail":
            db.delete_setting("digest.transport")
    return {"disconnected": True, "revoked_at_google": revoked, "note": "Token deleted locally" + (" and revoked at Google." if revoked else "; Google revocation could not be confirmed. You can also remove access at https://myaccount.google.com/permissions."), "history_kept": True}


# ---------------------------------------------------------------------------
# Read sync
# ---------------------------------------------------------------------------
def account(db: Database, purpose: str) -> dict | None:
    return db.fetchone("SELECT * FROM email_accounts WHERE provider = 'gmail' AND purpose = ? AND status = 'connected'", (purpose,))


def set_read_filter(db: Database, query: str | None = None, label: str | None = None, since_days: int | None = None) -> dict:
    acct = account(db, "read")
    if not acct:
        raise GmailError("Gmail read is not connected.")
    values = {}
    if query is not None:
        values["query"] = query
    if label is not None:
        values["label"] = label
    if since_days is not None:
        values["since_days"] = max(1, min(int(since_days), 365))
    if values:
        db.update("email_accounts", values, "id = ?", (acct["id"],))
    return account(db, "read")


class _Api:
    def __init__(self, creds):
        import httpx

        self.client = httpx.Client(timeout=30, headers={"Authorization": f"Bearer {creds.token}"})

    def get(self, path: str, params: dict | None = None) -> dict:
        r = self.client.get(f"{_api()}/{path}", params=params)
        if r.status_code == 401:
            raise GmailError("Gmail rejected the token (401). Connect again.")
        if r.status_code == 403:
            raise GmailError("Gmail refused the request (403). The granted scope may not allow it.")
        if r.status_code >= 400:
            raise GmailError(f"Gmail API error {r.status_code}")
        return r.json()

    def post(self, path: str, body: dict) -> dict:
        r = self.client.post(f"{_api()}/{path}", json=body)
        if r.status_code >= 400:
            raise GmailError(f"Gmail API error {r.status_code}: {r.text[:200]}")
        return r.json()


def sync(ctx: AppContext, db: Database, api=None, max_messages: int = MAX_MESSAGES_PER_SYNC) -> dict:
    """Fetch metadata for matching messages, classify deterministically, fetch bodies only when relevant."""
    acct = account(db, "read")
    if not acct:
        raise GmailError("Gmail read is not connected.")
    if not consent.is_granted(db, "mailbox_read"):
        raise GmailError("Mailbox reading is not allowed; consent was revoked.")
    if api is None:
        api = _Api(_credentials(ctx, acct))
    query = (acct.get("query") or DEFAULT_QUERY).format(days=acct.get("since_days") or DEFAULT_SINCE_DAYS)
    params = {"q": query, "maxResults": 100}
    if acct.get("label"):
        params["labelIds"] = acct["label"]
    ids: list[str] = []
    page = None
    while len(ids) < max_messages:
        if page:
            params["pageToken"] = page
        data = api.get("messages", params)
        ids += [m["id"] for m in data.get("messages", [])]
        page = data.get("nextPageToken")
        if not page:
            break
    stats = {"listed": len(ids), "new": 0, "skipped_known": 0, "bodies_fetched": 0, "classified": {}, "needs_confirmation": 0}
    for mid in ids[:max_messages]:
        if db.fetchone("SELECT id FROM email_messages WHERE provider_message_id = ?", (mid,)):
            stats["skipped_known"] += 1
            continue
        meta = api.get(f"messages/{mid}", {"format": "metadata", "metadataHeaders": ["From", "Subject", "Date", "List-Unsubscribe", "Auto-Submitted", "Precedence"]})
        headers = {h["name"].lower(): h["value"] for h in (meta.get("payload") or {}).get("headers", [])}
        snippet = meta.get("snippet") or ""
        result = classify.classify(headers.get("from", ""), headers.get("subject", ""), snippet, headers)
        body_text = None
        if result.needs_body:
            full = api.get(f"messages/{mid}", {"format": "full"})
            body_text = extract_body(full)[:MAX_BODY_CHARS]
            stats["bodies_fetched"] += 1
            result = classify.classify(headers.get("from", ""), headers.get("subject", ""), body_text or snippet, headers)
        received = _internal_date(meta)
        row = {
            "account_id": acct["id"],
            "provider_message_id": mid,
            "thread_id": meta.get("threadId"),
            "received_at": received,
            "from_addr": headers.get("from", "")[:200],
            "subject": headers.get("subject", "")[:300],
            "snippet": snippet[:300],
            "extract_text": body_text,
            "classification": result.kind,
            "classification_confidence": result.confidence,
            "is_automated": 1 if result.automated else 0,
            "origin": "gmail",
            "fetched_at": now_iso(),
        }
        with db.tx():
            em_id = db.insert("email_messages", row)
            linking.link_message(db, em_id)
        stats["new"] += 1
        stats["classified"][result.kind] = stats["classified"].get(result.kind, 0) + 1
    stats["needs_confirmation"] = db.fetchone("SELECT COUNT(*) AS n FROM email_messages WHERE link_status = 'needs_confirmation'")["n"]
    db.update("email_accounts", {"last_sync_at": now_iso(), "last_error": None}, "id = ?", (acct["id"],))
    return stats


def _internal_date(meta: dict) -> str | None:
    from jobhunter.util import epoch_to_iso

    return epoch_to_iso(meta.get("internalDate"))


def extract_body(full: dict) -> str:
    """Text/plain when it carries the job links; otherwise the HTML part with its links kept.
    Tracking pixels are dropped; real links lose only their tracking parameters."""
    from jobhunter.services.sources.import_text import count_job_links, html_to_text_with_links

    parts: list[tuple[str, str]] = []

    def walk(p: dict) -> None:
        mime = p.get("mimeType", "")
        body = p.get("body") or {}
        data = body.get("data")
        if data and mime in {"text/plain", "text/html"}:
            try:
                parts.append((mime, base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")))
            except Exception:
                pass
        for sub in p.get("parts") or []:
            walk(sub)

    walk(full.get("payload") or {})
    plain = next((t for m, t in parts if m == "text/plain"), None)
    html_part = next((t for m, t in parts if m == "text/html"), None)
    candidates = []
    if plain:
        candidates.append(_clean(plain))
    if html_part:
        candidates.append(_clean(html_to_text_with_links(html_part)))
    if not candidates:
        return ""
    # Alerts often put the real vacancy links only in the HTML part.
    return max(candidates, key=lambda t: (count_job_links(t), -candidates.index(t)))


def _clean(text: str) -> str:
    from jobhunter.services.sources.import_text import strip_tracking_links

    text = strip_tracking_links(text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


# ---------------------------------------------------------------------------
# Send (separate authorization)
# ---------------------------------------------------------------------------
def send_message(ctx: AppContext, db: Database, to_address: str, subject: str, text: str, html: str | None = None, api=None) -> dict:
    from jobhunter.services.email.transport import TransportError, TransportNotConfigured, TransportUncertain

    acct = account(db, "send")
    if not acct:
        raise TransportNotConfigured("Gmail send is not connected.")
    allowed = consent.details(db, "digest_email")
    if not allowed.get("confirmed") or allowed.get("address", "").lower() != to_address.lower():
        raise TransportError("Recipient is not the confirmed digest address.")
    if SCOPE_SEND not in json.loads(acct.get("scopes_json") or "[]"):
        raise TransportError("The connected Gmail account has no send permission.")
    msg = EmailMessage()
    msg["To"] = to_address
    msg["From"] = acct.get("address") or "me"
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid(domain="jobhunteros.local")
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    if api is None:
        try:
            api = _Api(_credentials(ctx, acct, db))
        except GmailError as exc:
            raise TransportError(str(exc)) from exc
    try:
        resp = api.post("messages/send", {"raw": raw})
    except GmailError as exc:
        if "error 5" in str(exc) or "timeout" in str(exc).lower():
            raise TransportUncertain(str(exc)) from exc
        if "401" in str(exc):
            _note_error(db, acct, EXPIRED_MESSAGE)
        raise TransportError(str(exc)) from exc
    _note_error(db, acct, None)
    return {"transport": "gmail", "message_id": resp.get("id")}


def describe(db: Database) -> dict:
    r = account(db, "read")
    s = account(db, "send")
    return {
        "read": {"connected": r is not None, "address": r["address"] if r else None, "query": r["query"] if r else None, "label": r["label"] if r else None, "since_days": r["since_days"] if r else None, "last_sync_at": r["last_sync_at"] if r else None, "last_error": r["last_error"] if r else None, "scope": SCOPE_READ, "scope_note": _scope_note("read")},
        "send": {"connected": s is not None, "address": s["address"] if s else None, "last_error": s["last_error"] if s else None, "connected_at": s["connected_at"] if s else None, "scope": SCOPE_SEND, "scope_note": _scope_note("send")},
        "messages_stored": db.fetchone("SELECT COUNT(*) AS n FROM email_messages")["n"],
        "needs_confirmation": db.fetchone("SELECT COUNT(*) AS n FROM email_messages WHERE link_status = 'needs_confirmation'")["n"],
    }


def last_sync_time(db: Database) -> str | None:
    r = account(db, "read")
    return parse_iso(r["last_sync_at"]).isoformat() if r and r.get("last_sync_at") else None
