"""User preferences with must/nice strength and reversible change history."""

from __future__ import annotations

import json

from jobhunter.db import Database
from jobhunter.models import Preference, Preferences
from jobhunter.util import now_iso

KEY = "preferences"

COUNTRY_ALIASES = {
    "netherlands": "NL", "the netherlands": "NL", "nl": "NL", "holland": "NL",
    "germany": "DE", "de": "DE", "deutschland": "DE",
    "united kingdom": "GB", "uk": "GB", "gb": "GB", "england": "GB", "scotland": "GB", "wales": "GB",
    "united states": "US", "usa": "US", "us": "US", "america": "US",
    "canada": "CA", "ca": "CA",
    "france": "FR", "fr": "FR", "spain": "ES", "es": "ES", "italy": "IT", "it": "IT",
    "belgium": "BE", "be": "BE", "austria": "AT", "at": "AT", "switzerland": "CH", "ch": "CH",
    "ireland": "IE", "ie": "IE", "portugal": "PT", "pt": "PT", "poland": "PL", "pl": "PL",
    "sweden": "SE", "se": "SE", "norway": "NO", "no": "NO", "denmark": "DK", "dk": "DK", "finland": "FI", "fi": "FI",
    "czech republic": "CZ", "czechia": "CZ", "cz": "CZ", "romania": "RO", "ro": "RO", "hungary": "HU", "hu": "HU",
    "greece": "GR", "gr": "GR", "estonia": "EE", "ee": "EE", "latvia": "LV", "lv": "LV", "lithuania": "LT", "lt": "LT",
    "luxembourg": "LU", "lu": "LU", "bulgaria": "BG", "bg": "BG", "croatia": "HR", "hr": "HR", "slovakia": "SK", "sk": "SK",
    "slovenia": "SI", "si": "SI", "serbia": "RS", "rs": "RS", "ukraine": "UA", "ua": "UA", "turkey": "TR", "tr": "TR",
    "mexico": "MX", "méxico": "MX", "mx": "MX", "iceland": "IS", "is": "IS",
    "cyprus": "CY", "malta": "MT", "liechtenstein": "LI", "bosnia": "BA", "bosnia and herzegovina": "BA", "montenegro": "ME",
    "north macedonia": "MK", "macedonia": "MK", "albania": "AL", "moldova": "MD", "türkiye": "TR", "turkiye": "TR",
    # Latin America. Two-letter codes (CR, AR, BR, ...) are accepted by country_code() directly.
    "costa rica": "CR", "argentina": "AR", "brazil": "BR", "brasil": "BR", "chile": "CL", "colombia": "CO",
    "peru": "PE", "perú": "PE", "uruguay": "UY", "paraguay": "PY", "bolivia": "BO", "ecuador": "EC", "venezuela": "VE",
    "guyana": "GY", "suriname": "SR", "panama": "PA", "panamá": "PA", "guatemala": "GT", "honduras": "HN",
    "el salvador": "SV", "nicaragua": "NI", "belize": "BZ", "dominican republic": "DO", "puerto rico": "PR", "cuba": "CU",
    # Other common countries. ("Georgia" is left out on purpose: it is also a US state.)
    "india": "IN", "australia": "AU", "new zealand": "NZ", "south africa": "ZA", "singapore": "SG", "japan": "JP",
    "south korea": "KR", "korea": "KR", "china": "CN", "hong kong": "HK", "taiwan": "TW", "philippines": "PH",
    "malaysia": "MY", "indonesia": "ID", "thailand": "TH", "vietnam": "VN", "viet nam": "VN", "pakistan": "PK",
    "bangladesh": "BD", "sri lanka": "LK", "israel": "IL", "united arab emirates": "AE", "uae": "AE",
    "saudi arabia": "SA", "qatar": "QA", "egypt": "EG", "nigeria": "NG", "kenya": "KE", "morocco": "MA", "ghana": "GH",
    "armenia": "AM",
}

