"""A Claude-operated cloud run, proven without Claude: the state file carries exactly the right things between
the laptop and a fresh cloud data folder, two cloud runs never repeat a digest, and a dashboard action survives
the round trip. The digest handoff claims the outbox row before Claude sends and records what really happened."""

from __future__ import annotations

import json

import pytest

from jobhunter.config import AppContext
from jobhunter.db import connect
from jobhunter.models import RawPosting
from jobhunter.services import (
    actions,
    cloudstate,
    consent,
    hunt,
    notes,
    notifications,
    schedule,
    views,
    worker,
)
from jobhunter.services import preferences as prefs_svc
from jobhunter.services import profile as profile_svc
from jobhunter.services.email import transport
from tests.conftest import approve_profile


def _seed(ctx, db, ext, **kw):
    base = dict(source_id="t", external_id=ext, title="Platform Engineer", employer_name=f"Employer {ext}", url=f"https://job-boards.greenhouse.io/x/jobs/{ext}", location_text="Amsterdam, Netherlands", country_codes=["NL"], description_text="You run Kubernetes clusters on AWS with Terraform, Linux and CI/CD pipelines. On call in a small platform team.", posted_at="2026-10-17T09:00:00+00:00", posted_at_provenance="t", is_employer_hosted=True)
    base.update(kw)
    return hunt.import_postings(ctx, db, [RawPosting(**base)])["job_ids"][0]


def _fresh(tmp_path, name):
    home = tmp_path / name
    ctx = AppContext.from_env(demo=False, home=home)
    return ctx, connect(ctx.paths.db_path)


def _cloud_run(ctx, db, bundle, fake_sources_ready=True):
    """What the routine does, minus Claude: restore, hunt, prepare the digest, 'send' it, record, export."""
    cloudstate.import_bundle(ctx, db, bundle, inputs=True, results=True)
    tag = cloudstate.new_run_tag()
    hunt.run_hunt(ctx, db, trigger="cloud")
    cloudstate.tag_latest_run(db, tag)
    handoff = notifications.prepare_handoff(ctx, db)
    if handoff["status"] == "ready":
        notifications.record_handoff(db, handoff["notification_id"], "sent", message_id="gmail-msg-" + tag[-4:])
    return handoff, cloudstate.export_bundle(ctx, db)


