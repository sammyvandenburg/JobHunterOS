"""Ashby Public Job Posting API adapter (read-only).

Docs: https://developers.ashbyhq.com/docs/public-job-posting-api
Endpoint: GET https://api.ashbyhq.com/posting-api/job-board/{board}?includeCompensation=true
Fields used: id, title, department, team, employmentType, location,
secondaryLocations[].location/address, publishedAt, isListed, isRemote,
workplaceType, address.postalAddress.addressCountry, jobUrl, applyUrl,
descriptionPlain, compensation.compensationTierSummary.
"""

from __future__ import annotations

from jobhunter.models import FetchResult, RawPosting
from jobhunter.services.http import FetchError, SafeClient, UnsafeURL
from jobhunter.services.sources.base import SourceAdapter

BASE = "https://api.ashbyhq.com/posting-api/job-board"


def _structured_salary(comp: dict) -> dict:
    """The first salary component of the compensation tiers (includeCompensation=true): numbers, currency and
    interval as the employer entered them. Equity and bonus components are not pay and are left out."""
    for tier in comp.get("compensationTiers") or []:
        for c in (tier.get("components") or []) if isinstance(tier, dict) else []:
            if not isinstance(c, dict) or str(c.get("compensationType") or "").lower() != "salary":
                continue
            lo, hi = c.get("minValue"), c.get("maxValue")
            if lo is None and hi is None:
                continue
            interval = str(c.get("interval") or "").upper()
            period = "year" if "YEAR" in interval else "month" if "MONTH" in interval else "hour" if "HOUR" in interval else None
            cur = c.get("currencyCode")
            return {"min": lo, "max": hi, "currency": str(cur).upper() if isinstance(cur, str) and cur else None, "period": period}
    return {}


class AshbyAdapter(SourceAdapter):
    kind = "ashby"
    employer_hosted = True

    def fetch(self, client: SafeClient, source_id: str, config: dict) -> FetchResult:
        board = (config.get("board") or "").strip()
        if not board or not board.replace("-", "").replace("_", "").replace(".", "").isalnum():
            return self.failed(source_id, "Ashby job board name is missing or invalid.")
        try:
            data = client.get(f"{BASE}/{board}", params={"includeCompensation": "true"}).json()
        except (FetchError, UnsafeURL) as exc:
            return self.failed(source_id, f"Could not read Ashby board '{board}': {exc}", getattr(exc, "status", None))
        except ValueError:
            return self.failed(source_id, f"Ashby board '{board}' returned data Hunter could not parse.")
        jobs = data.get("jobs") if isinstance(data, dict) else None
        if not isinstance(jobs, list):
            return self.failed(source_id, f"Ashby board '{board}' returned an unexpected shape.")
        listed = [j for j in jobs if not (isinstance(j, dict) and j.get("isListed") is False)]
        postings, skipped = self.convert_all(source_id, listed, config)
        return self.result(source_id, postings, skipped, len(listed))

    @staticmethod
    def _convert(source_id: str, j: dict, config: dict) -> RawPosting:
        addr = ((j.get("address") or {}).get("postalAddress") or {}) if isinstance(j.get("address"), dict) else {}
        countries = []
        c = addr.get("addressCountry")
        if isinstance(c, str) and c.strip():
            countries.append(c.strip())
        secondary = []
        for s in j.get("secondaryLocations") or []:
            if isinstance(s, dict):
                secondary.append(s.get("location") or "")
                sc = ((s.get("address") or {}).get("postalAddress") or {}).get("addressCountry")
                if isinstance(sc, str) and sc.strip():
                    countries.append(sc.strip())
        wt = (j.get("workplaceType") or "").lower()
        remote = {"remote": "remote", "hybrid": "hybrid", "onsite": "onsite"}.get(wt, "remote" if j.get("isRemote") else "unknown")
        comp = j.get("compensation") or {}
        summary = (comp.get("compensationTierSummary") or comp.get("scrapeableCompensationSalarySummary")) if isinstance(comp, dict) else None
        structured = _structured_salary(comp) if isinstance(comp, dict) else {}
        loc = j.get("location") or ""
        if secondary:
            loc = ", ".join([loc] + [s for s in secondary if s]) if loc else ", ".join(s for s in secondary if s)
        return RawPosting(
            source_id=source_id,
            external_id=str(j.get("id")),
            title=str(j.get("title") or "").strip(),
            employer_name=config.get("employer_name"),
            url=j.get("jobUrl") or "",
            apply_url=j.get("applyUrl") or j.get("jobUrl"),
            location_text=loc or None,
            country_codes=[],  # names, not codes; normalization maps them
            remote_type=remote,  # type: ignore[arg-type]
            employment_type=j.get("employmentType"),
            description_text=j.get("descriptionPlain") or "",
            salary_text=summary,
            salary_min=structured.get("min"),
            salary_max=structured.get("max"),
            salary_currency=structured.get("currency"),
            salary_period=structured.get("period"),
            posted_at=j.get("publishedAt"),
            posted_at_provenance="ashby.publishedAt" if j.get("publishedAt") else None,
            is_employer_hosted=True,
            raw={"department": j.get("department"), "team": j.get("team"), "country_names": countries, "secondary": secondary},
        )
