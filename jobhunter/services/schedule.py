"""Schedule settings and next-run computation (timezone-aware, DST-safe).

The worker (``jobhunter worker run``) reads this table. Nothing here starts a
process; enabling a schedule only records intent and the next planned run.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from jobhunter.db import Database
from jobhunter.util import now, now_iso, parse_iso

FREQUENCIES = {"manual": None, "daily": 24, "every_2h": 2, "every_4h": 4, "every_6h": 6}
NOTIFY_MODES = {"dashboard", "daily_email", "on_request"}
CATCH_UP_GRACE_HOURS = 36  # a missed run older than this is skipped, not replayed


def get(db: Database) -> dict:
    return db.fetchone("SELECT * FROM schedule WHERE id = 1") or {}


def _tz(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name or "UTC")
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return ZoneInfo("UTC")


def compute_next_run(sched: dict, after: datetime | None = None) -> datetime | None:
    """Next run time in UTC, computed in the user's local timezone."""
    if not sched.get("enabled") or sched.get("paused") or sched.get("frequency") == "manual":
        return None
    tz = _tz(sched.get("timezone") or "UTC")
    ref = (after or now()).astimezone(tz)
    hours = FREQUENCIES.get(sched.get("frequency"))
    if hours is None:
        return None
    if sched["frequency"] == "daily":
        hh, mm = _parse_hhmm(sched.get("time_of_day") or "08:00")
        candidate = datetime.combine(ref.date(), time(hh, mm), tzinfo=tz)
        if candidate <= ref:
            candidate = datetime.combine(ref.date() + timedelta(days=1), time(hh, mm), tzinfo=tz)
        # Re-attach tz after date arithmetic so DST transitions resolve to wall-clock time.
        candidate = candidate.replace(tzinfo=None).replace(tzinfo=tz)
        return candidate.astimezone(ZoneInfo("UTC"))
    # Interval runs: align to the next multiple of N hours on the local clock.
    start_of_day = datetime.combine(ref.date(), time(0, 0), tzinfo=tz)
    slots = range(0, 24, hours)
    for h in slots:
        candidate = (start_of_day + timedelta(hours=h)).replace(tzinfo=None).replace(tzinfo=tz)
        if candidate > ref:
            return candidate.astimezone(ZoneInfo("UTC"))
    nxt = datetime.combine(ref.date() + timedelta(days=1), time(0, 0)).replace(tzinfo=tz)
    return nxt.astimezone(ZoneInfo("UTC"))


def _parse_hhmm(value: str) -> tuple[int, int]:
    try:
        hh, mm = value.split(":")
        return max(0, min(23, int(hh))), max(0, min(59, int(mm)))
    except (ValueError, AttributeError):
        return 8, 0


def _refresh(db: Database, sched: dict | None = None) -> dict:
    sched = sched or get(db)
    nxt = compute_next_run(sched)
    db.execute("UPDATE schedule SET next_run_at = ?, updated_at = ? WHERE id = 1", (nxt.isoformat() if nxt else None, now_iso()))
    return get(db)


def set_frequency(db: Database, frequency: str, time_of_day: str | None = None) -> dict:
    if frequency not in FREQUENCIES:
        raise ValueError(f"unknown frequency {frequency}")
    values = ["frequency = ?", "updated_at = ?"]
    params: list = [frequency, now_iso()]
    if time_of_day:
        _parse_hhmm(time_of_day)
        values.append("time_of_day = ?")
        params.append(time_of_day)
    if frequency == "manual":
        values.append("enabled = 0")
    db.execute(f"UPDATE schedule SET {', '.join(values)} WHERE id = 1", tuple(params))
    return _refresh(db)


def set_timezone(db: Database, tz: str) -> dict:
    ZoneInfo(tz)  # raises on unknown
    db.execute("UPDATE schedule SET timezone = ?, updated_at = ? WHERE id = 1", (tz, now_iso()))
    return _refresh(db)


def set_notify_mode(db: Database, mode: str, digest_time: str | None = None) -> dict:
    if mode not in NOTIFY_MODES:
        raise ValueError(f"unknown notify mode {mode}")
    db.execute("UPDATE schedule SET notify_mode = ?, updated_at = ? WHERE id = 1", (mode, now_iso()))
    if digest_time:
        _parse_hhmm(digest_time)
        db.execute("UPDATE schedule SET digest_time = ? WHERE id = 1", (digest_time,))
    return get(db)


