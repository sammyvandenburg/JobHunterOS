"""The application workspace behind one job: what the dashboard asks Hunter for, what Hunter prepared, and what
the person decided. Everything here is deterministic code; the model (a Claude session or a provider) only
proposes research text and drafts through validated structured input (`research submit`).

Three parts:
  requests     the page writes `requests.<kind>` into its action document; Hunter records each request once
               (by its id), runs it, and writes the state back to the page (requested, running, ready,
               needs_input, failed) in the job's `work/<key>` document.
  package view the company/role brief with sources, the vacancy and application route, "your strongest case"
               (profile supports a claim vs. the selected CV communicates it), the answers with the person's
               own wording, optional introduction and cover letter with the facts they use, the checklist.
  page edits   CV choice, edited answers and drafts, "I applied" with which CV, contacts, follow-ups.

Nothing here submits anything, sends anything, rewrites a CV, or invents a fact."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import timedelta
from urllib.parse import urlparse

from jobhunter.config import AppContext
from jobhunter.db import Database
from jobhunter.models import Fact
from jobhunter.services import actions, notes
from jobhunter.services import documents as docs
from jobhunter.services import drafts as drafts_svc
from jobhunter.services import profile as profile_svc
from jobhunter.util import now, now_iso, parse_iso, truncate

REQUEST_KINDS = ("prepare", "research", "contact", "message", "intro", "letter", "followup_draft", "handoff", "cv_file")
STATES = ("requested", "running", "ready", "needs_input", "failed")
MAX_ANSWER_CHARS = 4000
MAX_QUESTION_CHARS = 300
MAX_QUESTIONS = 12
MAX_CONTACT_FIELD = 120
WORK_SCHEMA = "hunter-work/1"
CONTACT_TYPES = {"listed": "Listed contact", "team": "Possible team contact", "connection": "Your connection"}


class WorkspaceError(Exception):
    pass


def answer_id(question: str) -> str:
    return "a" + hashlib.sha1((question or "").strip().lower().encode("utf-8")).hexdigest()[:8]


# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------
def _clean_request_id(value) -> str:
    rid = re.sub(r"[^A-Za-z0-9:._-]", "", str(value or ""))[:80]
    if not rid:
        raise WorkspaceError("A request needs an id.")
    return rid


def request(db: Database, job_id: int | None, kind: str, params: dict | None = None, request_id: str | None = None, job_key: str | None = None) -> dict:
    """Record one request once. The same id again returns the existing row (the page may write it twice)."""
    if kind not in REQUEST_KINDS:
        raise WorkspaceError(f"Unknown request kind '{kind}'.")
    rid = _clean_request_id(request_id or f"{kind}:{now_iso()}")
    row = db.fetchone("SELECT * FROM requests WHERE request_id = ?", (rid,))
    if row:
        return _request_view(row)
    with db.tx():
        db.insert("requests", {"request_id": rid, "job_id": job_id, "job_key": job_key, "kind": kind, "params_json": json.dumps(_clean_params(kind, params or {})), "state": "requested", "requested_at": now_iso()})
    return _request_view(db.fetchone("SELECT * FROM requests WHERE request_id = ?", (rid,)))


def _clean_params(kind: str, params: dict) -> dict:
    """Only the fields a kind uses, bounded. Page input is untrusted."""
    out: dict = {}
    if not isinstance(params, dict):
        return out
    if kind in {"prepare", "research"} and isinstance(params.get("questions"), list):
        qs = []
        for q in params["questions"][:MAX_QUESTIONS]:
            if isinstance(q, dict):
                text = str(q.get("text") or "").strip()[:MAX_QUESTION_CHARS]
                limit = q.get("limit")
                try:
                    limit = int(limit) if limit not in (None, "") else None
                except (TypeError, ValueError):
                    limit = None
            else:
                text, limit = str(q or "").strip()[:MAX_QUESTION_CHARS], None
            if text:
                qs.append({"text": text, "limit": limit if limit and 0 < limit <= 20000 else None})
        out["questions"] = qs
    if kind in {"prepare"} and params.get("cv"):
        out["cv"] = str(params["cv"])[:40]
    if kind == "contact":
        for k in ("name", "role", "url", "note"):
            if params.get(k):
                out[k] = str(params[k]).strip()[:MAX_CONTACT_FIELD if k != "url" else 400]
    if kind == "followup_draft" and params.get("contact"):
        out["contact"] = str(params["contact"]).strip()[:MAX_CONTACT_FIELD]
    if kind == "message" and params.get("contact") is not None:
        try:
            out["contact"] = int(params["contact"])
        except (TypeError, ValueError):
            pass
    if kind == "cv_file" and params.get("cv"):
        out["cv"] = str(params["cv"])[:40]
    return out


def _request_view(row: dict) -> dict:
    return {"request_id": row["request_id"], "job_id": row.get("job_id"), "job_key": row.get("job_key"), "kind": row["kind"], "state": row["state"], "message": row.get("message"), "params": json.loads(row.get("params_json") or "{}"), "result": json.loads(row["result_json"]) if row.get("result_json") else None, "requested_at": row["requested_at"], "started_at": row.get("started_at"), "finished_at": row.get("finished_at")}


def list_requests(db: Database, state: str | None = "requested", job_id: int | None = None) -> list[dict]:
    sql, params = "SELECT * FROM requests", []
    clauses = []
    if state:
        clauses.append("state = ?")
        params.append(state)
    if job_id:
        clauses.append("job_id = ?")
        params.append(job_id)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    return [_request_view(r) for r in db.fetchall(sql + " ORDER BY requested_at, id", tuple(params))]


def _finish(db: Database, rid: str, state: str, message: str | None, result: dict | None = None) -> dict:
    with db.tx():
        db.update("requests", {"state": state, "message": message, "result_json": json.dumps(result) if result is not None else None, "finished_at": now_iso()}, "request_id = ?", (rid,))
    return _request_view(db.fetchone("SELECT * FROM requests WHERE request_id = ?", (rid,)))


def run_request(ctx: AppContext, db: Database, request_id: str, use_model: bool = False) -> dict:
    """Do the work one request asks for. Every outcome is recorded: ready, needs_input (what the person must
    add), or failed (what happened). A request is never left silently in 'requested'."""
    row = db.fetchone("SELECT * FROM requests WHERE request_id = ?", (request_id,))
    if not row:
        raise WorkspaceError(f"Request {request_id} not found.")
    if row["state"] in {"ready", "failed", "needs_input"}:
        return _request_view(row)
    rid = row["request_id"]
    params = json.loads(row.get("params_json") or "{}")
    with db.tx():
        db.update("requests", {"state": "running", "started_at": now_iso()}, "request_id = ?", (rid,))
    job = db.fetchone("SELECT * FROM jobs WHERE id = ?", (row["job_id"],)) if row.get("job_id") else None
    kind = row["kind"]
    try:
        if kind != "cv_file" and not job:
            return _finish(db, rid, "failed", "Hunter does not have this job on this computer yet. It is applied after the next search finds it.")
        if kind == "prepare":
            if params.get("cv"):
                drafts_svc.select_cv(db, job["id"], params["cv"])
            pkg = drafts_svc.prepare(ctx, db, job["id"], use_model=use_model)
            if params.get("questions"):
                add_questions(db, job["id"], params["questions"])
            ensure_research(db, job)
            return _finish(db, rid, "ready", "Your application is prepared." if not pkg.get("reused") else "Nothing changed since last time; the same preparation is shown.", {"draft_id": pkg["draft_id"], "version": pkg.get("version")})
        if kind == "research":
            r = ensure_research(db, job, force=True)
            return _finish(db, rid, "ready", "Company and role brief refreshed.", {"version": r["version"]})
        if kind == "contact":
            res = find_contact(db, job, params)
            state = "ready" if res["contacts"] else "needs_input"
            return _finish(db, rid, state, res["message"], {"contacts": [c["id"] for c in res["contacts"]]})
        if kind in {"intro", "letter"}:
            res = write_optional_text(db, job, kind)
            return _finish(db, rid, "ready", f"Your {'short introduction' if kind == 'intro' else 'cover letter'} is drafted. Read it, change it, and only use what is true.", {"version": res["version"]})
        if kind == "message":
            if params.get("contact") is None:
                return _finish(db, rid, "needs_input", "Which contact? Pick one under People & notes first.")
            d = contact_message(db, job["id"], int(params["contact"]))
            return _finish(db, rid, "ready", "Message drafted. Read it, change it, and send it yourself if you want to.", {"draft_id": d["draft_id"], "contact": int(params["contact"])})
        if kind == "followup_draft":
            d = drafts_svc.outreach(db, job["id"], params.get("contact"), purpose="followup")
            return _finish(db, rid, "ready", "Follow-up message drafted. Hunter never sends it.", {"draft_id": d["draft_id"]})
        if kind == "handoff":
            from jobhunter.services import interviewos

            try:
                res = interviewos.export(ctx, db, job["id"])
            except interviewos.HandoffError as exc:
                return _finish(db, rid, "needs_input", str(exc))
            return _finish(db, rid, "ready", "Interview hand-off written.", {"path": res["path"], "sent_items": res["sent_items"], "unsent_drafts": res["unsent_drafts"]})
        if kind == "cv_file":
            try:
                f = docs.cv_file(db, params.get("cv") or (docs.default_cv(db) or {}).get("key") or "")
            except docs.DocumentError as exc:
                return _finish(db, rid, "needs_input", str(exc))
            f.pop("data", None)
            return _finish(db, rid, "ready", f"The file {f['filename']} ({f['bytes']} bytes) is on this computer, unchanged (hash checked). Hunter can hand it to you from here.", f)
    except drafts_svc.DraftError as exc:
        return _finish(db, rid, "needs_input", str(exc))
    except (docs.DocumentError, actions.ActionError, WorkspaceError) as exc:
        return _finish(db, rid, "failed", str(exc))
    return _finish(db, rid, "failed", f"Hunter does not know how to do '{kind}'.")


def run_pending(ctx: AppContext, db: Database, use_model: bool = False) -> list[dict]:
    return [run_request(ctx, db, r["request_id"], use_model=use_model) for r in list_requests(db, "requested")]


# ---------------------------------------------------------------------------
# Research: the company and role brief, with sources
# ---------------------------------------------------------------------------
ABOUT_HEADINGS = re.compile(r"^(about (us|the company|[A-Z][\w&. -]{1,40})|who we are|our (mission|company|story)|the company)\b", re.I)
REQ_ID_RX = re.compile(r"\b(req(?:uisition)?\s*(?:id|#|no\.?)?\s*[:#]?\s*([A-Z0-9-]{4,20}))\b", re.I)


def _domain(url: str | None) -> str | None:
    try:
        host = urlparse(url or "").hostname or ""
    except ValueError:
        return None
    host = host.lower().removeprefix("www.")
    for ats in ("greenhouse.io", "lever.co", "ashbyhq.com", "workable.com", "myworkdayjobs.com", "smartrecruiters.com", "recruitee.com", "personio.de", "bamboohr.com", "remotive.com", "arbeitnow.com", "adzuna"):
        if ats in host:
            return None
    return host or None


def research_inputs_hash(job: dict) -> str:
    from jobhunter.util import sha256_text, stable_json

    return sha256_text(stable_json({k: job.get(k) for k in ("description_hash", "title", "employer_name", "direct_url", "apply_url", "discovery_url", "last_checked_at")}))


def rule_based_research(db: Database, job: dict) -> dict:
    """What can be said from the posting alone, in three labelled tiers. Nothing is fetched here; a Claude
    session may add a richer version through `research submit`, with sources it really read."""
    desc = job.get("description_text") or ""
    vacancy = drafts_svc._section(desc, drafts_svc.REQ_HEADINGS, 5)
    doing = drafts_svc._section(desc, drafts_svc.RESP_HEADINGS, 3)
    company = drafts_svc._section(desc, ABOUT_HEADINGS, 3)
    if not company:
        first = next((ln for ln in drafts_svc._lines(desc)[:3] if not drafts_svc.REQ_HEADINGS.match(ln)), None)
        company = [first] if first else []
    checked = job.get("last_checked_at") or job.get("last_seen_at") or job.get("first_seen_at")
    url = job.get("direct_url") if job.get("direct_url_status") == "verified" else (job.get("apply_url") or job.get("discovery_url"))
    sources = [{"label": "The job posting" + (" (employer site)" if job.get("direct_url_status") == "verified" else ""), "url": url, "checked_at": checked, "certainty": "read by Hunter"}]
    domain = _domain(job.get("direct_url")) or _domain(job.get("apply_url"))
    interp = []
    try:
        prof = profile_svc.approved(db)
        facts = [f for f in prof["facts"] if f.status != "rejected"] if prof else []
        fit = drafts_svc.fit_report(job, facts, years=float((prof or {}).get("targets", {}).get("professional_years_relevant") or 0)) if prof else None
        if fit:
            interp = fit["what_they_need"][:2] + ([fit["fit_line"]] if fit.get("fit_line") else [])
    except Exception:
        interp = []
    return {
        "vacancy_says": [truncate(x, 220) for x in (vacancy + [f"Day to day: {d}" for d in doing])][:6],
        "company_describes": [truncate(x, 260) for x in company][:3] if not job.get("employer_confidential") else [],
        "interpretation": interp[:3],
        "sources": sources,
        "employer": job.get("employer_name") if not job.get("employer_confidential") else None,
        "employer_domain": domain,
        "confidential": bool(job.get("employer_confidential")),
        "uncertainty": "Only the posting was read. Company lines are the employer's own words; Hunter's reading is marked as such." if not job.get("employer_confidential") else "The employer is not named in the posting, so nothing about the company is claimed.",
    }


def ensure_research(db: Database, job: dict, force: bool = False) -> dict:
    """The current brief for this job, made once per posting version (a changed posting makes a new version)."""
    h = research_inputs_hash(job)
    cur = db.fetchone("SELECT * FROM research WHERE job_id = ? ORDER BY version DESC LIMIT 1", (job["id"],))
    if cur and cur.get("inputs_hash") == h and not force:
        return _research_view(cur)
    content = rule_based_research(db, job)
    with db.tx():
        again = db.fetchone("SELECT * FROM research WHERE job_id = ? ORDER BY version DESC LIMIT 1", (job["id"],))
        if again and again.get("inputs_hash") == h and not force:
            return _research_view(again)
        version = (again["version"] if again else 0) + 1
        rid = db.insert("research", {"job_id": job["id"], "version": version, "inputs_hash": h, "content_json": json.dumps(content), "created_by": "rules", "created_at": now_iso()})
    return _research_view(db.fetchone("SELECT * FROM research WHERE id = ?", (rid,)))


def _research_view(row: dict) -> dict:
    c = json.loads(row["content_json"])
    c.update({"version": row["version"], "created_by": row["created_by"], "created_at": row["created_at"], "inputs_hash": row.get("inputs_hash")})
    return c


def current_research(db: Database, job_id: int) -> dict | None:
    row = db.fetchone("SELECT * FROM research WHERE job_id = ? ORDER BY version DESC LIMIT 1", (job_id,))
    return _research_view(row) if row else None


def submit_research(db: Database, job_id: int, payload: dict, created_by: str = "claude") -> dict:
    """A model's brief, validated: every line short, every source a real https URL with a time, company lines
    only when the employer is named, and the three tiers kept apart. Rejected input changes nothing."""
    job = db.fetchone("SELECT * FROM jobs WHERE id = ?", (job_id,))
    if not job:
        raise WorkspaceError(f"Job {job_id} not found.")
    if not isinstance(payload, dict):
        raise WorkspaceError("The research must be a JSON object.")
    problems: list[str] = []

    def lines(key: str, limit: int) -> list[str]:
        vals = payload.get(key) or []
        if not isinstance(vals, list):
            problems.append(f"{key} must be a list of short lines")
            return []
        out = []
        for v in vals[:limit]:
            v = re.sub(r"\s+", " ", str(v)).strip()
            if 3 <= len(v) <= 300:
                out.append(v)
            else:
                problems.append(f"{key}: a line is empty or longer than 300 characters")
        return out

    sources = []
    for s in (payload.get("sources") or [])[:8]:
        if not isinstance(s, dict):
            problems.append("a source is not an object")
            continue
        url = str(s.get("url") or "")
        if not url.startswith("https://"):
            problems.append(f"source without an https URL: {truncate(url, 60) or '(empty)'}")
            continue
        sources.append({"label": truncate(str(s.get("label") or _domain(url) or url), 80), "url": url[:500], "checked_at": str(s.get("checked_at") or now_iso())[:25], "certainty": truncate(str(s.get("certainty") or "read by Claude"), 80)})
    if not sources:
        problems.append("at least one source with an https URL is required")
    base = rule_based_research(db, job)
    company = lines("company_describes", 5)
    if base["confidential"] and company:
        problems.append("the employer is confidential: no company lines are allowed")
        company = []
    dom = base["employer_domain"]
    if dom and company and not any(dom in (s["url"] or "") for s in sources):
        problems.append(f"company lines need a source on the employer's own site ({dom})")
    content = {**base, "vacancy_says": lines("vacancy_says", 6) or base["vacancy_says"], "company_describes": company or base["company_describes"], "interpretation": lines("interpretation", 4) or base["interpretation"], "sources": base["sources"] + sources, "uncertainty": truncate(str(payload.get("uncertainty") or base["uncertainty"]), 300)}
    if problems:
        return {"accepted": False, "problems": problems}
    with db.tx():
        cur = db.fetchone("SELECT version FROM research WHERE job_id = ? ORDER BY version DESC LIMIT 1", (job_id,))
        version = (cur["version"] if cur else 0) + 1
        db.insert("research", {"job_id": job_id, "version": version, "inputs_hash": research_inputs_hash(job), "content_json": json.dumps(content), "created_by": created_by, "created_at": now_iso()})
    return {"accepted": True, "version": version, "problems": []}


# ---------------------------------------------------------------------------
# The vacancy and the application route
# ---------------------------------------------------------------------------
def route_view(db: Database, job: dict) -> dict:
    obs = db.fetchall("SELECT source_id, external_id, url, last_observed_at FROM job_observations WHERE job_id = ? ORDER BY first_observed_at", (job["id"],))
    primary = job.get("direct_url") if job.get("direct_url_status") == "verified" else (job.get("apply_url") or job.get("discovery_url"))
    others = [{"source": o["source_id"].split(":")[0], "url": o["url"], "seen_at": o["last_observed_at"]} for o in obs if o.get("url") and o["url"] not in {primary, job.get("apply_url"), job.get("direct_url")}][:5]
    m = REQ_ID_RX.search(job.get("description_text") or "")
    req_id = job.get("requisition_id") or (m.group(2) if m else None) or (obs[0]["external_id"] if obs and obs[0].get("external_id") and not obs[0]["external_id"].isdigit() else None)
    availability = job.get("availability") or "open"
    label = {"open": "Listed when Hunter last checked", "closed": "No longer listed where Hunter found it", "unknown": "Hunter could not check this listing"}.get(availability, availability)
    note = None
    if availability == "closed":
        note = "The posting disappeared from the feed Hunter reads. That is not proof it is closed: open the employer's page to check."
    elif job.get("direct_url_status") != "verified" and job.get("direct_url"):
        note = "Hunter could not confirm the employer's own page; the link below is where it found the job."
    return {
        "apply_url": primary,
        "employer_url": job.get("direct_url") if job.get("direct_url_status") == "verified" else None,
        "discovery_url": job.get("discovery_url"),
        "requisition_id": req_id,
        "other_listings": others,
        "last_checked_at": job.get("last_checked_at") or job.get("last_seen_at"),
        "availability": availability,
        "availability_label": label,
        "note": note,
    }


# ---------------------------------------------------------------------------
# Your strongest case: three requirements, the evidence type, the gap
# ---------------------------------------------------------------------------
EVIDENCE_WORDS = {"paid work": "a paid role", "project work": "a project", "a guided course project": "a guided course project", "a course or certification": "a course or certification", "your skills list": "your skills list"}


def strongest_case(db: Database, job: dict, facts: list[Fact], cv_text: str) -> dict:
    """Three things they ask for, what in your profile supports each (and what kind of evidence it is), whether
    the selected CV says it in so many words, and the gap when there is one. Profile support and CV wording
    are two separate columns on purpose: the profile is the truth, the CV is one presentation of it."""
    from jobhunter.services.matching.scoring import evidence_kinds, job_tools, tool_label
    from jobhunter.services.normalize import seniority_of

    desc = job.get("description_text") or ""
    title = job.get("title") or ""
    kinds = evidence_kinds(facts)
    tools = sorted(job_tools(desc, title), key=lambda t: (t not in kinds, t))
    low_cv = (cv_text or "").lower()
    items = []
    for t in tools[:3]:
        label = tool_label(t)
        ev = kinds.get(t)
        in_cv = bool(low_cv) and (label.lower() in low_cv or t.lower() in low_cv)
        items.append({
            "requirement": label,
            "profile_supports": bool(ev),
            "evidence_type": EVIDENCE_WORDS.get(ev[0], ev[0]) if ev else None,
            "evidence": truncate(ev[1].text, 160) if ev else None,
            "cv_communicates": in_cv if ev else False,
            "gap": None if ev else f"{label} is not in your profile yet. Say so honestly and name the closest thing you have used.",
            "note": None if not ev or in_cv or not low_cv else f"Your profile shows {label}, but the selected CV does not use that word. Check the CV says it where it matters.",
        })
    seniority, years_required = seniority_of(title, desc)
    if len(items) < 3 and years_required:
        paid = [f for f in facts if f.category == "role" and f.experience_kind == "professional"]
        items.append({"requirement": f"About {years_required}+ years of experience", "profile_supports": bool(paid), "evidence_type": "a paid role" if paid else None, "evidence": truncate(paid[0].text, 160) if paid else None, "cv_communicates": bool(paid) and bool(low_cv), "gap": None if paid else "Your profile has no paid experience yet. Be open about that and show your projects.", "note": None})
    examples = []
    for f in facts:
        if f.category == "project" and len(examples) < 2:
            examples.append({"kind": "project" if f.experience_kind != "guided" else "guided course project", "text": truncate(f.text, 200)})
    for f in facts:
        if f.category == "role" and f.experience_kind == "professional" and len(examples) < 3:
            examples.append({"kind": "paid role", "text": truncate(f.text, 200)})
    return {"items": items[:3], "examples": examples[:3], "note": "Two columns on purpose: what your profile proves, and whether the CV you picked says it. Neither column is a rating."}


# ---------------------------------------------------------------------------
# Answers, questions the person pastes, and the optional introduction / cover letter
# ---------------------------------------------------------------------------
def _package_row(db: Database, job_id: int) -> dict | None:
    return db.fetchone("SELECT * FROM drafts WHERE job_id = ? AND kind = 'package' AND status = 'draft' ORDER BY id DESC LIMIT 1", (job_id,))


def add_questions(db: Database, job_id: int, questions: list[dict]) -> dict:
    """Questions the person pasted from the application form, with limits. Kept with the package; Hunter
    drafts nothing for a sensitive one and marks the rest as 'write this yourself' unless a model drafts them."""
    row = _package_row(db, job_id)
    if not row:
        raise WorkspaceError("Prepare the application first; then add the form's questions.")
    content = json.loads(row["content_json"])
    custom = content.setdefault("custom_questions", [])
    known = {answer_id(q["text"]) for q in custom} | {answer_id(a["question"]) for a in content.get("draft", {}).get("answers", [])}
    added = 0
    for q in _clean_params("prepare", {"questions": questions}).get("questions", []):
        if answer_id(q["text"]) in known or len(custom) >= MAX_QUESTIONS:
            continue
        sensitive = bool(drafts_svc.SENSITIVE.search(q["text"]))
        custom.append({"text": q["text"], "limit": q["limit"], "needs_user": True, "note": "Only you can answer this one." if sensitive else "Write this one in your own words; Hunter can suggest a draft in Claude, from your facts only."})
        known.add(answer_id(q["text"]))
        added += 1
    with db.tx():
        db.update("drafts", {"content_json": json.dumps(content)}, "id = ?", (row["id"],))
    return {"draft_id": row["id"], "added": added, "questions": len(custom)}


def save_answer(db: Database, job_id: int, aid: str, text: str, rev: int | None = None, when: str | None = None) -> dict:
    """The person's own wording for one answer, by answer id. Their text is never validated against facts (it is
    theirs), never overwritten by a regeneration, and kept with the package version it belongs to."""
    row = _package_row(db, job_id) or db.fetchone("SELECT * FROM drafts WHERE job_id = ? AND kind = 'package' ORDER BY id DESC LIMIT 1", (job_id,))
    if not row:
        raise WorkspaceError("There is no prepared application for this job yet.")
    content = json.loads(row["content_json"])
    question = next((a["question"] for a in content.get("draft", {}).get("answers", []) if answer_id(a["question"]) == aid), None)
    if question is None:
        question = next((q["text"] for q in content.get("custom_questions", []) if answer_id(q["text"]) == aid), None)
    if question is None:
        raise WorkspaceError(f"No question with id {aid} in this application.")
    edits = content.setdefault("user_answers", {})
    text = (text or "").strip()[:MAX_ANSWER_CHARS]
    prev = edits.get(question) or {}
    if text:
        edits[question] = {"text": text, "saved_at": when or now_iso(), "rev": int(rev if rev is not None else (prev.get("rev") or 0) + 1)}
    else:
        edits.pop(question, None)
    with db.tx():
        db.update("drafts", {"content_json": json.dumps(content)}, "id = ?", (row["id"],))
    return {"draft_id": row["id"], "answer_id": aid, "saved": bool(text)}


def _facts_used(text: str, facts: list[Fact]) -> list[dict]:
    low = (text or "").lower()
    used = []
    for f in facts:
        head = f.text.lower().split(",")[0].split(" (")[0].strip()
        if head and len(head) >= 3 and head in low:
            used.append({"id": f.id, "text": truncate(f.text, 120), "kind": f.experience_kind if f.category in {"role", "project"} else f.category})
    return used[:10]


def write_optional_text(db: Database, job: dict, kind: str) -> dict:
    """A short introduction (about 60 to 100 words) or a cover letter (about 180 to 250 words), only on request,
    from approved facts only. Validated like every draft; a new version never touches the person's edited text."""
    prof, facts = drafts_svc._facts(db)
    row = _package_row(db, job["id"])
    if not row:
        raise WorkspaceError("Prepare the application first.")
    content = json.loads(row["content_json"])
    employer = job.get("employer_name") or "your company"
    skills = [f for f in facts if f.category == "skill"]
    desc = (job.get("description_text") or "").lower()
    relevant = [f for f in skills if f.text.lower().split(" (")[0] in desc][:4] or skills[:3]
    roles = [f for f in facts if f.category == "role" and f.experience_kind == "professional"]
    projects = [f for f in facts if f.category == "project"]
    certs = [f for f in facts if f.category == "certification"]
    tools = ", ".join(f.text.split(" (")[0] for f in relevant)
    if kind == "intro":
        parts = [f"I am applying for the {job['title']} role at {employer}."]
        if roles:
            parts.append(f"My paid experience: {roles[0].text}.")
        if tools:
            parts.append(f"I work hands-on with {tools}.")
        if projects:
            parts.append(f"Outside work I built {projects[0].text.split('.')[0].strip()}.")
        parts.append("I would be glad to tell you more about what I did and what I learned.")
        text = " ".join(parts)
        target = "60 to 100 words"
    else:
        paras = [f"Dear hiring team at {employer},", f"I am applying for the {job['title']} role. The work you describe is the direction I have been building towards: infrastructure and platform engineering done carefully and in the open."]
        if roles:
            paras.append(f"In my paid work so far: {roles[0].text}." + (f" Before that: {roles[1].text}." if len(roles) > 1 else ""))
        if tools:
            paras.append(f"The tools I use hands-on and can talk about in detail: {tools}." + (f" I hold {certs[0].text}." if certs else ""))
        if projects:
            paras.append(f"A project I can show: {projects[0].text.split('.')[0].strip()}. I am open about its size; it is not production work unless my CV says so.")
        paras.append("I would welcome a conversation about the role and what the first months would look like.")
        paras.append("Kind regards,\n[Your name]")
        text = "\n\n".join(paras)
        target = "180 to 250 words"
    problems = drafts_svc.validate_text(text, [f.id for f in relevant + roles[:2] + projects[:1] + certs[:1]], facts, job.get("description_text") or "", identity_text=f"{job.get('title') or ''} {employer}")
    if problems:
        text = ""
    words = len(re.findall(r"\w+", text))
    slot = content.setdefault("optional", {}).setdefault(kind, {"versions": [], "user_text": None})
    version = len(slot["versions"]) + 1
    slot["versions"].append({"version": version, "text": text, "words": words, "target": target, "facts_used": _facts_used(text, facts), "problems": problems, "created_at": now_iso(), "created_by": "rules"})
    with db.tx():
        db.update("drafts", {"content_json": json.dumps(content)}, "id = ?", (row["id"],))
    return {"kind": kind, "version": version, "text": text, "words": words, "problems": problems}


