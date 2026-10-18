# Architecture

One Python package (`jobhunter/`), one SQLite database, three front doors that share the same services:

```
Claude Code skill (/jobhunter)  ──┐
Dashboard (FastAPI, loopback)   ──┼──►  jobhunter CLI / services  ──►  SQLite (~/.jobhunteros)
Worker (jobhunter worker run)   ──┘             │
                                                ├── sources/ (Greenhouse, Lever, Ashby, Remotive, Arbeitnow, Adzuna, imports)
                                                ├── matching/ (deterministic scoring + validated explanations)
                                                ├── email/ (Gmail OAuth read/send, SMTP, classification, linking)
                                                └── model/ (claude -p or anthropic SDK, bounded, validated)
```

## Deterministic code owns
Fetching (`services/http.py`, SSRF-safe), adapters and the provider registry (`services/sources/`), normalization (`normalize.py`: countries, remote rules, salary units, sponsorship, relevance, seniority), dedupe (`dedupe.py`), persistence (`jobs.py`), freshness (`freshness.py`), hunts and discovery (`hunt.py`, `original_lookup.py`), matching (`matching/scoring.py`, `matching/engine.py`), actions and the pipeline (`actions.py`), counts (`stats.py`), consent (`consent.py`), preferences with history (`preferences.py`), scheduling and the worker (`schedule.py`, `worker.py`, `locks.py`), digests and transports (`notifications.py`, `email/transport.py`), Gmail (`email/gmail.py`, `classify.py`, `linking.py`), drafts (`drafts.py`), exports (`interviewos.py`, `datamgmt.py`), secrets (`secrets.py`), demo (`demo.py`), documents (`documents.py`), onboarding (`onboarding.py`), profile (`profile.py`).

## The model only
Interprets documents into facts (validated by `profile.validate_draft`), explains fit (validated by `engine.validate_explanations`), asks questions, drafts text (validated by `drafts.validate_draft`). Schemas live in `models.py`. In Claude Code the session model does this through CLI commands; unattended, `services/model/provider.py` runs `claude -p` with no tools and a budget, or the `anthropic` SDK.

## Data model (migrations `0001_init.sql`, `0002_notes_favorites.sql`)
settings, source_documents, profile_versions, profile_questions, preference_changes, employers, watch_sources, hunt_runs, source_runs, jobs, job_observations, matches, recommendations, applications, events, drafts, contacts, followups, email_accounts, email_messages, notifications, consents, feedback, schedule, worker_heartbeat, model_cache, model_calls, handoffs. Indexes on canonical keys, employers, dates, groups, dedupe keys. `0002` adds `job_notes` (private notes keyed by the stable job id, soft-deleted, never touched by rediscovery) and an index for the favorite/save toggles, which are `favorited`/`unfavorited` and `saved`/`unsaved` events (latest unreverted event wins). Later migrations are added as numbered SQL files and applied automatically; no migration ever deletes user rows.

## Counting rules
Found = distinct job rows (deduplicated), with "relevant" shown separately. Recommended = distinct jobs presented in the dashboard/digest (`recommendations`). Applied = jobs with an unreverted `applied` event (user or clear email evidence; manual + email dedupe to one). Contacted = unreverted `contacted` events (confirmed sends). Recruiter replies = unreverted non-automated `reply_received` events. Interviews = unreverted `interview` events that changed the pipeline. Lifetime by default.

## Matching policy (version in `config.POLICY_VERSION`)
One vocabulary (`matching/skills.py`) canonicalizes skills on both sides with whole-word matching, so "digital" is not Git and "laws" is not AWS; AKS implies Kubernetes but only partly covers a request for Azure (shown as "related"). Both the document parser and Claude's structured profile go through the same canonicalization.

Experience is derived from paid roles by what the work was (`profile.classify_role`, `derive_experience`): infrastructure roles, IT roles whose bullets show real infrastructure work, general IT support, software, and other careers are kept apart. Years in a different career never count. Level: senior needs years of real infrastructure roles with ownership or leadership; mid and junior follow relevant years; two years of adjacent IT work make a career changer junior rather than entry. The summary says what was counted and what was not, and "Fix it" produces a new approved revision that retracts or adds facts.

Relevance (`normalize.classify_relevance`): the title must name infrastructure work (an infrastructure noun), not just contain "platform" or "systems"; non-IT "systems engineer" titles and "platform" in a product sense are out; unusual titles whose descriptions clearly describe infrastructure work land in "uncertain" and stay visible.

Score = relevance + skills coverage (related tools count half) + level fit (one-level stretches shown with their gaps; senior roles excluded for people without paid infrastructure work) + location + preferences + a small freshness bonus. Groups: Good ≥ 60, Worth a shot ≥ 42, Backup ≥ 28, otherwise excluded with the reason shown. A Good match needs real text and real evidence: postings with almost no text, or candidates with no matching tools and no relevant paid work, are capped.

Location: the text beats a generic tag ("Remote worldwide" plus "must be based in the US" is US only, `normalize.residency_restriction`); a named office makes a "remote" tag hybrid; each job carries `location_confidence` (stated, inferred, conflict, worldwide, unknown). A clear restriction outside the person's countries excludes the job with the reason; vague or conflicting location goes to the "Needs checking" list (`match_group = 'uncertain'`), never into the main lists; worldwide sources stay useful. Preferences carry `work_modes` (which of remote, hybrid, on-site are acceptable at all) next to the remote preference. No visa questions are asked; sponsorship only matters when the person set it under More options.

Cache key = material job fields (full-text description hash, title, employer, location, salary, countries, remote rule, confidence, relevance, availability) + a fingerprint of the profile content + preferences + policy version, so a correction, a country change, or a policy bump re-scores without a new fetch (`engine.rematch_quietly` runs after preference and profile changes). Jobs reclassified as not infrastructure work keep their match row only as excluded. Observations compare every material field, so a provider changing the country, the apply link or the work mode updates the job even when the text is the same; a stale aggregator copy never reopens a job an employer board closed.

One selection for every surface: `views.list_jobs(view="new")` is what the dashboard, the CLI, the digest (`notifications.pending_jobs`) and the Recommended count (`stats.totals`) use: current approved profile and policy, current preferences, the three groups, the 14-day window, not closed, not dismissed, not applied (prepared jobs stay). Clicking the Recommended card opens exactly that set.

Dedupe (`dedupe.py`): tracking parameters are dropped from links but requisition-naming parameters are kept (`?id=100` and `?id=200` are two jobs); the same employer and title in two cities, or with two different work modes, are two jobs; same canonical link or same requisition id merges.

## Hunts in the background
"Find jobs now" starts the hunt in a background thread (`hunt.start_background`) and returns the run id at once. The run row carries a `progress_json` (stage, sources done, new jobs so far) that `hunt.progress` turns into one plain sentence; the dashboard polls `/api/hunt/<id>` every few seconds and reloads once when the run ends. Each source's jobs are stored as soon as that source answers, so the lists fill up during the search. The file lock and the running row still make overlapping hunts impossible across the dashboard, the CLI and the worker. After matching, the original-listing lookup takes the best matches first.

## Security boundaries
See docs/privacy.md. In short: loopback-only dashboard with host/origin/CSRF checks; recipient allowlist enforced in the transport; SSRF-safe fetcher; no file paths from the web; schema-validated model output; secrets in keyring or an encrypted file; untrusted content treated as data.
