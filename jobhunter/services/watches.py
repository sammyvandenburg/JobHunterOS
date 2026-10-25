"""Watches: what changed on the jobs the person cares about, and a restrained weekly observation.

A watched job is one the person saved, starred, prepared or applied to; a watched company is one they named.
Changes are recorded by the ordinary refresh (a source that lists the job again with different content) and by a
complete employer-board snapshot that no longer lists it. A failed or partial check records nothing: "couldn't
check" is never a change. Material means: pay, place or remote rule, title, the posting text, contract type,
availability, a closing date, the application link. Nothing here rewrites history: the old value stays with the
change, and the person acknowledges what they have seen.

The weekly observation is one plain fact from their own records with a sample-size guard: small numbers get
"too few to conclude", never a causal claim."""

from __future__ import annotations

import json
import re
from datetime import timedelta

from jobhunter.config import AppContext
from jobhunter.db import Database
from jobhunter.util import now, now_iso

COMPANY_WATCH_SETTING = "watches.companies"
OBSERVE_SETTING = "observe.last"
FIELD_WORDS = {
    "salary": "Pay",
    "location": "Location",
    "remote": "Remote or office rule",
    "title": "Title",
    "description": "Posting text",
    "employment_type": "Contract type",
    "availability": "Listing",
    "closing_date": "Closing date",
    "apply_url": "Application link",
}
CLOSING_RX = re.compile(r"\b(?:apply (?:before|by|until)|closing date|application deadline|deadline(?: for applications)?|applications close(?:s)? (?:on|by)|closes on)\s*[:\-]?\s*(?P<date>(?:\d{1,2}(?:st|nd|rd|th)?\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?(?:\s+\d{4})?)|(?:(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+\d{1,2}(?:st|nd|rd|th)?,?(?:\s+\d{4})?)|(?:\d{4}-\d{2}-\d{2})|(?:\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}))", re.I)


def closing_date(text: str | None) -> str | None:
    m = CLOSING_RX.search(text or "")
    return m.group("date").strip() if m else None


def _salary(v: dict) -> str | None:
    if v.get("salary_text"):
        return str(v["salary_text"])
    if v.get("salary_min") or v.get("salary_max"):
        return f"{v.get('salary_min') or '?'}-{v.get('salary_max') or '?'} {v.get('salary_currency') or ''}".strip()
    return None


def _remote(v: dict) -> str | None:
    parts = [v.get("remote_type") if v.get("remote_type") not in (None, "unknown") else None, v.get("remote_restriction")]
    s = " · ".join(p for p in parts if p)
    return s or None


def record_changes(db: Database, job: dict, values: dict, source_id: str | None, ts: str) -> list[dict]:
    """Compare what is stored with what is about to be written (only the keys being written) and record every
    material difference. A missing value is not a change (the authority rules already drop those)."""
    merged = {**job, **values}
    checks = []
    if any(k in values for k in ("salary_text", "salary_min", "salary_max", "salary_currency")):
        checks.append(("salary", _salary(job), _salary(merged)))
    if "location_text" in values:
        checks.append(("location", job.get("location_text"), values.get("location_text")))
    if any(k in values for k in ("remote_type", "remote_restriction")):
        checks.append(("remote", _remote(job), _remote(merged)))
    if "title" in values:
        checks.append(("title", job.get("title"), values.get("title")))
    if "description_hash" in values and values.get("description_hash") != job.get("description_hash"):
        checks.append(("description", str(job.get("description_hash") or "")[:12] or None, str(values.get("description_hash") or "")[:12] or None))
        checks.append(("closing_date", closing_date(job.get("description_text")), closing_date(values.get("description_text"))))
    if "employment_type" in values:
        checks.append(("employment_type", job.get("employment_type"), values.get("employment_type")))
    if "availability" in values:
        checks.append(("availability", job.get("availability"), values.get("availability")))
    if "apply_url" in values:
        checks.append(("apply_url", job.get("apply_url"), values.get("apply_url")))
    out = []
    for field, old, new in checks:
        if (old or None) == (new or None):
            continue
        if field in {"location", "title", "apply_url", "employment_type"} and old and new and str(old).strip().lower() == str(new).strip().lower():
            continue
        row = {"job_id": job["id"], "field": field, "old_value": str(old) if old is not None else None, "new_value": str(new) if new is not None else None, "source_id": source_id, "observed_at": ts, "material": 1, "created_at": ts}
        db.insert("job_changes", row)
        out.append(row)
    return out


