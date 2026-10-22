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
from jobhunter.util import now, now_iso, parse_iso, sha256_text, stable_json

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


CLAIM_RULES = [
    # (name, what the candidate text claims, what a paid role fact must show, plain message)
    ("leadership", re.compile(r"\b(led|lead|leading|managed|manage|managing|mentored|headed|supervised)\s+(?:[\w-]+\s+){0,3}?(team|teams|people|engineers|developers|department|squad|staff)\b|\b(team lead|tech lead|engineering manager)\b", re.I), re.compile(r"\b(lead|led|leading|manag\w*|mentor\w*|head of|team lead|tech lead|supervis\w*)\b", re.I), "says you led or managed people, but your paid roles do not show that"),
    ("ownership", re.compile(r"\b(i|we)\s+(?:[\w-]+\s+){0,2}?(owned|own|was responsible for|were responsible for|architected|operated|operate|ran|run|maintained|administered)\b", re.I), re.compile(r"\b(own\w*|responsib\w*|operat\w*|architect\w*|maintain\w*|administ\w*|manag\w*|run|ran|built|engineer\w*|sre|devops|platform|infrastructure|sysadmin|administrator)\b", re.I), "says you owned or ran something at work, but your paid roles do not show that"),
    ("production", re.compile(r"\bproduction\b|\bin prod\b", re.I), re.compile(r"\bproduction\b|\b(sre|site reliability|devops|platform|infrastructure|systems? (administrator|engineer)|sysadmin|operations|on-call|incident)\b", re.I), "mentions production work, but your paid roles do not show it"),
    ("scale", re.compile(r"\b(at scale|large[- ]scale|enterprise[- ]scale|thousands of|hundreds of|millions of)\b", re.I), re.compile(r"\b(scale|thousands|hundreds|millions)\b", re.I), "claims scale that your facts do not show"),
]
IDENTITY_OK = re.compile(r"\b\d+(?:[.,]\d+)?\b")


def validate_text(text: str, fact_ids: list[str], facts: list[Fact], job_text: str = "", identity_text: str | None = None) -> list[str]:
    """Return problems: unknown fact ids, numbers, credentials, or responsibility claims without candidate evidence.

    The job text never supports a candidate claim: an employer's "100 clusters" is theirs, not the person's.
    Only numbers that are part of the job's identity (title, employer name) are allowed from the job side.
    Leadership, ownership, production and scale claims need a paid role that shows them; projects do not count.
    """
    problems = []
    known = {f.id: f for f in facts}
    for fid in fact_ids:
        if fid not in known:
            problems.append(f"unknown fact id {fid}")
    cited = [known[f] for f in fact_ids if f in known]
    pool = cited or list(facts)
    all_fact_text = " ".join(f.text.lower() for f in facts)
    identity_numbers = set(IDENTITY_OK.findall(identity_text or ""))
    fact_numbers = set(IDENTITY_OK.findall(all_fact_text))
    for num in IDENTITY_OK.findall(text):
        if num not in fact_numbers and num not in identity_numbers:
            problems.append(f"number '{num}' is not in your facts (it may come from the job text)")
    for cert in ("cka", "ckad", "cks", "aws certified", "azure certified", "terraform associate", "rhcsa", "rhce", "lpic"):
        if cert in text.lower() and cert not in all_fact_text:
            problems.append(f"'{cert}' is not in your approved facts")
    if re.search(r"\bcertified\b", text, re.I) and not any(f.category == "certification" for f in facts):
        problems.append("says 'certified', but your facts list no certification")
    for word in ("years of experience", "years' experience"):
        if word in text.lower():
            m = re.search(r"(\d+)\+?\s+years", text.lower())
            if m and m.group(1) not in all_fact_text:
                problems.append("a years-of-experience claim is not supported by your facts")
    paid = [f for f in pool if f.category == "role" and f.experience_kind == "professional"]
    paid_text = " ".join(f.text.lower() for f in paid)
    for _name, claim_rx, support_rx, message in CLAIM_RULES:
        if claim_rx.search(text) and not (paid and support_rx.search(paid_text)):
            problems.append(message)
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


PACKAGE_POLICY_VERSION = "2026.10-2"


def _profile_fingerprint(profile: dict) -> str:
    facts = [f.model_dump(mode="json") for f in profile["facts"]]
    return sha256_text(stable_json({"id": profile["id"], "facts": facts, "targets": profile.get("targets")}))


