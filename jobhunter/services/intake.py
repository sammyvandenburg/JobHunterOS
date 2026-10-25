"""One intake for everything that arrives by hand: a link, a pasted description, a job-alert email, a recruiter
message, a referral from someone the person knows. Each becomes the same kind of vacancy record the hunt makes,
through the same identity and dedupe rules, so a Greenhouse link pasted today merges with the board's own posting
found tomorrow, while a different job stays a different job. Source differences are kept as observations. A
recruiter or referrer becomes a contact with its provenance; the message itself joins the timeline. Nothing is
fetched from the link and nothing is sent to anyone."""

from __future__ import annotations

import json
import re

from jobhunter.config import AppContext
from jobhunter.db import Database
from jobhunter.models import RawPosting
from jobhunter.services import hunt, workspace
from jobhunter.services.email import linking
from jobhunter.services.email.classify import classify
from jobhunter.services.sources.import_text import (
    URL_RE,
    _context,
    clean_url,
    detect_ats_link,
    html_to_text_with_links,
    looks_like_html,
    parse_alert_email,
    posting_from_text,
)
from jobhunter.util import now_iso

KINDS = ("link", "description", "alert", "recruiter", "referral")
REFERRAL_RX = re.compile(r"\b(referr(?:al|ed)|recommend(?:ed|s)? (?:me|you)|my (?:friend|colleague|former (?:colleague|manager)|mentor|contact)|told me about|passed (?:this|me) along|forwarded)\b", re.I)
RECRUITER_RX = re.compile(r"\b(recruiter|talent|hiring manager|came across your profile|your profile|we are hiring|we're hiring|open (?:role|position)|opportunity (?:at|with)|would you be (?:open|interested))\b", re.I)
TITLE_RX = re.compile(r"\b(?:role|position|vacancy|opening|job)\s+(?:of|as|for)?\s*(?:an?\s+)?(?P<title>(?:senior |lead |principal |staff |junior |medior )?(?:devops|platform|site reliability|sre|cloud|infrastructure|systems?|linux|kubernetes|network|security|release|build|observability|production|reliability)[\w /-]{0,40}?(?:engineer|administrator|architect|specialist|consultant|developer|lead|manager))\b", re.I)
COMPANY_RX = re.compile(r"\b(?:at|with|for|from|@)\s+(?P<company>[A-Z][\w&.'-]*(?:\s+[A-Z][\w&.'-]*){0,3})(?=[\s,.;:!?)]|$)")
SIGNOFF_RX = re.compile(r"(?:^|\n)\s*(?:best|kind regards|regards|cheers|thanks|thank you|warm regards|met vriendelijke groet|groeten)[,!.]?\s*\n+\s*(?P<name>[A-Z][a-z]+(?: [A-Z][\w'-]+){0,2})\s*$", re.I | re.M)


class IntakeError(Exception):
    pass


def detect_kind(text: str, sender: str = "", subject: str = "", url: str | None = None) -> str:
    body = (text or "").strip()
    if url and not body:
        return "link"
    if body and URL_RE.fullmatch(body):
        return "link"
    c = classify(sender, subject, body, {}, outgoing=False)
    if c.kind == "alert":
        return "alert"
    if REFERRAL_RX.search(body[:600]) and not c.automated:
        return "referral"
    if c.kind == "recruiter" or (sender and not c.automated and RECRUITER_RX.search(body[:800])):
        return "recruiter"
    return "description"


TITLE_FIRST_RX = re.compile(r"\b(?P<title>(?:senior |lead |principal |staff |junior |medior )?(?:devops|platform|site reliability|sre|cloud|infrastructure|systems?|linux|kubernetes|network|security|release|build|observability|production|reliability)[\w /-]{0,40}?(?:engineer|administrator|architect|specialist|consultant|developer|lead|manager))\s+(?:role|position|vacancy|opening|job)\b", re.I)


def _guess_title(text: str, subject: str) -> str | None:
    for blob in (subject or "", text or ""):
        m = TITLE_FIRST_RX.search(blob) or TITLE_RX.search(blob)
        if m:
            return m.group("title").strip()[:120]
    return None


