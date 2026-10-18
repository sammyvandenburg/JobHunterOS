"""The scheduled-hunt worker: a plain long-running process.

Run it with ``jobhunter worker run``. It checks the schedule every minute,
runs a hunt when one is due (one catch-up at most after downtime), sends a
daily digest when that mode is on, and writes a heartbeat the dashboard
shows. ``--once`` runs a single due-check and exits, for cron-style use.

Nothing installs it as a system service automatically; ``jobhunter schedule
install --print`` prints the unit/plist/task for you to review and install.
"""

from __future__ import annotations

import os
import platform
import signal
import sys
import time
from pathlib import Path

from jobhunter.config import AppContext
from jobhunter.db import Database, connect
from jobhunter.services import hunt, notifications, schedule
from jobhunter.services.locks import FileLock, LockHeld
from jobhunter.util import hostname, now_iso

TICK_SECONDS = 60


def _beat(db: Database, status: str, message: str | None = None, started_at: str | None = None) -> None:
    db.execute(
        "INSERT INTO worker_heartbeat (id, pid, hostname, started_at, last_beat_at, status, message) VALUES (1, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET pid = excluded.pid, hostname = excluded.hostname, last_beat_at = excluded.last_beat_at, status = excluded.status, message = excluded.message"
        + (", started_at = excluded.started_at" if started_at else ""),
        (os.getpid(), hostname(), started_at or now_iso(), now_iso(), status, message),
    )


def tick(ctx: AppContext, db: Database) -> dict:
    """One scheduler decision. Safe to call from a cron job (``worker run --once``)."""
    decision = schedule.due_decision(db)
    out = {"ran_hunt": False, "sent_digest": False, "decision": decision}
    if decision["run"]:
        try:
            stats = hunt.run_hunt(ctx, db, trigger="scheduled")
            out["ran_hunt"] = True
            out["hunt"] = {k: stats.get(k) for k in ("run_id", "status", "jobs_new", "jobs_relevant_new", "sources_failed")}
        except hunt.HuntInProgress as exc:
            out["hunt"] = {"skipped": str(exc)}
        finally:
            schedule.mark_ran(db)
    if schedule.digest_due(db):
        try:
            res = notifications.send_digest(ctx, db)
            out["sent_digest"] = res.get("status") == "sent"
            out["digest"] = res
        except notifications.DigestError as exc:
            out["digest"] = {"status": "failed", "error": str(exc)}
        # Mark the attempt so a failing transport does not retry every minute all day.
        db.execute("UPDATE schedule SET last_digest_at = ? WHERE id = 1", (now_iso(),))
    return out


def run_forever(ctx: AppContext, log=print) -> int:
    db = connect(ctx.paths.db_path)
    lock = FileLock(ctx.paths.locks_dir / "worker.lock")
    try:
        lock.acquire()
    except LockHeld:
        log("Another worker is already running for this data directory. Exiting.")
        return 2
    stop = {"flag": False}

    def _handle(signum, frame):  # noqa: ARG001
        stop["flag"] = True

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _handle)
        except (ValueError, OSError):  # pragma: no cover - not main thread on some platforms
            pass
    started = now_iso()
    _beat(db, "idle", "worker started", started_at=started)
    log(f"Hunter worker started (pid {os.getpid()}). Data: {ctx.paths.root}. Press Ctrl+C to stop.")
    try:
        while not stop["flag"]:
            try:
                _beat(db, "running", "checking schedule")
                result = tick(ctx, db)
                msg = "hunt ran" if result["ran_hunt"] else result["decision"]["reason"]
                if result["sent_digest"]:
                    msg += "; digest sent"
                _beat(db, "idle", msg)
                if result["ran_hunt"] or result["sent_digest"]:
                    log(f"{now_iso()}: {msg}")
            except Exception as exc:  # keep the worker alive; record the problem
                _beat(db, "idle", f"error: {type(exc).__name__}: {exc}"[:300])
                log(f"{now_iso()}: worker error: {type(exc).__name__}: {exc}")
            for _ in range(TICK_SECONDS):
                if stop["flag"]:
                    break
                time.sleep(1)
    finally:
        _beat(db, "stopped", "worker stopped")
        lock.release()
        db.close()
        log("Hunter worker stopped.")
    return 0


def service_templates(ctx: AppContext) -> dict:
    """Printable, reviewable templates. Nothing is installed."""
    python = sys.executable
    uv = _which("uv")
    repo = Path(__file__).resolve().parents[2]
    cmd = f"{uv} run --project {repo} jobhunter worker run" if uv else f"{python} -m jobhunter worker run"
    env_line = f"Environment=JOBHUNTER_HOME={ctx.paths.home}"
    systemd = f"""# Save as ~/.config/systemd/user/jobhunter-worker.service, then:
#   systemctl --user daemon-reload && systemctl --user enable --now jobhunter-worker
#   loginctl enable-linger $USER   # keeps it running after you log out (machine must stay on)
[Unit]
Description=JobHunterOS worker (scheduled job hunts)
After=network-online.target

[Service]
Type=simple
WorkingDirectory={repo}
{env_line}
# Optional: Environment=JOBHUNTER_SECRETS_PASSPHRASE=...   (or use EnvironmentFile=%h/.jobhunteros/worker.env with mode 600)
ExecStart={cmd}
Restart=on-failure
RestartSec=30

[Install]
WantedBy=default.target
"""
    launchd = f"""<!-- Save as ~/Library/LaunchAgents/com.kubecraft.jobhunter.worker.plist, then:
       launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.kubecraft.jobhunter.worker.plist
     Runs while you are logged in and the Mac is awake. -->
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.kubecraft.jobhunter.worker</string>
  <key>ProgramArguments</key><array>{''.join(f'<string>{p}</string>' for p in cmd.split())}</array>
  <key>WorkingDirectory</key><string>{repo}</string>
  <key>EnvironmentVariables</key><dict><key>JOBHUNTER_HOME</key><string>{ctx.paths.home}</string><key>PATH</key><string>/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>{ctx.paths.logs_dir}/worker.log</string>
  <key>StandardErrorPath</key><string>{ctx.paths.logs_dir}/worker.err.log</string>
</dict></plist>
"""
    windows = f"""# Windows (PowerShell, run as your user). Creates a task that starts the worker at logon.
$action = New-ScheduledTaskAction -Execute "{uv or python}" -Argument "{'run --project ' + str(repo) + ' jobhunter worker run' if uv else '-m jobhunter worker run'}" -WorkingDirectory "{repo}"
$trigger = New-ScheduledTaskTrigger -AtLogOn
Register-ScheduledTask -TaskName "JobHunterOS worker" -Action $action -Trigger $trigger -Description "Scheduled job hunts (runs while you are logged in)"
# Remove later with: Unregister-ScheduledTask -TaskName "JobHunterOS worker" -Confirm:$false
"""
    cron = f"""# Alternative without a long-running process: a cron entry that checks every 15 minutes.
# Add with `crontab -e`. The worker exits immediately when nothing is due.
*/15 * * * * cd {repo} && JOBHUNTER_HOME={ctx.paths.home} {cmd} --once >> {ctx.paths.logs_dir}/worker-cron.log 2>&1
"""
    return {"platform": platform.system(), "command": cmd, "systemd": systemd, "launchd": launchd, "windows": windows, "cron": cron}


def _which(name: str) -> str | None:
    import shutil

    return shutil.which(name)
