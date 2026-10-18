# Status: what is implemented, fixture-tested, live-tested, or not available (2026-10-18)

| Capability | Implemented | Fixture-tested | Live-tested in this build | Notes |
|---|---|---|---|---|
| Onboarding, resume, readiness gate | yes | yes | n/a | Self-reported readiness; no course verification exists. |
| Document import (PDF/DOCX/MD/TXT), hashing, immutability | yes | yes (text/markdown; PDF/DOCX parsers exercised by code paths) | n/a | Scans reported as unreadable. |
| GitHub evidence (public repos) | yes | parser only | no | Unauthenticated GitHub API; 60 req/h limit applies. |
| Profile builder, evidence validation, approval, corrections | yes | yes | n/a | Rule-based builder; Claude refines in-session. |
| Greenhouse / Lever / Ashby adapters | yes | yes | yes (all 85 catalogue boards answered on 2026-10-18; Lever EU host not exercised with a real EU site) | Greenhouse EU host undocumented, not used. |
| Remotive / Arbeitnow discovery feeds | yes | yes | yes (one read each) | Terms honoured (attribution, delays, rate limits). |
| Adzuna | yes | yes | no (no keys) | Optional. |
| LinkedIn/Indeed via alert-email or pasted text | yes | yes | n/a | No scraping. |
| New-employer discovery and board promotion | yes | yes | partially (live feeds read; promotion verified with fixtures) | Bounded to 5 boards per hunt. |
| Original-listing lookup | yes | yes | no | Never invents URLs. |
| Dedupe, freshness, availability | yes | yes | n/a | |
| Matching, groups, explanations (rule-based) | yes | yes | n/a | |
| Claude explanations unattended (`claude -p`) | yes | mocked | one tiny live call to confirm flags and JSON shape | Off by default; budget-capped. |
| Claude explanations via `anthropic` SDK | yes | no | no | Needs `ANTHROPIC_API_KEY` and `--extra api`. |
| Dashboard (sidebar shell, dashboard, New jobs with search/filters/sort/pages, Favorites, Saved, Applied, Finished, job pages with notes, settings, health) | yes | yes (TestClient + Playwright: toggles, notes, mobile layout) | yes, in a headless Chromium here, desktop and 390px wide | Loopback only. |
| Actions, pipeline, undo, honest counts | yes | yes | n/a | |
| Scheduler logic (DST, catch-up, pause/resume) and worker | yes | yes | worker loop run locally in tests (`tick`), not as an installed service | Service templates printed, never installed. |
| Digest composition, allowlist, history | yes | yes | no real email sent | |
| SMTP transport | yes | mocked | no | |
| Gmail send (OAuth) | yes | mocked | no | Needs a Google OAuth client. |
| Gmail read sync, classification, linking, disconnect | yes | yes (fake API) | no | Needs a Google OAuth client; Testing-mode tokens expire after 7 days. |
| Manual email import | yes | yes | n/a | |
| Application preparation, drafts, outreach, follow-ups | yes | yes | n/a | Never submits or sends. |
| InterviewOS Markdown hand-off | yes | yes | n/a | Plain export; no integration. |
| Export / backup / delete | yes | yes | n/a | |
| Demo mode | yes | yes | n/a | Separate folder, labelled. |
| Cloud / always-on mode | no | — | — | Documented as not enabled. |
| Application submission / browser automation | no | — | — | Out of scope for v1 by design. |

Tests: `uv run pytest` runs 70+ unit, integration, dashboard, and browser tests on synthetic fixtures with no network. `tests/live` holds opt-in read-only live checks (`JOBHUNTER_LIVE=1`).
