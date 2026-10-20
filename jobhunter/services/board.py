"""The personal dashboard: a private Claude Artifact page with a small database.

Hunter (Python) never talks to the artifact. Claude does, with its Artifact tools, acting as the
student. This module produces the documents Claude writes (`export`) and applies the actions the page
recorded (`import_actions`). Both sides are plain JSON files in a folder, so the hand-off is testable
without Claude, and nothing here needs a token.

Layout in the artifact database (all private to the student: the page is published with
owner-only read and write rules):
  meta/profile    approved profile summary and preferences. Never the CV text, never a name.
  meta/run        the last hunt, the recommended keys in order, counts, how many shards there are.
  board/jobs-N    shards of up to SHARD_SIZE jobs: posting, match, and the student's state.
                  Rewritten whole on every sync, so nothing has to be tracked or repaired.
  actions/<key>   written by the page when the student acts (favorite, save, dismiss, applied, note).
                  Hunter applies each one once (by its `rev`) and tells the page so through the shard.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import timedelta
from pathlib import Path

from jobhunter.config import POLICY_VERSION, AppContext
from jobhunter.db import Database
from jobhunter.services import actions, hunt, notes, views
from jobhunter.services import preferences as prefs_svc
from jobhunter.services import profile as profile_svc
from jobhunter.services.normalize import country_names
from jobhunter.util import now, now_iso, truncate

SCHEMA = "hunter-board/1"
URL_SETTING = "board.url"
SHARD_SIZE = 30
RECOMMENDED_LIMIT = 150
EXTRA_LIST_LIMIT = 40  # "Older" and "Needs checking" lists on the page
DESCRIPTION_CHARS = 3000
URL_RE = re.compile(r"^https://claude\.ai/(?:code/)?artifact/[A-Za-z0-9_-]{6,}/?$")
ACTED_VIEWS = ("favorites", "saved", "applied", "interviews", "finished", "dismissed")
REMOTE_WORDS = {"required": "Remote only", "preferred": "Remote preferred", "no_preference": "Remote, hybrid or office", "unset": "Not set"}


class BoardError(Exception):
    pass


def job_key(canonical_key: str) -> str:
    """Stable, opaque id for a job across machines: the same job gets the same key on the laptop and in the cloud."""
    return "j" + hashlib.sha1((canonical_key or "").encode("utf-8")).hexdigest()[:20]


def page_path() -> Path:
    """The page Claude publishes once per student. It ships with Hunter, so every student gets the same dashboard."""
    return Path(__file__).resolve().parents[1] / "board" / "hunter-board.html"


# ---------------------------------------------------------------------------
# Link and status
# ---------------------------------------------------------------------------
def link(db: Database, url: str) -> dict:
    url = (url or "").strip()
    if not URL_RE.match(url):
        raise BoardError("That is not a Claude artifact link. It looks like https://claude.ai/artifact/<id>.")
    url = url.rstrip("/")
    previous = db.get_setting(URL_SETTING)
    with db.tx():
        db.set_setting(URL_SETTING, url)
        if previous and previous != url:
            # A new page starts with an empty database: nothing on it has been applied yet.
            db.execute("DELETE FROM board_sync")
    return status(db)


def unlink(db: Database) -> dict:
    with db.tx():
        db.delete_setting(URL_SETTING)
        db.execute("DELETE FROM board_sync")
    return status(db)


def stale_reason(db: Database) -> str | None:
    """Why the page's actions must be read before Hunter recommends again, or None when they are current.
    Current means: read at least once, and after Hunter last wrote to the page. Between a write and the
    next read the student may have pressed Applied or Not interested on a job Hunter would otherwise show
    or email again."""
    if not db.get_setting(URL_SETTING):
        return None
    imp = db.get_setting("board.last_import") or {}
    exp = db.get_setting("board.last_export") or {}
    if not imp:
        return "Your dashboard is linked but its actions have never been read. Read them first: list the `actions` collection, then `jobhunter board import --in <folder>`."
    if exp and _is_before(imp, exp):
        return f"Your dashboard's actions have not been read since Hunter last wrote to it ({exp.get('at', '')[:16]}). Read them first: list the `actions` collection, then `jobhunter board import --in <folder>`."
    return None


def _is_before(a: dict, b: dict) -> bool:
    """Was record `a` made before record `b`? By sequence number when both have one (clock-proof), else by time."""
    if a.get("seq") is not None and b.get("seq") is not None:
        return int(a["seq"]) < int(b["seq"])
    return (a.get("at") or "") < (b.get("at") or "")


def status(db: Database) -> dict:
    url = db.get_setting(URL_SETTING)
    synced = db.fetchone("SELECT COUNT(*) AS n FROM board_sync WHERE synced_rev > 0")["n"]
    return {
        "linked": bool(url),
        "url": url,
        "page": str(page_path()),
        "last_export": db.get_setting("board.last_export"),
        "last_import": db.get_setting("board.last_import"),
        "actions_applied": synced,
        "actions_stale": stale_reason(db),
        "note": None if url else "No dashboard yet. Claude publishes the page once and links it with `jobhunter board link <url>`.",
    }


# ---------------------------------------------------------------------------
# Export: what the page shows
# ---------------------------------------------------------------------------
def _selected(db: Database, limit: int) -> tuple[list[dict], list[str], dict[int, str]]:
    """Recommended jobs first (best match first), then everything the student acted on, then the two
    side lists the page keeps out of the shortlist: older postings and jobs whose location needs checking."""
    items: dict[int, dict] = {}
    lists: dict[int, str] = {}
    recommended = views.current_recommended(db, limit=limit)
    for it in recommended:
        items[it["id"]] = it
        lists[it["id"]] = "recommended"
    for view in ACTED_VIEWS:
        for it in views.list_jobs(db, view=view, per_page=500, sort="added")["items"]:
            if it["id"] not in items:
                items[it["id"]] = it
                lists[it["id"]] = "acted"
    for view in ("old", "uncertain"):
        for it in views.list_jobs(db, view=view, per_page=EXTRA_LIST_LIMIT)["items"]:
            if it["id"] not in items:
                items[it["id"]] = it
                lists[it["id"]] = view
    keys = [job_key(_canonical(db, it["id"])) for it in recommended]
    return list(items.values()), keys, lists


def _canonical(db: Database, job_id: int) -> str:
    row = db.fetchone("SELECT canonical_key FROM jobs WHERE id = ?", (job_id,))
    return row["canonical_key"] if row else str(job_id)


def _doc(db: Database, it: dict, synced: dict[str, dict], recommended_keys: set[str], which: str = "recommended") -> dict:
    row = db.fetchone("SELECT canonical_key, description_text, country_codes_json, employment_type FROM jobs WHERE id = ?", (it["id"],))
    key = job_key(row["canonical_key"])
    note = notes.latest_for_job(db, it["id"])
    return {
        "key": key,
        "list": which,
        "title": it["title"],
        "employer": it["employer"],
        "location": it["location"],
        "remote_type": it.get("remote_type"),
        "remote_label": it.get("remote_type_label") or "",
        "remote_restriction": it.get("remote_restriction"),
        "salary": it.get("salary"),
        "posted_at": it.get("posted_at"),
        "first_seen_at": it.get("first_seen_at"),
        "freshness_label": it.get("freshness_label"),
        "verification": it.get("verification"),
        "countries": json.loads(row.get("country_codes_json") or "[]"),
        "employment_type": row.get("employment_type"),
        "links": it.get("links") or {},
        "description": truncate(row.get("description_text") or "", DESCRIPTION_CHARS),
        "group": it.get("group"),
        "group_label": it.get("group_label"),
        "explanation": it.get("explanation"),
        "explanation_label": it.get("explanation_label"),
        "reasons": [truncate(r.get("text") or "", 220) for r in (it.get("reasons") or [])[:3] if r.get("text")],
        "gaps": [truncate(g if isinstance(g, str) else str(g), 220) for g in (it.get("gaps") or [])[:3]],
        "next_step": it.get("next_step"),
        "why": _why_line(it),
        "check": _check_line(it),
        "recommended": key in recommended_keys,
        "state": {
            "favorited": bool(it.get("favorited")),
            "saved": bool(it.get("saved")),
            "dismissed": bool(it.get("dismissed")),
            "stage": it.get("stage"),
            "stage_label": it.get("stage_label"),
            "applied_at": it.get("applied_at"),
            "note": note["body"] if note else None,
            "notes_count": it.get("notes_count") or 0,
            "synced_rev": int((synced.get(key) or {}).get("synced_rev") or 0),
        },
    }


_ASK = re.compile(r"^They ask for (?P<asked>.+?)\. Your CV shows (?P<have>.+?)(?: \((?P<n>\d+ of \d+)\))?\.$")


def _why_line(it: dict) -> str:
    """One short, specific sentence for the list: the evidence, never the group word."""
    reasons = [r.get("text") or "" for r in (it.get("reasons") or []) if r.get("text")]
    lead = it.get("explanation") or (reasons[0] if reasons else "")
    lead = lead.split(": ", 1)[1] if lead.startswith(("Good match: ", "Worth a shot: ", "Backup option: ", "Needs checking: ", "Not recommended: ")) else lead
    if not lead and reasons:
        lead = reasons[0]
    m = _ASK.match(lead)
    if m:
        have = m.group("have")
        if have in {"all of them", "it"}:
            return f"Your {m.group('asked')} experience matches what they ask for."
        return f"Your {have} experience matches part of what they ask for ({m.group('n')})." if m.group("n") else f"Your {have} experience matches what they ask for."
    return truncate(lead, 140)


def _check_line(it: dict) -> str | None:
    gaps = [g if isinstance(g, str) else str(g) for g in (it.get("gaps") or [])]
    for g in gaps:
        if g.startswith("The posting has almost no text"):
            continue
        return truncate(g, 150)
    return None


LEVEL_WORDS = {"entry": "Entry-level", "junior": "Junior", "mid": "Mid-level", "senior": "Senior", "staff": "Staff"}
DIRECTION_WORDS = {"devops": "DevOps", "platform": "Platform", "sre": "SRE", "cloud": "Cloud", "infrastructure": "Infrastructure", "systems": "Systems"}


def _headline(targets: dict) -> str:
    dirs = [DIRECTION_WORDS.get(d, d.title()) for d in (targets.get("direction") or [])][:3]
    role = (" / ".join(dirs) + " Engineer") if dirs else "Infrastructure Engineer"
    level = LEVEL_WORDS.get(targets.get("level") or "")
    return f"{level} {role}" if level else role


def _profile_doc(db: Database) -> dict:
    prof = profile_svc.approved(db)
    if not prof:
        raise BoardError("There is no approved profile yet. Finish setup first (`jobhunter next`).")
    targets = json.loads(prof.get("targets_json") or "{}")
    facts = json.loads(prof.get("facts_json") or "[]")
    prefs = prefs_svc.get(db)
    live = [f for f in facts if f.get("status") != "rejected" and f.get("text")]
    skills = [f["text"] for f in live if f.get("category") == "skill"][:16]
    roles = [f["text"] for f in live if f.get("category") == "role"][:8]
    projects = [f["text"] for f in live if f.get("category") == "project"][:6]
    certs = [f["text"] for f in live if f.get("category") in {"certification", "course"}][:8]
    years = targets.get("professional_years_relevant")
    salary = f"{prefs.salary_min:,.0f} {prefs.salary_currency} per {prefs.salary_period or 'year'}" if prefs.salary_min else None
    prefs_sync = db.fetchone("SELECT synced_rev FROM board_sync WHERE job_key = '_prefs'")
    return {
        "schema": SCHEMA,
        "at": now_iso(),
        "headline": _headline(targets),
        "years_text": (f"about {float(years):g} years of relevant paid work" if years else "no paid infrastructure work yet"),
        "roles": roles,
        "projects": projects,
        "certifications": certs,
        "prefs_synced_rev": int(prefs_sync["synced_rev"]) if prefs_sync else 0,
        "prefs": {"location": prefs.current_location, "countries_text": country_names(sorted(prefs.work_countries or [])) if prefs.work_countries else "", "countries": sorted(prefs.work_countries or []), "salary_text": salary, "remote": prefs.remote, "work_modes": prefs.work_modes},
        "summary": prof.get("summary_text"),
        "direction": targets.get("direction") or [],
        "level": targets.get("level"),
        "years_relevant": targets.get("professional_years_relevant"),
        "skills": skills,
        "location": prefs.current_location,
        "countries": country_names(sorted(prefs.work_countries or [])) if prefs.work_countries else None,
        "remote": REMOTE_WORDS.get(prefs.remote, prefs.remote),
        "work_modes": prefs.work_modes,
        "salary": salary,
        "version": prof.get("version"),
        "approved_at": prof.get("approved_at"),
    }


def _run_doc(db: Database, recommended: list[str], docs: list[dict], shards: int) -> dict:
    last = hunt.last_run(db)
    stats = (last or {}).get("stats") or {}
    counts = {"recommended": len(recommended), "jobs": len(docs)}
    for name, flag in (("favorites", "favorited"), ("saved", "saved"), ("dismissed", "dismissed")):
        counts[name] = sum(1 for d in docs if d["state"].get(flag))
    counts["applied"] = sum(1 for d in docs if d["state"].get("stage") in {"applied", "replied", "interview", "offer", "rejected", "withdrawn"})
    counts["old"] = sum(1 for d in docs if d.get("list") == "old")
    counts["uncertain"] = sum(1 for d in docs if d.get("list") == "uncertain")
    imp = db.get_setting("board.last_import") or {}
    return {
        "schema": SCHEMA,
        "at": now_iso(),
        "run_kind": "cloud" if db.get_setting("cloud.role") == "cloud" else "laptop",
        "policy_version": POLICY_VERSION,
        "recommended": recommended,
        "shards": shards,
        "counts": counts,
        "sync": {"actions_read_at": imp.get("at"), "actions_seen": imp.get("seen"), "actions_applied": imp.get("applied")},
        "hunter": _hunter_status(db),
        "attention": _attention(db, docs),
        "last_hunt": None
        if not last
        else {
            "started_at": last.get("started_at"),
            "finished_at": last.get("finished_at"),
            "status": last.get("status"),
            "postings": stats.get("postings_fetched"),
            "new": stats.get("jobs_new"),
            "relevant": stats.get("jobs_relevant_new"),
            "sources_ok": stats.get("sources_ok"),
            "sources_failed": stats.get("sources_failed"),
            "failures": [f.get("source") for f in (stats.get("failures") or [])][:5],
        },
    }


def _hunter_status(db: Database) -> dict:
    """Only what the data supports: no "running" because a schedule preference exists."""
    from jobhunter.services import notifications, schedule

    ok = hunt.last_successful_run(db)
    sched = schedule.describe(db)
    worker = str(sched.get("worker") or "")
    running = worker.startswith("running")
    address = notifications.recipient(db)
    return {
        "last_successful_hunt_at": ok.get("finished_at") if ok else None,
        "automatic_hunting": "running" if (sched.get("enabled") and running) else ("set up, not running now" if sched.get("enabled") else "off"),
        "next_hunt_at": sched.get("next_run_at") if (sched.get("enabled") and running) else None,
        "email": {"enabled": bool(address) and db.get_setting("digest.transport") in {"claude", "gmail", "smtp"}, "address_hint": (address[:2] + "…@" + address.split("@", 1)[1]) if address and "@" in address else None},
    }


def _attention(db: Database, docs: list[dict]) -> list[dict]:
    """Outstanding things the data really shows. Never invented urgency."""
    from jobhunter.services import drafts as drafts_svc
    from jobhunter.services import notifications

    items: list[dict] = []
    last = hunt.last_run(db)
    if last and last.get("status") in {"failed", "no_sources", "aborted"}:
        items.append({"kind": "hunt_failed", "text": "Your last search stopped before it finished. Your results are saved. Hunter tries again on the next search."})
    for f in drafts_svc.due_followups(db)[:3]:
        items.append({"kind": "followup", "text": f"Follow-up due: {f['title']} at {f['employer_name']}.", "job_id": f["job_id"]})
    by_id = {}
    for d in docs:
        by_id[d["key"]] = d
    for r in db.fetchall(
        "SELECT j.id, j.canonical_key, j.title, j.employer_name, e.occurred_at FROM events e JOIN jobs j ON j.id = e.job_id "
        "WHERE e.kind = 'saved' AND e.reverted_by IS NULL AND e.occurred_at < datetime('now', '-7 days') "
        "AND NOT EXISTS (SELECT 1 FROM events u WHERE u.job_id = j.id AND u.kind = 'unsaved' AND u.id > e.id) "
        "AND NOT EXISTS (SELECT 1 FROM applications a WHERE a.job_id = j.id AND a.stage != 'prepared') ORDER BY e.occurred_at LIMIT 3"
    ):
        items.append({"kind": "saved_waiting", "text": f"You saved {r['title']} at {r['employer_name'] or 'an employer'} more than a week ago. Still interested?", "key": job_key(r["canonical_key"])})
    fresh_good = [d for d in docs if d.get("recommended") and d.get("group") == "good" and (d.get("first_seen_at") or "") >= (now() - timedelta(days=2)).isoformat()]
    if fresh_good:
        items.append({"kind": "new_good", "text": f"{len(fresh_good)} new good match{'es' if len(fresh_good) != 1 else ''} since the last two days.", "keys": [d["key"] for d in fresh_good[:5]]})
    failed = notifications.failed_digest(db)
    if failed:
        items.append({"kind": "digest_failed", "text": "The last job email could not be sent. Hunter will try again, or ask Hunter to send it."})
    if notifications.unresolved(db):
        items.append({"kind": "digest_uncertain", "text": "A job email may not have arrived. Check your inbox and tell Hunter."})
    return items[:6]


def export(ctx: AppContext, db: Database, out_dir: str | Path, limit: int = RECOMMENDED_LIMIT, require_actions: bool = True) -> dict:
    """Write the documents for one sync into `out_dir` and return the manifest Claude follows.

    Every document is rewritten whole, so a failed or skipped sync leaves nothing to repair: the next
    sync writes the truth again. The manifest lists, in order, the files to `set` and the shard names
    that may be left over from a bigger earlier export (`delete_if_present`). The page's actions must have
    been read since Hunter last wrote (`require_actions`): otherwise a job the student applied to on the
    page would be written back as recommended."""
    from jobhunter.services.matching import engine

    if require_actions:
        why = stale_reason(db)
        if why:
            raise BoardError(why)
    out = Path(out_dir)
    (out / "meta").mkdir(parents=True, exist_ok=True)
    (out / "board").mkdir(parents=True, exist_ok=True)
    profile = _profile_doc(db)
    # After a Hunter update with new matching rules, the page would otherwise show nothing until the next hunt.
    engine.rematch_quietly(ctx, db)
    synced = {r["job_key"]: r for r in db.fetchall("SELECT * FROM board_sync")}
    items, recommended, lists = _selected(db, limit)
    rec_set = set(recommended)
    docs = [_doc(db, it, synced, rec_set, lists.get(it["id"], "acted")) for it in items]
    shards = [docs[i : i + SHARD_SIZE] for i in range(0, len(docs), SHARD_SIZE)] or [[]]
    run = _run_doc(db, recommended, docs, len(shards))
    writes = []
    _write(out / "meta" / "profile.json", profile)
    writes.append({"collection": "meta", "doc_id": "profile", "file": str(out / "meta" / "profile.json")})
    for n, shard in enumerate(shards):
        body = {"schema": SCHEMA, "at": run["at"], "shard": n, "jobs": {d["key"]: d for d in shard}}
        _write(out / "board" / f"jobs-{n}.json", body)
        writes.append({"collection": "board", "doc_id": f"jobs-{n}", "file": str(out / "board" / f"jobs-{n}.json")})
    _write(out / "meta" / "run.json", run)
    writes.append({"collection": "meta", "doc_id": "run", "file": str(out / "meta" / "run.json")})
    manifest = {
        "url": db.get_setting(URL_SETTING),
        "writes": writes,
        "delete_if_present": [f"board/jobs-{n}" for n in range(len(shards), len(shards) + 6)],
        "jobs": len(docs),
        "recommended": len(recommended),
        "shards": len(shards),
        "largest_file_bytes": max((Path(w["file"]).stat().st_size for w in writes), default=0),
        "rules": [
            "Every write is a `set` of the whole file, in this order, in one batch (or two if the batch would exceed 1 MiB).",
            "A document that already exists needs its current `version` as if_version: take it from a `list` of the `meta` and `board` collections just before.",
            "If a write is refused because the version changed, list again and set again. Never write under actions/.",
        ],
    }
    _write(out / "manifest.json", manifest)
    db.set_setting("board.last_export", {"at": run["at"], "seq": _next_seq(db), "jobs": len(docs), "recommended": len(recommended), "shards": len(shards)})
    return manifest


def _next_seq(db: Database) -> int:
    """Reads and writes are ordered by this counter, not by the clock (two steps can share a second)."""
    seq = int(db.get_setting("board.seq") or 0) + 1
    db.set_setting("board.seq", seq)
    return seq


def _write(path: Path, body: dict) -> None:
    path.write_text(json.dumps(body, ensure_ascii=False, indent=1), encoding="utf-8")


# ---------------------------------------------------------------------------
# Import: what the student did on the page
# ---------------------------------------------------------------------------
def _unwrap(obj: dict) -> dict:
    """Files saved by the Artifact tools hold the bare document, or a wrapper around it. Accept both."""
    if isinstance(obj, dict) and "job_key" not in obj:
        for k in ("data", "document", "doc", "fields"):
            if isinstance(obj.get(k), dict):
                return obj[k]
    return obj


def _read_actions(folder: Path) -> list[dict]:
    if not folder.is_dir():
        return []
    out = []
    for p in sorted(folder.glob("*.json")):
        try:
            doc = _unwrap(json.loads(p.read_text(encoding="utf-8")))
        except (ValueError, OSError):
            continue
        if isinstance(doc, dict) and doc.get("job_key"):
            out.append(doc)
    return out


def _current(db: Database, job_id: int) -> dict:
    row = db.fetchone(f"SELECT * FROM ({views.BASE_SELECT}) WHERE id = ?", (POLICY_VERSION, job_id))
    return views.summarize(row)


def import_actions(ctx: AppContext, db: Database, in_dir: str | Path) -> dict:
    """Apply what the page recorded under actions/. Each action document is applied once, by its `rev`;
    untrusted input: only the known flags and the note text are read, everything else is ignored."""
    folder = Path(in_dir)
    docs = _read_actions(folder / "actions" if (folder / "actions").is_dir() else folder)
    by_key = {job_key(r["canonical_key"]): r["id"] for r in db.fetchall("SELECT id, canonical_key FROM jobs")}
    synced = {r["job_key"]: r for r in db.fetchall("SELECT * FROM board_sync")}
    applied: list[dict] = []
    orphans: list[str] = []
    skipped = 0
    for a in docs:
        key = str(a.get("job_key"))
        try:
            rev = int(a.get("rev") or 0)
        except (TypeError, ValueError):
            rev = 0
        prev = synced.get(key) or {}
        if rev <= int(prev.get("synced_rev") or 0):
            skipped += 1
            continue
        if key == PREFS_KEY:
            with db.tx():
                changes = _apply_prefs(db, a)
                values = {"synced_rev": rev, "synced_at": now_iso()}
                if prev:
                    db.update("board_sync", values, "job_key = ?", (key,))
                else:
                    db.insert("board_sync", {"job_key": key, **values})
            applied.append({"job_key": key, "rev": rev, "changes": changes})
            continue
        jid = by_key.get(key)
        if jid is None:
            orphans.append(key)
            continue
        # One transaction per action: the changes and the revision that records them land together or not at all.
        with db.tx():
            done = _apply_one(db, jid, a, prev)
            values = {"canonical_key": _canonical(db, jid), "synced_rev": rev, "synced_at": now_iso(), "note_id": done.pop("_note_id", prev.get("note_id"))}
            if prev:
                db.update("board_sync", values, "job_key = ?", (key,))
            else:
                db.insert("board_sync", {"job_key": key, **values})
        applied.append({"job_key": key, "job_id": jid, "rev": rev, "changes": done["changes"]})
    summary = {"seen": len(docs), "applied": applied, "already_applied": skipped, "orphans": orphans, "at": now_iso()}
    db.set_setting("board.last_import", {"at": summary["at"], "seq": _next_seq(db), "seen": len(docs), "applied": len(applied), "orphans": len(orphans)})
    return summary


PREFS_KEY = "_prefs"


def _apply_prefs(db: Database, a: dict) -> list[str]:
    """The page's preference edits (location, countries, salary, remote/work modes), applied through the same
    parser the setup uses. Untrusted input: only these four strings are read, bounded in length."""
    changes: list[str] = []
    fields = {k: str(a.get(k) or "")[:200] for k in ("location", "countries", "salary", "remote")}
    try:
        prefs_svc.update_basics(db, location=fields["location"] or None, countries=fields["countries"] or None, salary=fields["salary"] if a.get("salary") is not None else None, remote_text=fields["remote"] or None, reason="dashboard")
    except ValueError as exc:
        return [f"rejected: {exc}"]
    changes += [k for k, v in fields.items() if v]
    if a.get("salary") == "":
        changes.append("salary cleared")
    return changes


def _apply_one(db: Database, jid: int, a: dict, prev: dict) -> dict:
    cur = _current(db, jid)
    changes: list[str] = []
    out: dict = {"changes": changes}
    want_fav = bool(a.get("favorite"))
    if want_fav != cur["favorited"]:
        actions.favorite_job(db, jid, on=want_fav)
        changes.append("favorite" if want_fav else "unfavorite")
    want_saved = bool(a.get("saved"))
    if want_saved != cur["saved"]:
        actions.save_job(db, jid, save=want_saved)
        changes.append("save" if want_saved else "unsave")
    want_dismissed = bool(a.get("dismissed"))
    if want_dismissed != cur["dismissed"]:
        actions.dismiss_job(db, jid, undo=not want_dismissed)
        changes.append("dismiss" if want_dismissed else "undismiss")
    applied_at = a.get("applied_at")
    is_applied = cur.get("stage") in {"applied", "replied", "interview", "offer", "rejected", "withdrawn"}
    if applied_at and not is_applied:
        when = str(applied_at)[:25] if isinstance(applied_at, str) else None
        actions.mark_applied(db, jid, applied_at=when, source="user", evidence={"via": "dashboard"})
        changes.append("applied")
    elif not applied_at and cur.get("stage") == "applied":
        ev = db.fetchone("SELECT id FROM events WHERE job_id = ? AND kind = 'applied' AND reverted_by IS NULL ORDER BY id DESC LIMIT 1", (jid,))
        if ev:
            actions.correct(db, ev["id"], note="Undone on the dashboard.")
            changes.append("unapplied")
    note = a.get("note")
    text = (note.get("text") if isinstance(note, dict) else None) or ""
    text = str(text).strip()[: notes.MAX_NOTE_CHARS]
    if isinstance(note, dict) and (note.get("deleted") or (not text and int(note.get("rev") or 0) > 0)):
        note_id = prev.get("note_id")
        if note_id and db.fetchone("SELECT id FROM job_notes WHERE id = ? AND deleted_at IS NULL", (note_id,)):
            notes.delete(db, note_id)
            changes.append("note_deleted")
        out["_note_id"] = None
    elif text:
        latest = notes.latest_for_job(db, jid)
        if not latest or latest["body"] != text:
            note_id = prev.get("note_id")
            if note_id and db.fetchone("SELECT id FROM job_notes WHERE id = ? AND deleted_at IS NULL", (note_id,)):
                notes.update(db, note_id, text)
                out["_note_id"] = note_id
            else:
                out["_note_id"] = notes.add(db, jid, text)["id"]
            changes.append("note")
    return out


# ---------------------------------------------------------------------------
# What travels in the cloud state file
# ---------------------------------------------------------------------------
def export_sync_state(db: Database) -> dict:
    """The dashboard link and which page actions were already applied, so a cloud run and the laptop
    agree and never apply the same action twice."""
    return {"url": db.get_setting(URL_SETTING), "synced": {r["job_key"]: int(r["synced_rev"]) for r in db.fetchall("SELECT job_key, synced_rev FROM board_sync")}}


def import_sync_state(db: Database, data: dict) -> dict:
    if not isinstance(data, dict):
        return {"url": False, "synced": 0}
    changed = {"url": False, "synced": 0}
    url = data.get("url")
    if isinstance(url, str) and URL_RE.match(url) and db.get_setting(URL_SETTING) != url.rstrip("/"):
        db.set_setting(URL_SETTING, url.rstrip("/"))
        changed["url"] = True
    with db.tx():
        for key, rev in (data.get("synced") or {}).items():
            try:
                rev = int(rev)
            except (TypeError, ValueError):
                continue
            row = db.fetchone("SELECT synced_rev FROM board_sync WHERE job_key = ?", (key,))
            if row and int(row["synced_rev"]) >= rev:
                continue
            if row:
                db.update("board_sync", {"synced_rev": rev, "synced_at": now_iso()}, "job_key = ?", (key,))
            else:
                db.insert("board_sync", {"job_key": key, "synced_rev": rev, "synced_at": now_iso()})
            changed["synced"] += 1
    return changed
