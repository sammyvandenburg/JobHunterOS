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
Create it **from the claude.ai routines page** (https://claude.ai/code/routines) or with `/schedule`
inside Claude Code, because that is where the connectors are attached. A routine created through the
remote-control API from a cloud session stores no connectors on this account (verified 2026-10-19:
"the connectors parameter is not available for this organization"), and a session without the Drive
and Gmail tools cannot restore state or send the digest. Settings:
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

## Size of the state file
Claude carries the file through its own context and connector calls, so it must stay small. The
exporter therefore writes only what the other side cannot re-derive (the emailed and excluded jobs with
their match outcome and a short snippet, the email history, run summaries, and the inputs), gzipped:
about 9 KB for a night with two feeds in the proof. A full 89-source night is expected to stay under
30 KB because the count of emailed jobs is capped per day. The first attempt, every relevant job with
its full text, was 7 MB raw and could not be relayed at all; that is the data-persistence risk to keep
in mind if the bundle ever grows (for example by carrying full descriptions again).

## What is and is not verified
Live proof on 2026-10-19, owner's account, synthetic "Sam Example" profile, private Drive folder
`JobHunterOS-proof`, two public feeds (Remotive, Arbeitnow):
- Run one (fresh data folder): restored the laptop's state from Drive (byte-identical round trip),
  hunted 1,212 postings (125 relevant), prepared a 15-job digest, sent it through the owner's Gmail
  connector to the owner's own address (Gmail message id recorded), recorded `sent`, exported and
  uploaded `hunter-state-…json.gz` (size verified on download).
- Run two (another fresh folder): restored run one's state (15 jobs, 1 notification, matches),
  hunted again, and sent nothing: "Already emailed today", with the 15 emailed jobs held back from
  pending regardless of the day rule.
- Laptop: imported the cloud results (15 jobs with the cloud's groups, the sent notification, the
  cloud runs), made a real dashboard favorite and note on a cloud-found job, exported (neither the
  note nor the favorite left the machine); run three restored that, hunted, skipped the email; the
  laptop imported run three and still had the favorite, the note, one notification, its own profile
  and documents.
- Scheduled run: a one-time Routine created through the remote-control API fired on time
  (06:43:28Z) in a fresh cloud session on the owner's account, on a new machine. That session had no
  repository checkout (`/workspace` empty) and no Drive or Gmail tools, because this creation path
  stores neither a source repository nor connectors on this account. Hunter stopped at step 1 and
  reported "nothing was run, sent, or saved", which is the right behaviour, and the Drive folder was
  unchanged. So the scheduled proof is blocked on how the routine is created, not on the code: the
  owner creates it on https://claude.ai/code/routines with this repository (branch
  `claude/cloud-agent-proof`) and the Google Drive and Gmail connectors attached, using the prompt
  above. That run is still outstanding.
The round trip, the no-repeat rule and the dashboard-action survival are also pinned by
`tests/test_cloud_agent.py`.