def save_optional_text(db: Database, job_id: int, kind: str, text: str | None, rev: int | None = None, when: str | None = None) -> dict:
    row = _package_row(db, job_id) or db.fetchone("SELECT * FROM drafts WHERE job_id = ? AND kind = 'package' ORDER BY id DESC LIMIT 1", (job_id,))
    if not row or kind not in {"intro", "letter"}:
        raise WorkspaceError("There is no prepared application for this job yet.")
    content = json.loads(row["content_json"])
    slot = content.setdefault("optional", {}).setdefault(kind, {"versions": [], "user_text": None})
    text = (text or "").strip()[:MAX_ANSWER_CHARS * 2]
    slot["user_text"] = {"text": text, "saved_at": when or now_iso(), "rev": int(rev if rev is not None else ((slot.get("user_text") or {}).get("rev") or 0) + 1)} if text else None
    with db.tx():
        db.update("drafts", {"content_json": json.dumps(content)}, "id = ?", (row["id"],))
    return {"draft_id": row["id"], "kind": kind, "saved": bool(text)}


# ---------------------------------------------------------------------------
# Contacts (found on request, never guessed) and follow-ups
# ---------------------------------------------------------------------------
EMAIL_RX = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
NAMED_CONTACT_RX = re.compile(r"(?:contact|questions\??|reach out to|recruiter|hiring manager|talent partner)[^.\n]{0,40}?\b([A-Z][a-z]+(?: [A-Z][a-z]+){1,2})\b")


