"""Greenhouse Job Board API adapter (read-only).

Docs: https://docs.greenhouse.io/job-board.html
Endpoint: GET https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true
Fields used: id, title, updated_at, first_published, absolute_url, location.name,
offices, departments, content (HTML-escaped), requisition_id, company_name,
application_deadline.
"""

from __future__ import annotations

from jobhunter.models import FetchResult, RawPosting
from jobhunter.services.http import FetchError, SafeClient, UnsafeURL
from jobhunter.services.sources.base import SourceAdapter
from jobhunter.util import strip_html

DEFAULT_BASE = "https://boards-api.greenhouse.io/v1/boards"


class GreenhouseAdapter(SourceAdapter):
    kind = "greenhouse"
    employer_hosted = True

    def fetch(self, client: SafeClient, source_id: str, config: dict) -> FetchResult:
        token = (config.get("token") or "").strip()
        if not token or not token.replace("-", "").replace("_", "").isalnum():
            return self.failed(source_id, "Greenhouse board token is missing or invalid.")
        base = (config.get("api_base") or DEFAULT_BASE).rstrip("/")
        url = f"{base}/{token}/jobs"
        try:
            resp = client.get(url, params={"content": "true"})
            data = resp.json()
        except (FetchError, UnsafeURL) as exc:
            return self.failed(source_id, f"Could not read Greenhouse board '{token}': {exc}", getattr(exc, "status", None))
        except ValueError:
            return self.failed(source_id, f"Greenhouse board '{token}' returned data Hunter could not parse.")
        jobs = data.get("jobs") if isinstance(data, dict) else None
        if not isinstance(jobs, list):
            return self.failed(source_id, f"Greenhouse board '{token}' returned an unexpected shape.")
        postings, skipped = self.convert_all(source_id, jobs, config)
        return self.result(source_id, postings, skipped, len(jobs))

    @staticmethod
    def _convert(source_id: str, j: dict, config: dict) -> RawPosting:
        offices = [o.get("location") or o.get("name") for o in (j.get("offices") or []) if isinstance(o, dict)]
        loc = (j.get("location") or {}).get("name") if isinstance(j.get("location"), dict) else None
        content = strip_html(j.get("content"))
        return RawPosting(
            source_id=source_id,
            external_id=str(j.get("id")),
            title=str(j.get("title") or "").strip(),
            employer_name=j.get("company_name") or config.get("employer_name"),
            url=j.get("absolute_url") or "",
            apply_url=j.get("absolute_url"),
            location_text=loc or (", ".join(o for o in offices if o) or None),
            description_text=content,
            posted_at=j.get("first_published"),
            posted_at_provenance="greenhouse.first_published" if j.get("first_published") else None,
            source_updated_at=j.get("updated_at"),
            requisition_id=str(j.get("requisition_id")) if j.get("requisition_id") else None,
            is_employer_hosted=True,
            raw={"offices": offices, "departments": [d.get("name") for d in (j.get("departments") or []) if isinstance(d, dict)], "application_deadline": j.get("application_deadline"), "internal_job_id": j.get("internal_job_id")},
        )

    def fetch_single(self, client: SafeClient, token: str, job_id: str, api_base: str | None = None) -> RawPosting | None:
        base = (api_base or DEFAULT_BASE).rstrip("/")
        try:
            j = client.get(f"{base}/{token}/jobs/{job_id}").json()
        except (FetchError, UnsafeURL, ValueError):
            return None
        if not isinstance(j, dict) or "id" not in j:
            return None
        return self._convert(f"greenhouse:{token}", j, {})
