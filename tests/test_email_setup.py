"""Email setup in about a minute: Connect Gmail -> confirm the address -> test email -> restart -> scheduled digest.
Against a fake Google served on localhost (tests/fake_google.py); the dashboard, OAuth code, secret store,
outbox, scheduler and transport all run for real. Also: scope checks, state checks, expiry, disconnect, SMTP stays advanced."""

from __future__ import annotations

import html
import json
import re
from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient

from jobhunter.db import connect
from jobhunter.services import consent, notifications, schedule, views
from jobhunter.services.email import gmail, google_client, transport
from jobhunter.services.locks import FileLock
from jobhunter.services.secrets import SecretStore
from jobhunter.util import now
from jobhunter.web.app import create_app
from tests.conftest import approve_profile
from tests.fake_google import CLIENT_ID

H = {"origin": "http://127.0.0.1:8765"}
CALLBACK = "http://127.0.0.1:8765/connect/gmail/callback"


def _client(ctx):
    return TestClient(create_app(ctx), base_url="http://127.0.0.1:8765")


def _csrf(c, path="/connections"):
    return re.search(r'name="csrf" value="([0-9a-f]+)"', c.get(path).text).group(1)


def _flash(r) -> str:
    return parse_qs(urlparse(r.headers["location"]).query).get("msg", [""])[0]


def start_connect(c, purpose="send"):
    """Press Connect Gmail: a POST with the CSRF token, answered by the small page that continues to Google."""
    r = c.post("/connect/gmail/start", data={"csrf": _csrf(c), "purpose": purpose}, headers=H, follow_redirects=False)
    assert r.status_code == 200, (r.status_code, r.headers.get("location"), r.text[:200])
    m = re.search(r'data-go href="([^"]+)"', r.text)
    assert m, r.text[:500]
    return html.unescape(m.group(1))


def connect_gmail(c, google, purpose="send", state_override=None):
    """Press Connect Gmail, go through the (fake) Google screen, land back in Hunter. Returns the flash message."""
    at_google = httpx.get(start_connect(c, purpose), follow_redirects=False)
    assert at_google.status_code == 302, at_google.text
    back = at_google.headers["location"]
    assert back.startswith(CALLBACK)
    if state_override is not None:
        back = re.sub(r"state=[^&]*", f"state={state_override}", back)
    r2 = c.get(back.replace("http://127.0.0.1:8765", ""), follow_redirects=False)
    assert r2.status_code == 303
    return _flash(r2)


def _token_record(ctx):
    return json.loads(SecretStore(ctx.paths).get("gmail.send.token"))


def _expire_token(ctx):
    store = SecretStore(ctx.paths)
    data = json.loads(store.get("gmail.send.token"))
    data["expiry"] = (now() - timedelta(minutes=1)).isoformat()
    store.set("gmail.send.token", json.dumps(data))