def find_contact(db: Database, job: dict, params: dict | None = None) -> dict:
    """Listed contact: a person or route the posting itself names. Your connection: someone the person names.
    Nothing else is invented: no guessed addresses, no implied referrals. "I couldn't confirm a contact" is a
    valid answer. Public pages a Claude session reads can be added with `contacts add`, with their URL."""
    params = params or {}
    found = []
    ts = now_iso()
    desc = job.get("description_text") or ""
    if params.get("name"):
        found.append(_add_contact(db, job["id"], "connection", params["name"], params.get("role"), "Someone you know there, in your words.", params.get("url"), "user", route=params.get("url"), note=params.get("note")))
    else:
        name = NAMED_CONTACT_RX.search(desc)
        # The address that belongs to the named contact (right after the name), else one next to a contact word.
        email = EMAIL_RX.search(desc, name.end(), min(len(desc), name.end() + 80)) if name else None
        if not email:
            near = re.search(r"(?:contact|questions\??|reach out|recruit|apply|hiring)[^\n]{0,60}?(" + EMAIL_RX.pattern + ")", desc, re.I)
            email = near if near else None
        if email or name:
            who = name.group(1) if name else None
            route = (email.group(1) if email and email.re is not EMAIL_RX else email.group(0)) if email else None
            if not db.fetchone("SELECT id FROM contacts WHERE job_id = ? AND contact_type = 'listed' AND COALESCE(route,'') = COALESCE(?,'')", (job["id"], route)):
                found.append(_add_contact(db, job["id"], "listed", who or "Contact named in the posting", "Named in the posting", "The posting names this contact or address for questions about the role.", job.get("direct_url") or job.get("apply_url") or job.get("discovery_url"), "posting", route=route))
    if found:
        return {"contacts": found, "message": f"Found {len(found)} contact{'s' if len(found) != 1 else ''} with a real source.", "checked_at": ts}
    return {"contacts": [], "message": "I couldn't confirm a contact for this job. The posting names nobody. If you know someone there, tell me their name and I will keep it as your connection. I never guess email addresses.", "checked_at": ts}


