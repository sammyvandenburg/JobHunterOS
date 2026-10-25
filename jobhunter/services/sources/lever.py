"""Lever Postings API adapter (read-only).

Docs: https://github.com/lever/postings-api
Endpoint: GET https://api.lever.co/v0/postings/{site}?mode=json&skip=N&limit=M
EU: https://api.eu.lever.co/v0/postings/{site}
Fields used: id, text, categories{location,commitment,team,allLocations},
country, workplaceType, createdAt (ms epoch), descriptionPlain, lists,
additionalPlain, hostedUrl, applyUrl, salaryRange{currency,interval,min,max}.
"""

from __future__ import annotations

from jobhunter.models import FetchResult, RawPosting
from jobhunter.services.http import FetchError, SafeClient, UnsafeURL
from jobhunter.services.sources.base import SourceAdapter
from jobhunter.util import epoch_to_iso

BASES = {"us": "https://api.lever.co/v0/postings", "eu": "https://api.eu.lever.co/v0/postings"}
PAGE = 100
MAX_PAGES = 10


class LeverAdapter(SourceAdapter):
    kind = "lever"
    employer_hosted = True

    def fetch(self, client: SafeClient, source_id: str, config: dict) -> FetchResult:
        site = (config.get("site") or "").strip()
        if not site or not site.replace("-", "").replace("_", "").isalnum():
            return self.failed(source_id, "Lever site name is missing or invalid.")
        base = BASES.get((config.get("region") or "us").lower(), BASES["us"])
        postings: list[RawPosting] = []
        skipped: list[str] = []
        total = 0
        skip = 0
        complete = True
        for _ in range(MAX_PAGES):
            try:
                data = client.get(f"{base}/{site}", params={"mode": "json", "skip": skip, "limit": PAGE}).json()
            except (FetchError, UnsafeURL) as exc:
                if postings:
                    return FetchResult(source_id=source_id, status="partial", postings=postings, error=str(exc), complete=False, http_status=getattr(exc, "status", None))
                return self.failed(source_id, f"Could not read Lever site '{site}': {exc}", getattr(exc, "status", None), retry_after=getattr(exc, "retry_after", None))
            except ValueError:
                return self.failed(source_id, f"Lever site '{site}' returned data Hunter could not parse.")
            if not isinstance(data, list):
                return self.failed(source_id, f"Lever site '{site}' returned an unexpected shape.")
            got, bad = self.convert_all(source_id, data, config)
            postings += got
            skipped += bad
            total += len(data)
            if len(data) < PAGE:
                break
            skip += PAGE
        else:
            complete = False
        return self.result(source_id, postings, skipped, total, complete=complete)

    @staticmethod
    def _convert(source_id: str, j: dict, config: dict) -> RawPosting:
        cats = j.get("categories") or {}
        all_locs = cats.get("allLocations") or []
        loc = cats.get("location") or (", ".join(all_locs) if all_locs else None)
        wt = (j.get("workplaceType") or "unspecified").lower()
        remote = {"remote": "remote", "hybrid": "hybrid", "on-site": "onsite"}.get(wt, "unknown")
        lists_text = "\n".join(
            f"{item.get('text', '')}\n{item.get('content', '')}" for item in (j.get("lists") or []) if isinstance(item, dict)
        )
        from jobhunter.util import strip_html

        desc = "\n".join(
            part for part in [j.get("descriptionPlain") or "", strip_html(lists_text), j.get("additionalPlain") or ""] if part
        )
        sal = j.get("salaryRange") or {}
        country = j.get("country")
        return RawPosting(
            source_id=source_id,
            external_id=str(j.get("id")),
            title=str(j.get("text") or "").strip(),
            employer_name=config.get("employer_name"),
            url=j.get("hostedUrl") or "",
            apply_url=j.get("applyUrl") or j.get("hostedUrl"),
            location_text=loc,
            country_codes=[country.upper()] if isinstance(country, str) and len(country) == 2 else [],
            remote_type=remote,  # type: ignore[arg-type]
            employment_type=cats.get("commitment"),
            description_text=desc,
            salary_min=sal.get("min"),
            salary_max=sal.get("max"),
            salary_currency=sal.get("currency"),
            salary_period={"per-year-salary": "year", "per-month-salary": "month", "per-hour-wage": "hour"}.get(str(sal.get("interval")), None) if sal else None,
            salary_text=j.get("salaryDescriptionPlain"),
            posted_at=epoch_to_iso(j.get("createdAt")),
            posted_at_provenance="lever.createdAt" if j.get("createdAt") else None,
            is_employer_hosted=True,
            raw={"team": cats.get("team"), "department": cats.get("department"), "allLocations": all_locs},
        )