def _guess_company(text: str, subject: str, sender: str) -> str | None:
    for blob in (subject or "", (text or "")[:800]):
        for m in COMPANY_RX.finditer(blob):
            cand = m.group("company").strip()
            if cand.lower() not in {"the", "our", "you", "me", "i", "a", "an", "this", "that", "hi", "hello", "dear"} and not RECRUITER_RX.search(cand):
                return cand[:120]
    m = re.search(r"@([a-z0-9-]+)\.", sender or "", re.I)
    if m and m.group(1).lower() not in {"gmail", "outlook", "hotmail", "yahoo", "icloud", "proton", "protonmail", "live", "mail"}:
        return m.group(1).capitalize()
    return None


def _sender_name(sender: str, text: str, from_name: str | None) -> str | None:
    if from_name:
        return from_name.strip()[:120]
    m = re.match(r"\s*\"?([^\"<]+?)\"?\s*<", sender or "")
    if m:
        return m.group(1).strip()[:120]
    m = SIGNOFF_RX.search(text or "")
    return m.group("name").strip()[:120] if m else None


def _link_posting(url: str, text: str, title: str | None, company: str | None, kind: str) -> RawPosting:
    """A pasted ATS link carries the board's own identity (source and id), so the adapter's posting is the same
    job. Nothing is fetched here; the title and company are what the person said or the link shows."""
    info = detect_ats_link(url)
    body = html_to_text_with_links(text) if looks_like_html(text) else (text or "")
    if info:
        token = info.get("token") or info.get("site") or info.get("board")
        source_id = f"{info['kind']}:{token}"
        return RawPosting(source_id=source_id, external_id=str(info["job_id"]), title=(title or f"Vacancy {info['job_id']} at {token}")[:200], employer_name=company or token.replace("-", " ").title(), url=clean_url(url), description_text=body or f"Pasted link: {url}", is_employer_hosted=True, raw={"imported_from": kind})
    return posting_from_text(body or f"Pasted link: {url}", title, company, url, source_id=f"import:{kind}")


def _ats_links_in(body: str) -> list[RawPosting]:
    """Employer-board links inside an alert or a message (Greenhouse, Lever, Ashby): each gets the board's own
    identity, with the title, company and location read from the lines around it."""
    lines = [ln.strip() for ln in (body or "").splitlines()]
    seen: set[str] = set()
    out: list[RawPosting] = []
    for idx, line in enumerate(lines):
        for m in URL_RE.finditer(line):
            info = detect_ats_link(m.group(0))
            if not info:
                continue
            key = f"{info['kind']}:{info.get('token') or info.get('site') or info.get('board')}:{info['job_id']}"
            if key in seen:
                continue
            seen.add(key)
            title, company, location = _context(lines, idx, line)
            p = _link_posting(m.group(0), "", title, company, "alert")
            if location:
                p = p.model_copy(update={"location_text": location})
            out.append(p)
    return out


