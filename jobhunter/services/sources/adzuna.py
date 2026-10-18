"""Adzuna Jobs API adapter (optional; needs a free developer app_id/app_key).

Docs: https://developer.adzuna.com/docs/search
Endpoint: GET https://api.adzuna.com/v1/api/jobs/{country}/search/{page}
Parameters used: app_id, app_key, what_or, results_per_page, max_days_old,
sort_by=date. Credentials come from the secret store or environment and are
never logged.
"""

from __future__ import annotations

import os

from jobhunter.models import FetchResult, RawPosting
from jobhunter.services.http import FetchError, SafeClient, UnsafeURL
from jobhunter.services.sources.base import SourceAdapter
from jobhunter.util import strip_html

BASE = "https://api.adzuna.com/v1/api/jobs"
DEFAULT_COUNTRIES = ["gb", "us", "ca", "de", "nl", "fr", "at", "pl", "it", "es"]
DEFAULT_QUERY = "devops platform sre reliability infrastructure kubernetes cloud terraform"
MAX_PAGES = 2
RESULTS_PER_PAGE = 50


class AdzunaAdapter(SourceAdapter):
    kind = "adzuna"
    employer_hosted = False

    def __init__(self, app_id: str | None = None, app_key: str | None = None):
        self.app_id = app_id
        self.app_key = app_key

    def credentials(self, config: dict) -> tuple[str | None, str | None]:
        app_id = self.app_id or config.get("app_id") or os.environ.get("ADZUNA_APP_ID")
        app_key = self.app_key or config.get("app_key") or os.environ.get("ADZUNA_APP_KEY")
        return app_id, app_key

    def fetch(self, client: SafeClient, source_id: str, config: dict) -> FetchResult:
        app_id, app_key = self.credentials(config)
        if not app_id or not app_key:
            return FetchResult(
                source_id=source_id,
                status="skipped",
                error="Adzuna keys are not configured. Set ADZUNA_APP_ID and ADZUNA_APP_KEY to enable this source.",
                complete=False,
            )
        countries = [c.lower() for c in (config.get("countries") or DEFAULT_COUNTRIES)]
        query = config.get("query") or DEFAULT_QUERY
        max_days = int(config.get("max_days_old") or 14)
        postings: list[RawPosting] = []
        errors: list[str] = []
        for country in countries:
            for page in range(1, int(config.get("max_pages") or MAX_PAGES) + 1):
                try:
                    data = client.get(
                        f"{BASE}/{country}/search/{page}",
                        params={
                            "app_id": app_id,
                            "app_key": app_key,
                            "what_or": query,
                            "results_per_page": RESULTS_PER_PAGE,
                            "max_days_old": max_days,
                            "sort_by": "date",
                            "content-type": "application/json",
                        },
                    ).json()
                except (FetchError, UnsafeURL) as exc:
                    errors.append(f"{country}: {_scrub(str(exc), app_id, app_key)}")
                    break
                except ValueError:
                    errors.append(f"{country}: unparseable response")
                    break
                results = data.get("results") if isinstance(data, dict) else None
                if not isinstance(results, list):
                    errors.append(f"{country}: unexpected shape")
                    break
                for j in results:
                    try:
                        postings.append(self._convert(source_id, j, country))
                    except Exception:
                        continue
                if len(results) < RESULTS_PER_PAGE:
                    break
        if errors and not postings:
            return self.failed(source_id, "Could not read Adzuna: " + "; ".join(errors))
        return FetchResult(
            source_id=source_id,
            status="partial" if errors else "ok",
            postings=postings,
            error="; ".join(errors) or None,
            complete=not errors,
            notes="Source: Adzuna. Descriptions are snippets; open the listing for details.",
        )

    @staticmethod
    def _convert(source_id: str, j: dict, country: str) -> RawPosting:
        company = (j.get("company") or {}).get("display_name") if isinstance(j.get("company"), dict) else None
        loc = (j.get("location") or {}).get("display_name") if isinstance(j.get("location"), dict) else None
        predicted = str(j.get("salary_is_predicted")) == "1"
        return RawPosting(
            source_id=source_id,
            external_id=str(j.get("id")),
            title=str(j.get("title") or "").strip(),
            employer_name=company,
            url=j.get("redirect_url") or "",
            location_text=loc,
            country_codes=[country.upper()],
            employment_type=j.get("contract_time"),
            description_text=strip_html(j.get("description")),
            salary_min=None if predicted else j.get("salary_min"),
            salary_max=None if predicted else j.get("salary_max"),
            salary_text="Salary estimated by Adzuna (not stated by the employer)" if predicted and j.get("salary_min") else None,
            posted_at=j.get("created"),
            posted_at_provenance="adzuna.created" if j.get("created") else None,
            raw={"category": (j.get("category") or {}).get("label") if isinstance(j.get("category"), dict) else None},
        )


def _scrub(text: str, *secrets: str | None) -> str:
    for s in secrets:
        if s:
            text = text.replace(s, "***")
    return text
