"""Shared fixtures. Every test runs against an isolated temporary data directory;
network access is replaced by httpx MockTransport serving synthetic fixtures."""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest

from jobhunter.config import AppContext
from jobhunter.db import connect
from jobhunter.services import consent
from jobhunter.services import documents as docs
from jobhunter.services import preferences as prefs_svc
from jobhunter.services import profile as profile_svc
from jobhunter.services.http import SafeClient

FIXTURES = Path(__file__).parent / "fixtures"

SAMPLE_CV = """Sam Example
Rotterdam, Netherlands · sam@example.invalid

Summary
Linux support engineer moving into platform work after KubeCraft.

Experience
IT Support Engineer, Example Hosting BV — Jan 2022 to Present
- Managed 40 Ubuntu servers, patching, backups and monitoring with Prometheus and Grafana.
- Wrote Bash and Python scripts to automate provisioning; on-call one week per month.
Junior System Administrator, Demo Logistics — Mar 2020 to Dec 2021
- Maintained Windows Server and VMware hosts; introduced Ansible.
Helpdesk Analyst, Demo Logistics — Jun 2021 to Dec 2021
- Overlapping part-time helpdesk duties during the same months.

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

LINKEDIN_TEXT = """Sam Example — Linux Support Engineer | Kubernetes | CKA
Rotterdam Area, Netherlands

About
Support engineer with a passion for automation, moving into platform engineering after the KubeCraft programme.

Experience
IT Support Engineer · Example Hosting BV · Jan 2022 – Present
Junior System Administrator · Demo Logistics · Mar 2020 – Dec 2021

Licenses & certifications
Certified Kubernetes Administrator (CKA) · The Linux Foundation · Issued 2025

Skills
Kubernetes · Docker · Terraform · Ansible · Linux · Python · Bash · AWS · Argo CD · Helm · Prometheus
"""

ENTRY_CV = """Jo Newcomer
Berlin, Germany · jo@example.invalid

Summary
Career changer finishing the KubeCraft programme. No paid IT roles yet.

Projects
Homelab Kubernetes cluster with k3s, Argo CD, Terraform and Prometheus monitoring.
KubeCraft capstone: AWS EKS deployment with GitHub Actions CI/CD (guided course project).

Skills
Kubernetes, Docker, Terraform, Linux, Bash, Git, Argo CD, Prometheus, AWS

Certifications
Certified Kubernetes Administrator (CKA) course (in progress)