EU_EEA = {"AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU", "IE", "IT", "LV", "LT", "LU", "MT", "NL", "PL", "PT", "RO", "SK", "SI", "ES", "SE", "IS", "LI", "NO"}
EUROPE = EU_EEA | {"GB", "CH", "RS", "UA", "TR", "BA", "ME", "MK", "AL", "MD", "GE"}
NORTH_AMERICA = {"US", "CA", "MX"}
SOUTH_AMERICA = {"AR", "BO", "BR", "CL", "CO", "EC", "GY", "PE", "PY", "SR", "UY", "VE"}
CENTRAL_AMERICA = {"BZ", "CR", "GT", "HN", "NI", "PA", "SV"}
LATAM = SOUTH_AMERICA | CENTRAL_AMERICA | {"MX", "CU", "DO", "PR"}
AMERICAS = NORTH_AMERICA | LATAM
ASIA = {"CN", "HK", "TW", "JP", "KR", "SG", "MY", "ID", "TH", "VN", "PH", "IN", "PK", "BD", "LK"}
APAC = ASIA | {"AU", "NZ"}

# Region names a person can type instead of a list of countries.
REGION_INPUTS = {
    "eu": EU_EEA, "european union": EU_EEA, "eea": EU_EEA,
    "europe": EUROPE,
    "north america": NORTH_AMERICA, "na": NORTH_AMERICA,
    "latam": LATAM, "latin america": LATAM,
    "south america": SOUTH_AMERICA, "central america": CENTRAL_AMERICA,
    "americas": AMERICAS, "the americas": AMERICAS,
    "apac": APAC, "asia pacific": APAC, "asia-pacific": APAC, "asia": ASIA,
}


def country_code(text: str) -> str | None:
    t = text.strip().lower()
    if t in COUNTRY_ALIASES:
        return COUNTRY_ALIASES[t]
    if len(t) == 2 and t.isalpha():
        return t.upper()
    return None


def parse_country_list(text: str) -> tuple[list[str], list[str]]:
    """Return (codes, unrecognized)."""
    codes, unknown = [], []
    for part in [p for p in text.replace(";", ",").split(",")]:
        p = part.strip()
        if not p:
            continue
        low = p.lower()
        if low in REGION_INPUTS:
            codes.extend(sorted(REGION_INPUTS[low]))
            continue
        c = country_code(p)
        if c:
            codes.append(c)
        else:
            unknown.append(p)
    seen: list[str] = []
    for c in codes:
        if c not in seen:
            seen.append(c)
    return seen, unknown


def get(db: Database) -> Preferences:
    raw = db.get_setting(KEY)
    if not raw:
        return Preferences()
    try:
        return Preferences.model_validate(raw)
    except Exception:
        return Preferences()


def save(db: Database, prefs: Preferences, reason: str | None = None, confirmed: bool = True) -> None:
    old = db.get_setting(KEY) or {}
    new = prefs.model_dump(mode="json")
    with db.tx():
        for key in sorted(set(old) | set(new)):
            if old.get(key) != new.get(key):
                db.insert(
                    "preference_changes",
                    {
                        "key": key,
                        "old_json": json.dumps(old.get(key)),
                        "new_json": json.dumps(new.get(key)),
                        "reason": reason,
                        "confirmed": 1 if confirmed else 0,
                        "created_at": now_iso(),
                    },
                )
        db.set_setting(KEY, new)


def set_extra(db: Database, key: str, value, strength: str, reason: str | None = None) -> Preferences:
    prefs = get(db)
    prefs.extras[key] = Preference(value=value, strength=strength)  # type: ignore[arg-type]
    save(db, prefs, reason=reason)
    return prefs


def revert_last(db: Database, key: str) -> bool:
    """Undo the most recent change to one preference key."""
    row = db.fetchone(
        "SELECT * FROM preference_changes WHERE key = ? AND reverted_at IS NULL ORDER BY id DESC LIMIT 1", (key,)
    )
    if not row:
        return False
    raw = db.get_setting(KEY) or {}
    raw[key] = json.loads(row["old_json"]) if row["old_json"] else None
    prefs = Preferences.model_validate(raw)
    with db.tx():
        db.update("preference_changes", {"reverted_at": now_iso()}, "id = ?", (row["id"],))
        db.set_setting(KEY, prefs.model_dump(mode="json"))
    return True


def history(db: Database, limit: int = 50) -> list[dict]:
    return db.fetchall("SELECT * FROM preference_changes ORDER BY id DESC LIMIT ?", (limit,))


