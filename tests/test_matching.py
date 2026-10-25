"""Acceptance 9-12, 15: eligibility, remote restrictions, transferable skills, level policy, freshness vs fit."""

from __future__ import annotations

import json

from jobhunter.models import RawPosting
from jobhunter.services import hunt
from jobhunter.services import preferences as prefs_svc
from jobhunter.services.matching import engine
from jobhunter.services.normalize import countries_from_text, normalize
from tests.conftest import ENTRY_CV, approve_profile


def _job(db, job_id):
    row = db.fetchone("SELECT j.*, m.match_group, m.exclusion_reason, m.gaps_json, m.score FROM jobs j LEFT JOIN matches m ON m.job_id = j.id WHERE j.id = ?", (job_id,))
    row["gaps"] = json.loads(row.get("gaps_json") or "[]")
    return row


def _import(ctx, db, **kw):
    ext = kw.pop("external_id", "e1")
    base = dict(source_id="t:x", external_id=ext, title="Platform Engineer", employer_name="Acme", url=f"https://job-boards.greenhouse.io/acme/jobs/{ext}", description_text="Kubernetes, Terraform, AWS, Linux, CI/CD, Prometheus.", posted_at="2026-10-17T09:00:00+00:00", posted_at_provenance="t", is_employer_hosted=True)
    base.update(kw)
    res = hunt.import_postings(ctx, db, [RawPosting(**base)])
    return res["job_ids"][0]


def test_acc09_unknown_salary_not_excluded_but_wrong_country_is_visible(ctx, db):
    approve_profile(db, ctx, countries="NL, DE, FR", sponsorship="unset")
    p = prefs_svc.get(db)
    p.salary_min, p.salary_currency, p.salary_period, p.salary_strength = 50000, "EUR", "year", "nice"
    prefs_svc.save(db, p)
    unknown = _import(ctx, db, external_id="u1", location_text="Paris, France")  # salary unknown
    j = _job(db, unknown)
    assert j["match_group"] != "excluded", j
    assert any("Salary not listed" in g for g in j["gaps"])
    # Outside the person's countries: left out, with the reason visible (not hidden).
    outside = _import(ctx, db, external_id="u2", employer_name="Beta", url="https://job-boards.greenhouse.io/beta/jobs/2", location_text="Madrid, Spain")
    j2 = _job(db, outside)
    assert j2["match_group"] == "excluded" and "Spain" in j2["exclusion_reason"] and "Netherlands" in j2["exclusion_reason"]
    # Visa is never asked; only when someone sets it under More options does a 'no sponsorship' job get excluded.
    p = prefs_svc.get(db)
    p.needs_sponsorship = "yes"
    p.work_countries = ["NL"]
    prefs_svc.save(db, p)
    explicit = _import(ctx, db, external_id="u3", employer_name="Gamma", url="https://job-boards.greenhouse.io/gamma/jobs/3", location_text="Remote", remote_type="remote", description_text="Kubernetes, Terraform. We cannot offer visa sponsorship.")
    j3 = _job(db, explicit)
    assert j3["match_group"] == "excluded" and "sponsor" in j3["exclusion_reason"].lower()


def test_acc10_remote_in_one_country_is_not_worldwide(ctx, db):
    approve_profile(db, ctx, countries="NL", sponsorship="no")
    jid = _import(ctx, db, external_id="r1", location_text="Remote - Germany", remote_type="remote", country_codes=["DE"])
    j = _job(db, jid)
    assert j["remote_type"] == "remote" and "Germany" in (j["remote_restriction"] or "")
    assert j["match_group"] == "excluded" and "Netherlands" in j["exclusion_reason"]
    codes, tags = countries_from_text("Remote - US only")
    assert codes == ["US"] and "WORLD" not in tags
    codes, tags = countries_from_text("Worldwide")
    assert "WORLD" in tags and not codes


def test_acc11_related_titles_and_missing_tool_not_rejected(ctx, db):
    approve_profile(db, ctx)
    jid = _import(ctx, db, external_id="t1", title="Production Engineer", location_text="Amsterdam, Netherlands", description_text="Run our Linux fleet, Terraform, Pulumi, Nomad, Consul, CI/CD. Kubernetes experience is a plus.")
    j = _job(db, jid)
    assert j["relevance"] == "relevant"
    assert j["match_group"] in {"good", "worth_a_shot", "backup"}
    assert any("not established in your approved profile" in g or "not in your approved profile" in g or "Your profile shows" in g for g in j["gaps"]), j["gaps"]
    jid2 = _import(ctx, db, external_id="t2", title="Backend Engineer", location_text="Amsterdam, Netherlands", description_text="You will own Kubernetes clusters, Terraform modules, AWS accounts, CI/CD pipelines and on-call for the platform.")
    assert _job(db, jid2)["relevance"] == "uncertain"


