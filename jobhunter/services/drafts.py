"""Application preparation and drafts: answers, intro, outreach, follow-ups.

Nothing here changes the CV. Generated text must cite approved facts; numbers
and credentials that are not in the facts are rejected. Screening questions
about authorization, salary, availability, or sensitive declarations are
never guessed: they go on the checklist for the user.
"""

from __future__ import annotations

import json
import re
from datetime import timedelta

from jobhunter.config import AppContext
from jobhunter.db import Database
from jobhunter.models import ApplicationDraft, DraftAnswer, Fact, OutreachDraft
from jobhunter.services import actions
from jobhunter.services import documents as docs
from jobhunter.services import profile as profile_svc
from jobhunter.util import now, now_iso, parse_iso

SENSITIVE = re.compile(r"(work authori[sz]ation|right to work|visa|sponsorship|salary|compensation|expected pay|notice period|start date|availability|gender|ethnicity|race|disability|veteran|religion|sexual orientation|age|date of birth|criminal|background check|nationality|citizenship|marital)", re.I)
COMMON_QUESTIONS = [
    "Why are you interested in this role?",
    "Tell us about your experience with the technologies in this job.",
    "Describe a project you are proud of.",
]
SCREENING_QUESTIONS = [
    "Are you legally authorized to work in the country of this job?",
    "Will you now or in the future require visa sponsorship?",
    "What are your salary expectations?",
    "When could you start?",
]


class DraftError(Exception):
    pass


def _facts(db: Database) -> tuple[dict, list[Fact]]:
    profile = profile_svc.approved(db)
    if not profile:
        raise DraftError("Approve your profile first; drafts only use approved facts.")
    return profile, [f for f in profile["facts"] if f.status != "rejected"]


def validate_text(text: str, fact_ids: list[str], facts: list[Fact], job_text: str = "") -> list[str]:
    """Return problems: unknown fact ids, unsupported numbers or credentials."""
    problems = []
    known = {f.id: f for f in facts}
    for fid in fact_ids:
        if fid not in known:
            problems.append(f"unknown fact id {fid}")
    fact_text = " ".join(known[f].text.lower() for f in fact_ids if f in known) or " ".join(f.text.lower() for f in facts)
    allowed = (fact_text + " " + job_text.lower())
    for num in re.findall(r"\b\d+(?:[.,]\d+)?\b", text):
        if num not in allowed:
            problems.append(f"number '{num}' is not supported by your facts or the job text")
    for cert in ("cka", "ckad", "cks", "aws certified", "azure certified", "terraform associate", "rhcsa", "rhce", "lpic"):
        if cert in text.lower() and cert not in fact_text:
            problems.append(f"'{cert}' is not in your approved facts")
    for word in ("years of experience", "years' experience"):
        if word in text.lower():
            m = re.search(r"(\d+)\+?\s+years", text.lower())
            if m and m.group(1) not in fact_text:
                problems.append("a years-of-experience claim is not supported by your facts")
    return problems


