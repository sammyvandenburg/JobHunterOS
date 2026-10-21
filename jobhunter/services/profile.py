"""Candidate profile: facts with evidence, rule-based draft, validation, approval.

The rule-based builder is deliberately conservative. Claude (in the user's
session) can submit a richer draft, but every fact is validated here: document
evidence must point at a real document and the quote must appear in its text.
Unsupported facts are downgraded to "needs clarification", never approved
silently. Guided/personal projects are never marked professional by rules.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date

from jobhunter.db import Database
from jobhunter.models import EvidenceRef, Fact, ProfileDraft, ProfileQuestion, ProfileTargets
from jobhunter.services import documents as docs

# ---------------------------------------------------------------------------
# Vocabularies (kept small on purpose; the model can add facts with evidence)
# ---------------------------------------------------------------------------
from jobhunter.services.matching.skills import extract_skills  # noqa: E402  (shared vocabulary)
from jobhunter.util import now_iso

CERT_PATTERNS: list[tuple[str, str]] = [
    (r"\bCKA\b|Certified Kubernetes Administrator", "Certified Kubernetes Administrator (CKA)"),
    (r"\bCKAD\b|Certified Kubernetes Application Developer", "Certified Kubernetes Application Developer (CKAD)"),
    (r"\bCKS\b|Certified Kubernetes Security Specialist", "Certified Kubernetes Security Specialist (CKS)"),
    (r"\bKCNA\b", "Kubernetes and Cloud Native Associate (KCNA)"),
    (r"AWS Certified Solutions Architect", "AWS Certified Solutions Architect"),
    (r"AWS Certified (?:Cloud Practitioner|Developer|SysOps|DevOps)", "AWS certification"),
    (r"\bAZ-\d{3}\b|Azure (?:Administrator|Fundamentals|DevOps Engineer)", "Microsoft Azure certification"),
    (r"Terraform Associate|HashiCorp Certified", "HashiCorp Terraform Associate"),
    (r"\bRHCSA\b|\bRHCE\b", "Red Hat certification (RHCSA/RHCE)"),
    (r"\bLPIC-?\d\b|CompTIA Linux\+", "Linux certification (LPIC/CompTIA Linux+)"),
    (r"Google Cloud (?:Associate|Professional)", "Google Cloud certification"),
]
CERT_CONTEXT = re.compile(r"certif(?:ied|icate|ication)", re.I)
IN_PROGRESS = re.compile(r"in progress|studying|preparing|planned|expected|pursuing|currently", re.I)
COURSE_WORDS = re.compile(r"\b(course|bootcamp|udemy|coursera|kodekloud|kubecraft|training|curriculum|learning path|a cloud guru|linux academy)\b", re.I)

MONTHS = "jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec|january|february|march|april|june|july|august|september|october|november|december"
DATE_RANGE = re.compile(
    rf"((?:(?:{MONTHS})\.?\s+)?(?:19|20)\d{{2}})\s*(?:-|–|—|to|until)\s*((?:(?:{MONTHS})\.?\s+)?(?:19|20)\d{{2}}|present|current|now|today)",
    re.I,
)
SECTION_HEADINGS = {
    "experience": re.compile(r"^\s*(work |professional |relevant )?experience\s*:?\s*$|^\s*employment( history)?\s*:?\s*$", re.I),
    "projects": re.compile(r"^\s*(personal |side |selected |key )?projects?\s*:?\s*$|^\s*home ?lab\s*:?\s*$", re.I),
    "education": re.compile(r"^\s*education\s*:?\s*$|^\s*academic.*$", re.I),
    "skills": re.compile(r"^\s*(technical |core |key )?skills?( & tools| and tools)?\s*:?\s*$|^\s*technologies\s*:?\s*$", re.I),
    "certifications": re.compile(r"^\s*certifications?( & courses| and courses)?\s*:?\s*$|^\s*(courses|training)\s*:?\s*$", re.I),
    "summary": re.compile(r"^\s*(summary|profile|about( me)?|objective)\s*:?\s*$", re.I),
}
ROLE_TITLE = re.compile(
    r"\b(engineer|administrator|admin|developer|architect|analyst|consultant|specialist|technician|intern|trainee|manager|lead|operator|support|sre|devops|sysadmin)\b",
    re.I,
)
ROLE_INFRA = re.compile(
    r"\b(devops|dev ops|sre|site reliability|platform (engineer|engineering)|infrastructure|infra|cloud (engineer|architect|administrator|ops|operations|specialist)|systems? (engineer|administrator)|sysadmin|"
    r"linux (engineer|administrator|admin|specialist)|unix (engineer|administrator)|kubernetes|network (engineer|administrator)|release engineer|build engineer|automation engineer|observability|it operations|"
    r"operations engineer|datacenter|data center|hosting engineer|technical operations|noc engineer|devsecops|storage engineer|virtuali[sz]ation)\b",
    re.I,
)
ROLE_SUPPORT = re.compile(r"\b(helpdesk|help desk|service desk|desktop support|it support|support (engineer|analyst|technician|specialist|agent|associate)|it technician|field (service )?(engineer|technician)|1st line|first line|2nd line|second line|it specialist|technical support|it analyst|it assistant|it administrator|it coordinator)\b", re.I)
ROLE_SOFTWARE = re.compile(r"\b(software|developer|programmer|full ?stack|backend|back-end|frontend|front-end|web engineer|application engineer|data engineer|qa engineer|test engineer|mobile|ios|android|data analyst|business analyst)\b", re.I)
ROLE_OTHER = re.compile(r"\b(nurse|nursing|physician|doctor|teacher|lecturer|sales|marketing|accountant|lawyer|paralegal|driver|chef|cook|cashier|retail|warehouse|barista|waiter|waitress|hospitality|hotel|customer service|recruiter|hr |human resources|finance|logistics|carpenter|electrician|plumber|mechanic|security guard|soldier|military|police|firefighter|pharmacist|dentist|therapist|social worker|caregiver|cleaner|receptionist|administrative assistant|office manager|project manager|consultant)\b", re.I)
INFRA_CONTEXT = re.compile(
    r"\b(servers?|linux|unix|windows server|active directory|deploy(ed|ment|ments|ing)?|kubernetes|k8s|docker|containers?|terraform|ansible|puppet|chef|aws|azure|gcp|cloud|on-call|incidents?|monitoring|prometheus|grafana|nagios|zabbix|"
    r"backups?|patching|patched|provision(ed|ing)?|automat(ed|ion|ing)|ci/cd|pipelines?|networks?|networking|firewalls?|vlan|dns|dhcp|vmware|vsphere|hyper-v|virtuali[sz]ation|infrastructure|datacenter|data center|storage|san\b|nas\b|load balanc\w*|nginx|apache|bash|powershell|scripts?|scripting)\b",
    re.I,
)
SCOPE_LEAD = re.compile(r"\b(led|lead|leading|managed|managing|mentored|mentoring|supervised|supervising|coached|head of)\s+(?:a\s+|the\s+|an\s+|our\s+)?(?:[\w-]+\s+){0,2}?(team|teams|people|engineers|developers|staff|reports|interns|juniors|squad|department)\b|\bteam of \d+\b|\btech lead\b|\bteam lead\b|\bengineering manager\b", re.I)
SCOPE_OWNER = re.compile(r"\b(owned|owner|ownership|responsible for|architected|designed|migrated|migration|built|operated|ran|on-call|incident|production|sla|uptime|reliability|introduced|rolled out|maintained)\b", re.I)
SCOPE_RANK = {"none": 0, "contributor": 1, "owner": 2, "lead": 3}
RELEVANT_DOMAINS = {"infra", "infra_adjacent"}
ADJACENT_DOMAINS = {"it_support", "software", "unclear"}


def classify_role(title: str, context: str = "") -> tuple[str, str]:
    """(domain, scope) for one paid role.

    domain: infra (owned or ran infrastructure), infra_adjacent (an IT role whose bullets show real
    infrastructure work), it_support (general IT help), software, unclear (an IT-sounding title with
    nothing to go on), other (a different career). scope: lead | owner | contributor.
    General IT support, infrastructure ownership and senior platform responsibility are kept apart.
    """
    t = title or ""
    ctx = context or ""
    hits = len({m.group(0).lower() for m in INFRA_CONTEXT.finditer(ctx)})
    if ROLE_SUPPORT.search(t):
        domain = "infra_adjacent" if hits >= 3 else "it_support"
    elif ROLE_INFRA.search(t):
        domain = "infra"
    elif ROLE_SOFTWARE.search(t):
        domain = "infra_adjacent" if hits >= 4 else "software"
    elif ROLE_OTHER.search(t):
        domain = "other"
    elif ROLE_TITLE.search(t):
        domain = "infra" if hits >= 3 else "unclear"
    else:
        domain = "infra_adjacent" if hits >= 4 else "other"
    scope = "contributor"
    if domain != "other" and not re.search(r"\b(junior|intern|trainee|apprentice|graduate|werkstudent)\b", t, re.I):
        if SCOPE_LEAD.search(ctx) or SCOPE_LEAD.search(t):
            scope = "lead"
        elif SCOPE_OWNER.search(ctx) or re.search(r"\b(senior|lead|principal|staff|head)\b", t, re.I):
            scope = "owner"
    return domain, scope


def role_domain(fact: Fact) -> str:
    for tag in fact.tags:
        if tag.startswith("domain:"):
            return tag.split(":", 1)[1]
    domain, _ = classify_role(fact.text, fact.text)
    return domain


def role_scope(fact: Fact) -> str:
    for tag in fact.tags:
        if tag.startswith("scope:"):
            return tag.split(":", 1)[1]
    _, scope = classify_role(fact.text, fact.text)
    return scope


def derive_experience(facts: list[Fact], today: date | None = None) -> dict:
    """Years and level from paid roles by what the work was, never from the length of a different career."""
    roles = [f for f in facts if f.category == "role" and f.experience_kind == "professional" and f.status != "rejected" and f.start and f.end]
    by_domain: dict[str, list[tuple[str, str]]] = {}
    for f in roles:
        by_domain.setdefault(role_domain(f), []).append((f.start, f.end))
    years = lambda keys: round(merged_months([iv for k in keys for iv in by_domain.get(k, [])], today) / 12.0, 1)  # noqa: E731
    total = years(by_domain.keys())
    relevant = years(RELEVANT_DOMAINS)
    infra_only = years({"infra"})
    adjacent = years(ADJACENT_DOMAINS)
    scopes = [role_scope(f) for f in roles if role_domain(f) in RELEVANT_DOMAINS]
    responsibility = max(scopes, key=lambda x: SCOPE_RANK.get(x, 0)) if scopes else "none"
    # Senior means years of real infrastructure roles with ownership or leadership, not years of IT support
    # that touched servers. Adjacent years still count towards mid.
    if relevant >= 5 and infra_only >= 4 and responsibility in {"owner", "lead"}:
        level = "senior"
    elif relevant >= 2.5:
        level = "mid"
    elif relevant >= 1:
        level = "junior"
    elif adjacent >= 2:
        level = "junior"
    else:
        level = "entry"
    notes = []
    for f in roles:
        d = role_domain(f)
        short = f.text.split(" (")[0][:60]
        if d == "it_support":
            notes.append(f"I counted '{short}' as general IT support, not infrastructure work. Tell me if that is wrong.")
        elif d == "software":
            notes.append(f"I counted '{short}' as software work, which helps but is not infrastructure work.")
        elif d == "unclear":
            notes.append(f"I could not tell what kind of work '{short}' was, so I did not count it as infrastructure work. Tell me if it was.")
        elif d == "other":
            notes.append(f"'{short}' is a different career, so it does not count as IT experience here.")
    return {
        "professional_years_total": total,
        "professional_years_relevant": relevant,
        "professional_years_adjacent": adjacent,
        "responsibility": responsibility,
        "level": level,
        "experience_note": " ".join(notes[:3]) or None,
        "has_relevant_roles": any(role_domain(f) in RELEVANT_DOMAINS for f in roles),
    }


MONTH_INDEX = {m: i + 1 for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}


class ProfileError(Exception):
    pass


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------
def parse_month(token: str) -> str | None:
    """'Jan 2021' -> '2021-01'; '2021' -> '2021-01'; 'present' -> 'present'."""
    t = token.strip().lower().rstrip(".")
    if t in {"present", "current", "now", "today"}:
        return "present"
    m = re.match(rf"(?:({MONTHS})\.?\s+)?((?:19|20)\d{{2}})", t)
    if not m:
        return None
    month = 1
    if m.group(1):
        month = MONTH_INDEX.get(m.group(1)[:3], 1)
    return f"{m.group(2)}-{month:02d}"


def _ym_to_index(ym: str, today: date | None = None) -> int:
    today = today or date.today()
    if ym == "present":
        return today.year * 12 + today.month
    y, m = ym.split("-")
    return int(y) * 12 + int(m)


def merged_months(intervals: list[tuple[str, str]], today: date | None = None) -> int:
    """Total months covered by the union of (start, end) YYYY-MM intervals.

    Overlapping jobs are counted once; this is how we avoid double-counting.
    """
    spans = []
    for start, end in intervals:
        try:
            a, b = _ym_to_index(start, today), _ym_to_index(end, today)
        except (ValueError, AttributeError):
            continue
        if b < a:
            a, b = b, a
        spans.append((a, b + 1))
    spans.sort()
    total = 0
    cur_a, cur_b = None, None
    for a, b in spans:
        if cur_a is None:
            cur_a, cur_b = a, b
        elif a <= cur_b:
            cur_b = max(cur_b, b)
        else:
            total += cur_b - cur_a
            cur_a, cur_b = a, b
    if cur_a is not None:
        total += cur_b - cur_a
    return total


# ---------------------------------------------------------------------------
# Rule-based draft builder
# ---------------------------------------------------------------------------
@dataclass
class _Section:
    name: str
    lines: list[str]


def split_sections(text: str) -> list[_Section]:
    sections: list[_Section] = [_Section("summary", [])]
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("[[page"):
            continue
        matched = None
        if len(line) <= 48:
            for name, rx in SECTION_HEADINGS.items():
                if rx.match(line):
                    matched = name
                    break
        if matched:
            sections.append(_Section(matched, []))
        else:
            sections[-1].lines.append(line)
    return sections


def _quote(text: str, limit: int = 160) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def build_rule_based_draft(db: Database) -> ProfileDraft:
    """Build a conservative draft from the current CV (and LinkedIn text if present)."""
    cv = docs.current_document(db, "cv")
    if not cv:
        raise ProfileError("No CV on file yet. Add your finished CV first.")
    if cv["extraction_status"] != "ok":
        raise ProfileError(f"The CV could not be read: {cv['extraction_notes']}")
    cv_text = docs.document_text(db, cv["id"])
    linkedin = docs.current_document(db, "linkedin")
    li_text = docs.document_text(db, linkedin["id"]) if linkedin and linkedin["extraction_status"] == "ok" else ""
    gh = docs.current_document(db, "github")

    facts: list[Fact] = []
    questions: list[ProfileQuestion] = []
    counter = 0

    def nid(prefix: str) -> str:
        nonlocal counter
        counter += 1
        return f"{prefix}{counter}"

    sections = split_sections(cv_text)
    role_intervals: list[tuple[str, str]] = []
    seen_roles: set[str] = set()

    for sec in sections:
        if sec.name in {"experience", "summary"}:
            for i, line in enumerate(sec.lines):
                m = DATE_RANGE.search(line)
                if not m:
                    continue
                start, end = parse_month(m.group(1)), parse_month(m.group(2))
                if not start or not end:
                    continue
                context = " ".join(sec.lines[max(0, i - 1) : i + 2])
                title_line = line if ROLE_TITLE.search(line) else (sec.lines[i - 1] if i > 0 else line)
                key = f"{title_line.lower()}|{start}|{end}"
                if key in seen_roles:
                    continue
                seen_roles.add(key)
                kind = "professional" if sec.name == "experience" else "unknown"
                if re.search(r"\b(intern|internship|trainee|apprentice)\b", context, re.I):
                    kind = "professional"
                # Bullets under the role (until the next dated line) say what the work really was.
                bullets = []
                for follow in sec.lines[i + 1 : i + 12]:
                    if DATE_RANGE.search(follow):
                        break
                    bullets.append(follow)
                domain, scope = classify_role(title_line, " ".join(bullets))
                facts.append(
                    Fact(
                        id=nid("r"),
                        category="role",
                        text=(title_line.strip() if title_line is line else f"{title_line.strip()} ({start} to {end})"),
                        confidence="explicit",
                        experience_kind=kind,  # type: ignore[arg-type]
                        start=start,
                        end=end,
                        evidence=[EvidenceRef(type="document", ref=f"document:{cv['id']}", quote=_quote(line))],
                        tags=[f"domain:{domain}", f"scope:{scope}"],
                    )
                )
                if kind == "professional":
                    role_intervals.append((start, end))
        elif sec.name == "projects":
            for line in sec.lines:
                if len(line) < 12:
                    continue
                guided = bool(COURSE_WORDS.search(line))
                facts.append(
                    Fact(
                        id=nid("p"),
                        category="project",
                        text=line,
                        confidence="explicit",
                        experience_kind="guided" if guided else "personal",
                        evidence=[EvidenceRef(type="document", ref=f"document:{cv['id']}", quote=_quote(line))],
                    )
                )
        elif sec.name == "education":
            for line in sec.lines:
                if len(line) >= 8:
                    facts.append(
                        Fact(
                            id=nid("e"),
                            category="education",
                            text=line,
                            experience_kind="educational",
                            evidence=[EvidenceRef(type="document", ref=f"document:{cv['id']}", quote=_quote(line))],
                        )
                    )

    # Skills: look at the whole CV, keep the line as evidence. Whole words only, canonical keys,
    # the same vocabulary the job side uses ("AKS" is AKS and implies Kubernetes; it is not "Azure").
    low_lines = [(ln, ln.lower()) for ln in cv_text.splitlines() if ln.strip()]
    seen_skills: set[str] = set()
    for ln, _low in low_lines:
        for key in sorted(extract_skills(ln)):
            if key in seen_skills:
                continue
            seen_skills.add(key)
            facts.append(
                Fact(
                    id=nid("s"),
                    category="skill",
                    text=key,
                    confidence="explicit",
                    evidence=[EvidenceRef(type="document", ref=f"document:{cv['id']}", quote=_quote(ln))],
                    tags=["skill"],
                )
            )

    # Certifications vs courses: a course is not a certification.
    for pattern, label in CERT_PATTERNS:
        rx = re.compile(pattern, re.I)
        for ln, _ in low_lines:
            if rx.search(ln):
                if IN_PROGRESS.search(ln) or (COURSE_WORDS.search(ln) and not CERT_CONTEXT.search(ln)):
                    facts.append(
                        Fact(
                            id=nid("c"),
                            category="course",
                            text=f"{label} (course or in progress, not an earned certification)",
                            evidence=[EvidenceRef(type="document", ref=f"document:{cv['id']}", quote=_quote(ln))],
                        )
                    )
                else:
                    facts.append(
                        Fact(
                            id=nid("c"),
                            category="certification",
                            text=label,
                            status="needs_clarification" if not CERT_CONTEXT.search(cv_text) else "confirmed",
                            evidence=[EvidenceRef(type="document", ref=f"document:{cv['id']}", quote=_quote(ln))],
                        )
                    )
                break
    for ln, _low in low_lines:
        if COURSE_WORDS.search(ln) and not any(f.evidence and f.evidence[0].quote == _quote(ln) for f in facts):
            facts.append(
                Fact(
                    id=nid("c"),
                    category="course",
                    text=_quote(ln, 120),
                    evidence=[EvidenceRef(type="document", ref=f"document:{cv['id']}", quote=_quote(ln))],
                )
            )

    # GitHub evidence: never professional, ownership needs confirmation.
    if gh:
        try:
            payload = json.loads(docs.document_text(db, gh["id"]) or "{}")
        except json.JSONDecodeError:
            payload = {}
        for repo in payload.get("repositories", []):
            kind = "guided" if any(f.startswith("guided") for f in repo.get("flags", [])) else "personal"
            facts.append(
                Fact(
                    id=nid("g"),
                    category="project",
                    text=f"GitHub repository {repo.get('full_name')}: {repo.get('description') or 'no description'}",
                    confidence="explicit",
                    experience_kind=kind,
                    status="needs_clarification" if repo.get("fork") or repo.get("ownership") == "unknown" else "confirmed",
                    evidence=[EvidenceRef(type="repository", ref=repo.get("html_url") or repo.get("full_name", ""))],
                    tags=repo.get("languages", [])[:5],
                )
            )
            if repo.get("fork") or repo.get("ownership") == "unknown":
                questions.append(
                    ProfileQuestion(
                        key=f"github_ownership:{repo.get('full_name')}",
                        question=f"Is {repo.get('full_name')} your own work, a team project, or a tutorial you followed?",
                        reason="Hunter does not assume authorship from a repository link.",
                    )
                )

    # Conflicts between CV and LinkedIn roles need clarification, not silent reconciliation.
    if li_text:
        li_ranges = {(parse_month(m.group(1)), parse_month(m.group(2))) for m in DATE_RANGE.finditer(li_text)}
        cv_ranges = {(f.start, f.end) for f in facts if f.category == "role"}
        li_years = {s[:4] for s, _ in li_ranges if s}
        cv_years = {s[:4] for s, _ in cv_ranges if s}
        if li_ranges and cv_ranges and li_years != cv_years:
            questions.append(
                ProfileQuestion(
                    key="cv_linkedin_dates",
                    question="Your CV and LinkedIn profile show different job dates. Which dates are correct?",
                    reason="Hunter does not guess when two documents disagree.",
                )
            )

    if not role_intervals:
        questions.append(
            ProfileQuestion(
                key="professional_experience",
                question="I could not find paid work experience with dates in your CV. Do you have any? If yes, what role and when?",
                reason="This decides whether to look at entry-level or next-level jobs.",
            )
        )
    questions.append(
        ProfileQuestion(
            key="target_direction",
            question="Which direction do you want most: DevOps, Platform Engineering, SRE, Cloud, or Infrastructure? You can name more than one.",
            reason="Hunter searches widely, but this sets the order of results.",
        )
    )

    exp = derive_experience(facts)
    summary = _summary_text(facts, exp)
    return ProfileDraft(
        summary_text=summary,
        facts=facts,
        targets=ProfileTargets(direction=[], stretch_ok=True, **{k: v for k, v in exp.items() if k != "has_relevant_roles"}),  # type: ignore[arg-type]
        questions=questions,
        professional_years_relevant=exp["professional_years_relevant"],
    )


def _summary_text(facts: list[Fact], exp: dict) -> str:
    roles = [f for f in facts if f.category == "role" and f.experience_kind == "professional" and f.status != "rejected"]
    skills = [f.text for f in facts if f.category == "skill" and f.status != "rejected"]
    certs = [f.text for f in facts if f.category == "certification" and f.status != "rejected"]
    projects = [f for f in facts if f.category == "project"]
    parts = []
    total, rel, adj = exp.get("professional_years_total", 0), exp.get("professional_years_relevant", 0), exp.get("professional_years_adjacent", 0)
    if roles:
        line = f"About {total:g} years of paid work across {len(roles)} role(s)"
        if rel:
            line += f", of which about {rel:g} years infrastructure or systems work"
        if adj:
            line += f" and about {adj:g} years general IT or software work"
        parts.append(line + ".")
    else:
        parts.append("No paid work experience with dates found yet.")
    if exp.get("experience_note"):
        parts.append(exp["experience_note"])
    if skills:
        parts.append("Skills with evidence: " + ", ".join(skills[:12]) + ".")
    if certs:
        parts.append("Certifications: " + ", ".join(certs) + ".")
    if projects:
        parts.append(f"{len(projects)} project(s) (personal or guided, not production employment).")
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Validation of any submitted draft (rules or Claude)
# ---------------------------------------------------------------------------
def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def validate_draft(db: Database, draft: ProfileDraft) -> tuple[ProfileDraft, list[str]]:
    """Check evidence references. Returns (adjusted draft, warnings)."""
    warnings: list[str] = []
    doc_cache: dict[int, str] = {}
    known_ids = {r["id"] for r in db.fetchall("SELECT id FROM source_documents")}
    seen_ids: set[str] = set()
    cleaned: list[Fact] = []
    for fact in draft.facts:
        if fact.id in seen_ids:
            fact.id = f"{fact.id}_{len(seen_ids)}"
        seen_ids.add(fact.id)
        supported = False
        for ev in fact.evidence:
            if ev.type == "document":
                m = re.match(r"document:(\d+)", ev.ref)
                if not m or int(m.group(1)) not in known_ids:
                    warnings.append(f"Fact '{fact.text[:60]}' points at an unknown document ({ev.ref}).")
                    continue
                did = int(m.group(1))
                if did not in doc_cache:
                    doc_cache[did] = _norm(docs.document_text(db, did))
                if ev.quote:
                    if _norm(ev.quote)[:120] in doc_cache[did]:
                        supported = True
                    else:
                        warnings.append(f"Quote for '{fact.text[:60]}' was not found in document {did}.")
                else:
                    supported = True  # section reference without quote: accepted but weaker
            elif ev.type == "user":
                supported = True
            elif ev.type in {"repository", "link"}:
                supported = True
        if fact.category in {"role", "certification", "achievement"} and not supported:
            fact.status = "needs_clarification"
            fact.confidence = "inferred"
            warnings.append(f"'{fact.text[:60]}' has no supporting evidence and needs your confirmation.")
        # Rules never allow a project to count as professional experience without the user saying so.
        if fact.category == "project" and fact.experience_kind == "professional":
            if not any(ev.type == "user" for ev in fact.evidence):
                fact.experience_kind = "personal"
                warnings.append(f"Project '{fact.text[:50]}' was marked personal; only you can confirm production employment.")
        cleaned.append(fact)
    draft.facts = cleaned
    # Experience is derived the same way for every path: from paid roles, by what the work was.
    # Claude may suggest a level in its draft; the derived one wins when it is lower (no inflation).
    exp = derive_experience(cleaned)
    order = ["entry", "junior", "mid", "senior", "staff"]
    suggested = draft.targets.level if draft.targets.level in order else None
    derived = exp["level"]
    level = derived if not suggested or order.index(derived) <= order.index(suggested) else suggested
    draft.targets = draft.targets.model_copy(update={**{k: v for k, v in exp.items() if k != "has_relevant_roles"}, "level": level})
    draft.professional_years_relevant = exp["professional_years_relevant"]
    if suggested and order.index(suggested) > order.index(derived):
        warnings.append(f"The draft said level '{suggested}', but the paid roles support '{derived}'. Using '{derived}'.")
    return draft, warnings


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------
def save_draft(db: Database, draft: ProfileDraft, built_by: str) -> dict:
    draft, warnings = validate_draft(db, draft)
    doc_ids = [r["id"] for r in docs.list_documents(db)]
    prev = db.fetchone("SELECT MAX(version) AS v FROM profile_versions")
    version = (prev["v"] or 0) + 1
    with db.tx():
        db.execute("UPDATE profile_versions SET status = 'superseded' WHERE status = 'draft'")
        pid = db.insert(
            "profile_versions",
            {
                "version": version,
                "status": "draft",
                "summary_text": draft.summary_text,
                "facts_json": json.dumps([f.model_dump(mode="json") for f in draft.facts]),
                "targets_json": json.dumps(
                    {**draft.targets.model_dump(mode="json"), "professional_years_relevant": draft.professional_years_relevant}
                ),
                "document_ids_json": json.dumps(doc_ids),
                "built_by": built_by,
                "created_at": now_iso(),
            },
        )
        existing = {q["key"] for q in db.fetchall("SELECT key FROM profile_questions WHERE status != 'skipped'")}
        for q in draft.questions:
            if q.key in existing:
                continue
            db.insert(
                "profile_questions",
                {
                    "profile_version_id": pid,
                    "key": q.key,
                    "question": q.question,
                    "reason": q.reason,
                    "created_at": now_iso(),
                },
            )
        db.set_setting("profile.last_warnings", warnings)
    return {"profile_version_id": pid, "version": version, "warnings": warnings, "facts": len(draft.facts)}


def load_version(db: Database, profile_id: int | None = None, status: str | None = None) -> dict | None:
    if profile_id is not None:
        row = db.fetchone("SELECT * FROM profile_versions WHERE id = ?", (profile_id,))
    elif status:
        row = db.fetchone("SELECT * FROM profile_versions WHERE status = ? ORDER BY version DESC LIMIT 1", (status,))
    else:
        row = db.fetchone("SELECT * FROM profile_versions ORDER BY version DESC LIMIT 1")
    if not row:
        return None
    row["facts"] = [Fact.model_validate(f) for f in json.loads(row["facts_json"])]
    row["targets"] = json.loads(row["targets_json"] or "{}")
    return row


def approved(db: Database) -> dict | None:
    return load_version(db, status="approved")


def current_draft(db: Database) -> dict | None:
    return load_version(db, status="draft")


def approve(db: Database, profile_id: int | None = None, summary_override: str | None = None) -> dict:
    row = load_version(db, profile_id) if profile_id else current_draft(db)
    if not row:
        raise ProfileError("There is no profile draft to approve. Build one first.")
    open_q = db.fetchall("SELECT key FROM profile_questions WHERE status = 'open'")
    with db.tx():
        db.execute("UPDATE profile_versions SET status = 'superseded' WHERE status = 'approved'")
        values = {"status": "approved", "approved_at": now_iso()}
        if summary_override:
            values["summary_text"] = summary_override
        db.update("profile_versions", values, "id = ?", (row["id"],))
        db.delete_setting("profile.needs_review")
    return {"profile_version_id": row["id"], "version": row["version"], "open_questions": [q["key"] for q in open_q]}


def answer_question(db: Database, key: str, answer: str) -> dict:
    q = db.fetchone("SELECT * FROM profile_questions WHERE key = ? AND status = 'open' ORDER BY id DESC LIMIT 1", (key,))
    if not q:
        raise ProfileError(f"No open question with key '{key}'.")
    with db.tx():
        db.update("profile_questions", {"status": "answered", "answer": answer, "answered_at": now_iso()}, "id = ?", (q["id"],))
        _apply_answer_to_draft(db, key, answer)
    return {"key": key, "answered": True}


def skip_question(db: Database, key: str) -> None:
    with db.tx():
        db.update("profile_questions", {"status": "skipped", "answered_at": now_iso()}, "key = ? AND status = 'open'", (key,))


def open_questions(db: Database) -> list[dict]:
    return db.fetchall("SELECT * FROM profile_questions WHERE status = 'open' ORDER BY id")


def _apply_answer_to_draft(db: Database, key: str, answer: str) -> None:
    """Record user answers as facts with 'user' evidence on the current draft."""
    row = current_draft(db) or approved(db)
    if not row:
        return
    facts = row["facts"]
    today = now_iso()[:10]
    if key == "target_direction":
        targets = row["targets"]
        words = [w.strip().lower() for w in re.split(r"[,/;]|\band\b", answer) if w.strip()]
        mapping = {"devops": "devops", "platform": "platform", "platform engineering": "platform", "sre": "sre", "site reliability": "sre", "cloud": "cloud", "infrastructure": "infrastructure", "infra": "infrastructure", "linux": "systems", "systems": "systems"}
        targets["direction"] = sorted({mapping[w] for w in words if w in mapping} | {mapping[k] for k in mapping if any(k in w for w in words)})
        db.update("profile_versions", {"targets_json": json.dumps(targets)}, "id = ?", (row["id"],))
        return
    if key.startswith("github_ownership:"):
        repo = key.split(":", 1)[1]
        low = answer.lower()
        kind = "team" if "team" in low else "guided" if ("tutorial" in low or "course" in low or "follow" in low) else "personal"
        for f in facts:
            if f.category == "project" and repo in f.text:
                f.experience_kind = kind  # type: ignore[assignment]
                f.status = "confirmed"
                f.evidence.append(EvidenceRef(type="user", ref=f"user:{today}", quote=answer[:160]))
    else:
        facts.append(
            Fact(
                id=f"u{len(facts) + 1}",
                category="other",
                text=f"{key}: {answer}",
                confidence="asserted",
                evidence=[EvidenceRef(type="user", ref=f"user:{today}", quote=answer[:160])],
            )
        )
    db.update("profile_versions", {"facts_json": json.dumps([f.model_dump(mode="json") for f in facts])}, "id = ?", (row["id"],))


NEGATION = re.compile(r"\b(i do not|i don't|i dont|i have not|i haven't|i havent|i never|i have no|no longer|not|never|remove|delete|wrong|isn't|is not|incorrect|didn't|did not|haven't used|never used|no experience)\b", re.I)
CLAUSE_SPLIT = re.compile(r"\s*(?:,\s*but\s+|;\s*|\s+but\s+|\s+although\s+|\s+though\s+|\.\s+|\s+however\s+)\s*", re.I)


def _label(key: str) -> str:
    from jobhunter.services.matching.skills import tool_label

    return tool_label(key)


def _join_words(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def plan_correction(db: Database, text: str) -> dict:
    """Turn a correction in the person's own words into a proposal they confirm before anything changes.

    Each clause is read on its own, so "I do not use AWS, but I do use Kubernetes and Terraform" removes AWS
    and keeps Kubernetes and Terraform, never all three. Known tools and certifications are recognised; a
    sentence that names none becomes a note on the profile. Nothing is written here."""
    text = (text or "").strip()
    if not text:
        raise ProfileError("Tell me what is wrong in one sentence.")
    base = current_draft(db) or approved(db)
    if not base:
        raise ProfileError("No profile to correct yet.")
    active_skills = {k for f in base["facts"] if f.category == "skill" and f.status != "rejected" for k in extract_skills(f.text, implied=False)}
    active_certs = [f for f in base["facts"] if f.category in {"certification", "course"} and f.status != "rejected"]
    remove: list[str] = []
    keep: list[str] = []
    add: list[str] = []
    remove_certs: list[str] = []
    for clause in [c for c in CLAUSE_SPLIT.split(text) if c and c.strip()]:
        negative = bool(NEGATION.search(clause))
        keys = extract_skills(clause, implied=False)
        certs = [label for pattern, label in CERT_PATTERNS if re.search(pattern, clause, re.I)]
        for key in sorted(keys):
            if negative:
                if key in active_skills and key not in remove:
                    remove.append(key)
            elif key in active_skills:
                if key not in keep:
                    keep.append(key)
            elif key not in add:
                add.append(key)
        for label in certs:
            hit = [f.text for f in active_certs if label.lower() == f.text.lower() or label.split(" (")[0].lower() in f.text.lower()]
            if negative:
                remove_certs += [h for h in hit if h not in remove_certs]
    remove = [k for k in remove if k not in keep]
    parts = []
    if remove or remove_certs:
        parts.append("Remove " + _join_words([_label(k) for k in remove] + remove_certs) + ".")
    if keep:
        parts.append("Keep " + _join_words([_label(k) for k in keep]) + ".")
    if add:
        parts.append("Add " + _join_words([_label(k) for k in add]) + ".")
    note = None
    if not parts:
        note = text[:300]
        parts.append(f'Add this to your profile as a note: "{note}".')
    return {
        "text": text,
        "remove": [_label(k) for k in remove] + remove_certs,
        "remove_keys": remove,
        "remove_certs": remove_certs,
        "keep": [_label(k) for k in keep],
        "add": [_label(k) for k in add],
        "add_keys": add,
        "note": note,
        "confirmation": " ".join(parts) + " Correct?",
        "needs_confirmation": True,
    }


def apply_correction(db: Database, plan: dict) -> dict:
    """Write a confirmed proposal as a new profile revision. Retracted facts are kept, marked rejected, with the
    person's words as evidence; additions carry the same evidence. The revision is approved at once (the person
    wrote it); if a new document is waiting for review, the change goes into that draft instead."""
    text = str(plan.get("text") or "").strip()
    draft_row = current_draft(db)
    base = draft_row or approved(db)
    if not base:
        raise ProfileError("No profile to correct yet.")
    facts: list[Fact] = [Fact.model_validate(f.model_dump(mode="json")) for f in base["facts"]]
    today = now_iso()[:10]
    user_ev = EvidenceRef(type="user", ref=f"user:{today}", quote=text[:160])
    remove_keys = set(plan.get("remove_keys") or [])
    remove_certs = {c.lower() for c in plan.get("remove_certs") or []}
    retracted: list[str] = []
    added: list[str] = []
    for f in facts:
        if f.status == "rejected":
            continue
        if f.category == "skill" and extract_skills(f.text, implied=False) & remove_keys:
            f.status = "rejected"
            f.evidence.append(user_ev)
            retracted.append(f.text)
        elif f.category in {"certification", "course"} and f.text.lower() in remove_certs:
            f.status = "rejected"
            f.evidence.append(user_ev)
            retracted.append(f.text)
    existing = {k for f in facts if f.category == "skill" and f.status != "rejected" for k in extract_skills(f.text, implied=False)}
    for k in plan.get("add_keys") or []:
        if k in existing:
            continue
        facts.append(Fact(id=f"u{len(facts) + 1}", category="skill", text=k, confidence="asserted", evidence=[user_ev], tags=["skill"]))
        added.append(k)
    if plan.get("note"):
        facts.append(Fact(id=f"u{len(facts) + 1}", category="other", text=str(plan["note"])[:300], confidence="asserted", evidence=[user_ev]))
        added.append(str(plan["note"])[:60])
    exp = derive_experience(facts)
    targets = ProfileTargets.model_validate({**base["targets"], **{k: v for k, v in exp.items() if k != "has_relevant_roles"}})
    draft = ProfileDraft(summary_text=_summary_text(facts, exp), facts=facts, targets=targets, questions=[], professional_years_relevant=exp["professional_years_relevant"])
    if draft_row:
        draft, _ = validate_draft(db, draft)
        with db.tx():
            db.update("profile_versions", {"facts_json": json.dumps([f.model_dump(mode="json") for f in draft.facts]), "targets_json": json.dumps({**draft.targets.model_dump(mode="json"), "professional_years_relevant": draft.professional_years_relevant}), "summary_text": draft.summary_text}, "id = ?", (draft_row["id"],))
        return {"profile_version_id": draft_row["id"], "version": draft_row["version"], "retracted": retracted, "added": added, "approved": False, "note": "Added to the profile that is waiting for your review."}
    res = save_draft(db, draft, built_by="user")
    approve(db, res["profile_version_id"])
    note = ("Removed: " + ", ".join(retracted) + "." if retracted else "") + (" Added: " + ", ".join(added) + "." if added else "")
    return {"profile_version_id": res["profile_version_id"], "version": res["version"], "retracted": retracted, "added": added, "approved": True, "note": note.strip() or "Noted."}


def correct(db: Database, text: str) -> dict:
    """Plan and apply a correction in one step (the local dashboard form and tests). The command line and the
    skill plan first and apply only after the person confirms."""
    return apply_correction(db, plan_correction(db, text))


def add_user_fact(db: Database, text: str, category: str = "other", experience_kind: str = "unknown") -> dict:
    """A correction from the user updates the derived profile only."""
    row = current_draft(db) or approved(db)
    if not row:
        raise ProfileError("No profile to update yet.")
    facts = row["facts"]
    fact = Fact(
        id=f"u{len(facts) + 1}",
        category=category,  # type: ignore[arg-type]
        text=text,
        confidence="asserted",
        experience_kind=experience_kind,  # type: ignore[arg-type]
        evidence=[EvidenceRef(type="user", ref=f"user:{now_iso()[:10]}", quote=text[:160])],
    )
    facts.append(fact)
    with db.tx():
        db.update("profile_versions", {"facts_json": json.dumps([f.model_dump(mode="json") for f in facts])}, "id = ?", (row["id"],))
    return fact.model_dump(mode="json")


def profile_view(db: Database, row: dict) -> dict:
    """Plain-language view for the dashboard, CLI, and skill."""
    facts: list[Fact] = row["facts"]
    by_cat: dict[str, list[dict]] = {}
    for f in facts:
        by_cat.setdefault(f.category, []).append(
            {
                "id": f.id,
                "text": f.text,
                "kind": f.experience_kind,
                "confidence": f.confidence,
                "status": f.status,
                "evidence": [e.model_dump(mode="json") for e in f.evidence],
                "start": f.start,
                "end": f.end,
            }
        )
    return {
        "id": row["id"],
        "version": row["version"],
        "status": row["status"],
        "summary": row["summary_text"],
        "targets": row["targets"],
        "facts_by_category": by_cat,
        "needs_review": db.get_setting("profile.needs_review"),
        "open_questions": open_questions(db),
        "warnings": db.get_setting("profile.last_warnings", []),
        "built_by": row["built_by"],
        "approved_at": row.get("approved_at"),
    }


def skill_names(row: dict) -> set[str]:
    return {f.text.lower() for f in row["facts"] if f.category == "skill" and f.status != "rejected"}
