# Email through your Gmail connector, nightly hunting in the person's own Claude account

Everything stays theirs: their Claude account runs the hunt, their Gmail sends the digest to them, their
own private GitHub repository keeps one small state file. KubeCraft distributes this code; it never sees
the data. Python does the deterministic work (restore, hunt, match, compose, record, save); you send one
email with your Gmail connector and run git through the `jobhunter cloud` commands. You never copy
tokens anywhere and never pretend Python can use your connectors.

## What you need
- Your **Gmail** connector (`mcp__Gmail__send_message`). Without it, say so and offer the alternatives
  under Advanced on the Connections page (their own Gmail sign-in, SMTP).
- The person's **private state repository**: a small GitHub repository they own, for example
  `<their-github-user>/jobhunter-state`, holding `hunter-state.json` and `seen_jobs.json`. Nothing else
  goes there. Never use the KubeCraft repository for it.

## First time, on their computer (`/jobhunter email` or when they ask to be emailed)
1. Ask for the address, once: "Which email address should the job digest go to?" →
   `uv run jobhunter digest address <address>` then `uv run jobhunter digest transport claude`.
2. The state repository. If you have a GitHub tool that creates repositories, create a private one named
   `jobhunter-state` (with a README) and tell them. Otherwise say: "Create a private repository called
   `jobhunter-state` at https://github.com/new (tick 'Add a README'), then give me its URL." Then
   `git clone <url> ~/.jobhunteros/state` and `uv run jobhunter cloud link --repo ~/.jobhunteros/state`.
3. `uv run jobhunter cloud save --json`: the approved profile, preferences, the confirmed address and the
   emailed-jobs history go into their repository and get pushed. Nothing else (no CV, notes, favorites,
   drafts, tokens); the command refuses to save anything forbidden.
4. Send today's digest (below). Then offer nightly hunting.

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

## Nightly run (the Routine, a fresh cloud session every time)
1. In the repository checkout: `uv sync`, then `export JOBHUNTER_SECRETS_BACKEND=file`.
2. Attach and clone the person's state repository: `mcp__claude-code-remote__add_repo(owner=<user>,
   repo=jobhunter-state, access="push")` and run the clone command its result gives (into
   `~/jobhunter-state`). If the tool is missing or refuses, stop: say the routine needs GitHub access to
   that repository.
3. `uv run jobhunter cloud load --repo ~/jobhunter-state --restore --json`. If it says there is no
   state file yet, stop: the person has to run `/jobhunter email` on their computer first.
4. `uv run jobhunter hunt run --trigger cloud --json`, then `uv run jobhunter cloud tag-run cloud:$(date -u +%Y%m%dT%H%M%SZ)`.
   One failing source is not a failed hunt; report it in one line.
5. Send the digest as above (the Gmail connector must be attached to the routine).
6. `uv run jobhunter cloud save --repo ~/jobhunter-state --json` (commits and pushes; pulls and rebases
   first if the person saved meanwhile). If the push fails, say so plainly: the email may have gone out
   but the history did not reach the repository.
7. Report in three lines: jobs found (and sources that failed), email sent or not and why, state saved
   (commit) or not.

## Helping them switch nightly hunting on (`/jobhunter nightly`)
You cannot create the routine for them from inside this session; it needs their claude.ai account page.
Say, in this order, and offer to show it again:
1. Open https://claude.ai/code/routines and press New routine.
2. Repository: this JobHunterOS repository (the branch KubeCraft told them to use). Connector: Gmail.
   GitHub access must include their `jobhunter-state` repository (same GitHub app installation).
3. Schedule: once a day, early morning, their timezone.
4. Prompt: the text in `docs/cloud-routine.md` under "Prompt" (show it to them to paste).
5. Press "Run now" once; the run's report says whether it restored the state, hunted, emailed, and saved.
Routines need a paid Claude plan and draw on its usage like a normal session.

## Sync on their computer (`/jobhunter sync`, also when they open Hunter after a night)
`uv run jobhunter cloud load --json` (results only: adds the cloud's jobs and email history; never touches
notes, favorites, saved jobs, applications or the profile on this computer), then
`uv run jobhunter cloud save --json` so the next run knows what they dismissed or applied to.
Tell them what arrived: how many new jobs, and the last email.

## Never
- Send to any address other than the one in `digest prepare`'s output, or add recipients.
- Email an employer, recruiter, or anyone else. Hunter prepares; the person sends.
- Put the CV, notes, drafts, favorites, or any token in the state repository, in a prompt, or in a report.
- Claim an email was sent before Gmail returned a message id, or claim a hunt ran when the command failed.