def describe(prefs: Preferences) -> list[str]:
    """Plain-language lines for the dashboard and the skill."""
    lines = []
    lines.append(f"Current location: {prefs.current_location or 'not set'}")
    from jobhunter.services.normalize import country_names

    lines.append(f"Countries where you can work: {country_names(prefs.work_countries) if prefs.work_countries else 'not set'}")
    # Optional, advanced: only shown when the person set them (never asked during setup).
    if prefs.needs_sponsorship not in {"unset", "not_sure"}:
        lines.append(f"Need visa sponsorship: {prefs.needs_sponsorship.replace('_', ' ')}")
    if prefs.relocation not in {"unset", "not_sure"}:
        lines.append(f"Willing to relocate: {prefs.relocation.replace('_', ' ')}")
    if prefs.salary_min:
        lines.append(
            f"Minimum salary: {prefs.salary_min:,.0f} {prefs.salary_currency or ''} per {prefs.salary_period or 'year'} ({prefs.salary_strength})"
        )
    else:
        lines.append("Minimum salary: not set (that is fine)")
    lines.append(f"Remote work: {prefs.remote.replace('_', ' ')}")
    modes = prefs.work_modes or ["remote", "hybrid", "onsite"]
    lines.append("Okay with: " + ", ".join({"remote": "remote", "hybrid": "hybrid", "onsite": "in the office"}[m] for m in ("remote", "hybrid", "onsite") if m in modes))
    lines.append(f"Timezone: {prefs.timezone or 'not set'}")
    for k, v in prefs.extras.items():
        lines.append(f"{k}: {v.value} ({v.strength})")
    return lines


def update_basics(db: Database, location: str | None = None, countries: str | None = None, salary: str | None = None, salary_must: bool | None = None, remote_text: str | None = None, timezone: str | None = None, reason: str = "settings") -> dict:
    """Direct settings controls (no key/value editor). Empty fields leave the current value alone;
    the word 'skip' or 'none' clears the salary. Returns plain notes for the person."""
    from jobhunter.services.onboarding import parse_salary_answer, parse_work_modes

    prefs = get(db)
    notes: list[str] = []
    if location is not None and location.strip():
        prefs.current_location = location.strip()
    if countries is not None and countries.strip():
        low = countries.strip().lower()
        if low in {"not sure", "unsure", "anywhere", "don't know", "dont know"}:
            prefs.work_countries = []
            prefs.work_countries_note = "not sure"
            notes.append("Okay, I will show jobs everywhere and mark the country on each one.")
        else:
            codes, unknown = parse_country_list(countries)
            if not codes:
                raise ValueError("I did not recognize any country. Try names like 'Netherlands, Germany' or 'EU'.")
            prefs.work_countries = codes
            prefs.work_countries_note = ("unrecognized: " + ", ".join(unknown)) if unknown else None
            if unknown:
                notes.append("I did not recognize: " + ", ".join(unknown) + ". The others are saved.")
    if salary is not None:
        low = salary.strip().lower()
        if low in {"skip", "none", "no", "clear", "-"}:
            prefs.salary_min, prefs.salary_strength = None, "unset"
        elif low:
            parsed = parse_salary_answer(salary)
            if not parsed:
                raise ValueError("For salary, try something like '55000 EUR a year' or leave it empty.")
            prefs.salary_min, prefs.salary_currency, prefs.salary_period = parsed
            prefs.salary_strength = "must" if salary_must else "nice"
        if salary_must is not None and prefs.salary_min:
            prefs.salary_strength = "must" if salary_must else "nice"
    if remote_text is not None and remote_text.strip():
        hit, modes = parse_work_modes(remote_text)
        if not hit:
            raise ValueError("For remote, choose one of the options.")
        prefs.remote = hit  # type: ignore[assignment]
        prefs.work_modes = modes
    if timezone is not None and timezone.strip():
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(timezone.strip())
        except (ZoneInfoNotFoundError, ValueError, KeyError) as exc:
            raise ValueError("I do not know that timezone. Use a name like Europe/Berlin.") from exc
        prefs.timezone = timezone.strip()
        from jobhunter.services import schedule as sched

        sched.set_timezone(db, prefs.timezone)
    save(db, prefs, reason=reason)
    return {"ok": True, "note": " ".join(notes) or "Saved.", "preferences": prefs.model_dump(mode="json")}
