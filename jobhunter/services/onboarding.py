"""Resumable onboarding: one short question at a time, nothing asked twice.

State lives in the ``onboarding`` setting. Each step knows whether it is
already satisfied by stored data, so a restarted session never repeats a
question. Readiness is self-reported; there is no course verification.
"""

from __future__ import annotations

from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from jobhunter.db import Database
from jobhunter.services import consent
from jobhunter.services import documents as docs
from jobhunter.services import preferences as prefs_svc
from jobhunter.services import profile as profile_svc
from jobhunter.util import now_iso

KEY = "onboarding"


@dataclass
class Step:
    key: str
    question: str
    kind: str  # yesno | file | text | choice | optional_text | info
    optional: bool = False
    choices: list[str] | None = None
    help: str | None = None


STEPS: list[Step] = [
    Step(
        "readiness",
        "Have you finished JobMagnetOS and completed your CV and LinkedIn profile?",
        "yesno",
        help="Hunter works from your finished materials. It does not rewrite them.",
    ),
    Step("cv", "Please give me your finished CV. A file path to a PDF, DOCX, Markdown, or text file works.", "file"),
    Step("linkedin_url", "What is your LinkedIn profile URL?", "text"),
    Step(
        "linkedin_content",
        "I cannot read LinkedIn from the URL alone. Please give me an export, a saved PDF, or paste the profile text.",
        "file",
        help="A URL alone does not tell me what is on your profile. I never guess it.",
    ),
    Step(
        "github",
        "Optional: do you have GitHub repositories or project links you want me to look at? List them, or say 'skip'.",
        "optional_text",
        optional=True,
    ),
    Step("profile_build", "I will now build a profile from your materials.", "info"),
    Step("profile_approve", "Here is what I understand about you. Does this look right?", "yesno"),
    Step("current_location", "Where do you live now? City and country is enough.", "text"),
    Step(
        "work_countries",
        "In which countries can you work without a new visa? List countries, or say 'EU', 'Europe', or 'not sure'.",
        "text",
    ),
    Step("sponsorship", "Would you need visa sponsorship for jobs outside those countries?", "choice", choices=["yes", "no", "not sure"]),
    Step("relocation", "Would you move for the right job?", "choice", choices=["yes", "no", "not sure"]),
    Step("timezone", "Which timezone are you in? For example Europe/Amsterdam or America/Toronto.", "text"),
    Step(
        "salary_min",
        "Optional: is there a minimum salary you need? Give an amount with currency, like '55000 EUR per year', or say 'skip'.",
        "optional_text",
        optional=True,
    ),
    Step(
        "remote",
        "Optional: remote work. Is it required, preferred, or no preference? You can say 'skip'.",
        "choice",
        optional=True,
        choices=["required", "preferred", "no preference", "skip"],
    ),
    Step("hunt_frequency", "How often should I look for jobs?", "choice", choices=["daily", "every 2 hours", "every 4 hours", "every 6 hours", "only when asked"]),
    Step("notify_mode", "When should I tell you about new jobs?", "choice", choices=["dashboard only", "daily email", "email when asked"]),
]
STEP_INDEX = {s.key: s for s in STEPS}


def _state(db: Database) -> dict:
    return db.get_setting(KEY) or {"answers": {}, "started_at": now_iso(), "completed_at": None}


def _save(db: Database, state: dict) -> None:
    db.set_setting(KEY, state)


def _satisfied(db: Database, key: str, state: dict) -> bool:
    """A step is satisfied by stored data, even if it was answered another way."""
    answers = state.get("answers", {})
    if key in answers:
        return True
    prefs = prefs_svc.get(db)
    if key == "readiness":
        return consent.is_granted(db, "readiness")
    if key == "cv":
        return docs.current_document(db, "cv") is not None
    if key == "linkedin_url":
        return bool(db.get_setting("linkedin.url"))
    if key == "linkedin_content":
        return docs.current_document(db, "linkedin") is not None
    if key == "profile_build":
        return profile_svc.current_draft(db) is not None or profile_svc.approved(db) is not None
    if key == "profile_approve":
        return profile_svc.approved(db) is not None and not db.get_setting("profile.needs_review")
    if key == "current_location":
        return bool(prefs.current_location)
    if key == "work_countries":
        return bool(prefs.work_countries) or prefs.work_countries_note is not None
    if key == "sponsorship":
        return prefs.needs_sponsorship != "unset"
    if key == "relocation":
        return prefs.relocation != "unset"
    if key == "timezone":
        return bool(prefs.timezone)
    if key == "salary_min":
        return prefs.salary_strength != "unset"
    if key == "remote":
        return prefs.remote != "unset"
    if key == "hunt_frequency":
        return db.fetchone("SELECT frequency FROM schedule WHERE id = 1 AND frequency != 'manual'") is not None or "hunt_frequency" in answers
    if key == "notify_mode":
        return "notify_mode" in answers
    return False