def test_acc12_level_policy(ctx, db):
    approve_profile(db, ctx, cv_text=ENTRY_CV, countries="EU")
    senior = _import(ctx, db, external_id="s1", title="Senior Site Reliability Engineer", location_text="Berlin, Germany", description_text="8+ years running production Kubernetes. Terraform, AWS.")
    entry = _import(ctx, db, external_id="s2", title="Junior DevOps Engineer", location_text="Berlin, Germany", description_text="Kubernetes, Terraform, Linux, Git. Great for someone with strong project work.")
    mid = _import(ctx, db, external_id="s3", title="DevOps Engineer", location_text="Berlin, Germany", description_text="Kubernetes, Terraform, Linux, Git, Argo CD.")
    assert _job(db, senior)["match_group"] == "excluded" and "senior" in _job(db, senior)["exclusion_reason"].lower()
    assert _job(db, entry)["match_group"] in {"good", "worth_a_shot"}
    assert _job(db, mid)["match_group"] in {"worth_a_shot", "backup", "good"}


def test_acc12b_experienced_candidate_gets_senior_stretch(ctx, db):
    approve_profile(db, ctx, countries="EU")
    senior = _import(ctx, db, external_id="s4", title="Staff Platform Engineer", location_text="Amsterdam, Netherlands", description_text="Kubernetes, Terraform, AWS, Argo CD, Prometheus, Grafana, Linux, Python, CI/CD. 5+ years.")
    j = _job(db, senior)
    assert j["match_group"] in {"good", "worth_a_shot"}
    assert any("level above" in g.lower() or "levels above" in g.lower() for g in j["gaps"])


def test_acc15_fit_outranks_freshness_and_old_kept_separately(ctx, db):
    approve_profile(db, ctx)
    old_good = _import(ctx, db, external_id="f1", title="Platform Engineer", location_text="Amsterdam, Netherlands", posted_at="2026-10-08T09:00:00+00:00", description_text="Kubernetes, Terraform, AWS, Argo CD, Helm, Prometheus, Grafana, Linux, Python, Bash, CI/CD, Ansible.")
    new_poor = _import(ctx, db, external_id="f2", title="Infrastructure Engineer", employer_name="Other", url="https://job-boards.greenhouse.io/other/jobs/2", location_text="Amsterdam, Netherlands", posted_at="2026-10-18T06:00:00+00:00", description_text="Mainframe COBOL operations, z/OS, JCL. Some cloud exposure helpful.")
    a, b = _job(db, old_good), _job(db, new_poor)
    assert a["score"] > b["score"]
    from jobhunter.services import views

    very_old = _import(ctx, db, external_id="f3", title="Platform Engineer", employer_name="Oldco", url="https://job-boards.greenhouse.io/oldco/jobs/3", location_text="Amsterdam, Netherlands", posted_at="2026-09-01T09:00:00+00:00")
    new_ids = {i["id"] for i in views.list_jobs(db, view="new", per_page=50)["items"]}
    old_ids = {i["id"] for i in views.list_jobs(db, view="old", per_page=50)["items"]}
    assert very_old in old_ids and very_old not in new_ids
    undated = _import(ctx, db, external_id="f4", title="Platform Engineer", employer_name="Undated", url="https://job-boards.greenhouse.io/undated/jobs/4", location_text="Amsterdam, Netherlands", posted_at=None, posted_at_provenance=None)
    assert undated in {i["id"] for i in views.list_jobs(db, view="new", per_page=50)["items"]}


def test_salary_units_never_silently_converted(ctx, db):
    approve_profile(db, ctx)
    p = prefs_svc.get(db)
    p.salary_min, p.salary_currency, p.salary_period, p.salary_strength = 60000, "EUR", "year", "must"
    prefs_svc.save(db, p)
    usd = _import(ctx, db, external_id="m1", employer_name="UsdCo", location_text="Amsterdam, Netherlands", salary_text="$40,000 - $50,000 per year")
    j = _job(db, usd)
    assert j["match_group"] != "excluded" and any("did not convert" in g for g in j["gaps"])
    overlap = _import(ctx, db, external_id="m2", employer_name="OverlapCo", location_text="Amsterdam, Netherlands", salary_text="€55,000 - €65,000 per year")
    assert _job(db, overlap)["match_group"] != "excluded"
    below = _import(ctx, db, external_id="m3", employer_name="BelowCo", location_text="Amsterdam, Netherlands", salary_text="€40,000 - €50,000 per year")
    assert _job(db, below)["match_group"] == "excluded"