def package_inputs_hash(job: dict, profile: dict, cv: dict | None) -> str:
    """Everything a package depends on. Same hash = same package; a change marks older packages outdated."""
    return sha256_text(
        stable_json(
            {
                "job": {k: job.get(k) for k in ("description_hash", "title", "employer_name", "apply_url", "direct_url", "location_text", "salary_text", "remote_restriction")},
                "profile": _profile_fingerprint(profile),
                "cv": cv["sha256"] if cv else None,
                "policy": PACKAGE_POLICY_VERSION,
            }
        )
    )


def selected_cv(db: Database, job_id: int) -> dict | None:
    """The CV presentation for this application: the one chosen for this job, else the default. The choice is
    per job only; changing the default never changes an application that chose on its own."""
    app = db.fetchone("SELECT cv_variant_key, cv_choice FROM applications WHERE job_id = ?", (job_id,))
    chosen = (app or {}).get("cv_variant_key")
    if chosen:
        try:
            v = docs.get_cv(db, chosen, with_revisions=False)
            if not v["archived"]:
                v["choice"] = (app or {}).get("cv_choice") or "student"
                return v
        except docs.DocumentError:
            pass
    v = docs.default_cv(db)
    if v:
        v["choice"] = "default"
    return v


def select_cv(db: Database, job_id: int, variant_key: str, choice: str = "student") -> dict:
    """Record which CV to use for this job only. Nothing is prepared or sent; the next prepare uses it."""
    job = db.fetchone("SELECT * FROM jobs WHERE id = ?", (job_id,))
    if not job:
        raise DraftError(f"Job {job_id} not found.")
    v = docs.get_cv(db, variant_key, with_revisions=False)
    if v["archived"]:
        raise DraftError(f"The CV '{v['label']}' is archived. Bring it back first.")
    ts = now_iso()
    with db.tx():
        app = db.fetchone("SELECT * FROM applications WHERE job_id = ?", (job_id,))
        if not app:
            app_id = db.insert("applications", {"job_id": job_id, "stage": "prepared", "stage_changed_at": ts, "created_at": ts, "updated_at": ts})
            app = db.fetchone("SELECT * FROM applications WHERE id = ?", (app_id,))
        db.update("applications", {"cv_variant_key": variant_key, "cv_choice": choice if choice in {"default", "suggested", "student"} else "student", "updated_at": ts}, "id = ?", (app["id"],))
    return {"job_id": job_id, "application_id": app["id"], "cv": {"variant_key": variant_key, "label": v["label"], "version": v["version"]}, "choice": choice}


def _cv_block(db: Database, cv: dict | None) -> dict | None:
    if not cv:
        return None
    integrity = cv.get("file_available")
    return {"variant_key": cv["key"], "label": cv["label"], "revision_key": cv["revision_key"], "document_id": cv["document_id"], "filename": cv["filename"], "sha256": cv["sha256"], "version": cv["version"], "unchanged": bool(integrity), "choice": cv.get("choice") or "default"}


def _require_ready_profile(db: Database) -> tuple[dict, list[Fact]]:
    profile, facts = _facts(db)
    if db.get_setting("profile.needs_review"):
        raise DraftError("You added a new file. Please check your profile and press 'Yes, this looks right' first. Then I can help you apply.")
    return profile, facts


def current_package(db: Database, job_id: int) -> dict | None:
    row = db.fetchone("SELECT * FROM drafts WHERE job_id = ? AND kind = 'package' AND status = 'draft' ORDER BY id DESC LIMIT 1", (job_id,))
    if not row:
        row = db.fetchone("SELECT * FROM drafts WHERE job_id = ? AND kind = 'package' ORDER BY id DESC LIMIT 1", (job_id,))
    if not row:
        return None
    content = json.loads(row["content_json"])
    content["draft_id"] = row["id"]
    content["application_id"] = row.get("application_id")
    content["status"] = row["status"]
    content["inputs_hash"] = row.get("inputs_hash")
    content["created_at"] = row["created_at"]
    return content


def package_is_outdated(db: Database, job: dict, package: dict | None) -> bool:
    """True when the job text, the approved facts, or the CV changed since this package was made."""
    if not package:
        return False
    try:
        profile, _ = _facts(db)
    except DraftError:
        return False
    cv = selected_cv(db, job["id"])
    return package.get("inputs_hash") != package_inputs_hash(job, profile, cv)