def next_step(db: Database) -> dict | None:
    state = _state(db)
    for step in STEPS:
        if _satisfied(db, step.key, state):
            continue
        # Skip LinkedIn content step until we have a URL; skip profile steps until a CV exists.
        if step.key == "profile_approve" and not _satisfied(db, "profile_build", state):
            continue
        payload = {
            "key": step.key,
            "question": step.question,
            "kind": step.kind,
            "optional": step.optional,
            "choices": step.choices,
            "help": step.help,
        }
        if step.key == "profile_approve":
            row = profile_svc.current_draft(db)
            if row:
                payload["summary"] = row["summary_text"]
                payload["open_questions"] = profile_svc.open_questions(db)
        return payload
    if not state.get("completed_at"):
        state["completed_at"] = now_iso()
        _save(db, state)
    return None


def status(db: Database) -> dict:
    state = _state(db)
    done = [s.key for s in STEPS if _satisfied(db, s.key, state)]
    pending = [s.key for s in STEPS if s.key not in done]
    return {
        "started_at": state.get("started_at"),
        "completed_at": state.get("completed_at"),
        "done": done,
        "pending": pending,
        "readiness_confirmed": consent.is_granted(db, "readiness"),
        "profile_approved": profile_svc.approved(db) is not None,
        "needs_review": db.get_setting("profile.needs_review"),
    }


class AnswerError(Exception):
    pass