def rule_based_answers(job: dict, facts: list[Fact]) -> ApplicationDraft:
    """Conservative drafts straight from facts, plus a checklist of what only the user can answer."""
    skills = [f for f in facts if f.category == "skill"]
    roles = [f for f in facts if f.category == "role" and f.experience_kind == "professional"]
    projects = [f for f in facts if f.category == "project"]
    desc = (job.get("description_text") or "").lower()
    relevant_skills = [f for f in skills if f.text.lower() in desc] or skills[:4]
    answers: list[DraftAnswer] = []
    answers.append(
        DraftAnswer(
            question=COMMON_QUESTIONS[0],
            answer=f"The role is {job['title']} at {job.get('employer_name') or 'your company'}, and it matches the direction I am building towards: infrastructure and platform work. "
            + (f"I have hands-on evidence with {', '.join(f.text for f in relevant_skills[:4])}." if relevant_skills else ""),
            fact_ids=[f.id for f in relevant_skills[:4]],
            note="Edit the first sentence so it sounds like you; mention one concrete thing about the employer if you know it.",
        )
    )
    answers.append(
        DraftAnswer(
            question=COMMON_QUESTIONS[1],
            answer=(" ".join(f"{r.text}." for r in roles[:2]) + " " if roles else "")
            + (f"Hands-on tools with evidence: {', '.join(f.text for f in relevant_skills[:6])}." if relevant_skills else "")
            + ("" if roles else " My experience so far comes from projects rather than paid roles, and I say so openly."),
            fact_ids=[f.id for f in roles[:2]] + [f.id for f in relevant_skills[:6]],
        )
    )
    if projects:
        p = projects[0]
        answers.append(
            DraftAnswer(
                question=COMMON_QUESTIONS[2],
                answer=f"{p.text} ({'guided project' if p.experience_kind == 'guided' else p.experience_kind + ' project'}).",
                fact_ids=[p.id],
                note="Add what you built, what broke, and what you learned. Keep it honest about the project's size.",
            )
        )
    for q in SCREENING_QUESTIONS:
        answers.append(DraftAnswer(question=q, answer="", fact_ids=[], needs_user=True, note="Only you can answer this one."))
    missing = ["Questions about gender, background, or disability: answer them yourself or skip them. Hunter never does."]
    if job.get("remote_restriction"):
        missing.append(f"Check the location rule: {job['remote_restriction']}.")
    return ApplicationDraft(answers=answers, intro=None, missing=missing)


def prepare(ctx: AppContext, db: Database, job_id: int, use_model: bool = False) -> dict:
    """Build the application package: unchanged CV, links, answers, checklist."""
    job = db.fetchone("SELECT * FROM jobs WHERE id = ?", (job_id,))
    if not job:
        raise DraftError(f"Job {job_id} not found.")
    profile, facts = _facts(db)
    cv = docs.current_document(db, "cv")
    integrity = [r for r in docs.verify_integrity(db) if cv and r["id"] == cv["id"]]
    links = [e.ref for f in facts if f.category == "project" for e in f.evidence if e.type in {"repository", "link"}]
    draft = rule_based_answers(job, facts)
    created_by = "rules"
    if use_model:
        from jobhunter.services.model import provider as model_provider

        prov = model_provider.select_provider(ctx, db)
        if prov is not None:
            payload = {
                "job": {"title": job["title"], "employer": job.get("employer_name"), "description": (job.get("description_text") or "")[:2500]},
                "candidate": {"summary": profile["summary_text"], "facts": [{"id": f.id, "category": f.category, "kind": f.experience_kind, "text": f.text[:200]} for f in facts][:80]},
                "questions": COMMON_QUESTIONS + SCREENING_QUESTIONS,
            }
            try:
                model_draft = prov.draft(payload)
                ok, _ = validate_draft(db, job, model_draft)
                if ok.answers:
                    draft = ok
                    created_by = "claude"
            except model_provider.ModelError:
                created_by = "rules (Claude unavailable)"
    _, problems = validate_draft(db, job, draft)
    fit = fit_report(job, facts, years=float(profile["targets"].get("professional_years_relevant") or 0))
    package = {
        "fit": fit,
        "job": {"id": job["id"], "title": job["title"], "employer": job.get("employer_name"), "apply_url": job.get("apply_url") or job.get("direct_url") or job.get("discovery_url")},
        "cv": {"document_id": cv["id"], "path": cv["stored_path"], "filename": cv["original_filename"], "sha256": cv["sha256"], "version": cv["version"], "unchanged": bool(integrity and integrity[0]["ok"])} if cv else None,
        "project_links": links,
        "draft": draft.model_dump(mode="json"),
        "created_by": created_by,
        "validation_problems": problems,
        "reminder": "Hunter prepares, you apply. When you have sent it, press 'I applied'.",
    }
    with db.tx():
        draft_id = db.insert("drafts", {"job_id": job_id, "kind": "package", "content_json": json.dumps(package), "validation_json": json.dumps(problems), "created_by": created_by, "created_at": now_iso()})
        app = actions.mark_prepared(db, job_id, cv_document_id=cv["id"] if cv else None)
        db.update("drafts", {"application_id": app["application_id"]}, "id = ?", (draft_id,))
    package["draft_id"] = draft_id
    package["application_id"] = app["application_id"]
    return package


