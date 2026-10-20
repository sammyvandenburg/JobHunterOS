"""Matching engine: scores jobs against the approved profile, writes matches,
records recommendations, and produces explanations (rules or Claude).

Explanations from Claude are optional and validated. If the model is not
enabled, fails, or returns something unsupported, the rule-based explanation
stays and is labelled as such. Discovery results are never lost.
"""

from __future__ import annotations

import json

from jobhunter.config import POLICY_VERSION, AppContext
from jobhunter.db import Database
from jobhunter.models import ExplanationBatch, Fact, MatchExplanation
from jobhunter.services import consent, feedback
from jobhunter.services import preferences as prefs_svc
from jobhunter.services import profile as profile_svc
from jobhunter.services.matching.scoring import score_job
from jobhunter.util import now_iso, sha256_text, stable_json

MAX_MODEL_EXPLANATIONS_PER_RUN = 12
RANK_LIMIT_FOR_MODEL = 20


JOB_MATERIAL_FIELDS = ("description_hash", "title", "employer_name", "location_text", "salary_text", "salary_min", "salary_max", "salary_currency", "salary_period", "country_codes_json", "remote_type", "remote_restriction", "location_confidence", "relevance", "sponsorship", "posted_at", "availability")


def profile_fingerprint(profile: dict) -> str:
    """The content of the profile, not its row id: a correction or a new revision changes it, a re-save does not."""
    facts = [f.model_dump(mode="json") for f in profile["facts"]]
    return sha256_text(stable_json({"facts": facts, "targets": profile.get("targets")}))


def cache_key(job: dict, profile_key: int | str, prefs_hash: str) -> str:
    return sha256_text(stable_json({"j": {k: job.get(k) for k in JOB_MATERIAL_FIELDS}, "p": profile_key, "v": POLICY_VERSION, "pr": prefs_hash}))


def _prefs_hash(db: Database) -> str:
    return sha256_text(stable_json(db.get_setting("preferences") or {}))


def run_matching(ctx: AppContext, db: Database, hunt_run_id: int | None = None, job_ids: list[int] | None = None, use_model: bool | None = None) -> dict:
    profile = profile_svc.approved(db)
    if not profile:
        return {"status": "skipped", "reason": "No approved profile yet; jobs were saved but not matched."}
    prefs = prefs_svc.get(db)
    phash = _prefs_hash(db)
    flags = feedback.active_flags(db)
    facts: list[Fact] = profile["facts"]
    targets = profile["targets"]
    reclassified = _reclassify_relevance_if_rules_changed(db)
    cv_skills = _cv_skills(db)
    from jobhunter.services.matching.skills import canonical_skill_keys

    rejected_skills = {k for f in facts if f.category == "skill" and f.status == "rejected" for k in canonical_skill_keys(f.text)}
    if job_ids:
        marks = ",".join("?" for _ in job_ids)
        rows = db.fetchall(f"SELECT * FROM jobs WHERE id IN ({marks})", tuple(job_ids))
    else:
        rows = db.fetchall("SELECT * FROM jobs WHERE relevance IN ('relevant','uncertain') AND availability != 'closed'")
    pkey = profile_fingerprint(profile)
    scored = computed = reused = 0
    presented: list[tuple[int, int, str]] = []
    with db.tx():
        # Jobs reclassified as not infrastructure work keep a match row only as "excluded", so they leave every list.
        db.execute(
            "UPDATE matches SET match_group = 'excluded', exclusion_reason = COALESCE((SELECT relevance_reason FROM jobs WHERE jobs.id = matches.job_id), 'Not infrastructure work.'), computed_at = ? "
            "WHERE profile_version_id = ? AND policy_version = ? AND match_group != 'excluded' AND job_id IN (SELECT id FROM jobs WHERE relevance = 'irrelevant')",
            (now_iso(), profile["id"], POLICY_VERSION),
        )
        for job in rows:
            ck = cache_key(job, pkey, phash)
            existing = db.fetchone("SELECT * FROM matches WHERE job_id = ? AND profile_version_id = ? AND policy_version = ?", (job["id"], profile["id"], POLICY_VERSION))
            if existing and existing["cache_key"] == ck:
                reused += 1
                continue
            result = score_job(job, facts, targets, prefs, flags, cv_skills=cv_skills, rejected_skills=rejected_skills)
            expl = rules_explanation(job, result)
            values = {
                "job_id": job["id"],
                "profile_version_id": profile["id"],
                "policy_version": POLICY_VERSION,
                "cache_key": ck,
                "score": result.score,
                "match_group": result.group,
                "reasons_json": json.dumps(result.reasons),
                "gaps_json": json.dumps(result.gaps),
                "eligibility_json": json.dumps(result.eligibility),
                "exclusion_reason": result.exclusion_reason,
                "explanation_text": expl.summary,
                "explanation_source": "rules",
                "explanation_json": expl.model_dump_json(),
                "next_step": result.next_step,
                "computed_at": now_iso(),
            }
            if existing:
                db.update("matches", values, "id = ?", (existing["id"],))
                match_id = existing["id"]
            else:
                match_id = db.insert("matches", values)
            computed += 1
            scored += 1
            if result.group in {"good", "worth_a_shot", "backup"}:
                presented.append((job["id"], match_id, result.group))
        # Recommendation history: a job counts as "recommended" once per presentation channel.
        for job_id, match_id, group in presented:
            already = db.fetchone("SELECT id FROM recommendations WHERE job_id = ? AND channel = 'dashboard'", (job_id,))
            if not already:
                db.insert("recommendations", {"job_id": job_id, "match_id": match_id, "hunt_run_id": hunt_run_id, "channel": "dashboard", "match_group": group, "presented_at": now_iso()})
    model_result = maybe_model_explanations(ctx, db, use_model=use_model)
    return {"status": "ok", "profile_version": profile["version"], "scored": scored, "computed": computed, "reused": reused, "presented": len(presented), "reclassified": reclassified, "model": model_result}