def answer(db: Database, key: str, value: str) -> dict:
    """Store an answer. Returns a confirmation and the next step."""
    if key not in STEP_INDEX:
        raise AnswerError(f"Unknown setup step '{key}'.")
    state = _state(db)
    v = (value or "").strip()
    low = v.lower()
    prefs = prefs_svc.get(db)
    note = None

    if key == "readiness":
        if low in {"yes", "y", "true", "done", "finished"}:
            consent.grant(db, "readiness", {"self_reported": True})
            note = "Thanks. Let's get you set up."
        elif low in {"no", "n", "false", "not yet"}:
            consent.revoke(db, "readiness")
            state["answers"][key] = "no"
            _save(db, state)
            return {
                "ok": True,
                "note": "No problem. Finish your CV and LinkedIn profile first; I work from those finished materials. Your progress is saved. Come back with /jobhunter when you are ready. A demo is available if you want to see how Hunter works.",
                "paused": True,
                "next": None,
            }
        else:
            raise AnswerError("Please answer yes or no.")
    elif key == "linkedin_url":
        if not (low.startswith("https://") and "linkedin.com/in/" in low):
            raise AnswerError("That does not look like a LinkedIn profile URL (expected https://www.linkedin.com/in/...).")
        db.set_setting("linkedin.url", v)
        note = "Saved. I still need the profile content; a URL alone is not enough."
    elif key in {"cv", "linkedin_content"}:
        raise AnswerError("Add the document with `jobhunter docs add --kind cv <path>` or `--kind linkedin`; the dashboard upload also works.")
    elif key == "github":
        if low in {"skip", "no", "none", ""}:
            note = "Skipped. You can add project links later."
        else:
            raise AnswerError("Use `jobhunter evidence github <repo...>` to add repositories, or answer 'skip'.")
    elif key == "profile_build":
        raise AnswerError("Run `jobhunter profile build` to build the profile.")
    elif key == "profile_approve":
        if low in {"yes", "y", "looks right", "approve"}:
            profile_svc.approve(db)
            consent.grant(db, "profile_approved", {})
            note = "Great. The profile is approved. You can correct it any time."
        else:
            raise AnswerError("Tell me what is wrong with `jobhunter profile correct \"...\"`, then approve.")
    elif key == "current_location":
        prefs.current_location = v
        prefs_svc.save(db, prefs, reason="onboarding")
    elif key == "work_countries":
        if low in {"not sure", "unsure", "don't know", "dont know"}:
            prefs.work_countries = []
            prefs.work_countries_note = "not sure"
            note = "Okay. I will show work-eligibility as an open question on each job instead of guessing."
        else:
            codes, unknown = prefs_svc.parse_country_list(v)
            if not codes:
                raise AnswerError("I did not recognize any country. Try names like 'Netherlands, Germany' or codes like 'NL, DE'.")
            prefs.work_countries = codes
            prefs.work_countries_note = ("unrecognized: " + ", ".join(unknown)) if unknown else None
            if unknown:
                note = "I did not recognize: " + ", ".join(unknown) + ". The others are saved."
        prefs_svc.save(db, prefs, reason="onboarding")
    elif key == "sponsorship":
        mapping = {"yes": "yes", "y": "yes", "no": "no", "n": "no", "not sure": "not_sure", "unsure": "not_sure"}
        if low not in mapping:
            raise AnswerError("Please answer yes, no, or not sure.")
        prefs.needs_sponsorship = mapping[low]  # type: ignore[assignment]
        prefs_svc.save(db, prefs, reason="onboarding")
    elif key == "relocation":
        mapping = {"yes": "yes", "y": "yes", "no": "no", "n": "no", "not sure": "not_sure", "unsure": "not_sure", "maybe": "not_sure"}
        if low not in mapping:
            raise AnswerError("Please answer yes, no, or not sure.")
        prefs.relocation = mapping[low]  # type: ignore[assignment]
        prefs_svc.save(db, prefs, reason="onboarding")
    elif key == "timezone":
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError, KeyError) as exc:
            raise AnswerError("I do not know that timezone. Use a name like Europe/Berlin or America/New_York.") from exc
        prefs.timezone = v
        prefs_svc.save(db, prefs, reason="onboarding")
        db.execute("UPDATE schedule SET timezone = ?, updated_at = ? WHERE id = 1", (v, now_iso()))
    elif key == "salary_min":
        if low in {"skip", "no", "none", ""}:
            prefs.salary_strength = "unset"
            prefs.salary_min = None
            note = "Skipped. I will match broadly and show salaries as listed."
        else:
            parsed = parse_salary_answer(v)
            if not parsed:
                raise AnswerError("Try an amount with currency, like '55000 EUR per year' or '4000 EUR per month'.")
            prefs.salary_min, prefs.salary_currency, prefs.salary_period = parsed
            prefs.salary_strength = "must" if "must" in low else "nice"
            note = f"Saved as a {'must have' if prefs.salary_strength == 'must' else 'nice to have'}. Say 'must' in the answer to make it a hard rule."
        prefs_svc.save(db, prefs, reason="onboarding")
    elif key == "remote":
        mapping = {"required": "required", "preferred": "preferred", "no preference": "no_preference", "none": "no_preference", "skip": "no_preference"}
        if low not in mapping:
            raise AnswerError("Please answer required, preferred, no preference, or skip.")
        prefs.remote = mapping[low]  # type: ignore[assignment]
        prefs_svc.save(db, prefs, reason="onboarding")
    elif key == "hunt_frequency":
        mapping = {"daily": "daily", "every 2 hours": "every_2h", "every 4 hours": "every_4h", "every 6 hours": "every_6h", "only when asked": "manual", "manual": "manual"}
        if low not in mapping:
            raise AnswerError("Choose daily, every 2 hours, every 4 hours, every 6 hours, or only when asked.")
        from jobhunter.services import schedule as sched

        sched.set_frequency(db, mapping[low])
        note = "Saved. Nothing runs in the background until you start the worker; I will explain how."
    elif key == "notify_mode":
        mapping = {"dashboard only": "dashboard", "dashboard": "dashboard", "daily email": "daily_email", "email when asked": "on_request"}
        if low not in mapping:
            raise AnswerError("Choose dashboard only, daily email, or email when asked.")
        from jobhunter.services import schedule as sched

        sched.set_notify_mode(db, mapping[low])
        if mapping[low] == "daily_email":
            note = "Daily email needs a confirmed address: `jobhunter digest address you@example.com`."
    state["answers"][key] = v
    _save(db, state)
    return {"ok": True, "note": note, "next": next_step(db)}


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
    """Normal live hunting needs readiness confirmation and an approved profile."""
    if demo:
        return True, None
    if not consent.is_granted(db, "readiness"):
        return False, "Please confirm first that your CV and LinkedIn profile are finished (setup step 'readiness')."
    if profile_svc.approved(db) is None:
        return False, "Your profile is not approved yet. Run `jobhunter profile build`, then approve it."
    if db.get_setting("profile.needs_review"):
        return False, "A new document was added. Please review and approve the profile again before hunting."
    return True, None
