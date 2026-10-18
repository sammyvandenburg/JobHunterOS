"""Second review round: hunt frequency is not email frequency, the automatic-hunting status tells the truth,
a failed scheduled email is recovered without duplicates, and the sign-in's safety checks hold.
Fake Google on localhost (tests/fake_google.py); dashboard, scheduler, outbox and transport run for real."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from jobhunter.models import RawPosting
from jobhunter.services import autorun, hunt, notifications, schedule, views, worker
from jobhunter.services.email import gmail
from jobhunter.services.locks import FileLock
from jobhunter.util import now
from tests.conftest import approve_profile
from tests.test_email_setup import CALLBACK, H, _client, _csrf, _flash, connect_gmail, start_connect

SVC_OFF = {"platform": "Linux", "installed": False, "active": None, "path": None, "supported": True}


def _seed(ctx, db, ext="r1", **kw):
    base = dict(source_id="t", external_id=ext, title="Platform Engineer", employer_name=f"Employer {ext}", url=f"https://job-boards.greenhouse.io/x/jobs/{ext}", location_text="Amsterdam, Netherlands", country_codes=["NL"], description_text="You run Kubernetes clusters on AWS with Terraform, Linux and CI/CD pipelines. On call in a small platform team.", posted_at="2026-10-17T09:00:00+00:00", posted_at_provenance="t", is_employer_hosted=True)
    base.update(kw)
    return hunt.import_postings(ctx, db, [RawPosting(**base)])["job_ids"][0]


def _setup_sending(ctx, db, google, c):
    approve_profile(db, ctx)
    connect_gmail(c, google)
    c.post("/do/digest", data={"csrf": _csrf(c), "action": "use_suggested"}, headers=H)


def _due_now(db, digest_time="00:00"):
    db.execute("UPDATE schedule SET next_run_at = ?, digest_time = ?, last_digest_at = NULL WHERE id = 1", ((now() - timedelta(minutes=2)).isoformat(), digest_time))


# ---------------------------------------------------------------- 1. hunt frequency vs email frequency
def test_looking_often_never_means_emailing_often(ctx, db, google, fake_sources):
    c = _client(ctx)
    _setup_sending(ctx, db, google, c)
    r = c.post("/do/schedule", data={"csrf": _csrf(c), "action": "notify", "plan": "every_2h", "email": "1", "digest_time": "09:15"}, headers=H, follow_redirects=False)
    flash = _flash(r)
    assert flash.startswith("Hunter will look every 2 hours.") and "one email a day at 09:15" in flash and "looking more often never means more emails" in flash
    sched = schedule.get(db)
    assert sched["frequency"] == "every_2h" and sched["notify_mode"] == "daily_email" and sched["digest_time"] == "09:15"
    page = c.get("/connections").text
    assert "Looks: every 2 hours · Emails: once a day at 09:15" in page and "One email a day at most, however often Hunter looks" in page
    c.app.state.autorun.stop()
    # Two due looks on the same day: two hunts, one email.
    _due_now(db)
    first = c.app.state.autorun.tick_once()
    assert first["ran_hunt"] and first["sent_digest"] and len(google.sent) == 1
    db.execute("UPDATE schedule SET next_run_at = ? WHERE id = 1", ((now() - timedelta(minutes=1)).isoformat(),))
    second = c.app.state.autorun.tick_once()
    assert second["ran_hunt"] and not second["sent_digest"] and len(google.sent) == 1
    assert db.fetchone("SELECT COUNT(*) AS n FROM hunt_runs WHERE trigger = 'scheduled'")["n"] == 2
    # Without the email box, the plan is only about looking.
    r = c.post("/do/schedule", data={"csrf": _csrf(c), "action": "notify", "plan": "daily", "time_of_day": "07:00"}, headers=H, follow_redirects=False)
    assert "New jobs show up here; no emails." in _flash(r) and schedule.get(db)["notify_mode"] == "dashboard"
    assert "Emails: none, new jobs show up here" in c.get("/connections").text
    c.app.state.autorun.stop()


# ---------------------------------------------------------------- 2. a status you can trust
def test_automatic_status_comes_from_evidence(ctx, db, monkeypatch):
    svc = dict(SVC_OFF)
    monkeypatch.setattr(worker, "service_status", lambda c, runner=None: dict(svc))
    assert autorun.status(ctx, db)["label"] == "off"
    schedule.set_frequency(db, "every_2h")
    schedule.enable(db)
    st = autorun.status(ctx, db)
    assert st["label"] == "on, but nothing is running it" and st["runner"] is None and st["last_hunt"] is None
    # A separate worker with a fresh heartbeat really is running.
    worker._beat(db, "idle", "checking schedule")
    st = autorun.status(ctx, db)
    assert st["label"] == "running" and st["runner"] == "worker" and st["detail"].startswith("Running in the background")
    # The same heartbeat ten minutes old is not.
    db.execute("UPDATE worker_heartbeat SET last_beat_at = ? WHERE id = 1", ((now() - timedelta(minutes=10)).isoformat(),))
    assert autorun.status(ctx, db)["label"] == "on, but nothing is running it"
    # The login task claims to be active but nobody has heard from it: say so, do not say "running".
    svc.update(installed=True, active=True, path="/x")
    st = autorun.status(ctx, db)
    assert st["label"] == "installed, but not responding" and "10 min ago" in st["detail"]
    svc.update(active=False)
    assert autorun.status(ctx, db)["label"] == "installed, waiting"
    svc.update(installed=False, active=None)
    # The in-app scheduler: alive and healthy, alive but failing, then stopped.
    a = autorun.Autorun(ctx, tick_seconds=3600)
    a.start()
    st = autorun.status(ctx, db, a)
    assert st["label"] == "running" and st["runner"] == "hunter" and "inside Hunter" in st["detail"]
    a.last = {"at": now().isoformat(), "error": "RuntimeError: boom"}
    st = autorun.status(ctx, db, a)
    assert st["label"] == "running, last check failed" and "boom" in st["detail"]
    a.stop()
    hb = db.fetchone("SELECT status, message FROM worker_heartbeat WHERE id = 1")
    assert hb["status"] == "stopped" and "Hunter closed" in hb["message"]
    assert autorun.status(ctx, db, a)["label"] == "on, but nothing is running it"
    # Stopping never overwrites a separate worker's heartbeat.
    worker._beat(db, "idle", "checking schedule")
    lock = FileLock(ctx.paths.locks_dir / "worker.lock")
    lock.acquire()
    try:
        b = autorun.Autorun(ctx, tick_seconds=3600)
        b.start()
        b.stop()
    finally:
        lock.release()
    assert db.fetchone("SELECT message FROM worker_heartbeat WHERE id = 1")["message"] == "checking schedule"
    # Last successful execution is separate from "running": from hunt_runs and notifications, not from a wish.
    with db.tx():
        ok_id = db.insert("hunt_runs", {"trigger": "scheduled", "status": "completed", "started_at": "2026-10-18T06:00:00+00:00", "finished_at": "2026-10-18T06:01:00+00:00", "stats_json": json.dumps({"jobs_new": 3})})
        db.insert("hunt_runs", {"trigger": "scheduled", "status": "failed", "started_at": "2026-10-18T08:00:00+00:00", "finished_at": "2026-10-18T08:00:30+00:00", "error": "DNS failed"})
        db.insert("hunt_runs", {"trigger": "manual", "status": "completed", "started_at": "2026-10-18T09:00:00+00:00", "finished_at": "2026-10-18T09:01:00+00:00", "stats_json": json.dumps({"jobs_new": 9})})
    st = autorun.status(ctx, db)
    assert st["last_hunt"]["status"] == "failed" and st["last_hunt"]["error"] == "DNS failed" and st["last_hunt"]["last_ok_at"] == "2026-10-18T06:01:00+00:00"
    assert ok_id and st["last_email"] is None


def test_connections_page_shows_the_evidence(ctx, db, monkeypatch):
    svc = {**SVC_OFF, "installed": True, "active": True, "path": "/x"}
    monkeypatch.setattr(worker, "service_status", lambda c, runner=None: dict(svc))
    schedule.set_frequency(db, "daily", "07:00")
    schedule.enable(db)
    with db.tx():
        db.insert("hunt_runs", {"trigger": "scheduled", "status": "completed", "started_at": "2026-10-18T06:00:00+00:00", "finished_at": "2026-10-18T06:01:00+00:00", "stats_json": json.dumps({"jobs_new": 3})})
    c = _client(ctx)
    c.app.state.autorun.stop()
    db.execute("UPDATE worker_heartbeat SET last_beat_at = ?, status = 'idle', message = 'checking schedule' WHERE id = 1", ((now() - timedelta(minutes=10)).isoformat(),))
    page = c.get("/connections").text
    assert "Automatic hunting: installed, but not responding" in page and "has not heard from it" in page
    assert "Last automatic look:" in page and "3 new job(s)" in page
    assert '<span class="chip uncertain">' in page  # Needs attention chip
    assert "Automatic hunting is on, but nothing is running it right now" in c.get("/attention").text


# ---------------------------------------------------------------- 3. a failed email is recovered, never duplicated
def test_failed_scheduled_email_is_retried_after_a_wait_and_never_duplicated(ctx, db, google, fake_sources):
    c = _client(ctx)
    _setup_sending(ctx, db, google, c)
    c.post("/do/schedule", data={"csrf": _csrf(c), "action": "notify", "plan": "every_2h", "email": "1", "digest_time": "00:00"}, headers=H)
    c.app.state.autorun.stop()
    google.send_status = 400
    _due_now(db)
    res = c.app.state.autorun.tick_once()
    assert res["ran_hunt"] and res["sent_digest"] is False and res["digest"]["status"] == "failed"
    row = notifications.failed_digest(db)
    assert row and row["attempts"] == 1 and google.sent == []
    # Needs attention says what happened and what comes next; the page says it too.
    att = views.attention(db)
    item = next(i for i in att["items"] if i["kind"] == "digest_failed")
    assert "could not be sent" in item["text"] and "try again by itself in about 30 minutes" in item["text"] and item["will_retry"] is True
    assert "The last job email could not be sent" in c.get("/connections").text
    assert "Send again" in c.get("/attention").text and "Dismiss" in c.get("/attention").text
    # Too soon: no retry, no duplicate.
    assert c.app.state.autorun.tick_once()["sent_digest"] is False and google.sent == []
    # After the wait, with Gmail working again, the same digest goes out once; the same outbox row turns 'sent'.
    db.execute("UPDATE notifications SET claimed_at = ? WHERE id = ?", ((now() - timedelta(minutes=31)).isoformat(), row["id"]))
    google.send_status = 200
    res = c.app.state.autorun.tick_once()
    assert res["sent_digest"] is True and res["digest"]["retry_of"] == row["id"] and len(google.sent) == 1
    after = db.fetchone("SELECT id, status, attempts FROM notifications WHERE id = ?", (row["id"],))
    assert after["status"] == "sent" and after["attempts"] == 2
    assert notifications.failed_digest(db) is None and db.fetchone("SELECT COUNT(*) AS n FROM notifications WHERE kind = 'digest'")["n"] == 1
    # Later ticks the same day: nothing more.
    assert c.app.state.autorun.tick_once()["sent_digest"] is False and len(google.sent) == 1


def test_retry_is_bounded_and_waits_for_a_working_connection(ctx, db, google, fake_sources):
    c = _client(ctx)
    _setup_sending(ctx, db, google, c)
    c.post("/do/schedule", data={"csrf": _csrf(c), "action": "notify", "plan": "every_2h", "email": "1", "digest_time": "00:00"}, headers=H)
    c.app.state.autorun.stop()
    google.send_status = 400
    _due_now(db)
    c.app.state.autorun.tick_once()
    row = notifications.failed_digest(db)
    assert row
    # Out of attempts: Hunter says so and stops.
    db.execute("UPDATE notifications SET attempts = 3, claimed_at = ? WHERE id = ?", ((now() - timedelta(hours=2)).isoformat(), row["id"]))
    plan = notifications.retry_plan(db, notifications.failed_digest(db))
    assert plan["will_retry"] is False and "already tried 3 times" in plan["reason"]
    assert c.app.state.autorun.tick_once()["sent_digest"] is False and google.sent == []
    # Gmail sign-in expired: no blind retries; the message points at Connect Gmail again.
    db.execute("UPDATE notifications SET attempts = 1 WHERE id = ?", (row["id"],))
    db.execute("UPDATE email_accounts SET last_error = ? WHERE purpose = 'send' AND status = 'connected'", (gmail.EXPIRED_MESSAGE,))
    plan = notifications.retry_plan(db, notifications.failed_digest(db))
    assert plan["will_retry"] is False and "connect Gmail again" in plan["reason"]
    assert c.app.state.autorun.tick_once()["sent_digest"] is False and google.sent == []
    db.execute("UPDATE email_accounts SET last_error = NULL WHERE purpose = 'send'")
    # Dismiss: the row is settled and its jobs are back in the next email.
    ids = json.loads(row["job_ids_json"])
    r = c.post("/do/digest", data={"csrf": _csrf(c), "action": "dismiss", "notification_id": row["id"]}, headers=H, follow_redirects=False)
    assert "go into the next email" in _flash(r)
    assert notifications.failed_digest(db) is None and "digest_failed" not in [i["kind"] for i in views.attention(db)["items"]]
    assert set(ids) <= {j["id"] for j in notifications.pending_jobs(db, limit=50)}
    # The same content failing again reuses the same outbox row (no second entry for one digest); a later
    # digest with new jobs supersedes an unresolved failure instead of leaving it hanging.
    google.send_status = 400
    _due_now(db)
    c.app.state.autorun.tick_once()
    failed2 = notifications.failed_digest(db)
    assert failed2 and failed2["id"] == row["id"] and failed2["attempts"] == 2 and failed2["resolved_at"] is None
    google.send_status = 200
    _seed(ctx, db, "fresh-one")
    r = c.post("/do/digest", data={"csrf": _csrf(c), "action": "send"}, headers=H, follow_redirects=False)
    assert "Digest: sent" in _flash(r) and len(google.sent) == 1
    sup = db.fetchone("SELECT status, resolution FROM notifications WHERE id = ?", (failed2["id"],))
    assert sup["resolution"] == "superseded" and notifications.failed_digest(db) is None


# ---------------------------------------------------------------- 5. the sign-in's safety checks
def test_sign_in_start_and_callback_safety(ctx, db, google):
    c = _client(ctx)
    # Starting is a POST with the CSRF token in the body; the token never appears in a URL.
    assert c.get("/connect/gmail/start?purpose=send", follow_redirects=False).status_code == 405
    assert c.post("/connect/gmail/start", data={"csrf": "nope", "purpose": "send"}, headers=H).status_code == 403
    assert c.post("/connect/gmail/start", data={"csrf": _csrf(c), "purpose": "send"}).status_code == 403  # no Origin header
    url = start_connect(c)
    assert "client_secret" not in url and _csrf(c) not in url
    pending = db.get_setting(gmail.PENDING_KEY)
    assert pending["state"] in url and pending["redirect_uri"] == CALLBACK and len(pending["verifier"]) >= 43
    # A second start replaces the first: only the newest sign-in can finish.
    url2 = start_connect(c)
    import httpx

    back1 = httpx.get(url, follow_redirects=False).headers["location"].replace("http://127.0.0.1:8765", "")
    assert "not the one Hunter started" in _flash(_client(ctx).get(back1, follow_redirects=False))
    assert gmail.account(db, "send") is None
    # The callback is accepted without the session cookie (Google's redirect is cross-site), exactly once.
    back2 = httpx.get(url2, follow_redirects=False).headers["location"].replace("http://127.0.0.1:8765", "")
    assert "Gmail connected" in _flash(_client(ctx).get(back2, follow_redirects=False))
    exchanges = len(google.token_requests)
    assert "not the one Hunter started" in _flash(_client(ctx).get(back2, follow_redirects=False))
    assert len(google.token_requests) == exchanges and db.fetchone("SELECT COUNT(*) AS n FROM email_accounts WHERE status = 'connected'")["n"] == 1
    assert google.token_requests[-1]["redirect_uri"] == CALLBACK
    # Cancelling at Google (Google sends the error with our state): nothing changes, the message says so,
    # the pending sign-in is over, and the next try is a clean start.
    start_connect(c)
    state = db.get_setting(gmail.PENDING_KEY)["state"]
    msg = _flash(c.get(f"/connect/gmail/callback?error=access_denied&state={state}", follow_redirects=False))
    assert "cancelled or refused" in msg and "Nothing changed" in msg and db.get_setting(gmail.PENDING_KEY) is None
    # A stray or forged error without our state cannot cancel a sign-in that is under way.
    url3 = start_connect(c)
    msg = _flash(c.get("/connect/gmail/callback?error=%3Cscript%3Ealert(1)%3C/script%3E&state=x", follow_redirects=False))
    assert "<" not in msg and "script" in msg and db.get_setting(gmail.PENDING_KEY) is not None
    back3 = httpx.get(url3, follow_redirects=False).headers["location"].replace("http://127.0.0.1:8765", "")
    assert "Gmail connected" in _flash(_client(ctx).get(back3, follow_redirects=False))
    # Only a loopback redirect to Hunter's own callback is ever used.
    for bad in ("http://evil.example/connect/gmail/callback", "https://127.0.0.1:8765/connect/gmail/callback", "http://127.0.0.1:8765/other", "http://127.0.0.1:8765/connect/gmail/callback?x=1"):
        with pytest.raises(gmail.GmailError):
            gmail.begin_connect(ctx, db, "send", bad)
    assert gmail.begin_connect(ctx, db, "send", "http://localhost:8765/connect/gmail/callback")["url"].startswith(google.base)
    with pytest.raises(gmail.GmailError):
        gmail.begin_connect(ctx, db, "everything", CALLBACK)