def _add_contact(db: Database, job_id: int, ctype: str, name: str, role: str | None, relevance: str, source_url: str | None, provenance: str, route: str | None = None, note: str | None = None) -> dict:
    ts = now_iso()
    conf = {"listed": "confirmed", "connection": "confirmed", "team": "likely"}[ctype]
    with db.tx():
        cid = db.insert("contacts", {"job_id": job_id, "name": truncate(name, MAX_CONTACT_FIELD), "role": truncate(role or "", MAX_CONTACT_FIELD) or None, "organization": None, "route": truncate(route or "", 400) or None, "provenance": provenance if provenance == "user" else f"public_page:{source_url or ''}", "confidence": conf, "created_at": ts, "contact_type": ctype, "relevance": truncate(relevance, 300), "source_url": (source_url or "")[:500] or None, "checked_at": ts, "status": "found"})
        if note:
            db.update("contacts", {"relevance": truncate(f"{relevance} Note: {note}", 300)}, "id = ?", (cid,))
    return contact_view(db.fetchone("SELECT * FROM contacts WHERE id = ?", (cid,)))


def add_contact(db: Database, job_id: int, ctype: str, name: str, role: str | None = None, relevance: str | None = None, source_url: str | None = None, route: str | None = None) -> dict:
    """A contact a Claude session found on a public page (with its URL) or the person named. `team` contacts
    need a source URL; a route (public address or form) is kept only when it was given, never guessed."""
    if ctype not in CONTACT_TYPES:
        raise WorkspaceError("Contact type must be listed, team or connection.")
    if ctype in {"listed", "team"} and not (source_url or "").startswith("https://"):
        raise WorkspaceError("A listed or team contact needs the https page where it was found.")
    if route and not (EMAIL_RX.fullmatch(route) or route.startswith("https://")):
        raise WorkspaceError("A contact route must be an email address or an https link, exactly as published.")
    if not db.fetchone("SELECT id FROM jobs WHERE id = ?", (job_id,)):
        raise WorkspaceError(f"Job {job_id} not found.")
    return _add_contact(db, job_id, ctype, name, role, relevance or {"listed": "Named in the posting.", "team": "Works on a team the posting describes (public page).", "connection": "Someone you know there."}[ctype], source_url, "user" if ctype == "connection" else "public_page", route=route)