def prepare(ctx: AppContext, db: Database, job_id: int, use_model: bool = False, regenerate: bool = False, cv_variant_key: str | None = None) -> dict:
    """Build the application package: unchanged CV, links, answers, checklist.

    Idempotent: the same job, approved facts, selected CV version and policy give back the same package.
    When any of those changed, older packages are marked outdated and a new version is made.
    Repeated or simultaneous clicks never create duplicates (unique index + one write transaction).
    `cv_variant_key` chooses the CV presentation for this job only (the default is used otherwise).
    """
    job = db.fetchone("SELECT * FROM jobs WHERE id = ?", (job_id,))
    if not job:
        raise DraftError(f"Job {job_id} not found.")
    profile, facts = _require_ready_profile(db)
    if cv_variant_key:
        select_cv(db, job_id, cv_variant_key)
    cv = selected_cv(db, job_id)
    inputs_hash = package_inputs_hash(job, profile, cv)
    existing = db.fetchone("SELECT * FROM drafts WHERE job_id = ? AND kind = 'package' AND status = 'draft' ORDER BY id DESC LIMIT 1", (job_id,))
    if existing and existing.get("inputs_hash") == inputs_hash and not regenerate:
        return _package_result(existing, reused=True)

    suggestion = docs.suggest_cv(db, job)
    if suggestion and suggestion["variant_key"] == cv["key"]:
        suggestion = None
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
        "cv": _cv_block(db, cv),
        "cv_suggestion": suggestion,
        "inputs": {"job": job.get("description_hash"), "profile": _profile_fingerprint(profile), "cv": cv["sha256"] if cv else None, "policy": PACKAGE_POLICY_VERSION},
        "project_links": links,
        "draft": draft.model_dump(mode="json"),
        "created_by": created_by,
        "validation_problems": problems,
        "profile_version_id": profile["id"],
        "reminder": "Hunter prepares, you apply. When you have sent it, press 'I applied'.",
    }
    with db.tx():
        # Re-check inside the write lock: another click may have made this exact package meanwhile.
        again = db.fetchone("SELECT * FROM drafts WHERE job_id = ? AND kind = 'package' AND status = 'draft' ORDER BY id DESC LIMIT 1", (job_id,))
        if again and again.get("inputs_hash") == inputs_hash and not regenerate:
            return _package_result(again, reused=True)
        if again and regenerate and existing and again["id"] != existing["id"] and again.get("inputs_hash") == inputs_hash:
            return _package_result(again, reused=True)  # a parallel regenerate already made the new version
        db.execute("UPDATE drafts SET status = 'outdated' WHERE job_id = ? AND kind = 'package' AND status = 'draft'", (job_id,))
        version = db.fetchone("SELECT COUNT(*) AS n FROM drafts WHERE job_id = ? AND kind = 'package'", (job_id,))["n"] + 1
        package["version"] = version
        draft_id = db.insert("drafts", {"job_id": job_id, "kind": "package", "content_json": json.dumps(package), "validation_json": json.dumps(problems), "created_by": created_by, "created_at": now_iso(), "inputs_hash": inputs_hash})
        app = actions.mark_prepared(db, job_id, cv_document_id=cv["document_id"] if cv else None)
        db.update("drafts", {"application_id": app["application_id"]}, "id = ?", (draft_id,))
    package["draft_id"] = draft_id
    package["application_id"] = app["application_id"]
    package["reused"] = False
    package["status"] = "draft"
    return package


def _package_result(row: dict, reused: bool) -> dict:
    package = json.loads(row["content_json"])
    package["draft_id"] = row["id"]
    package["application_id"] = row.get("application_id")
    package["reused"] = reused
    package["status"] = row["status"]
    return package


