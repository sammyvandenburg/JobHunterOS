"""Read models for the dashboard, CLI, and skill: job lists with filters, job
detail, dashboard home data, applied list, and what needs attention."""

from __future__ import annotations

import json
import math

from jobhunter.config import POLICY_VERSION
from jobhunter.db import Database
from jobhunter.services import actions, freshness, hunt, notes
from jobhunter.util import now_iso, truncate

VIEWS = ("new", "favorites", "saved", "applied", "finished", "all", "dismissed", "excluded", "uncertain", "old", "closed", "interviews")

BASE_SELECT = (
    "SELECT j.*, m.match_group, m.score, m.explanation_text, m.explanation_source, m.explanation_json, m.next_step, m.gaps_json, m.reasons_json, m.eligibility_json, m.exclusion_reason, "
    "a.stage AS app_stage, a.applied_at AS app_applied_at, a.id AS app_id, "
    "(SELECT kind FROM events e WHERE e.job_id = j.id AND e.kind IN ('saved','unsaved') AND e.reverted_by IS NULL ORDER BY e.id DESC LIMIT 1) AS save_state, "
    "(SELECT kind FROM events e WHERE e.job_id = j.id AND e.kind IN ('favorited','unfavorited') AND e.reverted_by IS NULL ORDER BY e.id DESC LIMIT 1) AS fav_state, "
    "(SELECT kind FROM events e WHERE e.job_id = j.id AND e.kind IN ('dismissed','undismissed') AND e.reverted_by IS NULL ORDER BY e.id DESC LIMIT 1) AS dismiss_state, "
    "(SELECT COUNT(*) FROM job_notes n WHERE n.job_id = j.id AND n.deleted_at IS NULL) AS notes_count "
    "FROM jobs j LEFT JOIN matches m ON m.job_id = j.id AND m.policy_version = ? AND m.profile_version_id = (SELECT id FROM profile_versions WHERE status = 'approved' ORDER BY version DESC LIMIT 1) "
    "LEFT JOIN applications a ON a.job_id = j.id "
)

RECOMMENDED = "match_group IN ('good','worth_a_shot','backup') AND availability != 'closed' AND COALESCE(dismiss_state,'') != 'dismissed' AND app_stage IS NULL"


def _where(view: str, group: str | None, query: str | None, remote: str | None = None, age: str | None = None, country: str | None = None, fav_only: bool = False, saved_only: bool = False) -> tuple[str, list]:
    clauses, params = [], []
    if view == "new":
        clauses.append(RECOMMENDED)
        if age in (None, "", "14"):
            clauses.append("(posted_at IS NULL OR posted_at_provenance = 'unknown' OR datetime(posted_at) >= datetime('now', '-14 days'))")
    elif view == "old":
        clauses.append(RECOMMENDED)
        clauses.append("(posted_at IS NOT NULL AND posted_at_provenance != 'unknown' AND datetime(posted_at) < datetime('now', '-14 days'))")
    elif view == "favorites":
        clauses.append("fav_state = 'favorited'")
    elif view == "saved":
        clauses.append("save_state = 'saved'")
    elif view == "applied":
        clauses.append("app_stage IN ('applied','replied','interview')")
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
    if remote in {"remote", "hybrid", "onsite"}:
        clauses.append("remote_type = ?")
        params.append(remote)
    if age in {"2", "7"}:
        clauses.append(f"(posted_at IS NOT NULL AND posted_at_provenance != 'unknown' AND datetime(posted_at) >= datetime('now', '-{int(age)} days'))")
    if country and len(country) == 2 and country.isalpha():
        clauses.append("country_codes_json LIKE ?")
        params.append(f'%"{country.upper()}"%')
    if fav_only:
        clauses.append("fav_state = 'favorited'")
    if saved_only:
        clauses.append("save_state = 'saved'")
    if query:
        clauses.append("(title LIKE ? OR employer_name LIKE ? OR location_text LIKE ? OR description_text LIKE ?)")
        params += [f"%{query}%"] * 4
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", params


