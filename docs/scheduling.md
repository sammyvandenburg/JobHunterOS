# Scheduling: "How often should I look?" and "When should I tell you?"

Two independent settings:
- Hunt frequency: daily (suggested), every 2 / 4 / 6 hours, or only when asked.
- Notification mode: dashboard only (default), daily email, or email when asked. A frequent hunt never creates frequent emails; "daily email" sends at most one digest per local day.

Set them during setup, on the Connections page, or with `uv run jobhunter schedule set daily --time 08:00`, `schedule timezone Europe/Amsterdam`, `schedule notify dashboard|daily_email|on_request`. Then `schedule enable`, `pause`, `resume`, `disable`. `schedule show` prints the next planned run, the last run, and worker health.

## The worker
Enabling a schedule records intent; it does not start anything. Hunts happen only while the worker process runs:

```bash
uv run jobhunter worker run          # long-running; checks every minute; Ctrl+C stops it
uv run jobhunter worker run --once   # one due-check, then exit (for cron)
```

The worker holds a file lock (one worker per data folder), writes a heartbeat the dashboard shows, prevents overlapping hunts (hunt lock plus a database row with a heartbeat; a run without a heartbeat for 20 minutes is marked aborted), computes the next run in your timezone with proper daylight-saving handling (a daily 08:00 run stays at 08:00 local across the DST change), and catches up after downtime at most once: a run missed by less than 36 hours runs once now; anything older is skipped, never replayed as a backlog. Digests follow the same rule: one per day, no replay.

Closing the browser does not stop a running worker. Turning the computer off, sleeping it, or logging out (depending on the service type) does. Hunter says this plainly on the Connections page.

## Installing it as a service (you install it; Hunter only prints the template)
`uv run jobhunter schedule install --print` prints ready-to-review templates for your platform:
- Linux: a systemd **user** service (`~/.config/systemd/user/jobhunter-worker.service`; `loginctl enable-linger` keeps it running after logout; the machine must stay on).
- macOS: a launchd agent (`~/Library/LaunchAgents/com.kubecraft.jobhunter.worker.plist`; runs while you are logged in and the Mac is awake).
- Windows: a PowerShell `Register-ScheduledTask` command (at logon).
- Any Unix: a cron line calling `worker run --once` every 15 minutes (no long-running process).

The systemd path was the one exercised structurally in this build (unit file generation and the worker's tick/lock/heartbeat logic are tested); none of the service managers were installed on a real machine here, because installing an operating-system schedule needs your explicit go-ahead.

## What scheduled runs may do
Read the configured sources, update local state, match against the approved profile, and send the opted-in digest to the confirmed address. They never answer open questions, loosen must-have preferences, change the schedule, or send employer-facing messages.

## Cloud / always-on
Not provided. Claude Code's session-scoped scheduled tasks stop when the session closes and are not durable; a cloned repository in an ephemeral cloud session has no access to your private state, secrets, or mailbox tokens. A cloud mode would need all of those solved and is documented here as **not enabled**.
