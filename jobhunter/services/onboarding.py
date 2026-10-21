"""Short, resumable setup. Five steps, nothing asked twice.

1. readiness   Did you finish your CV and LinkedIn profile?
2. documents   Upload your CV and your LinkedIn PDF (one step, both files).
3. profile     Built automatically; you say "looks right" or correct it.
4. basics      One combined question: where you live, which countries you can
               work in, lowest salary (optional), remote, how often to look,
               how to tell you.
Timezone is detected from the computer. Visa and relocation are never asked;
they live under "More options" for people who want them.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from jobhunter.db import Database
from jobhunter.services import consent
from jobhunter.services import documents as docs
from jobhunter.services import preferences as prefs_svc
from jobhunter.services import profile as profile_svc
from jobhunter.util import now_iso

KEY = "onboarding"

LINKEDIN_HOWTO = (
    "To get your LinkedIn PDF: open your LinkedIn profile in a browser, click 'More' under your name, "
    "then 'Save to PDF'. Then drag both files into this window."
)


@dataclass
class Step:
    key: str
    question: str
    kind: str  # yesno | files | approve | basics
    help: str | None = None


STEPS: list[Step] = [
    Step("readiness", "Have you finished your CV and LinkedIn profile in JobMagnetOS?", "yesno", help="Hunter works from your finished CV and LinkedIn profile. It never changes them."),
    Step("documents", "Please give me two files: your CV and your LinkedIn profile saved as PDF.", "files", help=LINKEDIN_HOWTO),
    Step("profile_approve", "Here is what I understand about you. Does this look right?", "approve", help="Say yes, or tell me what is wrong."),
    Step(
        "basics",
        "Three quick things, all in one go:\n"
        "1. Where should I look? Where do you live, and which countries can you work in? (for example 'Rotterdam, Netherlands; EU')\n"
        "2. Lowest pay you would be happy with? (optional, for example '55000 EUR a year', or 'skip')\n"
        "3. Remote, hybrid, or either? ('remote preferred, hybrid ok, never fully on-site' is a fine answer)",
        "basics",
        help="Short answers are fine. You can change any of this later.",
    ),
]
STEP_INDEX = {s.key: s for s in STEPS}


class AnswerError(Exception):
    pass


def _state(db: Database) -> dict:
    return db.get_setting(KEY) or {"answers": {}, "started_at": now_iso(), "completed_at": None}


def _save(db: Database, state: dict) -> None:
    db.set_setting(KEY, state)


def detect_timezone() -> str:
    """Best effort IANA zone from the computer; UTC when unknown."""
    tz = os.environ.get("TZ")
    if tz:
        try:
            ZoneInfo(tz)
            return tz
        except (ZoneInfoNotFoundError, ValueError, KeyError):
            pass
    try:
        real = Path("/etc/localtime").resolve()
        parts = real.parts
        if "zoneinfo" in parts:
            name = "/".join(parts[parts.index("zoneinfo") + 1 :])
            ZoneInfo(name)
            return name
    except (OSError, ZoneInfoNotFoundError, ValueError, KeyError):
        pass
    try:
        name = datetime.now().astimezone().tzname() or ""
        ZoneInfo(name)
        return name
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return "UTC"


def documents_ready(db: Database) -> tuple[bool, list[str]]:
    missing = []
    cv = docs.current_document(db, "cv")
    li = docs.current_document(db, "linkedin")
    if not cv or cv["extraction_status"] != "ok":
        missing.append("CV")
    if not li or li["extraction_status"] != "ok":
        missing.append("LinkedIn PDF")
    return not missing, missing


def _satisfied(db: Database, key: str, state: dict) -> bool:
    prefs = prefs_svc.get(db)
    if key == "readiness":
        return consent.is_granted(db, "readiness")
    if key == "documents":
        return documents_ready(db)[0]
    if key == "profile_approve":
        return profile_svc.approved(db) is not None and not db.get_setting("profile.needs_review")
    if key == "basics":
        return bool(prefs.current_location) and (bool(prefs.work_countries) or prefs.work_countries_note is not None)
    return False


def ensure_profile_draft(db: Database) -> dict | None:
    """Build the rule-based draft as soon as both documents are readable."""
    ok, _ = documents_ready(db)
    if not ok:
        return None
    if profile_svc.current_draft(db) or (profile_svc.approved(db) and not db.get_setting("profile.needs_review")):
        return None
    try:
        draft = profile_svc.build_rule_based_draft(db)
    except profile_svc.ProfileError:
        return None
    return profile_svc.save_draft(db, draft, built_by="rules")


def next_step(db: Database) -> dict | None:
    state = _state(db)
    for step in STEPS:
        if _satisfied(db, step.key, state):
            continue
        payload = {"key": step.key, "question": step.question, "kind": step.kind, "help": step.help}
        if step.key == "documents":
            payload["missing"] = documents_ready(db)[1]
        if step.key == "profile_approve":
            ensure_profile_draft(db)
            row = profile_svc.current_draft(db) or profile_svc.approved(db)
            if row:
                payload["summary"] = row["summary_text"]
                payload["open_questions"] = profile_svc.open_questions(db)
                payload["needs_review"] = db.get_setting("profile.needs_review")
        if step.key == "basics":
            payload["detected_timezone"] = detect_timezone()
        return payload
    if not state.get("completed_at"):
        state["completed_at"] = now_iso()
        _save(db, state)
    return None


def status(db: Database) -> dict:
    state = _state(db)
    done = [s.key for s in STEPS if _satisfied(db, s.key, state)]
    return {
        "started_at": state.get("started_at"),
        "completed_at": state.get("completed_at"),
        "done": done,
        "pending": [s.key for s in STEPS if s.key not in done],
        "readiness_confirmed": consent.is_granted(db, "readiness"),
        "profile_approved": profile_svc.approved(db) is not None,
        "needs_review": db.get_setting("profile.needs_review"),
        "documents_missing": documents_ready(db)[1],
    }


def answer(db: Database, key: str, value: str) -> dict:
    """Store a single-value answer (readiness, profile approval)."""
    if key not in STEP_INDEX:
        raise AnswerError(f"Unknown setup step '{key}'.")
    state = _state(db)
    low = (value or "").strip().lower()
    note = None
    if key == "readiness":
        if low in {"yes", "y", "true", "done", "finished"}:
            consent.grant(db, "readiness", {"self_reported": True})
            note = "Great."
        elif low in {"no", "n", "false", "not yet"}:
            consent.revoke(db, "readiness")
            state["answers"][key] = "no"
            _save(db, state)
            return {"ok": True, "paused": True, "next": None, "note": "No problem. Finish your CV and LinkedIn profile first. Hunter works from those. Your progress is saved; come back with /jobhunter when you are ready."}
        else:
            raise AnswerError("Please answer yes or no.")
    elif key == "documents":
        raise AnswerError("Add the files with `jobhunter docs add --kind cv <file>` and `jobhunter docs add --kind linkedin <file>`, or drag them into the dashboard.")
    elif key == "profile_approve":
        if low in {"yes", "y", "looks right", "approve", "correct"}:
            profile_svc.approve(db)
            consent.grant(db, "profile_approved", {})
            note = "Great, your profile is approved. You can correct it any time."
        else:
            raise AnswerError("Tell me what is wrong with `jobhunter profile correct \"...\"`, then approve.")
    elif key == "basics":
        raise AnswerError("Use `jobhunter setup basics --location ... --countries ...`.")
    state["answers"][key] = value
    _save(db, state)
    return {"ok": True, "note": note, "next": next_step(db)}


def parse_work_modes(low: str) -> tuple[str | None, list[str]]:
    """'remote only' -> required, [remote]; 'remote preferred, hybrid okay' -> preferred, [remote, hybrid];
    'don't mind' -> no_preference, all three. Says which arrangements are acceptable at all."""
    low = (low or "").strip().lower()
    mapping = {"must": "required", "must have": "required", "required": "required", "only": "required", "yes": "required", "nice": "preferred", "nice to have": "preferred", "preferred": "preferred", "prefer": "preferred", "don't mind": "no_preference", "dont mind": "no_preference", "no preference": "no_preference", "either": "no_preference", "any": "no_preference", "skip": "no_preference", "no": "no_preference"}
    hit = next((v for k, v in mapping.items() if k in low), None)
    if not hit and "remote" in low:
        hit = "preferred"
    if not hit:
        return None, ["remote", "hybrid", "onsite"]
    modes = ["remote", "hybrid", "onsite"]
    if hit == "required":
        modes = ["remote"]
    if re.search(r"\bhybrid\b", low) and not re.search(r"\b(no|not|never)\s+hybrid\b", low):
        if "hybrid" not in modes:
            modes.append("hybrid")
    if re.search(r"\b(no|not|never)\s+(fully\s+)?(on-?site|office|in the office|in-office)\b|\bno office\b|\bnever (fully )?on-?site\b", low):
        modes = [m for m in modes if m != "onsite"]
    if re.search(r"\b(no|not|never)\s+hybrid\b", low):
        modes = [m for m in modes if m != "hybrid"]
    if re.search(r"\b(no|not|never)\s+remote\b", low):
        modes = [m for m in modes if m != "remote"]
    return hit, [m for m in ("remote", "hybrid", "onsite") if m in modes] or ["remote", "hybrid", "onsite"]


