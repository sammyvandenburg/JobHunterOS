"""Turn RawPostings into normalized job records.

Deterministic parsing only: locations to country codes, remote restrictions,
salary units, sponsorship statements, and a relevance classification. Unknown
stays unknown; nothing here guesses.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from jobhunter.models import RawPosting
from jobhunter.services.preferences import COUNTRY_ALIASES, EU_EEA, EUROPE, NORTH_AMERICA
from jobhunter.util import sha256_text, strip_html, truncate

CITY_COUNTRY = {
    "amsterdam": "NL", "rotterdam": "NL", "utrecht": "NL", "eindhoven": "NL", "the hague": "NL", "den haag": "NL", "groningen": "NL",
    "berlin": "DE", "munich": "DE", "münchen": "DE", "hamburg": "DE", "frankfurt": "DE", "cologne": "DE", "köln": "DE", "stuttgart": "DE", "düsseldorf": "DE", "dusseldorf": "DE", "leipzig": "DE", "karlsruhe": "DE", "nuremberg": "DE", "nürnberg": "DE", "dresden": "DE", "hannover": "DE", "bonn": "DE", "mannheim": "DE", "heidelberg": "DE", "bremen": "DE",
    "london": "GB", "manchester": "GB", "edinburgh": "GB", "bristol": "GB", "cambridge": "GB", "oxford": "GB", "glasgow": "GB", "leeds": "GB", "birmingham": "GB", "belfast": "GB",
    "paris": "FR", "lyon": "FR", "toulouse": "FR", "nantes": "FR", "bordeaux": "FR", "lille": "FR", "marseille": "FR", "grenoble": "FR",
    "madrid": "ES", "barcelona": "ES", "valencia": "ES", "malaga": "ES", "málaga": "ES", "seville": "ES",
    "lisbon": "PT", "lisboa": "PT", "porto": "PT",
    "milan": "IT", "milano": "IT", "rome": "IT", "roma": "IT", "turin": "IT", "bologna": "IT",
    "dublin": "IE", "cork": "IE",
    "brussels": "BE", "ghent": "BE", "antwerp": "BE", "leuven": "BE",
    "zurich": "CH", "zürich": "CH", "geneva": "CH", "bern": "CH", "basel": "CH", "lausanne": "CH",
    "vienna": "AT", "wien": "AT", "graz": "AT", "linz": "AT",
    "stockholm": "SE", "gothenburg": "SE", "göteborg": "SE", "malmö": "SE", "malmo": "SE",
    "oslo": "NO", "bergen": "NO", "copenhagen": "DK", "aarhus": "DK", "helsinki": "FI", "tampere": "FI", "espoo": "FI",
    "warsaw": "PL", "warszawa": "PL", "krakow": "PL", "kraków": "PL", "wroclaw": "PL", "wrocław": "PL", "gdansk": "PL", "poznan": "PL",
    "prague": "CZ", "praha": "CZ", "brno": "CZ", "budapest": "HU", "bucharest": "RO", "cluj": "RO", "sofia": "BG", "athens": "GR", "tallinn": "EE", "riga": "LV", "vilnius": "LT", "luxembourg": "LU", "zagreb": "HR", "ljubljana": "SI", "bratislava": "SK", "belgrade": "RS", "kyiv": "UA", "kiev": "UA", "istanbul": "TR", "reykjavik": "IS",
    "new york": "US", "nyc": "US", "san francisco": "US", "sf": "US", "seattle": "US", "austin": "US", "boston": "US", "chicago": "US", "denver": "US", "los angeles": "US", "san diego": "US", "portland": "US", "atlanta": "US", "dallas": "US", "houston": "US", "miami": "US", "washington": "US", "philadelphia": "US", "phoenix": "US", "salt lake city": "US", "raleigh": "US", "pittsburgh": "US", "minneapolis": "US", "san jose": "US", "palo alto": "US", "mountain view": "US", "menlo park": "US", "sunnyvale": "US", "redmond": "US", "bellevue": "US", "nashville": "US", "charlotte": "US", "detroit": "US", "columbus": "US", "boulder": "US", "oakland": "US", "brooklyn": "US", "remote - us": "US",
    "toronto": "CA", "vancouver": "CA", "montreal": "CA", "montréal": "CA", "ottawa": "CA", "calgary": "CA", "waterloo": "CA", "kitchener": "CA", "edmonton": "CA", "quebec": "CA", "halifax": "CA",
    "mexico city": "MX", "guadalajara": "MX", "monterrey": "MX",
}
US_STATES = {"al", "ak", "az", "ar", "ca", "co", "ct", "de", "fl", "ga", "hi", "id", "il", "in", "ia", "ks", "ky", "la", "me", "md", "ma", "mi", "mn", "ms", "mo", "mt", "ne", "nv", "nh", "nj", "nm", "ny", "nc", "nd", "oh", "ok", "or", "pa", "ri", "sc", "sd", "tn", "tx", "ut", "vt", "va", "wa", "wv", "wi", "wy", "dc"}
US_STATE_NAMES = {"california", "texas", "new york", "washington", "colorado", "massachusetts", "illinois", "florida", "georgia", "oregon", "virginia", "north carolina", "pennsylvania", "arizona", "utah", "minnesota", "ohio", "michigan", "new jersey", "maryland", "tennessee", "wisconsin", "missouri", "indiana", "nevada"}
CA_PROVINCES = {"ontario", "quebec", "british columbia", "alberta", "manitoba", "saskatchewan", "nova scotia", "on", "qc", "bc", "ab"}
REGION_WORDS = {
    "worldwide": "WORLD", "anywhere": "WORLD", "global": "WORLD", "international": "WORLD",
    "emea": "EMEA", "europe": "EUROPE", "european union": "EU", "eu": "EU", "eea": "EU", "eu/uk": "EU_UK", "uk/eu": "EU_UK", "dach": "DACH", "nordics": "NORDICS", "benelux": "BENELUX", "cet": "EUROPE", "cest": "EUROPE", "european time zones": "EUROPE", "european timezones": "EUROPE",
    "north america": "NA", "americas": "AMERICAS", "latam": "LATAM", "apac": "APAC", "asia": "APAC", "usa": "US", "u.s.": "US", "us": "US", "united states": "US", "us only": "US", "canada": "CA", "uk": "GB", "united kingdom": "GB",
    "us timezones": "US", "usa timezones": "US", "est": "US", "pst": "US",
}
REGION_SETS = {
    "EU": set(EU_EEA), "EU_UK": set(EU_EEA) | {"GB"}, "EUROPE": set(EUROPE), "EMEA": set(EUROPE),
    "DACH": {"DE", "AT", "CH"}, "NORDICS": {"SE", "NO", "DK", "FI", "IS"}, "BENELUX": {"BE", "NL", "LU"},
    "NA": set(NORTH_AMERICA), "AMERICAS": set(NORTH_AMERICA), "US": {"US"}, "CA": {"CA"}, "GB": {"GB"},
}

REMOTE_WORDS = re.compile(r"\b(remote|work from home|wfh|home ?office|distributed|anywhere)\b", re.I)
HYBRID_WORDS = re.compile(r"\bhybrid\b", re.I)
ONSITE_WORDS = re.compile(r"\b(on-?site|in-?office|onsite only|office-based)\b", re.I)
NO_SPONSOR = re.compile(
    r"(no (visa )?sponsorship|unable to (provide|offer|support) (visa |work )?(visa )?sponsorship|cannot (provide|offer) (visa )?sponsorship|not able to sponsor|without (the need for |requiring )?(visa )?sponsorship|must (already )?(be|have) (legally )?(authori[sz]ed|the right|eligible) to work|will not sponsor|do(es)? not (offer|provide) (visa )?sponsorship|not eligible for sponsorship|sponsorship is not available)",
    re.I,
)
YES_SPONSOR = re.compile(r"(visa sponsorship (is )?available|we (can |will )?(offer|provide) (visa )?sponsorship|sponsorship available|relocation (support|package|assistance) and visa)", re.I)
SALARY_RE = re.compile(
    r"(?P<cur1>€|\$|£|usd|eur|gbp|cad|chf|sek|dkk|nok|pln|us\$|ca\$|c\$)?\s?(?P<min>\d{2,3}(?:[.,]\d{3})+|\d{2,3}\s?k|\d{4,7})\s?(?P<cur2>€|\$|£|usd|eur|gbp|cad|chf|sek|dkk|nok|pln)?\s?(?:-|–|—|to|and)\s?(?P<cur3>€|\$|£|usd|eur|gbp|cad|chf|sek|dkk|nok|pln|us\$|ca\$|c\$)?\s?(?P<max>\d{2,3}(?:[.,]\d{3})+|\d{2,3}\s?k|\d{4,7})\s?(?P<cur4>€|\$|£|usd|eur|gbp|cad|chf|sek|dkk|nok|pln)?",
    re.I,
)
CUR_MAP = {"€": "EUR", "$": "USD", "£": "GBP", "usd": "USD", "eur": "EUR", "gbp": "GBP", "cad": "CAD", "chf": "CHF", "sek": "SEK", "dkk": "DKK", "nok": "NOK", "pln": "PLN", "us$": "USD", "ca$": "CAD", "c$": "CAD"}
PERIOD_RE = {"hour": re.compile(r"(per hour|/ ?h(ou)?r|hourly|an hour)", re.I), "month": re.compile(r"(per month|/ ?mo(nth)?|monthly|a month|p\.?m\.?\b)", re.I), "year": re.compile(r"(per year|per annum|/ ?y(ea)?r|annual|yearly|a year|p\.?a\.?\b|base salary)", re.I)}
TOTAL_COMP = re.compile(r"(total compensation|total comp|otc|on-target|ote\b|including equity|equity)", re.I)

# Relevance: role families. Titles decide most; descriptions resolve "uncertain".
TITLE_INFRA = re.compile(
    r"\b(devops( engineer| specialist| architect| lead)?|dev ops|sre|site reliability|system reliability|reliability engineer|production engineer|"
    r"platform (engineer|engineering|infrastructure|operations|reliability|architect)|infrastructure( engineer| architect| specialist| administrator| lead| developer)?|infra( engineer| lead)?|"
    r"cloud (engineer|architect|administrator|operations|ops|infrastructure|platform|specialist|consultant|developer|native engineer)|cloudops|cloud ops|"
    r"systems? engineer|systems? administrator|sysadmin|system operations|linux( engineer| administrator| admin| specialist| sysadmin)?|unix( engineer| administrator)?|"
    r"kubernetes|k8s|openshift|release engineer|build engineer|automation engineer|observability( engineer)?|network(ing)? engineer|network administrator|"
    r"it operations|operations engineer|technical operations|devsecops|mlops|dataops|platform operations|deployment engineer|virtuali[sz]ation|storage engineer|"
    r"datacenter|data center|hpc|hosting engineer|noc engineer|site operations)\b",
    re.I,
)
TITLE_RELEVANT = TITLE_INFRA  # kept for callers that import the old name
# "systems engineer" in a non-IT discipline, or "platform" in a non-infrastructure sense.
TITLE_NOT_IT = re.compile(r"\b(rf|radio|radar|mechanical|electrical|electronics?|embedded|hardware|hvac|civil|structural|aerospace|automotive|chemical|biomedical|audio|video|optical|photonics|power systems|control systems|controls|avionics|naval|marine|process|manufacturing|industrial|plant|weapons?|satellite|telecom(munication)?s?|5g)\b", re.I)
TITLE_PLATFORM_NOT_INFRA = re.compile(r"\b(power platform|dynamics 365|salesforce|servicenow|sap|low[- ]?code|no[- ]?code|passenger|customer|ticketing|marketplace|e-?commerce|b2b|b2c|payments? platform|platform (agent|assistant|coordinator|specialist|manager|owner|analyst|consultant|sales|success|support|strategist|marketer)|product platform)\b", re.I)
TITLE_SOFTWARE = re.compile(r"\b(software|developer|programmer|full ?stack|backend|back-end|back end|web engineer|application engineer|laravel|php|java|\.net|node(js)?|ruby|rails|django|react|angular|vue|data engineer|security engineer|engineer)\b", re.I)
TITLE_IRRELEVANT = re.compile(
    r"\b(sales|solutions? (engineer|architect|consultant)|pre-?sales|customer experience|cx\b|growth|account (executive|manager)|marketing|recruiter|recruiting|talent|hr\b|people (ops|partner)|legal|counsel|finance|accountant|controller|payroll|designer|ux|ui\b|product manager|product owner|copywriter|content|customer (success|support|service)|support specialist|business development|bdr|sdr|partnerships|community manager|social media|data scientist|machine learning engineer|research scientist|ios|android|mobile|frontend|front-end|front end|react|qa engineer|test engineer|quality assurance|technical writer|office manager|executive assistant|chief of staff|project manager|scrum master|agile coach|procurement|logistics|warehouse|driver|nurse|physician|teacher|intern(ship)? - (sales|marketing)|graphic|video editor|animator|game designer|economist|actuary|underwriter|claims|compliance officer|auditor|tax|agent|cashier|barista|receptionist|cleaner|chef|cook|waiter|waitress|retail)\b",
    re.I,
)
DESC_INFRA = re.compile(
    r"\b(kubernetes|terraform|ansible|docker|ci/cd|gitops|argo ?cd|helm|prometheus|grafana|aws|azure|gcp|linux|infrastructure as code|iac|sre|on-call|observability|cloudformation|pulumi|eks|gke|aks|load balanc|vpc|nginx|bash)\b",
    re.I,
)
SALES_IN_TITLE = re.compile(r"\b(sales|account executive|marketing|recruit|solutions? (engineer|architect)|pre-?sales|customer experience|cx)\b", re.I)
SENIORITY_RE = {
    "intern": re.compile(r"\b(intern|internship|werkstudent|working student|trainee|apprentice)\b", re.I),
    "junior": re.compile(r"\b(junior|jr\.?|entry[- ]level|graduate|associate|early career|level 1|l1)\b", re.I),
    "senior": re.compile(r"\b(senior|sr\.?|lead|level 3|l3|iii)\b", re.I),
    "staff": re.compile(r"\b(staff|principal|distinguished|architect|head of|director|vp|chief|manager|level 4|l4|iv)\b", re.I),
}
YEARS_RE = re.compile(r"(\d{1,2})\s*\+?\s*(?:-|to)?\s*(\d{1,2})?\s*\+?\s*years?", re.I)


@dataclass
class Normalized:
    title: str
    employer_name: str | None
    location_text: str | None
    country_codes: list[str]
    region_tags: list[str]
    remote_type: str
    remote_restriction: str | None
    employment_type: str | None
    salary_text: str | None
    salary_min: float | None
    salary_max: float | None
    salary_currency: str | None
    salary_period: str | None
    salary_basis: str | None
    sponsorship: str
    description_text: str
    description_hash: str
    relevance: str
    relevance_reason: str
    seniority: str
    years_required: int | None
    location_confidence: str = "unknown"
    extra: dict = field(default_factory=dict)


def countries_from_text(text: str | None) -> tuple[list[str], list[str]]:
    """Return (country codes, region tags) found in a location string."""
    if not text:
        return [], []
    codes: list[str] = []
    tags: list[str] = []
    low = text.lower().replace("-", " ").replace("/", " / ")
    low_compact = re.sub(r"\s+", " ", low)
    for word, tag in sorted(REGION_WORDS.items(), key=lambda kv: -len(kv[0])):
        if re.search(rf"(?<![a-z]){re.escape(word)}(?![a-z])", low_compact):
            if tag == "WORLD":
                tags.append("WORLD")
            elif tag in REGION_SETS:
                tags.append(tag)
    for alias, code in sorted(COUNTRY_ALIASES.items(), key=lambda kv: -len(kv[0])):
        if len(alias) <= 2:
            continue  # two-letter aliases are handled below
        if re.search(rf"(?<![a-z]){re.escape(alias)}(?![a-z])", low_compact):
            codes.append(code)
    for part in re.split(r"[,;/|()·•]| - ", low_compact):
        p = part.strip()
        if not p:
            continue
        if p in CITY_COUNTRY:
            codes.append(CITY_COUNTRY[p])
        elif p in US_STATE_NAMES or p in US_STATES and p not in {"ca", "de", "in", "me", "or"}:
            codes.append("US")
        elif p in CA_PROVINCES and p not in {"on"}:
            codes.append("CA")
        elif len(p) == 2 and p.isalpha() and p.upper() in {"NL", "DE", "GB", "US", "CA", "FR", "ES", "IT", "BE", "AT", "CH", "IE", "PT", "PL", "SE", "NO", "DK", "FI", "CZ"}:
            # Two-letter token alone: ambiguous for CA (California vs Canada) and DE (Delaware); prefer country when it stands alone with a comma before
            codes.append(p.upper())
        else:
            for city, code in CITY_COUNTRY.items():
                if re.search(rf"(?<![a-z]){re.escape(city)}(?![a-z])", p):
                    codes.append(code)
                    break
    # "San Francisco, CA" -> state, not Canada
    if "US" in codes and "CA" in codes and re.search(r",\s*ca\b", low_compact) and "canada" not in low_compact:
        codes = [c for c in codes if c != "CA"]
    for tag in tags:
        if tag in {"US", "CA", "GB"}:
            codes.append(tag)
    out: list[str] = []
    for c in codes:
        if c not in out:
            out.append(c)
    return out, sorted(set(tags))


def _parse_amount(token: str) -> float | None:
    t = token.lower().replace(" ", "")
    try:
        if t.endswith("k"):
            return float(t[:-1]) * 1000
        return float(t.replace(",", "").replace(".", ""))
    except ValueError:
        return None


def parse_salary(text: str | None) -> dict:
    out = {"min": None, "max": None, "currency": None, "period": None, "basis": None}
    if not text:
        return out
    m = SALARY_RE.search(text)
    if not m:
        return out
    cur = next((m.group(g) for g in ("cur1", "cur2", "cur3", "cur4") if m.group(g)), None)
    lo, hi = _parse_amount(m.group("min")), _parse_amount(m.group("max"))
    if lo is None or hi is None or lo <= 0 or hi <= 0:
        return out
    if lo > hi:
        lo, hi = hi, lo
    out["min"], out["max"] = lo, hi
    out["currency"] = CUR_MAP.get(cur.lower()) if cur else None
    for period, rx in PERIOD_RE.items():
        if rx.search(text):
            out["period"] = period
            break
    if out["period"] is None:
        out["period"] = "year" if hi >= 15000 else ("month" if hi >= 1000 else "hour")
        out["period_inferred"] = True
    out["basis"] = "total" if TOTAL_COMP.search(text) else "base" if re.search(r"\bbase\b", text, re.I) else "unknown"
    return out


def detect_sponsorship(text: str) -> str:
    if NO_SPONSOR.search(text):
        return "no"
    if YES_SPONSOR.search(text):
        return "yes"
    return "unknown"


def seniority_of(title: str, description: str) -> tuple[str, int | None]:
    years = None
    for m in YEARS_RE.finditer(description[:4000]):
        try:
            n = int(m.group(1))
        except ValueError:
            continue
        if 0 < n <= 20:
            years = n if years is None else min(years, n)
    t = title
    if SENIORITY_RE["intern"].search(t):
        return "intern", years
    if SENIORITY_RE["staff"].search(t):
        return "staff", years
    if SENIORITY_RE["senior"].search(t):
        return "senior", years
    if SENIORITY_RE["junior"].search(t):
        return "junior", years
    return "mid", years


TITLE_SPLIT = re.compile(r"\s+[-–—|/]\s+|\s*\|\s*|,\s+|\s*\(|\s+(?:at|@|for)\s+", re.I)
# A primary role that is clearly software development, whatever team name follows it.
SOFTWARE_PRIMARY = re.compile(r"\b(software|developer|programmer|full[- ]?stack|back[- ]?end|front[- ]?end|web engineer|application engineer|mobile|ios|android|data engineer|data scientist|machine learning engineer|ml engineer|qa|test engineer|security engineer)\b", re.I)
# "Machine Learning Platform Engineer", "AI Infrastructure Engineer": platform work with a data flavour, shown but never a Good match by title alone.
DATA_FLAVOUR = re.compile(r"\b(machine learning|ml|ai|artificial intelligence|genai|data|analytics)\b", re.I)


MANAGEMENT = re.compile(r"\b(head of|director|vp|vice president|chief|cto|cio|ciso|manager|management)\b", re.I)
SECURITY_OPS = re.compile(r"\b(security operations|secops|soc|cyber ?security|cyber|penetration|pentest(er|ing)?|appsec|application security|security (analyst|engineer|architect|specialist|consultant)|information security|infosec|threat|incident responder|forensics)\b", re.I)
DESC_SOFTWARE = re.compile(r"\b(java|kotlin|c#|\.net|typescript|react|angular|vue|node\.?js|spring boot|django|rails|php|laravel|swift|flutter|graphql|rest apis?|microservices|unit tests?|front-?end|back-?end)\b", re.I)


def primary_role(title: str) -> str:
    """The role before any team name or qualifier: "Full-Stack Engineer - Platform Engineering" → "Full-Stack Engineer"."""
    t = (title or "").strip()
    head = TITLE_SPLIT.split(t, 1)[0].strip()
    return head or t


def classify_relevance(title: str, description: str) -> tuple[str, str]:
    """Is this infrastructure / platform / cloud / SRE / DevOps work?

    The title must name the work (an infrastructure noun), not merely contain "platform" or "systems".
    The role before a dash, comma, or bracket is what counts: "Full-Stack Engineer - Platform Engineering"
    is a software job on a platform team. Unusual titles are resolved by what the description says the
    work involves, and land in "uncertain" (shown, with a note) rather than being thrown away.
    """
    from jobhunter.services.matching.skills import infra_signal_count

    t = title or ""
    hits = infra_signal_count(description or "")
    title_infra = bool(TITLE_INFRA.search(t))
    primary = primary_role(t)
    if title_infra and SOFTWARE_PRIMARY.search(primary) and not TITLE_INFRA.search(primary):
        title_infra = False  # the infrastructure word names the team, the role is software development
    if title_infra and TITLE_INFRA.search(primary) and DATA_FLAVOUR.search(primary) and not re.search(r"\b(devops|sre|site reliability|mlops|kubernetes|cloud)\b", primary, re.I):
        return "uncertain", "This is a data or machine-learning platform job. Part of the work may be infrastructure work; check the description."
    if MANAGEMENT.search(primary) and not re.search(r"\blead\b", primary, re.I):
        if title_infra or hits >= 3:
            return "uncertain", "This is a leadership role (people and budget). A stretch only with experience leading a team."
        return "irrelevant", "This is a management role, not an engineering job."
    if SECURITY_OPS.search(t) and not re.search(r"\bdevsecops\b", t, re.I):
        if hits >= 3:
            return "uncertain", "This is a security job. Part of the work may be infrastructure work; check what they expect day to day."
        return "irrelevant", "This is a security job, not infrastructure work."
    if title_infra and hits == 0 and len(DESC_SOFTWARE.findall(description or "")) >= 3 and len(description or "") >= 150:
        return "uncertain", "The title says infrastructure, but the description reads like software development. Check what the work really is."
    if title_infra and TITLE_NOT_IT.search(t) and re.search(r"systems? (engineer|administrator)|infrastructure|platform", t, re.I):
        title_infra = False  # "RF Communication Systems Engineer" is not a sysadmin
    if title_infra and TITLE_PLATFORM_NOT_INFRA.search(t) and not re.search(r"\b(devops|sre|kubernetes|infrastructure|cloud)\b", t, re.I):
        title_infra = False  # "Passenger Services Platform Agent", "Power Platform Consultant"
    if SALES_IN_TITLE.search(t) and not re.search(r"\b(devops|sre|infrastructure) (engineer|lead)\b", t, re.I):
        return "irrelevant", "This is a sales, customer, or recruiting role, even though it mentions cloud or platform."
    if TITLE_IRRELEVANT.search(t) and not title_infra:
        return "irrelevant", "This is a different kind of job."
    if title_infra:
        return "relevant", "This is the kind of work you are looking for."
    if TITLE_PLATFORM_NOT_INFRA.search(t) or TITLE_NOT_IT.search(t):
        if hits >= 5:
            return "uncertain", "The title sounds like something else, but the work described is infrastructure work."
        return "irrelevant", "This uses the word platform or systems, but it is not infrastructure work."
    if TITLE_SOFTWARE.search(t):
        if hits >= 3:
            return "uncertain", "The title is a software or broad engineering title, but the work described is infrastructure work."
        return "irrelevant", "This is a software development job, not infrastructure work."
    if hits >= 5:
        return "uncertain", "Unusual title, but the work looks like infrastructure work."
    return "irrelevant", "Not infrastructure work."


# ---- location restrictions stated in the text -------------------------------------------------
RESIDENCY_PATTERNS = [
    re.compile(r"\b(?:must|need to|needs to|have to|has to|should|required to|are required to|will need to)\s+(?:currently\s+)?(?:be\s+)?(?:(?:legally\s+|physically\s+)?(?:based|located|living|residing|resident|reside|live|domiciled)\s+(?:in|within)|be\s+a\s+resident\s+of)\s+(?:the\s+)?(?P<place>[^.;,\n()]{2,60})", re.I),
    re.compile(r"\b(?:only|exclusively)\s+(?:open\s+to|hiring|accepting|available\s+to|considering|for)\s+(?:candidates|applicants|people|those|residents|individuals|talent)?\s*(?:who\s+are\s+)?(?:(?:based|located|living|residing|resident)\s+)?(?:in|within|from)\s+(?:the\s+)?(?P<place>[^.;,\n()]{2,60})", re.I),
    re.compile(r"\b(?P<place>(?:the\s+)?(?:us|usa|u\.s\.|uk|eu|canada|united states|united kingdom|germany|netherlands|france|spain|portugal|ireland|poland|italy|sweden|denmark|norway|finland|austria|switzerland|belgium|australia|india|brazil|mexico|japan|singapore))[- ]based\s+(?:candidates|applicants|only|remote|role|position|talent|employees|team members|individuals)\b", re.I),
    re.compile(r"\bremote\s+(?:work\s+)?(?:within|from|across|in)\s+(?:the\s+)?(?P<place>(?:us|usa|u\.s\.|uk|eu|eea|european union|europe|canada|united states|united kingdom|germany|netherlands|france|spain|portugal|ireland|poland|italy|sweden|denmark|norway|finland|austria|switzerland|belgium|dach|benelux|nordics|emea|latam|apac)(?:\s+(?:or|and)\s+[a-z .]{2,30})?)\b", re.I),
    re.compile(r"\b(?:candidates|applicants|employees|you)\s+(?:must|need to|needs to|have to|should|are required to)\s+(?:currently\s+)?(?:be\s+)?(?:located|based|living|residing|resident)\s+(?:in|within)\s+(?:the\s+)?(?P<place>[^.;,\n()]{2,60})", re.I),
    re.compile(r"\b(?:this (?:role|position|job) is|we are|positions? (?:is|are))\s+(?:only\s+)?open to (?:candidates|applicants|people|individuals|those)\s+(?:who are\s+)?(?:based|located|living|residing|resident|authorized to work|eligible to work)?\s*(?:in|within)\s+(?:the\s+)?(?P<place>[^.;,\n()]{2,60})", re.I),
]
OFFICE_PATTERNS = [
    re.compile(r"\b(?:\d+|one|two|three|four|five)\s+days?\s+(?:a|per|each)\s+week\s+(?:in|at|from)\s+(?:our|the)\s+(?P<place>[A-Za-z][A-Za-z .'-]{1,30}?)\s+(?:office|hq|headquarters|hub)\b", re.I),
    re.compile(r"\b(?:in|at|from)\s+our\s+(?P<place>[A-Za-z][A-Za-z .'-]{1,30}?)\s+(?:office|hq|headquarters|hub)\s+(?:\d+|one|two|three|four|five|a few|several|some)\s+days?\b", re.I),
    re.compile(r"\b(?:attend|come to|work from|be in|visit)\s+(?:our|the)\s+(?P<place>[A-Za-z][A-Za-z .'-]{1,30}?)\s+(?:office|hq|headquarters|hub)\b", re.I),
]
ELIGIBILITY_ONLY = re.compile(r"\b(authori[sz]ed|eligible|right|permit|permission|allowed)\s+to\s+work\b", re.I)


def residency_restriction(description: str | None) -> tuple[list[str], list[str], str | None]:
    """Countries the text restricts applicants to, with the sentence that says so. Eligibility-to-work
    sentences are not location rules and are ignored here."""
    text = (description or "")[:12000]
    for rx in RESIDENCY_PATTERNS:
        for m in rx.finditer(text):
            place = m.group("place").strip()
            window = text[max(0, m.start() - 40) : m.end()]
            if ELIGIBILITY_ONLY.search(window) and not re.search(r"\b(based|located|living|residing|resident|reside|live)\b", window, re.I):
                continue
            codes, tags = countries_from_text(place)
            if "WORLD" in tags and not codes:
                continue
            for tag in tags:
                for c in REGION_SETS.get(tag, set()):
                    if c not in codes:
                        codes.append(c)
            if codes:
                phrase = text[max(0, m.start()) : min(len(text), m.end() + 1)].strip()
                return codes, tags, phrase
    return [], [], None


def office_requirement(description: str | None) -> tuple[list[str], str | None]:
    """A named office the text expects people to come to (makes a 'remote' tag hybrid in practice)."""
    text = (description or "")[:12000]
    for rx in OFFICE_PATTERNS:
        m = rx.search(text)
        if m:
            place = m.group("place").strip()
            codes, _ = countries_from_text(place)
            return codes, place
    return [], None


def normalize(raw: RawPosting) -> Normalized:
    desc = raw.description_text or strip_html(raw.description_html) or ""
    desc = desc[:20000]
    codes, tags = countries_from_text(raw.location_text)
    for c in raw.country_codes:
        cc = c.upper()
        if len(cc) == 2 and cc not in codes:
            codes.append(cc)
    for name in raw.raw.get("country_names", []) if isinstance(raw.raw, dict) else []:
        extra, extra_tags = countries_from_text(name)
        for c in extra:
            if c not in codes:
                codes.append(c)
        tags = sorted(set(tags) | set(extra_tags))
    for tag in tags:
        for c in REGION_SETS.get(tag, set()):
            if c not in codes:
                codes.append(c)
    # Remote type
    remote = raw.remote_type
    loc_text = raw.location_text or ""
    if remote == "unknown":
        if REMOTE_WORDS.search(loc_text) or REMOTE_WORDS.search(raw.title):
            remote = "remote"
        elif HYBRID_WORDS.search(loc_text) or HYBRID_WORDS.search(raw.title):
            remote = "hybrid"
        elif ONSITE_WORDS.search(loc_text):
            remote = "onsite"
        elif loc_text:
            if HYBRID_WORDS.search(desc[:3000]):
                remote = "hybrid"
            else:
                remote = "onsite" if codes and not REMOTE_WORDS.search(desc[:1500]) else "unknown"
    if remote != "remote" and HYBRID_WORDS.search(loc_text):
        remote = "hybrid"
    confidence = "stated" if codes else "unknown"
    if "WORLD" in tags and not raw.country_codes:
        confidence = "worldwide"
    restriction = raw.remote_restriction
    # What the text says beats a generic tag: "Remote worldwide" plus "must be based in the US" is US only.
    res_codes, res_tags, phrase = residency_restriction(desc)
    if res_codes:
        if not codes or "WORLD" in tags or remote == "remote":
            codes = list(res_codes)
            tags = sorted(set(res_tags))
            confidence = "stated"
            restriction = f"Only for people in {_country_names(codes, tags)} (the posting says: \"{truncate(phrase or '', 90)}\")"
        elif set(codes) & set(res_codes):
            codes = [c for c in codes if c in res_codes]
            confidence = "stated"
        else:
            confidence = "conflict"
            restriction = f"The location says {_country_names(codes, tags)}, but the text says only {_country_names(res_codes, res_tags)}. Check the posting."
    elif remote == "remote":
        office_codes, office = office_requirement(desc)
        if office:
            remote = "hybrid"
            confidence = "inferred"
            if office_codes and not codes:
                codes = list(office_codes)
            restriction = f"Listed as remote, but the text expects days in the {office} office. Check before you apply."
    if remote == "remote" and not restriction:
        if "WORLD" in tags:
            restriction = "Remote worldwide (as stated by the source)"
        elif codes:
            restriction = f"Remote, but only in {_country_names(codes, tags)}"
        else:
            restriction = "Remote, but they don't say which countries"
    # Salary
    sal = parse_salary(raw.salary_text or "")
    if sal["min"] is None and raw.salary_min is not None:
        sal = {"min": raw.salary_min, "max": raw.salary_max, "currency": raw.salary_currency, "period": raw.salary_period or "year", "basis": "unknown"}
    if sal["min"] is None:
        sal2 = parse_salary(_salary_sentence(desc))
        if sal2["min"] is not None:
            sal = sal2
    salary_text = raw.salary_text
    if not salary_text and sal["min"] is not None:
        salary_text = _salary_sentence(desc) or None
    sponsorship = detect_sponsorship(desc)
    relevance, reason = classify_relevance(raw.title, desc)
    seniority, years = seniority_of(raw.title, desc)
    return Normalized(
        title=raw.title,
        employer_name=raw.employer_name,
        location_text=raw.location_text,
        country_codes=codes,
        region_tags=tags,
        remote_type=remote,
        remote_restriction=restriction,
        employment_type=raw.employment_type,
        salary_text=salary_text,
        salary_min=sal["min"],
        salary_max=sal["max"],
        salary_currency=sal["currency"],
        salary_period=sal["period"],
        salary_basis=sal.get("basis"),
        sponsorship=sponsorship,
        description_text=desc,
        description_hash=sha256_text(re.sub(r"\s+", " ", desc.lower())),
        relevance=relevance,
        relevance_reason=reason,
        seniority=seniority,
        years_required=years,
        location_confidence=confidence,
        extra={"salary_period_inferred": bool(sal.get("period_inferred"))},
    )


def _salary_sentence(desc: str) -> str:
    for sentence in re.split(r"(?<=[.\n])\s+", desc[:8000]):
        if re.search(r"(salary|compensation|pay range|base pay|\bOTE\b|€|\$|£)", sentence, re.I) and SALARY_RE.search(sentence):
            return truncate(sentence.strip(), 240)
    return ""


COUNTRY_NAMES = {
    "US": "the USA", "CA": "Canada", "MX": "Mexico", "GB": "the UK", "NL": "the Netherlands", "DE": "Germany", "FR": "France", "ES": "Spain",
    "IT": "Italy", "BE": "Belgium", "AT": "Austria", "CH": "Switzerland", "IE": "Ireland", "PT": "Portugal", "PL": "Poland", "SE": "Sweden",
    "NO": "Norway", "DK": "Denmark", "FI": "Finland", "CZ": "Czechia", "RO": "Romania", "HU": "Hungary", "GR": "Greece", "EE": "Estonia",
    "LV": "Latvia", "LT": "Lithuania", "LU": "Luxembourg", "BG": "Bulgaria", "HR": "Croatia", "SK": "Slovakia", "SI": "Slovenia", "RS": "Serbia",
    "UA": "Ukraine", "TR": "Turkey", "IS": "Iceland", "CY": "Cyprus", "MT": "Malta", "LI": "Liechtenstein",
}


def country_names(codes: list[str], tags: list[str] | None = None) -> str:
    """Plain-language list of countries; collapses well-known regions."""
    tags = tags or []
    cs = set(codes)
    if cs and cs == set(EU_EEA):
        return "the EU/EEA"
    if "EU" in tags and len(cs) > 10:
        return "the EU/EEA"
    if "EUROPE" in tags or "EMEA" in tags or (cs and cs >= set(EUROPE)):
        return "Europe"
    if "NA" in tags or (cs and cs == set(NORTH_AMERICA)):
        return "North America"
    pretty = [COUNTRY_NAMES.get(c, c) for c in codes[:4]]
    more = f" and {len(codes) - 4} more" if len(codes) > 4 else ""
    return ", ".join(pretty) + more


def _country_names(codes: list[str], tags: list[str]) -> str:
    return country_names(codes, tags)
