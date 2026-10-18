"""Outbound digest transports: SMTP (user credentials) or Gmail send (OAuth).

``send`` takes the recipient from the caller, but the only caller is
``notifications.send_digest`` which reads the confirmed address. The transport
itself re-checks the allowlist so no other code path can widen it.
"""

from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage
from email.utils import make_msgid

from jobhunter.config import AppContext
from jobhunter.db import Database
from jobhunter.services import consent
from jobhunter.services.secrets import SecretStore


class TransportError(Exception):
    pass


class TransportNotConfigured(TransportError):
    pass


class TransportUncertain(TransportError):
    pass


def _allowed_recipient(db: Database, to_address: str) -> None:
    allowed = consent.details(db, "digest_email")
    if not allowed.get("confirmed") or allowed.get("address", "").lower() != to_address.lower():
        raise TransportError("Recipient is not the confirmed digest address. Refusing to send.")


def configure_smtp(db: Database, store: SecretStore, host: str, port: int, username: str, password: str, from_address: str, use_tls: str = "starttls") -> dict:
    if use_tls not in {"starttls", "ssl", "none"}:
        raise TransportError("use_tls must be starttls, ssl, or none")
    db.set_setting("smtp", {"host": host, "port": int(port), "username": username, "from": from_address, "tls": use_tls})
    store.set("smtp.password", password)
    db.set_setting("digest.transport", "smtp")
    return {"transport": "smtp", "host": host, "port": port, "from": from_address}


def configure_gmail_send(db: Database, store: SecretStore) -> dict:
    db.set_setting("digest.transport", "gmail")
    return {"transport": "gmail"}


def describe(db: Database, store: SecretStore | None = None) -> dict:
    t = db.get_setting("digest.transport")
    smtp = db.get_setting("smtp") or {}
    if t == "smtp":
        return {"transport": "smtp", "configured": bool(smtp.get("host")), "detail": f"{smtp.get('username')} via {smtp.get('host')}:{smtp.get('port')}"}
    if t == "gmail":
        acct = db.fetchone("SELECT * FROM email_accounts WHERE provider = 'gmail' AND purpose = 'send' AND status = 'connected'")
        return {"transport": "gmail", "configured": acct is not None, "detail": acct["address"] if acct else "Gmail send is not connected"}
    return {"transport": None, "configured": False, "detail": "No email transport configured. Digests can still be previewed in the dashboard."}


def send(ctx: AppContext, db: Database, to_address: str, subject: str, text: str, html: str | None = None) -> dict:
    _allowed_recipient(db, to_address)
    t = db.get_setting("digest.transport")
    if t == "smtp":
        return _send_smtp(ctx, db, to_address, subject, text, html)
    if t == "gmail":
        from jobhunter.services.email import gmail

        return gmail.send_message(ctx, db, to_address, subject, text, html)
    raise TransportNotConfigured("No email transport is configured. Run `jobhunter digest smtp ...` or `jobhunter email connect gmail-send`.")


def _send_smtp(ctx: AppContext, db: Database, to_address: str, subject: str, text: str, html: str | None) -> dict:
    cfg = db.get_setting("smtp") or {}
    if not cfg.get("host"):
        raise TransportNotConfigured("SMTP is not configured.")
    store = SecretStore(ctx.paths)
    password = store.get("smtp.password")
    if password is None:
        raise TransportNotConfigured("SMTP password is missing from the secret store.")
    msg = EmailMessage()
    msg["From"] = cfg["from"]
    msg["To"] = to_address
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid(domain="jobhunteros.local")
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")
    try:
        if cfg.get("tls") == "ssl":
            server = smtplib.SMTP_SSL(cfg["host"], cfg["port"], timeout=30, context=ssl.create_default_context())
        else:
            server = smtplib.SMTP(cfg["host"], cfg["port"], timeout=30)
            if cfg.get("tls") == "starttls":
                server.starttls(context=ssl.create_default_context())
        with server:
            if cfg.get("username"):
                server.login(cfg["username"], password)
            refused = server.send_message(msg)
    except smtplib.SMTPRecipientsRefused as exc:
        raise TransportError(f"The mail server refused the recipient: {exc}") from exc
    except smtplib.SMTPAuthenticationError as exc:
        raise TransportError("SMTP login failed. Check the username and password.") from exc
    except (smtplib.SMTPException, OSError) as exc:
        # We cannot tell whether the message left the server queue.
        raise TransportUncertain(f"SMTP error after connecting: {type(exc).__name__}: {exc}") from exc
    if refused:
        raise TransportError(f"Some recipients were refused: {refused}")
    return {"transport": "smtp", "message_id": msg["Message-ID"]}
