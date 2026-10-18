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
import plistlib
import shlex
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
        except (hunt.HuntInProgress, hunt.HuntNotAllowed) as exc:
            # Nothing ran (another hunt holds the lock, or setup is not finished). Leave the slot due so the next tick tries again.
            out["hunt"] = {"skipped": str(exc)}
        except Exception:
            # The hunt started and failed; run_hunt recorded that on its hunt_runs row.
            # Count the slot as used so a broken source does not retry every minute.
            schedule.mark_ran(db)
            raise
        else:
            out["ran_hunt"] = True
            out["hunt"] = {k: stats.get(k) for k in ("run_id", "status", "jobs_new", "jobs_relevant_new", "sources_failed")}
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
    argv = [uv, "run", "--project", str(repo), "jobhunter", "worker", "run"] if uv else [python, "-m", "jobhunter", "worker", "run"]
    cmd = shlex.join(argv)  # one shell-safe line; paths with spaces stay intact
    home = str(ctx.paths.home)
    logs = ctx.paths.logs_dir
    env_line = f'Environment="JOBHUNTER_HOME={_systemd_escape(home)}"'
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
    # launchd: a real plist built by plistlib (valid XML, argv as a list). Instructions stay outside the XML
    # because anything before the <?xml ...?> declaration makes launchctl reject the file.
    launchd_instructions = (
        "# Save ONLY the XML below as ~/Library/LaunchAgents/com.kubecraft.jobhunter.worker.plist, then:\n"
        "#   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.kubecraft.jobhunter.worker.plist\n"
        "# Runs while you are logged in and the Mac is awake.\n"
    )
    launchd = plistlib.dumps(
        {
            "Label": "com.kubecraft.jobhunter.worker",
            "ProgramArguments": argv,
            "WorkingDirectory": str(repo),
            "EnvironmentVariables": {"JOBHUNTER_HOME": home, "PATH": "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin"},
            "RunAtLoad": True,
            "KeepAlive": True,
            "StandardOutPath": str(logs / "worker.log"),
            "StandardErrorPath": str(logs / "worker.err.log"),
        },
        sort_keys=False,
    ).decode("utf-8")
    ps_args = " ".join(_win_arg(a) for a in argv[1:])
    windows = f"""# Windows (PowerShell, run as your user). Creates a task that starts the worker at logon.
# If you moved the data folder, tell Windows first (the task inherits your user environment):
#   [Environment]::SetEnvironmentVariable('JOBHUNTER_HOME', {_ps_str(home)}, 'User')
$action = New-ScheduledTaskAction -Execute {_ps_str(argv[0])} -Argument {_ps_str(ps_args)} -WorkingDirectory {_ps_str(str(repo))}
$trigger = New-ScheduledTaskTrigger -AtLogOn
Register-ScheduledTask -TaskName "JobHunterOS worker" -Action $action -Trigger $trigger -Description "Scheduled job hunts (runs while you are logged in)"
# Remove later with: Unregister-ScheduledTask -TaskName "JobHunterOS worker" -Confirm:$false
"""
    cron = f"""# Alternative without a long-running process: a cron entry that checks every 15 minutes.
# Add with `crontab -e`. The worker exits immediately when nothing is due.
*/15 * * * * cd {shlex.quote(str(repo))} && JOBHUNTER_HOME={shlex.quote(home)} {cmd} --once >> {shlex.quote(str(logs / "worker-cron.log"))} 2>&1
"""
    return {
        "platform": platform.system(),
        "command": cmd,
        "command_argv": argv,
        "systemd": systemd,
        "launchd": launchd,
        "launchd_instructions": launchd_instructions,
        "windows": windows,
        "cron": cron,
    }


def _systemd_escape(value: str) -> str:
    """Inside a double-quoted systemd Environment= assignment only backslash and double quote need escaping."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _ps_str(value: str) -> str:
    """A PowerShell single-quoted literal: only the quote itself is special (doubled)."""
    return "'" + value.replace("'", "''") + "'"


def _win_arg(value: str) -> str:
    """One argument inside a Windows command line: double-quote it when it has spaces."""
    if any(ch.isspace() for ch in value) or not value:
        return '"' + value.replace('"', '\\"') + '"'
    return value


def _which(name: str) -> str | None:
    import shutil

    return shutil.which(name)