# ---------------------------------------------------------------------------
# Watches
# ---------------------------------------------------------------------------
def watched_job_ids(db: Database) -> set[int]:
    saved = {r["job_id"] for r in db.fetchall("SELECT job_id FROM (SELECT job_id, kind FROM events e WHERE kind IN ('saved','unsaved','favorited','unfavorited') AND reverted_by IS NULL AND id = (SELECT MAX(id) FROM events WHERE job_id = e.job_id AND kind IN ('saved','unsaved','favorited','unfavorited') AND reverted_by IS NULL)) WHERE kind IN ('saved','favorited')")}
    apps = {r["job_id"] for r in db.fetchall("SELECT job_id FROM applications WHERE stage NOT IN ('rejected', 'withdrawn')")}
    return saved | apps


def companies(db: Database) -> list[str]:
    return list(db.get_setting(COMPANY_WATCH_SETTING) or [])


def watch_company(db: Database, name: str, on: bool = True) -> dict:
    name = (name or "").strip()[:120]
    if not name:
        raise ValueError("Give a company name.")
    cur = companies(db)
    low = [c.lower() for c in cur]
    if on and name.lower() not in low:
        cur.append(name)
    if not on:
        cur = [c for c in cur if c.lower() != name.lower()]
    db.set_setting(COMPANY_WATCH_SETTING, cur[:50])
    return {"companies": cur[:50]}


def changes(db: Database, days: int = 7, only_watched: bool = True, unacknowledged: bool = True, job_id: int | None = None) -> list[dict]:
    since = (now() - timedelta(days=days)).isoformat()
    rows = db.fetchall("SELECT c.*, j.title, j.employer_name, j.canonical_key FROM job_changes c JOIN jobs j ON j.id = c.job_id WHERE c.observed_at >= ? AND c.material = 1 ORDER BY c.observed_at DESC, c.id DESC LIMIT 200", (since,))
    watched = watched_job_ids(db) if only_watched else None
    out = []
    for r in rows:
        if job_id is not None and r["job_id"] != job_id:
            continue
        if watched is not None and r["job_id"] not in watched:
            continue
        if unacknowledged and r.get("acknowledged_at"):
            continue
        out.append({"id": r["id"], "job_id": r["job_id"], "key": _key(r["canonical_key"]), "job": f"{r['title']} at {r['employer_name'] or 'an employer'}", "field": r["field"], "what": FIELD_WORDS.get(r["field"], r["field"]), "old": r.get("old_value"), "new": r.get("new_value"), "source": r.get("source_id"), "observed_at": r["observed_at"], "acknowledged_at": r.get("acknowledged_at")})
    return out


def _key(canonical_key: str) -> str:
    from jobhunter.services.board import job_key

    return job_key(canonical_key)


def change_line(c: dict) -> str:
    if c["field"] == "availability":
        if c["new"] == "closed":
            return f"{c['job']}: no longer listed where Hunter found it (since {c['observed_at'][:10]}). Not proof it is closed."
        if c["new"] == "open":
            return f"{c['job']}: listed again (since {c['observed_at'][:10]})."
    if c["field"] == "description":
        return f"{c['job']}: the posting text changed ({c['observed_at'][:10]})."
    old = c["old"] or "not stated"
    new = c["new"] or "not stated"
    return f"{c['job']}: {c['what'].lower()} changed from {old} to {new} ({c['observed_at'][:10]})."


def acknowledge(db: Database, job_id: int) -> dict:
    n = db.update("job_changes", {"acknowledged_at": now_iso()}, "job_id = ? AND acknowledged_at IS NULL", (job_id,))
    return {"job_id": job_id, "acknowledged": n}


def new_at_watched_companies(db: Database, days: int = 7) -> list[dict]:
    names = companies(db)
    if not names:
        return []
    since = (now() - timedelta(days=days)).isoformat()
    out = []
    for r in db.fetchall("SELECT id, canonical_key, title, employer_name, first_seen_at, relevance FROM jobs WHERE first_seen_at >= ? AND employer_name IS NOT NULL ORDER BY first_seen_at DESC LIMIT 200", (since,)):
        if (r["employer_name"] or "").lower() in {n.lower() for n in names} and r["relevance"] in {"relevant", "uncertain"}:
            out.append({"job_id": r["id"], "key": _key(r["canonical_key"]), "job": f"{r['title']} at {r['employer_name']}", "first_seen_at": r["first_seen_at"]})
    return out[:10]