def test_normalize_parses_sponsorship_and_salary():
    n = normalize(RawPosting(source_id="t", external_id="1", title="DevOps Engineer", employer_name="X", url="https://example.com/j", description_text="Unfortunately we cannot offer visa sponsorship. Base salary £50,000 - £60,000 per annum.", location_text="London, UK"))
    assert n.sponsorship == "no" and n.salary_currency == "GBP" and n.salary_min == 50000 and n.country_codes == ["GB"]


def test_explanations_validate_fact_ids_and_numbers(ctx, db):
    approve_profile(db, ctx)
    jid = _import(ctx, db, external_id="x1", location_text="Amsterdam, Netherlands")
    from jobhunter.models import ExplanationBatch

    batch = ExplanationBatch.model_validate({"explanations": [
        {"job_id": jid, "summary": "Good match for your Kubernetes work.", "reasons": [{"text": "You have Kubernetes evidence.", "fact_ids": ["nope"]}], "gaps": [], "next_step": "Apply."},
        {"job_id": jid, "summary": "You have 12 years of experience with Kubernetes.", "reasons": [{"text": "Kubernetes.", "fact_ids": []}], "gaps": [], "next_step": "Apply."},
    ]})
    ok, problems = engine.validate_explanations(db, batch)
    assert not ok and len(problems) == 2


def test_latin_american_countries_parse_and_display(db):
    from jobhunter.services import onboarding
    from jobhunter.services.normalize import country_names

    assert prefs_svc.parse_country_list("Costa Rica") == (["CR"], [])
    assert country_names(["CR"]) == "Costa Rica"
    codes, unknown = prefs_svc.parse_country_list("Argentina, Brazil, Chile, Colombia, Peru, Uruguay, Panama, Guatemala, India, Australia, South Africa")
    assert codes == ["AR", "BR", "CL", "CO", "PE", "UY", "PA", "GT", "IN", "AU", "ZA"] and not unknown
    assert "CR" not in country_names(codes)
    for region in ("latam", "Latin America"):
        codes, unknown = prefs_svc.parse_country_list(region)
        assert {"CR", "AR", "BR", "MX"} <= set(codes) and "US" not in codes and not unknown
        assert country_names(codes) == "Latin America"
    # The CLI path (`jobhunter setup basics --countries "Costa Rica"`) stores the code and shows the name.
    onboarding.basics(db, location="San José, Costa Rica", countries="Costa Rica")
    p = prefs_svc.get(db)
    assert p.work_countries == ["CR"]
    assert "Countries where you can work: Costa Rica" in prefs_svc.describe(p)


def test_latam_text_is_a_region_not_the_usa():
    codes, tags = countries_from_text("Remote - Latin America")
    assert tags == ["LATAM"] and "US" not in codes
    codes, tags = countries_from_text("San Jose, Costa Rica")
    assert codes == ["CR"]
    codes, tags = countries_from_text("San Jose, CA")
    assert codes == ["US"]


def test_latam_remote_job_is_open_to_costa_rica(ctx, db):
    approve_profile(db, ctx, countries="Costa Rica")
    latam = _job(db, _import(ctx, db, external_id="l1", location_text="Remote, LATAM", remote_type="remote"))
    assert "CR" in json.loads(latam["country_codes_json"])
    assert latam["match_group"] in {"good", "worth_a_shot", "backup"}, latam
    assert latam["remote_restriction"] == "Remote, but only in Latin America"
    americas = _job(db, _import(ctx, db, external_id="l2", employer_name="Beta", url="https://job-boards.greenhouse.io/beta/jobs/2", location_text="Remote - Americas", remote_type="remote"))
    assert americas["match_group"] in {"good", "worth_a_shot", "backup"}, americas
    us_only = _job(db, _import(ctx, db, external_id="l3", employer_name="Gamma", url="https://job-boards.greenhouse.io/gamma/jobs/3", location_text="Remote - US only", remote_type="remote"))
    assert us_only["match_group"] == "excluded" and "Costa Rica" in us_only["exclusion_reason"]
