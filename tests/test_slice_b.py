"""Slice B: Today, automation modes and bounded auto-preparation, readiness labels, the contextual answer library,
and the opened marker. Synthetic data and local fakes only: nothing here reaches an employer, a mailbox or the
page service."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jobhunter.services import actions, board, hunt, notifications, workspace
from jobhunter.services import documents as docs
from jobhunter.services import drafts as drafts_svc
from jobhunter.services import today as today_svc
from tests.conftest import approve_profile
from tests.test_board import _action, _key_for, _seed_jobs


def _good_ids(db):
    from jobhunter.services import views

    return [it["id"] for it in views.current_recommended(db) if it["group"] == "good"]


def test_auto_preparation_is_off_by_default_bounded_and_uses_the_same_requests(ctx, db):
    approve_profile(db, ctx, countries="Netherlands")
    _seed_jobs(ctx, db)
    good = _good_ids(db)
    assert len(good) >= 2
    # Off by default: a search prepares nothing on its own.
    res = today_svc.auto_prepare(ctx, db)
    assert res["mode"] == "on_request" and res["count"] == 0 and "off" in res["note"]
    with pytest.raises(today_svc.TodayError, match="not available"):
        today_svc.set_mode(db, "assisted_apply")  # the assisted lane is a later slice; the mode cannot be chosen before it exists
    today_svc.set_mode(db, "prepare_best")
    today_svc.set_limits(db, preparations_per_day=1, unopened_ready=5)
    # The person's own request goes first: nothing automatic while it waits.
    workspace.request(db, good[0], "prepare", {}, request_id="prepare:mine")
    assert today_svc.auto_prepare(ctx, db)["count"] == 0
    workspace.run_pending(ctx, db)
    res = today_svc.auto_prepare(ctx, db)
    assert res["count"] == 1 and res["prepared"][0]["state"] == "ready" and res["prepared"][0]["job_id"] != good[0]
    row = db.fetchone("SELECT * FROM requests WHERE job_id = ? AND kind = 'prepare'", (res["prepared"][0]["job_id"],))
    assert row["origin"] == "auto" and row["state"] == "ready"  # the same request chain the page uses
    assert db.fetchone("SELECT origin FROM requests WHERE request_id = 'prepare:mine'")["origin"] == "user"
    assert drafts_svc.current_package(db, res["prepared"][0]["job_id"])
    # The day's limit holds; the queue limit holds until the person opens what is waiting.
    res2 = today_svc.auto_prepare(ctx, db)
    assert res2["count"] == 0 and "Limit reached" in res2["note"] and res2["prepared_today"] == 1
    today_svc.set_limits(db, preparations_per_day=5, unopened_ready=1)
    assert today_svc.auto_prepare(ctx, db)["count"] == 0  # one prepared pack nobody opened fills the queue
    assert today_svc.mark_opened(db, res["prepared"][0]["job_id"]) and not today_svc.mark_opened(db, res["prepared"][0]["job_id"])
    res3 = today_svc.auto_prepare(ctx, db)
    assert res3["count"] == (1 if len(good) >= 3 else 0)
    # A dismissed job is never prepared; nothing is ever marked applied.
    for jid in good:
        actions.dismiss_job(db, jid)
    today_svc.set_limits(db, preparations_per_day=10, unopened_ready=10)
    assert today_svc.auto_prepare(ctx, db)["count"] == 0
    assert db.fetchone("SELECT COUNT(*) AS n FROM applications WHERE applied_at IS NOT NULL")["n"] == 0
    assert db.fetchone("SELECT COUNT(*) AS n FROM drafts WHERE kind = 'outreach' AND status = 'sent'")["n"] == 0


def test_a_search_runs_the_bounded_preparation_and_reports_it(ctx, db, fake_sources):
    approve_profile(db, ctx, countries="Netherlands")
    today_svc.set_mode(db, "prepare_best")
    stats = hunt.run_hunt(ctx, db)
    assert stats["status"] in {"completed", "partial"} and stats["auto_prepared"] >= 1 and stats["auto_prepared"] <= 3
    again = hunt.run_hunt(ctx, db)
    assert again["auto_prepared"] == 0  # nothing new to prepare; the limits and the queue hold
    db.set_setting(today_svc.MODE_SETTING, "on_request")
    assert hunt.run_hunt(ctx, db)["auto_prepared"] == 0


def test_today_has_five_groups_with_one_action_each(ctx, db, tmp_path):
    approve_profile(db, ctx, countries="Netherlands")
    _seed_jobs(ctx, db)
    good = _good_ids(db)
    t = today_svc.today(ctx, db)
    assert [g["id"] for g in t["groups"]] == ["ready", "needs_answer", "followup", "changed", "attention"] and t["total"] == 0 and t["empty_text"]
    today_svc.set_mode(db, "prepare_best")
    prepared = today_svc.auto_prepare(ctx, db)["prepared"]
    pid = prepared[0]["job_id"]
    # Needs your answer: a form question only the person can answer; a request that needs input.
    workspace.add_questions(db, pid, [{"text": "What is your notice period?", "limit": 100}])
    other = next(j for j in good if j != pid)
    drafts_svc.prepare(ctx, db, other)
    rq = workspace.request(db, other, "message", {}, request_id="message:nocontact")
    workspace.run_request(ctx, db, rq["request_id"])
    # Follow-up due on an applied job; something changed on the auto-prepared one.
    third = db.fetchone("SELECT id FROM jobs WHERE id NOT IN (?, ?) ORDER BY id", (pid, other))["id"]
    actions.mark_applied(db, third, applied_at="2026-10-01T09:00:00+00:00", cv_choice="unknown")
    workspace.set_followup(db, third, due_at="2026-10-02T09:00:00+00:00")
    db.execute("UPDATE jobs SET description_text = description_text || ' Now with Pulumi.', description_hash = 'h-changed' WHERE id = ?", (pid,))
    # Attention: the page was written but never confirmed.
    board.link(db, "https://claude.ai/artifact/5wUUffKaZnmpqGgPnRzPVE")
    board.import_actions(ctx, db, tmp_path / "none", listed=0)
    board.export(ctx, db, tmp_path / "out")
    t = today_svc.today(ctx, db)
    by = {g["id"]: g["items"] for g in t["groups"]}
    ready = by["ready"]
    assert ready and ready[0]["action"]["label"] == "Open" and ready[0]["action"]["key"].startswith("j") and "Hunter prepared" in ready[0]["text"]
    assert {i["kind"] for i in by["needs_answer"]} == {"needs_input", "question"} and all(i["action"]["label"] == "Answer" for i in by["needs_answer"])
    assert len(by["followup"]) == 1 and "follow-up was due" in by["followup"][0]["text"] and by["followup"][0]["action"]["key"]
    assert by["changed"][0]["kind"] == "job_changed" and by["changed"][0]["action"]["label"] == "Prepare again" and "wording is kept" in by["changed"][0]["text"]
    assert any(i["kind"] == "not_published" for i in by["attention"]) and all(i["action"]["label"] for i in by["attention"])
    assert t["automation"]["mode"] == "prepare_best" and t["automation"]["prepared_today"] >= 1
    text = today_svc.text_view(t)
    assert "Ready to review (" in text and "Needs your answer (" in text and "Follow-up due (" in text and "Something changed (" in text and "Needs attention (" in text
    # The export carries it to the page as one document, and the status knows the mode.
    assert (tmp_path / "out" / "meta" / "today.json").exists()
    st = board._hunter_status(db)
    assert st["automation"]["mode"] == "prepare_best" and st["automation"]["limits"]["preparations_per_day"] == 3


def test_readiness_is_a_label_not_a_gate_review_then_submit(ctx, db):
    approve_profile(db, ctx, countries="Netherlands")
    _seed_jobs(ctx, db)
    jid = _good_ids(db)[0]
    drafts_svc.prepare(ctx, db, jid)
    rd = workspace.readiness(db, jid)
    assert rd["state"] == "ready_to_submit" and rd["missing"] == []  # no form questions, CV at hand, a place to apply
    workspace.add_questions(db, jid, [{"text": "What is your notice period?"}, {"text": "Why us? (200 words)", "limit": 200, "limit_unit": "words"}])
    rd = workspace.readiness(db, jid)
    assert rd["state"] == "ready_for_review" and "2 of 2 form questions" in rd["missing"][0] and rd["label"] == "Ready for review"
    workspace.save_answer(db, jid, workspace.answer_id("What is your notice period?"), "One month.")
    workspace.save_answer(db, jid, workspace.answer_id("Why us? (200 words)"), "Because of the platform work.")
    assert workspace.readiness(db, jid)["state"] == "ready_to_submit"
    # The CV file left this computer: not ready to submit until it is added again (durable retrieval, not a guess).
    cv = drafts_svc.selected_cv(db, jid)
    rev = docs.cv_revision(db, cv["revision_key"])
    Path(rev["stored_path"]).unlink()
    rd = workspace.readiness(db, jid)
    assert rd["state"] == "ready_for_review" and "add it again" in rd["missing"][0]
    key = _key_for(db, db.fetchone("SELECT employer_name FROM jobs WHERE id = ?", (jid,))["employer_name"])
    w = workspace.work_doc(db, jid, key)
    assert w["package"]["readiness"]["state"] == "ready_for_review" and w["cv"]["selected"]["file_available"] is False and w["package"]["origin"] == "user"


def test_the_answer_library_offers_reusable_wording_only(ctx, db):
    approve_profile(db, ctx, countries="Netherlands")
    _seed_jobs(ctx, db)
    a, b = _good_ids(db)[:2]
    drafts_svc.prepare(ctx, db, a)
    drafts_svc.prepare(ctx, db, b)
    q = next(x["question"] for x in drafts_svc.current_package(db, a)["draft"]["answers"] if x["answer"])
    aid = workspace.answer_id(q)
    emp_a = db.fetchone("SELECT employer_name FROM jobs WHERE id = ?", (a,))["employer_name"]
    # Names the employer: this job's alone.
    assert workspace.save_answer(db, a, aid, f"I want to join {emp_a} because of its platform team.")["scope"] == "job"
    key_b = _key_for(db, db.fetchone("SELECT employer_name FROM jobs WHERE id = ?", (b,))["employer_name"])
    w = workspace.work_doc(db, b, key_b)
    assert next(x for x in w["package"]["answers"] if x["id"] == aid)["library"] is None
    # General wording: offered on the other job, with where and when it was written, never filled in by itself.
    assert workspace.save_answer(db, a, aid, "I enjoy running platforms that other engineers build on.")["scope"] == "reusable"
    w = workspace.work_doc(db, b, key_b)
    lib = next(x for x in w["package"]["answers"] if x["id"] == aid)["library"]
    assert lib["text"].startswith("I enjoy running platforms") and emp_a in lib["from"] and lib["saved_at"]
    assert workspace.user_text(db, b, "answer", aid) is None
    # Once the person wrote their own, no suggestion; a declaration expires; a personal answer is never offered.
    workspace.save_answer(db, b, aid, "My own.")
    assert next(x for x in workspace.work_doc(db, b, key_b)["package"]["answers"] if x["id"] == aid)["library"] is None
    workspace.add_questions(db, a, [{"text": "What is your notice period?"}, {"text": "Do you need visa sponsorship?"}])
    workspace.add_questions(db, b, [{"text": "What is your notice period?"}, {"text": "Do you need visa sponsorship?"}])
    nid, vid = workspace.answer_id("What is your notice period?"), workspace.answer_id("Do you need visa sponsorship?")
    assert workspace.save_answer(db, a, nid, "One month.")["scope"] == "reusable"
    assert workspace.save_answer(db, a, vid, "No.")["scope"] == "job"
    row = workspace.user_text(db, a, "answer", nid)
    assert row["expires_at"] and row["expires_at"][:4] >= "2027"
    answers = workspace.work_doc(db, b, key_b)["package"]["answers"]
    assert next(x for x in answers if x["id"] == nid)["library"]["text"] == "One month." and next(x for x in answers if x["id"] == vid)["library"] is None
    db.execute("UPDATE user_texts SET expires_at = '2020-01-01T00:00:00' WHERE job_id = ? AND key = ?", (a, nid))
    assert next(x for x in workspace.work_doc(db, b, key_b)["package"]["answers"] if x["id"] == nid)["library"] is None


def test_the_opened_marker_comes_from_the_page_and_the_digest_names_prepared_packs(ctx, db, tmp_path):
    approve_profile(db, ctx, countries="Netherlands")
    _seed_jobs(ctx, db)
    board.link(db, "https://claude.ai/artifact/5wUUffKaZnmpqGgPnRzPVE")
    board.import_actions(ctx, db, tmp_path / "none", listed=0)
    today_svc.set_mode(db, "prepare_best")
    today_svc.set_limits(db, preparations_per_day=1)
    pid = today_svc.auto_prepare(ctx, db)["prepared"][0]["job_id"]
    key = _key_for(db, db.fetchone("SELECT employer_name FROM jobs WHERE id = ?", (pid,))["employer_name"])
    board.export(ctx, db, tmp_path / "out")
    t = json.loads((tmp_path / "out" / "meta" / "today.json").read_text())
    assert t["groups"][0]["items"][0]["opened"] is False and t["automation"]["unopened_ready"] == 1
    # The digest, with no new jobs: the prepared pack is named, as something to review, never as something sent.
    notifications.set_recipient(db, "student@example.com")
    db.set_setting("digest.transport", "claude")
    db.execute("UPDATE jobs SET posted_at = '2025-01-01T00:00:00+00:00'")
    board.import_actions(ctx, db, tmp_path / "none2", listed=0)
    prep = notifications.prepare_handoff(ctx, db)
    assert prep["status"] == "ready" and prep["subject"] == "JobHunterOS: 1 application ready to review" and "Ready to review on your dashboard" in prep["text"] and "never sends" in prep["text"] and prep["jobs"] == []
    # The page says the person opened it: Hunter records it once; the queue has room again.
    _action(tmp_path / "in", key, 1, opened_at="2026-10-25T10:00:00Z")
    res = board.import_actions(ctx, db, tmp_path / "in", listed=1)
    assert "opened" in res["applied"][0]["changes"]
    assert db.fetchone("SELECT opened_at FROM applications WHERE job_id = ?", (pid,))["opened_at"].startswith("2026-10-25")
    assert today_svc.unopened_ready(db) == []
    _action(tmp_path / "in2", key, 2, opened_at="2026-10-26T10:00:00Z")
    assert "opened" not in board.import_actions(ctx, db, tmp_path / "in2", listed=1)["applied"][0]["changes"]
    assert workspace.work_doc(db, pid, key)["application"]["opened_at"].startswith("2026-10-25")
