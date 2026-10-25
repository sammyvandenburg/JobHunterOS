"""Link classified emails to jobs and applications, cautiously.

Confident matches (employer name + title words, or a known ATS job link) create
reversible pipeline events. Anything less asks the user to confirm. A late
automated receipt never moves a pipeline backwards (see actions.set_stage).
"""

from __future__ import annotations

import re

from jobhunter.db import Database
from jobhunter.services import actions
from jobhunter.services.sources.import_text import LINKEDIN_VIEW, parse_alert_email
from jobhunter.util import normalize_company

STAGE_FOR = {"receipt": "applied", "rejection": "rejected", "interview": "interview", "offer": "offer", "recruiter": "replied"}
POLICY_SETTING = "mail.auto_update"
POLICIES = ("receipts", "suggest", "all")
POLICY_WORDS = {
    "receipts": "Automated application receipts may mark a job as applied by themselves (undoable). Rejections, interviews, offers and recruiter replies are suggested for your confirmation.",
    "suggest": "Nothing changes by itself. Every email that looks like a status change waits for your confirmation.",
    "all": "Confident matches (a known job link, or the employer and the title) change the pipeline by themselves, reversibly. Ambiguous ones still wait for you.",
}


def policy(db: Database) -> str:
    p = db.get_setting(POLICY_SETTING) or "receipts"
    return p if p in POLICIES else "receipts"


def set_policy(db: Database, value: str) -> dict:
    if value not in POLICIES:
        raise ValueError(f"Unknown policy '{value}'. Choose receipts, suggest or all.")
    db.set_setting(POLICY_SETTING, value)
    return {"policy": value, "meaning": POLICY_WORDS[value]}


def _may_auto(db: Database, kind: str, automated: bool) -> bool:
    p = policy(db)
    if p == "suggest":
        return False
    if p == "receipts":
        return kind == "receipt" and automated
    return True


def _candidates(db: Database) -> list[dict]:
    return db.fetchall(
        "SELECT j.id, j.title, j.employer_name, j.direct_url, j.apply_url, j.discovery_url, a.id AS application_id, a.stage FROM jobs j "
        "LEFT JOIN applications a ON a.job_id = j.id WHERE a.id IS NOT NULL OR j.id IN (SELECT job_id FROM events WHERE kind IN ('saved','prepared','applied') AND reverted_by IS NULL)"
    )


