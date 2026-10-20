"""One skill vocabulary for CVs and job texts.

Word boundaries (so "digital" is not Git and "laws" is not AWS), canonical keys, and two
relations: IMPLIES (AKS experience implies Kubernetes experience) and RELATED (a job that asks
for Azure is only partly covered by AKS). Both the document parser and Claude's structured
profile output are canonicalized here, so a phrase like "Kubernetes (AKS, k3s)" and a job line
like "Kubernetes on Azure" compare the same way whichever path produced the fact.
"""

from __future__ import annotations

import re

SKILL_VOCAB: dict[str, list[str]] = {
    "kubernetes": ["kubernetes", "k8s", "k3s", "openshift", "rancher"],
    "aks": ["aks", "azure kubernetes service"],
    "eks": ["eks", "elastic kubernetes service"],
    "gke": ["gke", "google kubernetes engine"],
    "docker": ["docker", "containers", "containerd", "podman", "containerisation", "containerization"],
    "terraform": ["terraform", "opentofu"],
    "pulumi": ["pulumi"],
    "cloudformation": ["cloudformation", "cloud formation"],
    "ansible": ["ansible"],
    "helm": ["helm", "helm charts"],
    "argocd": ["argo cd", "argocd", "gitops", "flux", "fluxcd"],
    "aws": ["aws", "amazon web services", "ec2", "s3", "iam roles", "lambda", "amazon eks"],
    "azure": ["azure", "microsoft azure", "azure devops", "entra"],
    "gcp": ["gcp", "google cloud", "google cloud platform"],
    "linux": ["linux", "ubuntu", "debian", "rhel", "centos", "red hat", "bash", "shell scripting", "shell scripts"],
    "ci/cd": ["ci/cd", "ci cd", "cicd", "jenkins", "gitlab ci", "github actions", "pipelines", "circleci", "tekton", "continuous integration", "continuous delivery", "continuous deployment"],
    "python": ["python"],
    "go": ["golang", "go programming", "go language", "go (golang)"],
    "monitoring": ["prometheus", "grafana", "datadog", "observability", "monitoring", "opentelemetry", "elk", "loki", "alerting", "new relic", "splunk", "zabbix", "nagios"],
    "networking": ["networking", "tcp/ip", "dns", "load balancing", "load balancer", "load balancers", "vpc", "firewall", "firewalls", "vpn", "bgp"],
    "security": ["security", "vault", "secrets management", "cis benchmark", "devsecops"],
    "sql": ["postgresql", "postgres", "mysql", "sql"],
    "git": ["git", "github", "gitlab", "version control"],
    "windows server": ["windows server", "active directory"],
    "vmware": ["vmware", "vsphere", "proxmox", "hyper-v"],
    "nginx": ["nginx"],
    "on-call": ["on-call", "on call", "incident response", "pagerduty", "incident management"],
    "cloud": ["cloud"],
}
# Having the left-hand skill means having the right-hand ones too.
IMPLIES: dict[str, list[str]] = {"aks": ["kubernetes"], "eks": ["kubernetes"], "gke": ["kubernetes"], "cloudformation": ["aws"]}
# A job asking for the left-hand skill is only partly covered by the right-hand ones.
RELATED: dict[str, list[str]] = {"azure": ["aks"], "aws": ["eks", "cloudformation"], "gcp": ["gke"], "kubernetes": ["aks", "eks", "gke"]}
GENERIC = {"cloud"}
# Skills that show the work itself is infrastructure work (used to resolve odd job titles).
INFRA_SIGNAL = {"kubernetes", "aks", "eks", "gke", "docker", "terraform", "pulumi", "cloudformation", "ansible", "helm", "argocd", "aws", "azure", "gcp", "linux", "ci/cd", "monitoring", "on-call", "nginx", "vmware", "windows server", "networking"}
TOOL_LABELS = {
    "monitoring": "monitoring (Prometheus, Grafana)", "argocd": "Argo CD / GitOps", "ci/cd": "CI/CD pipelines", "on-call": "on-call", "aws": "AWS", "gcp": "Google Cloud", "azure": "Azure",
    "sql": "SQL databases", "aks": "Azure Kubernetes Service (AKS)", "eks": "Amazon EKS", "gke": "Google GKE", "cloudformation": "CloudFormation", "windows server": "Windows Server", "vmware": "VMware", "go": "Go",
}


def _needle_rx(needle: str) -> str:
    return r"(?<![a-z0-9])" + re.escape(needle) + r"(?![a-z0-9])"


_PATTERNS: dict[str, re.Pattern] = {key: re.compile("|".join(_needle_rx(n) for n in needles), re.I) for key, needles in SKILL_VOCAB.items()}
_FALLBACK_CLEAN = re.compile(r"[^a-z0-9+#./ -]+")


def extract_skills(text: str | None, implied: bool = True) -> set[str]:
    """Canonical skill keys named in any text (job description or CV line). Whole words only."""
    if not text:
        return set()
    low = text.lower()
    found = {key for key, rx in _PATTERNS.items() if rx.search(low)}
    if implied:
        for key in list(found):
            found.update(IMPLIES.get(key, []))
    return found - GENERIC


def canonical_skill_keys(text: str) -> set[str]:
    """Keys for one candidate skill fact. Unknown tools keep a cleaned version of their own name,
    so the same unknown tool on the job side still matches."""
    keys = extract_skills(text)
    if keys:
        return keys
    cleaned = _FALLBACK_CLEAN.sub("", (text or "").lower()).strip()
    return {cleaned} if 1 < len(cleaned) <= 40 else set()


def job_tools(description: str | None, title: str | None) -> set[str]:
    """Tools a job names, as canonical keys. Generic words such as 'cloud' never count."""
    return extract_skills(f"{title or ''}\n{description or ''}") - GENERIC


def coverage(tools: set[str], candidate: set[str]) -> tuple[list[str], list[tuple[str, str]], list[str]]:
    """(have, related, missing). `related` pairs a requested tool with the candidate's partial match."""
    have = sorted(t for t in tools if t in candidate)
    related: list[tuple[str, str]] = []
    missing: list[str] = []
    for t in sorted(tools):
        if t in candidate:
            continue
        partial = next((r for r in RELATED.get(t, []) if r in candidate), None)
        if partial:
            related.append((t, partial))
        else:
            missing.append(t)
    return have, related, missing


def tool_label(key: str) -> str:
    return TOOL_LABELS.get(key, key.capitalize() if key.islower() and " " not in key else key)


def infra_signal_count(text: str | None) -> int:
    return len(extract_skills(text, implied=False) & INFRA_SIGNAL)