def _cv_skills(db: Database) -> set[str]:
    """Tools named anywhere in the latest CV text. Used only to word gaps (missing evidence versus missing
    experience); scoring itself counts approved facts, never raw text."""
    from jobhunter.services import documents as docs
    from jobhunter.services.matching.skills import extract_skills

    row = db.fetchone("SELECT id FROM source_documents WHERE kind = 'cv' ORDER BY version DESC, id DESC LIMIT 1")
    if not row:
        return set()
    try:
        return extract_skills(docs.document_text(db, row["id"]))
    except Exception:
        return set()


RELEVANCE_RULES_SETTING = "matching.relevance_rules"


def _reclassify_relevance_if_rules_changed(db: Database) -> int:
    """Title rules live in code. After an update, jobs already in the database keep their old
    classification until they are seen again, so re-run the rules once per policy version."""
    if db.get_setting(RELEVANCE_RULES_SETTING) == POLICY_VERSION:
        return 0
    from jobhunter.services.normalize import classify_relevance

    changed = 0
    with db.tx():
        for j in db.fetchall("SELECT id, title, description_text, relevance FROM jobs WHERE availability != 'closed'"):
            rel, reason = classify_relevance(j.get("title") or "", j.get("description_text") or "")
            if rel != j.get("relevance"):
                db.update("jobs", {"relevance": rel, "relevance_reason": reason, "updated_at": now_iso()}, "id = ?", (j["id"],))
                changed += 1
        db.set_setting(RELEVANCE_RULES_SETTING, POLICY_VERSION)
    return changed


