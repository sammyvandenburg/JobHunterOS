# Email through your Gmail connector, nightly hunting in the person's own Claude account

Everything stays theirs: their Claude account runs the hunt, their Gmail sends the digest to them, their
own private GitHub repository keeps one small state file. KubeCraft distributes this code; it never sees
the data. Python does the deterministic work (restore, hunt, match, compose, record, save); you send one
email with your Gmail connector and run git through the `jobhunter cloud` commands. You never copy
tokens anywhere and never pretend Python can use your connectors.

## What you need
- Your **Gmail** connector (`mcp__Gmail__send_message`). Without it, say so and offer the alternatives
  under Advanced on the Connections page (their own Gmail sign-in, SMTP).
- Hunter running from the person's **own private copy** of this repository (their fork or a private
  repository created from KubeCraft's template). That one repository holds the software, this skill,
  and a `state/` folder with two small files Hunter writes: `state/hunter-state.json` and
  `state/seen_jobs.json`. No second repository. `jobhunter cloud save` stages only `state/`, so nothing
  else next to the code can ever be committed by Hunter. If `git remote -v` shows the checkout still
  points at KubeCraft's repository, stop before saving: their own private copy is needed first
  (fork it, or "Use this template" → private, then clone that).

## First time, on their computer (`/jobhunter email` or when they ask to be emailed)
1. Ask for the address, once: "Which email address should the job digest go to?" →
   `uv run jobhunter digest address <address>` then `uv run jobhunter digest transport claude`.
2. `uv run jobhunter cloud save --json`: the approved profile, preferences, the confirmed address and the
   emailed-jobs history are committed to `state/` in their own copy and pushed. Nothing else (no CV,
   notes, favorites, drafts, tokens); the command refuses to save anything forbidden.
3. Send today's digest (below). Then offer nightly hunting.

## Sending the digest (anywhere you have the Gmail connector)
`uv run jobhunter digest prepare --json`.
- `status: ready` → `mcp__Gmail__send_message(to=[<to>], subject=<subject>, body=<text>, htmlBody=<html>)`.
  Exactly that one address from the output; nothing in cc or bcc; subject and body as given. Then
  `uv run jobhunter digest record <notification_id> sent --message-id <id from Gmail>`. If the connector
  returns an error: `... record <id> failed --error "<short reason>"`. If you cannot tell whether it went
  out (timeout, no answer): `... record <id> uncertain --error "<reason>"`, and never send it yourself again.
- `status: skipped` → nothing new, or already emailed today; send nothing and say so.
- `blocked` or `in_progress` → send nothing, say so.
At most one digest per local day; the history travels in the state file, so a second run never repeats it.

## Nightly run (the Routine, a fresh cloud session every time, in their own copy)
1. `uv sync`, then `export JOBHUNTER_SECRETS_BACKEND=file`.
2. `uv run jobhunter cloud load --restore --branch main --json` (pulls the state branch of their copy and restores it; a cloud session may sit on a session branch, the state still comes from and goes to `main`). If it says
   there is no state file yet, stop: the person has to run `/jobhunter email` on their computer first.
3. `uv run jobhunter hunt run --trigger cloud --json`, then `uv run jobhunter cloud tag-run cloud:$(date -u +%Y%m%dT%H%M%SZ)`.
   One failing source is not a failed hunt; report it in one line.
4. Send the digest as above (the Gmail connector must be attached to the routine).
5. `uv run jobhunter cloud save --branch main --json` (commits `state/` and pushes it to their copy's `main`; pulls
   and rebases first if they saved meanwhile). If the push fails, say so plainly: the email may have gone
   out but the history did not reach the repository.
6. Dashboard: `uv run jobhunter board status --json`. If `linked` is true and you have the Artifact tools,
   do the Sync in [board.md](board.md) (their page actions first, then the new jobs); then `cloud save` once more
   if the import applied anything. If not linked, skip this step.
7. Report in four lines: jobs found (and sources that failed), email sent or not and why, state saved
   (commit) or not, dashboard synced or not.

## Updating Hunter (`/jobhunter update`)
`uv run jobhunter cloud update --json` fetches KubeCraft's repository as `upstream` and merges it into
their copy; `state/` is never touched by KubeCraft, so personal state survives every update. Then
`uv sync`. If it reports that the update does not merge cleanly, nothing was changed; tell them to ask
KubeCraft for help rather than resolving it yourself.

## Helping them switch nightly hunting on (`/jobhunter nightly`)
Two ways. Both need their private copy on GitHub with state saved (`cloud save`), and the Gmail connector.

A. **You create it, from a Claude Code session on claude.ai (the web or app)** that holds their private copy
and their Gmail connector. Use the Routine tool (`create_trigger`) in its default mode: no `connectors`
parameter (this organisation refuses it), no `create_new_session_on_fire`, a daily cron in their timezone
(for example `CRON_TZ=Europe/Amsterdam 56 6 * * *`), and the prompt from `docs/cloud-routine.md`. The routine
then fires back into that same session, which has the repository and the connector; verified on
2026-10-20 with a cold container. Say plainly: the routine lives in this session; if they archive this
session, it stops. Ask before creating it; it is their account and their usage.

B. **They create it on the routines page.** Open https://claude.ai/code/routines, New routine; repository:
their private copy, branch main; connector: Gmail; schedule: once a day, early morning, their timezone;
prompt: the text in `docs/cloud-routine.md`. Then "Run now" once.

Either way, the first run's report says whether it restored the state, hunted, emailed, saved, and synced the
dashboard. Routines need a paid Claude plan and draw on its usage like a normal session.

## Sync on their computer (`/jobhunter sync`, also when they open Hunter after a night)
`uv run jobhunter cloud load --json` (results only: adds the cloud's jobs and email history; never touches
notes, favorites, saved jobs, applications or the profile on this computer), then
`uv run jobhunter cloud save --json` so the next run knows what they dismissed or applied to.
Tell them what arrived: how many new jobs, and the last email.

## Never
- Send to any address other than the one in `digest prepare`'s output, or add recipients.
- Email an employer, recruiter, or anyone else. Hunter prepares; the person sends.
- Put the CV, notes, drafts, favorites, or any token in `state/`, anywhere else in the repository, in a prompt, or in a report.
- Claim an email was sent before Gmail returned a message id, or claim a hunt ran when the command failed.