# ---------------------------------------------------------------- the one-minute journey
def test_connect_gmail_to_test_digest_restart_and_scheduled_digest(ctx, db, google, fake_sources):
    approve_profile(db, ctx)
    c = _client(ctx)

    # Before: Connect Gmail is the big button; SMTP is folded away under "advanced", never the default.
    page = c.get("/connections").text
    assert "Gmail not connected" in page and 'class="btn btn-primary btn-lg"' in page and "Connect Gmail</button>" in page
    assert page.index("Connect Gmail") < page.index("SMTP")
    assert re.search(r'<details class="more card" id="advanced">.*SMTP', page, re.S)

    # 1. Connect Gmail: the standard Google screen asks for sending only, with PKCE and a loopback redirect.
    google_url = start_connect(c)
    q = {k: v[0] for k, v in parse_qs(urlparse(google_url).query).items()}
    assert q["client_id"] == CLIENT_ID and q["redirect_uri"] == CALLBACK and q["response_type"] == "code"
    assert q["scope"] == gmail.SCOPE_SEND and q["code_challenge_method"] == "S256" and q["access_type"] == "offline" and q["prompt"] == "consent"
    assert "client_secret" not in q
    at_google = httpx.get(google_url, follow_redirects=False)
    back = at_google.headers["location"].replace("http://127.0.0.1:8765", "")
    # Google sends the browser back cross-site, so the dashboard's SameSite=Strict cookie is not there: a fresh
    # client stands in for that. The one-time state is what proves this callback belongs to the sign-in we started.
    r2 = _client(ctx).get(back, follow_redirects=False)
    assert _flash(r2).startswith("Gmail connected as student@gmail.example.") and "Say yes below" in _flash(r2)
    assert google.token_requests[-1]["pkce_ok"] is True

    # What got stored: one connected send account with exactly the send scope, the token in the secret store only.
    acct = gmail.account(db, "send")
    assert acct and acct["address"] == "student@gmail.example" and json.loads(acct["scopes_json"]) == [gmail.SCOPE_SEND]
    assert consent.details(db, "digest_sender").get("address") == "student@gmail.example"
    assert not consent.details(db, "digest_email").get("confirmed")  # the recipient is a separate decision
    assert db.get_setting("digest.transport") == "gmail" and db.get_setting("digest.suggested_recipient") == "student@gmail.example"
    tok = _token_record(ctx)
    assert tok["refresh_token"].startswith("rt-") and tok["token_uri"] == google.env()["JOBHUNTER_GOOGLE_TOKEN_URL"]
    for table in ("settings", "email_accounts", "consents"):
        assert tok["refresh_token"] not in json.dumps(db.fetchall(f"SELECT * FROM {table}"), default=str)
    assert google.sent == []

    # 2. Hunter detected the address and asks; one click confirms it as the only recipient.
    page = c.get("/connections").text
    assert "Connected: student@gmail.example" in page and "Sending not set up" in page and "Yes, send them there" in page
    r = c.post("/do/digest", data={"csrf": _csrf(c), "action": "use_suggested"}, headers=H, follow_redirects=False)
    assert _flash(r).startswith("Okay, job emails go to student@gmail.example.")
    assert consent.details(db, "digest_email") == {**consent.details(db, "digest_email"), "address": "student@gmail.example", "confirmed": True}
    assert db.get_setting("digest.suggested_recipient") is None
    page = c.get("/connections").text
    assert "Sending enabled" in page and "Send test email" in page

    # 3. Send a test email: goes through the outbox and the real transport to the (fake) Gmail API.
    r = c.post("/do/digest", data={"csrf": _csrf(c), "action": "test"}, headers=H, follow_redirects=False)
    assert _flash(r).startswith("Test email sent to student@gmail.example.")
    assert len(google.sent) == 1
    m = google.sent[0]
    assert m["to"] == "student@gmail.example" and m["from"] == "student@gmail.example" and m["subject"] == "Hunter test: your job emails work"
    assert "no job was applied to" in m["text"]
    rows = db.fetchall("SELECT kind, status, recipient, provider_message_id FROM notifications")
    assert rows == [{"kind": "test", "status": "sent", "recipient": "student@gmail.example", "provider_message_id": "m-" + m["id"].split("-")[1]}]
    assert "Hunter test: your job emails work" in c.get("/connections").text

    # 4. Choose daily + email. Automatic hunting runs inside Hunter straight away.
    r = c.post("/do/schedule", data={"csrf": _csrf(c), "action": "notify", "plan": "daily", "time_of_day": "07:15", "email": "1"}, headers=H, follow_redirects=False)
    assert "every day at 07:15" in _flash(r) and "one email a day at" in _flash(r)
    sched = schedule.get(db)
    assert sched["enabled"] == 1 and sched["frequency"] == "daily" and sched["notify_mode"] == "daily_email" and sched["time_of_day"] == "07:15"
    assert c.app.state.autorun.running
    assert "Automatic hunting: running" in c.get("/connections").text
    c.app.state.autorun.stop()

    # 5. Restart Hunter: still connected, the schedule comes back by itself. A separate worker holding the
    #    lock makes the in-app scheduler step aside, so nothing runs twice.
    other_worker = FileLock(ctx.paths.locks_dir / "worker.lock")
    other_worker.acquire()
    c2 = _client(ctx)
    page = c2.get("/connections").text
    assert "Connected: student@gmail.example" in page and "Sending enabled" in page
    assert c2.app.state.autorun.running
    assert c2.app.state.autorun.tick_once() == {"skipped": "a separate worker is running"}
    c2.app.state.autorun.stop()
    other_worker.release()

    # 6. The scheduled run: a due hunt finds jobs with the synthetic source, then the daily digest goes out once.
    db.execute("UPDATE schedule SET next_run_at = ?, digest_time = '00:00', last_digest_at = NULL WHERE id = 1", ((now() - timedelta(minutes=2)).isoformat(),))
    res = c2.app.state.autorun.tick_once()
    assert res["ran_hunt"] is True and res["hunt"]["status"] == "completed" and res["hunt"]["jobs_new"] == 5, res
    assert res["sent_digest"] is True, res
    assert len(google.sent) == 2
    digest = google.sent[1]
    assert digest["to"] == "student@gmail.example" and "Northwind Cloud" in digest["text"] and "Austin Labs" not in digest["text"]
    assert "%" not in digest["text"]
    kinds = [r["kind"] + ":" + r["status"] for r in db.fetchall("SELECT kind, status FROM notifications ORDER BY id")]
    assert kinds == ["test:sent", "digest:sent"]
    # Same day, same content: nothing is sent again.
    res2 = c2.app.state.autorun.tick_once()
    assert res2["sent_digest"] is False and len(google.sent) == 2
    hb = db.fetchone("SELECT status, message FROM worker_heartbeat WHERE id = 1")
    assert hb["status"] == "idle" and "inside Hunter" in hb["message"]
    # The thread was stopped for these manual ticks, so the page must not claim it is running.
    assert "Automatic hunting: on, but nothing is running it" in c2.get("/connections").text
    c2.app.state.autorun.start()
    assert "Automatic hunting: running" in c2.get("/connections").text
    c2.app.state.autorun.stop()


