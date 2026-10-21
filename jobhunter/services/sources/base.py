from __future__ import annotations

from jobhunter.models import FetchResult
from jobhunter.services.http import SafeClient


class SourceAdapter:
    kind = "base"
    employer_hosted = False  # True when postings come straight from the employer's ATS

    def fetch(self, client: SafeClient, source_id: str, config: dict) -> FetchResult:  # pragma: no cover - interface
        raise NotImplementedError

    @staticmethod
    def failed(source_id: str, error: str, status: int | None = None) -> FetchResult:
        return FetchResult(source_id=source_id, status="failed", error=error, http_status=status, complete=False)

    def convert_all(self, source_id: str, items: list, config: dict) -> tuple[list, list[str]]:
        """Convert every raw item, keeping the ids of the ones that could not be read. The caller turns a non-empty
        skipped list into a partial result: a feed Hunter only half read is never a complete snapshot."""
        postings, skipped = [], []
        for j in items:
            rid = str(j.get("id")) if isinstance(j, dict) and j.get("id") is not None else "?"
            try:
                p = self._convert(source_id, j, config)
                if not p.title.strip() or not p.external_id.strip() or p.external_id == "None":
                    raise ValueError("a posting without a title or an id cannot be read")
                postings.append(p)
            except Exception:
                skipped.append(rid)
        return postings, skipped

    @staticmethod
    def result(source_id: str, postings: list, skipped: list[str], total: int, complete: bool = True, status: int | None = 200) -> FetchResult:
        if skipped:
            shown = ", ".join(skipped[:5]) + (" …" if len(skipped) > 5 else "")
            return FetchResult(source_id=source_id, status="partial", postings=postings, error=f"{len(skipped)} of {total} postings could not be read (ids {shown}); the rest were used, nothing was closed.", complete=False, http_status=status, skipped_ids=skipped)
        return FetchResult(source_id=source_id, status="ok" if complete else "partial", postings=postings, complete=complete, http_status=status)
