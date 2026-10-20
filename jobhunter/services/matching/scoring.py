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
from jobhunter.services.matching.skills import TOOL_LABELS, canonical_skill_keys, coverage
from jobhunter.services.matching.skills import job_tools as _job_tools
from jobhunter.services.matching.skills import tool_label as _tool_label
from jobhunter.services.normalize import MANAGEMENT, country_names, primary_role, seniority_of
from jobhunter.services.preferences import EU_EEA, EUROPE, NORTH_AMERICA
from jobhunter.services.profile import RELEVANT_DOMAINS, role_domain

LEADERSHIP = re.compile(r"\b(lead|led|leading|head of|manager|managed|team of \d+|mentor(ed|ing)?|line manage)\b", re.I)

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


MIN_DESCRIPTION_CHARS = 120
__all__ = ["TOOL_LABELS", "job_tools", "tool_label", "candidate_skills", "score_job", "ScoreResult"]


def job_tools(description: str, title: str) -> set[str]:
    """Tools the job names, as canonical skill keys (the same keys the CV skills use)."""
    return _job_tools(description, title)


def tool_label(key: str) -> str:
    return _tool_label(key)


def candidate_skills(facts: list[Fact]) -> dict[str, Fact]:
    """Canonical skill key -> the fact that proves it. A phrase-valued fact ("Kubernetes (AKS, k3s)")
    yields every key it names, so parser-made and Claude-made facts behave the same."""
    out: dict[str, Fact] = {}
    for f in facts:
        if f.category == "skill" and f.status != "rejected":
            for key in canonical_skill_keys(f.text):
                out.setdefault(key, f)
    return out


DIRECTION_WORDS = {"devops": ["devops", "dev ops"], "platform": ["platform"], "sre": ["sre", "reliability"], "cloud": ["cloud"], "infrastructure": ["infrastructure", "infra"], "systems": ["systems", "linux", "sysadmin"]}
DIRECTION_LABELS = {"devops": "DevOps", "platform": "platform engineering", "sre": "SRE", "cloud": "cloud", "infrastructure": "infrastructure", "systems": "systems"}


def _direction_match(title: str, targets: dict) -> str | None:
    """Which chosen direction the role itself names. Only the role before a dash, comma or bracket counts,
    so "Full-Stack Engineer - Platform Engineering" does not pass as a platform job."""
    from jobhunter.services.normalize import primary_role

    t = primary_role(title).lower()
    for d in targets.get("direction") or []:
        if any(k in t for k in DIRECTION_WORDS.get(d, [d])):
            return d
    return None