def validate_draft(db: Database, job: dict, draft: ApplicationDraft) -> tuple[ApplicationDraft, list[str]]:
    _, facts = _facts(db)
    job_text = (job.get("description_text") or "") + " " + (job.get("title") or "")
    problems: list[str] = []
    kept: list[DraftAnswer] = []
    for a in draft.answers:
        if SENSITIVE.search(a.question) and a.answer.strip():
            problems.append(f"'{a.question}' must be answered by you; the draft answer was removed.")
            a.answer = ""
            a.needs_user = True
        issues = validate_text(a.answer, a.fact_ids, facts, job_text) if a.answer else []
        if issues:
            problems.append(f"'{a.question[:40]}': " + "; ".join(issues))
            a.note = ((a.note or "") + " Removed an unsupported claim; please rewrite.").strip()
            a.answer = ""
            a.needs_user = True
        kept.append(a)
    draft.answers = kept
    if draft.intro:
        issues = validate_text(draft.intro, draft.intro_fact_ids, facts, job_text)
        if issues:
            problems.append("intro: " + "; ".join(issues))
            draft.intro = None
    return draft, problems


def outreach(db: Database, job_id: int, contact_name: str | None, purpose: str = "outreach") -> dict:
    """Draft a short message for the user to review and send themselves."""
    job = db.fetchone("SELECT * FROM jobs WHERE id = ?", (job_id,))
    if not job:
        raise DraftError(f"Job {job_id} not found.")
    _, facts = _facts(db)
    skills = [f for f in facts if f.category == "skill"][:3]
    greeting = f"Hi {contact_name}," if contact_name else "Hello,"
    if purpose == "followup":
        app = db.fetchone("SELECT * FROM applications WHERE job_id = ?", (job_id,))
        when = (app or {}).get("applied_at") or ""
        body = (
            f"{greeting}\n\nI applied for the {job['title']} role at {job.get('employer_name') or 'your company'}"
            + (f" on {when[:10]}" if when else "")
            + ". I remain very interested and wanted to ask whether there is anything else you need from me.\n\n"
            + (f"In short: hands-on with {', '.join(f.text for f in skills)}." if skills else "")
            + "\n\nThank you for your time.\n\n[Your name]"
        )
        subject = f"Following up on my application: {job['title']}"
    else:
        body = (
            f"{greeting}\n\nI saw the {job['title']} opening at {job.get('employer_name') or 'your company'} and I am applying. "
            + (f"My hands-on experience includes {', '.join(f.text for f in skills)}. " if skills else "")
            + "I would appreciate any guidance on the process or the team.\n\nThank you,\n[Your name]"
        )
        subject = f"Question about the {job['title']} role"
    draft = OutreachDraft(subject=subject, body=body, fact_ids=[f.id for f in skills])
    problems = validate_text(draft.body, draft.fact_ids, facts, job.get("description_text") or "")
    with db.tx():
        app = db.fetchone("SELECT id FROM applications WHERE job_id = ?", (job_id,))
        did = db.insert("drafts", {"job_id": job_id, "application_id": app["id"] if app else None, "kind": purpose, "content_json": draft.model_dump_json(), "validation_json": json.dumps(problems), "created_by": "rules", "created_at": now_iso()})
    return {"draft_id": did, **draft.model_dump(mode="json"), "problems": problems, "note": "Review, edit, and send this yourself. Then tell Hunter: `jobhunter drafts sent <draft_id>`."}


