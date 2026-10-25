"""Company marks for the dashboard, fetched once by Hunter, never by every student's browser.

Hunter asks the employer's own verified domain for its favicon during a search (bounded per run), keeps a small
private copy as a data URI, and the dashboard renders that copy. Nothing is guessed from a company's name, a
job-board host is never an employer, and a missing mark falls back to initials. A failed fetch is remembered
and retried only after a month."""

from __future__ import annotations

import base64
from datetime import timedelta

from jobhunter.db import Database
from jobhunter.services import roles
from jobhunter.util import now, now_iso

MAX_LOGO_BYTES = 16 * 1024
DOC_BUDGET_BYTES = 600 * 1024
RECHECK_DAYS = 30
IMAGE_TYPES = ("image/x-icon", "image/vnd.microsoft.icon", "image/png", "image/svg+xml", "image/jpeg", "image/gif", "image/webp", "image/ico", "image/icon")


def domains_to_check(db: Database, limit: int) -> list[str]:
    """Verified employer domains without a fresh mark, most recently seen jobs first."""
    since = (now() - timedelta(days=RECHECK_DAYS)).isoformat()
    known = {r["domain"]: r for r in db.fetchall("SELECT domain, checked_at FROM employer_logos")}
    out: list[str] = []
    for j in db.fetchall("SELECT employer_name, direct_url, direct_url_status, employer_confidential FROM jobs WHERE direct_url_status = 'verified' ORDER BY last_seen_at DESC LIMIT 400"):
        d = roles.company_mark(j["employer_name"], j["direct_url"], j["direct_url_status"], bool(j.get("employer_confidential")))["domain"]
        if not d or d in out:
            continue
        k = known.get(d)
        if k and k["checked_at"] >= since:
            continue
        out.append(d)
        if len(out) >= limit:
            break
    return out


def fetch_one(db: Database, client, domain: str) -> dict:
    ts = now_iso()
    row = {"domain": domain, "data_uri": None, "media_type": None, "bytes": None, "status": "none", "checked_at": ts}
    try:
        resp = client.get(f"https://{domain}/favicon.ico")
        ctype = str((getattr(resp, "headers", {}) or {}).get("content-type") or "").split(";")[0].strip().lower()
        data = getattr(resp, "content", b"") or b""
        if getattr(resp, "status", 200) == 200 and data and len(data) <= MAX_LOGO_BYTES and (ctype in IMAGE_TYPES or (not ctype and data[:4] in (b"\x00\x00\x01\x00", b"\x89PNG"))):
            if not ctype:
                ctype = "image/png" if data[:4] == b"\x89PNG" else "image/x-icon"
            row.update({"data_uri": f"data:{ctype};base64,{base64.b64encode(data).decode('ascii')}", "media_type": ctype, "bytes": len(data), "status": "ok"})
    except Exception:
        row["status"] = "failed"
    db.execute("INSERT OR REPLACE INTO employer_logos (domain, data_uri, media_type, bytes, status, checked_at) VALUES (?, ?, ?, ?, ?, ?)", (row["domain"], row["data_uri"], row["media_type"], row["bytes"], row["status"], row["checked_at"]))
    return row


def refresh(db: Database, client, limit: int = 15) -> dict:
    """Fetch marks for a few employers whose mark is missing or stale. Bounded; never fails the caller."""
    done = []
    for d in domains_to_check(db, limit):
        done.append(fetch_one(db, client, d))
    return {"checked": len(done), "ok": sum(1 for r in done if r["status"] == "ok")}


def doc(db: Database) -> dict:
    """One document for the page: domain -> data URI, within a byte budget (newest first)."""
    out: dict[str, str] = {}
    total = 0
    for r in db.fetchall("SELECT domain, data_uri, bytes FROM employer_logos WHERE status = 'ok' AND data_uri IS NOT NULL ORDER BY checked_at DESC"):
        size = len(r["data_uri"])
        if total + size > DOC_BUDGET_BYTES:
            continue
        out[r["domain"]] = r["data_uri"]
        total += size
    return {"schema": 1, "at": now_iso(), "logos": out, "note": "Each mark is the employer's own favicon from its verified domain, fetched once by Hunter; everything else shows initials."}
