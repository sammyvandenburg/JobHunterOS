"""Assisted Apply: one supported lane, every step visible, nothing submitted without one bound approval.

Capabilities, per destination (the registry below): prepare_only (Hunter prepares; the person applies on the
site), assisted_fill (Hunter opens the form in a browser on this computer, fills what it knows, reads it back; the
person checks and presses the site's own submit), reviewed_submit (as assisted_fill, then the person approves the
exact read-back with a single-use code and Hunter presses submit once, keeping the receipt), manual_only (sites
whose terms or forms rule automation out: the pack is still there to copy from).

What is filled: the person's confirmed identity (never guessed from the CV), the exact CV version chosen for the
job (bytes verified against its hash), their own answers and letter (never Hunter's drafts), nothing for a
question they left open. Consent boxes are listed and ticked only after the approval that names them.
Crash safety: a run that was told to submit keeps a lease; if the process dies the run reads "uncertain" and no
second click happens until the person says what they saw. A cloud session has no browser of the person's: the
lane refuses there and says so."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlparse

from jobhunter.config import AppContext
from jobhunter.db import Database
from jobhunter.services import actions, workspace
from jobhunter.services import documents as docs
from jobhunter.services import drafts as drafts_svc
from jobhunter.services.sources.registry import ATS_HOSTS
from jobhunter.util import now, now_iso

IDENTITY_SETTING = "assisted.identity"
SUBMIT_HOSTS_SETTING = "assisted.submit_hosts"  # hosts where reviewed_submit is allowed; empty by default
CAPABILITY_SETTING = "capabilities.assisted_apply"
APPROVAL_MINUTES = 15
SUBMIT_LEASE_SECONDS = 180
MAX_FIELD_CHARS = 5000

CAPABILITY_LABELS = {
    "prepare_only": "Prepare only: Hunter prepares, you apply on the site",
    "assisted_fill": "Assisted fill: Hunter fills the form on your computer, you check and submit",
    "reviewed_submit": "Reviewed submit: Hunter fills, you approve the exact read-back, Hunter presses submit once",
    "manual_only": "Manual only: copy from your pack; this site does not allow automation",
}
MANUAL_HOSTS = {"www.linkedin.com", "linkedin.com", "www.indeed.com", "indeed.com", "nl.indeed.com", "de.indeed.com", "uk.indeed.com"}
# Which ATS forms the generic filler knows. "simulated" means verified on a local form shaped like the vendor's,
# never on the vendor's live site; nothing here was verified on a real employer's form in this build.
FORM_SUPPORT = {"greenhouse": "simulated", "fixture": "simulated"}


class AssistedError(Exception):
    pass


def _host(url: str | None) -> str:
    try:
        return (urlparse(url or "").hostname or "").lower()
    except ValueError:
        return ""


# ---------------------------------------------------------------------------
# Capability registry
# ---------------------------------------------------------------------------
def capability_for(db: Database, job: dict) -> dict:
    """What Hunter may do for this destination, and why. The person's own settings only widen reviewed_submit to
    hosts they named; nothing here submits by itself."""
    url = job.get("apply_url") or job.get("direct_url") or job.get("discovery_url")
    host = _host(url)
    submit_hosts = set(db.get_setting(SUBMIT_HOSTS_SETTING) or [])
    if not url:
        return _cap("prepare_only", "No application page is known for this job.", url, None)
    if host in MANUAL_HOSTS or host.endswith(".linkedin.com") or host.endswith(".indeed.com"):
        return _cap("manual_only", "This site's terms rule out automated forms. Copy from your pack and apply there yourself.", url, None)
    ats = ATS_HOSTS.get(host) or ("fixture" if host in submit_hosts and host.startswith("127.0.0.1") else None)
    support = FORM_SUPPORT.get(ats or "")
    if not support:
        return _cap("prepare_only", f"Hunter does not know this form ({host}). It prepares; you apply on the site.", url, ats)
    if host in submit_hosts:
        return _cap("reviewed_submit", f"You allowed Hunter to press submit on {host} after your approval of the exact read-back.", url, ats, verified=support)
    return _cap("assisted_fill", f"Hunter can fill this {ats} form on your computer and read it back; you press submit on the site.", url, ats, verified=support)


def _cap(cid: str, note: str, url: str | None, ats: str | None, verified: str | None = None) -> dict:
    return {"id": cid, "label": CAPABILITY_LABELS[cid], "note": note, "destination": url, "ats": ats, "verified": verified or "not verified", "needs_local_browser": cid in {"assisted_fill", "reviewed_submit"}}


def allow_submit_host(db: Database, host: str, allow: bool = True) -> dict:
    host = _host("https://" + host) if "://" not in host else _host(host)
    if not host:
        raise AssistedError("Give a host name, such as job-boards.greenhouse.io.")
    cur = set(db.get_setting(SUBMIT_HOSTS_SETTING) or [])
    (cur.add if allow else cur.discard)(host)
    db.set_setting(SUBMIT_HOSTS_SETTING, sorted(cur))
    return {"submit_hosts": sorted(cur)}


# ---------------------------------------------------------------------------
# Identity: confirmed by the person, never guessed
# ---------------------------------------------------------------------------
def set_identity(db: Database, first_name: str, last_name: str, email: str, phone: str | None = None) -> dict:
    if not (first_name or "").strip() or not (last_name or "").strip():
        raise AssistedError("First and last name are needed, as you want them on the form.")
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", (email or "").strip()):
        raise AssistedError("That does not look like an email address.")
    ident = {"first_name": first_name.strip()[:80], "last_name": last_name.strip()[:80], "email": email.strip()[:200], "phone": (phone or "").strip()[:40] or None, "confirmed_at": now_iso()}
    db.set_setting(IDENTITY_SETTING, ident)
    return ident


def identity(db: Database) -> dict | None:
    return db.get_setting(IDENTITY_SETTING)


def browser_available() -> tuple[bool, str | None]:
    try:
        import playwright.sync_api  # noqa: F401
    except Exception:
        return False, "Playwright is not installed on this computer (uv sync --extra browser)."
    return True, None


# ---------------------------------------------------------------------------
# The lane: preflight -> permit -> fill (read-back) -> review -> approve -> submit (receipt)
# ---------------------------------------------------------------------------
def _run_key() -> str:
    return "ar" + secrets.token_hex(5)


def _row(db: Database, run_key: str) -> dict:
    r = db.fetchone("SELECT * FROM assisted_runs WHERE run_key = ?", (run_key,))
    if not r:
        raise AssistedError(f"No assisted run {run_key}.")
    return r


def _view(r: dict) -> dict:
    out = {k: r.get(k) for k in ("run_key", "job_id", "capability", "destination", "state", "approval_expires_at", "approved_at", "consumed_at", "error", "created_at", "updated_at")}
    for k in ("permission_json", "readback_json", "receipt_json"):
        out[k[:-5]] = json.loads(r[k]) if r.get(k) else None
    out["readback_hash"] = r.get("readback_hash")
    return out


def _update(db: Database, run_key: str, **values) -> dict:
    values["updated_at"] = now_iso()
    db.update("assisted_runs", values, "run_key = ?", (run_key,))
    return _view(_row(db, run_key))


def current_run(db: Database, job_id: int) -> dict | None:
    r = db.fetchone("SELECT * FROM assisted_runs WHERE job_id = ? ORDER BY id DESC LIMIT 1", (job_id,))
    return _view(r) if r else None


def recover_stale(db: Database) -> list[dict]:
    """A submit that was in flight when the process died: nobody knows whether the click landed. Say so; never
    click again by itself."""
    out = []
    for r in db.fetchall("SELECT * FROM assisted_runs WHERE state = 'submitting' AND lease_until < ?", (now_iso(),)):
        out.append(_update(db, r["run_key"], state="uncertain", lease_until=None, error="Hunter was interrupted while submitting. Check the site or your inbox, then tell Hunter: `jobhunter assisted resolve <job> submitted|not_submitted`."))
    return out


def preflight(ctx: AppContext, db: Database, job_id: int) -> dict:
    """Everything that must be true before a browser opens, with the reasons when it is not."""
    job = db.fetchone("SELECT * FROM jobs WHERE id = ?", (job_id,))
    if not job:
        raise AssistedError(f"Job {job_id} not found.")
    recover_stale(db)
    cap = capability_for(db, job)
    blockers: list[str] = []
    if cap["id"] in {"prepare_only", "manual_only"}:
        blockers.append(cap["note"])
    if db.get_setting("cloud.role") == "cloud":
        blockers.append("This is a cloud session: it has no browser of yours. Run this on your own computer.")
    ok, why = browser_available()
    if not ok:
        blockers.append(why)
    ident = identity(db)
    if not ident:
        blockers.append("Confirm the name, email and phone to put on forms: `jobhunter assisted identity --first ... --last ... --email ... [--phone ...]`.")
    pkg = drafts_svc.current_package(db, job_id)
    if not pkg:
        blockers.append("Prepare the application first.")
    rd = workspace.readiness(db, job_id) if pkg else {"state": "none", "missing": []}
    cv = drafts_svc.selected_cv(db, job_id)
    cv_file = None
    if cv:
        try:
            f = docs.cv_file(db, cv["revision_key"])
            cv_file = {"revision_key": f["revision_key"], "label": f["label"], "number": f["number"], "filename": f["filename"], "sha256": f["sha256"], "bytes": f["bytes"]}
        except docs.DocumentError as exc:
            blockers.append(str(exc))
    else:
        blockers.append("Choose a CV for this job.")
    existing = current_run(db, job_id)
    if existing and existing["state"] in {"submitting", "uncertain"}:
        blockers.append(f"An earlier attempt is {existing['state']}: {existing.get('error') or 'say what you saw first'} (`jobhunter assisted resolve`).")
    if existing and existing["state"] == "submitted":
        blockers.append("This application was already submitted through Hunter. Nothing is sent twice.")
    app = db.fetchone("SELECT applied_at FROM applications WHERE job_id = ?", (job_id,))
    if app and app.get("applied_at"):
        blockers.append("You already applied to this job (recorded). Nothing is sent twice.")
    if not blockers and cap["needs_local_browser"]:
        db.set_setting(CAPABILITY_SETTING, {"at": now_iso(), "note": "A local browser and a confirmed identity were present at the last preflight."})
    return {"job_id": job_id, "capability": cap, "identity": {k: v for k, v in (ident or {}).items() if k != "confirmed_at"} if ident else None, "cv": cv_file, "readiness": rd, "blockers": blockers, "ok": not blockers}


def permit(ctx: AppContext, db: Database, job_id: int) -> dict:
    """The person allows Hunter to open the form, fill it and upload this exact CV version. Nothing is sent."""
    pf = preflight(ctx, db, job_id)
    if not pf["ok"]:
        raise AssistedError(" ".join(pf["blockers"]))
    key = _run_key()
    ts = now_iso()
    perm = {"fill": True, "upload_cv": pf["cv"], "identity": pf["identity"], "at": ts}
    with db.tx():
        app = db.fetchone("SELECT id FROM applications WHERE job_id = ?", (job_id,))
        db.insert("assisted_runs", {"run_key": key, "job_id": job_id, "application_id": app["id"] if app else None, "capability": pf["capability"]["id"], "destination": pf["capability"]["destination"], "state": "permitted", "permission_json": json.dumps(perm), "created_at": ts, "updated_at": ts})
    return _view(_row(db, key))


# ---- what goes into the form -------------------------------------------------
FIELD_RULES = [
    ("first_name", re.compile(r"first.?name|given.?name|voornaam|^first$", re.I)),
    ("last_name", re.compile(r"last.?name|family.?name|surname|achternaam|^last$", re.I)),
    ("full_name", re.compile(r"^(your |full )?name$|^name$|volledige naam", re.I)),
    ("email", re.compile(r"e-?mail", re.I)),
    ("phone", re.compile(r"phone|telefoon|mobile|tel\b", re.I)),
    ("resume", re.compile(r"resume|résumé|\bcv\b|curriculum", re.I)),
    ("cover_letter", re.compile(r"cover.?letter|motivation|motivatie", re.I)),
]
CONSENT_RX = re.compile(r"privacy|consent|agree|terms|gdpr|data protection|i accept|akkoord", re.I)


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def plan_fill(db: Database, job_id: int, fields: list[dict]) -> dict:
    """Decide what goes into each form field from what the person confirmed. `fields` describe the form as the
    browser sees it (name, id, type, label, placeholder, autocomplete, required, options). Returns values to set,
    the file to upload, consents to list, and what stays blank and why. Deterministic and auditable."""
    ident = identity(db) or {}
    cv = drafts_svc.selected_cv(db, job_id)
    mine = {_norm(r["question"] or ""): r for r in workspace.user_texts(db, job_id, "answer") if (r.get("text") or "").strip()}
    letter = workspace.user_text(db, job_id, "letter", "letter")
    letter_text = (letter or {}).get("text") or ""
    values: list[dict] = []
    blanks: list[dict] = []
    consents: list[dict] = []
    upload: dict | None = None
    file_inputs = [f for f in fields if f.get("type") == "file"]
    for f in fields:
        key = " ".join(str(f.get(k) or "") for k in ("name", "id", "label", "placeholder", "autocomplete", "aria_label"))
        label = f.get("label") or f.get("placeholder") or f.get("name") or f.get("id") or "field"
        ftype = (f.get("type") or "text").lower()
        locator = f.get("locator") or f.get("id") or f.get("name")
        if ftype in {"hidden", "submit", "button"}:
            continue
        if ftype == "checkbox":
            if CONSENT_RX.search(key):
                consents.append({"label": label, "locator": locator, "required": bool(f.get("required"))})
            else:
                blanks.append({"label": label, "why": "A box only you can tick."})
            continue
        if ftype == "file":
            if (FIELD_RULES[5][1].search(key) or len(file_inputs) == 1) and cv and not upload:
                upload = {"label": label, "locator": locator, "revision_key": cv["revision_key"]}
            else:
                blanks.append({"label": label, "why": "Hunter uploads only your chosen CV, nothing else."})
            continue
        if ftype in {"select", "select-one", "radio"}:
            blanks.append({"label": label, "why": "A choice only you can make." + (" It is required: choose it on the form." if f.get("required") else ""), "required": bool(f.get("required"))})
            continue
        matched = None
        for fid, rx in FIELD_RULES:
            if fid in {"resume"}:
                continue
            if rx.search(key) or (f.get("autocomplete") or "") in {"given-name" if fid == "first_name" else "", "family-name" if fid == "last_name" else "", "email" if fid == "email" else "", "tel" if fid == "phone" else ""} - {""}:
                matched = fid
                break
        if ftype == "email":
            matched = "email"
        if ftype == "tel":
            matched = "phone"
        if matched == "first_name" and ident.get("first_name"):
            values.append({"label": label, "locator": locator, "value": ident["first_name"], "source": "your identity"})
        elif matched == "last_name" and ident.get("last_name"):
            values.append({"label": label, "locator": locator, "value": ident["last_name"], "source": "your identity"})
        elif matched == "full_name" and ident.get("first_name"):
            values.append({"label": label, "locator": locator, "value": f"{ident['first_name']} {ident['last_name']}".strip(), "source": "your identity"})
        elif matched == "email" and ident.get("email"):
            values.append({"label": label, "locator": locator, "value": ident["email"], "source": "your identity"})
        elif matched == "phone":
            if ident.get("phone"):
                values.append({"label": label, "locator": locator, "value": ident["phone"], "source": "your identity"})
            else:
                blanks.append({"label": label, "why": "No phone number confirmed."})
        elif matched == "cover_letter":
            if letter_text:
                values.append({"label": label, "locator": locator, "value": letter_text[:MAX_FIELD_CHARS], "source": "your letter"})
            else:
                blanks.append({"label": label, "why": "You have not written a letter; Hunter's drafts are never sent."})
        else:
            q = mine.get(_norm(label))
            if q:
                values.append({"label": label, "locator": locator, "value": (q["text"] or "")[:MAX_FIELD_CHARS], "source": "your answer", "answer_id": q["key"]})
            else:
                blanks.append({"label": label, "why": "No answer of yours for this question." + (" It is required: fill it on the form." if f.get("required") else ""), "required": bool(f.get("required"))})
    return {"values": values, "upload": upload, "consents": consents, "blanks": blanks}


# ---- the browser driver -------------------------------------------------------
class BrowserDriver:
    """Playwright on this computer. Headed by default so the person watches; headless in tests."""

    def __init__(self, headless: bool = False, executable_path: str | None = None):
        import os

        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        candidates = [executable_path or os.environ.get("JOBHUNTER_CHROMIUM") or None]
        bundled = os.path.join(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", ""), "chromium")
        if not candidates[0] and os.environ.get("PLAYWRIGHT_BROWSERS_PATH") and os.path.isfile(bundled) and os.access(bundled, os.X_OK):
            candidates.append(bundled)  # a pre-installed Chromium next to the Playwright-managed ones
        last: Exception | None = None
        for cand in candidates:
            try:
                self.browser = self._pw.chromium.launch(headless=headless, **({"executable_path": cand} if cand else {}))
                break
            except Exception as exc:
                last = exc
        else:
            self._pw.stop()
            raise AssistedError(f"No browser could be started on this computer ({last}). Install Playwright's Chromium (uv run playwright install chromium) or set JOBHUNTER_CHROMIUM.")
        self.page = self.browser.new_page()

    def open(self, url: str) -> None:
        self.page.goto(url, wait_until="domcontentloaded", timeout=30000)

    def fields(self) -> list[dict]:
        return self.page.evaluate(
            """() => Array.from(document.querySelectorAll('input, textarea, select')).map((e, i) => {
              const id = e.id || ''; let label = '';
              if (id) { const l = document.querySelector('label[for="' + CSS.escape(id) + '"]'); if (l) label = l.textContent.trim(); }
              if (!label) { const p = e.closest('label'); if (p) label = p.textContent.trim(); }
              if (!label && e.getAttribute('aria-labelledby')) { const l = document.getElementById(e.getAttribute('aria-labelledby')); if (l) label = l.textContent.trim(); }
              e.setAttribute('data-hunter', 'f' + i);
              return { locator: '[data-hunter="f' + i + '"]', name: e.name || '', id, type: (e.type || e.tagName).toLowerCase(), label: label.slice(0, 200), placeholder: e.placeholder || '', autocomplete: e.getAttribute('autocomplete') || '', aria_label: e.getAttribute('aria-label') || '', required: !!e.required };
            })"""
        )

    def fill(self, plan: dict, cv: dict | None) -> None:
        for v in plan["values"]:
            self.page.locator(v["locator"]).fill(v["value"])
        if plan.get("upload") and cv:
            # The employer sees the person's own file name, with the verified bytes, never the hash-named copy.
            self.page.locator(plan["upload"]["locator"]).set_input_files({"name": cv["filename"], "mimeType": cv.get("media_type") or "application/octet-stream", "buffer": cv["data"]})

    def read_back(self) -> list[dict]:
        return self.page.evaluate(
            """() => Array.from(document.querySelectorAll('[data-hunter]')).map((e) => {
              const id = e.id || ''; let label = ''; if (id) { const l = document.querySelector('label[for="' + CSS.escape(id) + '"]'); if (l) label = l.textContent.trim(); }
              if (!label) { const p = e.closest('label'); if (p) label = p.textContent.trim(); }
              const t = (e.type || e.tagName).toLowerCase();
              let value = t === 'file' ? Array.from(e.files || []).map((f) => f.name).join(', ') : (t === 'checkbox' || t === 'radio') ? (e.checked ? 'checked' : '') : (e.value || '');
              return { label: label.slice(0, 200) || e.name || id, name: e.name || '', type: t, value: String(value).slice(0, 5000) };
            }).filter((f) => !['hidden', 'submit', 'button'].includes(f.type))"""
        )

    def tick(self, locator: str) -> None:
        self.page.locator(locator).check()

    def screenshot(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.page.screenshot(path=str(path), full_page=True)

    def submit(self) -> None:
        btn = self.page.locator("button[type=submit], input[type=submit]").first
        if btn.count() == 0:
            btn = self.page.get_by_role("button", name=re.compile(r"submit|apply|send", re.I)).first
        with self.page.expect_navigation(wait_until="domcontentloaded", timeout=30000):
            btn.click()

    def receipt(self) -> dict:
        text = self.page.evaluate("() => (document.body && document.body.innerText || '').slice(0, 4000)")
        m = re.search(r"(?:application|confirmation|reference)\s*(?:id|number|no\.?|#)?\s*[:#]?\s*([A-Z0-9][A-Z0-9-]{3,})", text, re.I)
        return {"url": self.page.url, "title": self.page.title(), "text": text[:600], "confirmation_id": m.group(1) if m else None, "looks_submitted": bool(re.search(r"thank you|received your application|application (?:has been|was) (?:received|submitted)|successfully (?:applied|submitted)|we have received", text, re.I))}

    def close(self) -> None:
        try:
            self.browser.close()
        finally:
            self._pw.stop()


def fill(ctx: AppContext, db: Database, run_key: str, driver: BrowserDriver) -> dict:
    """Open the form, fill from the plan, read every field back from the page, store that read-back. The read-back,
    not the plan, is what the person reviews and approves."""
    r = _row(db, run_key)
    if r["state"] != "permitted":
        raise AssistedError(f"This run is {r['state']}; filling needs a fresh permission.")
    perm = json.loads(r["permission_json"] or "{}")
    cv = None
    if perm.get("upload_cv"):
        cv = docs.cv_file(db, perm["upload_cv"]["revision_key"])  # bytes verified against the hash again, right now
    driver.open(r["destination"])
    fields = driver.fields()
    plan = plan_fill(db, r["job_id"], fields)
    driver.fill(plan, cv)
    readback = driver.read_back()
    shot = ctx.paths.exports_dir / "assisted" / f"{run_key}-filled.png"
    try:
        driver.screenshot(shot)
    except Exception:
        shot = None
    rb = {"destination": driver.page.url, "fields": readback, "cv": perm.get("upload_cv"), "consents": plan["consents"], "blanks": plan["blanks"], "sources": {v["label"]: v["source"] for v in plan["values"]}, "screenshot": str(shot) if shot else None, "at": now_iso()}
    digest = hashlib.sha256(json.dumps({"d": rb["destination"], "f": readback, "cv": (perm.get("upload_cv") or {}).get("sha256"), "c": [c["label"] for c in plan["consents"]]}, sort_keys=True).encode()).hexdigest()
    return _update(db, run_key, state="filled", readback_json=json.dumps(rb), readback_hash=digest)


def review(db: Database, run_key: str) -> dict:
    """The one review: what the form holds now, where it goes, which file, what stays blank, which boxes an approval
    would tick. Plain lines for the person; the hash is what the approval binds to."""
    r = _row(db, run_key)
    if not r.get("readback_json"):
        raise AssistedError("Nothing to review yet: fill the form first.")
    rb = json.loads(r["readback_json"])
    lines = [f"Destination: {rb['destination']}"]
    for f in rb["fields"]:
        if f["type"] in {"checkbox", "radio"}:
            continue
        src = rb["sources"].get(f["label"])
        lines.append(f"- {f['label']}: {f['value'] or '(empty)'}" + (f"  [{src}]" if src else ""))
    if rb.get("cv"):
        lines.append(f"- File: {rb['cv']['filename']} = {rb['cv']['label']} version {rb['cv']['number']} (sha256 {rb['cv']['sha256'][:12]}…)")
    for b in rb.get("blanks") or []:
        lines.append(f"- Left for you: {b['label']} ({b['why']})")
    for c in rb.get("consents") or []:
        lines.append(f"- Will be ticked only with your approval: {c['label']}")
    required_blank = [b for b in rb.get("blanks") or [] if b.get("required") and not _filled_now(rb, b["label"])]
    return {"run_key": run_key, "state": r["state"], "capability": r["capability"], "lines": lines, "readback_hash": r["readback_hash"], "can_submit": r["capability"] == "reviewed_submit" and not required_blank, "why_not": ("This site allows filling only: press submit on the site yourself." if r["capability"] != "reviewed_submit" else ("A required field is still empty." if required_blank else None)), "screenshot": rb.get("screenshot")}


def _filled_now(rb: dict, label: str) -> bool:
    return any(f["label"] == label and f.get("value") for f in rb.get("fields") or [])


def reread(db: Database, run_key: str, driver: BrowserDriver) -> dict:
    """The person completed fields on the form themselves (a choice, a box): read the whole form back again. The
    review and the approval then bind to what the form holds now."""
    r = _row(db, run_key)
    if r["state"] not in {"filled", "approved"}:
        raise AssistedError(f"This run is {r['state']}.")
    rb = json.loads(r["readback_json"] or "{}")
    rb["fields"] = driver.read_back()
    rb["destination"] = driver.page.url
    rb["at"] = now_iso()
    digest = hashlib.sha256(json.dumps({"d": rb["destination"], "f": rb["fields"], "cv": (rb.get("cv") or {}).get("sha256"), "c": [c["label"] for c in rb.get("consents") or []]}, sort_keys=True).encode()).hexdigest()
    return _update(db, run_key, state="filled", readback_json=json.dumps(rb), readback_hash=digest, approval_hash=None, approval_expires_at=None, approved_at=None)


def approve(db: Database, run_key: str, readback_hash: str) -> dict:
    """One single-use code, bound to this run, this read-back and this destination, good for 15 minutes. Shown
    once; only its hash is stored. Approving a different read-back than the one on the page is impossible."""
    r = _row(db, run_key)
    if r["state"] != "filled":
        raise AssistedError(f"This run is {r['state']}; approval needs a filled, reviewed form.")
    if r["capability"] != "reviewed_submit":
        raise AssistedError("This site allows filling only. Press submit on the site yourself; then tell Hunter you applied.")
    if readback_hash != r["readback_hash"]:
        raise AssistedError("The read-back you approve is not the one on the page. Review again.")
    rv = review(db, run_key)
    if not rv["can_submit"]:
        raise AssistedError(rv["why_not"])
    code = secrets.token_hex(4).upper()
    expires = (now() + timedelta(minutes=APPROVAL_MINUTES)).isoformat()
    _update(db, run_key, state="approved", approval_hash=hashlib.sha256(f"{run_key}|{readback_hash}|{code}".encode()).hexdigest(), approval_expires_at=expires, approved_at=now_iso())
    return {"run_key": run_key, "code": code, "expires_at": expires, "binds": {"readback_hash": readback_hash, "destination": r["destination"]}, "note": "Give this code back to submit. It works once, for this exact form, for 15 minutes."}


def submit(ctx: AppContext, db: Database, run_key: str, code: str, driver: BrowserDriver) -> dict:
    """Press submit once. The code must match this run and read-back and be unused and unexpired; the page must
    still hold the approved read-back. A lease covers the click: a crash leaves 'uncertain', never a second click."""
    r = _row(db, run_key)
    if r["state"] == "submitted":
        raise AssistedError("Already submitted through Hunter. Nothing is sent twice.")
    if r["state"] != "approved":
        raise AssistedError(f"This run is {r['state']}; submitting needs your approval code.")
    if r.get("consumed_at"):
        raise AssistedError("That approval was already used.")
    if (r.get("approval_expires_at") or "") < now_iso():
        _update(db, run_key, state="filled", approval_hash=None, approval_expires_at=None, approved_at=None)
        raise AssistedError("The approval expired (15 minutes). Review and approve again.")
    want = hashlib.sha256(f"{run_key}|{r['readback_hash']}|{(code or '').strip().upper()}".encode()).hexdigest()
    if not secrets.compare_digest(want, r["approval_hash"] or ""):
        raise AssistedError("That code does not match this form. Nothing was submitted.")
    live = driver.read_back()
    rb = json.loads(r["readback_json"])
    if live != rb["fields"] or driver.page.url != rb["destination"]:
        raise AssistedError("The form changed since you approved it. Review and approve again.")
    cur = db.execute("UPDATE assisted_runs SET state = 'submitting', consumed_at = ?, lease_until = ?, updated_at = ? WHERE run_key = ? AND state = 'approved' AND consumed_at IS NULL", (now_iso(), (now() + timedelta(seconds=SUBMIT_LEASE_SECONDS)).isoformat(), now_iso(), run_key))
    if cur.rowcount != 1:
        raise AssistedError("Another process is submitting this form. Nothing was sent twice.")
    try:
        for c in rb.get("consents") or []:
            driver.tick(c["locator"])
        driver.submit()
        rec = driver.receipt()
    except Exception as exc:
        return _update(db, run_key, state="uncertain", lease_until=None, error=f"Hunter pressed submit but could not read the answer ({type(exc).__name__}). Check the site or your inbox, then `jobhunter assisted resolve`.")
    shot = ctx.paths.exports_dir / "assisted" / f"{run_key}-receipt.png"
    try:
        driver.screenshot(shot)
        rec["screenshot"] = str(shot)
    except Exception:
        rec["screenshot"] = None
    rec["at"] = now_iso()
    if not rec["looks_submitted"]:
        return _update(db, run_key, state="uncertain", lease_until=None, receipt_json=json.dumps(rec), error="Hunter pressed submit, but the page does not clearly say the application was received. Check the site or your inbox, then `jobhunter assisted resolve`.")
    _record_applied(db, r, rec, source="assisted")
    return _update(db, run_key, state="submitted", lease_until=None, receipt_json=json.dumps(rec), error=None)


def _record_applied(db: Database, r: dict, rec: dict, source: str) -> None:
    perm = json.loads(r.get("permission_json") or "{}")
    rev = (perm.get("upload_cv") or {}).get("revision_key")
    actions.mark_applied(db, r["job_id"], source=source, evidence={"source": source, "run_key": r["run_key"], "receipt_url": rec.get("url"), "confirmation_id": rec.get("confirmation_id"), "screenshot": rec.get("screenshot")}, cv_choice=rev or "unknown", dedupe_key=f"assisted:{r['run_key']}")
    db.execute("UPDATE applications SET submitted_destination = COALESCE(submitted_destination, ?) WHERE job_id = ?", (rec.get("url") or r["destination"], r["job_id"]))


def user_submitted(ctx: AppContext, db: Database, run_key: str, driver: BrowserDriver | None = None) -> dict:
    """assisted_fill: the person pressed submit on the site; Hunter reads the page for a receipt and records what
    it saw, with the person's word as the evidence."""
    r = _row(db, run_key)
    if r["state"] not in {"filled", "approved"}:
        raise AssistedError(f"This run is {r['state']}.")
    rec = {"url": r["destination"], "title": None, "text": "", "confirmation_id": None, "looks_submitted": False, "at": now_iso()}
    if driver is not None:
        try:
            rec.update(driver.receipt())
        except Exception:
            pass
    rec["evidence"] = "You said you pressed submit." + (" The page agrees." if rec["looks_submitted"] else " The page shows no clear confirmation.")
    _record_applied(db, r, rec, source="user")
    return _update(db, run_key, state="user_submitted", receipt_json=json.dumps(rec))