def contact_view(c: dict) -> dict:
    return {"id": c["id"], "type": c.get("contact_type") or "listed", "type_label": CONTACT_TYPES.get(c.get("contact_type") or "listed"), "name": c.get("name"), "role": c.get("role"), "relevance": c.get("relevance"), "source_url": c.get("source_url"), "checked_at": c.get("checked_at") or c.get("created_at"), "route": c.get("route"), "status": c.get("status") or "found", "contacted_at": c.get("contacted_at")}


def list_contacts(db: Database, job_id: int) -> list[dict]:
    """Contacts with the latest drafted message for each (the draft is shown; sending is the person's own act)."""
    out = []
    drafts = db.fetchall("SELECT content_json, created_at FROM drafts WHERE job_id = ? AND kind = 'outreach' ORDER BY id DESC", (job_id,))
    for c in db.fetchall("SELECT * FROM contacts WHERE job_id = ? AND COALESCE(status,'found') != 'dismissed' ORDER BY id", (job_id,)):
        v = contact_view(c)
        greeting = f"Hi {c.get('name')}," if c.get("name") else "Hello,"
        for d in drafts:
            body = (json.loads(d["content_json"] or "{}").get("body") or "")
            if body.startswith(greeting):
                v["message"] = body
                v["message_at"] = d["created_at"]
                break
        out.append(v)
    return out