def list_jobs(db: Database, view: str = "new", group: str | None = None, query: str | None = None, page: int = 1, per_page: int = 20, sort: str = "best", remote: str | None = None, age: str | None = None, country: str | None = None, fav_only: bool = False, saved_only: bool = False) -> dict:
    where, params = _where(view, group, query, remote, age, country, fav_only, saved_only)
    order = {
        "best": "CASE match_group WHEN 'good' THEN 0 WHEN 'worth_a_shot' THEN 1 WHEN 'backup' THEN 2 WHEN 'uncertain' THEN 3 ELSE 4 END, score DESC, first_seen_at DESC",
        "newest": "COALESCE(posted_at, first_seen_at) DESC",
        "added": "first_seen_at DESC",
        "employer": "employer_name, title",
        "applied": "app_applied_at DESC",
    }.get(sort, "score DESC")
    wrapped = f"({BASE_SELECT})"
    total = db.fetchone(f"SELECT COUNT(*) AS n FROM {wrapped}{where}", (POLICY_VERSION, *params))["n"]
    page = max(1, page)
    rows = db.fetchall(f"SELECT * FROM {wrapped}{where} ORDER BY {order} LIMIT ? OFFSET ?", (POLICY_VERSION, *params, per_page, (page - 1) * per_page))
    items = [summarize(r) for r in rows]
    return {"view": view, "group": group, "query": query, "page": page, "per_page": per_page, "total": total, "pages": max(1, math.ceil(total / per_page)), "items": items, "filters": {"remote": remote, "age": age, "country": country, "fav_only": fav_only, "saved_only": saved_only, "sort": sort}}


GROUP_LABELS = {"good": "Good match", "worth_a_shot": "Worth a shot", "backup": "Backup", "excluded": "Left out", "uncertain": "Not sure"}


