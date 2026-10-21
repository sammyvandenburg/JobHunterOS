"""Reading a posting the way a careful person does.

A tool named under "Requirements" or "What you'll do" is essential; under "Nice to have" it is preferred; in
"About us" or a benefits paragraph it is incidental (the company's history, not the job); "No Azure experience
required" negates it. Languages and the contract type are read here too. Deterministic, untrusted input: only
known tool names and language names are recognised, and nothing here can change settings."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from jobhunter.services.matching.skills import FAMILIES, GENERIC, TRIVIAL, extract_skills

SECTION_PREFERRED = re.compile(r"^\W*(nice[- ]to[- ]haves?|bonus(?: points| skills)?|preferred(?: qualifications| skills| experience)?|plus(?:es)?|good to have|desirable|would be (?:nice|great|a plus)|ideally|optional|extra credit|nice if)\b", re.I)
SECTION_ESSENTIAL = re.compile(r"^\W*(requirements?|what you bring|what you(?:'ll| will) need|must[- ]haves?|you have|(?:minimum |basic |required )?qualifications|skills(?: and experience| & experience)?|experience|about you|who you are|what we(?:'re| are) looking for|your profile|you bring|essential|what you(?:'ll| will) do|responsibilities|your responsibilities|the role|about the role|our (?:tech )?stack|tech stack|technologies|what you(?:'ll| will) be doing|your day|day to day|what you(?:'ll| will) work on)\b", re.I)
SECTION_CONTEXT = re.compile(r"^\W*(about (?:us|the company|the team|[a-z0-9 .&-]{2,30}$)|who we are|what we do|our (?:story|mission|values|culture|history)|benefits|what we offer|perks|how we work|why (?:join|work with) us|compensation|equal opportunity|diversity|the company|our company|our team|the team)\b", re.I)
PREFERRED_CUE = re.compile(r"\b(nice[- ]to[- ]have|bonus|a plus|is a plus|are a plus|plus:|preferred|preferably|ideally|would be (?:nice|great|an advantage|a plus)|good to have|familiarity with|exposure to|not required but|advantage(?:ous)?|desirable|appreciated|welcome)\b", re.I)
NEGATION_CUE = re.compile(r"\b(?:no|without|not)\s+(?:prior\s+|previous\s+|deep\s+|hands[- ]on\s+)?(?P<tool>[a-z0-9+#./ -]{2,40}?)\s+(?:experience|knowledge|skills|background)\s+(?:is\s+)?(?:required|needed|necessary|expected|assumed)\b|\b(?P<tool2>[a-z0-9+#./ -]{2,40}?)\s+(?:experience|knowledge)?\s*(?:is|are)\s+not\s+(?:required|needed|necessary|a requirement|expected)\b|\bdon'?t need (?:to know )?(?P<tool3>[a-z0-9+#./ -]{2,40}?)[.,;)]", re.I)
INCIDENTAL_CUE = re.compile(r"\b(moved (?:off|away from|from)|migrat(?:ed|ing) (?:off|away from|from)|replaced|replacing|retired|used to (?:run|use)|legacy|instead of|no longer|we (?:left|dropped))\b", re.I)
LANGUAGES = ["Dutch", "German", "French", "English", "Spanish", "Italian", "Portuguese", "Swedish", "Danish", "Norwegian", "Finnish", "Polish", "Flemish", "Czech", "Romanian", "Hungarian", "Greek", "Turkish", "Japanese", "Mandarin", "Chinese", "Arabic", "Hebrew"]
_LANG = "|".join(LANGUAGES)
LANG_REQUIRED = re.compile(rf"\b(?:fluent|fluency|native|proficien(?:t|cy)|professional|business[- ]level|excellent|strong|good)\s+(?:in\s+|level\s+of\s+)?(?:spoken\s+|written\s+)?(?:and\s+written\s+)?(?P<lang>{_LANG})\b|\b(?P<lang2>{_LANG})\s+(?:\((?:[abc][12]|native)\)\s*)?(?:is|are)?\s*(?:required|mandatory|essential|a must|needed|necessary|speaking)\b|\b(?P<lang3>{_LANG})\s+(?:language\s+)?(?:skills|proficiency)\s+(?:are|is)\s+(?:required|essential|a must|mandatory)\b|\bmust (?:speak|be fluent in)\s+(?P<lang4>{_LANG})\b|\b(?P<lang5>{_LANG})[- ]speaking\b", re.I)
LANG_PREFERRED = re.compile(rf"\b(?P<lang>{_LANG})\s+(?:is|would be)\s+(?:a plus|an advantage|nice to have|preferred|a bonus|appreciated|welcome)\b|\b(?:bonus|plus|preferred|nice to have)\s*:?\s*(?P<lang2>{_LANG})\b", re.I)
LANG_WORKING = re.compile(rf"\b(?P<lang>{_LANG})\s+is\s+(?:our|the)\s+(?:working|company|office|main)\s+language\b|\bwe (?:work|communicate|speak) in\s+(?P<lang2>{_LANG})\b|\b(?:working|company|office) language\s+(?:is|:)\s+(?P<lang3>{_LANG})\b", re.I)

CONTRACT_WORDS = re.compile(r"\b(freelance|freelancer|zzp|zzp'er|contractor|contract (?:role|position|basis|assignment)|b2b contract|interim|opdracht van \d+|fixed[- ]term contract|\d+[- ]month contract|temporary contract|day rate|hourly rate)\b", re.I)
INTERN_WORDS = re.compile(r"\b(internship|intern\b|stage(?:plaats|opdracht)?|werkstudent|working student|trainee(?:ship)?|apprentice(?:ship)?)\b", re.I)
PARTTIME_WORDS = re.compile(r"\b(part[- ]time|parttime|deeltijd|\d{1,2}\s*(?:-|to)\s*\d{1,2}\s*hours? (?:a|per) week|\b(?:16|20|24|28|32)\s*(?:hours?|uur)\b)", re.I)
TEMP_WORDS = re.compile(r"\b(temporary|tijdelijk|fixed[- ]term|seasonal|maternity cover)\b", re.I)


@dataclass
class Requirements:
    essential: set[str] = field(default_factory=set)
    preferred: set[str] = field(default_factory=set)
    incidental: set[str] = field(default_factory=set)
    negated: set[str] = field(default_factory=set)

    @property
    def asked(self) -> set[str]:
        """What the job really asks for: the essentials, or every non-negated tool when no requirement text exists."""
        return set(self.essential)


def _dedupe_families(keys: set[str]) -> set[str]:
    """A family word next to a specific tool adds nothing: "monitoring with Datadog" asks for Datadog."""
    out = set(keys)
    for family, members in FAMILIES.items():
        if family in out and any(m in out for m in members):
            out.discard(family)
    return out


def _section_kind(line: str) -> str | None:
    s = line.strip()
    if len(s) > 70 or len(s) < 3:
        return None
    header_like = s.endswith(":") or len(s.split()) <= 7
    if not header_like:
        return None
    if SECTION_PREFERRED.match(s):
        return "preferred"
    if SECTION_CONTEXT.match(s):
        return "context"
    if SECTION_ESSENTIAL.match(s):
        return "essential"
    return None


def parse(description: str | None, title: str | None = None) -> Requirements:
    req = Requirements()
    text = description or ""
    section = "essential"
    seen_header = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        kind = _section_kind(line)
        if kind:
            section = kind
            seen_header = True
            if len(line.split()) <= 4:
                continue
        elif section == "preferred" and not re.match(r"^[-*•·▪◦]|^\d+[.)]", line) and (len(line) > 40 or line.endswith((".", "!"))) and not PREFERRED_CUE.search(line):
            section = "essential"  # a "nice to have" list is a list; a full sentence after it is the posting again
        for m in NEGATION_CUE.finditer(line):
            phrase = m.group("tool") or m.group("tool2") or m.group("tool3") or ""
            req.negated |= extract_skills(phrase, implied=False)
        tools = _dedupe_families(extract_skills(line, implied=False)) - req.negated
        if not tools:
            continue
        if INCIDENTAL_CUE.search(line):
            req.incidental |= tools
        elif section == "preferred" or PREFERRED_CUE.search(line):
            req.preferred |= tools
        elif section == "context":
            req.incidental |= tools
        else:
            req.essential |= tools
    if title:
        req.essential |= _dedupe_families(extract_skills(title, implied=False)) - req.negated
    req.essential -= GENERIC | TRIVIAL
    req.preferred -= req.essential | GENERIC | TRIVIAL
    req.incidental -= req.essential | req.preferred | GENERIC | TRIVIAL
    if not seen_header and not req.essential:
        # No structure at all: everything named (and not negated) is what they ask for, as before.
        req.essential = req.incidental | req.preferred
        req.preferred = set()
        req.incidental = set()
    return req


def languages(description: str | None) -> list[tuple[str, str]]:
    """[(language, strength)] in order of appearance: required, preferred, or working language."""
    text = description or ""
    found: dict[str, tuple[int, str]] = {}

    def note(pos: int, lang: str, strength: str) -> None:
        name = lang.capitalize()
        if name not in found or (found[name][1] != "required" and strength == "required"):
            found[name] = (pos, strength)

    for m in LANG_WORKING.finditer(text):
        note(m.start(), m.group("lang") or m.group("lang2") or m.group("lang3"), "working language")
    for m in LANG_PREFERRED.finditer(text):
        note(m.start(), m.group("lang") or m.group("lang2"), "preferred")
    for m in LANG_REQUIRED.finditer(text):
        lang = next((m.group(g) for g in ("lang", "lang2", "lang3", "lang4", "lang5") if m.groupdict().get(g)), None)
        if lang:
            prev = found.get(lang.capitalize())
            if prev and prev[1] == "working language":
                continue
            note(m.start(), lang, "required")
    return [(name, strength) for name, (pos, strength) in sorted(found.items(), key=lambda kv: kv[1][0])]


def employment_label(raw_type: str | None, description: str | None = None) -> str | None:
    """One contract vocabulary: Full-time, Part-time, Contract, Temporary, Internship. From the provider's field
    first, then from the text; None when nothing says."""
    t = (raw_type or "").strip().lower().replace("_", " ").replace("-", " ")
    if t:
        if re.search(r"\b(intern|internship|trainee|apprentice|werkstudent)\b", t):
            return "Internship"
        if re.search(r"\b(contract|contractor|freelance|freelancer|zzp|interim|b2b)\b", t):
            return "Contract"
        if re.search(r"\b(temporary|temp|fixed term|seasonal)\b", t):
            return "Temporary"
        if re.search(r"\b(part ?time|deeltijd)\b", t):
            return "Part-time"
        if re.search(r"\b(full ?time|fulltime|permanent|regular|vast)\b", t):
            return "Full-time"
    head = (description or "")[:2500]
    if INTERN_WORDS.search(head) and not re.search(r"\b(mentor|supervise|coach)\b.{0,40}\b(interns?|trainees?)\b", head, re.I):
        return "Internship"
    if CONTRACT_WORDS.search(head):
        return "Contract"
    if TEMP_WORDS.search(head):
        return "Temporary"
    if PARTTIME_WORDS.search(head):
        return "Part-time"
    return raw_type.strip() if raw_type and raw_type.strip() else None
