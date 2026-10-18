"""The `jobhunter` command line. The Claude Code skill and the dashboard share
these same services; the CLI is the narrow interface Claude is allowed to use.

Every command accepts --json for machine-readable output.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from pathlib import Path

from jobhunter import __version__
from jobhunter.config import AppContext, dashboard_bind
from jobhunter.db import Database, connect
from jobhunter.services.secrets import SecretStoreLocked


class CliError(Exception):
    pass


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------
def emit(args, payload, text: str | None = None) -> None:
    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2, default=str, ensure_ascii=False))
        return
    if text is not None:
        print(text)
        return
    _print_plain(payload)


def _print_plain(obj, indent: int = 0) -> None:
    pad = "  " * indent
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, dict | list) and v:
                print(f"{pad}{k}:")
                _print_plain(v, indent + 1)
            else:
                print(f"{pad}{k}: {v if v not in (None, [], {}) else '-'}")
    elif isinstance(obj, list):
        for item in obj:
            if isinstance(item, dict | list):
                _print_plain(item, indent)
                print()
            else:
                print(f"{pad}- {item}")
    else:
        print(f"{pad}{obj}")


def _open(args) -> tuple[AppContext, Database]:
    ctx = AppContext.from_env(demo=args.demo or None, home=Path(args.home) if args.home else None)
    db = connect(ctx.paths.db_path)
    return ctx, db


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
def cmd_status(args):
    from jobhunter.services import hunt, onboarding, schedule, stats
    from jobhunter.services.email import gmail

    ctx, db = _open(args)
    t = stats.totals(db)
    last = hunt.last_run(db)
    sched = schedule.describe(db)
    payload = {
        "demo": ctx.demo,
        "data_dir": str(ctx.paths.root),
        "summary": stats.summary_line(t),
        "totals": t,
        "onboarding": onboarding.status(db),
        "last_hunt": {"id": last["id"], "status": last["status"], "finished_at": last.get("finished_at"), "stats": last.get("stats")} if last else None,
        "schedule": {"frequency": sched.get("frequency_label"), "next_run_at": sched.get("next_run_at"), "worker": sched.get("worker"), "notify": sched.get("notify_label")},
        "mailbox": gmail.describe(db)["read"],
        "source_failures": [s["id"] for s in hunt.source_health(db) if s.get("last_status") == "failed"],
    }
    emit(args, payload)


def cmd_doctor(args):
    from jobhunter.services import doctor

    ctx, _ = _open(args)
    res = doctor.run(ctx)
    if args.json:
        emit(args, res)
        return
    for c in res["checks"]:
        print(f"[{'ok' if c['ok'] else 'FAIL'}] {c['name']}: {c['detail']}" + (f"\n       Fix: {c['fix']}" if (not c["ok"] and c.get("fix")) else ""))
    print("All good." if res["ok"] else "Some checks failed; see fixes above.")


def cmd_setup(args):
    from jobhunter.services import onboarding

    ctx, db = _open(args)
    if args.setup_cmd == "next":
        nxt = onboarding.next_step(db)
        emit(args, {"next": nxt, "done": nxt is None, "status": onboarding.status(db)}, text=None if args.json else (f"{nxt['question']}" + (f"\n({nxt['help']})" if nxt and nxt.get("help") else "") if nxt else "Setup is complete."))
    elif args.setup_cmd == "status":
        emit(args, onboarding.status(db))
    elif args.setup_cmd == "answer":
        try:
            res = onboarding.answer(db, args.key, " ".join(args.value))
        except onboarding.AnswerError as exc:
            raise CliError(str(exc)) from exc
        emit(args, res)
    elif args.setup_cmd == "basics":
        try:
            res = onboarding.basics(db, location=args.location, countries=args.countries, salary=args.salary, remote=args.remote, frequency=args.frequency, notify=args.notify, timezone=args.timezone, linkedin_url=args.linkedin)
        except onboarding.AnswerError as exc:
            raise CliError(str(exc)) from exc
        emit(args, res)


def cmd_docs(args):
    from jobhunter.services import documents as docs

    ctx, db = _open(args)
    try:
        if args.docs_cmd == "add":
            p = Path(args.path).expanduser()
            if not p.is_file():
                raise CliError(f"File not found: {p}")
            row = docs.add_document(db, ctx.paths, args.kind, p.read_bytes(), p.name, label=args.label)
            note = {"ok": "Read it fine.", "empty": "I could not read text from it: " + (row["extraction_notes"] or ""), "failed": "Could not read it: " + (row["extraction_notes"] or ""), "unsupported": row["extraction_notes"]}[row["extraction_status"]]
            from jobhunter.services import onboarding

            built = onboarding.ensure_profile_draft(db)
            ready, missing = onboarding.documents_ready(db)
            emit(args, {"document": {k: row[k] for k in ("id", "kind", "version", "original_filename", "extraction_status")}, "note": note, "still_missing": missing, "profile_built": built is not None, "next": onboarding.next_step(db) if ready else None})
        elif args.docs_cmd == "list":
            emit(args, docs.list_documents(db, current_only=not args.all))
        elif args.docs_cmd == "text":
            print(docs.document_text(db, args.id))
        elif args.docs_cmd == "verify":
            emit(args, docs.verify_integrity(db))
        elif args.docs_cmd == "linkedin":
            if args.url:
                if not (args.url.startswith("https://") and "linkedin.com/in/" in args.url):
                    raise CliError("Expected a URL like https://www.linkedin.com/in/your-name")
                db.set_setting("linkedin.url", args.url)
            if args.file:
                p = Path(args.file).expanduser()
                row = docs.add_document(db, ctx.paths, "linkedin", p.read_bytes(), p.name, source_url=args.url or db.get_setting("linkedin.url"))
                emit(args, {"document": row, "note": "LinkedIn content stored."})
            elif args.text:
                row = docs.add_text_document(db, ctx.paths, "linkedin", args.text, "linkedin-pasted", source_url=args.url or db.get_setting("linkedin.url"))
                emit(args, {"document": row, "note": "LinkedIn content stored."})
            else:
                emit(args, {"url": db.get_setting("linkedin.url"), "note": "URL saved. I still need the profile content: give me an export file (--file) or pasted text (--text). I never guess profile content from a URL."})
    except docs.DocumentError as exc:
        raise CliError(str(exc)) from exc


def cmd_evidence(args):
    from jobhunter.services import consent, github_evidence
    from jobhunter.services import documents as docs

    ctx, db = _open(args)
    if args.evidence_cmd == "github":
        consent.grant(db, "github_read", {"repos": args.repos})
        payload = github_evidence.inspect_repos(args.repos)
        row = docs.github_document(db, ctx.paths, payload, "github-evidence")
        emit(args, {"document_id": row["id"], "repositories": [{k: v for k, v in r.items() if k != "readme_excerpt"} for r in payload["repositories"]], "errors": payload["errors"], "note": "Ownership is not assumed. Hunter will ask whether each repository is your own work."})


def cmd_profile(args):
    from jobhunter.models import ProfileDraft
    from jobhunter.services import profile as profile_svc

    ctx, db = _open(args)
    try:
        if args.profile_cmd == "build":
            draft = profile_svc.build_rule_based_draft(db)
            res = profile_svc.save_draft(db, draft, built_by="rules")
            row = profile_svc.current_draft(db)
            emit(args, {**res, "profile": profile_svc.profile_view(db, row), "note": "Rule-based draft. In Claude Code, Hunter refines it and asks you to confirm. Nothing is approved yet."})
        elif args.profile_cmd == "show":
            row = profile_svc.approved(db) if args.approved else (profile_svc.current_draft(db) or profile_svc.approved(db))
            if not row:
                raise CliError("No profile yet. Run `jobhunter profile build` after adding your CV.")
            emit(args, profile_svc.profile_view(db, row))
        elif args.profile_cmd == "approve":
            res = profile_svc.approve(db, summary_override=args.summary)
            from jobhunter.services import consent

            consent.grant(db, "profile_approved", {"version": res["version"]})
            emit(args, {**res, "note": "Profile approved. Live hunting is now allowed."})
        elif args.profile_cmd == "questions":
            emit(args, profile_svc.open_questions(db))
        elif args.profile_cmd == "answer":
            emit(args, profile_svc.answer_question(db, args.key, " ".join(args.answer)))
        elif args.profile_cmd == "skip":
            profile_svc.skip_question(db, args.key)
            emit(args, {"skipped": args.key})
        elif args.profile_cmd == "correct":
            emit(args, profile_svc.add_user_fact(db, " ".join(args.text), category=args.category, experience_kind=args.kind))
        elif args.profile_cmd == "submit":
            data = json.loads(Path(args.file).read_text(encoding="utf-8"))
            draft = ProfileDraft.model_validate(data)
            res = profile_svc.save_draft(db, draft, built_by="claude")
            emit(args, {**res, "note": "Draft saved after evidence validation. Ask the user to approve it."})
        elif args.profile_cmd == "schema":
            emit(args, ProfileDraft.model_json_schema())
    except profile_svc.ProfileError as exc:
        raise CliError(str(exc)) from exc


def cmd_prefs(args):
    from jobhunter.services import preferences as prefs_svc

    ctx, db = _open(args)
    if args.prefs_cmd == "show":
        p = prefs_svc.get(db)
        emit(args, p.model_dump(mode="json"), text=None if args.json else "\n".join(prefs_svc.describe(p)))
    elif args.prefs_cmd == "set":
        p = prefs_svc.get(db)
        key, value = args.key, " ".join(args.value)
        if key in {"current_location", "timezone", "salary_currency"}:
            setattr(p, key, value)
        elif key == "work_countries":
            codes, unknown = prefs_svc.parse_country_list(value)
            p.work_countries = codes
            p.work_countries_note = ("unrecognized: " + ", ".join(unknown)) if unknown else None
        elif key in {"needs_sponsorship", "relocation"}:
            p.__setattr__(key, value.replace(" ", "_"))
        elif key == "remote":
            p.remote = value.replace(" ", "_")  # type: ignore[assignment]
        elif key == "salary_min":
            from jobhunter.services.onboarding import parse_salary_answer

            parsed = parse_salary_answer(value)
            if not parsed:
                raise CliError("Use e.g. '55000 EUR per year'.")
            p.salary_min, p.salary_currency, p.salary_period = parsed
            p.salary_strength = args.strength or "nice"
        elif key == "salary_strength":
            p.salary_strength = value  # type: ignore[assignment]
        elif key == "regions":
            p.regions = [r.strip() for r in value.split(",")]
        else:
            prefs_svc.set_extra(db, key, value, args.strength or "nice", reason=args.reason)
            emit(args, {"set": key, "value": value, "strength": args.strength or "nice"})
            return
        try:
            prefs_svc.save(db, p, reason=args.reason or "cli")
        except Exception as exc:
            raise CliError(f"Invalid value: {exc}") from exc
        emit(args, {"set": key, "value": value})
    elif args.prefs_cmd == "revert":
        emit(args, {"reverted": prefs_svc.revert_last(db, args.key)})
    elif args.prefs_cmd == "history":
        emit(args, prefs_svc.history(db))


def cmd_sources(args):
    from jobhunter.services import catalog, hunt
    from jobhunter.services.sources import PROVIDERS
    from jobhunter.util import now_iso

    ctx, db = _open(args)
    if args.sources_cmd == "list":
        emit(args, [{k: s.get(k) for k in ("id", "kind", "enabled", "origin", "last_status", "last_success_at", "plain_status", "last_error")} for s in hunt.source_health(db)])
    elif args.sources_cmd == "registry":
        emit(args, PROVIDERS)
    elif args.sources_cmd in {"enable", "disable"}:
        n = db.update("watch_sources", {"enabled": 1 if args.sources_cmd == "enable" else 0}, "id = ?", (args.id,))
        if not n:
            raise CliError(f"Unknown source {args.id}")
        emit(args, {"id": args.id, "enabled": args.sources_cmd == "enable"})
    elif args.sources_cmd == "add":
        if args.kind not in {"greenhouse", "lever", "ashby"}:
            raise CliError("kind must be greenhouse, lever, or ashby")
        eid = catalog.upsert_employer(db, args.name, origin="user", ats_kind=args.kind, ats_token=args.token, ats_region=args.region, careers_url=args.careers)
        sid = catalog.source_id_for(args.kind, args.token)
        created = catalog.ensure_source(db, sid, args.kind, catalog.source_config(args.kind, args.token, args.name, args.region), "user", eid)
        emit(args, {"source_id": sid, "created": created, "note": "Run `jobhunter sources check " + sid + "` to confirm the board answers."})
    elif args.sources_cmd == "check":
        from jobhunter.services.http import SafeClient
        from jobhunter.services.sources import get_adapter

        s = db.fetchone("SELECT * FROM watch_sources WHERE id = ?", (args.id,))
        if not s:
            raise CliError(f"Unknown source {args.id}")
        with SafeClient() as client:
            res = get_adapter(s["kind"]).fetch(client, s["id"], json.loads(s["config_json"]))
        db.update("watch_sources", {"last_run_at": now_iso(), "last_status": res.status, "last_error": res.error, **({"last_success_at": now_iso()} if res.status in {"ok", "partial"} else {})}, "id = ?", (s["id"],))
        emit(args, {"id": s["id"], "status": res.status, "postings": len(res.postings), "error": res.error, "notes": res.notes, "sample": [p.title for p in res.postings[:5]]})
    elif args.sources_cmd == "sync-catalog":
        emit(args, catalog.sync_catalog(db))


def cmd_employers(args):
    ctx, db = _open(args)
    sql = "SELECT id, name, ats_kind, ats_token, origin, discovered_via, regions_json, careers_url, catalogue_checked_at FROM employers"
    params: tuple = ()
    if args.origin:
        sql += " WHERE origin = ?"
        params = (args.origin,)
    sql += " ORDER BY name LIMIT ?"
    emit(args, db.fetchall(sql, params + (args.limit,)))


def cmd_hunt(args):
    from jobhunter.services import hunt, onboarding

    ctx, db = _open(args)
    if args.hunt_cmd == "run":
        ok, why = onboarding.live_hunting_allowed(db, ctx.demo)
        if not ok:
            raise CliError(why)
        if ctx.demo:
            raise CliError("Demo mode uses synthetic data only (`jobhunter --demo demo seed`). Live hunts run without --demo.")
        try:
            stats = hunt.run_hunt(ctx, db, trigger=args.trigger, source_ids=args.source or None, progress=(None if args.json else lambda sid, st: print(f"  {sid}: {st}", file=sys.stderr)))
        except (hunt.HuntInProgress, hunt.HuntNotAllowed) as exc:
            raise CliError(str(exc)) from exc
        emit(args, stats, text=None if args.json else _hunt_text(stats))
    elif args.hunt_cmd == "status":
        last = hunt.last_run(db)
        emit(args, {"last_run": last, "sources": [{k: s.get(k) for k in ("id", "plain_status", "last_success_at")} for s in hunt.source_health(db)]})
    elif args.hunt_cmd == "history":
        rows = db.fetchall("SELECT id, trigger, status, started_at, finished_at, stats_json, error FROM hunt_runs ORDER BY id DESC LIMIT ?", (args.limit,))
        for r in rows:
            r["stats"] = json.loads(r.pop("stats_json") or "{}")
        emit(args, rows)


def _hunt_text(stats: dict) -> str:
    lines = [
        f"Hunt {stats.get('status')}: {stats['sources_ok']} source(s) worked, {stats['sources_partial']} partly, {stats['sources_failed']} failed, {stats['sources_skipped']} skipped.",
        f"Looked at {stats['postings_fetched']} postings; {stats['jobs_new']} new jobs, {stats['jobs_relevant_new']} of them relevant; {stats['jobs_closed']} no longer listed.",
    ]
    if stats.get("employers_new"):
        lines.append(f"{stats['employers_new']} new employer(s) discovered; {stats.get('sources_added', 0)} new career board(s) now watched.")
    for f in stats.get("failures", []):
        lines.append(f"I couldn't check {f['source']}: {f['error']}. Your other searches still worked.")
    m = stats.get("matching") or {}
    if m.get("status") == "ok":
        lines.append(f"Matched {m.get('computed', 0)} job(s) against profile v{m.get('profile_version')}; {m.get('presented', 0)} recommended.")
    elif m.get("status") == "skipped":
        lines.append(m.get("reason", "Matching skipped."))
    elif m.get("status") == "failed":
        lines.append("Matching failed; the jobs are saved and will be matched next time. " + str(m.get("error")))
    return "\n".join(lines)


def cmd_jobs(args):
    from jobhunter.services import hunt, views
    from jobhunter.services import jobs as jobs_svc
    from jobhunter.services.sources.import_text import detect_ats_link, parse_alert_email, posting_from_text

    ctx, db = _open(args)
    if args.jobs_cmd == "list":
        res = views.list_jobs(db, view=args.view, group=args.group, query=args.query, page=args.page, per_page=args.limit, sort=args.sort, remote=args.remote, age=args.age, country=args.country, fav_only=args.favorites, saved_only=args.saved)
        emit(args, res, text=None if args.json else views.render_list_text(res))
    elif args.jobs_cmd == "show":
        job = views.job_detail(db, args.id)
        if not job:
            raise CliError(f"Job {args.id} not found.")
        emit(args, job, text=None if args.json else views.render_detail_text(job))
    elif args.jobs_cmd == "import":
        text = Path(args.file).read_text(encoding="utf-8") if args.file else (args.text or "")
        if not text.strip() and not args.url:
            raise CliError("Give me the vacancy text (--text or --file) or a link (--url).")
        if args.url and not text.strip():
            info = detect_ats_link(args.url)
            if not info:
                raise CliError("I can't read that page myself. Please paste the vacancy text with --text or --file (LinkedIn and Indeed pages must be pasted).")
            posting = _lookup_ats(info)
            if posting is None:
                raise CliError("I could not fetch that listing from the employer's job board. Please paste the text.")
        else:
            posting = posting_from_text(text, args.title, args.company, args.url, args.location)
        res = hunt.import_postings(ctx, db, [posting])
        emit(args, {**res, "note": "Imported from text you gave me (not scraped)."})
    elif args.jobs_cmd == "import-alert":
        text = Path(args.file).read_text(encoding="utf-8")
        postings = parse_alert_email(text)
        if not postings:
            raise CliError("I did not find LinkedIn or Indeed job links in that email.")
        res = hunt.import_postings(ctx, db, postings)
        emit(args, {**res, "titles": [p.title for p in postings], "note": "Imported from a job-alert email. Posting dates are unknown unless the email states them."})
    elif args.jobs_cmd == "lookup":
        from jobhunter.services import original_lookup

        job = jobs_svc.get_job(db, args.id)
        if not job:
            raise CliError(f"Job {args.id} not found.")
        emit(args, original_lookup.resolve(db, job))


def _lookup_ats(info: dict):
    from jobhunter.services.http import SafeClient
    from jobhunter.services.sources import get_adapter

    with SafeClient() as client:
        if info["kind"] == "greenhouse":
            from jobhunter.services.sources.greenhouse import GreenhouseAdapter

            return GreenhouseAdapter().fetch_single(client, info["token"], info["job_id"])
        if info["kind"] == "lever":
            res = get_adapter("lever").fetch(client, f"lever:{info['site']}", {"site": info["site"], "region": info.get("region", "us")})
        else:
            res = get_adapter("ashby").fetch(client, f"ashby:{info['board']}", {"board": info["board"]})
        for p in res.postings:
            if p.external_id == info["job_id"]:
                return p
    return None


def cmd_match(args):
    from jobhunter.models import ExplanationBatch
    from jobhunter.services.matching import engine

    ctx, db = _open(args)
    if args.match_cmd == "run":
        emit(args, engine.run_matching(ctx, db, use_model=True if args.model else None))
    elif args.match_cmd == "pending":
        pend = engine.pending_for_model(db, limit=args.limit)
        emit(args, {"count": len(pend), "payload": engine.model_payload(db, pend), "schema": ExplanationBatch.model_json_schema(), "instructions": "Write explanations only from the listed facts; cite fact ids; then `jobhunter match submit --file explanations.json`."})
    elif args.match_cmd == "submit":
        data = json.loads(Path(args.file).read_text(encoding="utf-8"))
        batch = ExplanationBatch.model_validate(data)
        ok, problems = engine.validate_explanations(db, batch)
        n = engine.apply_explanations(db, ok, source="claude")
        emit(args, {"applied": n, "rejected": len(problems), "problems": problems})


def cmd_act(args):
    from jobhunter.services import actions

    ctx, db = _open(args)
    try:
        c = args.act_cmd
        if c == "open":
            emit(args, actions.open_job(db, args.id))
        elif c == "save":
            emit(args, actions.save_job(db, args.id, True))
        elif c == "unsave":
            emit(args, actions.save_job(db, args.id, False))
        elif c == "favorite":
            emit(args, actions.favorite_job(db, args.id, True))
        elif c == "unfavorite":
            emit(args, actions.favorite_job(db, args.id, False))
        elif c == "notes":
            from jobhunter.services import notes

            emit(args, notes.list_for_job(db, args.id))
        elif c == "dismiss":
            emit(args, actions.dismiss_job(db, args.id, args.reason, args.note))
        elif c == "undismiss":
            emit(args, actions.dismiss_job(db, args.id, undo=True))
        elif c == "applied":
            emit(args, actions.mark_applied(db, args.id, applied_at=args.when))
        elif c == "stage":
            emit(args, actions.set_stage(db, args.id, args.stage, actor="user", occurred_at=args.when, allow_backwards=True))
        elif c == "contacted":
            emit(args, actions.mark_contacted(db, args.id, args.who, args.channel, args.when))
        elif c == "note":
            from jobhunter.services import notes

            emit(args, notes.add(db, args.id, " ".join(args.text)))
        elif c == "undo":
            emit(args, actions.correct(db, args.event_id, note=args.note))
        elif c == "history":
            emit(args, actions.history(db, args.id))
    except actions.ActionError as exc:
        raise CliError(str(exc)) from exc


def cmd_apply(args):
    from jobhunter.services import drafts, interviewos

    ctx, db = _open(args)
    try:
        if args.apply_cmd == "prepare":
            emit(args, drafts.prepare(ctx, db, args.id, use_model=args.model, regenerate=args.regenerate))
        elif args.apply_cmd == "followup":
            emit(args, drafts.schedule_followup(db, args.id, days=args.days))
        elif args.apply_cmd == "handoff":
            emit(args, interviewos.export(ctx, db, args.id, include_messages=args.include_messages))
    except (drafts.DraftError, interviewos.HandoffError) as exc:
        raise CliError(str(exc)) from exc


def cmd_drafts(args):
    from jobhunter.services import drafts

    ctx, db = _open(args)
    try:
        if args.drafts_cmd == "list":
            emit(args, drafts.list_drafts(db, args.job))
        elif args.drafts_cmd == "outreach":
            emit(args, drafts.outreach(db, args.id, args.to, purpose="outreach"))
        elif args.drafts_cmd == "followup":
            emit(args, drafts.outreach(db, args.id, args.to, purpose="followup"))
        elif args.drafts_cmd == "sent":
            emit(args, drafts.mark_sent(db, args.draft_id))
        elif args.drafts_cmd == "due":
            emit(args, drafts.due_followups(db))
    except drafts.DraftError as exc:
        raise CliError(str(exc)) from exc


def cmd_schedule(args):
    from jobhunter.services import schedule, worker

    ctx, db = _open(args)
    try:
        c = args.schedule_cmd
        if c == "show":
            emit(args, schedule.describe(db))
        elif c == "set":
            emit(args, schedule.set_frequency(db, args.frequency, args.time))
        elif c == "timezone":
            emit(args, schedule.set_timezone(db, args.tz))
        elif c == "notify":
            emit(args, schedule.set_notify_mode(db, args.mode, args.digest_time))
        elif c == "enable":
            res = schedule.enable(db)
            emit(args, {**res, "note": "Schedule enabled. It only runs while `jobhunter worker run` is running; see `jobhunter schedule install --print`."})
        elif c == "pause":
            emit(args, schedule.pause(db))
        elif c == "resume":
            emit(args, schedule.resume(db))
        elif c == "disable":
            emit(args, schedule.disable(db))
        elif c == "install":
            t = worker.service_templates(ctx)
            if args.json:
                emit(args, t)
            else:
                print(f"# Detected platform: {t['platform']}\n# Nothing is installed automatically. Review, then install the one you want.\n")
                print("## Linux (systemd user service)\n" + t["systemd"])
                print("## macOS (launchd)\n" + t["launchd_instructions"] + t["launchd"])
                print("## Windows (Task Scheduler)\n" + t["windows"])
                print("## Any Unix (cron, no long-running process)\n" + t["cron"])
    except (ValueError, KeyError) as exc:
        raise CliError(str(exc)) from exc


def cmd_worker(args):
    from jobhunter.services import worker

    ctx, db = _open(args)
    if args.once:
        emit(args, worker.tick(ctx, db))
        return
    db.close()
    sys.exit(worker.run_forever(ctx))


def cmd_digest(args):
    from jobhunter.services import notifications
    from jobhunter.services.email import transport
    from jobhunter.services.secrets import SecretStore

    ctx, db = _open(args)
    try:
        c = args.digest_cmd
        if c == "retry":
            emit(args, notifications.send_digest(ctx, db, force=args.force, notification_id=args.id))
        elif c == "resolve":
            emit(args, notifications.resolve(db, args.id, args.outcome.replace("-", "_")))
        elif c == "address":
            emit(args, {**notifications.set_recipient(db, args.email), "note": "Digests can only ever go to this address. Change it with the same command."})
        elif c == "preview":
            emit(args, notifications.send_digest(ctx, db, dry_run=True))
        elif c == "send":
            if not args.confirm:
                raise CliError("Add --confirm to send a real email.")
            emit(args, notifications.send_digest(ctx, db))
        elif c == "smtp":
            password = os.environ.get("JOBHUNTER_SMTP_PASSWORD") or getpass.getpass("SMTP password (not echoed): ")
            emit(args, transport.configure_smtp(db, SecretStore(ctx.paths), args.host, args.port, args.username, password, args.from_address, args.tls))
        elif c == "history":
            emit(args, notifications.history(db))
        elif c == "status":
            emit(args, {"recipient": notifications.recipient(db), "transport": transport.describe(db)})
    except (notifications.DigestError, transport.TransportError) as exc:
        raise CliError(str(exc)) from exc


def cmd_email(args):
    from jobhunter.services.email import gmail, linking

    ctx, db = _open(args)
    try:
        c = args.email_cmd
        if c == "status":
            emit(args, gmail.describe(db))
        elif c == "connect":
            purpose = "read" if args.what == "gmail-read" else "send"
            emit(args, gmail.connect(ctx, db, purpose, args.client_secret, open_browser=not args.no_browser))
        elif c == "disconnect":
            emit(args, gmail.disconnect(ctx, db, "read" if args.what == "gmail-read" else "send"))
        elif c == "sync":
            emit(args, gmail.sync(ctx, db))
        elif c == "filter":
            emit(args, gmail.set_read_filter(db, args.query, args.label, args.since_days))
        elif c == "import":
            text = Path(args.file).read_text(encoding="utf-8")
            emit(args, linking.import_text_message(db, text, sender=args.sender or "", subject=args.subject or "", outgoing=args.outgoing))
        elif c == "pending":
            emit(args, linking.pending_confirmations(db))
        elif c == "confirm":
            emit(args, linking.apply_link(db, args.email_id, args.job_id, actor="user"))
        elif c == "reject":
            emit(args, linking.reject_link(db, args.email_id))
        elif c == "unlink":
            emit(args, linking.unlink(db, args.email_id))
    except gmail.GmailError as exc:
        raise CliError(str(exc)) from exc


def cmd_model(args):
    from jobhunter.services import consent
    from jobhunter.services.model import provider

    ctx, db = _open(args)
    c = args.model_cmd
    if c == "status":
        emit(args, provider.describe(ctx, db))
    elif c == "enable":
        consent.grant(db, "model_processing", {"scope": "job text and profile facts for explanations and drafts"})
        db.set_setting("model.unattended_enabled", True)
        emit(args, {"enabled": True, **provider.describe(ctx, db)})
    elif c == "disable":
        db.set_setting("model.unattended_enabled", False)
        consent.revoke(db, "model_processing")
        emit(args, {"enabled": False})
    elif c == "set":
        if args.provider:
            db.set_setting("model.provider", args.provider)
        if args.budget is not None:
            db.set_setting("model.budget_usd", float(args.budget))
        if args.model_name:
            db.set_setting("model.model", args.model_name)
        emit(args, provider.describe(ctx, db))


def cmd_consent(args):
    from jobhunter.services import consent

    ctx, db = _open(args)
    if args.consent_cmd == "list":
        emit(args, consent.summary(db))
    elif args.consent_cmd == "revoke":
        emit(args, {"revoked": consent.revoke(db, args.kind)})


def cmd_feedback(args):
    from jobhunter.services import feedback
    from jobhunter.services import preferences as prefs_svc

    ctx, db = _open(args)
    if args.feedback_cmd == "show":
        emit(args, feedback.summary(db))
    elif args.feedback_cmd == "apply":
        yes = args.answer.lower() in {"yes", "y"}
        if yes:
            prefs_svc.set_extra(db, args.key, True, "nice", reason="confirmed suggestion")
        else:
            prefs_svc.set_extra(db, args.key, False, "unset", reason="declined suggestion")
        emit(args, {"key": args.key, "applied": yes, "note": "Reversible with `jobhunter prefs revert " + args.key + "`."})


def cmd_attention(args):
    from jobhunter.services import views

    ctx, db = _open(args)
    emit(args, views.attention(db))


def cmd_next(args):
    from jobhunter.services import onboarding, views

    ctx, db = _open(args)
    nxt = onboarding.next_step(db)
    payload = {"demo": ctx.demo, "setup_next": nxt, "attention": views.attention(db) if nxt is None else None}
    emit(args, payload)


def cmd_dashboard(args):
    import threading
    import webbrowser

    import uvicorn

    from jobhunter.web.app import create_app

    ctx, db = _open(args)
    db.close()
    host, port = dashboard_bind()
    host = args.host or host
    port = args.port or port
    if host not in {"127.0.0.1", "localhost", "::1"}:
        print("WARNING: binding to a non-loopback address exposes your private data to the network. Stop and reconsider unless you set this up deliberately.", file=sys.stderr)
    app = create_app(ctx)
    url = f"http://{host}:{port}/"
    print(f"Hunter dashboard: {url}  (Ctrl+C to stop; a separate worker keeps running if you started one)")
    if not args.no_open:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=host, port=port, log_level="warning")


def cmd_data(args):
    from jobhunter.services import datamgmt

    ctx, db = _open(args)
    try:
        if args.data_cmd == "export":
            emit(args, datamgmt.export_all(ctx, db))
        elif args.data_cmd == "backup":
            emit(args, datamgmt.backup(ctx, db))
        elif args.data_cmd == "delete":
            emit(args, datamgmt.delete_all(ctx, db, args.confirm or ""))
        elif args.data_cmd == "reset-secrets":
            from jobhunter.services.secrets import reset_file_store

            emit(args, reset_file_store(ctx.paths, force=args.force))
    except ValueError as exc:
        raise CliError(str(exc)) from exc


def cmd_demo(args):
    from jobhunter.services import demo

    ctx, db = _open(args)
    if not ctx.demo:
        raise CliError("Demo commands need --demo (or JOBHUNTER_DEMO=1) so they never touch your real data.")
    try:
        if args.demo_cmd == "seed":
            emit(args, demo.seed(ctx, db))
        elif args.demo_cmd == "reset":
            emit(args, demo.reset(ctx, db))
    except RuntimeError as exc:
        raise CliError(str(exc)) from exc


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="jobhunter", description="Hunter: a private job-hunt companion.")
    p.add_argument("--version", action="version", version=f"jobhunter {__version__}")
    p.add_argument("--demo", action="store_true", help="use the separate demo data directory with synthetic data")
    p.add_argument("--home", help="override the private data directory (default ~/.jobhunteros)")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="overall state and honest totals").set_defaults(fn=cmd_status)
    sub.add_parser("doctor", help="environment checks").set_defaults(fn=cmd_doctor)
    sub.add_parser("attention", help="what needs your attention").set_defaults(fn=cmd_attention)
    sub.add_parser("next", help="next setup step or next actions (for the skill)").set_defaults(fn=cmd_next)

    s = sub.add_parser("setup", help="guided onboarding, one question at a time")
    ss = s.add_subparsers(dest="setup_cmd", required=True)
    ss.add_parser("next")
    ss.add_parser("status")
    a = ss.add_parser("answer")
    a.add_argument("key")
    a.add_argument("value", nargs="+")
    b = ss.add_parser("basics", help="the combined answer: location, countries, salary, remote, frequency, notify")
    b.add_argument("--location")
    b.add_argument("--countries")
    b.add_argument("--salary")
    b.add_argument("--remote")
    b.add_argument("--frequency")
    b.add_argument("--notify")
    b.add_argument("--timezone")
    b.add_argument("--linkedin")
    s.set_defaults(fn=cmd_setup)

    d = sub.add_parser("docs", help="your original documents")
    ds = d.add_subparsers(dest="docs_cmd", required=True)
    x = ds.add_parser("add")
    x.add_argument("--kind", required=True, choices=["cv", "linkedin", "project", "other"])
    x.add_argument("path")
    x.add_argument("--label")
    x = ds.add_parser("list")
    x.add_argument("--all", action="store_true")
    x = ds.add_parser("text")
    x.add_argument("id", type=int)
    ds.add_parser("verify")
    x = ds.add_parser("linkedin")
    x.add_argument("--url")
    x.add_argument("--file")
    x.add_argument("--text")
    d.set_defaults(fn=cmd_docs)

    e = sub.add_parser("evidence", help="optional project evidence")
    es = e.add_subparsers(dest="evidence_cmd", required=True)
    x = es.add_parser("github")
    x.add_argument("repos", nargs="+")
    e.set_defaults(fn=cmd_evidence)

    pr = sub.add_parser("profile", help="the candidate profile built from your materials")
    ps = pr.add_subparsers(dest="profile_cmd", required=True)
    ps.add_parser("build")
    x = ps.add_parser("show")
    x.add_argument("--approved", action="store_true")
    x = ps.add_parser("approve")
    x.add_argument("--summary")
    ps.add_parser("questions")
    x = ps.add_parser("answer")
    x.add_argument("key")
    x.add_argument("answer", nargs="+")
    x = ps.add_parser("skip")
    x.add_argument("key")
    x = ps.add_parser("correct")
    x.add_argument("text", nargs="+")
    x.add_argument("--category", default="other")
    x.add_argument("--kind", default="unknown")
    x = ps.add_parser("submit")
    x.add_argument("--file", required=True)
    ps.add_parser("schema")
    pr.set_defaults(fn=cmd_profile)

    pf = sub.add_parser("prefs", help="preferences (must-have vs nice-to-have)")
    pfs = pf.add_subparsers(dest="prefs_cmd", required=True)
    pfs.add_parser("show")
    x = pfs.add_parser("set")
    x.add_argument("key")
    x.add_argument("value", nargs="+")
    x.add_argument("--strength", choices=["must", "nice", "unset"])
    x.add_argument("--reason")
    x = pfs.add_parser("revert")
    x.add_argument("key")
    pfs.add_parser("history")
    pf.set_defaults(fn=cmd_prefs)

    so = sub.add_parser("sources", help="job sources and their health")
    sos = so.add_subparsers(dest="sources_cmd", required=True)
    sos.add_parser("list")
    sos.add_parser("registry")
    sos.add_parser("sync-catalog")
    for name in ("enable", "disable", "check"):
        x = sos.add_parser(name)
        x.add_argument("id")
    x = sos.add_parser("add")
    x.add_argument("--kind", required=True)
    x.add_argument("--token", required=True)
    x.add_argument("--name", required=True)
    x.add_argument("--region")
    x.add_argument("--careers")
    so.set_defaults(fn=cmd_sources)

    em = sub.add_parser("employers", help="employers Hunter knows")
    em.add_argument("--origin", choices=["catalogue", "discovered", "user"])
    em.add_argument("--limit", type=int, default=100)
    em.set_defaults(fn=cmd_employers)

    h = sub.add_parser("hunt", help="find jobs now")
    hs = h.add_subparsers(dest="hunt_cmd", required=True)
    x = hs.add_parser("run")
    x.add_argument("--source", action="append")
    x.add_argument("--trigger", default="manual")
    hs.add_parser("status")
    x = hs.add_parser("history")
    x.add_argument("--limit", type=int, default=10)
    h.set_defaults(fn=cmd_hunt)

    j = sub.add_parser("jobs", help="browse and import jobs")
    js = j.add_subparsers(dest="jobs_cmd", required=True)
    x = js.add_parser("list")
    x.add_argument("--view", default="new", choices=["new", "favorites", "saved", "applied", "interviews", "finished", "all", "dismissed", "excluded", "uncertain", "old", "closed"])
    x.add_argument("--group", choices=["good", "worth_a_shot", "backup"])
    x.add_argument("--query", help="matches title, employer, location, or description")
    x.add_argument("--remote", choices=["remote", "hybrid", "onsite"])
    x.add_argument("--age", choices=["2", "7", "14"], help="posted within N days")
    x.add_argument("--country", help="two-letter country code")
    x.add_argument("--favorites", action="store_true", help="only starred jobs")
    x.add_argument("--saved", action="store_true", help="only bookmarked jobs")
    x.add_argument("--page", type=int, default=1)
    x.add_argument("--limit", type=int, default=10)
    x.add_argument("--sort", default="best", choices=["best", "newest", "added", "employer"])
    x = js.add_parser("show")
    x.add_argument("id", type=int)
    x = js.add_parser("import")
    x.add_argument("--text")
    x.add_argument("--file")
    x.add_argument("--url")
    x.add_argument("--title")
    x.add_argument("--company")
    x.add_argument("--location")
    x = js.add_parser("import-alert")
    x.add_argument("--file", required=True)
    x = js.add_parser("lookup")
    x.add_argument("id", type=int)
    j.set_defaults(fn=cmd_jobs)

    m = sub.add_parser("match", help="matching and explanations")
    ms = m.add_subparsers(dest="match_cmd", required=True)
    x = ms.add_parser("run")
    x.add_argument("--model", action="store_true", help="also ask the unattended Claude provider for explanations")
    x = ms.add_parser("pending")
    x.add_argument("--limit", type=int, default=12)
    x = ms.add_parser("submit")
    x.add_argument("--file", required=True)
    m.set_defaults(fn=cmd_match)

    ac = sub.add_parser("act", help="save, dismiss, applied, notes, undo")
    acs = ac.add_subparsers(dest="act_cmd", required=True)
    for name in ("open", "save", "unsave", "favorite", "unfavorite", "undismiss", "history", "notes"):
        x = acs.add_parser(name)
        x.add_argument("id", type=int)
    x = acs.add_parser("dismiss")
    x.add_argument("id", type=int)
    x.add_argument("--reason", choices=["consultancy", "too_senior", "too_junior", "location", "salary", "employer", "other"])
    x.add_argument("--note")
    x = acs.add_parser("applied")
    x.add_argument("id", type=int)
    x.add_argument("--when")
    x = acs.add_parser("stage")
    x.add_argument("id", type=int)
    x.add_argument("stage", choices=["prepared", "applied", "replied", "interview", "offer", "rejected", "withdrawn"])
    x.add_argument("--when")
    x = acs.add_parser("contacted")
    x.add_argument("id", type=int)
    x.add_argument("--who", required=True)
    x.add_argument("--channel", default="email")
    x.add_argument("--when")
    x = acs.add_parser("note")
    x.add_argument("id", type=int)
    x.add_argument("text", nargs="+")
    x = acs.add_parser("undo")
    x.add_argument("event_id", type=int)
    x.add_argument("--note")
    ac.set_defaults(fn=cmd_act)

    ap = sub.add_parser("apply", help="prepare an application (never submits)")
    aps = ap.add_subparsers(dest="apply_cmd", required=True)
    x = aps.add_parser("prepare")
    x.add_argument("id", type=int)
    x.add_argument("--model", action="store_true")
    x.add_argument("--regenerate", action="store_true", help="make a new version even if nothing changed")
    x = aps.add_parser("followup")
    x.add_argument("id", type=int)
    x.add_argument("--days", type=int, default=7)
    x = aps.add_parser("handoff")
    x.add_argument("id", type=int)
    x.add_argument("--include-messages", action="store_true")
    ap.set_defaults(fn=cmd_apply)

    dr = sub.add_parser("drafts", help="outreach and follow-up drafts")
    drs = dr.add_subparsers(dest="drafts_cmd", required=True)
    x = drs.add_parser("list")
    x.add_argument("--job", type=int)
    for name in ("outreach", "followup"):
        x = drs.add_parser(name)
        x.add_argument("id", type=int)
        x.add_argument("--to")
    x = drs.add_parser("sent")
    x.add_argument("draft_id", type=int)
    drs.add_parser("due")
    dr.set_defaults(fn=cmd_drafts)

    sc = sub.add_parser("schedule", help="how often Hunter looks and when it tells you")
    scs = sc.add_subparsers(dest="schedule_cmd", required=True)
    scs.add_parser("show")
    x = scs.add_parser("set")
    x.add_argument("frequency", choices=["manual", "daily", "every_2h", "every_4h", "every_6h"])
    x.add_argument("--time", help="HH:MM local time for daily runs")
    x = scs.add_parser("timezone")
    x.add_argument("tz")
    x = scs.add_parser("notify")
    x.add_argument("mode", choices=["dashboard", "daily_email", "on_request"])
    x.add_argument("--digest-time")
    for name in ("enable", "pause", "resume", "disable"):
        scs.add_parser(name)
    x = scs.add_parser("install")
    x.add_argument("--print", action="store_true", default=True)
    sc.set_defaults(fn=cmd_schedule)

    w = sub.add_parser("worker", help="the background worker process")
    ws = w.add_subparsers(dest="worker_cmd", required=True)
    x = ws.add_parser("run")
    x.add_argument("--once", action="store_true", help="one due-check, then exit (for cron)")
    w.set_defaults(fn=cmd_worker)

    dg = sub.add_parser("digest", help="email digests to your confirmed address")
    dgs = dg.add_subparsers(dest="digest_cmd", required=True)
    x = dgs.add_parser("address")
    x.add_argument("email")
    dgs.add_parser("preview")
    x = dgs.add_parser("send")
    x.add_argument("--confirm", action="store_true")
    x = dgs.add_parser("smtp")
    x.add_argument("--host", required=True)
    x.add_argument("--port", type=int, default=587)
    x.add_argument("--username", required=True)
    x.add_argument("--from-address", required=True)
    x.add_argument("--tls", default="starttls", choices=["starttls", "ssl", "none"])
    dgs.add_parser("history")
    dgs.add_parser("status")
    x = dgs.add_parser("retry", help="send a failed or uncertain digest again (asks for --force when it may already have arrived)")
    x.add_argument("id", type=int)
    x.add_argument("--force", action="store_true")
    x = dgs.add_parser("resolve", help="tell Hunter whether an uncertain digest arrived")
    x.add_argument("id", type=int)
    x.add_argument("outcome", choices=["arrived", "not-arrived"])
    dg.set_defaults(fn=cmd_digest)

    el = sub.add_parser("email", help="read your job-search emails (Gmail, read-only) or import them")
    els = el.add_subparsers(dest="email_cmd", required=True)
    els.add_parser("status")
    x = els.add_parser("connect")
    x.add_argument("what", choices=["gmail-read", "gmail-send"])
    x.add_argument("--client-secret", required=True, help="path to your Google OAuth desktop-app client secret JSON")
    x.add_argument("--no-browser", action="store_true")
    x = els.add_parser("disconnect")
    x.add_argument("what", choices=["gmail-read", "gmail-send"])
    els.add_parser("sync")
    x = els.add_parser("filter")
    x.add_argument("--query")
    x.add_argument("--label")
    x.add_argument("--since-days", type=int)
    x = els.add_parser("import")
    x.add_argument("--file", required=True)
    x.add_argument("--sender")
    x.add_argument("--subject")
    x.add_argument("--outgoing", action="store_true")
    els.add_parser("pending")
    x = els.add_parser("confirm")
    x.add_argument("email_id", type=int)
    x.add_argument("job_id", type=int)
    for name in ("reject", "unlink"):
        x = els.add_parser(name)
        x.add_argument("email_id", type=int)
    el.set_defaults(fn=cmd_email)

    mo = sub.add_parser("model", help="unattended Claude explanations (optional)")
    mos = mo.add_subparsers(dest="model_cmd", required=True)
    for name in ("status", "enable", "disable"):
        mos.add_parser(name)
    x = mos.add_parser("set")
    x.add_argument("--provider", choices=["auto", "claude-cli", "anthropic-api"])
    x.add_argument("--budget", type=float)
    x.add_argument("--model-name")
    mo.set_defaults(fn=cmd_model)

    co = sub.add_parser("consent", help="what you allowed")
    cos = co.add_subparsers(dest="consent_cmd", required=True)
    cos.add_parser("list")
    x = cos.add_parser("revoke")
    x.add_argument("kind")
    co.set_defaults(fn=cmd_consent)

    fb = sub.add_parser("feedback", help="what Hunter learned from your dismissals")
    fbs = fb.add_subparsers(dest="feedback_cmd", required=True)
    fbs.add_parser("show")
    x = fbs.add_parser("apply")
    x.add_argument("key")
    x.add_argument("answer")
    fb.set_defaults(fn=cmd_feedback)

    db_ = sub.add_parser("dashboard", help="open the local dashboard")
    db_.add_argument("--host")
    db_.add_argument("--port", type=int)
    db_.add_argument("--no-open", action="store_true")
    db_.set_defaults(fn=cmd_dashboard)

    da = sub.add_parser("data", help="export, backup, delete")
    das = da.add_subparsers(dest="data_cmd", required=True)
    das.add_parser("export")
    das.add_parser("backup")
    x = das.add_parser("delete")
    x.add_argument("--confirm", help="type DELETE")
    x = das.add_parser("reset-secrets", help="move an unreadable secrets file aside (kept, never deleted) and start fresh")
    x.add_argument("--force", action="store_true", help="move it aside even when it can be read")
    da.set_defaults(fn=cmd_data)

    de = sub.add_parser("demo", help="synthetic demo (requires --demo)")
    des = de.add_subparsers(dest="demo_cmd", required=True)
    des.add_parser("seed")
    des.add_parser("reset")
    de.set_defaults(fn=cmd_demo)
    return p


def _add_json_everywhere(parser: argparse.ArgumentParser) -> None:
    """Let --json appear after any subcommand too (default suppressed so it never overrides the global)."""
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for sub in action.choices.values():
                if not any(a.dest == "json" for a in sub._actions):
                    sub.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
                _add_json_everywhere(sub)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    _add_json_everywhere(parser)
    args = parser.parse_args(argv)
    if not hasattr(args, "json"):
        args.json = False
    try:
        args.fn(args)
        return 0
    except (CliError, SecretStoreLocked) as exc:
        if args.json:
            print(json.dumps({"error": str(exc)}))
        else:
            print(f"Hunter: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
