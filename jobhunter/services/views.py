"""Read models for the dashboard, CLI, and skill: job lists, details, attention."""

from __future__ import annotations

import json
import math

from jobhunter.config import POLICY_VERSION
from jobhunter.db import Database
from jobhunter.services import actions, freshness, hunt
from jobhunter.services.matching.engine import group_label
from jobhunter.util import truncate

BASE_SELECT = (
    "SELECT j.*, m.match_group, m.score, m.explanation_text, m.explanation_source, m.explanation_json, m.next_step, m.gaps_json, m.reasons_json, m.eligibility_json, m.exclusion_reason, "
    "a.stage AS app_stage, a.applied_at AS app_applied_at, "
    "(SELECT kind FROM events e WHERE e.job_id = j.id AND e.kind IN ('saved','unsaved') AND e.reverted_by IS NULL ORDER BY e.id DESC LIMIT 1) AS save_state, "
    "(SELECT kind FROM events e WHERE e.job_id = j.id AND e.kind IN ('dismissed','undismissed') AND e.reverted_by IS NULL ORDER BY e.id DESC LIMIT 1) AS dismiss_state "
    "FROM jobs j LEFT JOIN matches m ON m.job_id = j.id AND m.policy_version = ? AND m.profile_version_id = (SELECT id FROM profile_versions WHERE status = 'approved' ORDER BY version DESC LIMIT 1) "
    "LEFT JOIN applications a ON a.job_id = j.id "
)


def _where(view: str, group: str | None, query: str | None) -> tuple[str, list]:
    """Filters expressed against the wrapped SELECT's output columns."""
    clauses, params = [], []
    recommended = "match_group IN ('good','worth_a_shot','backup') AND availability != 'closed' AND COALESCE(dismiss_state,'') != 'dismissed' AND app_stage IS NULL"
    if view == "new":
        clauses.append(recommended)
        clauses.append("(posted_at IS NULL OR posted_at_provenance = 'unknown' OR datetime(posted_at) >= datetime('now', '-14 days'))")
    elif view == "old":
        clauses.append(recommended)
        clauses.append("(posted_at IS NOT NULL AND posted_at_provenance != 'unknown' AND datetime(posted_at) < datetime('now', '-14 days'))")
    elif view == "saved":
        clauses.append("save_state = 'saved' AND COALESCE(dismiss_state,'') != 'dismissed'")
    elif view == "applied":
        clauses.append("app_stage IN ('applied','replied')")
    elif view == "interviews":
        clauses.append("app_stage = 'interview'")
    elif view == "finished":
        clauses.append("app_stage IN ('offer','rejected','withdrawn')")
    elif view == "dismissed":
        clauses.append("dismiss_state = 'dismissed'")
    elif view == "excluded":
        clauses.append("match_group = 'excluded'")
    elif view == "uncertain":
        clauses.append("relevance = 'uncertain' AND (match_group IS NULL OR match_group NOT IN ('good','worth_a_shot','backup'))")
    elif view == "closed":
        clauses.append("availability = 'closed'")
    if group:
        clauses.append("match_group = ?")
        params.append(group)
    if query:
        clauses.append("(title LIKE ? OR employer_name LIKE ? OR location_text LIKE ?)")
        params += [f"%{query}%"] * 3
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", params


def list_jobs(db: Database, view: str = "new", group: str | None = None, query: str | None = None, page: int = 1, per_page: int = 10, sort: str = "best") -> dict:
    where, params = _where(view, group, query)
    order = {
        "best": "CASE match_group WHEN 'good' THEN 0 WHEN 'worth_a_shot' THEN 1 WHEN 'backup' THEN 2 WHEN 'uncertain' THEN 3 ELSE 4 END, score DESC, first_seen_at DESC",
        "newest": "COALESCE(posted_at, first_seen_at) DESC",
        "employer": "employer_name, title",
    }[sort]
    wrapped = f"({BASE_SELECT})"
    total = db.fetchone(f"SELECT COUNT(*) AS n FROM {wrapped}{where}", (POLICY_VERSION, *params))["n"]
    page = max(1, page)
    rows = db.fetchall(f"SELECT * FROM {wrapped}{where} ORDER BY {order} LIMIT ? OFFSET ?", (POLICY_VERSION, *params, per_page, (page - 1) * per_page))
    items = [summarize(r) for r in rows]
    return {"view": view, "group": group, "query": query, "page": page, "per_page": per_page, "total": total, "pages": max(1, math.ceil(total / per_page)), "items": items}


