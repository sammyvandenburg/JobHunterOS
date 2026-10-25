"""Slice D: one intake for links, descriptions, alert emails, recruiter messages and referrals into the same vacancy
identity; mail status updates under a policy with a review queue and undo; the best route; continuity into the
hand-off. Synthetic data only; nothing is fetched or sent."""

from __future__ import annotations

from jobhunter.services import actions, board, hunt, interviewos, workspace
from jobhunter.services import drafts as drafts_svc
from jobhunter.services import intake as intake_svc
from jobhunter.services import today as today_svc
from jobhunter.services.email import linking
from tests.conftest import approve_profile
from tests.test_gate_a import _posting

GH = "https://job-boards.greenhouse.io/northwind/jobs/4411"
ALERT = """New jobs for you
Platform Engineer
Northwind Cloud · Amsterdam, Netherlands
https://job-boards.greenhouse.io/northwind/jobs/4411
View job
Site Reliability Engineer
Contoso Payments · Utrecht, Netherlands
https://job-boards.greenhouse.io/contoso/jobs/9001
View job
"""
RECRUITER = """Hi Sam,

I came across your profile and wanted to reach out about a Platform Engineer role at Northwind Cloud. The team runs Kubernetes on AWS with Terraform. Would you be open to a chat this week?

Best,
Maya Jansen
"""
REFERRAL = """Hey, my former colleague Tom at Contoso Payments told me about an SRE opening on his team. They use Kubernetes and Terraform. He said to mention his name.

https://job-boards.greenhouse.io/contoso/jobs/9001
"""


def test_a_link_an_alert_and_the_board_itself_are_one_job_and_a_different_job_stays_distinct(ctx, db):
    approve_profile(db, ctx, countries="Netherlands")
    r1 = intake_svc.intake(ctx, db, GH)
    assert r1["kind"] == "link" and len(r1["jobs"]) == 1 and r1["jobs"][0]["new"] and "not fetched" in r1["note"]
    jid = r1["jobs"][0]["id"]
    obs = db.fetchall("SELECT source_id, external_id FROM job_observations WHERE job_id = ?", (jid,))
    assert obs[0]["source_id"] == "greenhouse:northwind" and obs[0]["external_id"] == "4411"  # the board's own identity
    key = board.job_key(db.fetchone("SELECT canonical_key FROM jobs WHERE id = ?", (jid,))["canonical_key"])
    # The board's own posting arrives with the next search: the same job, now with its real title and text.
    hunt.import_postings(ctx, db, [_posting("4411", "Platform Engineer", source_id="greenhouse:northwind", url=GH, apply_url=GH + "/apply")])
    assert db.fetchone("SELECT COUNT(*) AS n FROM jobs")["n"] == 1
    job = db.fetchone("SELECT * FROM jobs WHERE id = ?", (jid,))
    assert job["title"] == "Platform Engineer" and "Kubernetes" in job["description_text"] and board.job_key(job["canonical_key"]) == key
    # An alert email naming the same link merges; the second job in it is a different job.
    r2 = intake_svc.intake(ctx, db, ALERT, sender="jobalerts-noreply@linkedin.com", subject="Platform Engineer: 2 new jobs")
    assert r2["kind"] == "alert" and len(r2["jobs"]) == 2
    by_id = {j["id"]: j for j in r2["jobs"]}
    assert by_id[jid]["merged_into_existing"] and sum(1 for j in r2["jobs"] if j["new"]) == 1
    assert db.fetchone("SELECT COUNT(*) AS n FROM jobs")["n"] == 2
    assert db.fetchone("SELECT COUNT(*) AS n FROM job_observations WHERE job_id = ?", (jid,))["n"] == 1  # one board identity, observed again, never a second job
    assert len(intake_svc.history(db, job_id=jid)) == 2  # the link and the alert both remembered against the same job
    # A pasted description with no link is its own job; the history lists all three intakes.
    r3 = intake_svc.intake(ctx, db, "Cloud Engineer\nWe run Azure and Terraform.", title="Cloud Engineer", company="Fabrikam")
    assert r3["kind"] == "description" and r3["jobs"][0]["new"]
    assert [h["kind"] for h in intake_svc.history(db)] == ["description", "alert", "link"]
    assert intake_svc.history(db, job_id=jid) and all(jid in h["job_ids"] for h in intake_svc.history(db, job_id=jid))