def validate_draft(db: Database, job: dict, draft: ApplicationDraft) -> tuple[ApplicationDraft, list[str]]:
    _, facts = _facts(db)
    job_text = (job.get("description_text") or "") + " " + (job.get("title") or "")
    identity = f"{job.get('title') or ''} {job.get('employer_name') or ''}"
    problems: list[str] = []
    kept: list[DraftAnswer] = []
    for a in draft.answers:
        if SENSITIVE.search(a.question) and a.answer.strip():
            problems.append(f"'{a.question}' must be answered by you; the draft answer was removed.")
            a.answer = ""
            a.needs_user = True
        issues = validate_text(a.answer, a.fact_ids, facts, job_text, identity_text=identity) if a.answer else []
        if issues:
            problems.append(f"'{a.question[:40]}': " + "; ".join(issues))
            a.note = ((a.note or "") + " Removed an unsupported claim; please rewrite.").strip()
            a.answer = ""
            a.needs_user = True
        kept.append(a)
    draft.answers = kept
    if draft.intro:
        issues = validate_text(draft.intro, draft.intro_fact_ids, facts, job_text, identity_text=identity)
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
    problems = validate_text(draft.body, draft.fact_ids, facts, job.get("description_text") or "", identity_text=f"{job.get('title') or ''} {job.get('employer_name') or ''}")
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
REQ_HEADINGS = re.compile(r"^(what you('ll|’ll)? bring|what we('re|’re| are) looking for|requirements|qualifications|you have|about you|who you are|must have|you are|what you need|skills|experience|you might be a (good )?fit|you may be a (good )?fit|what we need|we expect you to have|what we expect|your profile|you bring|ideal candidate|what you('ll|’ll) need)\b", re.I)
RESP_HEADINGS = re.compile(r"^(what you('ll|’ll| will) do|responsibilities|the role|your role|you will|what you('ll|’ll) be doing|in this role|what you('ll|’ll) be responsible for|what you('ll|’ll) own|your responsibilities|the role entails|what your day will look like)\b", re.I)
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
    """Plain-language view in four parts: what they need, your proof, what to check, your next step.

    "Proof" only ever comes from the person's own facts. Missing things are "not in your profile",
    never "you have never done this".
    """
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
    need = []
    level_word = {"intern": "an intern", "junior": "someone early in their career", "mid": "someone who can work on their own", "senior": "an experienced engineer who can lead work", "staff": "a very senior engineer who sets direction"}.get(seniority, "an engineer")
    need.append(f"They want {level_word}" + (f" with about {years_required}+ years." if years_required else "."))
    if tools:
        need.append("Tools they name: " + ", ".join(tool_label(t) for t in tools[:8]) + ("." if len(tools) <= 8 else " and more."))
    need += [f"They say: {w}" for w in want[:4]]
    if doing:
        need.append("Day to day: " + "; ".join(doing[:3]) + ".")

    proof = []
    if have:
        proof.append("Your CV shows " + ", ".join(tool_label(t) for t in have[:6]) + ".")
    if roles:
        proof.append(f"Paid experience: {roles[0].text}.")
    if certs:
        proof.append("Certified: " + ", ".join(c.text for c in certs[:2]) + ".")
    if projects and (miss or not roles):
        proof.append(f"Project you can point to: {projects[0].text[:120]}.")

    check = []
    if miss:
        check.append("They mention " + ", ".join(tool_label(t) for t in miss[:6]) + ". " + ("It is" if len(miss) == 1 else "These are") + " not in your profile yet.")
    if years_required and years is not None and years_required > years + 1:
        check.append(f"They ask for about {years_required} years. Your profile shows about {years:g}.")
    if not roles:
        check.append("Your profile has no paid experience yet. Be open about that, and show your projects.")
    if job.get("remote_restriction"):
        check.append(f"Location rule: {job['remote_restriction']}.")

    if tools:
        fit_line = f"Tools they name that your CV shows: {len(have)} of {len(tools)}."
    else:
        fit_line = "They do not list specific tools. Your fit depends on your story."

    nxt = []
    if have:
        nxt.append("Lead with " + ", ".join(tool_label(t) for t in have[:3]) + ". Say where you used " + ("it" if len(have) == 1 else "them") + " and what happened.")
    for m in miss[:2]:
        needle = m.replace("argocd", "argo").split(" ")[0]
        related = next((p for p in projects if needle in p.text.lower()), None)
        if related:
            nxt.append(f"For {tool_label(m)}: mention your project '{related.text[:60]}'.")
        else:
            nxt.append(f"For {tool_label(m)}: say honestly it is not in your profile yet, and name the closest thing you have used.")
    if seniority in {"senior", "staff"} and roles:
        nxt.append("Show ownership: one example from a paid role where you were responsible for a system, not just a task.")
    if not roles and projects:
        nxt.append("Describe one project as if it were a job: what broke, what you fixed, what you learned.")
    nxt.append("Keep it short and honest. Hunter never adds things your CV does not say.")
    return {"what_they_need": need, "your_proof": proof, "what_to_check": check, "next_step": nxt, "fit_line": fit_line}


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


def save_user_answer(db: Database, draft_id: int, question: str, text: str) -> dict:
    """Keep the person's own wording for one drafted answer. Hunter's draft stays as 'suggested';
    what they wrote is what they will copy and send. Nothing is validated against facts here:
    these are the person's words, not Hunter's claims."""
    row = db.fetchone("SELECT * FROM drafts WHERE id = ? AND kind = 'package'", (draft_id,))
    if not row:
        raise DraftError("That help-to-apply package was not found.")
    content = json.loads(row["content_json"])
    edits = content.setdefault("user_answers", {})
    text = (text or "").strip()
    if text:
        edits[question] = {"text": text[:4000], "saved_at": now_iso()}
    else:
        edits.pop(question, None)
    with db.tx():
        db.update("drafts", {"content_json": json.dumps(content)}, "id = ?", (draft_id,))
    return {"draft_id": draft_id, "question": question, "saved": bool(text)}
