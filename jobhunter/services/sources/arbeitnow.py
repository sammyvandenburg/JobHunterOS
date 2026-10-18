"""Arbeitnow Job Board API adapter (discovery feed, Europe-focused).

Docs: https://www.arbeitnow.com/blog/job-board-api
Endpoints: https://www.arbeitnow.com/api/job-board-api (Europe/Germany),
https://www.arbeitnow.co.uk/api/job-board-api (UK). Paginated with ?page=N;
``links.next`` is null on the last page. ``created_at`` is a unix timestamp.
"""

from __future__ import annotations

from jobhunter.models import FetchResult, RawPosting
from jobhunter.services.http import FetchError, SafeClient, UnsafeURL
from jobhunter.services.sources.base import SourceAdapter
from jobhunter.util import epoch_to_iso, strip_html

FEEDS = {"eu": "https://www.arbeitnow.com/api/job-board-api", "uk": "https://www.arbeitnow.co.uk/api/job-board-api"}
DEFAULT_MAX_PAGES = 4


class ArbeitnowAdapter(SourceAdapter):
    kind = "arbeitnow"
    employer_hosted = False

    def fetch(self, client: SafeClient, source_id: str, config: dict) -> FetchResult:
        feed = FEEDS.get((config.get("feed") or "eu").lower(), FEEDS["eu"])
        max_pages = int(config.get("max_pages") or DEFAULT_MAX_PAGES)
        postings: list[RawPosting] = []
        page = 1
        complete = True
        error = None
        while page <= max_pages:
            try:
                data = client.get(feed, params={"page": page}).json()
            except (FetchError, UnsafeURL) as exc:
                error = str(exc)
                complete = False
                break
            except ValueError:
                error = "unparseable response"
                complete = False
                break
            items = data.get("data") if isinstance(data, dict) else None
            if not isinstance(items, list):
                error = "unexpected shape"
                complete = False
                break
            for j in items:
                try:
                    postings.append(self._convert(source_id, j, feed))
                except Exception:
                    continue
            nxt = (data.get("links") or {}).get("next")
            if not nxt:
                break
            page += 1
        else:
            complete = False  # stopped at the page cap; the feed has more
        if error and not postings:
            return self.failed(source_id, f"Could not read Arbeitnow: {error}")
        return FetchResult(
            source_id=source_id,
            status="partial" if (error or not complete) else "ok",
            postings=postings,
            error=error,
            complete=complete,
            notes="Source: Arbeitnow (arbeitnow.com). Only the newest pages are read each hunt.",
        )

    @staticmethod
    def _convert(source_id: str, j: dict, feed: str) -> RawPosting:
        remote = bool(j.get("remote"))
        default_country = "GB" if "co.uk" in feed else None
        return RawPosting(
            source_id=source_id,
            external_id=str(j.get("slug")),
            title=str(j.get("title") or "").strip(),
            employer_name=j.get("company_name"),
            url=j.get("url") or "",
            location_text=j.get("location") or None,
            country_codes=[default_country] if default_country else [],
            remote_type="remote" if remote else "unknown",
            employment_type=", ".join(j.get("job_types") or []) or None,
            description_text=strip_html(j.get("description")),
            posted_at=epoch_to_iso(j.get("created_at")),
            posted_at_provenance="arbeitnow.created_at" if j.get("created_at") else None,
            raw={"tags": j.get("tags") or [], "feed": feed},
        )