def contact_message(db: Database, job_id: int, contact_id: int) -> dict:
    c = db.fetchone("SELECT * FROM contacts WHERE id = ? AND job_id = ?", (contact_id, job_id))
    if not c:
        raise WorkspaceError("Contact not found.")
    return drafts_svc.outreach(db, job_id, c.get("name"), purpose="outreach")


def mark_contacted(db: Database, job_id: int, contact_id: int, when: str | None = None, channel: str = "message") -> dict:
    """Only the person's confirmation records "contacted". Preparing or copying a message never does."""
    c = db.fetchone("SELECT * FROM contacts WHERE id = ? AND job_id = ?", (contact_id, job_id))
    if not c:
        raise WorkspaceError("Contact not found.")
    if c.get("contacted_at"):
        return contact_view(c)
    ts = when or now_iso()
    with db.tx():
        db.update("contacts", {"status": "contacted", "contacted_at": ts}, "id = ?", (contact_id,))
    actions.mark_contacted(db, job_id, who=c.get("name") or "a contact", channel=channel, occurred_at=ts)
    return contact_view(db.fetchone("SELECT * FROM contacts WHERE id = ?", (contact_id,)))


STOP_STAGES = {"replied", "interview", "offer", "rejected", "withdrawn"}


def set_followup(db: Database, job_id: int, due_at: str | None = None, days: int | None = None, timezone: str | None = None) -> dict:
    """One pending reminder per application, with a date the person can change. Dashboard reminders by
    default; the email digest lists them only when `followups.email` is on (a separate preference)."""
    app = db.fetchone("SELECT * FROM applications WHERE job_id = ?", (job_id,))
    if not app or not app.get("applied_at"):
        raise WorkspaceError("Follow-ups are for jobs you told me you applied to. Press 'I applied' first.")
    if app["stage"] in STOP_STAGES:
        raise WorkspaceError(f"No reminder: the application is at stage '{app['stage']}'.")
    when = parse_iso(due_at) if due_at else None
    if not when:
        when = (parse_iso(app["applied_at"]) or now()) + timedelta(days=days or 7)
    if when < now():
        when = now()
    ts = now_iso()
    with db.tx():
        cur = db.fetchone("SELECT * FROM followups WHERE application_id = ? AND status = 'pending' ORDER BY due_at LIMIT 1", (app["id"],))
        if cur:
            db.update("followups", {"due_at": when.isoformat(), "snoozed_until": None, "timezone": timezone or cur.get("timezone")}, "id = ?", (cur["id"],))
            fid = cur["id"]
        else:
            fid = db.insert("followups", {"application_id": app["id"], "due_at": when.isoformat(), "created_at": ts, "timezone": timezone})
    return followup_view(db.fetchone("SELECT * FROM followups WHERE id = ?", (fid,)))


def followup_action(db: Database, job_id: int, action: str, due_at: str | None = None, days: int = 3) -> dict | None:
    """snooze (a few days or a date), done, stop. Replies, rejections, withdrawals and interviews stop reminders
    on their own (actions.set_stage)."""
    app = db.fetchone("SELECT id FROM applications WHERE job_id = ?", (job_id,))
    cur = db.fetchone("SELECT * FROM followups WHERE application_id = ? AND status = 'pending' ORDER BY due_at LIMIT 1", (app["id"],)) if app else None
    if action == "set":
        return set_followup(db, job_id, due_at=due_at)
    if not cur:
        return None
    ts = now_iso()
    with db.tx():
        if action == "snooze":
            when = parse_iso(due_at) if due_at else None
            when = when or max(now(), parse_iso(cur["due_at"]) or now()) + timedelta(days=days)
            db.update("followups", {"due_at": when.isoformat(), "snoozed_until": when.isoformat()}, "id = ?", (cur["id"],))
        elif action == "done":
            db.update("followups", {"status": "done", "resolved_at": ts}, "id = ?", (cur["id"],))
        elif action == "stop":
            db.update("followups", {"status": "cancelled", "resolved_at": ts, "reason": "stopped by you"}, "id = ?", (cur["id"],))
        else:
            raise WorkspaceError(f"Unknown follow-up action '{action}'.")
    return followup_view(db.fetchone("SELECT * FROM followups WHERE id = ?", (cur["id"],)))


def followup_view(f: dict | None) -> dict | None:
    if not f:
        return None
    due = parse_iso(f["due_at"])
    return {"id": f["id"], "due_at": f["due_at"], "due": due.date().isoformat() if due else None, "status": f["status"], "timezone": f.get("timezone"), "snoozed_until": f.get("snoozed_until"), "overdue": bool(due and due <= now() and f["status"] == "pending"), "resolved_at": f.get("resolved_at")}


def current_followup(db: Database, job_id: int) -> dict | None:
    app = db.fetchone("SELECT id, stage FROM applications WHERE job_id = ?", (job_id,))
    if not app:
        return None
    f = db.fetchone("SELECT * FROM followups WHERE application_id = ? AND status = 'pending' ORDER BY due_at LIMIT 1", (app["id"],))
    return followup_view(f)


# ---------------------------------------------------------------------------
# The document the page shows: one per job with an application
# ---------------------------------------------------------------------------
def _needs_review(db: Database, job: dict, package: dict | None, profile: dict | None, cv: dict | None) -> list[str]:
    if not package or not profile:
        return []
    out = []
    comp = package.get("inputs") or {}
    if comp.get("job") and comp["job"] != job.get("description_hash"):
        out.append("The job text changed since this was prepared.")
    if comp.get("profile") and comp["profile"] != drafts_svc._profile_fingerprint(profile):
        out.append("Your profile changed since this was prepared.")
    if comp.get("cv") and cv and comp["cv"] != cv.get("sha256"):
        out.append(f"The CV '{cv.get('label')}' has a newer version than the one this was prepared with.")
    if not out and drafts_svc.package_is_outdated(db, job, package):
        out.append("Something this preparation depends on changed. Prepare again to refresh it; your own wording stays.")
    return out


