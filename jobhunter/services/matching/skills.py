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
    "argocd": ["argo cd", "argocd", "argo-cd"],
    "flux": ["flux", "fluxcd", "flux cd"],
    "gitops": ["gitops"],
    "aws": ["aws", "amazon web services", "ec2", "s3", "iam roles", "lambda", "amazon eks"],
    "azure": ["azure", "microsoft azure", "azure devops", "entra"],
    "gcp": ["gcp", "google cloud", "google cloud platform"],
    "linux": ["linux", "ubuntu", "debian", "rhel", "centos", "red hat", "bash", "shell scripting", "shell scripts"],
    "ci/cd": ["ci/cd", "ci cd", "cicd", "pipelines", "continuous integration", "continuous delivery", "continuous deployment", "build pipelines", "deployment pipelines"],
    "jenkins": ["jenkins"],
    "github actions": ["github actions"],
    "gitlab ci": ["gitlab ci", "gitlab-ci", "gitlab ci/cd", "gitlab pipelines"],
    "circleci": ["circleci"],
    "tekton": ["tekton"],
    "python": ["python"],
    "go": ["golang", "go programming", "go language", "go (golang)"],
    "monitoring": ["observability", "monitoring", "alerting", "telemetry"],
    "prometheus": ["prometheus", "alertmanager"],
    "grafana": ["grafana"],
    "datadog": ["datadog"],
    "newrelic": ["new relic", "newrelic"],
    "splunk": ["splunk"],
    "elk": ["elk", "elasticsearch", "elastic stack", "kibana", "logstash"],
    "loki": ["loki"],
    "opentelemetry": ["opentelemetry", "otel"],
    "zabbix": ["zabbix"],
    "nagios": ["nagios"],
    "puppet": ["puppet"],
    "chef": ["chef"],
    "salt": ["saltstack", "salt stack"],
    "bicep": ["bicep"],
    "proxmox": ["proxmox"],
    "hyperv": ["hyper-v", "hyperv"],
    "virtualization": ["virtualization", "virtualisation", "hypervisor", "virtual machines"],
    "networking": ["networking", "tcp/ip", "dns", "load balancing", "load balancer", "load balancers", "vpc", "firewall", "firewalls", "vpn", "bgp"],
    "security": ["security", "vault", "secrets management", "cis benchmark", "devsecops"],
    "sql": ["postgresql", "postgres", "mysql", "sql"],
    "git": ["git", "github", "gitlab", "version control"],
    "windows server": ["windows server", "active directory"],
    "vmware": ["vmware", "vsphere", "esxi", "vcenter"],
    "nginx": ["nginx"],
    "on-call": ["on-call", "on call", "incident response", "pagerduty", "incident management"],
    "cloud": ["cloud"],
}
# A family word and the specific tools that belong to it ("monitoring" is satisfied by Prometheus or Datadog;
# a job that names Datadog asks for Datadog, not for "monitoring").
FAMILIES: dict[str, list[str]] = {
    "monitoring": ["prometheus", "grafana", "datadog", "newrelic", "splunk", "elk", "loki", "opentelemetry", "zabbix", "nagios"],
    "gitops": ["argocd", "flux"],
    "ci/cd": ["jenkins", "github actions", "gitlab ci", "circleci", "tekton"],
    "virtualization": ["vmware", "proxmox", "hyperv"],
    "kubernetes": ["aks", "eks", "gke"],
}
# Having the left-hand skill means having the right-hand ones too.
IMPLIES: dict[str, list[str]] = {"aks": ["kubernetes"], "eks": ["kubernetes"], "gke": ["kubernetes"], "cloudformation": ["aws"], "bicep": ["azure"]}
for _family, _members in FAMILIES.items():
    for _m in _members:
        IMPLIES.setdefault(_m, [])
        if _family not in IMPLIES[_m]:
            IMPLIES[_m].append(_family)