def resolve(ctx: AppContext, db: Database, job_id: int, outcome: str) -> dict:
    """After an uncertain attempt the person says what they saw: submitted (recorded with their word as evidence)
    or not_submitted (the run is closed; a new attempt may start)."""
    r = db.fetchone("SELECT * FROM assisted_runs WHERE job_id = ? AND state = 'uncertain' ORDER BY id DESC LIMIT 1", (job_id,))
    if not r:
        raise AssistedError("No uncertain attempt for this job.")
    if outcome == "submitted":
        rec = json.loads(r.get("receipt_json") or "{}") or {"url": r["destination"]}
        rec["evidence"] = "You confirmed the application went through."
        _record_applied(db, r, rec, source="user")
        return _update(db, r["run_key"], state="submitted", receipt_json=json.dumps(rec), error=None)
    if outcome == "not_submitted":
        return _update(db, r["run_key"], state="failed", error="You said it did not go through. You can try again.")
    raise AssistedError("Say submitted or not_submitted.")


def cancel(db: Database, run_key: str) -> dict:
    r = _row(db, run_key)
    if r["state"] in {"submitted", "submitting"}:
        raise AssistedError(f"This run is {r['state']} and cannot be cancelled.")
    return _update(db, run_key, state="cancelled", approval_hash=None, approval_expires_at=None)