def test_two_cloud_runs_and_a_dashboard_action_survive_the_round_trip(ctx, db, tmp_path, fake_sources):
    # The laptop: approved profile, a confirmed address, some history the cloud must respect but never receive.
    approve_profile(db, ctx)
    notifications.set_recipient(db, "student@example.com")
    dismissed = _seed(ctx, db, "nl1", title="Platform Engineer", employer_name="Northwind Cloud", url="https://job-boards.greenhouse.io/northwind/jobs/1")
    applied = _seed(ctx, db, "nl2", title="DevOps Engineer", employer_name="Contoso Payments", url="https://job-boards.greenhouse.io/contoso/jobs/2")
    kept = _seed(ctx, db, "local-only", employer_name="Local Only BV", url="https://job-boards.greenhouse.io/local/jobs/9")
    actions.dismiss_job(db, dismissed, reason_code="not_for_me")
    actions.mark_applied(db, applied, applied_at="2026-10-16")
    actions.favorite_job(db, kept)
    notes.add(db, kept, "Ask about on-call rota.")
    bundle = cloudstate.export_bundle(ctx, db)
    text = json.dumps(bundle)
    # What leaves the laptop: profile facts, preferences, consents, exclusion keys, public job data. Not the CV, notes, favorites or secrets.
    assert bundle["inputs"]["consents"]["digest_email"]["address"] == "student@example.com"
    assert set(bundle["inputs"]["exclusions"]["dismissed"]) == {db.fetchone("SELECT canonical_key AS k FROM jobs WHERE id = ?", (dismissed,))["k"]}
    assert set(bundle["inputs"]["exclusions"]["applied"]) == {db.fetchone("SELECT canonical_key AS k FROM jobs WHERE id = ?", (applied,))["k"]}
    assert "Ask about on-call" not in text and "Rotterdam, Netherlands ·" not in text and "sam@example.invalid" not in text and "favorited" not in text and "applied_at" not in text
    assert cloudstate.check_bundle_privacy(bundle) == []
    assert "source_documents" not in bundle and "notes" not in bundle["results"]

    # Run one: a fresh cloud folder restores, hunts (synthetic source), sends one digest, exports.
    c1, d1 = _fresh(tmp_path, "cloud1")
    handoff1, out1 = _cloud_run(c1, d1, bundle)
    assert profile_svc.approved(d1)["version"] == profile_svc.approved(db)["version"]
    assert prefs_svc.get(d1).work_countries == prefs_svc.get(db).work_countries
    assert notifications.recipient(d1) == "student@example.com" and d1.get_setting("digest.transport") == "claude"
    assert consent.is_granted(d1, "readiness") and consent.is_granted(d1, "profile_approved")
    assert handoff1["status"] == "ready" and handoff1["to"] == "student@example.com" and handoff1["jobs"]
    titles = {views.job_detail(d1, j)["employer"] for j in handoff1["jobs"]}
    assert "Northwind Cloud" not in titles and "Contoso Payments" not in titles, "dismissed and applied jobs never come back in the cloud digest"
    assert "Berlin Robotics" in titles  # a job the laptop had never seen
    sent1 = d1.fetchone("SELECT status, provider_message_id, recipient FROM notifications WHERE id = ?", (handoff1["notification_id"],))
    assert sent1["status"] == "sent" and sent1["provider_message_id"].startswith("gmail-msg-") and sent1["recipient"] == "student@example.com"
    assert d1.fetchone("SELECT COUNT(*) AS n FROM hunt_runs WHERE trigger = 'cloud' AND owner LIKE 'cloud:%'")["n"] == 1
    # Python would refuse to send in this mode even if asked.
    with pytest.raises(transport.TransportNotConfigured):
        transport.send(c1, d1, "student@example.com", "x", "y")
    d1.close()

    # Run two: another fresh folder restores run one's state, hunts the same sources, and sends nothing again.
    c2, d2 = _fresh(tmp_path, "cloud2")
    handoff2, out2 = _cloud_run(c2, d2, out1)
    assert handoff2["status"] == "skipped" and ("Already emailed today" in handoff2["note"] or "Nothing new" in handoff2["note"])
    assert d2.fetchone("SELECT COUNT(*) AS n FROM notifications WHERE status = 'sent'")["n"] == 1  # the history came along
    assert d2.fetchone("SELECT COUNT(*) AS n FROM hunt_runs WHERE trigger = 'cloud'")["n"] == 2
    d2.close()

    # Back on the laptop: results merge in; notes, favorites and applications are untouched.
    before_notes = notes.list_for_job(db, kept)
    res = cloudstate.import_bundle(ctx, db, out2, inputs=False, results=True)
    assert res["results"]["jobs_added"] >= 1 and res["results"]["notifications_added"] == 1
    assert views.job_detail(db, kept)["favorited"] is True and notes.list_for_job(db, kept) == before_notes
    assert views.job_detail(db, applied)["stage"] == "applied" and views.job_detail(db, dismissed)["dismissed"] is True
    local_note = db.fetchone("SELECT status, job_ids_json FROM notifications WHERE status = 'sent'")
    digested_ids = json.loads(local_note["job_ids_json"])
    assert digested_ids and all(db.fetchone("SELECT id FROM jobs WHERE id = ?", (i,)) for i in digested_ids)
    berlin = db.fetchone("SELECT id FROM jobs WHERE employer_name = 'Berlin Robotics'")
    assert berlin and berlin["id"] in digested_ids
    # The cloud's match came along and is what the dashboard shows; the snippet was not rescored into a thin match.
    detail = views.job_detail(db, berlin["id"])
    assert detail["group"] in {"good", "worth_a_shot", "backup"} and res["results"]["matches_added"] >= 1
    cloud_group = next(j["match"]["match_group"] for j in out2["results"]["jobs"] if j["employer_name"] == "Berlin Robotics")
    assert detail["group"] == cloud_group
    assert len(detail["description"]) <= cloudstate.DESCRIPTION_CAP + 2
    # The laptop will not email those jobs again either, and the cloud runs show up in history.
    assert berlin["id"] not in {j["id"] for j in notifications.pending_jobs(db, limit=50)}
    assert db.fetchone("SELECT COUNT(*) AS n FROM hunt_runs WHERE trigger = 'cloud'")["n"] == 2
    assert cloudstate.status(db)["role"] == "laptop"

    # A dashboard action on a cloud-found job survives the next round trip.
    actions.favorite_job(db, berlin["id"])
    notes.add(db, berlin["id"], "Nice team.")
    bundle3 = cloudstate.export_bundle(ctx, db)
    assert "Nice team." not in json.dumps(bundle3)
    c3, d3 = _fresh(tmp_path, "cloud3")
    handoff3, out3 = _cloud_run(c3, d3, bundle3)
    assert handoff3["status"] == "skipped"
    d3.close()
    cloudstate.import_bundle(ctx, db, out3, inputs=False, results=True)
    assert views.job_detail(db, berlin["id"])["favorited"] is True and [n["body"] for n in notes.list_for_job(db, berlin["id"])] == ["Nice team."]
    assert db.fetchone("SELECT COUNT(*) AS n FROM notifications")["n"] == 1