def basics(
    db: Database,
    location: str | None = None,
    countries: str | None = None,
    salary: str | None = None,
    remote: str | None = None,
    frequency: str | None = None,
    notify: str | None = None,
    timezone: str | None = None,
    linkedin_url: str | None = None,
) -> dict:
    """Store the combined basics answer. Missing optional parts stay unset."""
    prefs = prefs_svc.get(db)
    notes: list[str] = []
    if location:
        prefs.current_location = location.strip()
    if countries:
        low = countries.strip().lower()
        if low in {"not sure", "unsure", "don't know", "dont know"}:
            prefs.work_countries = []
            prefs.work_countries_note = "not sure"
            notes.append("Okay. I will show jobs everywhere and mark the country on each one.")
        else:
            codes, unknown = prefs_svc.parse_country_list(countries)
            if not codes:
                raise AnswerError("I did not recognize any country. Try names like 'Netherlands, Germany' or 'EU'.")
            prefs.work_countries = codes
            prefs.work_countries_note = ("unrecognized: " + ", ".join(unknown)) if unknown else None
            if unknown:
                notes.append("I did not recognize: " + ", ".join(unknown) + ". The others are saved.")
    if salary is not None:
        low = salary.strip().lower()
        if low in {"", "skip", "no", "none", "any"}:
            prefs.salary_min = None
            prefs.salary_strength = "unset"
        else:
            parsed = parse_salary_answer(salary)
            if not parsed:
                raise AnswerError("For salary, try something like '55000 EUR a year' or 'skip'.")
            prefs.salary_min, prefs.salary_currency, prefs.salary_period = parsed
            prefs.salary_strength = "must" if "must" in low else "nice"
    if remote is not None:
        low = remote.strip().lower()
        hit, modes = parse_work_modes(low)
        if not hit:
            raise AnswerError("For remote, say: must have, nice to have, or don't mind. You can add 'hybrid is fine' or 'no office'.")
        prefs.remote = hit  # type: ignore[assignment]
        prefs.work_modes = modes
    tz = timezone or prefs.timezone or detect_timezone()
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError, KeyError) as exc:
        raise AnswerError("I do not know that timezone. Use a name like Europe/Berlin.") from exc
    prefs.timezone = tz
    if linkedin_url:
        if linkedin_url.startswith("https://") and "linkedin.com/in/" in linkedin_url:
            db.set_setting("linkedin.url", linkedin_url.strip())
        else:
            notes.append("That LinkedIn link did not look right, so I skipped it.")
    prefs_svc.save(db, prefs, reason="setup")

    from jobhunter.services import schedule as sched

    sched.set_timezone(db, tz)
    if frequency is not None:
        low = frequency.strip().lower()
        freq = "daily" if any(w in low for w in ("day", "daily", "every")) and "ask" not in low else "manual"
        if "2 hour" in low or "two hour" in low:
            freq = "every_2h"
        elif "4 hour" in low:
            freq = "every_4h"
        elif "6 hour" in low:
            freq = "every_6h"
        sched.set_frequency(db, freq)
        if freq != "manual":
            notes.append("Daily looking needs the worker running; I will show you the one command when we get there.")
    if notify is not None:
        low = notify.strip().lower()
        mode = "daily_email" if "mail" in low else "dashboard"
        sched.set_notify_mode(db, mode)
        if mode == "daily_email":
            notes.append("For the daily email I need your address: `jobhunter digest address you@example.com`.")
    state = _state(db)
    state["answers"]["basics"] = {"location": location, "countries": countries, "salary": salary, "remote": remote, "frequency": frequency, "notify": notify, "timezone": tz}
    _save(db, state)
    return {"ok": True, "note": " ".join(notes) or "Saved.", "timezone": tz, "next": next_step(db)}