def summary(db: Database) -> dict:
    watched = watched_job_ids(db)
    rows = []
    for jid in sorted(watched):
        j = db.fetchone("SELECT id, canonical_key, title, employer_name, availability, last_checked_at, availability_note FROM jobs WHERE id = ?", (jid,))
        if not j:
            continue
        srcs = db.fetchall("SELECT o.source_id, w.last_status, w.last_fetch_at FROM job_observations o LEFT JOIN watch_sources w ON w.id = o.source_id WHERE o.job_id = ?", (jid,))
        unchecked = [s["source_id"] for s in srcs if (s.get("last_status") or "") in {"failed", "partial"}]
        rows.append({"job_id": jid, "key": _key(j["canonical_key"]), "job": f"{j['title']} at {j['employer_name'] or 'an employer'}", "availability": j["availability"], "last_checked_at": j.get("last_checked_at"), "couldnt_check": unchecked, "changes": len(changes(db, days=30, only_watched=False, unacknowledged=True, job_id=jid))})
    return {"watched_jobs": rows, "companies": companies(db), "new_at_companies": new_at_watched_companies(db), "note": "Watched jobs are refreshed by the ordinary search of their sources; a site that could not be checked shows here as such, never as closed."}


# ---------------------------------------------------------------------------
# The weekly observation: one fact, with a guard against small numbers
# ---------------------------------------------------------------------------
MIN_SAMPLE = 5


def observation_due(db: Database) -> bool:
    last = db.get_setting(OBSERVE_SETTING) or {}
    return (last.get("at") or "") < (now() - timedelta(days=7)).isoformat()


def weekly_observation(ctx: AppContext, db: Database, mark: bool = True) -> dict:
    """One observation from the person's own records. Priority: unopened prepared packs, overdue follow-ups, saved
    jobs left undecided, reply rate (only with enough applications), else nothing to say."""
    from jobhunter.services import today as today_svc

    since14 = (now() - timedelta(days=14)).isoformat()
    unopened = today_svc.unopened_ready(db)
    overdue = db.fetchall("SELECT f.id FROM followups f JOIN applications a ON a.id = f.application_id WHERE f.status = 'pending' AND f.due_at < ?", ((now() - timedelta(days=3)).isoformat(),))
    saved_old = db.fetchall(
        "SELECT j.id FROM events e JOIN jobs j ON j.id = e.job_id WHERE e.kind = 'saved' AND e.reverted_by IS NULL AND e.occurred_at < ? "
        "AND NOT EXISTS (SELECT 1 FROM events u WHERE u.job_id = j.id AND u.kind = 'unsaved' AND u.id > e.id) AND NOT EXISTS (SELECT 1 FROM applications a WHERE a.job_id = j.id AND a.applied_at IS NOT NULL)",
        (since14,),
    )
    applied = db.fetchone("SELECT COUNT(*) AS n FROM applications WHERE applied_at IS NOT NULL AND applied_at >= ?", ((now() - timedelta(days=60)).isoformat(),))["n"]
    replied = db.fetchone("SELECT COUNT(DISTINCT job_id) AS n FROM events WHERE kind IN ('reply_received', 'interview', 'offer') AND reverted_by IS NULL AND occurred_at >= ?", ((now() - timedelta(days=60)).isoformat(),))["n"]
    prepared = db.fetchone("SELECT COUNT(*) AS n FROM applications WHERE stage = 'prepared' AND applied_at IS NULL")["n"]
    text, kind = None, None
    if len(unopened) >= 3:
        kind, text = "unopened", f"{len(unopened)} prepared applications are waiting unopened. Open one and decide: apply, or not interested. Hunter prepares fewer while they wait."
    elif overdue:
        kind, text = "followups", f"{len(overdue)} follow-up{'s are' if len(overdue) != 1 else ' is'} more than three days overdue. A short message or 'Stop reminding' clears it."
    elif len(saved_old) >= 3:
        kind, text = "saved", f"{len(saved_old)} saved jobs have waited more than two weeks. Decide on them this week: prepare, or let them go."
    elif applied >= MIN_SAMPLE:
        kind, text = "replies", f"In the last 60 days: {applied} applications, {replied} with a reply, interview or offer. That is a fact, not a verdict; the sample is small, so change one thing at a time if you want to learn from it."
    elif applied:
        kind, text = "early", f"{applied} application{'s' if applied != 1 else ''} in the last 60 days: too few to conclude anything about replies. Keep going; Hunter will say more after {MIN_SAMPLE}."
    elif prepared:
        kind, text = "prepared", f"{prepared} application{'s are' if prepared != 1 else ' is'} prepared and not yet sent. The next step is yours: open one and apply on the employer's page."
    out = {"at": now_iso(), "kind": kind, "text": text, "numbers": {"unopened": len(unopened), "overdue_followups": len(overdue), "saved_undecided": len(saved_old), "applied_60d": applied, "replies_60d": replied, "prepared": prepared}, "rule": "One observation a week, from your own records, no causal claims from small numbers."}
    if mark and text:
        db.set_setting(OBSERVE_SETTING, {"at": out["at"], "kind": kind})
    return out


def to_json(obj) -> str:
    return json.dumps(obj, indent=2)