def work_doc(db: Database, job_id: int, key: str) -> dict | None:
    """The job's workspace for the page: states, the brief, the route, the case, answers, optional texts,
    checklist, contacts, follow-up, hand-off. Carries the person's own drafts (owner-only page), never the CV
    text or bytes, never a token."""
    job = db.fetchone("SELECT * FROM jobs WHERE id = ?", (job_id,))
    if not job:
        return None
    app = db.fetchone("SELECT * FROM applications WHERE job_id = ?", (job_id,))
    reqs = db.fetchall("SELECT * FROM requests WHERE job_id = ? ORDER BY id", (job_id,))
    if not app and not reqs:
        return None
    apply_pending(db, job_id, key)
    profile = profile_svc.approved(db)
    facts = [f for f in profile["facts"] if f.status != "rejected"] if profile else []
    cv = drafts_svc.selected_cv(db, job_id)
    package = drafts_svc.current_package(db, job_id)
    cvs = [c for c in docs.list_cvs(db) if not c["archived"]]
    suggestion = docs.suggest_cv(db, job)
    if suggestion and cv and suggestion["variant_key"] == cv["key"]:
        suggestion = None
    if suggestion:
        suggestion = {"key": suggestion["variant_key"], "label": suggestion["label"], "reason": suggestion["reason"]}
    requests_state: dict = {}
    for r in reqs:
        requests_state[r["kind"]] = {"id": r["request_id"], "state": r["state"], "message": r.get("message"), "at": r.get("finished_at") or r.get("started_at") or r["requested_at"], "result": json.loads(r["result_json"]) if r.get("result_json") and r["kind"] in {"handoff", "cv_file"} else None}
    pkg_view = None
    if package:
        draft = package.get("draft") or {}
        user_answers = package.get("user_answers") or {}
        answers = []
        for a in draft.get("answers", []):
            q = a["question"]
            answers.append({"id": answer_id(q), "question": q, "suggested": a.get("answer") or "", "needs_user": bool(a.get("needs_user")), "note": a.get("note"), "limit": None, "user": user_answers.get(q)})
        for q in package.get("custom_questions", []):
            answers.append({"id": answer_id(q["text"]), "question": q["text"], "suggested": "", "needs_user": True, "note": q.get("note"), "limit": q.get("limit"), "user": user_answers.get(q["text"]), "custom": True})
        research = current_research(db, job_id) or ensure_research(db, job)
        try:
            cv_text = docs.cv_text(db, cv["revision_key"]) if cv and cv.get("revision_key") else ""
        except docs.DocumentError:
            cv_text = ""
        case = strongest_case(db, job, facts, cv_text) if facts else {"items": [], "examples": [], "note": None}
        optional = {}
        for kind in ("intro", "letter"):
            slot = (package.get("optional") or {}).get(kind)
            if slot:
                latest = slot["versions"][-1] if slot.get("versions") else None
                optional[kind] = {"versions": len(slot.get("versions") or []), "latest": latest, "user_text": slot.get("user_text")}
        confirmed = sum(1 for a in answers if a.get("user") and (a["user"] or {}).get("text"))
        pkg_view = {
            "draft_id": package["draft_id"],
            "version": package.get("version"),
            "created_at": package.get("created_at"),
            "needs_review": _needs_review(db, job, package, profile, cv),
            "brief": research,
            "route": route_view(db, job),
            "case": case,
            "answers": answers,
            "optional": optional,
            "project_links": package.get("project_links") or [],
            "checklist": [
                {"id": "cv", "text": f"CV: {cv['label']} (version {cv['version']})" if cv else "Choose a CV", "done": bool(cv)},
                {"id": "answers", "text": f"Answers in your own words: {confirmed} of {len(answers)}", "done": confirmed >= len([a for a in answers if not a['needs_user']]) and len(answers) > 0},
                {"id": "open", "text": "Open the application page and apply there yourself", "done": False},
                {"id": "applied", "text": "Press 'I applied' so Hunter records the date and the CV you sent", "done": bool(app and app.get("applied_at"))},
            ],
            "reminder": package.get("reminder"),
        }
    submitted = None
    if app and app.get("applied_at"):
        rev = docs.cv_revision(db, app["submitted_cv_revision_key"]) if app.get("submitted_cv_revision_key") else None
        submitted = {"choice": app.get("submitted_cv_choice") or "unknown", "label": rev["label"] if rev else None, "version": rev["number"] if rev else None, "filename": rev["original_filename"] if rev else None}
    handoff = db.fetchone("SELECT path, created_at FROM handoffs WHERE application_id = ? ORDER BY id DESC LIMIT 1", (app["id"],)) if app else None
    followup = current_followup(db, job_id)
    last_followup = db.fetchone("SELECT * FROM followups WHERE application_id = ? AND status != 'pending' ORDER BY id DESC LIMIT 1", (app["id"],)) if app else None
    return {
        "schema": WORK_SCHEMA,
        "key": key,
        "job_id": job_id,
        "at": now_iso(),
        "state": "applied" if app and app.get("applied_at") else ("prepared" if package else "none"),
        "application": {"stage": app["stage"], "stage_label": {"prepared": "Ready to apply", "applied": "Applied", "replied": "Reply received", "interview": "Interview", "offer": "Offer", "rejected": "Rejected", "withdrawn": "Withdrawn"}.get(app["stage"], app["stage"]), "applied_at": app.get("applied_at"), "submitted_cv": submitted, "prepared_at": package.get("created_at") if package else None} if app else None,
        "cv": {"selected": {"key": cv["key"], "label": cv["label"], "version": cv["version"], "choice": cv.get("choice") or "default"} if cv else None, "suggestion": suggestion, "options": [{"key": c["key"], "label": c["label"], "version": c["version"], "default": c["default"]} for c in cvs]},
        "requests": requests_state,
        "package": pkg_view,
        "contacts": list_contacts(db, job_id),
        "followup": followup,
        "followup_last": followup_view(last_followup) if last_followup else None,
        "handoff": {"path": handoff["path"], "at": handoff["created_at"]} if handoff else None,
        "notes_count": notes.count_for_job(db, job_id),
    }


def work_keys(db: Database) -> list[tuple[int, str]]:
    """Every job that has a workspace: an application record or a request."""
    from jobhunter.services.board import job_key

    rows = db.fetchall("SELECT DISTINCT j.id, j.canonical_key FROM jobs j WHERE j.id IN (SELECT job_id FROM applications UNION SELECT job_id FROM requests WHERE job_id IS NOT NULL) ORDER BY j.id")
    return [(r["id"], job_key(r["canonical_key"])) for r in rows]


# ---------------------------------------------------------------------------
# What the page wrote: workspace fields of an action document
# ---------------------------------------------------------------------------
PENDING_SETTING = "work.pending"  # page edits that arrived before the package they belong to exists (kept, applied later)