def parse_salary_answer(text: str) -> tuple[float, str, str] | None:
    import re

    t = text.lower().replace(",", "").replace(".", "")
    m = re.search(r"(\d{2,7})\s*(k)?", t)
    if not m:
        return None
    amount = float(m.group(1)) * (1000 if m.group(2) else 1)
    currency = "EUR"
    for cur, needles in {"EUR": ["eur", "€", "euro"], "USD": ["usd", "$", "dollar"], "GBP": ["gbp", "£", "pound"], "CAD": ["cad"], "CHF": ["chf"], "SEK": ["sek"], "DKK": ["dkk"], "NOK": ["nok"], "PLN": ["pln"]}.items():
        if any(n in t for n in needles):
            currency = cur
            break
    period = "year"
    if "month" in t or "/m" in t:
        period = "month"
    elif "hour" in t or "/h" in t:
        period = "hour"
    return amount, currency, period


def live_hunting_allowed(db: Database, demo: bool) -> tuple[bool, str | None]:
    if demo:
        return True, None
    if not consent.is_granted(db, "readiness"):
        return False, "First tell me your CV and LinkedIn profile are finished (setup step 'readiness')."
    if profile_svc.approved(db) is None:
        return False, "Your profile is not approved yet. Look at it and say it looks right."
    if db.get_setting("profile.needs_review"):
        return False, "You added a new document. Please look at your profile again and approve it."
    return True, None