# ---------------------------------------------------------------- safeguards
def test_send_connection_refuses_read_scope_and_wrong_or_old_state(ctx, db, google):
    c = _client(ctx)
    google.grant_scope = f"{gmail.SCOPE_SEND} {gmail.SCOPE_READ}"
    msg = connect_gmail(c, google)
    assert "must not include read permission" in msg
    assert gmail.account(db, "send") is None and SecretStore(ctx.paths).get("gmail.send.token") is None
    assert db.get_setting("digest.transport") is None
    google.grant_scope = None
    msg = connect_gmail(c, google, state_override="forged")
    assert "not the one Hunter started" in msg and gmail.account(db, "send") is None
    # A sign-in that took longer than the pending window is refused, even with the right state.
    google_url = start_connect(c)
    pending = db.get_setting(gmail.PENDING_KEY)
    pending["created_at"] = (now() - timedelta(seconds=gmail.PENDING_MAX_AGE_S + 5)).isoformat()
    db.set_setting(gmail.PENDING_KEY, pending)
    back = httpx.get(google_url, follow_redirects=False).headers["location"].replace("http://127.0.0.1:8765", "")
    assert "took too long" in _flash(c.get(back, follow_redirects=False))
    assert db.get_setting(gmail.PENDING_KEY) is None
    # Cancelling at Google is a plain message, not a stack trace.
    assert "cancelled or refused" in _flash(c.get("/connect/gmail/callback?error=access_denied", follow_redirects=False))
    # A callback nobody started is refused.
    assert "not the one Hunter started" in _flash(c.get("/connect/gmail/callback?code=x&state=y", follow_redirects=False))
    # The read-only connection is separate and must not carry send permission.
    google.grant_scope = f"{gmail.SCOPE_READ} {gmail.SCOPE_SEND}"
    assert "must not include send permission" in connect_gmail(c, google, purpose="read")
    google.grant_scope = None
    assert connect_gmail(c, google, purpose="read").startswith("Gmail reading connected as student@gmail.example")
    assert json.loads(gmail.account(db, "read")["scopes_json"]) == [gmail.SCOPE_READ]
    assert consent.details(db, "mailbox_read").get("address") == "student@gmail.example"
    assert gmail.account(db, "send") is None  # reading never enables sending


