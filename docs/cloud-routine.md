# Nightly hunting in the student's own Claude account

Nothing here is operated by KubeCraft. The student's claude.ai account runs a scheduled Claude Code
cloud session (a Routine) with the student's own Gmail connector; a small private GitHub repository the
student owns keeps the state between runs. The KubeCraft repository holds code only.

## One private copy per student
The student's unit of ownership is one private GitHub repository: their copy of Hunter (fork of
KubeCraft's repository, or a private repository created from it as a template). It holds the software,
the `/jobhunter` skill, and a `state/` folder with the two small files Hunter writes. There is no second
repository and no KubeCraft service. KubeCraft's own repository never contains a state file: `state/`
there holds only a README, and its CI fails if a `state/*.json` is ever committed to it.

## The student's path
1. Open my Hunter: their private copy in Claude Code (local, or a cloud session on that repository).
2. Upload CV: `/jobhunter`, the CV and the LinkedIn PDF, confirm the profile.
3. Set preferences: the six basics.
4. Find jobs: the existing engine and sources.
5. Open dashboard: `uv run jobhunter dashboard` on their computer (see below for the cloud workspace).
6. Enable daily emails: "Shall I email you these?" → address once → `cloud save` commits `state/` to
   their copy → the digest goes out through Claude's Gmail connector → `/jobhunter nightly` gives the
   routine settings.
Every morning the routine restores the state from `state/`, hunts, emails once, and pushes `state/`
back to the copy's main branch. On the laptop, `/jobhunter sync` pulls the results into the dashboard;
notes, favorites and applications stay local and are never overwritten. `/jobhunter update` merges
KubeCraft's newer Hunter into the copy; `state/` is never touched by upstream, so it cannot conflict.

## What is reused and what was replaced
Reused unchanged: onboarding, profile builder and evidence rules, matching engine, sources, dashboard,
notes, favorites, application tracker, the digest composer and outbox, the `digest prepare` /
`digest record` handoff, the "claude" transport, the state bundle (`cloud export` / `restore` / `import`)
and its privacy filter, the standalone Gmail OAuth, SMTP and local scheduler (all optional).
Replaced: the Google Drive relay by `cloud save` / `cloud load` over the student's own copy (git moves
bytes on disk, nothing passes through the model); the separate state repository by the `state/` folder
of that same copy. `cloud save` stages only `state/`, so a CV or database left next to the code is never
committed by Hunter. `cloud update` is the distribution path.

## What `state/` holds
`hunter-state.json`: the approved profile facts and targets, preferences, three consents (readiness,
profile approval, the confirmed digest address), the keys of dismissed and applied jobs, the emailed
jobs with a snippet and their match outcome, cloud run summaries, the email history.
`seen_jobs.json`: emailed job keys with time and status. Never: CV or LinkedIn text, notes, drafts,
favorites, saved flags, application details, tokens, mailbox contents. `cloud save` refuses a bundle
with a forbidden section. The copy is private on GitHub; the profile facts in it are the student's own.

## Distributing updates
Fork or template, then `jobhunter cloud update`: it adds KubeCraft's repository as `upstream`, fetches,
and merges `upstream/main` into the student's main. Because KubeCraft's commits never touch `state/`,
the merge cannot conflict with personal state; a code conflict (only possible if the student edited the
code) is reported and aborted, never resolved blindly. GitHub's "Sync fork" button does the same for a
fork. Tested in `tests/test_cloud_repo.py`.

## The dashboard from a cloud workspace
The dashboard binds to 127.0.0.1 on purpose and has no login. A Claude Code cloud session can start it
(`uv run jobhunter dashboard`) and Claude can drive it with a browser inside the container, but the
session exposes no preview port or public URL to the student (checked 2026-10-19: no port-forwarding
or preview facility is documented or present in the environment, and the network proxy does not
support tunnels). So from the cloud workspace the dashboard is only reachable by Claude, not by the
student. Temporary access for the student means running it on their own computer (or Remote Control
to their computer); persistent hosting would need authentication the dashboard does not have and is a
separate decision, not part of this design.

## The routine
Two ways to create it, verified on this account:

1. **Hunter creates it** from a Claude Code session on claude.ai that holds the student's private copy
   and their Gmail connector, with the Routine tool in its default, self-bound mode (no `connectors`
   parameter, which this organisation refuses; no fresh session per run). The routine fires back into
   that session, which has the repository and the connectors. Verified 2026-10-20: a one-time routine
   created this way fired at 05:57 UTC into a cold container (new boot id, 23 s uptime), restored the state
   from the private repository's `main`, hunted 72 sources, sent one digest through the Gmail connector
   with none of the 15 previously emailed jobs, and saved the state (commit 38f4f9d). Limits: the routine
   lives in that session (archiving the session stops it); a fresh-session routine created through the
   API gets no repository and no connectors, so that mode is not used.
2. **The student creates it on https://claude.ai/code/routines**, where the repository and the connector are
   attached by the page itself.

Settings either way:
- Repository: the student's own private copy of Hunter, branch main.
- Connector: Gmail. Nothing else.
- Schedule: once a day, early morning, the student's timezone (minimum interval is hourly).
- Prompt:

```
You are Hunter (JobHunterOS). Do the "Nightly run" in .claude/skills/jobhunter/cloud.md, step by step,
in this repository, with `--branch main` on `cloud load` and `cloud save`. Use only the Gmail connector
and the Artifact tools. Send email only to the address `uv run jobhunter digest prepare --json` prints,
and only when its status is "ready". Commit and push nothing except what `uv run jobhunter cloud save`
does. If `uv run jobhunter board status --json` says the dashboard is linked, sync it as
.claude/skills/jobhunter/board.md describes. Finish with the four-line report the file asks for: state
restored (commit), hunt numbers, email sent (Gmail message id) or why not, state saved (commit) and
dashboard synced or not.
```

`cloud save` and `cloud load` take `--branch`; the routine uses `main` explicitly, so it does not
matter whether the cloud session's checkout sits on `main` or on a session branch: the state always
comes from and returns to the persistent branch (pushed as `HEAD:refs/heads/main`). If the account's
push policy refuses that branch on the first real run, the simplest fix is to set the routine's outcome
branch to `main` on the routines page; no second repository is needed.

Two guards protect the state: `cloud save` refuses when the checkout's origin is KubeCraft's shared
repository, and refuses when the origin is a public GitHub repository (an anonymous request to the
GitHub API tells a public repository from a private one without any token).

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
