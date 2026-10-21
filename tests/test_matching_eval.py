"""The labelled evaluation set (tests/fixtures/eval_vacancies.json): four synthetic candidates against twenty
vacancies labelled by hand. Two numbers are reported separately and both must hold: false positives
(recommended although the label says out) and missed (left out although the label says recommend).
A 'check' label accepts Needs checking or a recommendation, never out. The numbers are printed so a change in
the rules shows its effect; docs/matching-eval.md records the baseline."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jobhunter.models import RawPosting
from jobhunter.services import hunt
from jobhunter.services import preferences as prefs_svc
from tests.conftest import approve_profile
from tests.test_matching_candidates import BEGINNER_CV
from tests.test_matching_matrix import CAREER_CHANGER_CV, MID_CV, SENIOR_CV

CANDIDATES = {"beginner": BEGINNER_CV, "career_changer": CAREER_CHANGER_CV, "mid": MID_CV, "senior": SENIOR_CV}
FIXTURE = Path(__file__).parent / "fixtures" / "eval_vacancies.json"
RECOMMEND = {"good", "worth_a_shot", "backup"}


def _outcome(row: dict) -> str:
    g = row.get("match_group")
    if g in RECOMMEND:
        return "recommend"
    if g == "uncertain":
        return "check"
    return "out"


def evaluate(ctx, db, name: str, cv: str) -> dict:
    approve_profile(db, ctx, cv_text=cv, countries="Netherlands")
    prefs = prefs_svc.get(db)
    prefs.remote = "preferred"
    prefs.work_modes = ["remote", "hybrid"]
    prefs_svc.save(db, prefs, reason="eval")
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    posts = []
    for v in data["vacancies"]:
        posts.append(RawPosting(source_id="eval", external_id=f"{name}-{v['id']}", title=v["title"], employer_name=f"Employer {v['id']}", url=f"https://job-boards.greenhouse.io/eval/jobs/{v['id']}", location_text=v["location"], country_codes=v["countries"], remote_type=v["remote"], employment_type=v.get("employment_type"), description_text=v["description"], posted_at="2026-10-19T09:00:00+00:00", posted_at_provenance="t", is_employer_hosted=True))
    ids = hunt.import_postings(ctx, db, posts)["job_ids"]
    results = {"false_positives": [], "missed": [], "right": 0, "total": 0}
    for v, jid in zip(data["vacancies"], ids, strict=True):
        row = db.fetchone("SELECT m.match_group, m.exclusion_reason FROM jobs j LEFT JOIN matches m ON m.job_id = j.id AND m.profile_version_id = (SELECT id FROM profile_versions WHERE status = 'approved' ORDER BY version DESC LIMIT 1) WHERE j.id = ?", (jid,))
        got = _outcome(row)
        want = v["labels"][name]
        results["total"] += 1
        if want == "out" and got == "recommend":
            results["false_positives"].append((v["id"], v["title"], row["match_group"]))
        elif want == "recommend" and got == "out":
            results["missed"].append((v["id"], v["title"], row["exclusion_reason"]))
        elif want == "check" and got == "out":
            results["missed"].append((v["id"], v["title"], row["exclusion_reason"]))
        else:
            results["right"] += 1
    return results


@pytest.mark.parametrize("name", list(CANDIDATES))
def test_labelled_set_has_no_false_positives_and_nothing_missed(ctx, db, name):
    r = evaluate(ctx, db, name, CANDIDATES[name])
    print(f"\n[eval] {name}: {r['right']}/{r['total']} right, false positives {len(r['false_positives'])}, missed {len(r['missed'])}")
    for fp in r["false_positives"]:
        print(f"  false positive: {fp}")
    for m in r["missed"]:
        print(f"  missed: {m}")
    assert r["false_positives"] == [], r["false_positives"]
    assert r["missed"] == [], r["missed"]