def test_token_refresh_and_expiry_are_handled_and_visible(ctx, db, google):
    approve_profile(db, ctx)
    c = _client(ctx)
    connect_gmail(c, google)
    c.post("/do/digest", data={"csrf": _csrf(c), "action": "use_suggested"}, headers=H)
    # Access token expired locally: Hunter refreshes with Google and sends.
    tok_before = _token_record(ctx)["token"]
    _expire_token(ctx)
    r = c.post("/do/digest", data={"csrf": _csrf(c), "action": "test"}, headers=H, follow_redirects=False)
    assert _flash(r).startswith("Test email sent")
    assert [t["grant_type"] for t in google.token_requests] == ["authorization_code", "refresh_token"]
    assert len(google.sent) == 1 and _token_record(ctx)["token"] != tok_before
    assert gmail.account(db, "send")["last_error"] is None
    # Google refuses the refresh (7-day testing expiry, or access removed): plain words, Needs attention, and a way back.
    google.refresh_ok = False
    _expire_token(ctx)
    r = c.post("/do/digest", data={"csrf": _csrf(c), "action": "test"}, headers=H, follow_redirects=False)
    assert "Press Connect Gmail again" in _flash(r) and len(google.sent) == 1
    acct = gmail.account(db, "send")
    assert acct["last_error"] == gmail.EXPIRED_MESSAGE
    assert db.fetchone("SELECT status FROM notifications ORDER BY id DESC LIMIT 1")["status"] == "failed"
    page = c.get("/connections").text
    assert "Needs attention" in page and "Connect Gmail again" in page
    att = views.attention(db)
    assert [i["kind"] for i in att["items"] if i["kind"] == "gmail_expired"] == ["gmail_expired"]
    assert "Connect Gmail again" in c.get("/attention").text
    # Connect again: new permission, old row closed, the warning is gone, the recipient was never touched.
    google.refresh_ok = True
    assert connect_gmail(c, google).startswith("Gmail connected as student@gmail.example.")
    assert gmail.account(db, "send")["last_error"] is None
    assert db.fetchone("SELECT COUNT(*) AS n FROM email_accounts WHERE purpose = 'send' AND status = 'disconnected'")["n"] == 1
    assert notifications.recipient(db) == "student@gmail.example" and db.get_setting("digest.suggested_recipient") is None
    assert "gmail_expired" not in [i["kind"] for i in views.attention(db)["items"]]
    assert '<span class="chip uncertain">' not in c.get("/connections").text
    r = c.post("/do/digest", data={"csrf": _csrf(c), "action": "test"}, headers=H, follow_redirects=False)
    assert _flash(r).startswith("Test email sent") and len(google.sent) == 2


def test_disconnect_from_dashboard_keeps_history_and_recipient(ctx, db, google):
    approve_profile(db, ctx)
    c = _client(ctx)
    connect_gmail(c, google)
    c.post("/do/digest", data={"csrf": _csrf(c), "action": "use_suggested"}, headers=H)
    c.post("/do/digest", data={"csrf": _csrf(c), "action": "test"}, headers=H)
    refresh_token = _token_record(ctx)["refresh_token"]
    r = c.post("/do/digest", data={"csrf": _csrf(c), "action": "disconnect_send"}, headers=H, follow_redirects=False)
    assert _flash(r).startswith("Token deleted locally and revoked at Google.")
    assert google.revoked == [refresh_token]
    assert gmail.account(db, "send") is None and SecretStore(ctx.paths).get("gmail.send.token") is None
    assert db.get_setting("digest.transport") is None and transport.describe(db)["configured"] is False
    assert consent.details(db, "digest_sender").get("granted") in (False, None) or not consent.details(db, "digest_sender").get("address")
    assert notifications.recipient(db) == "student@gmail.example"  # the address decision stays
    assert db.fetchone("SELECT COUNT(*) AS n FROM notifications")["n"] == 1  # history stays
    page = c.get("/connections").text
    assert "Gmail not connected" in page and "Connect Gmail</button>" in page and "Sending not set up" in page
    assert "Hunter test: your job emails work" in page
    # Nothing can be sent until Gmail is connected again; the outbox records why.
    r = c.post("/do/digest", data={"csrf": _csrf(c), "action": "test"}, headers=H, follow_redirects=False)
    assert "No email transport" in _flash(r) and len(google.sent) == 1


