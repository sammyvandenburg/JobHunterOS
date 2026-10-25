"""InterviewOS hand-off: a readable Markdown export plus a short copyable summary for a confirmed interview.

What travels: the vacancy as Hunter saw it, the company and role brief with its sources, the exact CV version the
person said they sent (or "unknown", never a guess), what was confirmed as sent against what stayed a draft, the
strongest case (evidence and gaps), the contacts and follow-ups, the person's notes, the interview details and the
timeline. Every export is a snapshot: a later export never rewrites an earlier file."""

from __future__ import annotations

import json

from jobhunter.config import AppContext, write_private_file
from jobhunter.db import Database
from jobhunter.services import actions, notes
from jobhunter.services import profile as profile_svc
from jobhunter.util import now_iso, slugify


class HandoffError(Exception):
    pass


def _fmt(ts: str | None) -> str:
    return (ts or "")[:16].replace("T", " ") or "unknown"


def export(ctx: AppContext, db: Database, job_id: int, include_messages: bool = False, include_notes: bool = True) -> dict:
    from jobhunter.services import documents as docs
    from jobhunter.services import workspace

    job = db.fetchone("SELECT * FROM jobs WHERE id = ?", (job_id,))
    if not job:
        raise HandoffError(f"Job {job_id} not found.")
    app = db.fetchone("SELECT * FROM applications WHERE job_id = ?", (job_id,))
    if not app:
        raise HandoffError("There is no application for this job yet.")
    interview = db.fetchone("SELECT * FROM events WHERE job_id = ? AND kind = 'interview' AND reverted_by IS NULL ORDER BY occurred_at DESC LIMIT 1", (job_id,))
    if not interview and not app.get("applied_at"):
        raise HandoffError("Tell Hunter you applied first ('I applied'). The hand-off is for applications that are under way; mark the interview when it is confirmed (`jobhunter act stage <job> interview`).")
    profile = profile_svc.approved(db)
    facts = [f for f in profile["facts"] if f.status != "rejected"] if profile else []
    rev = docs.cv_revision(db, app["submitted_cv_revision_key"]) if app.get("submitted_cv_revision_key") else None
    cv_choice = app.get("submitted_cv_choice") or "unknown"
    match = db.fetchone("SELECT * FROM matches WHERE job_id = ? ORDER BY computed_at DESC LIMIT 1", (job_id,))
    drafts = db.fetchall("SELECT * FROM drafts WHERE job_id = ? ORDER BY id", (job_id,))
    sent = [d for d in drafts if d["status"] == "sent_confirmed"]
    unsent = [d for d in drafts if d["status"] != "sent_confirmed" and d["kind"] != "package"]
    history = actions.history(db, job_id)
    emails = db.fetchall("SELECT * FROM email_messages WHERE linked_job_id = ? ORDER BY received_at", (job_id,)) if include_messages else []
    submission = json.loads(app["submission_json"]) if app.get("submission_json") else {}
    research = workspace.current_research(db, job_id)
    contacts = workspace.list_contacts(db, job_id)
    followups = db.fetchall("SELECT * FROM followups WHERE application_id = ? ORDER BY id", (app["id"],))
    job_notes = notes.list_for_job(db, job_id) if include_notes else []
    try:
        cv_text = docs.cv_text(db, rev["revision_key"]) if rev else ""
    except docs.DocumentError:
        cv_text = ""
    case = workspace.strongest_case(db, job, facts, cv_text, years_relevant=(profile.get("targets") or {}).get("professional_years_relevant")) if facts else {"items": [], "examples": []}
    employer = job.get("employer_name") or ("confidential employer" if job.get("employer_confidential") else "unknown employer")

    lines = [f"# InterviewOS hand-off: {job['title']} at {employer}", "", f"Exported by Hunter (JobHunterOS by KubeCraft) on {now_iso()}. A snapshot: nothing here is rewritten later.", ""]
    lines += ["## Vacancy snapshot (as Hunter saw it)", "", f"- Title: {job['title']}", f"- Employer: {employer}", f"- Location: {job.get('location_text') or 'not stated'}" + (f" · {job['remote_restriction']}" if job.get("remote_restriction") else ""), f"- Pay as listed: {job.get('salary_text') or 'not stated'}", f"- Posted: {job.get('posted_at') or 'unknown'} (source field: {job.get('posted_at_provenance') or 'unknown'})", f"- First seen by Hunter: {job['first_seen_at']}; last checked {job.get('last_checked_at') or job.get('last_seen_at') or 'unknown'}", f"- Discovery link: {job.get('discovery_url') or 'none'}", f"- Employer link: {job.get('direct_url') if job.get('direct_url_status') == 'verified' else 'not confirmed'}", f"- Apply link: {job.get('apply_url') or 'none'}", "", "### Description (stored copy)", "", (job.get("description_text") or "")[:6000], ""]
    lines += ["## Company and role brief (with sources)", ""]
    if research:
        for title, key in (("The vacancy says", "vacancy_says"), ("The company describes itself", "company_describes"), ("Hunter's reading", "interpretation")):
            if research.get(key):
                lines += [f"### {title}", ""] + [f"- {x}" for x in research[key]] + [""]
        lines += ["### Sources", ""] + [f"- {s.get('label') or s.get('url')}: {s.get('url')} (checked {_fmt(s.get('checked_at'))}; {s.get('certainty') or ''})" for s in research.get("sources") or []] + [f"- Uncertainty: {research.get('uncertainty')}" if research.get("uncertainty") else "", f"- Brief version {research.get('version')}, {research.get('created_by')}", ""]
    else:
        lines += ["- No brief was prepared for this job.", ""]
    lines += ["## CV version you said you sent", ""]
    tomb = json.loads(app["submitted_cv_tombstone_json"]) if app.get("submitted_cv_tombstone_json") else None
    if rev:
        lines += [f"- {rev['label']}, version {rev['number']}: {rev['original_filename']} ({rev['byte_size']} bytes), SHA-256 {rev['sha256']}", f"- Recorded from your answer to 'which CV?': {cv_choice}. The file itself is not in this export; attach it yourself if InterviewOS needs it."]
    elif tomb:
        lines += [f"- {tomb.get('label')}, version {tomb.get('number')}: {tomb.get('filename')}, SHA-256 {tomb.get('sha256')} (you deleted this CV on {str(tomb.get('deleted_at') or '')[:10]}; the record of what was sent stays)."]
    else:
        lines += [f"- Unknown: no CV version was recorded for this application ({'you said you were not sure' if cv_choice == 'unknown' else 'nothing recorded'}). Hunter does not guess."]
    if submission.get("destination"):
        lines += [f"- Sent to: {submission['destination']} (evidence: {(submission.get('evidence') or {}).get('source', 'you')})"]
    lines += ["", "## Answers you confirmed (your own wording)", ""]
    answers = submission.get("answers") or {}
    if submission.get("answers_state") == "confirmed":
        for q, a in answers.items():
            lines += [f"### {q}", "", (a.get("text") or "").strip() or "(you left this empty)", ""]
    elif submission.get("answers_state") == "explicitly_empty":
        lines += ["- You cleared your answers before applying; nothing is listed as yours.", ""]
    elif submission:
        lines += ["- No answers were recorded at the time you applied. Later drafts are not listed as sent.", ""]
    else:
        lines += ["- Not yet applied: nothing recorded as sent.", ""]
    if submission.get("texts"):
        for kind, t in submission["texts"].items():
            lines += [f"### Your {'introduction' if kind == 'intro' else 'cover letter'} at the time you applied", "", (t.get("text") or "").strip(), ""]
    lines += ["## Messages confirmed as sent", ""]
    if sent:
        for d in sent:
            c = json.loads(d["content_json"] or "{}")
            lines += [f"### {d['kind']} (you confirmed it was sent {_fmt(d['sent_confirmed_at'])})", "", f"Subject: {c.get('subject', '')}", "", (c.get("body") or "")[:4000], ""]
    else:
        lines += ["- Nothing was confirmed as sent. Drafts below were never submitted unless you did so outside Hunter.", ""]
    lines += ["## Drafts that stayed drafts (for reference only; not sent)", ""]
    lines += [f"- Draft {d['id']} ({d['kind']}, status {d['status']}, created {_fmt(d['created_at'])})" for d in unsent] or ["- None."]
    lines += ["", "## Your strongest case: evidence and gaps", ""]
    for i in case.get("items") or []:
        lines.append(f"- {i['requirement']}: " + (f"your profile supports it ({i.get('evidence_type')}): {i.get('evidence')}" if i.get("profile_supports") else "not in your profile yet") + ("; the CV you sent says it" if i.get("cv_communicates") else ("; the CV you sent does not use that word" if i.get("profile_supports") and cv_text else "")))
    for x in case.get("examples") or []:
        lines.append(f"- Example to tell ({x['kind']}): {x['text']}")
    if match:
        lines += [f"- Hunter's assessment ({match['explanation_source']}): {match.get('explanation_text')}"]
        for g in json.loads(match.get("gaps_json") or "[]"):
            lines.append(f"- Gap to prepare for: {g}")
    lines += ["", "## People", ""]
    lines += [f"- {c['name']} ({c['type_label']}{', ' + c['role'] if c.get('role') else ''}): {c.get('relevance') or ''}" + (f" Source: {c['source_url']}" if c.get("source_url") else "") + (f" Route: {c['route']}" if c.get("route") else "") + (f" You contacted them {_fmt(c['contacted_at'])}." if c.get("contacted_at") else " Not contacted.") for c in contacts] or ["- No contacts recorded."]
    lines += ["", "## Follow-ups", ""]
    lines += [f"- Due {_fmt(f['due_at'])}: {f['status']}" + (f" ({f['reason']})" if f.get("reason") else "") for f in followups] or ["- None."]
    if include_notes:
        lines += ["", "## Your notes", ""]
        lines += [f"- {_fmt(n['updated_at'])}: {n['body']}" for n in job_notes] or ["- None."]
    lines += ["", "## Interview details and timeline", ""]
    if interview:
        payload = json.loads(interview.get("payload_json") or "{}")
        lines += [f"- Interview recorded {_fmt(interview['occurred_at'])}" + (f": {json.dumps(payload)}" if payload and not payload.get("ignored_for_pipeline") else ""), ""]
    for ev in history:
        if ev["kind"] in {"interview", "applied", "reply_received", "contacted", "note", "offer", "rejected", "withdrawn", "followup"}:
            lines.append(f"- {_fmt(ev['occurred_at'])}: {ev['label']} ({ev['actor']})" + (f" — {ev['note']}" if ev.get("note") else ""))
    if include_messages and emails:
        lines += ["", "## Messages you authorized for this hand-off", ""]
        for em in emails:
            lines += [f"### {em['received_at']} — {em['subject']} (from {em['from_addr']})", "", (em.get("extract_text") or em.get("snippet") or "")[:2000], ""]
    summary = " · ".join([
        f"{job['title']} at {employer}",
        f"applied {_fmt(app.get('applied_at'))}" if app.get("applied_at") else "not yet applied",
        f"CV sent: {rev['label']} v{rev['number']}" if rev else "CV sent: unknown",
        f"{len(answers)} confirmed answer{'s' if len(answers) != 1 else ''}",
        f"{len(sent)} message{'s' if len(sent) != 1 else ''} confirmed sent, {len(unsent)} draft{'s' if len(unsent) != 1 else ''} not sent",
        f"{len(contacts)} contact{'s' if len(contacts) != 1 else ''}",
    ])
    lines += ["", "---", "Copyable summary: " + summary, "", "This export is a plain Markdown file. It does not include your CV file; attach it yourself if InterviewOS needs it."]
    n = db.fetchone("SELECT COUNT(*) AS n FROM handoffs WHERE application_id = ?", (app["id"],))["n"] + 1
    path = ctx.paths.handoffs_dir / f"{slugify(job.get('employer_name') or 'employer')}-{slugify(job['title'])}-{job_id}-{n}.md"
    while path.exists():
        n += 1
        path = path.with_name(f"{slugify(job.get('employer_name') or 'employer')}-{slugify(job['title'])}-{job_id}-{n}.md")
    write_private_file(path, "\n".join(lines).encode("utf-8"))
    db.insert("handoffs", {"application_id": app["id"], "path": str(path), "created_at": now_iso()})
    return {"path": str(path), "summary": summary, "sent_items": len(sent), "unsent_drafts": len(unsent), "cv_known": rev is not None, "confirmed_answers": len(answers), "contacts": len(contacts), "interview_recorded": interview is not None}