def intake(ctx: AppContext, db: Database, text: str = "", kind: str | None = None, sender: str = "", subject: str = "", url: str | None = None, title: str | None = None, company: str | None = None, location: str | None = None, from_name: str | None = None, received_at: str | None = None) -> dict:
    text = (text or "").strip()
    if not text and not url:
        raise IntakeError("Paste the link, the description or the message.")
    kind = kind or detect_kind(text, sender, subject, url)
    if kind not in KINDS:
        raise IntakeError(f"Unknown intake kind '{kind}'.")
    body = html_to_text_with_links(text) if looks_like_html(text) else text
    postings: list[RawPosting] = []
    note = None
    if kind == "alert":
        postings = parse_alert_email(text, source_id="import:alert-email") + _ats_links_in(body)
        note = "From a job-alert email: posting dates are unknown unless the email states them." if postings else "No job links found in this alert."
    elif kind == "link" or (url and kind == "description"):
        link = url or (URL_RE.search(body).group(0) if URL_RE.search(body) else None)
        if not link:
            raise IntakeError("No link found.")
        postings = [_link_posting(link, body if body != link else "", title, company, kind)]
        note = "The link is stored, not fetched. The next search of that board (or `jobs lookup`) fills in the rest." if detect_ats_link(link) else "The link is stored, not fetched."
    elif kind in {"recruiter", "referral"}:
        link = url or (URL_RE.search(body).group(0) if URL_RE.search(body) else None)
        t = title or _guess_title(body, subject)
        c = company or _guess_company(body, subject, sender)
        if link and detect_ats_link(link):
            postings = [_link_posting(link, body, t, c, kind)]
        else:
            if not t and not c:
                raise IntakeError("Hunter could not tell which job or company this is about. Say it: --title and --company.")
            postings = [posting_from_text(body, t or f"Role at {c}", c, link, location, source_id=f"import:{kind}")]
        note = "Posting dates are unknown for a pasted message." if not link else None
    else:
        postings = [posting_from_text(body, title, company, url, location, source_id="import:description")]
    before = {p.external_id: db.fetchone("SELECT j.id FROM jobs j JOIN job_observations o ON o.job_id = j.id WHERE o.source_id = ? AND o.external_id = ?", (p.source_id, p.external_id)) for p in postings}
    res = hunt.import_postings(ctx, db, postings, trigger="intake") if postings else {"job_ids": [], "jobs_new": 0}
    job_ids = res.get("job_ids") or []
    new_ids = [jid for p, jid in zip(postings, job_ids, strict=False) if not before.get(p.external_id)]
    contact_ids: list[int] = []
    email_id = None
    if kind in {"recruiter", "referral"} and job_ids:
        who = _sender_name(sender, body, from_name)
        jid = job_ids[0]
        if who:
            exists = db.fetchone("SELECT id FROM contacts WHERE job_id = ? AND name = ?", (jid, who[:200]))
            if exists:
                contact_ids.append(exists["id"])
            else:
                ctype = "recruiter" if kind == "recruiter" else "connection"
                relevance = "Wrote to you about this role (message you pasted)." if kind == "recruiter" else "Referred you to this role (message you pasted)."
                route = sender.strip()[:400] if re.fullmatch(r"[^@\s<>]+@[^@\s<>]+", sender.strip()) else None
                c = workspace._add_contact(db, jid, ctype, who, None, relevance, None, "user", route=route)
                contact_ids.append(c["id"])
        msg = linking.import_text_message(db, body, sender=sender or (who or ""), subject=subject or f"{kind.title()} message about {postings[0].title}", received_at=received_at)
        email_id = msg.get("email_id")
        if email_id and not msg.get("duplicate"):
            db.update("email_messages", {"linked_job_id": jid, "link_status": "confirmed", "classification": "recruiter" if kind == "recruiter" else "other"}, "id = ?", (email_id,))
    row_id = db.insert("intakes", {"kind": kind, "received_at": received_at or now_iso(), "job_ids_json": json.dumps(job_ids), "new_job_ids_json": json.dumps(new_ids), "contact_ids_json": json.dumps(contact_ids), "email_id": email_id, "from_name": from_name or _sender_name(sender, body, None), "note": note, "created_at": now_iso()})
    jobs = [db.fetchone("SELECT id, canonical_key, title, employer_name FROM jobs WHERE id = ?", (j,)) for j in job_ids]
    return {"intake_id": row_id, "kind": kind, "jobs": [{"id": j["id"], "title": j["title"], "employer": j.get("employer_name"), "new": j["id"] in new_ids, "merged_into_existing": j["id"] not in new_ids} for j in jobs if j], "contacts": contact_ids, "email_id": email_id, "note": note, "matching": res.get("matching")}


def history(db: Database, job_id: int | None = None, limit: int = 30) -> list[dict]:
    rows = db.fetchall("SELECT * FROM intakes ORDER BY id DESC LIMIT ?", (limit,))
    out = []
    for r in rows:
        ids = json.loads(r["job_ids_json"] or "[]")
        if job_id is not None and job_id not in ids:
            continue
        out.append({"id": r["id"], "kind": r["kind"], "received_at": r["received_at"], "job_ids": ids, "new_job_ids": json.loads(r["new_job_ids_json"] or "[]"), "contacts": json.loads(r["contact_ids_json"] or "[]"), "from_name": r.get("from_name"), "note": r.get("note")})
    return out


def provenance_line(db: Database, job_id: int) -> str | None:
    """For the hand-off: how this job reached Hunter by hand, if it did."""
    rows = history(db, job_id, limit=200)
    if not rows:
        return None
    r = rows[-1]
    words = {"link": "a link you pasted", "description": "a description you pasted", "alert": "a job-alert email you imported", "recruiter": f"a recruiter message{(' from ' + r['from_name']) if r.get('from_name') else ''} you pasted", "referral": f"a referral{(' from ' + r['from_name']) if r.get('from_name') else ''} you pasted"}
    return f"How it reached Hunter: {words.get(r['kind'], r['kind'])} on {r['received_at'][:10]}."
