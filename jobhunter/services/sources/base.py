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
