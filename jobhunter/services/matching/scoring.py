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


def job_tools(description: str, title: str) -> set[str]:
    text = f" {title.lower()} {description.lower()} "
    found = set()
    for skill, needles in SKILL_VOCAB.items():
        if any(n in text for n in needles):
            found.add(skill)
    for m in DESC_INFRA.findall(description.lower()):
        found.add(m.lower())
    return found


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
            res.reasons.append({"text": "This is the kind of role you said you want.", "fact_ids": []})
        else:
            res.reasons.append({"text": "The role is infrastructure, platform, cloud, SRE, or DevOps work.", "fact_ids": []})
    elif relevance == "uncertain":
        rel = 12.0
        res.reasons.append({"text": "The title is broader, but the work described is infrastructure work.", "fact_ids": []})
    else:
        return _exclude(res, "This is not an infrastructure, platform, cloud, SRE, or DevOps role.")
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
        res.reasons.append({"text": "You have evidence for " + ", ".join(sorted(have)[:5]) + (" and more" if len(have) > 5 else "") + ".", "fact_ids": ids})
    if missing:
        res.gaps.append("Tools named in the job you have no evidence for yet: " + ", ".join(sorted(missing)[:6]) + ".")

    # 3. Level fit --------------------------------------------------------------
    seniority, years_required = seniority_of(title, desc)
    job_idx = JOB_LEVEL_INDEX.get(seniority, 3)
    diff = job_idx - cand_idx
    level_score = 0.0
    if seniority == "intern":
        if cand_level in {"entry", "unknown"} and not professional_roles:
            level_score = 8.0
        else:
            return _exclude(res, "This is an internship; it is below the level you are aiming for.")
    elif diff == 0:
        level_score = 15.0
        res.reasons.append({"text": "The level matches your experience.", "fact_ids": [f.id for f in professional_roles[:2]]})
    elif diff == 1:
        if professional_roles or cand_level == "entry":
            level_score = 8.0 if professional_roles else 4.0
            res.reasons.append({"text": "A step up from your current level; worth a try with your experience." if professional_roles else "A step up; your projects can support the application.", "fact_ids": [f.id for f in professional_roles[:2]]})
            res.gaps.append("The role is one level above your experience; expect questions about ownership and scale.")
        else:
            level_score = 2.0
    elif diff >= 2:
        if not professional_roles and seniority in {"senior", "staff"}:
            return _exclude(res, "This role asks for senior experience, and your profile has no paid experience yet. It is not a fair shot right now.")
        if not professional_roles:
            # Entry-level candidate, plain mid-level title: a stretch, shown honestly.
            level_score = 2.0 if (years_required is None or years_required <= 2) else -8.0
            res.gaps.append("The job does not say junior; expect questions about hands-on experience, and lean on your project evidence.")
        else:
            level_score = -12.0
            res.gaps.append("This role is two or more levels above your experience.")
    elif diff == -1:
        level_score = 6.0
        res.gaps.append("This is a step below your current level; it could still be a good employer.")
    else:
        level_score = -6.0
        res.gaps.append("This role is well below your level.")
    if years_required is not None:
        if years_required > years + 3:
            level_score -= 8.0
            res.gaps.append(f"The job asks for about {years_required} years; your profile shows about {years:g}. That is not automatic rejection, but be ready to explain.")
        elif years_required > years + 1:
            level_score -= 3.0
            res.gaps.append(f"The job mentions {years_required} years of experience; you have about {years:g}.")
    res.components["level"] = level_score

    # 4. Eligibility --------------------------------------------------------------
    elig = {"countries": "unknown", "sponsorship": job.get("sponsorship") or "unknown", "remote_restriction": job.get("remote_restriction")}
    elig_score = 0.0
    work = set(prefs.work_countries or [])
    remote_type = job.get("remote_type") or "unknown"
    if codes and work:
        if set(codes) & work:
            elig["countries"] = "yes"
        else:
            elig["countries"] = "no"
            if remote_type == "remote":
                return _exclude(res, f"{job.get('remote_restriction') or 'Remote, but only in ' + country_names(codes)}. You said you can work in {country_names(sorted(work))}.", elig)
            if elig["sponsorship"] == "no":
                return _exclude(res, "The job is in a country where you cannot work yet, and the employer says it does not sponsor visas.", elig)
            if prefs.relocation == "no":
                return _exclude(res, "The job is in a country where you cannot work yet, and you said you do not want to move.", elig)
            elig_score -= 8.0
            res.gaps.append("Work location: the job is in a country where you may need a visa. Sponsorship: " + ("offered" if elig["sponsorship"] == "yes" else "not stated") + ".")
    elif codes and not work:
        res.gaps.append("I don't know yet where you can work, so I can't check this job's location rule.")
        elig_score -= 3.0
    elif remote_type == "remote" and not codes:
        res.gaps.append("Remote, but the allowed countries are not stated. Check before applying.")
        elig_score -= 2.0
    if prefs.needs_sponsorship == "yes" and elig["sponsorship"] == "no" and elig["countries"] != "yes":
        return _exclude(res, "You need visa sponsorship and this employer says it does not sponsor.", elig)
    if elig["sponsorship"] == "unknown" and prefs.needs_sponsorship == "yes" and elig["countries"] != "yes":
        res.gaps.append("Visa sponsorship: not stated. Ask before investing time.")
    res.components["eligibility"] = elig_score

    # 5. Preferences ----------------------------------------------------------------
    pref_score = 0.0
    if prefs.remote == "required" and remote_type == "onsite":
        return _exclude(res, "This job is on-site and you said remote is required.", elig)
    if prefs.remote in {"required", "preferred"}:
        if remote_type == "remote":
            pref_score += 5.0
            res.reasons.append({"text": "Remote, which you prefer.", "fact_ids": []})
        elif remote_type == "hybrid":
            pref_score += 1.0
        elif remote_type == "onsite":
            pref_score -= 6.0
            res.gaps.append("On-site, while you prefer remote.")
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
        res.gaps.append("Posted more than 14 days ago; it may already be filled.")

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
        return _exclude(res, "The fit is weak: " + (res.gaps[0] if res.gaps else "few of the job's needs match your evidence."), elig)
    res.next_step = _next_step(res, job)
    res.reasons = res.reasons[:3]
    return res