def test_recruiter_messages_and_referrals_become_jobs_contacts_and_timeline_entries(ctx, db):
    approve_profile(db, ctx, countries="Netherlands")
    assert intake_svc.detect_kind(RECRUITER, sender="maya@northwindcloud.example", subject="Platform Engineer at Northwind Cloud") == "recruiter"
    r = intake_svc.intake(ctx, db, RECRUITER, sender="maya@northwindcloud.example", subject="Platform Engineer at Northwind Cloud")
    assert r["kind"] == "recruiter" and r["jobs"][0]["title"] == "Platform Engineer" and r["jobs"][0]["employer"] == "Northwind Cloud" and r["jobs"][0]["new"]
    jid = r["jobs"][0]["id"]
    cs = workspace.list_contacts(db, jid)
    assert len(cs) == 1 and cs[0]["name"] == "Maya Jansen" and cs[0]["type"] == "recruiter" and "message you pasted" in cs[0]["relevance"] and cs[0]["route"] == "maya@northwindcloud.example"
    em = db.fetchone("SELECT * FROM email_messages WHERE id = ?", (r["email_id"],))
    assert em["linked_job_id"] == jid and em["link_status"] == "confirmed" and em["classification"] == "recruiter"
    assert db.fetchone("SELECT stage FROM applications WHERE job_id = ?", (jid,)) is None  # a message is not a reply to an application
    # The same message pasted again changes nothing; the same recruiter is one contact.
    again = intake_svc.intake(ctx, db, RECRUITER, sender="maya@northwindcloud.example", subject="Platform Engineer at Northwind Cloud")
    assert again["jobs"][0]["id"] == jid and not again["jobs"][0]["new"] and len(workspace.list_contacts(db, jid)) == 1
    # A referral with a board link: the job carries the board identity, the referrer is a connection.
    assert intake_svc.detect_kind(REFERRAL) == "referral"
    r2 = intake_svc.intake(ctx, db, REFERRAL, from_name="Tom de Vries")
    assert r2["kind"] == "referral" and r2["jobs"][0]["new"]
    j2 = r2["jobs"][0]["id"]
    assert db.fetchone("SELECT source_id, external_id FROM job_observations WHERE job_id = ?", (j2,))["external_id"] == "9001"
    c2 = workspace.list_contacts(db, j2)
    assert c2[0]["name"] == "Tom de Vries" and c2[0]["type"] == "connection" and "Referred you" in c2[0]["relevance"] and c2[0]["route"] is None
    # Hand-off continuity: how the job reached Hunter, and the message, travel with it.
    drafts_svc.prepare(ctx, db, jid)
    actions.mark_applied(db, jid, cv_choice="unknown")
    actions.set_stage(db, jid, "interview")
    text = __import__("pathlib").Path(interviewos.export(ctx, db, jid, include_messages=True)["path"]).read_text()
    assert "How it reached Hunter: a recruiter message from Maya Jansen you pasted" in text and "Maya Jansen" in text and "came across your profile" in text
    # The route names the best place to apply and the recruiter as the person to mention.
    key = board.job_key(db.fetchone("SELECT canonical_key FROM jobs WHERE id = ?", (jid,))["canonical_key"])
    route = workspace.work_doc(db, jid, key)["package"]["route"]
    assert route["best"] is None or "Maya Jansen" in (route["best"].get("contact") or "")


