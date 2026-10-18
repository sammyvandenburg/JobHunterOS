"""Deterministic fit scoring. Transparent, explainable, never a hiring probability.

Policy: aim high, accept a little lower. One level up is a stretch worth
showing when there is real scope evidence; two levels up for someone without
professional experience is explained and excluded. Missing tools are gaps,
not rejections. Unknown eligibility or salary is a question, not a rejection.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from jobhunter.models import Fact, Preferences
from jobhunter.services import freshness
from jobhunter.services.normalize import DESC_INFRA, country_names, seniority_of
from jobhunter.services.preferences import EU_EEA, EUROPE, NORTH_AMERICA
from jobhunter.services.profile import SKILL_VOCAB

LEVELS = ["intern", "entry", "junior", "mid", "senior", "staff"]
CANDIDATE_LEVEL_INDEX = {"entry": 1, "junior": 2, "mid": 3, "senior": 4, "staff": 5, "unknown": 2}
JOB_LEVEL_INDEX = {"intern": 0, "junior": 2, "mid": 3, "senior": 4, "staff": 5}
CONSULTANCY = re.compile(r"\b(consultan(cy|t|ts|ting)|our clients|client projects|staffing|outsourc|managed services provider|msp\b|detachering|secondment)\b", re.I)
THRESHOLDS = {"good": 60.0, "worth_a_shot": 42.0, "backup": 28.0}


@dataclass
class ScoreResult:
    score: float
    group: str
    reasons: list[dict] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    eligibility: dict = field(default_factory=dict)
    exclusion_reason: str | None = None
    next_step: str = "Open the job and read the full description."
    components: dict = field(default_factory=dict)


_NEEDLE_TO_KEY = {n.strip(): k for k, needles in SKILL_VOCAB.items() for n in needles}
TOOL_LABELS = {"monitoring": "monitoring (Prometheus, Grafana)", "argocd": "Argo CD / GitOps", "ci/cd": "CI/CD pipelines", "on-call": "on-call", "aws": "AWS", "gcp": "Google Cloud", "azure": "Azure", "sql": "SQL databases"}


def job_tools(description: str, title: str) -> set[str]:
    """Tools the job names, as canonical skill keys (the same keys the CV skills use)."""
    text = f" {title.lower()} {description.lower()} "
    found = set()
    for skill, needles in SKILL_VOCAB.items():
        if skill == "cloud":
            continue  # too generic to count as a tool
        if any(n in text for n in needles):
            found.add(skill)
    for m in DESC_INFRA.findall(description.lower()):
        key = _NEEDLE_TO_KEY.get(m.lower().strip())
        if key:
            if key != "cloud":
                found.add(key)
        elif m.lower() not in {"sre", "iac", "infrastructure as code", "load balanc", "vpc", "eks", "gke", "aks"}:
            found.add(m.lower())
    return found


def tool_label(key: str) -> str:
    return TOOL_LABELS.get(key, key)


def candidate_skills(facts: list[Fact]) -> dict[str, Fact]:
    out = {}
    for f in facts:
        if f.category == "skill" and f.status != "rejected":
            out[f.text.lower()] = f
    return out


def _direction_match(title: str, targets: dict) -> bool:
    dirs = targets.get("direction") or []
    t = title.lower()
    keys = {"devops": ["devops", "dev ops"], "platform": ["platform"], "sre": ["sre", "reliability"], "cloud": ["cloud"], "infrastructure": ["infrastructure", "infra"], "systems": ["systems", "linux", "sysadmin"]}
    return any(any(k in t for k in keys.get(d, [d])) for d in dirs)


def score_job(job: dict, facts: list[Fact], targets: dict, prefs: Preferences, feedback_flags: dict | None = None) -> ScoreResult:
    feedback_flags = feedback_flags or {}
    res = ScoreResult(score=0.0, group="excluded")
    desc = job.get("description_text") or ""
    title = job.get("title") or ""
    codes = json.loads(job.get("country_codes_json") or "[]") if isinstance(job.get("country_codes_json"), str) else (job.get("country_codes") or [])
    skills = candidate_skills(facts)
    professional_roles = [f for f in facts if f.category == "role" and f.experience_kind == "professional" and f.status != "rejected"]
    years = float(targets.get("professional_years_relevant") or 0.0)
    cand_level = targets.get("level") or "unknown"
    cand_idx = CANDIDATE_LEVEL_INDEX.get(cand_level, 2)

    # 1. Role relevance -----------------------------------------------------
    relevance = job.get("relevance") or "unknown"
    if relevance == "relevant":
        rel = 25.0
        if _direction_match(title, targets):
            rel += 5.0
            res.reasons.append({"text": "This is the kind of job you said you want.", "fact_ids": []})
        else:
            res.reasons.append({"text": "This is infrastructure work, the kind you are looking for.", "fact_ids": []})
    elif relevance == "uncertain":
        rel = 12.0
        res.reasons.append({"text": "The title is a bit different, but the work is infrastructure work.", "fact_ids": []})
    else:
        return _exclude(res, "This is not an infrastructure, platform, cloud, SRE, or DevOps job.")
    res.components["relevance"] = rel

    # 2. Skills overlap -------------------------------------------------------
    tools = job_tools(desc, title)
    have = [t for t in tools if t in skills]
    missing = [t for t in tools if t not in skills]
    if tools:
        coverage = len(have) / len(tools)
        skill_score = 10.0 + 15.0 * coverage
    else:
        coverage = 0.0
        skill_score = 12.0
    res.components["skills"] = skill_score
    if have:
        ids = [skills[t].id for t in have[:3]]
        res.reasons.append({"text": "Your CV shows " + ", ".join(tool_label(t) for t in sorted(have)[:5]) + (" and more" if len(have) > 5 else "") + ", which they ask for.", "fact_ids": ids})
    if missing:
        res.gaps.append("They mention " + ", ".join(tool_label(t) for t in sorted(missing)[:5]) + ". Your CV does not show " + ("it" if len(missing) == 1 else "these") + " yet.")

    # 3. Level fit --------------------------------------------------------------
    seniority, years_required = seniority_of(title, desc)
    job_idx = JOB_LEVEL_INDEX.get(seniority, 3)
    diff = job_idx - cand_idx
    level_score = 0.0
    if seniority == "intern":
        if cand_level in {"entry", "unknown"} and not professional_roles:
            level_score = 8.0
        else:
            return _exclude(res, "This is an internship, below what you are aiming for.")
    elif diff == 0:
        level_score = 15.0
        res.reasons.append({"text": "The level fits your experience.", "fact_ids": [f.id for f in professional_roles[:2]]})
    elif diff == 1:
        if professional_roles or cand_level == "entry":
            level_score = 8.0 if professional_roles else 4.0
            res.reasons.append({"text": "A step up from where you are now. Worth a try." if professional_roles else "A step up. Your projects can back you up.", "fact_ids": [f.id for f in professional_roles[:2]]})
            res.gaps.append("One level above your experience. Expect questions about what you owned and how big it was.")
        else:
            level_score = 2.0
    elif diff >= 2:
        if not professional_roles and seniority in {"senior", "staff"}:
            return _exclude(res, "They want senior experience and your CV has no paid experience yet. Not a fair shot right now.")
        if not professional_roles:
            level_score = 2.0 if (years_required is None or years_required <= 2) else -8.0
            res.gaps.append("The job does not say junior. Lean on your project work when you apply.")
        else:
            level_score = -12.0
            res.gaps.append("Two or more levels above your experience.")
    elif diff == -1:
        level_score = 6.0
        res.gaps.append("A step below your current level. Could still be a good employer.")
    else:
        level_score = -6.0
        res.gaps.append("Well below your level.")
    if years_required is not None:
        if years_required > years + 3:
            level_score -= 8.0
            res.gaps.append(f"They ask for about {years_required} years. Your CV shows about {years:g}. Not a no, but be ready to explain.")
        elif years_required > years + 1:
            level_score -= 3.0
            res.gaps.append(f"They mention {years_required} years. You have about {years:g}.")
    res.components["level"] = level_score

    # 4. Location (your countries decide) ---------------------------------------------
    elig = {"countries": "unknown", "sponsorship": job.get("sponsorship") or "unknown", "remote_restriction": job.get("remote_restriction")}
    elig_score = 0.0
    work = set(prefs.work_countries or [])
    remote_type = job.get("remote_type") or "unknown"
    if codes and work:
        if set(codes) & work:
            elig["countries"] = "yes"
        else:
            elig["countries"] = "no"
            where = job.get("remote_restriction") if remote_type == "remote" else f"This job is in {country_names(codes)}"
            return _exclude(res, f"{where}. You said you can work in {country_names(sorted(work))}.", elig)
    elif remote_type == "remote" and not codes:
        res.gaps.append("Remote, but they don't say which countries. Check before you apply.")
        elig_score -= 2.0
    elif not codes:
        res.gaps.append("No location given. Check before you apply.")
        elig_score -= 2.0
    if prefs.needs_sponsorship == "yes" and elig["sponsorship"] == "no" and elig["countries"] != "yes":
        return _exclude(res, "You need visa sponsorship and this employer says it does not sponsor.", elig)
    res.components["eligibility"] = elig_score

    # 5. Preferences ----------------------------------------------------------------
    pref_score = 0.0
    if prefs.remote == "required" and remote_type == "onsite":
        return _exclude(res, "This job is in the office and you said remote is a must.", elig)
    if prefs.remote in {"required", "preferred"}:
        if remote_type == "remote":
            pref_score += 5.0
            res.reasons.append({"text": "Remote, like you want.", "fact_ids": []})
        elif remote_type == "hybrid":
            pref_score += 1.0
        elif remote_type == "onsite":
            pref_score -= 6.0
            res.gaps.append("In the office, while you prefer remote.")
    sal_note = _salary_check(job, prefs)
    if sal_note["status"] == "below" and prefs.salary_strength == "must":
        return _exclude(res, sal_note["text"], elig)
    if sal_note["status"] == "below":
        pref_score -= 6.0
        res.gaps.append(sal_note["text"])
    elif sal_note["status"] == "overlap":
        pref_score += 2.0
        res.reasons.append({"text": sal_note["text"], "fact_ids": []})
    elif sal_note["status"] in {"unknown", "incomparable"}:
        res.gaps.append(sal_note["text"])
    if feedback_flags.get("fewer_consultancy") and (CONSULTANCY.search(job.get("employer_name") or "") or CONSULTANCY.search(desc[:3000])):
        pref_score -= 10.0 if feedback_flags["fewer_consultancy"] == "must" else 5.0
        res.gaps.append("Looks like consultancy work, which you asked to see less of.")
    res.components["preferences"] = pref_score

    # 6. Freshness (small) ------------------------------------------------------------
    b, _ = freshness.bucket(job)
    fresh = freshness.freshness_bonus(b)
    res.components["freshness"] = fresh
    if b == "old":
        res.gaps.append("Posted more than 14 days ago. It may be filled already.")

    total = rel + skill_score + level_score + elig_score + pref_score + fresh
    res.score = round(total, 1)
    res.eligibility = elig
    if total >= THRESHOLDS["good"] and relevance == "relevant":
        res.group = "good"
    elif total >= THRESHOLDS["worth_a_shot"]:
        res.group = "worth_a_shot"
    elif total >= THRESHOLDS["backup"] and relevance == "relevant":
        res.group = "backup"
    else:
        return _exclude(res, "Not a good fit: " + (res.gaps[0] if res.gaps else "little of what they ask for shows in your CV."), elig)
    res.next_step = _next_step(res, job)
    res.reasons = res.reasons[:3]
    return res


def _exclude(res: ScoreResult, reason: str, elig: dict | None = None) -> ScoreResult:
    res.group = "excluded"
    res.exclusion_reason = reason
    res.eligibility = elig or res.eligibility
    res.next_step = "Nothing to do, unless you disagree. You can still open it."
    return res


def _salary_check(job: dict, prefs: Preferences) -> dict:
    if not prefs.salary_min:
        if job.get("salary_min"):
            return {"status": "listed", "text": ""}
        return {"status": "listed", "text": ""}
    jmin, jmax, jcur, jper = job.get("salary_min"), job.get("salary_max"), job.get("salary_currency"), job.get("salary_period")
    if not jmin and not jmax:
        return {"status": "unknown", "text": "Salary not listed, so I could not check it against your minimum."}
    if not jcur or jcur != prefs.salary_currency or (jper or "year") != (prefs.salary_period or "year"):
        return {"status": "incomparable", "text": f"Salary is in {jcur or 'another currency'} per {jper or 'unknown period'}. I did not convert it, so compare it yourself."}
    hi = jmax or jmin
    if hi is not None and hi < prefs.salary_min:
        return {"status": "below", "text": f"The pay tops out at {hi:,.0f} {jcur}, below your minimum of {prefs.salary_min:,.0f} {prefs.salary_currency}."}
    return {"status": "overlap", "text": "The pay reaches your minimum."}


def _next_step(res: ScoreResult, job: dict) -> str:
    if any("which countries" in g for g in res.gaps):
        return "Open the job and check which countries they allow. Then press Help me apply."
    if any("Salary not listed" in g for g in res.gaps):
        return "Open the job to look for the pay. Then press Help me apply."
    if res.group == "good":
        return "Press Help me apply."
    if res.group == "worth_a_shot":
        return "Read the job. If it feels right, press Help me apply."
    return "Save it for later. Look at the better matches first."


def region_of(codes: list[str]) -> list[str]:
    out = []
    if set(codes) & NORTH_AMERICA:
        out.append("na")
    if set(codes) & (EUROPE | EU_EEA):
        out.append("eu")
    return out
