"""Phase 1 reliability: recipient consent survives sender setup (JH-12), the digest outbox never resends
silently (JH-13), first use loads sources and an empty search is never 'completed' (JH-14),
notes are exported (JH-17)."""

from __future__ import annotations

import json
import sys
import threading
import types
from pathlib import Path

import pytest

from jobhunter.db import connect
from jobhunter.models import FetchResult, RawPosting
from jobhunter.services import actions, consent, datamgmt, hunt, notes, notifications, views
from jobhunter.services.email import gmail, transport
from tests.conftest import approve_profile


def _seed(ctx, db, ext="r1", **kw):
    base = dict(source_id="t", external_id=ext, title="Platform Engineer", employer_name=f"Employer {ext}", url=f"https://job-boards.greenhouse.io/x/jobs/{ext}", location_text="Amsterdam, Netherlands", country_codes=["NL"], description_text="Kubernetes, Terraform, AWS, Linux.", posted_at="2026-10-17T09:00:00+00:00", posted_at_provenance="t", is_employer_hosted=True)
    base.update(kw)
    return hunt.import_postings(ctx, db, [RawPosting(**base)])["job_ids"][0]


class _FakeCreds:
    def __init__(self, scopes):
        self.scopes = scopes
        self.token = "tok"

    def to_json(self):
        return json.dumps({"token": "tok", "refresh_token": "r", "scopes": self.scopes})


def _fake_oauth(monkeypatch, scopes):
    class Flow:
        @classmethod
        def from_client_secrets_file(cls, path, scopes=None):
            return cls()

        def run_local_server(self, **kw):
            return _FakeCreds(scopes)

    mod = types.ModuleType("google_auth_oauthlib.flow")
    mod.InstalledAppFlow = Flow
    monkeypatch.setitem(sys.modules, "google_auth_oauthlib.flow", mod)
    monkeypatch.setattr(gmail, "_profile_address", lambda creds: "sender@example.com")


# ---------------------------------------------------------------- JH-12
def test_connecting_gmail_send_keeps_the_confirmed_recipient(ctx, db, monkeypatch, tmp_path):
    monkeypatch.setenv("JOBHUNTER_SECRETS_BACKEND", "file")
    notifications.set_recipient(db, "me@example.com")
    secret = tmp_path / "client.json"
    secret.write_text("{}")
    _fake_oauth(monkeypatch, [gmail.SCOPE_SEND])
    gmail.connect(ctx, db, "send", str(secret), open_browser=False)
    assert notifications.recipient(db) == "me@example.com"
    assert consent.is_granted(db, "digest_sender") and consent.details(db, "digest_sender")["address"] == "sender@example.com"
    # Other order: connect first, confirm later; then disconnect the sender. The recipient decision stays.
    gmail.disconnect(ctx, db, "send")
    assert notifications.recipient(db) == "me@example.com" and not consent.is_granted(db, "digest_sender")
    _fake_oauth(monkeypatch, [gmail.SCOPE_SEND])
    gmail.connect(ctx, db, "send", str(secret), open_browser=False)
    notifications.set_recipient(db, "other@example.com")
    assert notifications.recipient(db) == "other@example.com"
    # The transport still refuses any address that is not the confirmed one.
    with pytest.raises(transport.TransportError):
        transport._allowed_recipient(db, "sender@example.com")


# ---------------------------------------------------------------- JH-13
class _Box:
    def __init__(self):
        self.sent = []
        self.mode = "ok"
        self.lock = threading.Lock()

    def send(self, ctx, db, to_address, subject, text, html=None):
        with self.lock:
            self.sent.append(to_address)
        if self.mode == "timeout":
            raise transport.TransportUncertain("timed out waiting for the provider")
        if self.mode == "fail":
            raise transport.TransportError("mailbox refused the message")
        if self.mode == "crash":
            raise KeyboardInterrupt
        return {"transport": "fake", "message_id": f"<m{len(self.sent)}@x>"}


@pytest.fixture
def box(monkeypatch):
    b = _Box()
    monkeypatch.setattr(transport, "send", b.send)
    return b


def test_outbox_sends_once_and_never_resends_uncertain_without_force(ctx, db, box):
    approve_profile(db, ctx)
    _seed(ctx, db, "a")
    _seed(ctx, db, "b")
    notifications.set_recipient(db, "me@example.com")
    r1 = notifications.send_digest(ctx, db)
    assert r1["status"] == "sent" and len(box.sent) == 1
    r2 = notifications.send_digest(ctx, db)
    assert r2["status"] == "skipped" and len(box.sent) == 1  # same jobs already sent: nothing new
    # New job, provider times out: one attempt, status uncertain, no automatic retry.
    _seed(ctx, db, "c")
    box.mode = "timeout"
    r3 = notifications.send_digest(ctx, db)
    assert r3["status"] == "uncertain" and len(box.sent) == 2
    nid = r3["notification_id"]
    box.mode = "ok"
    r4 = notifications.send_digest(ctx, db)
    assert r4["status"] in {"blocked", "skipped"} and len(box.sent) == 2, r4
    assert "already have" in (r4.get("note") or "") or r4["status"] == "skipped"
    # It shows under Needs attention with the two honest choices.
    att = views.attention(db)
    item = next(i for i in att["items"] if i["kind"] == "digest_uncertain")
    assert item["notification_id"] == nid
    # Manual retry without force is refused with the warning; with force it sends again.
    r5 = notifications.send_digest(ctx, db, notification_id=nid)
    assert r5["status"] == "blocked" and len(box.sent) == 2
    r6 = notifications.send_digest(ctx, db, notification_id=nid, force=True)
    assert r6["status"] == "sent" and len(box.sent) == 3
    assert views.attention(db)["count"] == 0 or all(i["kind"] != "digest_uncertain" for i in views.attention(db)["items"])
    # Resolving "arrived" instead of retrying also clears it and keeps the jobs out of future digests.
    _seed(ctx, db, "d")
    box.mode = "timeout"
    r7 = notifications.send_digest(ctx, db)
    box.mode = "ok"
    notifications.resolve(db, r7["notification_id"], "arrived")
    assert all(i["kind"] != "digest_uncertain" for i in views.attention(db)["items"])
    assert notifications.send_digest(ctx, db)["status"] == "skipped" and len(box.sent) == 4
    # "Not arrived" frees the jobs for a normal send.
    _seed(ctx, db, "e")
    box.mode = "timeout"
    r8 = notifications.send_digest(ctx, db)
    box.mode = "ok"
    notifications.resolve(db, r8["notification_id"], "not_arrived")
    assert notifications.send_digest(ctx, db)["status"] == "sent"