def summarize(r: dict) -> dict:
    b, flabel = freshness.bucket(r)
    expl = json.loads(r.get("explanation_json") or "{}") if r.get("explanation_json") else {}
    return {
        "id": r["id"],
        "title": r["title"],
        "employer": r.get("employer_name") or ("Confidential employer" if r.get("employer_confidential") else "Unknown employer"),
        "group": r.get("match_group"),
        "group_label": group_label(r["match_group"]) if r.get("match_group") else "Not matched yet",
        "score": r.get("score"),
        "location": r.get("location_text") or "Location not stated",
        "remote_type": r.get("remote_type"),
        "remote_restriction": r.get("remote_restriction"),
        "salary": r.get("salary_text") or "Not listed",
        "freshness": b,
        "freshness_label": flabel,
        "verification": freshness.verification_label(r),
        "availability": r.get("availability"),
        "explanation": r.get("explanation_text"),
        "explanation_source": r.get("explanation_source"),
        "explanation_label": {"rules": "Rule-based explanation (no AI)", "claude": "Explained by Claude", "pending": "Explanation pending", "failed": "Rule-based explanation (Claude unavailable)"}.get(r.get("explanation_source") or "pending", ""),
        "reasons": expl.get("reasons", []),
        "gaps": expl.get("gaps", []) or json.loads(r.get("gaps_json") or "[]"),
        "next_step": r.get("next_step"),
        "exclusion_reason": r.get("exclusion_reason"),
        "links": {"direct": r.get("direct_url") if r.get("direct_url_status") == "verified" else None, "apply": r.get("apply_url"), "discovery": r.get("discovery_url"), "direct_status": r.get("direct_url_status")},
        "saved": r.get("save_state") == "saved",
        "dismissed": r.get("dismiss_state") == "dismissed",
        "stage": r.get("app_stage"),
        "sponsorship": r.get("sponsorship"),
        "posted_at": r.get("posted_at"),
        "posted_at_provenance": r.get("posted_at_provenance"),
        "first_seen_at": r.get("first_seen_at"),
        "relevance": r.get("relevance"),
    }


def job_detail(db: Database, job_id: int) -> dict | None:
    row = db.fetchone(f"SELECT * FROM ({BASE_SELECT}) WHERE id = ?", (POLICY_VERSION, job_id))
    if not row:
        return None
    s = summarize(row)
    s["description"] = row.get("description_text") or ""
    s["countries"] = json.loads(row.get("country_codes_json") or "[]")
    s["employment_type"] = row.get("employment_type")
    s["salary_parsed"] = {"min": row.get("salary_min"), "max": row.get("salary_max"), "currency": row.get("salary_currency"), "period": row.get("salary_period"), "basis": row.get("salary_basis")}
    s["dates"] = {"posted_at": row.get("posted_at"), "posted_at_provenance": row.get("posted_at_provenance"), "source_updated_at": row.get("source_updated_at"), "first_seen_at": row.get("first_seen_at"), "last_seen_at": row.get("last_seen_at"), "last_checked_at": row.get("last_checked_at")}
    s["eligibility"] = json.loads(row.get("eligibility_json") or "{}") if row.get("eligibility_json") else {}
    s["observations"] = db.fetchall("SELECT source_id, external_id, url, first_observed_at, last_observed_at FROM job_observations WHERE job_id = ? ORDER BY first_observed_at", (job_id,))
    s["history"] = actions.history(db, job_id)
    s["drafts"] = db.fetchall("SELECT id, kind, status, created_by, created_at, sent_confirmed_at FROM drafts WHERE job_id = ? ORDER BY id DESC", (job_id,))
    s["application"] = db.fetchone("SELECT * FROM applications WHERE job_id = ?", (job_id,))
    s["source_note"] = _source_note(s["observations"])
    return s


def _source_note(observations: list[dict]) -> str | None:
    for o in observations:
        if o["source_id"].startswith("remotive"):
            return "Found via Remotive (remotive.com). Listings there are delayed by 24 hours."
        if o["source_id"].startswith("arbeitnow"):
            return "Found via Arbeitnow (arbeitnow.com)."
        if o["source_id"].startswith("adzuna"):
            return "Found via Adzuna; the description is a snippet."
        if o["source_id"].startswith("import"):
            return "Imported from text or an alert email you gave Hunter (not scraped)."
    return None


