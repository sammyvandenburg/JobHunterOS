"""Gmail integration: read-only sync and separately authorized sending.

Scopes (https://developers.google.com/workspace/gmail/api/auth/scopes):
  read: https://www.googleapis.com/auth/gmail.readonly  (restricted scope)
  send: https://www.googleapis.com/auth/gmail.send      (sensitive scope)

What the token really grants: a readonly token can read the whole mailbox.
Hunter's label/query filter is applied on Hunter's side; it does not narrow
the token. Hunter never archives, deletes, labels, or marks messages read,
and never asks for a mailbox password.

OAuth client: the user supplies a Google Cloud "Desktop app" OAuth client
secret file. In a project whose consent screen is in Testing status, Google
expires refresh tokens after 7 days and limits test users; restricted scopes
need verification (and a security assessment) for a published app. See
docs/email.md for what that means for KubeCraft.
"""

from __future__ import annotations

import base64
import json
import re
from email.message import EmailMessage
from email.utils import make_msgid

from jobhunter.config import AppContext
from jobhunter.db import Database
from jobhunter.services import consent
from jobhunter.services.email import classify, linking
from jobhunter.services.secrets import SecretStore
from jobhunter.util import now_iso, parse_iso

SCOPE_READ = "https://www.googleapis.com/auth/gmail.readonly"
SCOPE_SEND = "https://www.googleapis.com/auth/gmail.send"
API = "https://gmail.googleapis.com/gmail/v1/users/me"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
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
def connect(ctx: AppContext, db: Database, purpose: str, client_secret_path: str, open_browser: bool = True) -> dict:
    """Run the installed-app OAuth flow for one purpose (read or send)."""
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as exc:
        raise GmailError("google-auth-oauthlib is not installed") from exc
    scopes = [SCOPE_READ] if purpose == "read" else [SCOPE_SEND]
    flow = InstalledAppFlow.from_client_secrets_file(client_secret_path, scopes=scopes)
    creds = flow.run_local_server(port=0, open_browser=open_browser, prompt="consent", access_type="offline")
    granted = set(creds.scopes or scopes)
    if purpose == "read" and SCOPE_SEND in granted:
        raise GmailError("The read connection must not include send permission.")
    if purpose == "send" and SCOPE_READ in granted:
        raise GmailError("The send connection must not include read permission.")
    store = SecretStore(ctx.paths)
    secret_ref = f"gmail.{purpose}.token"
    store.set(secret_ref, creds.to_json())
    address = _profile_address(creds) or "unknown"
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
    return {"account_id": acct_id, "address": address, "scopes": sorted(granted), "note": _scope_note(purpose)}


def _scope_note(purpose: str) -> str:
    if purpose == "read":
        return "Google granted read-only access to the whole mailbox. Hunter only fetches messages matching your job-search query, but the token itself is not limited to those."
    return "Google granted permission to send email as you. Hunter only uses it for digests to your confirmed address."


def _profile_address(creds) -> str | None:
    try:
        import httpx

        r = httpx.get(f"{API}/profile", headers={"Authorization": f"Bearer {creds.token}"}, timeout=20)
        if r.status_code == 200:
            return r.json().get("emailAddress")
    except Exception:
        return None
    return None


def _credentials(ctx: AppContext, acct: dict):
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
    except ImportError as exc:
        raise GmailError("google-auth is not installed") from exc
    store = SecretStore(ctx.paths)
    raw = store.get(acct["secret_ref"])
    if not raw:
        raise GmailError("The Gmail token is missing. Connect again.")
    creds = Credentials.from_authorized_user_info(json.loads(raw))
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception as exc:
                raise GmailError(f"Could not refresh the Gmail token ({type(exc).__name__}). If your Google project is in Testing mode, tokens expire after 7 days; connect again.") from exc
            store.set(acct["secret_ref"], creds.to_json())
        else:
            raise GmailError("The Gmail token is no longer valid. Connect again.")
    return creds


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
                r = httpx.post(REVOKE_URL, params={"token": token}, timeout=20)
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
        r = self.client.get(f"{API}/{path}", params=params)
        if r.status_code == 401:
            raise GmailError("Gmail rejected the token (401). Connect again.")
        if r.status_code == 403:
            raise GmailError("Gmail refused the request (403). The granted scope may not allow it.")
        if r.status_code >= 400:
            raise GmailError(f"Gmail API error {r.status_code}")
        return r.json()

    def post(self, path: str, body: dict) -> dict:
        r = self.client.post(f"{API}/{path}", json=body)
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
            api = _Api(_credentials(ctx, acct))
        except GmailError as exc:
            raise TransportError(str(exc)) from exc
    try:
        resp = api.post("messages/send", {"raw": raw})
    except GmailError as exc:
        if "error 5" in str(exc) or "timeout" in str(exc).lower():
            raise TransportUncertain(str(exc)) from exc
        raise TransportError(str(exc)) from exc
    return {"transport": "gmail", "message_id": resp.get("id")}


def describe(db: Database) -> dict:
    r = account(db, "read")
    s = account(db, "send")
    return {
        "read": {"connected": r is not None, "address": r["address"] if r else None, "query": r["query"] if r else None, "label": r["label"] if r else None, "since_days": r["since_days"] if r else None, "last_sync_at": r["last_sync_at"] if r else None, "last_error": r["last_error"] if r else None, "scope": SCOPE_READ, "scope_note": _scope_note("read")},
        "send": {"connected": s is not None, "address": s["address"] if s else None, "scope": SCOPE_SEND, "scope_note": _scope_note("send")},
        "messages_stored": db.fetchone("SELECT COUNT(*) AS n FROM email_messages")["n"],
        "needs_confirmation": db.fetchone("SELECT COUNT(*) AS n FROM email_messages WHERE link_status = 'needs_confirmation'")["n"],
    }


def last_sync_time(db: Database) -> str | None:
    r = account(db, "read")
    return parse_iso(r["last_sync_at"]).isoformat() if r and r.get("last_sync_at") else None