def test_mail_updates_follow_the_policy_with_a_review_queue_and_undo(ctx, db):
    approve_profile(db, ctx, countries="Netherlands")
    jid = hunt.import_postings(ctx, db, [_posting("nl1", "Platform Engineer")])["job_ids"][0]
    drafts_svc.prepare(ctx, db, jid)
    assert linking.policy(db) == "receipts"
    # Default: an automated receipt marks applied by itself (undoable); a rejection waits for the person.
    rec = linking.import_text_message(db, "Thank you for applying to the Platform Engineer role at Northwind Cloud. We received your application.", sender="no-reply@greenhouse-mail.io", subject="Thank you for your application - Northwind Cloud")
    assert rec["linked"] and db.fetchone("SELECT stage FROM applications WHERE job_id = ?", (jid,))["stage"] == "applied"
    rej = linking.import_text_message(db, "Unfortunately we will not be moving forward with your application for Platform Engineer at Northwind Cloud.", sender="talent@northwindcloud.example", subject="Update on your application")
    assert rej["needs_confirmation"] and rej["suggested_job_id"] == jid and db.fetchone("SELECT stage FROM applications WHERE job_id = ?", (jid,))["stage"] == "applied"
    q = linking.review_queue(db)
    assert q["policy"] == "receipts" and q["suggested"][0]["kind"] == "rejection" and q["suggested"][0]["job"].startswith("Platform Engineer") and q["applied_by_itself"][0]["kind"] == "receipt"
    t = today_svc.today(ctx, db)
    by = {g["id"]: g["items"] for g in t["groups"]}
    item = next(i for i in by["needs_answer"] if i["kind"] == "email_review")
    assert "looks like a rejection for Platform Engineer at Northwind Cloud. Is it?" in item["text"] and f"email confirm {rej['email_id']} {jid}" in item["action"]["command"]
    undo = next(i for i in by["changed"] if i["kind"] == "email_applied")
    assert "marked applied from an email" in undo["text"] and undo["action"]["label"] == "Undo" and f"email unlink {rec['email_id']}" in undo["action"]["command"]
    # Confirm the rejection; then undo it; then undo the receipt: every step reversible, nothing deleted.
    linking.apply_link(db, rej["email_id"], jid, actor="user")
    assert db.fetchone("SELECT stage FROM applications WHERE job_id = ?", (jid,))["stage"] == "rejected" and linking.review_queue(db)["suggested"] == []
    linking.unlink(db, rej["email_id"])
    assert db.fetchone("SELECT stage FROM applications WHERE job_id = ?", (jid,))["stage"] == "applied"
    linking.unlink(db, rec["email_id"])
    assert db.fetchone("SELECT applied_at FROM applications WHERE job_id = ?", (jid,))["applied_at"] is None
    assert db.fetchone("SELECT COUNT(*) AS n FROM events WHERE kind = 'correction'")["n"] == 2
    # "suggest": even receipts wait. "all": confident human mails change the pipeline (reversibly). A newsletter never does.
    linking.set_policy(db, "suggest")
    rec2 = linking.import_text_message(db, "We received your application for Platform Engineer at Northwind Cloud.", sender="noreply@northwindcloud.example", subject="Application received", received_at="2026-10-26T10:00:00+00:00")
    assert rec2["needs_confirmation"] and rec2["policy"] == "suggest" and db.fetchone("SELECT applied_at FROM applications WHERE job_id = ?", (jid,))["applied_at"] is None
    linking.set_policy(db, "all")
    iv = linking.import_text_message(db, "Please schedule your interview for the Platform Engineer role at Northwind Cloud via calendly.com/x", sender="recruiting@northwindcloud.example", subject="Interview invitation: Platform Engineer")
    assert iv["linked"] and db.fetchone("SELECT stage FROM applications WHERE job_id = ?", (jid,))["stage"] == "interview"
    news = linking.import_text_message(db, "Our monthly newsletter: 10 tips for platform teams at Northwind Cloud.", sender="news@northwindcloud.example", subject="Northwind Cloud newsletter")
    assert not news.get("linked") and not news.get("needs_confirmation")
    # A reply stops the pending follow-up reminder (continuity): a follow-up set while applied is cancelled by the interview mail.
    other = hunt.import_postings(ctx, db, [_posting("nl2", "DevOps Engineer")])["job_ids"][0]
    actions.mark_applied(db, other, applied_at="2026-10-01T09:00:00+00:00", cv_choice="unknown")
    workspace.set_followup(db, other, due_at="2026-10-02T09:00:00+00:00")
    assert any(f["job_id"] == other for f in drafts_svc.due_followups(db))
    iv2 = linking.import_text_message(db, "Please schedule your interview for the DevOps Engineer role at Northwind Cloud via calendly.com/y", sender="recruiting@northwindcloud.example", subject="Interview invitation: DevOps Engineer")
    assert iv2["linked"] and not any(f["job_id"] == other for f in drafts_svc.due_followups(db))
    assert db.fetchone("SELECT status, reason FROM followups WHERE application_id = (SELECT id FROM applications WHERE job_id = ?) ORDER BY id DESC LIMIT 1", (other,))["status"] == "cancelled"


def test_the_best_route_prefers_the_confirmed_employer_page(ctx, db):
    approve_profile(db, ctx, countries="Netherlands")
    jid = hunt.import_postings(ctx, db, [_posting("nl1", "Platform Engineer", apply_url="https://job-boards.greenhouse.io/northwind/jobs/nl1/apply")])["job_ids"][0]
    drafts_svc.prepare(ctx, db, jid)
    key = board.job_key(db.fetchone("SELECT canonical_key FROM jobs WHERE id = ?", (jid,))["canonical_key"])
    route = workspace.work_doc(db, jid, key)["package"]["route"]
    assert route["best"]["url"] == "https://job-boards.greenhouse.io/northwind/jobs/nl1" and "confirmed" in route["best"]["why"] and "contact" not in route["best"]
    workspace.add_contact(db, jid, "listed", "Priya Nair", role="Hiring manager", source_url="https://job-boards.greenhouse.io/northwind/jobs/nl1")
    route = workspace.work_doc(db, jid, key)["package"]["route"]
    assert "Priya Nair (Hiring manager) is named in the posting" in route["best"]["contact"]
    other = hunt.import_postings(ctx, db, [_posting("x9", "SRE", url="https://remotive.com/remote-jobs/9", apply_url="https://remotive.com/remote-jobs/9", source_id="remotive:devops", is_employer_hosted=False)])["job_ids"][0]
    drafts_svc.prepare(ctx, db, other)
    key2 = board.job_key(db.fetchone("SELECT canonical_key FROM jobs WHERE id = ?", (other,))["canonical_key"])
    best = workspace.work_doc(db, other, key2)["package"]["route"]["best"]
    assert best["url"].startswith("https://remotive.com") and "not confirmed" in best["why"]