def enable(db: Database) -> dict:
    sched = get(db)
    if sched.get("frequency") == "manual":
        raise ValueError("Choose how often to look first (daily, every 2/4/6 hours).")
    db.execute("UPDATE schedule SET enabled = 1, paused = 0, updated_at = ? WHERE id = 1", (now_iso(),))
    return _refresh(db)


def pause(db: Database) -> dict:
    db.execute("UPDATE schedule SET paused = 1, next_run_at = NULL, updated_at = ? WHERE id = 1", (now_iso(),))
    return get(db)


def resume(db: Database) -> dict:
    db.execute("UPDATE schedule SET paused = 0, updated_at = ? WHERE id = 1", (now_iso(),))
    return _refresh(db)


def disable(db: Database) -> dict:
    db.execute("UPDATE schedule SET enabled = 0, paused = 0, next_run_at = NULL, updated_at = ? WHERE id = 1", (now_iso(),))
    return get(db)


def due_decision(db: Database, at: datetime | None = None) -> dict:
    """Should the worker run now? Handles missed runs: at most one catch-up, no backlog replay."""
    sched = get(db)
    at = at or now()
    if not sched.get("enabled") or sched.get("paused") or sched.get("frequency") == "manual":
        return {"run": False, "reason": "schedule off or paused", "next_run_at": None}
    nxt = parse_iso(sched.get("next_run_at"))
    if nxt is None:
        sched = _refresh(db, sched)
        nxt = parse_iso(sched.get("next_run_at"))
        return {"run": False, "reason": "next run planned", "next_run_at": sched.get("next_run_at")}
    if at < nxt:
        return {"run": False, "reason": "not due yet", "next_run_at": nxt.isoformat()}
    missed_by = at - nxt
    if missed_by > timedelta(hours=CATCH_UP_GRACE_HOURS):
        new_next = compute_next_run(sched, after=at)
        db.execute("UPDATE schedule SET next_run_at = ?, updated_at = ? WHERE id = 1", (new_next.isoformat() if new_next else None, now_iso()))
        return {"run": False, "reason": f"missed run from {nxt.isoformat()} was too old; skipped it", "next_run_at": new_next.isoformat() if new_next else None}
    return {"run": True, "reason": "due" if missed_by < timedelta(minutes=5) else f"catching up one missed run from {nxt.isoformat()}", "next_run_at": nxt.isoformat()}


def mark_ran(db: Database, at: datetime | None = None) -> dict:
    at = at or now()
    sched = get(db)
    new_next = compute_next_run(sched, after=at)
    db.execute("UPDATE schedule SET last_run_at = ?, next_run_at = ?, updated_at = ? WHERE id = 1", (at.isoformat(), new_next.isoformat() if new_next else None, now_iso()))
    return get(db)


def digest_due(db: Database, at: datetime | None = None) -> bool:
    """Daily email mode: at most one digest per local day, after the configured time."""
    sched = get(db)
    if sched.get("notify_mode") != "daily_email":
        return False
    tz = _tz(sched.get("timezone") or "UTC")
    at_local = (at or now()).astimezone(tz)
    hh, mm = _parse_hhmm(sched.get("digest_time") or "08:30")
    if at_local.time() < time(hh, mm):
        return False
    last = parse_iso(sched.get("last_digest_at"))
    if last and last.astimezone(tz).date() == at_local.date():
        return False
    return True


def describe(db: Database) -> dict:
    sched = get(db)
    hb = db.fetchone("SELECT * FROM worker_heartbeat WHERE id = 1")
    worker = "not running"
    if hb and hb.get("last_beat_at"):
        last = parse_iso(hb["last_beat_at"])
        if last and (now() - last) < timedelta(minutes=3) and hb.get("status") != "stopped":
            worker = f"running (pid {hb.get('pid')} on {hb.get('hostname')})"
        else:
            worker = f"stopped (last seen {hb['last_beat_at']})"
    freq_words = {"manual": "only when asked", "daily": "daily", "every_2h": "every 2 hours", "every_4h": "every 4 hours", "every_6h": "every 6 hours"}
    notify_words = {"dashboard": "dashboard only", "daily_email": "daily email", "on_request": "email when asked"}
    return {
        **sched,
        "frequency_label": freq_words.get(sched.get("frequency"), sched.get("frequency")),
        "notify_label": notify_words.get(sched.get("notify_mode"), sched.get("notify_mode")),
        "worker": worker,
        "worker_heartbeat": hb,
        "note": "Closing the browser does not stop a running worker. Turning off or sleeping this computer does.",
    }