# A job asking for the left-hand tool is only partly covered by the right-hand ones: related, never the same.
RELATED: dict[str, list[str]] = {
    "azure": ["aks", "bicep"], "aws": ["eks", "cloudformation"], "gcp": ["gke"], "kubernetes": ["aks", "eks", "gke"],
    "datadog": ["prometheus", "grafana", "newrelic", "splunk", "elk", "loki", "monitoring"],
    "prometheus": ["datadog", "newrelic", "splunk", "elk", "loki", "grafana", "monitoring"],
    "grafana": ["prometheus", "datadog", "elk", "monitoring"],
    "newrelic": ["datadog", "prometheus", "monitoring"], "splunk": ["elk", "datadog", "monitoring"], "elk": ["splunk", "loki", "datadog", "prometheus", "monitoring"], "loki": ["elk", "prometheus", "monitoring"],
    "opentelemetry": ["prometheus", "datadog", "monitoring"], "zabbix": ["nagios", "prometheus", "monitoring"], "nagios": ["zabbix", "prometheus", "monitoring"],
    "flux": ["argocd", "gitops"], "argocd": ["flux", "gitops"],
    "proxmox": ["vmware", "hyperv", "virtualization"], "vmware": ["proxmox", "hyperv", "virtualization"], "hyperv": ["vmware", "proxmox", "virtualization"],
    "pulumi": ["terraform", "cloudformation", "bicep"], "terraform": ["pulumi", "cloudformation", "bicep"], "cloudformation": ["terraform", "pulumi"], "bicep": ["terraform", "pulumi", "cloudformation"],
    "jenkins": ["github actions", "gitlab ci", "circleci", "ci/cd"], "github actions": ["gitlab ci", "jenkins", "circleci", "ci/cd"], "gitlab ci": ["github actions", "jenkins", "circleci", "ci/cd"], "circleci": ["github actions", "gitlab ci", "jenkins", "ci/cd"], "tekton": ["github actions", "gitlab ci", "jenkins", "ci/cd"],
    "ansible": ["puppet", "chef", "salt"], "puppet": ["ansible", "chef", "salt"], "chef": ["ansible", "puppet", "salt"], "salt": ["ansible", "puppet", "chef"],
}
GENERIC = {"cloud"}
# Near-universal tools a job may mention but that never decide a match ("GitHub Actions" also says "git").
TRIVIAL = {"git"}
# Skills that show the work itself is infrastructure work (used to resolve odd job titles).
INFRA_SIGNAL = {"kubernetes", "aks", "eks", "gke", "docker", "terraform", "pulumi", "cloudformation", "bicep", "ansible", "puppet", "chef", "salt", "helm", "argocd", "flux", "gitops", "aws", "azure", "gcp", "linux", "ci/cd", "jenkins", "github actions", "gitlab ci", "circleci", "tekton", "monitoring", "prometheus", "grafana", "datadog", "newrelic", "splunk", "elk", "loki", "opentelemetry", "zabbix", "nagios", "on-call", "nginx", "vmware", "proxmox", "hyperv", "virtualization", "windows server", "networking"}
TOOL_LABELS = {
    "monitoring": "monitoring", "argocd": "Argo CD", "flux": "Flux", "gitops": "GitOps", "ci/cd": "CI/CD pipelines", "jenkins": "Jenkins", "github actions": "GitHub Actions", "gitlab ci": "GitLab CI", "circleci": "CircleCI", "tekton": "Tekton",
    "on-call": "on-call", "aws": "AWS", "gcp": "Google Cloud", "azure": "Azure", "sql": "SQL databases", "aks": "Azure Kubernetes Service (AKS)", "eks": "Amazon EKS", "gke": "Google GKE", "cloudformation": "CloudFormation", "bicep": "Bicep",
    "windows server": "Windows Server", "vmware": "VMware", "proxmox": "Proxmox", "hyperv": "Hyper-V", "virtualization": "virtualisation", "go": "Go", "newrelic": "New Relic", "elk": "the ELK stack", "opentelemetry": "OpenTelemetry", "datadog": "Datadog", "prometheus": "Prometheus", "grafana": "Grafana", "splunk": "Splunk", "loki": "Loki",
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
    return extract_skills(f"{title or ''}\n{description or ''}") - GENERIC - TRIVIAL


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