def score_job(job: dict, facts: list[Fact], targets: dict, prefs: Preferences, feedback_flags: dict | None = None, cv_skills: set[str] | None = None, rejected_skills: set[str] | None = None) -> ScoreResult:
    """`cv_skills`: tools named anywhere in the CV text (not only in approved facts). They tell missing
    evidence ("it is in your CV, not in your profile yet") from missing experience ("it is not in your CV")."""
    feedback_flags = feedback_flags or {}
    cv_skills = cv_skills or set()
    rejected_skills = rejected_skills or set()
    res = ScoreResult(score=0.0, group="excluded")
    desc = job.get("description_text") or ""
    title = job.get("title") or ""
    codes = json.loads(job.get("country_codes_json") or "[]") if isinstance(job.get("country_codes_json"), str) else (job.get("country_codes") or [])
    skills = candidate_skills(facts)
    all_roles = [f for f in facts if f.category == "role" and f.experience_kind == "professional" and f.status != "rejected"]
    # Only paid roles that were infrastructure work count as experience for these jobs.
    professional_roles = [f for f in all_roles if role_domain(f) in RELEVANT_DOMAINS]
    years = float(targets.get("professional_years_relevant") or 0.0)
    adjacent_years = float(targets.get("professional_years_adjacent") or 0.0)
    cand_level = targets.get("level") or "unknown"
    cand_idx = CANDIDATE_LEVEL_INDEX.get(cand_level, 2)
    thin_text = len(desc.strip()) < MIN_DESCRIPTION_CHARS
    location_confidence = job.get("location_confidence") or "unknown"

    # 1. Role relevance -----------------------------------------------------
    relevance = job.get("relevance") or "unknown"
    if relevance == "uncertain" and MANAGEMENT.search(primary_role(title)):
        # A leadership role is a credible stretch only with leadership evidence in paid work.
        if not (cand_level in {"senior", "staff"} and any(LEADERSHIP.search(f.text) for f in professional_roles)):
            return _exclude(res, "A leadership role. Your approved profile has no paid experience leading a team yet.")
    direction = _direction_match(title, targets) if relevance == "relevant" else None
    if relevance == "relevant":
        rel = 25.0
        if direction:
            rel += 5.0
            res.reasons.append({"text": f"A {DIRECTION_LABELS.get(direction, direction)} role: the direction you chose.", "fact_ids": []})
        else:
            res.reasons.append({"text": "This is infrastructure work, the kind you are looking for.", "fact_ids": []})
    elif relevance == "uncertain":
        rel = 12.0
        res.reasons.append({"text": job.get("relevance_reason") or "The title is a bit different, but the work described is infrastructure work.", "fact_ids": []})
    else:
        return _exclude(res, "This is not an infrastructure, platform, cloud, SRE, or DevOps job.")
    res.components["relevance"] = rel

    # 2. Skills overlap -------------------------------------------------------
    tools = job_tools(desc, title)
    have, related, missing = coverage(tools, set(skills))
    if tools:
        cov = (len(have) + 0.5 * len(related)) / len(tools)
        skill_score = 10.0 + 15.0 * cov
    else:
        cov = 0.0
        skill_score = 12.0 if not thin_text else 6.0
    res.components["skills"] = skill_score
    if have:
        ids = [skills[t].id for t in have[:3]]
        asked = sorted(tools)
        # The concrete evidence leads: the requirement, then what the CV proves, before the direction sentence.
        if len(have) == len(asked):
            text = "They ask for " + _join(asked) + ". Your CV shows " + ("all of them" if len(asked) > 1 else "it") + "."
        else:
            text = f"They ask for {_join(asked)}. Your CV shows {_join(have)} ({len(have)} of {len(asked)})."
        res.reasons.insert(0, {"text": text, "fact_ids": ids})
    for want, got in related[:2]:
        res.gaps.append(f"They ask for {tool_label(want)}. Your CV shows {tool_label(got)}, which is related but not the same.")
    if missing:
        # Missing evidence (in the CV, not yet in the approved profile) is a different problem from missing experience.
        said_no = [t for t in missing if t in rejected_skills]
        in_cv = [t for t in missing if t in cv_skills and t not in rejected_skills]
        not_in_cv = [t for t in missing if t not in cv_skills and t not in rejected_skills]
        if said_no:
            res.gaps.append("They ask for " + _join(said_no[:4]) + ". You told Hunter you do not have " + ("it" if len(said_no) == 1 else "these") + ".")
        if in_cv:
            res.gaps.append("Your CV mentions " + _join(in_cv[:4]) + ", but " + ("it is" if len(in_cv) == 1 else "they are") + " not in your approved profile yet. Add " + ("it" if len(in_cv) == 1 else "them") + " under Profile so Hunter can count " + ("it" if len(in_cv) == 1 else "them") + ".")
        if not_in_cv:
            res.gaps.append("They also ask for " + _join(not_in_cv[:5]) + ". " + ("It is" if len(not_in_cv) == 1 else "These are") + " not established in your approved profile.")
    if thin_text:
        res.gaps.append("The posting has almost no text, so this is a guess from the title. Open it to check.")

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
            why = "your paid work so far was not infrastructure work" if all_roles else "your CV has no paid experience yet"
            return _exclude(res, f"They want senior experience and {why}. Not a fair shot right now.")
        if not professional_roles:
            level_score = 2.0 if (years_required is None or years_required <= 2) else -8.0
            if adjacent_years >= 2:
                level_score += 2.0
                res.gaps.append("The job does not say junior. Your IT experience and project work are your case here.")
            else:
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
    elig = {"countries": "unknown", "sponsorship": job.get("sponsorship") or "unknown", "remote_restriction": job.get("remote_restriction"), "location_confidence": location_confidence}
    elig_score = 0.0
    work = set(prefs.work_countries or [])
    remote_type = job.get("remote_type") or "unknown"
    needs_check: str | None = None
    if codes and work:
        if set(codes) & work:
            elig["countries"] = "yes" if location_confidence != "inferred" else "probably"
            if location_confidence == "inferred":
                needs_check = job.get("remote_restriction") or "The location was read from the text, not stated. Check before you apply."
        elif location_confidence == "conflict":
            elig["countries"] = "unclear"
            needs_check = job.get("remote_restriction") or "The location and the text disagree. Check the posting."
        elif location_confidence == "inferred":
            elig["countries"] = "unclear"
            needs_check = (job.get("remote_restriction") or f"The text points at {country_names(codes)}") + " You said you can work in " + country_names(sorted(work)) + "."
        else:
            elig["countries"] = "no"
            where = job.get("remote_restriction") if remote_type == "remote" else f"This job is in {country_names(codes)}"
            return _exclude(res, f"{where}. You said you can work in {country_names(sorted(work))}.", elig)
    elif location_confidence == "worldwide":
        elig["countries"] = "yes"
        res.reasons.append({"text": "Remote worldwide, so your country is fine.", "fact_ids": []})
    elif work and remote_type == "remote" and not codes:
        elig["countries"] = "unclear"
        needs_check = "Remote, but they don't say which countries. Check before you apply."
    elif work and not codes:
        elig["countries"] = "unclear"
        needs_check = "No location given. Check before you apply."
    if prefs.needs_sponsorship == "yes" and elig["sponsorship"] == "no" and elig["countries"] not in {"yes", "probably"}:
        return _exclude(res, "You need visa sponsorship and this employer says it does not sponsor.", elig)
    res.components["eligibility"] = elig_score

    # 5. Preferences ----------------------------------------------------------------
    pref_score = 0.0
    modes = set(prefs.work_modes or ["remote", "hybrid", "onsite"])
    if remote_type in {"remote", "hybrid", "onsite"} and remote_type not in modes:
        words = {"onsite": "fully in the office", "hybrid": "hybrid (some days in the office)", "remote": "fully remote"}[remote_type]
        return _exclude(res, f"This job is {words}, and you said that does not work for you.", elig)
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
    # Confidence caps: a Good match needs real text and real evidence (a tool the CV proves and the job asks
    # for), never a title alone. Without that overlap it is at most worth a shot.
    if res.group == "good" and (thin_text or not have):
        res.group = "worth_a_shot"
    if res.group == "worth_a_shot" and thin_text and not have and not professional_roles:
        res.group = "backup"
    # Unresolved location: shown under "Location needs checking", never as a main recommendation.
    if needs_check:
        res.group = "uncertain"
        res.exclusion_reason = needs_check
        res.gaps.insert(0, needs_check)
        res.next_step = "Open the job and check where they allow people to be. If it fits, press Help me apply."
        res.reasons = res.reasons[:3]
        return res
    res.next_step = _next_step(res, job)
    res.reasons = res.reasons[:3]
    return res


def _join(keys: list[str], limit: int = 5) -> str:
    labels = [tool_label(k) for k in keys[:limit]]
    more = len(keys) - len(labels)
    if more > 0:
        labels.append(f"{more} more")
    if len(labels) <= 1:
        return labels[0] if labels else ""
    return ", ".join(labels[:-1]) + " and " + labels[-1]


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
