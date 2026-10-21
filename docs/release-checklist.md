# Release checklist: what to verify on real machines before students get Hunter

Everything below runs in the test suite against a fake Google and a recorded command runner (see the
table at the end). None of it has been run against a live Google account or on a physical Mac or
Windows machine in this build. This is the list for the person who does that. Tick each line on the
real thing; if a step fails, the Troubleshooting doc says what the student would see.

## A. Google: the consent flow, live
Prerequisites: the Google Cloud project, consent screen and Desktop OAuth client from docs/email.md,
and the client JSON published at `https://jobhunter.kubecraft.nl/google-oauth-client.json` (or set
`JOBHUNTER_GOOGLE_CLIENT_ID` / `JOBHUNTER_GOOGLE_CLIENT_SECRET` on the test machine).

1. Fresh data folder (`JOBHUNTER_HOME` pointing at an empty directory), `uv run jobhunter dashboard`,
   finish setup with a real CV, open Settings → Connections. Expected: "Gmail not connected", one
   **Connect Gmail** button, SMTP only under Advanced.
2. Press Connect Gmail with a **personal Gmail account that is on the project's test-user list**
   (Testing status) or any account (In production). Expected: the "Taking you to Google" page flips
   to Google's sign-in within a second; Google's screen names only "Send email on your behalf".
   While the app is unverified, Google shows "Google hasn't verified this app": note the exact text
   for the student docs.
3. Approve. Expected: back in Hunter at Connections with "Gmail connected as you@gmail.com" and the
   question "Should I send your job emails there?". Nothing was sent (check Gmail's Sent folder).
4. Press **Yes, send them there**, then **Send test email**. Expected: the email arrives in that inbox
   from that same address within a minute; Email history shows `test · sent`.
5. Press Connect Gmail again and **cancel** on Google's screen. Expected: "Google sign-in was cancelled
   or refused. Nothing changed", still connected as before.
6. Token refresh: wait more than one hour (or set `expiry` in the secret store to the past, see
   `tests/test_email_setup.py::_expire_token`), press Send test email. Expected: arrives; no prompt.
7. Revocation: remove Hunter at https://myaccount.google.com/permissions, press Send test email.
   Expected: "Your Gmail connection stopped working (Google sign-in expired). Press Connect Gmail
   again", a **Needs attention** chip and item with a **Connect Gmail again** button; after
   reconnecting the warning is gone and a test email arrives.
8. Testing-status expiry (only in Testing): come back after 8 days. Expected: the same "stopped
   working" message, not a silent failure, and the scheduled digest shows up under Needs attention
   with "connect Gmail again first".
9. **Disconnect Gmail**. Expected: "Token deleted locally and revoked at Google", Hunter is gone from
   myaccount.google.com/permissions, Email history is still there, the address choice is kept.
10. Read-only (only if KubeCraft offers it): **Connect Gmail (read-only)** asks for "View your email
    messages and settings" only; a send connection and a read connection are two separate entries at
    myaccount.google.com; **Sync now** lists job-search mail; nothing is labelled, archived or read.
11. The daily digest, live: choose a plan with "Email me once a day at" a few minutes ahead, keep
    Hunter open, wait. Expected: exactly one email, after the chosen time, only if there are new jobs;
    the Connections page shows "Last email: …, sent". Change the time to earlier the same day:
    no second email that day.
12. Cross-site cookie check (any browser): after step 3, the dashboard still works without a
    re-login; the CSRF token changed (Google's redirect comes back without the SameSite=Strict
    cookie, which is expected). Open the browser's address bar history: no `csrf=` in any URL.

## B. macOS (Apple silicon and Intel if both are supported)
1. Install with `uv`, run `uv run jobhunter doctor`. Note the secret store line: Keychain expected.
2. Turn on a plan, tick the approval box, press **Keep hunting when Hunter is closed**. Expected:
   "Hunter now starts a background search task when you log in to this Mac"; the page shows
   "Background task: installed and running (…/Library/LaunchAgents/com.kubecraft.jobhunter.worker.plist)".
   `launchctl print gui/$(id -u)/com.kubecraft.jobhunter.worker` lists the agent; the status chip says
   "Automatic hunting: running" with "Running in the background".
3. Quit Hunter (Ctrl+C in the terminal). Expected: the chip, when you reopen Hunter, still says running
   in the background (the agent beats every minute); a due hunt runs while Hunter is closed
   (`uv run jobhunter schedule status` → last hunt time moves).
4. Keychain prompt: the first time the agent sends a digest, macOS may ask for Keychain access for
   `python`. Note whether it prompts and whether "Always allow" is needed; document it for students.
5. Sleep the Mac across a scheduled time, wake it. Expected: one catch-up hunt within a minute of
   waking, never a backlog.
6. Log out and in. Expected: the agent is back (RunAtLoad), heartbeat fresh within two minutes.
7. **Turn off automatic hunting**. Expected: the plist is gone, `launchctl print` says "Could not
   find service", the chip says off.
8. Logs: `~/.jobhunteros/logs/worker.log` and `worker.err.log` exist and are readable.
9. Safari and Chrome: the Connect Gmail flow (section A, steps 2 and 3) in both.

## C. Windows 10/11
1. Install with `uv` in PowerShell, `uv run jobhunter doctor`. Secret store: Credential Locker expected.
2. **Keep hunting when Hunter is closed**. Expected: "Windows now starts Hunter's background search
   when you log in"; `schtasks /Query /TN "JobHunterOS worker"` lists it as Running; the chip says
   running in the background. Check that the task's command line found `uv` (the PATH of the
   logged-in user); if not, the page shows "Windows did not accept the task" with the error.