def _remember_pending(db: Database, key: str, kind: str, ident: str, value: dict) -> None:
    pending = db.get_setting(PENDING_SETTING) or {}
    slot = pending.setdefault(key, {}).setdefault(kind, {})
    if int(value.get("rev") or 0) >= int((slot.get(ident) or {}).get("rev") or 0):
        slot[ident] = {"text": str(value.get("text") or "")[:8000], "rev": int(value.get("rev") or 0), "updated_at": str(value.get("updated_at") or "")[:25]}
        db.set_setting(PENDING_SETTING, pending)


def apply_pending(db: Database, jid: int, key: str) -> list[str]:
    """Edits kept from an earlier read (an answer saved on the page before Hunter had prepared the job here, as
    after a restore) land once the package exists. Never lost, never applied twice."""
    pending = db.get_setting(PENDING_SETTING) or {}
    slot = pending.get(key)
    if not slot or not _package_row(db, jid):
        return []
    changes: list[str] = []
    for aid, v in list((slot.get("answers") or {}).items()):
        try:
            save_answer(db, jid, aid, v.get("text") or "", rev=v.get("rev"), when=v.get("updated_at") or None)
            changes.append("answer")
            slot["answers"].pop(aid, None)
        except WorkspaceError:
            slot["answers"].pop(aid, None)  # the question is gone: nothing to keep
    for kind, v in list((slot.get("texts") or {}).items()):
        try:
            save_optional_text(db, jid, kind, v.get("text"), rev=v.get("rev"), when=v.get("updated_at") or None)
            changes.append(f"text:{kind}")
        except WorkspaceError:
            pass
        slot["texts"].pop(kind, None)
    if not (slot.get("answers") or slot.get("texts")):
        pending.pop(key, None)
    db.set_setting(PENDING_SETTING, pending)
    return changes
def apply_action(db: Database, jid: int, a: dict, key: str) -> list[str]:
    """Workspace fields of one action document, applied once with the rest of it (same transaction, same
    `rev`). Untrusted input: only known fields, bounded. Returns the list of changes."""
    changes: list[str] = []
    job = db.fetchone("SELECT * FROM jobs WHERE id = ?", (jid,))
    if not job:
        return changes
    # Requests: the latest per kind; an id Hunter already has is not recorded again.
    reqs = a.get("requests")
    if isinstance(reqs, dict):
        for kind, r in list(reqs.items())[:len(REQUEST_KINDS)]:
            if kind in REQUEST_KINDS and isinstance(r, dict) and r.get("id"):
                try:
                    before = db.fetchone("SELECT id FROM requests WHERE request_id = ?", (_clean_request_id(r["id"]),))
                    request(db, jid, kind, r.get("params") if isinstance(r.get("params"), dict) else {}, request_id=r["id"], job_key=key)
                    if not before:
                        changes.append(f"request:{kind}")
                except WorkspaceError:
                    continue
    # CV choice for this job only.
    cvk = a.get("cv_variant")
    if isinstance(cvk, str) and cvk:
        cur = db.fetchone("SELECT cv_variant_key FROM applications WHERE job_id = ?", (jid,))
        if not cur or cur.get("cv_variant_key") != cvk:
            try:
                drafts_svc.select_cv(db, jid, cvk, choice=str(a.get("cv_choice") or "student"))
                changes.append("cv")
            except (drafts_svc.DraftError, docs.DocumentError):
                pass
    # Edited answers and optional texts: the person's wording, by revision.
    answers = a.get("answers")
    if isinstance(answers, dict):
        pkg = db.fetchone("SELECT content_json FROM drafts WHERE job_id = ? AND kind = 'package' ORDER BY id DESC LIMIT 1", (jid,))
        current = (json.loads(pkg["content_json"]).get("user_answers") or {}) if pkg else {}
        by_id = {answer_id(q): v for q, v in current.items()}
        for aid, v in list(answers.items())[:MAX_QUESTIONS + 10]:
            if not isinstance(v, dict) or not re.fullmatch(r"a[0-9a-f]{8}", str(aid)):
                continue
            rev = int(v.get("rev") or 0)
            if rev <= int((by_id.get(aid) or {}).get("rev") or 0):
                continue
            try:
                save_answer(db, jid, aid, str(v.get("text") or ""), rev=rev, when=str(v.get("updated_at") or "")[:25] or None)
                changes.append("answer")
            except WorkspaceError:
                _remember_pending(db, key, "answers", str(aid), v)
                changes.append("answer:kept")
    texts = a.get("texts")
    if isinstance(texts, dict):
        for kind in ("intro", "letter"):
            v = texts.get(kind)
            if isinstance(v, dict):
                try:
                    pkg = _package_row(db, jid)
                    cur = ((json.loads(pkg["content_json"]).get("optional") or {}).get(kind) or {}).get("user_text") if pkg else None
                    rev = int(v.get("rev") or 0)
                    if rev > int((cur or {}).get("rev") or 0):
                        save_optional_text(db, jid, kind, v.get("text"), rev=rev, when=str(v.get("updated_at") or "")[:25] or None)
                        changes.append(f"text:{kind}")
                except WorkspaceError:
                    _remember_pending(db, key, "texts", kind, v)
                    changes.append(f"text:{kind}:kept")
    # Contacts the person named or confirmed.
    for c in (a.get("contacts_new") or [])[:5] if isinstance(a.get("contacts_new"), list) else []:
        if isinstance(c, dict) and c.get("name") and c.get("tmp"):
            if db.fetchone("SELECT id FROM contacts WHERE job_id = ? AND contact_type = 'connection' AND name = ?", (jid, str(c["name"])[:MAX_CONTACT_FIELD])):
                continue
            try:
                route = str(c.get("route") or "")[:400] or None
                add_contact(db, jid, "connection", str(c["name"]), str(c.get("role") or "")[:MAX_CONTACT_FIELD] or None, None, None, route if route and (EMAIL_RX.fullmatch(route) or route.startswith("https://")) else None)
                changes.append("contact")
            except WorkspaceError:
                continue
    contacted = a.get("contacted")
    if isinstance(contacted, dict):
        for cid, v in list(contacted.items())[:20]:
            try:
                cid_i = int(cid)
            except (TypeError, ValueError):
                continue
            if isinstance(v, dict) and v.get("at"):
                c = db.fetchone("SELECT contacted_at FROM contacts WHERE id = ? AND job_id = ?", (cid_i, jid))
                if c and not c.get("contacted_at"):
                    mark_contacted(db, jid, cid_i, when=str(v["at"])[:25], channel=str(v.get("channel") or "message")[:30])
                    changes.append("contacted")
    # Follow-up changes.
    fu = a.get("followup")
    if isinstance(fu, dict) and fu.get("action") and fu.get("at"):
        last = db.fetchone("SELECT id FROM applications WHERE job_id = ?", (jid,))
        done_key = f"followup:{key}:{fu['at']}"
        if last and not db.fetchone("SELECT id FROM events WHERE dedupe_key = ?", (done_key,)):
            try:
                followup_action(db, jid, str(fu["action"]), due_at=str(fu.get("due_at") or "")[:25] or None)
                with db.tx():
                    actions._event(db, "followup", jid, "user", {"action": fu.get("action"), "due_at": fu.get("due_at")}, application_id=last["id"], dedupe_key=done_key)
                changes.append(f"followup:{fu['action']}")
            except WorkspaceError:
                pass
    return changes
