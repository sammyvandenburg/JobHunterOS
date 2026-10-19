# Nightly hunting in the student's own Claude account (proof setup)

Nothing here is operated by KubeCraft. The student's claude.ai account runs a scheduled Claude Code
cloud session (a Routine), with the student's own Gmail and Google Drive connectors. The repository
only provides the code and the procedure (`.claude/skills/jobhunter/cloud.md`).

## What the student needs
- Claude Pro, Max, Team or Enterprise (Routines and cloud sessions are not on the free plan).
- The Gmail and Google Drive connectors connected at https://claude.ai/customize/connectors with
  the account that should receive the digest and hold the state file.
- This repository reachable from their claude.ai GitHub access (a private fork is fine; it holds
  code only, never data).
- One local step first: on their computer, in Claude Code, `/jobhunter cloud sync` creates the private
  Drive folder (with their consent) and uploads the first state file from their approved profile.

## The routine
Create it at https://claude.ai/code/routines (or with `/schedule` in Claude Code):
- Repository: this one, default branch (or the branch KubeCraft tells students to use).
- Connectors: Google Drive and Gmail. Nothing else.
- Schedule: once a day, early morning in the student's timezone (minimum interval is hourly).
- Prompt:

```
You are Hunter. Do the "Nightly run" in .claude/skills/jobhunter/cloud.md, step by step, in this
repository. Use only the Google Drive and Gmail connectors as that file says. Send email only to the
address `uv run jobhunter digest prepare --json` prints, and only when its status is "ready".
Do not email anyone else, do not write anything outside the Drive folder "JobHunterOS", and finish
with the three-line report the file asks for.
```

## What each run does
restore state from Drive → hunt with the same Python code as on the laptop → prepare the digest →
Claude sends it through the student's Gmail → record the outcome → export the state back to Drive.
Two runs in a row never send the same digest: the email history travels in the state file.

## Costs and limits (checked 2026-10-19 against code.claude.com/docs/en/routines)
Routines draw on the student's subscription usage like an interactive session. Each run clones the
repository and spends a few minutes of tool calls; the hunt itself is deterministic Python and costs
no tokens beyond reading its short JSON output. Limits: minimum hourly schedule; 100 scheduled runs
per hour per account. Network: cloud sessions reach the public job boards (verified in this build).

## What is and is not verified
See the end of docs/release-checklist.md and the report in docs/status.md: the round trip, the
no-repeat rule and the dashboard-action survival are proven by `tests/test_cloud_agent.py`; the
connector steps are verified by hand in one cloud session; a real scheduled run needs the student's
explicit approval and a Routine on their account.