def test_outbox_failed_sends_can_retry_and_history_is_honest(ctx, db, box):
    approve_profile(db, ctx)
    _seed(ctx, db, "a")
    notifications.set_recipient(db, "me@example.com")
    box.mode = "fail"
    r = notifications.send_digest(ctx, db)
    assert r["status"] == "failed"
    box.mode = "ok"
    r2 = notifications.send_digest(ctx, db)  # a plain failure may be retried automatically
    assert r2["status"] == "sent" and r2["notification_id"] == r["notification_id"]
    row = db.fetchone("SELECT * FROM notifications WHERE id = ?", (r["notification_id"],))
    assert row["attempts"] == 2 and row["status"] == "sent" and row["delivery_key"]


def test_concurrent_digest_sends_claim_once(ctx, db, box):
    approve_profile(db, ctx)
    _seed(ctx, db, "a")
    notifications.set_recipient(db, "me@example.com")
    db.close()
    results = []

    def go():
        d = connect(ctx.paths.db_path)
        try:
            results.append(notifications.send_digest(ctx, d)["status"])
        finally:
            d.close()

    threads = [threading.Thread(target=go) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(box.sent) == 1 and results.count("sent") == 1, results


def test_crash_during_send_leaves_an_uncertain_row_not_a_resend(ctx, db, box):
    approve_profile(db, ctx)
    _seed(ctx, db, "a")
    notifications.set_recipient(db, "me@example.com")
    box.mode = "crash"
    with pytest.raises(KeyboardInterrupt):
        notifications.send_digest(ctx, db)
    box.mode = "ok"
    assert db.fetchone("SELECT status FROM notifications")["status"] == "uncertain"
    assert notifications.send_digest(ctx, db)["status"] in {"blocked", "skipped"} and len(box.sent) == 1


# ---------------------------------------------------------------- JH-14
def test_first_hunt_loads_sources_and_empty_source_sets_are_never_a_success(ctx, db, monkeypatch):
    approve_profile(db, ctx)
    assert db.fetchone("SELECT COUNT(*) AS n FROM watch_sources")["n"] == 0
    seen = {}

    def fake_fetch(client, s):
        seen[s["id"]] = True
        return FetchResult(source_id=s["id"], status="ok", postings=[], complete=True)

    monkeypatch.setattr(hunt, "_fetch_one", fake_fetch)
    monkeypatch.setattr(hunt, "_promote_discovered_employers", lambda *a, **k: 0)
    monkeypatch.setattr(hunt, "_lookup_originals", lambda *a, **k: None)
    st = hunt.run_hunt(ctx, db)
    assert st["status"] == "completed" and st["sources_total"] > 10 and seen
    assert db.get_setting("catalog.synced_at")
    # The person turns every source off: the next hunt says so instead of pretending it searched.
    db.execute("UPDATE watch_sources SET enabled = 0")
    st = hunt.run_hunt(ctx, db)
    assert st["status"] == "no_sources" and st["problem"] == "all_disabled" and "switched off" in st["note"]
    assert hunt.last_successful_run(db)["status"] == "completed"  # the empty run is not counted as a success
    assert db.fetchone("SELECT status FROM hunt_runs ORDER BY id DESC LIMIT 1")["status"] == "no_sources"
    # Sources enabled again stay as the person left them: the catalogue sync never re-enables anything.
    db.execute("UPDATE watch_sources SET enabled = 1 WHERE id = (SELECT id FROM watch_sources LIMIT 1)")
    hunt.ensure_sources_initialized(db)
    assert db.fetchone("SELECT COUNT(*) AS n FROM watch_sources WHERE enabled = 1")["n"] == 1


# ---------------------------------------------------------------- JH-17
def test_export_includes_notes(ctx, db):
    approve_profile(db, ctx)
    jid = _seed(ctx, db)
    n = notes.add(db, jid, "Ask about the on-call rota.")
    notes.add(db, jid, "Second note")
    notes.delete(db, n["id"])
    actions.favorite_job(db, jid)
    res = datamgmt.export_all(ctx, db)
    exported = json.loads((Path(res["path"]) / "job_notes.json").read_text())
    assert {e["body"] for e in exported} == {"Ask about the on-call rota.", "Second note"}
    assert any(e["deleted_at"] for e in exported) and res["tables"]["job_notes"] == 2