def test_handoff_records_outcomes_honestly(ctx, db):
    approve_profile(db, ctx)
    notifications.set_recipient(db, "student@example.com")
    db.set_setting("digest.transport", "claude")
    _seed(ctx, db, "h1")
    h = notifications.prepare_handoff(ctx, db)
    assert h["status"] == "ready" and h["subject"] and h["html"] and "Send to exactly this address" in h["rules"][0]
    # Claimed: a second prepare does not hand the same jobs out twice while the first send is open.
    again = notifications.prepare_handoff(ctx, db)
    assert again["status"] == "skipped"
    # Failed, then prepared again: same row, another attempt.
    assert notifications.record_handoff(db, h["notification_id"], "failed", error="Gmail connector: 429")["status"] == "failed"
    assert notifications.failed_digest(db)["id"] == h["notification_id"]
    assert notifications.retry_plan(db, notifications.failed_digest(db))["will_retry"] is False  # Claude retries, not the Python worker
    h2 = notifications.prepare_handoff(ctx, db)
    assert h2["status"] == "ready" and h2["notification_id"] == h["notification_id"]
    with pytest.raises(notifications.DigestError):
        notifications.record_handoff(db, h["notification_id"], "delivered")
    assert notifications.record_handoff(db, h["notification_id"], "uncertain", error="no reply from Gmail")["status"] == "uncertain"
    assert [u["id"] for u in notifications.unresolved(db)] == [h["notification_id"]]
    assert notifications.prepare_handoff(ctx, db)["status"] == "skipped"  # those jobs are held back, never silently resent
    with pytest.raises(notifications.DigestError):
        notifications.record_handoff(db, h["notification_id"], "sent")  # not claimed any more
    notifications.resolve(db, h["notification_id"], "arrived")
    assert notifications.prepare_handoff(ctx, db)["status"] == "skipped"
    db.execute("UPDATE notifications SET sent_at = '2026-01-01T08:00:00+00:00' WHERE id = ?", (h["notification_id"],))
    # Sent: schedule and supersede bookkeeping, as for any other transport; and one digest per local day after that.
    _seed(ctx, db, "h2")
    h3 = notifications.prepare_handoff(ctx, db)
    notifications.record_handoff(db, h3["notification_id"], "sent", message_id="m-1")
    assert schedule.get(db)["last_digest_at"] and db.fetchone("SELECT provider_message_id FROM notifications WHERE id = ?", (h3["notification_id"],))["provider_message_id"] == "m-1"
    _seed(ctx, db, "h2b")
    assert notifications.prepare_handoff(ctx, db)["note"].startswith("Already emailed today")
    db.execute("UPDATE notifications SET sent_at = '2026-01-01T08:00:00+00:00' WHERE id = ?", (h3["notification_id"],))
    assert notifications.prepare_handoff(ctx, db)["status"] == "ready"
    # The scheduler never tries to send in this mode.
    schedule.set_frequency(db, "daily")
    schedule.set_notify_mode(db, "daily_email", "00:00")
    schedule.enable(db)
    db.execute("UPDATE schedule SET last_digest_at = NULL WHERE id = 1")
    _seed(ctx, db, "h3")
    res = worker.tick(ctx, db)
    assert res["sent_digest"] is False and "Claude sends" in res["digest"]["error"]


def test_bundle_rejects_wrong_format_and_requires_a_profile(ctx, db):
    with pytest.raises(cloudstate.BundleError):
        cloudstate.export_bundle(ctx, db)
    with pytest.raises(cloudstate.BundleError):
        cloudstate.import_bundle(ctx, db, {"format": "something-else"}, inputs=True, results=True)