def attention(db: Database) -> dict:
    from jobhunter.services import drafts as drafts_svc
    from jobhunter.services import feedback, onboarding
    from jobhunter.services import profile as profile_svc
    from jobhunter.services.email import gmail, linking

    items: list[dict] = []
    needs_review = db.get_setting("profile.needs_review")
    if needs_review:
        items.append({"kind": "profile_review", "text": "A new document was added. Please review and approve your profile again.", "action": "jobhunter profile show"})
    for q in profile_svc.open_questions(db)[:3]:
        items.append({"kind": "question", "text": q["question"], "action": f"jobhunter profile answer {q['key']} \"...\"", "key": q["key"]})
    last = hunt.last_run(db)
    if last and last["status"] in {"failed", "partial"}:
        fails = (last.get("stats") or {}).get("failures", [])
        for f in fails[:3]:
            items.append({"kind": "source_failure", "text": f"I couldn't check {f['source']}: {truncate(f['error'], 120)}. Your other searches still worked.", "action": f"jobhunter sources check {f['source']}"})
    for s in feedback.suggestions(db):
        items.append({"kind": "suggestion", "text": s["question"], "action": f"jobhunter feedback apply {s['key']} yes|no", "key": s["key"]})
    for p in linking.pending_confirmations(db)[:5]:
        sug = p.get("suggested")
        items.append({"kind": "email_confirmation", "text": f"Email '{truncate(p['subject'], 60)}' looks like a {p['classification']}" + (f" for {sug['title']} at {sug['employer_name']}" if sug else "") + ". Is that right?", "action": f"jobhunter email confirm {p['id']} <job_id>  |  jobhunter email reject {p['id']}", "email_id": p["id"], "job_id": p.get("linked_job_id")})
    for f in drafts_svc.due_followups(db)[:5]:
        items.append({"kind": "followup", "text": f"Follow-up due for {f['title']} at {f['employer_name']}.", "action": f"jobhunter drafts followup {f['job_id']}", "job_id": f["job_id"]})
    sched = db.fetchone("SELECT * FROM schedule WHERE id = 1") or {}
    if sched.get("enabled"):
        from jobhunter.services.schedule import describe

        if "running" not in describe(db)["worker"]:
            items.append({"kind": "worker", "text": "Your schedule is on, but no worker is running, so nothing happens in the background.", "action": "jobhunter worker run   (or see jobhunter schedule install --print)"})
    g = gmail.describe(db)
    if g["read"]["connected"] and g["read"].get("last_error"):
        items.append({"kind": "mailbox", "text": f"Mailbox sync problem: {g['read']['last_error']}", "action": "jobhunter email sync"})
    nxt = onboarding.next_step(db)
    return {"setup_pending": nxt, "items": items, "count": len(items)}


def render_list_text(res: dict) -> str:
    if not res["items"]:
        return {"new": "No new recommended jobs right now. Check 'Search health' if you expected some.", "saved": "Nothing saved yet."}.get(res["view"], "Nothing here.")
    lines = [f"{res['view']} · page {res['page']}/{res['pages']} · {res['total']} job(s)"]
    for it in res["items"]:
        lines.append(f"#{it['id']} {it['title']} — {it['employer']} [{it['group_label']}]")
        lines.append(f"    {it['location']}" + (f" · {it['remote_restriction']}" if it.get("remote_restriction") else "") + f" · Salary: {it['salary']} · {it['freshness_label']}")
        if it.get("explanation"):
            lines.append(f"    {it['explanation']}")
        if it.get("next_step"):
            lines.append(f"    Next: {it['next_step']}")
    return "\n".join(lines)


def render_detail_text(j: dict) -> str:
    lines = [f"#{j['id']} {j['title']} — {j['employer']} [{j['group_label']}]", f"{j['location']}" + (f" · {j['remote_restriction']}" if j.get("remote_restriction") else ""), f"Salary: {j['salary']}", f"{j['freshness_label']} · {j['verification']}"]
    if j.get("explanation"):
        lines.append(f"{j['explanation_label']}: {j['explanation']}")
    for r in j.get("reasons", []):
        lines.append(f"- {r.get('text')}")
    for g in j.get("gaps", []):
        lines.append(f"? {g}")
    if j.get("exclusion_reason"):
        lines.append(f"Not recommended: {j['exclusion_reason']}")
    links = j["links"]
    lines.append(f"Employer link: {links['direct'] or 'not confirmed'}")
    lines.append(f"Apply link: {links['apply'] or '-'}")
    lines.append(f"Found at: {links['discovery'] or '-'}")
    if j.get("source_note"):
        lines.append(j["source_note"])
    lines.append(f"Next step: {j.get('next_step') or '-'}")
    lines.append("")
    lines.append(truncate(j.get("description") or "", 3000))
    return "\n".join(lines)