def find_job(db: Database, sender: str, subject: str, text: str) -> tuple[dict | None, str]:
    """Return (job, confidence) where confidence is high | medium | none. Two jobs at the same employer that share a
    word ("Engineer"): the fuller title match wins; a tie is a question for the person, never a guess."""
    blob = f"{sender}\n{subject}\n{text}".lower()
    plain = re.sub(r"[^a-z0-9 ]+", " ", blob)
    best, best_conf = None, "none"
    scored: list[tuple[float, dict]] = []
    for j in _candidates(db):
        comp = normalize_company(j.get("employer_name"))
        comp_hit = bool(comp) and comp in plain
        for url in (j.get("direct_url"), j.get("apply_url"), j.get("discovery_url")):
            if url and url.lower() in blob:
                return j, "high"
        title_words = [w for w in re.sub(r"[^a-z0-9 ]+", " ", (j.get("title") or "").lower()).split() if len(w) > 3]
        hits = sum(w in plain for w in title_words)
        if comp_hit and title_words and hits >= max(1, len(title_words) // 2):
            scored.append((hits / len(title_words), j))
        elif comp_hit and best_conf == "none":
            best, best_conf = j, "medium"
    if scored:
        scored.sort(key=lambda t: -t[0])
        if len(scored) == 1 or scored[0][0] > scored[1][0]:
            return scored[0][1], "high"
        return scored[0][1], "medium"
    return best, best_conf


def link_message(db: Database, email_id: int) -> dict:
    em = db.fetchone("SELECT * FROM email_messages WHERE id = ?", (email_id,))
    if not em:
        return {"linked": False}
    kind = em["classification"]
    if kind == "alert":
        return _import_alert(db, em)
    if kind in {"other", "outreach_sent"}:
        return {"linked": False, "reason": "not a pipeline event"}
    job, conf = find_job(db, em["from_addr"] or "", em["subject"] or "", em["extract_text"] or em["snippet"] or "")
    if not job:
        db.update("email_messages", {"link_status": "needs_confirmation" if kind in STAGE_FOR else "none"}, "id = ?", (email_id,))
        return {"linked": False, "needs_confirmation": kind in STAGE_FOR}
    if conf == "high" and em["classification_confidence"] in {"high", "medium"} and not (kind == "recruiter" and em["classification_confidence"] == "low") and _may_auto(db, kind, bool(em["is_automated"])):
        return apply_link(db, email_id, job["id"], actor="email")
    db.update("email_messages", {"linked_job_id": job["id"], "link_status": "needs_confirmation"}, "id = ?", (email_id,))
    return {"linked": False, "needs_confirmation": True, "suggested_job_id": job["id"], "policy": policy(db)}


def apply_link(db: Database, email_id: int, job_id: int, actor: str = "email") -> dict:
    em = db.fetchone("SELECT * FROM email_messages WHERE id = ?", (email_id,))
    kind = em["classification"]
    stage = STAGE_FOR.get(kind)
    if not stage:
        db.update("email_messages", {"linked_job_id": job_id, "link_status": "confirmed" if actor == "user" else "auto"}, "id = ?", (email_id,))
        return {"linked": True, "stage": None}
    payload = {"email_id": email_id, "subject": em["subject"], "automated": 1 if em["is_automated"] else 0, "from": em["from_addr"]}
    dedupe_key = f"email:{em['provider_message_id']}"
    if stage == "applied":
        res = actions.mark_applied(db, job_id, applied_at=em["received_at"], source="email", evidence=payload, dedupe_key=dedupe_key)
    elif stage == "replied" and em["is_automated"]:
        # An automated message is never a recruiter reply.
        res = {"stage": None, "note": "automated message recorded without a reply event"}
    else:
        res = actions.set_stage(db, job_id, stage, actor="email", occurred_at=em["received_at"], payload=payload, dedupe_key=dedupe_key)
    ev = db.fetchone("SELECT id FROM events WHERE dedupe_key = ?", (dedupe_key,))
    db.update("email_messages", {"linked_job_id": job_id, "linked_application_id": res.get("application_id"), "link_status": "confirmed" if actor == "user" else "auto", "event_id": ev["id"] if ev else None}, "id = ?", (email_id,))
    return {"linked": True, **res}


def reject_link(db: Database, email_id: int) -> dict:
    db.update("email_messages", {"link_status": "rejected"}, "id = ?", (email_id,))
    return {"rejected": True}


def unlink(db: Database, email_id: int) -> dict:
    """Reverse an inferred update: revert its event and clear the link."""
    em = db.fetchone("SELECT * FROM email_messages WHERE id = ?", (email_id,))
    if em and em.get("event_id"):
        actions.correct(db, em["event_id"], note="email link undone by user")
    db.update("email_messages", {"link_status": "rejected", "linked_job_id": None, "linked_application_id": None, "event_id": None}, "id = ?", (email_id,))
    return {"unlinked": True}


def _import_alert(db: Database, em: dict) -> dict:
    text = em.get("extract_text") or em.get("snippet") or ""
    postings = parse_alert_email(text, source_id="import:alert-email")
    if not postings and not LINKEDIN_VIEW.search(text):
        db.update("email_messages", {"link_status": "none"}, "id = ?", (em["id"],))
        return {"linked": False, "imported": 0, "note": "No job links found in this alert (bodies are only fetched for relevant messages)."}
    from jobhunter.config import AppContext
    from jobhunter.services import hunt

    ctx = AppContext.from_env()
    res = hunt.import_postings(ctx, db, postings, trigger="import") if postings else {"imported": 0}
    db.update("email_messages", {"link_status": "auto"}, "id = ?", (em["id"],))
    return {"linked": False, "imported": res.get("imported", 0)}


def review_queue(db: Database, days: int = 7) -> dict:
    """What the mailbox suggested (waiting for the person) and what it changed by itself lately (undoable)."""
    from datetime import timedelta

    from jobhunter.util import now

    since = (now() - timedelta(days=days)).isoformat()
    suggested = []
    for r in pending_confirmations(db):
        j = r.get("suggested") or {}
        suggested.append({"email_id": r["id"], "kind": r["classification"], "confidence": r["classification_confidence"], "from": r.get("from_addr"), "subject": r.get("subject"), "received_at": r.get("received_at"), "job_id": r.get("linked_job_id"), "job": f"{j.get('title')} at {j.get('employer_name')}" if j else None})
    applied = []
    for r in db.fetchall("SELECT e.*, j.title, j.employer_name FROM email_messages e JOIN jobs j ON j.id = e.linked_job_id WHERE e.link_status = 'auto' AND e.event_id IS NOT NULL AND e.received_at >= ? ORDER BY e.received_at DESC LIMIT 20", (since,)):
        applied.append({"email_id": r["id"], "kind": r["classification"], "from": r.get("from_addr"), "subject": r.get("subject"), "received_at": r.get("received_at"), "job_id": r["linked_job_id"], "job": f"{r['title']} at {r['employer_name']}", "stage": STAGE_FOR.get(r["classification"])})
    return {"policy": policy(db), "meaning": POLICY_WORDS[policy(db)], "suggested": suggested, "applied_by_itself": applied}


def pending_confirmations(db: Database) -> list[dict]:
    rows = db.fetchall("SELECT * FROM email_messages WHERE link_status = 'needs_confirmation' ORDER BY received_at DESC LIMIT 50")
    for r in rows:
        if r.get("linked_job_id"):
            j = db.fetchone("SELECT title, employer_name FROM jobs WHERE id = ?", (r["linked_job_id"],))
            r["suggested"] = j
    return rows


def import_text_message(db: Database, text: str, sender: str = "", subject: str = "", received_at: str | None = None, outgoing: bool = False) -> dict:
    """Manual fallback: paste an email. Deduplicated by content hash."""
    from jobhunter.services.email.classify import classify
    from jobhunter.util import now_iso, sha256_text

    result = classify(sender, subject, text, {}, outgoing=outgoing)
    pid = "import:" + sha256_text(f"{sender}|{subject}|{text}")[:32]
    if db.fetchone("SELECT id FROM email_messages WHERE provider_message_id = ?", (pid,)):
        return {"duplicate": True}
    with db.tx():
        em_id = db.insert(
            "email_messages",
            {
                "provider_message_id": pid,
                "received_at": received_at or now_iso(),
                "from_addr": sender[:200],
                "subject": subject[:300],
                "snippet": text[:300],
                "extract_text": text[:4000],
                "classification": result.kind,
                "classification_confidence": result.confidence,
                "is_automated": 1 if result.automated else 0,
                "origin": "import",
                "fetched_at": now_iso(),
            },
        )
        link = link_message(db, em_id)
    return {"email_id": em_id, "classification": result.kind, "confidence": result.confidence, "automated": result.automated, **link}
