"""Low-volume live checks. Run on purpose with: JOBHUNTER_LIVE=1 uv run pytest tests/live -q
They make a handful of read-only requests and are skipped otherwise."""

from __future__ import annotations

import os

import pytest

from jobhunter.services.http import SafeClient
from jobhunter.services.sources import get_adapter

pytestmark = pytest.mark.skipif(os.environ.get("JOBHUNTER_LIVE") != "1", reason="set JOBHUNTER_LIVE=1 to run live source checks")


@pytest.mark.parametrize("kind,sid,cfg", [
    ("greenhouse", "greenhouse:gitlab", {"token": "gitlab", "employer_name": "GitLab"}),
    ("lever", "lever:palantir", {"site": "palantir", "employer_name": "Palantir", "region": "us"}),
    ("ashby", "ashby:ashby", {"board": "ashby", "employer_name": "Ashby"}),
    ("remotive", "remotive:devops", {"categories": ["devops"]}),
    ("arbeitnow", "arbeitnow:eu", {"feed": "eu", "max_pages": 1}),
])
def test_live_source(kind, sid, cfg):
    with SafeClient() as client:
        res = get_adapter(kind).fetch(client, sid, cfg)
    assert res.status in {"ok", "partial"}, res.error
    assert res.postings, "live source returned no postings"
    p = res.postings[0]
    assert p.title and p.url.startswith("https://")


def test_live_adzuna_if_configured():
    if not (os.environ.get("ADZUNA_APP_ID") and os.environ.get("ADZUNA_APP_KEY")):
        pytest.skip("no Adzuna keys configured")
    with SafeClient() as client:
        res = get_adapter("adzuna").fetch(client, "adzuna:test", {"countries": ["gb"], "max_pages": 1})
    assert res.status in {"ok", "partial"} and res.postings
