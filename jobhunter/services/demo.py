"""Demo mode: clearly labelled synthetic data in a separate data directory.

Nothing here touches live state. Demo jobs come from fixture files, not the
internet, and the dashboard shows a banner on every page.
"""

from __future__ import annotations

import json
from pathlib import Path

from jobhunter.config import AppContext
from jobhunter.db import Database
from jobhunter.models import RawPosting
from jobhunter.services import actions, consent, hunt, onboarding
from jobhunter.services import documents as docs
from jobhunter.services import preferences as prefs_svc
from jobhunter.services import profile as profile_svc
from jobhunter.util import now_iso

FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures"

DEMO_CV = """Alex Demo
Amsterdam, Netherlands · alex.demo@example.invalid · github.com/alex-demo

Summary
Linux support engineer moving into platform and DevOps work after completing the KubeCraft programme.

Experience
IT Support Engineer, Example Hosting BV — Jan 2022 to Present
- Managed 40 Ubuntu servers, patching, backups and monitoring with Prometheus and Grafana.
- Wrote Bash and Python scripts to automate user provisioning; on-call one week per month.
Junior System Administrator, Demo Logistics — Mar 2020 to Dec 2021
- Maintained Windows Server and VMware hosts; introduced Ansible for configuration.

Projects
Homelab Kubernetes cluster (k3s) with GitOps via Argo CD, Terraform-managed DNS, and Helm charts.
KubeCraft capstone: deployed a three-tier app on AWS EKS with CI/CD in GitHub Actions (guided course project).

Skills
Kubernetes, Docker, Terraform, Ansible, Helm, Argo CD, AWS (EC2, S3, IAM), Linux, Bash, Python, Git, CI/CD (GitHub Actions), Prometheus, Grafana, Networking (DNS, TCP/IP)

Certifications
Certified Kubernetes Administrator (CKA), 2025
AWS Certified Solutions Architect – Associate (in progress)

Education
BSc Information Technology, Demo University of Applied Sciences, 2016–2020
"""

DEMO_LINKEDIN = """Alex Demo — Linux Support Engineer | Kubernetes | CKA
Amsterdam Area

About
Support engineer with a passion for automation, moving into platform engineering.

Experience
IT Support Engineer · Example Hosting BV · Jan 2022 – Present
Junior System Administrator · Demo Logistics · Mar 2020 – Dec 2021

Licenses & certifications
Certified Kubernetes Administrator (CKA) · The Linux Foundation · Issued 2025

Skills
Kubernetes · Docker · Terraform · Ansible · Linux · Python · Bash · AWS · Argo CD · Helm · Prometheus
"""


def demo_postings() -> list[RawPosting]:
    with open(FIXTURES / "demo_jobs.json", encoding="utf-8") as fh:
        raw = json.load(fh)
    return [RawPosting.model_validate(r) for r in raw]


def seed(ctx: AppContext, db: Database) -> dict:
    if not ctx.demo:
        raise RuntimeError("Refusing to seed demo data into live state. Use `jobhunter --demo demo seed`.")
    if db.get_setting("demo.seeded"):
        return {"seeded": False, "note": "Demo already seeded. Use `jobhunter --demo demo reset` to start over."}
    consent.grant(db, "readiness", {"self_reported": True, "demo": True})
    docs.add_text_document(db, ctx.paths, "cv", DEMO_CV, "demo-cv")
    db.set_setting("linkedin.url", "https://www.linkedin.com/in/alex-demo-example")
    docs.add_text_document(db, ctx.paths, "linkedin", DEMO_LINKEDIN, "demo-linkedin", source_url="https://www.linkedin.com/in/alex-demo-example")
    draft = profile_svc.build_rule_based_draft(db)
    profile_svc.save_draft(db, draft, built_by="rules")
    profile_svc.answer_question(db, "target_direction", "platform, devops")
    profile_svc.approve(db)
    consent.grant(db, "profile_approved", {"demo": True})
    prefs = prefs_svc.get(db)
    prefs.current_location = "Amsterdam, Netherlands"
    prefs.work_countries = sorted(prefs_svc.EU_EEA)
    prefs.needs_sponsorship = "no"
    prefs.relocation = "not_sure"
    prefs.timezone = "Europe/Amsterdam"
    prefs.remote = "preferred"
    prefs_svc.save(db, prefs, reason="demo seed")
    db.execute("UPDATE schedule SET timezone = 'Europe/Amsterdam' WHERE id = 1")
    state = onboarding._state(db)
    for key in ("github", "hunt_frequency", "notify_mode"):
        state["answers"][key] = "demo"
    onboarding._save(db, state)
    result = hunt.import_postings(ctx, db, demo_postings(), trigger="demo")
    ids = result["job_ids"]
    if len(ids) >= 3:
        actions.save_job(db, ids[0])
        from jobhunter.services import drafts

        drafts.prepare(ctx, db, ids[1])  # records which CV version was prepared
        actions.mark_applied(db, ids[1], applied_at="2026-10-10T09:00:00+00:00")
        actions.set_stage(db, ids[1], "interview", actor="user", occurred_at="2026-10-15T10:00:00+00:00", payload={"note": "demo"})
        actions.dismiss_job(db, ids[-1], reason_code="consultancy")
    db.set_setting("demo.seeded", {"at": now_iso(), "jobs": len(ids)})
    return {"seeded": True, "jobs": len(ids), "matching": result.get("matching"), "note": "Demo data is synthetic. Nothing was fetched from the internet."}


def reset(ctx: AppContext, db: Database) -> dict:
    if not ctx.demo:
        raise RuntimeError("Refusing to reset live state. Use `jobhunter --demo demo reset`.")
    from jobhunter.services import datamgmt

    datamgmt.delete_all(ctx, db, confirm="DELETE")
    return {"reset": True}