def mark_sent(db: Database, draft_id: int) -> dict:
    d = db.fetchone("SELECT * FROM drafts WHERE id = ?", (draft_id,))
    if not d:
        raise DraftError(f"Draft {draft_id} not found.")
    with db.tx():
        db.update("drafts", {"status": "sent_confirmed", "sent_confirmed_at": now_iso()}, "id = ?", (draft_id,))
        if d["kind"] in {"outreach", "followup"} and d["job_id"]:
            actions.mark_contacted(db, d["job_id"], who=json.loads(d["content_json"]).get("subject", ""), channel=d["kind"])
    return {"draft_id": draft_id, "status": "sent_confirmed"}


def list_drafts(db: Database, job_id: int | None = None) -> list[dict]:
    rows = db.fetchall("SELECT * FROM drafts WHERE (? IS NULL OR job_id = ?) ORDER BY id DESC", (job_id, job_id))
    for r in rows:
        r["content"] = json.loads(r["content_json"])
    return rows


# ---------------------------------------------------------------------------
# Who they want / how you fit / how to present yourself
# ---------------------------------------------------------------------------
REQ_HEADINGS = re.compile(r"^(what you('ll)? bring|what we('re| are) looking for|requirements|qualifications|you have|about you|who you are|must have|you are|what you need|skills|experience)\b", re.I)
RESP_HEADINGS = re.compile(r"^(what you('ll| will) do|responsibilities|the role|your role|you will|what you'll be doing|in this role)\b", re.I)
SKIP_LINE = re.compile(r"^(we offer|benefits|perks|about us|our company|equal opportunity|diversity)", re.I)


def _lines(desc: str) -> list[str]:
    out = []
    for raw in (desc or "").splitlines():
        ln = raw.strip(" •-*–·\t")
        if 12 <= len(ln) <= 220:
            out.append(ln)
    return out


def _section(desc: str, heading_rx, limit: int = 6) -> list[str]:
    lines = _lines(desc)
    out: list[str] = []
    active = False
    for ln in lines:
        if heading_rx.match(ln):
            active = True
            continue
        if active and (REQ_HEADINGS.match(ln) or RESP_HEADINGS.match(ln) or SKIP_LINE.match(ln)):
            if out:
                break
            active = True
            continue
        if active:
            out.append(ln)
            if len(out) >= limit:
                break
    return out


