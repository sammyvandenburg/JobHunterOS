# Automatic hunting: "How often should I look?" and "Should I email you?"

One place to set it: **Settings → Connections → How often should I look**. Pick *only when I press Find
jobs now*, *every day at …*, or every 2 / 4 / 6 hours; tick *Email me what you find (once a day)* if you
want a digest (needs Gmail connected and your address confirmed, see docs/email.md). Press **Save**.

From a terminal: `uv run jobhunter schedule set daily --time 08:00`, `schedule timezone Europe/Amsterdam`,
`schedule notify dashboard|daily_email|on_request`, `schedule automatic on|off`, `schedule status`.

A frequent hunt never creates frequent emails: "daily email" sends at most one digest per local day.

## What runs it
Two things can run the schedule. They share one lock, so only one acts at a time.

1. **Inside Hunter (no install).** While the dashboard is open, a background thread checks the schedule
   every minute and runs due hunts and digests. It starts by itself when a schedule is on, also after
   you restart Hunter. It stops when you close Hunter.
2. **When Hunter is closed (optional login task).** On the same page, tick *I allow Hunter to add a
   small task that starts when I log in* and press **Keep hunting when Hunter is closed**. Hunter then
   writes and enables, for your operating system:
   - macOS: a launchd agent, `~/Library/LaunchAgents/com.kubecraft.jobhunter.worker.plist`
     (`launchctl bootstrap gui/<uid> …`); runs while you are logged in and the Mac is awake.
   - Linux: a systemd **user** service, `~/.config/systemd/user/jobhunter-worker.service`
     (`systemctl --user enable --now jobhunter-worker`); `loginctl enable-linger` keeps it after logout.
   - Windows: a Task Scheduler task "JobHunterOS worker" at logon (`schtasks /Create … /SC ONLOGON`).
   The task runs `jobhunter worker run`, the same worker as before. **Turn off automatic hunting**
   removes it again; **Remove only the background task** keeps the in-Hunter schedule.
   Nothing is installed without that tick, in the dashboard or with
   `uv run jobhunter schedule automatic on --keep-running-when-closed`. `uv run jobhunter schedule
   install --print` still prints the templates (plus a cron line) if you prefer to review and install
   by hand.

The status line says which one is active: **Automatic hunting: running** (inside Hunter, or the login
task), **installed, waiting** (the task exists but is not running right now, for example before the
next login), **on, but nothing is running it** (open Hunter or install the task), or **off**.

## The worker, in detail
`uv run jobhunter worker run` is the long-running process (Ctrl+C stops it); `--once` does one due-check
and exits (for cron). It holds a file lock (one worker per data folder), writes a heartbeat the dashboard
shows, prevents overlapping hunts (hunt lock plus a database row with a heartbeat; a run without a
heartbeat for 20 minutes is marked aborted), computes the next run in your timezone with daylight-saving
handling (a daily 08:00 run stays at 08:00 local across the DST change), and catches up after downtime at
most once: a run missed by less than 36 hours runs once now; anything older is skipped, never replayed as
a backlog. Digests follow the same rule: one per day, no replay. The in-Hunter scheduler calls exactly
the same tick.

Closing the browser does not stop a running worker or the in-Hunter scheduler (the Hunter process
does). Turning the computer off, sleeping it, or logging out (depending on the task type) does.

## What scheduled runs may do
Read the configured sources, update local state, match against the approved profile, and send the
opted-in digest to the confirmed address. They never answer open questions, loosen must-have
preferences, change the schedule, or send employer-facing messages.

## Cloud / always-on
Not provided. Claude Code's session-scoped scheduled tasks stop when the session closes; cloud Routines
run in a fresh clone without access to your private state, secrets, or mail tokens. A cloud mode would
need all of those solved and is documented here as **not enabled**.

## What was tested here
The in-Hunter scheduler (start on boot, step aside for a separate worker, hunt and digest once, skip the
same day), the status words, and the login-task install/remove for all three systems run in the test
suite with a recorded command runner (`tests/test_autorun_service.py`); the full browser journey
including a restart and a scheduled digest is in `tests/test_email_journey.py`. The real `launchctl`,
`systemctl --user` and `schtasks` calls were not run on a real machine in this environment.
