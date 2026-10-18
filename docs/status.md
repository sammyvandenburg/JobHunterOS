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
| Background hunt with live progress (dashboard polls, jobs stored source by source) | yes | yes | yes, in a headless Chromium here | Same lock as the worker and CLI. |
| LinkedIn/Indeed alert import (HTML links kept, tracking removed, multipart) | yes | yes (synthetic alerts) | no real mailbox used | Posting dates stay unknown. |
| Matching, groups, explanations (rule-based) | yes | yes (candidate x job matrix: beginner, career changer, mid, senior; complete, borderline, decoy, empty and misleading postings) | n/a | Location that cannot be resolved goes to "Needs checking"; one selection feeds dashboard, CLI, digest and counts. |
| Claude explanations unattended (`claude -p`) | yes | mocked | one tiny live call to confirm flags and JSON shape | Off by default; budget-capped. |
| Claude explanations via `anthropic` SDK | yes | no | no | Needs `ANTHROPIC_API_KEY` and `--extra api`. |
| Dashboard (sidebar shell, dashboard, New jobs with search/filters/sort/pages, Favorites, Saved, Applied, Finished, job pages with notes, settings, health) | yes | yes (TestClient + Playwright: toggles, notes, mobile layout) | yes, in a headless Chromium here, desktop and 390px wide | Loopback only. |
| Fresh-install journey in a real browser (upload -> approve -> preferences -> background search -> favorite/save/notes -> Help me apply twice -> applied -> restart -> profile and country change move dashboard and digest together) | yes | yes (Playwright, synthetic sources) | yes, headless Chromium here | `tests/test_journey.py`; on macOS the same walk is a manual check. |
| Actions, pipeline, undo, honest counts | yes | yes | n/a | |
| Scheduler logic (DST, catch-up, pause/resume) and worker | yes | yes | worker loop run locally in tests (`tick`), not as an installed service | |
| Automatic hunting inside Hunter (starts with the dashboard, one lock with the worker) | yes | yes (unit + browser, incl. restart and a scheduled digest) | yes, in tests | `jobhunter/services/autorun.py` |
| Login task install/remove from the dashboard after explicit approval (launchd, systemd user, schtasks) | yes | yes (recorded command runner, all three systems) | no real `launchctl`/`systemctl`/`schtasks` run here | Needs a check on a real Mac/Windows machine. |
| Digest composition, allowlist, history, test email | yes | yes | no real email sent | |
| SMTP transport (advanced fallback) | yes | mocked | no | |
| Connect Gmail from the dashboard (PKCE, loopback redirect, scope check, suggested recipient, refresh, expiry → Needs attention, disconnect) | yes | yes (fake Google on localhost, HTTP + real browser) | no live Google account or OAuth client here | KubeCraft must publish the shared OAuth client and pass Google verification for `gmail.send`; see docs/email.md. |
| Gmail read sync, classification, linking, disconnect | yes | yes (fake API) | no | Needs a Google OAuth client; Testing-mode tokens expire after 7 days. |
| Manual email import | yes | yes | n/a | |
| Application preparation, drafts, outreach, follow-ups | yes | yes | n/a | Never submits or sends. |
| InterviewOS Markdown hand-off | yes | yes | n/a | Plain export; no integration. |
| Export / backup / delete | yes | yes | n/a | |
| Demo mode | yes | yes | n/a | Separate folder, labelled. |
| Cloud / always-on mode | no | — | — | Documented as not enabled. |
| Application submission / browser automation | no | — | — | Out of scope for v1 by design. |

Tests: `uv run pytest` runs the unit, integration, dashboard, and browser tests on synthetic fixtures with no network (the command prints the real count; 118 at the time of writing). GitHub Actions runs the same suite with locked dependencies on every push (`.github/workflows/ci.yml`). `tests/live` holds opt-in read-only live checks (`JOBHUNTER_LIVE=1`).