def _exclude(res: ScoreResult, reason: str, elig: dict | None = None) -> ScoreResult:
    res.group = "excluded"
    res.exclusion_reason = reason
    res.eligibility = elig or res.eligibility
    res.next_step = "Nothing to do unless you disagree; you can still open it."
    return res


def _salary_check(job: dict, prefs: Preferences) -> dict:
    if not prefs.salary_min:
        if job.get("salary_min"):
            return {"status": "listed", "text": ""}
        return {"status": "listed", "text": ""}
    jmin, jmax, jcur, jper = job.get("salary_min"), job.get("salary_max"), job.get("salary_currency"), job.get("salary_period")
    if not jmin and not jmax:
        return {"status": "unknown", "text": "Salary not listed; your minimum could not be checked."}
    if not jcur or jcur != prefs.salary_currency or (jper or "year") != (prefs.salary_period or "year"):
        return {"status": "incomparable", "text": f"Salary is listed in {jcur or 'an unknown currency'} per {jper or 'unknown period'}; I did not convert it to compare with your minimum."}
    hi = jmax or jmin
    if hi is not None and hi < prefs.salary_min:
        return {"status": "below", "text": f"The listed range tops out at {hi:,.0f} {jcur}, below your minimum of {prefs.salary_min:,.0f} {prefs.salary_currency}."}
    return {"status": "overlap", "text": "The listed salary range reaches your minimum."}


def _next_step(res: ScoreResult, job: dict) -> str:
    if res.eligibility.get("countries") == "no" or any("visa" in g.lower() for g in res.gaps):
        return "Check the work-location and visa rule first, then decide."
    if any("Salary not listed" in g for g in res.gaps):
        return "Open the job and look for the salary; then ask me to help you apply."
    if res.group == "good":
        return "Ask me to help you apply."
    if res.group == "worth_a_shot":
        return "Read the full description and decide if the stretch is right for you."
    return "Save it as a backup and focus on the better matches first."


def region_of(codes: list[str]) -> list[str]:
    out = []
    if set(codes) & NORTH_AMERICA:
        out.append("na")
    if set(codes) & (EUROPE | EU_EEA):
        out.append("eu")
    return out
