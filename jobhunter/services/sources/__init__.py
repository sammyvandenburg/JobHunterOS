"""Source adapters. Each adapter is read-only and returns RawPostings."""

from __future__ import annotations

from jobhunter.services.sources.base import SourceAdapter
from jobhunter.services.sources.registry import PROVIDERS, provider_info


def get_adapter(kind: str) -> SourceAdapter:
    if kind == "greenhouse":
        from jobhunter.services.sources.greenhouse import GreenhouseAdapter

        return GreenhouseAdapter()
    if kind == "lever":
        from jobhunter.services.sources.lever import LeverAdapter

        return LeverAdapter()
    if kind == "ashby":
        from jobhunter.services.sources.ashby import AshbyAdapter

        return AshbyAdapter()
    if kind == "remotive":
        from jobhunter.services.sources.remotive import RemotiveAdapter

        return RemotiveAdapter()
    if kind == "arbeitnow":
        from jobhunter.services.sources.arbeitnow import ArbeitnowAdapter

        return ArbeitnowAdapter()
    if kind == "adzuna":
        from jobhunter.services.sources.adzuna import AdzunaAdapter

        return AdzunaAdapter()
    raise KeyError(f"no adapter for source kind '{kind}'")


__all__ = ["PROVIDERS", "SourceAdapter", "get_adapter", "provider_info"]