def fit_report(job: dict, facts: list[Fact], years: float | None = None) -> dict:
    """Plain-language view: who they want, what you match, what you miss, how to present yourself."""
    from jobhunter.services.matching.scoring import candidate_skills, job_tools, tool_label
    from jobhunter.services.normalize import seniority_of

    desc = job.get("description_text") or ""
    title = job.get("title") or ""
    skills = candidate_skills(facts)
    tools = sorted(job_tools(desc, title))
    have = [t for t in tools if t in skills]
    miss = [t for t in tools if t not in skills]
    seniority, years_required = seniority_of(title, desc)
    roles = [f for f in facts if f.category == "role" and f.experience_kind == "professional"]
    projects = [f for f in facts if f.category == "project"]
    certs = [f for f in facts if f.category == "certification"]

    want = _section(desc, REQ_HEADINGS, 6)
    doing = _section(desc, RESP_HEADINGS, 4)
    who = []
    level_word = {"intern": "an intern", "junior": "someone early in their career", "mid": "someone who can work on their own", "senior": "an experienced engineer who can lead work", "staff": "a very senior engineer who sets direction"}.get(seniority, "an engineer")
    who.append(f"They want {level_word}" + (f" with about {years_required}+ years." if years_required else "."))
    if tools:
        who.append("Tools they name: " + ", ".join(tool_label(t) for t in tools[:8]) + ("." if len(tools) <= 8 else " and more."))
    who += [f"They say: {w}" for w in want[:4]]
    if doing:
        who.append("Day to day: " + "; ".join(doing[:3]) + ".")

    match_lines = []
    if have:
        match_lines.append("Your CV shows " + ", ".join(tool_label(t) for t in have[:6]) + ".")
    if roles:
        match_lines.append(f"You have paid experience: {roles[0].text}.")
    if certs:
        match_lines.append("Certified: " + ", ".join(c.text for c in certs[:2]) + ".")
    if projects and (miss or not roles):
        match_lines.append(f"Project work you can point to: {projects[0].text[:120]}.")

    miss_lines = []
    if miss:
        miss_lines.append("They mention " + ", ".join(tool_label(t) for t in miss[:6]) + ". Your CV does not show " + ("it" if len(miss) == 1 else "these") + ".")
    if years_required and years is not None and years_required > years + 1:
        miss_lines.append(f"They ask for about {years_required} years. You have about {years:g}.")
    if not roles:
        miss_lines.append("You have no paid experience yet. Be open about that, and show your projects.")

    total = len(tools) + (1 if years_required else 0)
    got = len(have) + (1 if (years_required and years is not None and years >= years_required - 1) else 0)
    if total:
        fit_line = f"You match {got} of {total} things they ask for."
    else:
        fit_line = "They do not list many specific tools. Your fit depends on your story."

    present = []
    if have:
        present.append("Lead with " + ", ".join(tool_label(t) for t in have[:3]) + ". Say where you used " + ("it" if len(have) == 1 else "them") + " and what happened.")
    for m in miss[:2]:
        needle = m.replace("argocd", "argo").split(" ")[0]
        related = next((p for p in projects if needle in p.text.lower()), None)
        if related:
            present.append(f"For {tool_label(m)}: mention your project '{related.text[:60]}'.")
        else:
            present.append(f"For {tool_label(m)}: say honestly you have not used it at work, and name the closest thing you have used.")
    if seniority in {"senior", "staff"} and roles:
        present.append("Show ownership: one example where you were responsible for a system, not just a task.")
    if not roles and projects:
        present.append("Describe one project as if it were a job: what broke, what you fixed, what you learned.")
    present.append("Keep it short and honest. Hunter never adds things your CV does not say.")
    return {"who_they_want": who, "you_match": match_lines, "you_miss": miss_lines, "fit_line": fit_line, "how_to_present": present}


# ---------------------------------------------------------------------------
# Follow-ups
# ---------------------------------------------------------------------------
def schedule_followup(db: Database, job_id: int, days: int = 7) -> dict:
    app = db.fetchone("SELECT * FROM applications WHERE job_id = ?", (job_id,))
    if not app or not app.get("applied_at"):
        raise DraftError("Follow-ups are only for jobs you told me you applied to.")
    if app["stage"] in actions.FINISHED_STAGES or app["stage"] in {"replied", "interview"}:
        raise DraftError(f"No follow-up needed: the application is at stage '{app['stage']}'.")
    due = (parse_iso(app["applied_at"]) or now()) + timedelta(days=days)
    if due < now():
        due = now()
    with db.tx():
        fid = db.insert("followups", {"application_id": app["id"], "due_at": due.isoformat(), "created_at": now_iso()})
    return {"followup_id": fid, "due_at": due.isoformat(), "note": "Hunter will remind you and draft the message; it never sends it."}


def due_followups(db: Database) -> list[dict]:
    rows = db.fetchall(
        "SELECT f.*, a.job_id, a.stage, j.title, j.employer_name FROM followups f JOIN applications a ON a.id = f.application_id JOIN jobs j ON j.id = a.job_id "
        "WHERE f.status = 'pending' AND f.due_at <= ? ORDER BY f.due_at",
        (now_iso(),),
    )
    return [r for r in rows if r["stage"] not in actions.FINISHED_STAGES and r["stage"] not in {"replied", "interview"}]


def resolve_followup(db: Database, followup_id: int, status: str = "done") -> None:
    db.update("followups", {"status": status, "resolved_at": now_iso()}, "id = ?", (followup_id,))