3. Close the terminal that ran Hunter. Expected: the task keeps running (it is its own process).
4. Lock, sleep and wake, sign out and in: as in B.5 and B.6.
5. A due hunt while Hunter is closed updates "Last automatic look"; a due digest arrives.
6. **Turn off automatic hunting** removes the task (`schtasks /Query` fails with "cannot find").
7. Windows Defender SmartScreen or a corporate policy may block the task or Python; note what students
   would see.
8. Edge and Chrome: the Connect Gmail flow.

## D. Linux desktop (Ubuntu or Fedora, a normal user session)
1. `uv run jobhunter doctor`: Secret Service (GNOME Keyring or KWallet) expected; if the machine has
   none, the page says "file-plain … not encryption" and `JOBHUNTER_SECRETS_PASSPHRASE` is needed in
   the service's environment too (docs/privacy.md).
2. **Keep hunting when Hunter is closed**. Expected: "Hunter now runs a background search service while
   you are logged in"; `systemctl --user status jobhunter-worker` is active; chip says running in the
   background. On a machine without `systemctl --user` (no systemd user session, some servers,
   containers), the page must say "This Linux has no systemd user services. Keep Hunter open instead."
3. `loginctl enable-linger $USER` if the student wants it to keep running after logout; document.
4. Sleep/wake, log out/in, turn off: as in B.
5. Firefox and Chromium: the Connect Gmail flow.

## E. Every platform
- Restart Hunter with a plan on: the Connections page says "Automatic hunting: running" within a
  minute and shows the last automatic look; nothing asks for Gmail again.
- Run the dashboard and the login task at the same time: the chip says "Running in the background",
  `uv run jobhunter schedule status` shows one heartbeat owner, and a due hunt happens once (one new
  `hunt_runs` row).
- Kill the worker process while a hunt is running (kill -9 / Task Manager): within 20 minutes the
  run is marked aborted and the next due slot runs normally.
- Disconnect the network, let a scheduled hunt run: sources show "couldn't check", no jobs are
  closed, no email is sent for nothing.
- Make the digest fail (disconnect Gmail during the day): Needs attention shows "could not be sent"
  with **Send again** and **Dismiss**; after reconnecting, Hunter retries by itself within about 30
  minutes, once, and the Email history shows a single `digest · sent`.

## What is covered by tests versus live-verified

