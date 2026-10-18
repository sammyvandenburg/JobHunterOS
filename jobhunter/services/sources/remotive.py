"""Remotive public API adapter (discovery feed; attribution required).

Docs: https://github.com/remotive-com/remote-jobs-api
Endpoint: GET https://remotive.com/api/remote-jobs?category=devops[&search=...]
Terms: link back to the Remotive job URL and credit Remotive; listings are
delayed 24 hours; at most 2 requests per minute, advised max 4 per day.
"""

from __future__ import annotations

from jobhunter.models import FetchResult, RawPosting
from jobhunter.services.http import FetchError, SafeClient, UnsafeURL
from jobhunter.services.sources.base import SourceAdapter
from jobhunter.util import strip_html

URL = "https://remotive.com/api/remote-jobs"
DEFAULT_CATEGORIES = ["devops"]
MAX_CALLS_PER_FETCH = 2


class RemotiveAdapter(SourceAdapter):
    kind = "remotive"
    employer_hosted = False

    def fetch(self, client: SafeClient, source_id: str, config: dict) -> FetchResult:
        categories = config.get("categories") or DEFAULT_CATEGORIES
        searches = config.get("searches") or []
        calls = [{"category": c} for c in categories] + [{"search": s} for s in searches]
        calls = calls[:MAX_CALLS_PER_FETCH]
        postings: dict[str, RawPosting] = {}
        errors = []
        for params in calls:
            try:
                data = client.get(URL, params=params).json()
            except (FetchError, UnsafeURL) as exc:
                errors.append(str(exc))
                continue
            except ValueError:
                errors.append("unparseable response")
                continue
            for j in data.get("jobs", []) if isinstance(data, dict) else []:
                try:
                    p = self._convert(source_id, j)
                    postings[p.external_id] = p
                except Exception:
                    continue
        if errors and not postings:
            return self.failed(source_id, "Could not read Remotive: " + "; ".join(errors))
        return FetchResult(
            source_id=source_id,
            status="partial" if errors else "ok",
            postings=list(postings.values()),
            error="; ".join(errors) or None,
            complete=not errors,
            notes="Source: Remotive (remotive.com). Listings are delayed by 24 hours.",
        )

    @staticmethod
    def _convert(source_id: str, j: dict) -> RawPosting:
        req_loc = (j.get("candidate_required_location") or "").strip()
        return RawPosting(
            source_id=source_id,
            external_id=str(j.get("id")),
            title=str(j.get("title") or "").strip(),
            employer_name=j.get("company_name"),
            url=j.get("url") or "",
            apply_url=None,
            location_text=req_loc or None,
            remote_type="remote",
            remote_restriction=req_loc or None,
            employment_type=j.get("job_type"),
            description_text=strip_html(j.get("description")),
            salary_text=(j.get("salary") or "").strip() or None,
            posted_at=j.get("publication_date"),
            posted_at_provenance="remotive.publication_date" if j.get("publication_date") else None,
            raw={"category": j.get("category"), "tags": j.get("tags") or []},
        )