def status(db: Database, job_id: int) -> dict:
    recover_stale(db)
    job = db.fetchone("SELECT * FROM jobs WHERE id = ?", (job_id,))
    if not job:
        raise AssistedError(f"Job {job_id} not found.")
    return {"job_id": job_id, "capability": capability_for(db, job), "run": current_run(db, job_id), "identity_confirmed": bool(identity(db))}


def run_interactive(ctx: AppContext, db: Database, job_id: int, headless: bool = False, ask=input, say=print, executable_path: str | None = None) -> dict:
    """The whole lane in one sitting on the person's computer: permission, fill, read-back, then either the
    approval code (reviewed_submit) or 'tell me when you pressed submit' (assisted_fill)."""
    run = permit(ctx, db, job_id)
    driver = BrowserDriver(headless=headless, executable_path=executable_path)
    try:
        run = fill(ctx, db, run["run_key"], driver)
        rv = review(db, run["run_key"])
        say("\n".join(rv["lines"]))
        while run["capability"] == "reviewed_submit" and not rv["can_submit"] and rv["why_not"] and "required" in rv["why_not"]:
            ans = (ask("Fill the remaining required fields on the form yourself, then type READY (anything else stops): ") or "").strip()
            if ans.upper() != "READY":
                return cancel(db, run["run_key"])
            run = reread(db, run["run_key"], driver)
            rv = review(db, run["run_key"])
            say("\n".join(rv["lines"]))
        if rv["can_submit"]:
            ans = (ask("Approve this exact form? Type YES to get a one-time code, anything else to stop: ") or "").strip()
            if ans != "YES":
                return cancel(db, run["run_key"])
            ap = approve(db, run["run_key"], rv["readback_hash"])
            say(f"Your one-time code: {ap['code']} (valid {APPROVAL_MINUTES} minutes, this form only).")
            code = (ask("Type the code to submit, or press Enter to stop: ") or "").strip()
            if not code:
                return cancel(db, run["run_key"])
            return submit(ctx, db, run["run_key"], code, driver)
        say(rv["why_not"] or "")
        ans = (ask("Press submit on the site yourself. Then type DONE here (or anything else to stop): ") or "").strip()
        if ans.upper() == "DONE":
            return user_submitted(ctx, db, run["run_key"], driver)
        return cancel(db, run["run_key"])
    finally:
        driver.close()