| Area | Automated here | Live-verified in this build |
|---|---|---|
| Connect Gmail flow (PKCE, loopback redirect, one-time state, scope checks, cancel, replay, cross-site callback without cookie) | yes, fake Google, HTTP layer and real browser | no (no live OAuth client) |
| Token refresh, refusal → Needs attention → reconnect; disconnect and revoke | yes, fake Google | no |
| Test email and digest through the outbox and allowlist | yes, fake Gmail API | no real email sent |
| Hunt frequency vs one email a day; status words from heartbeat, task state and last run | yes | partly (status logic only in tests) |
| Failed digest: bounded automatic retry, Send again, Dismiss, superseding | yes | no |
| In-app scheduler and a real worker process at the same time; shutdown; restart; missed runs; stale run reaped | yes (real subprocess for the worker) | no |
| Login task install and removal | yes, with a recorded command runner, for launchd, systemd user, schtasks | no (`launchctl`, `systemctl --user`, `schtasks` never executed on a real machine) |
| Browsers | headless Chromium | Safari, Edge, Firefox not tried |
| Google consent-screen text, verification warning, Keychain/Credential Locker prompts | not testable here | no |

## F. The personal dashboard and the nightly routine (a Claude account with the Gmail connector)
- [ ] From a fresh private copy, `/jobhunter` through setup; after the basics Hunter runs a hunt, publishes the dashboard and gives the link without being asked.
- [ ] The link opens on a phone and on a laptop, signed in; signed out it shows "Sign in to see your jobs"; another account sees no jobs.
- [ ] Favorite, Save, Mark applied, Not interested and a note each show "Saved. Hunter reads it on its next run"; closing and reopening keeps them.
- [ ] `/jobhunter hunt` (or the nightly run) applies them: `board import` reports the changes, the applied job leaves Recommended, the note is on the job in `jobs show`.
- [ ] A second hunt shows new jobs on the same page; no second page is created (`board status` keeps one link).
- [ ] Nightly: the routine's report names the restored commit, the hunt numbers, the Gmail message id, the saved commit and "dashboard synced"; the next morning's email repeats none of the jobs from the day before.
- [ ] After `cloud update` to a newer Hunter, the dashboard still opens and a sync still works (export rematches with the new rules).

## G. Release candidate, student update and rollback
A release is one commit on KubeCraft's shared repository that students' copies update to. Nothing else changes
hands: no student data ever flows the other way.

Before KubeCraft publishes a candidate commit:
- [ ] `uv run ruff check` and `uv run pytest` are clean on the candidate (the command prints the real counts).
- [ ] `docs/status.md` names the candidate's date and what was live-tested.
- [ ] The repository's visibility and licence are unchanged (`jobhunter cloud save` refuses a public or shared
      repository as a state destination; the shared repository itself holds no `state/` content: `state/README.md` only).
- [ ] If the KubeCraft mark changed, `brand/KubeCraft-Cog-3.png` is the supplied file and `tests/test_branding.py` passes.
- [ ] Tag the candidate (`git tag -a v<date> -m "..."`) after the checks, push the tag; students update to `main`.

A student updates (Hunter does this on "update Hunter"): `uv run jobhunter cloud update --json`.
- A copy that shares history with KubeCraft's repository merges; a copy made with "Use this template" gets the
  changes since its starting version as a patch (the starting commit is found by matching the copy's first
  commit tree, or read from `.jobhunter/upstream.json`, which every update writes).
- `state/`, `.env`, `.jobhunter/upstream.json` and the paths in `.jobhunter/protected` are never touched.
- Refused while the copy has unsaved changes outside `state/`; a backup tag `hunter-update-backup-<time>` is set
  first; a change that does not apply cleanly changes nothing and says so.
- Then `uv sync`. Hunter's database migrates itself on the next command (migrations are additive and idempotent).

Rollback: `uv run jobhunter cloud update --rollback --json` returns the copy to the backup tag (refused while
there are unsaved changes; `jobhunter cloud save` first). The database is not rolled back: migrations only add
tables and columns, and an older Hunter ignores columns it does not know.

Verified in this build: `tests/test_update_template.py` (template copy: patch, protected paths, backup tag,
rollback, conflict, dirty tree) and `tests/test_cloud_repo.py` (shared history: merge). Not yet run against a real
GitHub template-created repository; that is the first thing to do on the pilot.
