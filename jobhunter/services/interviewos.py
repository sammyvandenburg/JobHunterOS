"""InterviewOS hand-off: a readable Markdown export for a confirmed interview."""

from __future__ import annotations

import json

from jobhunter.config import AppContext, write_private_file
from jobhunter.db import Database
from jobhunter.services import actions
from jobhunter.services import profile as profile_svc
from jobhunter.util import now_iso, slugify


class HandoffError(Exception):
    pass


def export(ctx: AppContext, db: Database, job_id: int, include_messages: bool = False) -> dict:
    job = db.fetchone("SELECT * FROM jobs WHERE id = ?", (job_id,))
    if not job:
        raise HandoffError(f"Job {job_id} not found.")
    app = db.fetchone("SELECT * FROM applications WHERE job_id = ?", (job_id,))
    if not app:
        raise HandoffError("There is no application for this job yet.")
    interview = db.fetchone("SELECT * FROM events WHERE job_id = ? AND kind = 'interview' AND reverted_by IS NULL ORDER BY occurred_at DESC LIMIT 1", (job_id,))
    if not interview:
        raise HandoffError("Mark the interview first (`jobhunter apply stage <job_id> interview`). The hand-off is for confirmed interviews.")
    profile = profile_svc.approved(db)
    cv = db.fetchone("SELECT * FROM source_documents WHERE id = ?", (app["cv_document_id"],)) if app.get("cv_document_id") else None
    match = db.fetchone("SELECT * FROM matches WHERE job_id = ? ORDER BY computed_at DESC LIMIT 1", (job_id,))
    drafts = db.fetchall("SELECT * FROM drafts WHERE job_id = ? ORDER BY id", (job_id,))
    sent = [d for d in drafts if d["status"] == "sent_confirmed"]
    unsent = [d for d in drafts if d["status"] != "sent_confirmed"]
    history = actions.history(db, job_id)
    emails = db.fetchall("SELECT * FROM email_messages WHERE linked_job_id = ? ORDER BY received_at", (job_id,)) if include_messages else []

    lines = [f"# InterviewOS hand-off: {job['title']} at {job.get('employer_name') or 'unknown employer'}", "", f"Exported by Hunter on {now_iso()}.", ""]
    lines += ["## Vacancy snapshot (as Hunter saw it)", "", f"- Title: {job['title']}", f"- Employer: {job.get('employer_name') or 'unknown'}", f"- Location: {job.get('location_text') or 'not stated'}" + (f" · {job['remote_restriction']}" if job.get("remote_restriction") else ""), f"- Salary as listed: {job.get('salary_text') or 'Not listed'}", f"- Posting date: {job.get('posted_at') or 'unknown'} (source field: {job.get('posted_at_provenance') or 'unknown'})", f"- First seen by Hunter: {job['first_seen_at']}", f"- Discovery link: {job.get('discovery_url') or 'none'}", f"- Employer link: {job.get('direct_url') if job.get('direct_url_status') == 'verified' else 'not confirmed'}", f"- Apply link: {job.get('apply_url') or 'none'}", "", "### Description (stored copy)", "", (job.get("description_text") or "")[:6000], ""]
    lines += ["## CV version used", ""]
    if cv:
        lines += [f"- Document id {cv['id']}, version {cv['version']}, SHA-256 {cv['sha256']}", f"- Original file: {cv['original_filename']} ({cv['byte_size']} bytes), unchanged by Hunter"]
    else:
        lines += ["- Unknown: no CV version was recorded for this application."]
    lines += ["", "## Application answers and outreach actually sent", ""]
    if sent:
        for d in sent:
            lines += [f"### {d['kind']} (confirmed sent {d['sent_confirmed_at']})", "", "```", json.dumps(json.loads(d["content_json"]), indent=2)[:4000], "```", ""]
    else:
        lines += ["- Nothing was confirmed as sent. Drafts below were never submitted unless you did so outside Hunter.", ""]
    lines += ["## Unsubmitted drafts (for reference only)", ""]
    if unsent:
        for d in unsent:
            lines += [f"- Draft {d['id']} ({d['kind']}, status {d['status']}, created {d['created_at']})"]
    else:
        lines += ["- None."]
    lines += ["", "## Relevant evidence, gaps, and discussion points", ""]
    if match:
        expl = json.loads(match.get("explanation_json") or "{}")
        lines += [f"- Hunter's assessment ({match['explanation_source']}): {match.get('explanation_text')}"]
        for r in expl.get("reasons", []):
            lines.append(f"- Reason: {r.get('text')} (facts: {', '.join(r.get('fact_ids', [])) or 'none cited'})")
        for g in json.loads(match.get("gaps_json") or "[]"):
            lines.append(f"- Gap to prepare for: {g}")
    if profile:
        lines += ["", "### Approved profile facts (short)", ""]
        for f in profile["facts"][:40]:
            lines.append(f"- [{f.id}] {f.category} ({f.experience_kind}): {f.text}")
    lines += ["", "## Interview details and timeline", ""]
    for ev in history:
        if ev["kind"] in {"interview", "applied", "reply_received", "contacted", "note", "offer", "rejected"}:
            lines.append(f"- {ev['occurred_at']}: {ev['label']} ({ev['actor']})" + (f" — {ev['note']}" if ev.get("note") else ""))
    if include_messages and emails:
        lines += ["", "## Messages you authorized for this hand-off", ""]
        for em in emails:
            lines += [f"### {em['received_at']} — {em['subject']} (from {em['from_addr']})", "", (em.get("extract_text") or em.get("snippet") or "")[:2000], ""]
    lines += ["", "---", "This export is a plain Markdown file. It does not include your CV file; attach it yourself if InterviewOS needs it."]
    path = ctx.paths.handoffs_dir / f"{slugify(job.get('employer_name') or 'employer')}-{slugify(job['title'])}-{job_id}.md"
    write_private_file(path, "\n".join(lines).encode("utf-8"))
    db.insert("handoffs", {"application_id": app["id"], "path": str(path), "created_at": now_iso()})
    return {"path": str(path), "sent_items": len(sent), "unsent_drafts": len(unsent), "cv_known": cv is not None}