def rules_explanation(job: dict, result) -> MatchExplanation:
    group_word = {"good": "Good match", "worth_a_shot": "Worth a shot", "backup": "Backup option", "excluded": "Not recommended", "uncertain": "Needs checking"}[result.group]
    if result.group == "excluded":
        summary = f"{group_word}: {result.exclusion_reason}"
        reasons = [{"text": result.exclusion_reason or "Excluded by a hard rule.", "fact_ids": []}]
    elif result.group == "uncertain":
        summary = f"{group_word}: {result.exclusion_reason or 'the location is unclear.'}"
        reasons = result.reasons or [{"text": "The work fits, but the location needs checking.", "fact_ids": []}]
    else:
        lead = result.reasons[0]["text"] if result.reasons else "This job fits what you want."
        summary = f"{group_word}: {lead}"
        reasons = result.reasons or [{"text": lead, "fact_ids": []}]
    return MatchExplanation(
        job_id=job["id"],
        group_hint=result.group if result.group in {"good", "worth_a_shot", "backup", "excluded"} else None,
        summary=summary[:400],
        reasons=[{"text": r["text"][:300], "fact_ids": r.get("fact_ids", [])} for r in reasons[:3]],
        gaps=[g[:300] for g in result.gaps[:4]],
        next_step=result.next_step[:200],
    )


# ---------------------------------------------------------------------------
# Model explanations (optional, validated)
# ---------------------------------------------------------------------------
def pending_for_model(db: Database, limit: int = RANK_LIMIT_FOR_MODEL) -> list[dict]:
    """Top recommended matches that still carry a rule-based explanation."""
    profile = profile_svc.approved(db)
    if not profile:
        return []
    rows = db.fetchall(
        "SELECT m.*, j.title, j.employer_name, j.location_text, j.remote_restriction, j.salary_text, j.description_text, j.sponsorship, j.remote_type "
        "FROM matches m JOIN jobs j ON j.id = m.job_id WHERE m.profile_version_id = ? AND m.policy_version = ? "
        "AND m.match_group IN ('good','worth_a_shot','backup') AND m.explanation_source != 'claude' AND j.availability != 'closed' "
        "ORDER BY CASE m.match_group WHEN 'good' THEN 0 WHEN 'worth_a_shot' THEN 1 ELSE 2 END, m.score DESC LIMIT ?",
        (profile["id"], POLICY_VERSION, limit),
    )
    out = []
    for r in rows:
        if feedback.is_dismissed(db, r["job_id"]):
            continue
        out.append(r)
    return out


def model_payload(db: Database, matches: list[dict]) -> dict:
    """Minimized payload: no documents, no raw CV, bounded description text."""
    profile = profile_svc.approved(db)
    facts: list[Fact] = profile["facts"] if profile else []
    return {
        "candidate": {
            "summary": profile["summary_text"] if profile else "",
            "level": (profile["targets"].get("level") if profile else None),
            "years_professional": (profile["targets"].get("professional_years_relevant") if profile else None),
            "facts": [
                {"id": f.id, "category": f.category, "kind": f.experience_kind, "text": f.text[:200]}
                for f in facts
                if f.status != "rejected"
            ][:80],
        },
        "jobs": [
            {
                "job_id": m["job_id"],
                "title": m["title"],
                "employer": m["employer_name"],
                "location": m["location_text"],
                "remote_rule": m["remote_restriction"],
                "salary": m["salary_text"] or "Not listed",
                "sponsorship": m["sponsorship"],
                "rule_group": m["match_group"],
                "rule_reasons": json.loads(m["reasons_json"]),
                "rule_gaps": json.loads(m["gaps_json"]),
                "description": (m["description_text"] or "")[:1500],
            }
            for m in matches
        ],
    }