def summarize(r: dict) -> dict:
    b, flabel = freshness.bucket(r)
    expl = json.loads(r.get("explanation_json") or "{}") if r.get("explanation_json") else {}
    reasons = [x for x in expl.get("reasons", []) if x.get("text") and x["text"] not in (r.get("explanation_text") or "")]
    location = r.get("location_text") or "No location given"
    remote_label = {"remote": "Remote", "hybrid": "Hybrid", "onsite": "In office", "unknown": ""}.get(r.get("remote_type") or "unknown", "")
    if remote_label and remote_label.lower() in location.lower():
        remote_label = ""
    return {
        "id": r["id"],
        "title": r["title"],
        "employer": r.get("employer_name") or ("Confidential employer" if r.get("employer_confidential") else "Unknown employer"),
        "group": r.get("match_group"),
        "group_label": GROUP_LABELS.get(r.get("match_group") or "", "Not checked yet"),
        "score": r.get("score"),
        "location": location,
        "remote_type": r.get("remote_type"),
        "remote_label": remote_label,
        "remote_type_label": {"remote": "Remote", "hybrid": "Hybrid", "onsite": "In office"}.get(r.get("remote_type") or "unknown", ""),
        "remote_restriction": r.get("remote_restriction"),
        "salary": r.get("salary_text") or None,
        "freshness": b,
        "freshness_label": flabel,
        "verification": freshness.verification_label(r),
        "availability": r.get("availability"),
        "explanation": r.get("explanation_text"),
        "explanation_source": r.get("explanation_source"),
        "explanation_label": {"rules": "Explained by Hunter's rules (no AI used)", "claude": "Explained by Claude", "pending": "Explanation coming", "failed": "Explained by Hunter's rules (Claude was not available)"}.get(r.get("explanation_source") or "pending", ""),
        "reasons": reasons or expl.get("reasons", []),
        "gaps": expl.get("gaps", []) or json.loads(r.get("gaps_json") or "[]"),
        "next_step": r.get("next_step"),
        "exclusion_reason": r.get("exclusion_reason"),
        "links": {"direct": r.get("direct_url") if r.get("direct_url_status") == "verified" else None, "apply": r.get("apply_url"), "discovery": r.get("discovery_url"), "direct_status": r.get("direct_url_status")},
        "saved": r.get("save_state") == "saved",
        "favorited": r.get("fav_state") == "favorited",
        "dismissed": r.get("dismiss_state") == "dismissed",
        "notes_count": r.get("notes_count") or 0,
        "stage": r.get("app_stage"),
        "applied_at": r.get("app_applied_at"),
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
    s["notes"] = notes.list_for_job(db, job_id)
    s["drafts"] = db.fetchall("SELECT id, kind, status, created_by, created_at, sent_confirmed_at FROM drafts WHERE job_id = ? ORDER BY id DESC", (job_id,))
    pkg = db.fetchone("SELECT content_json FROM drafts WHERE job_id = ? AND kind = 'package' ORDER BY id DESC LIMIT 1", (job_id,))
    s["package"] = json.loads(pkg["content_json"]) if pkg else None
    s["outreach"] = [{**r, "content": json.loads(r["content_json"])} for r in db.fetchall("SELECT id, kind, status, content_json FROM drafts WHERE job_id = ? AND kind IN ('outreach','followup') ORDER BY id DESC LIMIT 5", (job_id,))]
    s["application"] = db.fetchone("SELECT * FROM applications WHERE job_id = ?", (job_id,))
    s["followup"] = db.fetchone("SELECT * FROM followups WHERE application_id = ? AND status = 'pending' ORDER BY due_at LIMIT 1", (s["application"]["id"],)) if s["application"] else None
    s["source_note"] = _source_note(s["observations"])
    s["evidence"] = _evidence_for(db, s)
    return s


def _evidence_for(db: Database, s: dict) -> dict:
    """Approved facts behind the reasons: experience, skills, projects (for 'Why this fits you')."""
    from jobhunter.services import profile as profile_svc

    prof = profile_svc.approved(db)
    out = {"experience": [], "skills": [], "projects": []}
    if not prof:
        return out
    cited = {fid for r in s.get("reasons", []) for fid in r.get("fact_ids", [])}
    by_id = {f.id: f for f in prof["facts"]}
    for fid in cited:
        f = by_id.get(fid)
        if not f:
            continue
        if f.category == "role":
            out["experience"].append(f.text)
        elif f.category == "skill":
            out["skills"].append(f.text)
        elif f.category == "project":
            out["projects"].append(f.text)
    if not out["skills"]:
        desc = (s.get("description") or "").lower()
        out["skills"] = [f.text for f in prof["facts"] if f.category == "skill" and f.text.lower() in desc][:8]
    if not out["projects"]:
        out["projects"] = [f.text for f in prof["facts"] if f.category == "project"][:2]
    return out


def _source_note(observations: list[dict]) -> str | None:
    for o in observations:
        if o["source_id"].startswith("remotive"):
            return "Found via Remotive (remotive.com). Listings there are delayed by 24 hours."
        if o["source_id"].startswith("arbeitnow"):
            return "Found via Arbeitnow (arbeitnow.com)."
        if o["source_id"].startswith("adzuna"):
            return "Found via Adzuna. The description is a snippet."
        if o["source_id"].startswith("import"):
            return "Imported from text or an alert email you gave Hunter."
    return None


# ---------------------------------------------------------------------------
# Dashboard home
# ---------------------------------------------------------------------------
def dashboard(db: Database) -> dict:
    from jobhunter.services import preferences as prefs_svc
    from jobhunter.services import schedule, stats

    totals = stats.totals(db)
    rec = list_jobs(db, view="new", per_page=5)
    last = hunt.last_run(db)
    last_ok = hunt.last_successful_run(db)
    sched = schedule.describe(db)
    sources = hunt.source_health(db)
    checked = [s for s in sources if s.get("last_status") in {"ok", "partial"}]
    failed = [s for s in sources if s.get("enabled") and s.get("last_status") == "failed"]
    att = attention(db)
    prefs = prefs_svc.get(db)
    activity = db.fetchall(
        "SELECT e.kind, e.occurred_at, e.job_id, j.title, j.employer_name FROM events e LEFT JOIN jobs j ON j.id = e.job_id "
        "WHERE e.reverted_by IS NULL AND e.kind IN ('favorited','saved','applied','dismissed','prepared','interview','offer','rejected') ORDER BY e.id DESC LIMIT 8"
    )
    for a in activity:
        a["label"] = actions.EVENT_KINDS.get(a["kind"], a["kind"])
    hunts = db.fetchall("SELECT id, started_at, status, stats_json FROM hunt_runs WHERE trigger IN ('manual','scheduled') ORDER BY id DESC LIMIT 3")
    for h in hunts:
        h["stats"] = json.loads(h.pop("stats_json") or "{}")
    status = "Idle"
    running = db.fetchone("SELECT id FROM hunt_runs WHERE status = 'running'")
    if running:
        status = "Searching now"
    elif str(sched.get("worker", "")).startswith("running"):
        status = "Watching (worker running)"
    return {
        "totals": totals,
        "recommended": rec["items"],
        "recommended_total": rec["total"],
        "status": status,
        "last_run": last,
        "last_success": last_ok,
        "next_run_at": sched.get("next_run_at"),
        "worker": sched.get("worker"),
        "frequency_label": sched.get("frequency_label"),
        "notify_label": sched.get("notify_label"),
        "sources_checked": len(checked),
        "sources_total": len([s for s in sources if s.get("enabled")]),
        "sources_failed": failed,
        "problems": att["count"],
        "attention": att,
        "prefs": {
            "location": prefs.current_location,
            "countries": prefs.work_countries,
            "remote": {"required": "Remote only", "preferred": "Remote preferred", "no_preference": "Any", "unset": "Not set"}.get(prefs.remote, prefs.remote),
            "salary": f"{prefs.salary_min:,.0f} {prefs.salary_currency} / {prefs.salary_period}" if prefs.salary_min else None,
        },
        "activity": activity,
        "hunts": hunts,
    }


def applied_rows(db: Database) -> list[dict]:
    rows = db.fetchall(
        f"SELECT * FROM ({BASE_SELECT}) WHERE app_stage IS NOT NULL AND app_stage != 'prepared' ORDER BY COALESCE(app_applied_at, first_seen_at) DESC",
        (POLICY_VERSION,),
    )
    out = []
    for r in rows:
        s = summarize(r)
        s["note"] = notes.latest_for_job(db, r["id"])
        fu = db.fetchone("SELECT due_at FROM followups WHERE application_id = ? AND status = 'pending' ORDER BY due_at LIMIT 1", (r["app_id"],)) if r.get("app_id") else None
        s["followup_due"] = fu["due_at"] if fu else None
        out.append(s)
    return out


# ---------------------------------------------------------------------------
# Needs attention
# ---------------------------------------------------------------------------
def attention(db: Database) -> dict:
    from jobhunter.services import drafts as drafts_svc
    from jobhunter.services import feedback, onboarding
    from jobhunter.services import profile as profile_svc
    from jobhunter.services.email import gmail, linking

    items: list[dict] = []
    needs_review = db.get_setting("profile.needs_review")
    if needs_review:
        items.append({"kind": "profile_review", "text": "You added a new file. Please check your profile and press yes again.", "action": "jobhunter profile show"})
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

        if not str(describe(db)["worker"]).startswith("running"):
            items.append({"kind": "worker", "text": "Daily searching is on, but the worker is not running, so nothing happens by itself. Start it with one command.", "action": "jobhunter worker run   (or see jobhunter schedule install --print)"})
    g = gmail.describe(db)
    if g["read"]["connected"] and g["read"].get("last_error"):
        items.append({"kind": "mailbox", "text": f"Mailbox sync problem: {g['read']['last_error']}", "action": "jobhunter email sync"})
    nxt = onboarding.next_step(db)
    return {"setup_pending": nxt, "items": items, "count": len(items)}


# ---------------------------------------------------------------------------
# Text renderers for the CLI
# ---------------------------------------------------------------------------
def render_list_text(res: dict) -> str:
    if not res["items"]:
        return {"new": "No new recommended jobs right now. Check 'Search health' if you expected some.", "saved": "Nothing saved yet.", "favorites": "No favorites yet."}.get(res["view"], "Nothing here.")
    lines = [f"{res['view']} · page {res['page']}/{res['pages']} · {res['total']} job(s)"]
    for it in res["items"]:
        flags = ("★ " if it["favorited"] else "") + ("🔖 " if it["saved"] else "")
        lines.append(f"#{it['id']} {flags}{it['title']} — {it['employer']} [{it['group_label']}]")
        lines.append(f"    {it['location']}" + (f" · {it['remote_restriction']}" if it.get("remote_restriction") else "") + f" · Pay: {it['salary'] or 'Not listed'} · {it['freshness_label']}")
        if it.get("explanation"):
            lines.append(f"    {it['explanation']}")
        if it.get("next_step"):
            lines.append(f"    Next: {it['next_step']}")
    return "\n".join(lines)


def render_detail_text(j: dict) -> str:
    lines = [f"#{j['id']} {j['title']} — {j['employer']} [{j['group_label']}]", f"{j['location']}" + (f" · {j['remote_restriction']}" if j.get("remote_restriction") else ""), f"Pay: {j['salary'] or 'Not listed'}", f"{j['freshness_label']} · {j['verification']}"]
    if j.get("explanation"):
        lines.append(f"{j['explanation_label']}: {j['explanation']}")
    for r in j.get("reasons", []):
        lines.append(f"- {r.get('text')}")
    for g in j.get("gaps", []):
        lines.append(f"? {g}")
    if j.get("exclusion_reason"):
        lines.append(f"Left out: {j['exclusion_reason']}")
    links = j["links"]
    lines.append(f"Employer link: {links['direct'] or 'not confirmed'}")
    lines.append(f"Apply link: {links['apply'] or '-'}")
    lines.append(f"Found at: {links['discovery'] or '-'}")
    if j.get("source_note"):
        lines.append(j["source_note"])
    if j.get("notes"):
        lines.append("Your notes:")
        for n in j["notes"]:
            lines.append(f"  [{n['updated_at'][:10]}] {truncate(n['body'], 200)}")
    lines.append(f"Next step: {j.get('next_step') or '-'}")
    lines.append("")
    lines.append(truncate(j.get("description") or "", 3000))
    return "\n".join(lines)


def _unused():  # keep now_iso import meaningful for future use
    return now_iso()