Education
BA History, Demo University, 2014–2018
"""


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    h = tmp_path / "jh"
    monkeypatch.setenv("JOBHUNTER_HOME", str(h))
    monkeypatch.delenv("JOBHUNTER_DEMO", raising=False)
    monkeypatch.setenv("JOBHUNTER_SECRETS_BACKEND", "file")
    return h


@pytest.fixture
def ctx(home) -> AppContext:
    return AppContext.from_env(demo=False, home=home)


@pytest.fixture
def db(ctx):
    d = connect(ctx.paths.db_path)
    yield d
    d.close()


def load_fixture(name: str):
    with open(FIXTURES / name, encoding="utf-8") as fh:
        return json.load(fh)


def approve_profile(db, ctx, cv_text: str = SAMPLE_CV, direction: str = "platform, devops", countries: str = "EU", sponsorship: str = "no"):
    """Full onboarding shortcut used by many tests."""
    consent.grant(db, "readiness", {"self_reported": True})
    docs.add_text_document(db, ctx.paths, "cv", cv_text, "cv")
    db.set_setting("linkedin.url", "https://www.linkedin.com/in/sam-example")
    docs.add_text_document(db, ctx.paths, "linkedin", LINKEDIN_TEXT, "linkedin")
    draft = profile_svc.build_rule_based_draft(db)
    profile_svc.save_draft(db, draft, built_by="rules")
    if any(q["key"] == "target_direction" for q in profile_svc.open_questions(db)):
        profile_svc.answer_question(db, "target_direction", direction)
    profile_svc.approve(db)
    consent.grant(db, "profile_approved", {})
    prefs = prefs_svc.get(db)
    prefs.current_location = "Rotterdam, Netherlands"
    prefs.work_countries, _ = prefs_svc.parse_country_list(countries)
    prefs.needs_sponsorship = sponsorship  # type: ignore[assignment]
    prefs.relocation = "not_sure"
    prefs.timezone = "Europe/Amsterdam"
    prefs_svc.save(db, prefs, reason="test")
    return profile_svc.approved(db)


class FixtureTransport(httpx.BaseTransport):
    """Serves fixture JSON per host/path; records requests; can fail hosts on demand."""

    def __init__(self):
        self.routes: dict[str, object] = {}
        self.fail_hosts: set[str] = set()
        self.status: dict[str, int] = {}
        self.calls: list[str] = []

    def add(self, host: str, path_prefix: str, payload) -> None:
        self.routes[f"{host}{path_prefix}"] = payload

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        self.calls.append(str(url))
        if url.host in self.fail_hosts:
            raise httpx.ConnectError("simulated outage")
        key_full = f"{url.host}{url.path}"
        for key, payload in sorted(self.routes.items(), key=lambda kv: -len(kv[0])):
            if key_full.startswith(key):
                status = self.status.get(key, 200)
                body = payload(request) if callable(payload) else payload
                if isinstance(body, bytes | str):
                    return httpx.Response(status, content=body, request=request)
                return httpx.Response(status, json=body, request=request)
        return httpx.Response(404, json={"error": "not found"}, request=request)


@pytest.fixture
def transport(monkeypatch):
    t = FixtureTransport()
    # Resolve nothing: avoid DNS in validate_url by patching the resolver to a public address.
    import jobhunter.services.http as http_mod

    monkeypatch.setattr(http_mod.socket, "getaddrinfo", lambda host, *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))])
    return t


@pytest.fixture
def client(transport):
    c = SafeClient(transport=transport, min_interval_per_host=0.0, retries=0)
    yield c
    c.close()


@pytest.fixture
def seeded_sources(db, transport):
    """Catalogue-like sources backed by fixtures for greenhouse, lever, ashby, remotive, arbeitnow."""
    from jobhunter.services import catalog

    with db.tx():
        e1 = catalog.upsert_employer(db, "Northwind Cloud", origin="catalogue", ats_kind="greenhouse", ats_token="northwind")
        catalog.ensure_source(db, "greenhouse:northwind", "greenhouse", {"token": "northwind", "employer_name": "Northwind Cloud"}, "catalogue", e1)
        e2 = catalog.upsert_employer(db, "Fabrikam Mobility", origin="catalogue", ats_kind="lever", ats_token="fabrikam")
        catalog.ensure_source(db, "lever:fabrikam", "lever", {"site": "fabrikam", "employer_name": "Fabrikam Mobility", "region": "us"}, "catalogue", e2)
        e3 = catalog.upsert_employer(db, "Contoso Payments", origin="catalogue", ats_kind="ashby", ats_token="contoso")
        catalog.ensure_source(db, "ashby:contoso", "ashby", {"board": "contoso", "employer_name": "Contoso Payments"}, "catalogue", e3)
        catalog.ensure_source(db, "remotive:devops", "remotive", {"categories": ["devops"]}, "builtin")
        catalog.ensure_source(db, "arbeitnow:eu", "arbeitnow", {"feed": "eu", "max_pages": 1}, "builtin")
    transport.add("boards-api.greenhouse.io", "/v1/boards/northwind/jobs", load_fixture("greenhouse_sample.json"))
    transport.add("api.lever.co", "/v0/postings/fabrikam", load_fixture("lever_sample.json"))
    transport.add("api.ashbyhq.com", "/posting-api/job-board/contoso", load_fixture("ashby_sample.json"))
    transport.add("remotive.com", "/api/remote-jobs", load_fixture("remotive_sample.json"))
    transport.add("www.arbeitnow.com", "/api/job-board-api", load_fixture("arbeitnow_sample.json"))
    return transport


def has_env(name: str) -> bool:
    return os.environ.get(name, "").strip() not in {"", "0", "false"}