def validate_explanations(db: Database, batch: ExplanationBatch) -> tuple[list[MatchExplanation], list[str]]:
    """Reject explanations that cite unknown facts or unsupported numbers/credentials."""
    profile = profile_svc.approved(db)
    if not profile:
        return [], ["no approved profile"]
    fact_ids = {f.id for f in profile["facts"]}
    fact_text = " ".join(f.text.lower() for f in profile["facts"])
    ok: list[MatchExplanation] = []
    problems: list[str] = []
    for e in batch.explanations:
        job = db.fetchone("SELECT id, description_text, title, salary_text FROM jobs WHERE id = ?", (e.job_id,))
        if not job:
            problems.append(f"job {e.job_id}: unknown job id")
            continue
        bad_ids = [fid for r in e.reasons for fid in r.fact_ids if fid not in fact_ids]
        if bad_ids:
            problems.append(f"job {e.job_id}: unknown fact ids {bad_ids}")
            continue
        if not any(r.fact_ids for r in e.reasons) and e.group_hint != "excluded":
            problems.append(f"job {e.job_id}: no reason cites a profile fact")
            continue
        job_text = ((job["description_text"] or "") + " " + (job["title"] or "") + " " + (job["salary_text"] or "")).lower()
        import re

        for num in re.findall(r"\b\d+(?:[.,]\d+)?\b", e.summary + " " + " ".join(r.text for r in e.reasons)):
            if num not in job_text and num not in fact_text and num not in {"1", "2", "3", "14", "48"}:
                problems.append(f"job {e.job_id}: number '{num}' is not in the job or your profile")
                break
        else:
            for cert in ("cka", "ckad", "cks", "aws certified", "azure", "terraform associate", "rhcsa", "rhce"):
                claim = cert in (e.summary + " " + " ".join(r.text for r in e.reasons)).lower()
                if claim and cert not in fact_text:
                    problems.append(f"job {e.job_id}: mentions '{cert}' which is not in your profile")
                    break
            else:
                ok.append(e)
    return ok, problems


def apply_explanations(db: Database, explanations: list[MatchExplanation], source: str = "claude") -> int:
    profile = profile_svc.approved(db)
    if not profile:
        return 0
    n = 0
    with db.tx():
        for e in explanations:
            m = db.fetchone("SELECT id FROM matches WHERE job_id = ? AND profile_version_id = ? AND policy_version = ?", (e.job_id, profile["id"], POLICY_VERSION))
            if not m:
                continue
            db.update(
                "matches",
                {"explanation_text": e.summary, "explanation_source": source, "explanation_json": e.model_dump_json(), "next_step": e.next_step},
                "id = ?",
                (m["id"],),
            )
            n += 1
    return n


def maybe_model_explanations(ctx: AppContext, db: Database, use_model: bool | None = None) -> dict:
    from jobhunter.services.model import provider as model_provider

    enabled = db.get_setting("model.unattended_enabled", False) if use_model is None else use_model
    if not enabled:
        return {"status": "off", "note": "Explanations are rule-based. Enable unattended Claude explanations in settings, or run /jobhunter next in Claude Code."}
    if not consent.is_granted(db, "model_processing"):
        return {"status": "no_consent", "note": "You have not allowed sending job and profile text to Claude."}
    pending = pending_for_model(db, limit=MAX_MODEL_EXPLANATIONS_PER_RUN)
    if not pending:
        return {"status": "nothing_pending"}
    prov = model_provider.select_provider(ctx, db)
    if prov is None:
        return {"status": "unavailable", "note": "No usable Claude provider (CLI login or API key) was found."}
    payload = model_payload(db, pending)
    try:
        batch = prov.explain(payload)
    except model_provider.ModelError as exc:
        db.insert("model_calls", {"kind": "explain", "provider": prov.name, "model": prov.model, "status": "failed", "error": str(exc)[:500], "created_at": now_iso()})
        return {"status": "failed", "note": f"Claude explanations failed; rule-based ones are shown. ({exc})"}
    ok, problems = validate_explanations(db, batch)
    applied = apply_explanations(db, ok, source="claude")
    db.insert("model_calls", {"kind": "explain", "provider": prov.name, "model": prov.model, "status": "ok" if applied else "invalid_output", "cost_usd": prov.last_cost_usd, "error": "; ".join(problems)[:500] or None, "created_at": now_iso()})
    return {"status": "ok", "applied": applied, "rejected": len(problems), "problems": problems[:5], "cost_usd": prov.last_cost_usd}


def group_label(group: str) -> str:
    return {"good": "Good matches", "worth_a_shot": "Worth a shot", "backup": "Backup options", "excluded": "Not recommended", "uncertain": "Uncertain"}.get(group, group)


def rematch_quietly(ctx: AppContext, db: Database) -> dict:
    """Re-score everything after a profile, preference, or policy change. Never raises; nothing is re-fetched."""
    try:
        return run_matching(ctx, db)
    except Exception as exc:  # pragma: no cover - defensive: a scoring bug must not block saving preferences
        return {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
