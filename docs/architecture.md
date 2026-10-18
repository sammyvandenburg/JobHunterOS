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

## Data model (migration `0001_init.sql`)
settings, source_documents, profile_versions, profile_questions, preference_changes, employers, watch_sources, hunt_runs, source_runs, jobs, job_observations, matches, recommendations, applications, events, drafts, contacts, followups, email_accounts, email_messages, notifications, consents, feedback, schedule, worker_heartbeat, model_cache, model_calls, handoffs. Indexes on canonical keys, employers, dates, groups, dedupe keys. Later migrations are added as numbered SQL files and applied automatically.

## Counting rules
Found = distinct job rows (deduplicated), with "relevant" shown separately. Recommended = distinct jobs presented in the dashboard/digest (`recommendations`). Applied = jobs with an unreverted `applied` event (user or clear email evidence; manual + email dedupe to one). Contacted = unreverted `contacted` events (confirmed sends). Recruiter replies = unreverted non-automated `reply_received` events. Interviews = unreverted `interview` events that changed the pipeline. Lifetime by default.

## Matching policy (version in `config.POLICY_VERSION`)
Score = relevance (title family, direction) + skills coverage (job tools vs evidenced skills) + level fit (candidate level from merged professional months vs job seniority, with one-level stretches, entry→mid stretches, senior-only exclusions for no-experience candidates) + eligibility (countries, remote restriction, sponsorship: yes/no/unknown) + preferences (remote, salary with unit checks, confirmed feedback flags) + a small freshness bonus. Groups: Good ≥ 60, Worth a shot ≥ 42, Backup ≥ 28 (relevant titles only), otherwise excluded with the reason shown. Cache key = job content + profile version + preferences + policy version.

## Security boundaries
See docs/privacy.md. In short: loopback-only dashboard with host/origin/CSRF checks; recipient allowlist enforced in the transport; SSRF-safe fetcher; no file paths from the web; schema-validated model output; secrets in keyring or an encrypted file; untrusted content treated as data.
