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
from jobhunter.util import now_iso

# ---------------------------------------------------------------------------
# Vocabularies (kept small on purpose; the model can add facts with evidence)
# ---------------------------------------------------------------------------
SKILL_VOCAB: dict[str, list[str]] = {
    "kubernetes": ["kubernetes", "k8s", "eks", "aks", "gke", "openshift"],
    "docker": ["docker", "containers", "containerd", "podman"],
    "terraform": ["terraform", "opentofu"],
    "ansible": ["ansible"],
    "helm": ["helm"],
    "argocd": ["argo cd", "argocd", "gitops", "flux"],
    "aws": ["aws", "amazon web services", "ec2", "s3", "iam", "eks", "lambda", "cloudformation"],
    "azure": ["azure", "aks", "azure devops"],
    "gcp": ["gcp", "google cloud", "gke"],
    "linux": ["linux", "ubuntu", "debian", "rhel", "centos", "bash", "shell scripting"],
    "ci/cd": ["ci/cd", "cicd", "jenkins", "gitlab ci", "github actions", "pipelines", "circleci", "tekton"],
    "python": ["python"],
    "go": ["golang", " go "],
    "monitoring": ["prometheus", "grafana", "datadog", "observability", "monitoring", "opentelemetry", "elk", "loki", "alerting"],
    "networking": ["networking", "tcp/ip", "dns", "load balancing", "vpc", "firewall", "vpn"],
    "security": ["security", "iam", "vault", "secrets management", "cis benchmark"],
    "sql": ["postgresql", "postgres", "mysql", "sql"],
    "git": ["git"],
    "windows server": ["windows server", "active directory"],
    "vmware": ["vmware", "vsphere", "proxmox", "hyper-v"],
    "on-call": ["on-call", "on call", "incident response", "pagerduty"],
    "cloud": ["cloud"],
}

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
                facts.append(
                    Fact(
                        id=nid("r"),
                        category="role",
                        text=f"{title_line.strip()} ({start} to {end})",
                        confidence="explicit",
                        experience_kind=kind,  # type: ignore[arg-type]
                        start=start,
                        end=end,
                        evidence=[EvidenceRef(type="document", ref=f"document:{cv['id']}", quote=_quote(line))],
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

    # Skills: look at the whole CV, keep the line as evidence.
    low_lines = [(ln, ln.lower()) for ln in cv_text.splitlines() if ln.strip()]
    for skill, needles in SKILL_VOCAB.items():
        for ln, low in low_lines:
            padded = f" {low} "
            if any(n in padded for n in needles):
                facts.append(
                    Fact(
                        id=nid("s"),
                        category="skill",
                        text=skill,
                        confidence="explicit",
                        evidence=[EvidenceRef(type="document", ref=f"document:{cv['id']}", quote=_quote(ln))],
                        tags=["skill"],
                    )
                )
                break

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

    months = merged_months(role_intervals)
    years = round(months / 12.0, 1)
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

    level = "entry" if years < 1 else "junior" if years < 2.5 else "mid" if years < 5 else "senior"
    summary = _summary_text(facts, years)
    return ProfileDraft(
        summary_text=summary,
        facts=facts,
        targets=ProfileTargets(direction=[], level=level, stretch_ok=True),  # type: ignore[arg-type]
        questions=questions,
        professional_years_relevant=years,
    )


def _summary_text(facts: list[Fact], years: float) -> str:
    roles = [f for f in facts if f.category == "role" and f.experience_kind == "professional"]
    skills = [f.text for f in facts if f.category == "skill"]
    certs = [f.text for f in facts if f.category == "certification"]
    projects = [f for f in facts if f.category == "project"]
    parts = []
    if roles:
        parts.append(f"About {years:g} years of paid work across {len(roles)} role(s).")
    else:
        parts.append("No paid work experience with dates found yet.")
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
    # Recompute years from professional roles only, with overlap merged.
    intervals = [(f.start, f.end) for f in cleaned if f.category == "role" and f.experience_kind == "professional" and f.start and f.end]
    draft.professional_years_relevant = round(merged_months(intervals) / 12.0, 1)
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