def test_two_test_emails_in_the_same_second_both_send(ctx, db, google):
    approve_profile(db, ctx)
    c = _client(ctx)
    connect_gmail(c, google)
    notifications.confirm_suggested_recipient(db)
    first, second = notifications.send_test(ctx, db), notifications.send_test(ctx, db)
    assert first["status"] == "sent" and second["status"] == "sent" and first["notification_id"] != second["notification_id"]
    assert len(google.sent) == 2


def test_recipient_allowlist_holds_for_gmail_transport(ctx, db, google):
    approve_profile(db, ctx)
    c = _client(ctx)
    connect_gmail(c, google)
    notifications.set_recipient(db, "someone.else@example.com")
    with pytest.raises(transport.TransportError):
        gmail.send_message(ctx, db, "student@gmail.example", "x", "y")
    assert google.sent == []


# ---------------------------------------------------------------- the shared KubeCraft client
def test_client_config_sources_and_unavailable_state(ctx, db, monkeypatch, google):
    assert google_client.resolve_client(ctx.paths)["source"] == "environment"
    monkeypatch.delenv("JOBHUNTER_GOOGLE_CLIENT_ID")
    monkeypatch.delenv("JOBHUNTER_GOOGLE_CLIENT_SECRET")
    # No file, and KubeCraft's copy is not reachable over plain http. The page never waits on the network:
    # the button is there, the click fetches, and a failed fetch is a plain message.
    monkeypatch.setenv("JOBHUNTER_GOOGLE_CLIENT_URL", google.base + "/google-oauth-client.json")
    fetches: list[str] = []
    real_fetch = google_client._fetch
    monkeypatch.setattr(google_client, "_fetch", lambda url: (fetches.append(url), real_fetch(url))[1])
    assert google_client.describe(ctx.paths)["available"] is False and fetches == []
    c = _client(ctx)
    page = c.get("/connections").text
    assert fetches == [] and "Connect Gmail</button>" in page and "fetched from KubeCraft when you press Connect Gmail" in page
    assert "Try again in a minute" in _flash(c.post("/connect/gmail/start", data={"csrf": _csrf(c), "purpose": "send"}, headers=H, follow_redirects=False))
    assert fetches == [google.base + "/google-oauth-client.json"]
    # The JSON Google lets you download, dropped in the private folder, is enough.
    f = google_client.client_file(ctx.paths)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"installed": {"client_id": CLIENT_ID, "client_secret": "from-file", "redirect_uris": ["http://localhost"]}}))
    got = google_client.resolve_client(ctx.paths, fetch=False)
    assert got == {"client_id": CLIENT_ID, "client_secret": "from-file", "source": "file"}
    assert google_client.describe(ctx.paths)["available"] is True and "from file" in c.get("/connections").text
    # Nothing in the repository holds a client secret.
    from pathlib import Path

    repo = Path(__file__).resolve().parents[1]
    for p in list(repo.glob("jobhunter/**/*.py")) + list(repo.glob("docs/*.md")) + [repo / "README.md"]:
        assert "GOCSPX-" not in p.read_text(encoding="utf-8"), p


def test_demo_mode_never_connects(home, google, monkeypatch):
    from jobhunter.config import AppContext

    ctx = AppContext.from_env(demo=True, home=home)
    connect(ctx.paths.db_path).close()
    c = _client(ctx)
    r = c.post("/connect/gmail/start", data={"csrf": _csrf(c), "purpose": "send"}, headers=H, follow_redirects=False)
    assert "Demo mode" in _flash(r) and google.auth_requests == []
    assert "Demo mode" in _flash(c.get("/connect/gmail/callback?code=x&state=y", follow_redirects=False))
