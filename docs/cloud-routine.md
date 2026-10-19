# Nightly hunting in the student's own Claude account

Nothing here is operated by KubeCraft. The student's claude.ai account runs a scheduled Claude Code
cloud session (a Routine) with the student's own Gmail connector; a small private GitHub repository the
student owns keeps the state between runs. The KubeCraft repository holds code only.

## The student's path (what `/jobhunter` does)
1. Clone or open JobHunterOS in Claude Code, run `/jobhunter`, give the CV and the LinkedIn PDF, confirm
   the profile, answer the six basics. (Unchanged.)
2. `Find jobs now` → the existing engine and sources rank the matches. (Unchanged.)
3. "Shall I email you these?" → Hunter asks for the address once, then: a private `jobhunter-state`
   repository (Claude creates it when it has a GitHub tool that can; otherwise the student creates it at
   github.com/new and pastes the URL), `jobhunter cloud link`, `jobhunter cloud save`, and the digest goes
   out through Claude's own Gmail connector (`digest prepare` → send → `digest record`).
4. `/jobhunter nightly` → Hunter walks them through the routine (below).
5. Every morning the routine restores the state from their repository, hunts, emails once, saves.
6. On the laptop, `/jobhunter sync` (or opening Hunter) pulls the results into the dashboard; notes,
   favorites and applications stay local and are never overwritten.

## What is reused and what was replaced
Reused unchanged: onboarding, profile builder and evidence rules, matching engine, sources, dashboard,
notes, favorites, application tracker, the digest composer and outbox, the `digest prepare` /
`digest record` handoff, the "claude" transport, the state bundle (`cloud export` / `restore` / `import`)
and its privacy filter, the standalone Gmail OAuth, SMTP and local scheduler (all optional).
Replaced: the Google Drive relay (search, download, base64, upload through Claude's context) by three
commands over the student's own git repository: `cloud link`, `cloud load [--restore]`, `cloud save`.
The file is the same bundle, pretty-printed as `hunter-state.json`, plus `seen_jobs.json` (the emailed
job keys, the one thing the original tiny Hunter kept). Git moves bytes on disk, so nothing passes
through the model and size no longer matters; pushes rebase on a race.

## What the state repository holds
`hunter-state.json`: the approved profile facts and targets, preferences, three consents (readiness,
profile approval, the confirmed digest address), the keys of dismissed and applied jobs, the emailed
jobs with a snippet and their match outcome, cloud run summaries, the email history.
`seen_jobs.json`: emailed job keys with time and status. Never: CV or LinkedIn text, notes, drafts,
favorites, saved flags, application details, tokens. `cloud save` refuses a bundle with a forbidden section.

## The routine
Create it on https://claude.ai/code/routines (or with `/schedule` in Claude Code); that is where the
repository and the connector are attached. A routine created through the remote-control API from a
cloud session got neither on this account (verified 2026-10-19), so the agent gives the student these
settings instead of creating it:
- Repository: JobHunterOS (the branch KubeCraft tells students to use). The student's GitHub access in
  Claude must also cover their `jobhunter-state` repository; the routine attaches it with `add_repo`.
- Connector: Gmail. Nothing else.
- Schedule: once a day, early morning, the student's timezone (minimum interval is hourly).
- Prompt:

```
You are Hunter (JobHunterOS). Do the "Nightly run" in .claude/skills/jobhunter/cloud.md, step by step,
in this repository. My private state repository is <github-user>/jobhunter-state. Use only the Gmail
connector and the add_repo tool as that file says. Send email only to the address
`uv run jobhunter digest prepare --json` prints, and only when its status is "ready". Do not commit or
push anything except the state repository through `jobhunter cloud save`. Finish with the three-line
report the file asks for.
```

## Why JobSpy is not a default source
The earlier tiny Hunter searched LinkedIn with python-jobspy. JobSpy scrapes LinkedIn, Indeed, Glassdoor
and Google Jobs pages. LinkedIn's User Agreement and Indeed's terms forbid automated collection, which
is why this repository's rules say "No scraping of LinkedIn or Indeed" and docs/sources.md lists them as
import-only (alert emails, pasted text). Adding JobSpy would reverse that rule; it is a product decision,
not a technical one. The existing 89 employer boards and feeds found 125 relevant jobs for the synthetic
profile in the proof, so the default works without it.

## Costs and limits (checked 2026-10-19 against code.claude.com/docs/en/routines)
Routines draw on the student's subscription usage like an interactive session (Pro, Max, Team,
Enterprise). Each run clones the repository and spends a few minutes of tool calls; the hunt itself is
deterministic Python. Limits: minimum hourly schedule; 100 scheduled runs per hour per account.

## What is and is not verified
- `tests/test_cloud_repo.py`: two fresh cloud machines and the laptop share the state through a bare git
  repository: restore, hunt, one email, no repeat, dashboard favorite and note survive, a push race is
  resolved by rebase, nothing forbidden leaves the laptop.
- `tests/test_cloud_agent.py`: the bundle, the handoff outcomes, the once-per-day rule.
- Live, 2026-10-19, owner's account, synthetic profile: two independent cloud runs with the Drive relay,
  one real digest sent through the owner's Gmail connector and recorded, no repeat, dashboard action
  survived. (That relay is superseded by the repository store; the Gmail step is the same.)
- A one-time routine fired on time in a fresh cloud session but, created through the API, had no
  repository and no connectors; it stopped honestly. The scheduled run with the repository and the Gmail
  connector attached, created on the routines page, is the remaining proof.
