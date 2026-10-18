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
    "mexico": "MX", "mx": "MX", "iceland": "IS", "is": "IS",
}

EU_EEA = {"AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU", "IE", "IT", "LV", "LT", "LU", "MT", "NL", "PL", "PT", "RO", "SK", "SI", "ES", "SE", "IS", "LI", "NO"}
EUROPE = EU_EEA | {"GB", "CH", "RS", "UA", "TR", "BA", "ME", "MK", "AL", "MD", "GE"}
NORTH_AMERICA = {"US", "CA", "MX"}


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
        if low in {"eu", "european union", "eea"}:
            codes.extend(sorted(EU_EEA))
            continue
        if low in {"europe"}:
            codes.extend(sorted(EUROPE))
            continue
        if low in {"north america", "na"}:
            codes.extend(sorted(NORTH_AMERICA))
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
    lines.append(f"Countries where you can work: {', '.join(prefs.work_countries) if prefs.work_countries else 'not set'}")
    lines.append(f"Need visa sponsorship: {prefs.needs_sponsorship.replace('_', ' ')}")
    lines.append(f"Willing to relocate: {prefs.relocation.replace('_', ' ')}")
    if prefs.salary_min:
        lines.append(
            f"Minimum salary: {prefs.salary_min:,.0f} {prefs.salary_currency or ''} per {prefs.salary_period or 'year'} ({prefs.salary_strength})"
        )
    else:
        lines.append("Minimum salary: not set (that is fine)")
    lines.append(f"Remote work: {prefs.remote.replace('_', ' ')}")
    lines.append(f"Timezone: {prefs.timezone or 'not set'}")
    for k, v in prefs.extras.items():
        lines.append(f"{k}: {v.value} ({v.strength})")
    return lines
