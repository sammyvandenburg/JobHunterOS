"""The local dashboard: server-rendered pages over the same services as the CLI.

Layout: left sidebar navigation, central workspace, contextual right panel.
All state changes are POSTs with CSRF; favorite/save/notes also answer JSON
for the small progressive-enhancement script so pages update in place.
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlencode

from fastapi import FastAPI, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from jobhunter import __version__
from jobhunter.config import MAX_DOCUMENT_BYTES, AppContext, dashboard_secret
from jobhunter.db import connect
from jobhunter.services import (
    actions,
    autorun,
    consent,
    drafts,
    feedback,
    hunt,
    interviewos,
    notes,
    notifications,
    onboarding,
    schedule,
    stats,
    views,
    worker,
)
from jobhunter.services import documents as docs
from jobhunter.services import preferences as prefs_svc
from jobhunter.services import profile as profile_svc
from jobhunter.services.email import gmail, google_client, linking, transport
from jobhunter.services.matching import engine
from jobhunter.services.model import provider as model_provider
from jobhunter.services.normalize import country_names
from jobhunter.services.secrets import SecretStore
from jobhunter.util import now_iso, parse_iso
from jobhunter.web.security import LocalOnlyMiddleware, check_csrf

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(HERE / "templates"))


def _countries_input(codes) -> str:
    """A value the countries box can be saved back from: 'EU' for the whole EU/EEA, else country names."""
    from jobhunter.services.normalize import COUNTRY_NAMES
    from jobhunter.services.preferences import EU_EEA

    codes = list(codes or [])
    if codes and set(codes) == set(EU_EEA):
        return "EU"
    return ", ".join(COUNTRY_NAMES.get(c, c) for c in codes)


def _dt(value):
    """Short human date: '18 Oct 12:35' or '—'."""
    d = parse_iso(value) if isinstance(value, str) else None
    if not d:
        return ""
    return d.strftime("%d %b %H:%M")


def _countries(codes):
    if not codes:
        return "not set"
    return country_names(list(codes))


templates.env.filters["dt"] = _dt
templates.env.filters["countries"] = _countries
templates.env.filters["countries_input"] = _countries_input

LIST_PAGES = {
    "new": ("New jobs", "Ranked for you. Best match first; fresh jobs are highlighted.", "search", "No new jobs right now", "Press <strong>Find jobs now</strong>, or check <a href='/jobs/old'>older matches</a> and <a href='/jobs/excluded'>what was left out</a>."),
    "favorites": ("Favorites", "Jobs you starred. They stay here even when they get older.", "star", "No favorites yet", "Press the star on any job to keep it here."),
    "saved": ("Saved", "Jobs you want to come back to later.", "bookmark", "Nothing saved yet", "Press the bookmark on any job to keep it here."),
    "finished": ("Finished", "Offers, rejections, and withdrawn applications.", "flag", "Nothing finished yet", "Finished applications will show here."),
    "old": ("Older matches", "Recommended jobs posted more than 14 days ago, or without a date.", "clock", "No older matches", "Everything recommended is recent."),
    "excluded": ("Left out, and why", "Jobs Hunter decided not to recommend. The reason is shown on each one.", "x", "Nothing was left out", ""),
    "uncertain": ("Needs checking", "Hunter could not tell where these jobs allow people to be, or the title is unusual. Open each one and check it yourself.", "alert", "Nothing to check", "Jobs with an unclear location or an unusual title will wait here."),
    "dismissed": ("Not interested", "Jobs you dismissed. You can bring any back.", "x", "Nothing dismissed", ""),
    "closed": ("No longer listed", "Jobs that disappeared from their source.", "clock", "Nothing closed", ""),
    "all": ("Everything found", "Every unique vacancy Hunter has seen.", "briefcase", "Nothing found yet", "Run a search first."),
    "interviews": ("Interviews", "Applications at the interview stage.", "send", "No interviews recorded", ""),
}


def create_app(ctx: AppContext) -> FastAPI:
    app = FastAPI(title="Hunter", docs_url=None, redoc_url=None, openapi_url=None)
    secret = dashboard_secret(ctx.paths)
    app.add_middleware(LocalOnlyMiddleware, secret=secret)
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    def db():
        return connect(ctx.paths.db_path)

    def render(request: Request, name: str, active: str | None = None, **kw):
        d = db()
        try:
            t = stats.totals(d)
            att_count = views.attention(d)["count"]
            new_count = views.list_jobs(d, view="new", per_page=1)["total"]
            dash = views.dashboard(d) if name == "dashboard.html" else None
            last_ok = hunt.last_successful_run(d)
            sched = schedule.describe(d)
            status = "Searching now" if d.fetchone("SELECT id FROM hunt_runs WHERE status = 'running'") else ("Watching (worker running)" if str(sched.get("worker", "")).startswith("running") else "Idle")
            base = {
                "request": request,
                "csrf": request.state.csrf,
                "demo": ctx.demo,
                "version": __version__,
                "active": active,
                "navitems": [
                    ("dashboard", "Dashboard", "home", "/", None),
                    ("new", "New jobs", "search", "/jobs/new", new_count or None),
                    ("favorites", "Favorites", "star", "/jobs/favorites", t["favorites"] or None),
                    ("saved", "Saved", "bookmark", "/jobs/saved", t["saved"] or None),
                    ("applied", "Applied", "send", "/jobs/applied", t["applied"] or None),
                    ("finished", "Finished", "flag", "/jobs/finished", None),
                    ("attention", "Needs attention", "bell", "/attention", att_count or None),
                    ("settings", "Profile & settings", "settings", "/profile", None),
                ],
                "totals": t,
                "attention_count": att_count,
                "hunter_status": status,
                "last_search": _dt(last_ok["finished_at"]) if last_ok else None,
                "flash": request.query_params.get("msg"),
                "d": dash,
            }
        finally:
            d.close()
        base.update(kw)
        return templates.TemplateResponse(request, name, base)

    app.state.autorun = autorun.Autorun(ctx)
    if not ctx.demo:
        try:
            _d = db()
            try:
                if (schedule.get(_d) or {}).get("enabled"):
                    app.state.autorun.start()
            finally:
                _d.close()
        except Exception:
            pass

    def guard(request: Request, token: str | None):
        if not check_csrf(request, token):
            return PlainTextResponse("Form expired or invalid. Go back and try again.", status_code=403)
        return None

    def wants_json(request: Request) -> bool:
        return request.headers.get("x-requested-with") == "fetch" or "application/json" in request.headers.get("accept", "")

    # ---- pages ------------------------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    def home(request: Request):
        d = db()
        try:
            nxt = onboarding.next_step(d)
        finally:
            d.close()
        if nxt and not ctx.demo and nxt["key"] in {"readiness", "documents"}:
            return render(request, "welcome.html", active="dashboard", step=nxt)
        if nxt and not ctx.demo and nxt["key"] in {"profile_approve", "basics"}:
            return RedirectResponse("/profile", status_code=303)
        return render(request, "dashboard.html", active="dashboard")

    @app.get("/jobs/{view}", response_class=HTMLResponse)
    def jobs_view(request: Request, view: str):
        if view == "applied":
            d = db()
            try:
                rows = views.applied_rows(d)
            finally:
                d.close()
            return render(request, "applied.html", active="applied", rows=rows)
        if view not in LIST_PAGES:
            return PlainTextResponse("No such list.", status_code=404)
        q = request.query_params
        try:
            page = max(1, int(q.get("page", "1") or 1))
        except ValueError:
            page = 1
        d = db()
        try:
            res = views.list_jobs(
                d,
                view=view,
                group=q.get("group") or None,
                query=q.get("q") or None,
                page=page,
                per_page=20,
                sort=q.get("sort") or "best",
                remote=q.get("remote") or None,
                age=q.get("age") or None,
                country=q.get("country") or None,
                fav_only=q.get("fav") == "1",
                saved_only=q.get("savedonly") == "1",
            )
            last = hunt.last_run(d)
        finally:
            d.close()
        heading, sub, empty_icon, empty_title, empty_text = LIST_PAGES[view]
        qs = urlencode([(k, v) for k, v in q.multi_items() if k != "page" and v])
        active = view if view in {"new", "favorites", "saved", "finished"} else "new"
        return render(request, "jobs.html", active=active, view=view, res=res, last_run=last, heading=heading, subheading=sub, empty_icon=empty_icon, empty_title=empty_title, empty_text=empty_text, qs=qs)

    @app.get("/job/{job_id}", response_class=HTMLResponse)
    def job_page(request: Request, job_id: int):
        d = db()
        try:
            j = views.job_detail(d, job_id)
        finally:
            d.close()
        if not j:
            return PlainTextResponse("Job not found", status_code=404)
        active = "favorites" if j["favorited"] else ("saved" if j["saved"] else ("applied" if j["stage"] else "new"))
        return render(request, "job.html", active=active, job=j)

    @app.get("/go/{job_id}")
    def go(job_id: int):
        d = db()
        try:
            try:
                res = actions.open_job(d, job_id)
            except actions.ActionError:
                return PlainTextResponse("Job not found", status_code=404)
        finally:
            d.close()
        url = res.get("url") or ""
        if url.startswith("https://") or url.startswith("http://"):
            return RedirectResponse(url, status_code=303)
        return PlainTextResponse("This job has no link. See 'Where this job was seen' on the job page.", status_code=404)

    @app.get("/attention", response_class=HTMLResponse)
    def attention_page(request: Request):
        d = db()
        try:
            att = views.attention(d)
        finally:
            d.close()
        return render(request, "attention.html", active="attention", att=att)

    @app.get("/profile", response_class=HTMLResponse)
    def profile_page(request: Request):
        d = db()
        try:
            row = profile_svc.current_draft(d) or profile_svc.approved(d)
            pv = profile_svc.profile_view(d, row) if row else None
            prefs = prefs_svc.get(d)
            kw = dict(profile=pv, prefs=prefs, pref_lines=prefs_svc.describe(prefs), documents=docs.list_documents(d), setup=onboarding.status(d), next_step=onboarding.next_step(d), consents=consent.summary(d), feedback=feedback.summary(d), linkedin_url=d.get_setting("linkedin.url"))
        finally:
            d.close()
        return render(request, "profile.html", active="settings", settings_tab="profile", **kw)

    @app.get("/connections", response_class=HTMLResponse)
    def connections_page(request: Request):
        d = db()
        try:
            secrets_info = SecretStore(ctx.paths).describe()
            if not secrets_info.get("readable", True):
                # A locked store (wrong passphrase, damaged file) must be visible, not a silent "not set".
                secrets_info["note"] = f"{secrets_info['note']} {secrets_info['problem']}"
            g = gmail.describe(d)
            rec = notifications.recipient(d)
            tr = transport.describe(d)
            auto = autorun.status(ctx, d, app.state.autorun)
            problems = []
            if g["send"]["connected"] and g["send"].get("last_error"):
                problems.append(g["send"]["last_error"])
            if g["read"]["connected"] and g["read"].get("last_error"):
                problems.append("Mailbox reading: " + g["read"]["last_error"])
            unresolved = notifications.unresolved(d)
            if unresolved:
                problems.append(f"{len(unresolved)} email digest(s) may or may not have arrived. See Needs attention.")
            if auto["on"] and auto["label"].startswith("on, but"):
                problems.append("Automatic hunting is on, but nothing is running it.")
            status = {
                "connected": g["send"]["connected"],
                "sending": bool(g["send"]["connected"] and rec and tr["configured"]),
                "automatic": auto["label"],
                "problems": problems,
            }
            kw = dict(gmail=g, client=google_client.describe(ctx.paths), suggested=d.get_setting("digest.suggested_recipient"), digest={"recipient": rec, "transport": tr}, model=model_provider.describe(ctx, d), secrets=secrets_info, sched=schedule.describe(d), auto=auto, status=status, pending=linking.pending_confirmations(d), notifications=notifications.history(d, 10))
        finally:
            d.close()
        return render(request, "connections.html", active="settings", settings_tab="connections", **kw)

    @app.get("/connect/gmail/start")
    def gmail_start(request: Request, purpose: str = "send", csrf: str = ""):
        bad = guard(request, csrf)
        if bad:
            return bad
        if ctx.demo:
            return RedirectResponse(f"/connections?msg={_q('Demo mode does not connect real accounts.')}", status_code=303)
        d = db()
        try:
            try:
                redirect_uri = str(request.base_url).rstrip("/") + "/connect/gmail/callback"
                res = gmail.begin_connect(ctx, d, purpose, redirect_uri)
            except (gmail.GmailError, google_client.ClientConfigError) as exc:
                return RedirectResponse(f"/connections?msg={_q(str(exc))}", status_code=303)
        finally:
            d.close()
        return RedirectResponse(res["url"], status_code=303)

    @app.get("/connect/gmail/callback")
    def gmail_callback(request: Request, state: str = "", code: str = "", error: str = ""):
        d = db()
        try:
            try:
                res = gmail.finish_connect(ctx, d, state or None, code or None, error or None)
                if res["purpose"] == "send":
                    msg = f"Gmail connected as {res['address']}." + (" Should I send your job emails there? Say yes below." if res.get("suggested_recipient") else "")
                else:
                    msg = f"Gmail reading connected as {res['address']}."
            except (gmail.GmailError, google_client.ClientConfigError) as exc:
                msg = str(exc)
        finally:
            d.close()
        return RedirectResponse(f"/connections?msg={_q(msg)}#email", status_code=303)

    @app.get("/health", response_class=HTMLResponse)
    def health_page(request: Request):
        d = db()
        try:
            runs = d.fetchall("SELECT id, trigger, status, started_at, finished_at, stats_json, error FROM hunt_runs ORDER BY id DESC LIMIT 10")
            for r in runs:
                r["stats"] = json.loads(r.pop("stats_json") or "{}")
            kw = dict(sources=hunt.source_health(d), runs=runs, sched=schedule.describe(d), gmail=gmail.describe(d), employers=d.fetchone("SELECT COUNT(*) AS n, SUM(CASE WHEN origin='discovered' THEN 1 ELSE 0 END) AS discovered FROM employers"))
        finally:
            d.close()
        return render(request, "health.html", active="settings", settings_tab="health", **kw)

    @app.get("/digest/preview", response_class=HTMLResponse)
    def digest_preview(request: Request):
        d = db()
        try:
            jobs = notifications.pending_jobs(d)
            dg = notifications.compose(d, jobs) if jobs else None
            kw = dict(digest=dg, recipient=notifications.recipient(d), transport=transport.describe(d))
        finally:
            d.close()
        return render(request, "digest.html", active="settings", **kw)

    @app.get("/api/status")
    def api_status():
        d = db()
        try:
            return JSONResponse({"totals": stats.totals(d), "attention": views.attention(d)["count"], "schedule": schedule.describe(d), "running": hunt.running_run(d)})
        finally:
            d.close()

    # ---- job actions ------------------------------------------------------
    @app.post("/do/job/{job_id}/notes")
    def note_add(request: Request, job_id: int, csrf: str = Form(...), body: str = Form("")):
        bad = guard(request, csrf)
        if bad:
            return JSONResponse({"error": "Form expired. Reload the page."}, status_code=403) if wants_json(request) else bad
        d = db()
        try:
            try:
                n = notes.add(d, job_id, body)
                if wants_json(request):
                    return JSONResponse({"note_id": n["id"], "when": _dt(n["updated_at"])})
                msg = "Note saved."
            except notes.NoteError as exc:
                if wants_json(request):
                    return JSONResponse({"error": str(exc)}, status_code=400)
                msg = str(exc)
        finally:
            d.close()
        return RedirectResponse(f"/job/{job_id}?msg={_q(msg)}#notes", status_code=303)

    @app.post("/do/job/{job_id}/{action}")
    def job_action(request: Request, job_id: int, action: str, csrf: str = Form(...), reason: str = Form(None), note: str = Form(None), when: str = Form(None), stage: str = Form(None)):
        bad = guard(request, csrf)
        if bad:
            return JSONResponse({"error": "Form expired. Reload the page."}, status_code=403) if wants_json(request) else bad
        d = db()
        msg = ""
        payload = None
        try:
            if action in {"favorite", "unfavorite", "save", "unsave"}:
                on = action in {"favorite", "save"}
                kind = "favorite" if "favorite" in action else "save"
                if kind == "favorite":
                    actions.favorite_job(d, job_id, on)
                    msg = "Added to favorites." if on else "Removed from favorites."
                else:
                    actions.save_job(d, job_id, on)
                    msg = "Saved for later." if on else "Removed from saved."
                t = stats.totals(d)
                payload = {"job_id": job_id, "kind": kind, "on": on, "message": msg, "count": t["favorites"] if kind == "favorite" else t["saved"]}
            elif action == "dismiss":
                res = actions.dismiss_job(d, job_id, reason or None, note or None)
                msg = "Okay, I will not show this one again." + (" I have a quick question for you under Needs attention." if res.get("suggestions") else "")
            elif action == "undismiss":
                actions.dismiss_job(d, job_id, undo=True)
                msg = "Back in your list."
            elif action == "applied":
                actions.mark_applied(d, job_id, applied_at=when or None)
                msg = "Recorded: you applied."
            elif action == "prepare":
                pkg = drafts.prepare(ctx, d, job_id, regenerate=(stage == "regenerate"))
                msg = "Here is your help to apply (below)." if not pkg.get("reused") else "Nothing changed since last time, so here is the same help to apply (below)."
            elif action == "stage" and stage:
                actions.set_stage(d, job_id, stage, actor="user", occurred_at=when or None, allow_backwards=True)
                msg = f"Status set to {stage}."
            elif action == "followup":
                drafts.schedule_followup(d, job_id)
                msg = "Follow-up reminder set for 7 days after you applied."
            elif action == "outreach":
                drafts.outreach(d, job_id, None)
                msg = "Message draft created. Review and send it yourself."
            elif action == "handoff":
                res = interviewos.export(ctx, d, job_id)
                msg = f"InterviewOS hand-off written to {res['path']}"
            elif action == "lookup":
                from jobhunter.services import jobs as jobs_svc
                from jobhunter.services import original_lookup

                res = original_lookup.resolve(d, jobs_svc.get_job(d, job_id))
                msg = ("Found the company's own listing." if res["status"] == "verified" else "I could not confirm the original listing.") + " " + res["how"]
            else:
                msg = "Unknown action."
        except (actions.ActionError, drafts.DraftError, interviewos.HandoffError) as exc:
            msg = str(exc)
            if wants_json(request):
                return JSONResponse({"error": msg}, status_code=400)
        finally:
            d.close()
        if wants_json(request) and payload:
            return JSONResponse(payload)
        anchor = "#apply" if action == "prepare" else ""
        back = request.headers.get("referer") or f"/job/{job_id}"
        target = back if (action in {"favorite", "unfavorite", "save", "unsave"} and back.startswith("http://127.0.0.1") or back.startswith("http://localhost")) else f"/job/{job_id}"
        if "?" in target:
            target = target.split("?")[0]
        return RedirectResponse(f"{target}?msg={_q(msg)}{anchor}", status_code=303)

    # ---- notes ------------------------------------------------------------
    @app.post("/do/note/{note_id}/update")
    def note_update(request: Request, note_id: int, csrf: str = Form(...), body: str = Form(""), job_id: int = Form(...)):
        bad = guard(request, csrf)
        if bad:
            return JSONResponse({"error": "Form expired. Reload the page."}, status_code=403) if wants_json(request) else bad
        d = db()
        try:
            try:
                n = notes.update(d, note_id, body)
                if wants_json(request):
                    return JSONResponse({"note_id": n["id"], "when": _dt(n["updated_at"])})
                msg = "Note updated."
            except notes.NoteError as exc:
                if wants_json(request):
                    return JSONResponse({"error": str(exc)}, status_code=400)
                msg = str(exc)
        finally:
            d.close()
        return RedirectResponse(f"/job/{job_id}?msg={_q(msg)}#notes", status_code=303)

    @app.post("/do/note/{note_id}/delete")
    def note_delete(request: Request, note_id: int, csrf: str = Form(...), job_id: int = Form(...)):
        bad = guard(request, csrf)
        if bad:
            return bad
        d = db()
        try:
            try:
                notes.delete(d, note_id)
                msg = "Note deleted."
            except notes.NoteError as exc:
                msg = str(exc)
        finally:
            d.close()
        return RedirectResponse(f"/job/{job_id}?msg={_q(msg)}#notes", status_code=303)

    @app.post("/do/event/{event_id}/undo")
    def undo_event(request: Request, event_id: int, csrf: str = Form(...), job_id: int = Form(...)):
        bad = guard(request, csrf)
        if bad:
            return bad
        d = db()
        try:
            actions.correct(d, event_id, note="undone in dashboard")
        finally:
            d.close()
        return RedirectResponse(f"/job/{job_id}?msg={_q('Undone.')}", status_code=303)

    @app.post("/do/hunt")
    def do_hunt(request: Request, csrf: str = Form(...)):
        bad = guard(request, csrf)
        if bad:
            return bad
        d = db()
        try:
            ok, why = onboarding.live_hunting_allowed(d, ctx.demo)
            if not ok:
                return RedirectResponse(f"/?msg={_q(why)}", status_code=303)
            if ctx.demo:
                return RedirectResponse(f"/?msg={_q('Demo mode uses made-up data; there is nothing to fetch.')}", status_code=303)
            try:
                res = hunt.start_background(ctx, trigger="manual")
                return RedirectResponse(f"/?run={res['run_id']}", status_code=303)
            except (hunt.HuntInProgress, hunt.HuntNotAllowed) as exc:
                msg = str(exc)
        finally:
            d.close()
        return RedirectResponse(f"/?msg={_q(msg)}", status_code=303)

    @app.get("/api/hunt/{run_id}")
    def api_hunt(run_id: int):
        d = db()
        try:
            prog = hunt.progress(d, run_id)
            if not prog:
                return JSONResponse({"error": "No such search."}, status_code=404)
            prog["totals"] = {"new": views.current_recommended_count(d)}
            return JSONResponse(prog)
        finally:
            d.close()

    # ---- setup ------------------------------------------------------------
    @app.post("/do/setup/answer")
    def setup_answer(request: Request, csrf: str = Form(...), key: str = Form(...), value: str = Form("")):
        bad = guard(request, csrf)
        if bad:
            return bad
        d = db()
        try:
            try:
                res = onboarding.answer(d, key, value)
                msg = res.get("note") or "Saved."
            except onboarding.AnswerError as exc:
                msg = str(exc)
        finally:
            d.close()
        target = "/profile" if key not in {"readiness", "documents"} else "/"
        return RedirectResponse(f"{target}?msg={_q(msg)}", status_code=303)

    @app.post("/do/upload-both")
    async def upload_both(request: Request, csrf: str = Form(...), cv: UploadFile | None = None, linkedin: UploadFile | None = None):
        bad = guard(request, csrf)
        if bad:
            return bad
        d = db()
        msgs = []
        try:
            for kind, f in (("cv", cv), ("linkedin", linkedin)):
                if f and f.filename:
                    data = await f.read(MAX_DOCUMENT_BYTES + 1)
                    try:
                        row = docs.add_document(d, ctx.paths, kind, data, f.filename, f.content_type)
                        label = "CV" if kind == "cv" else "LinkedIn PDF"
                        msgs.append({"ok": f"{label}: read OK.", "empty": f"{label}: I could not read text from it. {row['extraction_notes']}", "failed": f"{label}: could not read it. {row['extraction_notes']}", "unsupported": f"{label}: {row['extraction_notes']}"}[row["extraction_status"]])
                    except docs.DocumentError as exc:
                        msgs.append(f"{'CV' if kind == 'cv' else 'LinkedIn PDF'}: {exc}")
            if not msgs:
                msgs.append("Choose at least one file.")
            built = onboarding.ensure_profile_draft(d)
            ready, missing = onboarding.documents_ready(d)
            if built:
                msgs.append("I built your profile. Please check it.")
            elif not ready:
                msgs.append("Still missing: " + ", ".join(missing) + ".")
        finally:
            d.close()
        return RedirectResponse(("/profile" if ready else "/") + f"?msg={_q(' '.join(msgs))}", status_code=303)

    @app.post("/do/setup/basics")
    def setup_basics(request: Request, csrf: str = Form(...), location: str = Form(""), countries: str = Form(""), salary: str = Form(""), remote: str = Form(""), frequency: str = Form(""), notify: str = Form("")):
        bad = guard(request, csrf)
        if bad:
            return bad
        d = db()
        try:
            try:
                res = onboarding.basics(d, location=location or None, countries=countries or None, salary=salary or "skip", remote=remote or None, frequency=frequency or None, notify=notify or None)
                msg = "All set. " + (res.get("note") or "") + " Press Find jobs now."
                target = "/"
            except onboarding.AnswerError as exc:
                msg = str(exc)
                target = "/profile"
        finally:
            d.close()
        return RedirectResponse(f"{target}?msg={_q(msg)}", status_code=303)

    @app.post("/do/upload")
    async def upload(request: Request, csrf: str = Form(...), kind: str = Form(...), file: UploadFile | None = None, text: str = Form(""), url: str = Form("")):
        bad = guard(request, csrf)
        if bad:
            return bad
        d = db()
        try:
            if kind not in {"cv", "linkedin", "project"}:
                msg = "Unknown document kind."
            else:
                try:
                    if url and kind == "linkedin":
                        if url.startswith("https://") and "linkedin.com/in/" in url:
                            d.set_setting("linkedin.url", url.strip())
                        else:
                            raise docs.DocumentError("That does not look like a LinkedIn profile URL.")
                    if file and file.filename:
                        data = await file.read(MAX_DOCUMENT_BYTES + 1)
                        row = docs.add_document(d, ctx.paths, kind, data, file.filename, file.content_type)
                        msg = {"ok": "Document stored and read.", "empty": "Stored, but I could not read text from it: " + (row["extraction_notes"] or ""), "failed": "Could not read it: " + (row["extraction_notes"] or ""), "unsupported": row["extraction_notes"]}[row["extraction_status"]]
                    elif text.strip():
                        docs.add_text_document(d, ctx.paths, kind, text, f"{kind}-pasted", source_url=url or None)
                        msg = "Text stored."
                    elif url:
                        msg = "URL saved. I still need the profile content (an export file or pasted text); I never guess it from the URL."
                    else:
                        msg = "Choose a file or paste text."
                except docs.DocumentError as exc:
                    msg = str(exc)
        finally:
            d.close()
        return RedirectResponse(f"/profile?msg={_q(msg)}", status_code=303)

    @app.post("/do/profile/{action}")
    def profile_action(request: Request, action: str, csrf: str = Form(...), key: str = Form(None), answer: str = Form(None), text: str = Form(None), summary: str = Form(None)):
        bad = guard(request, csrf)
        if bad:
            return bad
        d = db()
        try:
            try:
                if action == "build":
                    draft = profile_svc.build_rule_based_draft(d)
                    profile_svc.save_draft(d, draft, built_by="rules")
                    msg = "I rebuilt your profile from your files. Please check it."
                elif action == "approve":
                    profile_svc.approve(d, summary_override=summary or None)
                    consent.grant(d, "profile_approved", {})
                    engine.rematch_quietly(ctx, d)
                    msg = "Thanks. Your profile is approved."
                elif action == "answer" and key and answer:
                    profile_svc.answer_question(d, key, answer)
                    msg = "Thanks, noted."
                elif action == "skip" and key:
                    profile_svc.skip_question(d, key)
                    msg = "Skipped."
                elif action == "correct" and text:
                    res = profile_svc.correct(d, text)
                    engine.rematch_quietly(ctx, d)
                    msg = ("Thanks. " + (res.get("note") or "Noted.") + " Your files are unchanged." + (" Your matches were checked again." if res.get("approved") else "")).strip()
                elif action == "rematch":
                    res = engine.run_matching(ctx, d)
                    msg = f"Re-checked: {res.get('computed', 0)} job(s) updated." if res.get("status") == "ok" else res.get("reason", "Not matched.")
                else:
                    msg = "Nothing to do."
            except profile_svc.ProfileError as exc:
                msg = str(exc)
        finally:
            d.close()
        return RedirectResponse(f"/profile?msg={_q(msg)}", status_code=303)

    @app.post("/do/prefs")
    def prefs_action(request: Request, csrf: str = Form(...), key: str = Form(...), value: str = Form(""), strength: str = Form("nice")):
        bad = guard(request, csrf)
        if bad:
            return bad
        d = db()
        try:
            from jobhunter.cli import CliError, cmd_prefs

            class A:
                pass

            a = A()
            a.demo, a.home, a.json = ctx.demo, str(ctx.paths.home), True
            a.prefs_cmd, a.key, a.value, a.strength, a.reason = "set", key, [value], strength, "dashboard"
            try:
                import contextlib
                import io

                with contextlib.redirect_stdout(io.StringIO()):
                    cmd_prefs(a)
                msg = f"Saved {key.replace('_', ' ')}."
            except CliError as exc:
                msg = str(exc)
        finally:
            d.close()
        return RedirectResponse(f"/profile?msg={_q(msg)}#preferences", status_code=303)

    @app.post("/do/prefs/basics")
    def prefs_basics(request: Request, csrf: str = Form(...), location: str = Form(""), countries: str = Form(""), salary: str = Form(""), salary_must: str = Form(""), remote_mode: str = Form(""), no_onsite: str = Form(""), no_hybrid: str = Form(""), timezone: str = Form("")):
        bad = guard(request, csrf)
        if bad:
            return bad
        d = db()
        try:
            words = {"remote_only": "remote only", "remote_preferred": "remote preferred", "no_preference": "don't mind"}.get(remote_mode, "")
            if words and no_onsite:
                words += ", never in the office"
            if words and no_hybrid:
                words += ", no hybrid"
            try:
                res = prefs_svc.update_basics(d, location=location, countries=countries, salary=salary, salary_must=bool(salary_must), remote_text=words or None, timezone=timezone)
                engine.rematch_quietly(ctx, d)
                msg = res["note"] + " Your matches were checked again."
            except ValueError as exc:
                msg = str(exc)
        finally:
            d.close()
        return RedirectResponse(f"/profile?msg={_q(msg)}#preferences", status_code=303)

    @app.post("/do/draft/{draft_id}/answer")
    def draft_answer(request: Request, draft_id: int, csrf: str = Form(...), job_id: int = Form(...), question: str = Form(...), text: str = Form("")):
        bad = guard(request, csrf)
        if bad:
            return JSONResponse({"error": "Form expired. Reload the page."}, status_code=403) if wants_json(request) else bad
        d = db()
        try:
            try:
                res = drafts.save_user_answer(d, draft_id, question, text)
                if wants_json(request):
                    return JSONResponse({"saved": res["saved"], "when": _dt(now_iso())})
                msg = "Saved your wording." if res["saved"] else "Back to Hunter's draft."
            except drafts.DraftError as exc:
                if wants_json(request):
                    return JSONResponse({"error": str(exc)}, status_code=400)
                msg = str(exc)
        finally:
            d.close()
        return RedirectResponse(f"/job/{job_id}?msg={_q(msg)}#apply", status_code=303)

    @app.post("/do/feedback/{key}")
    def feedback_apply(request: Request, key: str, csrf: str = Form(...), answer: str = Form(...)):
        bad = guard(request, csrf)
        if bad:
            return bad
        d = db()
        try:
            prefs_svc.set_extra(d, key, answer == "yes", "nice" if answer == "yes" else "unset", reason="confirmed in dashboard")
        finally:
            d.close()
        return RedirectResponse(f"/attention?msg={_q('Got it. You can change this on the profile page.')}", status_code=303)

    @app.post("/do/email/{email_id}/{action}")
    def email_action(request: Request, email_id: int, action: str, csrf: str = Form(...), job_id: int = Form(None)):
        bad = guard(request, csrf)
        if bad:
            return bad
        d = db()
        try:
            if action == "confirm" and job_id:
                linking.apply_link(d, email_id, job_id, actor="user")
                msg = "Linked."
            elif action == "reject":
                linking.reject_link(d, email_id)
                msg = "Ignored."
            elif action == "unlink":
                linking.unlink(d, email_id)
                msg = "Link undone."
            else:
                msg = "Nothing done."
        finally:
            d.close()
        return RedirectResponse(f"/attention?msg={_q(msg)}", status_code=303)

    @app.post("/do/schedule")
    def schedule_action(request: Request, csrf: str = Form(...), action: str = Form(...), frequency: str = Form(None), time_of_day: str = Form(None), notify_mode: str = Form(None), timezone: str = Form(None), plan: str = Form(None), email: str = Form(None), approve: str = Form(None), persist: str = Form(None)):
        bad = guard(request, csrf)
        if bad:
            return bad
        d = db()
        try:
            try:
                if action == "notify":
                    # One choice: how often Hunter looks, and whether it emails you.
                    plan = plan or "manual"
                    schedule.set_frequency(d, plan, time_of_day or None)
                    schedule.set_notify_mode(d, "daily_email" if email else "dashboard", time_of_day or None)
                    if plan == "manual":
                        schedule.disable(d)
                        app.state.autorun.stop()
                        msg = "Okay. Hunter only looks when you press Find jobs now."
                    else:
                        schedule.enable(d)
                        app.state.autorun.start()
                        words = {"daily": f"every day at {time_of_day or schedule.get(d).get('time_of_day')}", "every_2h": "every 2 hours", "every_4h": "every 4 hours", "every_6h": "every 6 hours"}[plan]
                        msg = f"Hunter will look {words}" + (" and email you what it finds." if email else " and show it here.") + " This runs while Hunter is open; turn on the background task to keep it running when Hunter is closed."
                elif action == "automatic_on":
                    if not approve:
                        msg = "Tick the box to allow automatic hunting first."
                    else:
                        if schedule.get(d).get("frequency") == "manual":
                            schedule.set_frequency(d, "daily")
                        schedule.enable(d)
                        app.state.autorun.start()
                        msg = "Automatic hunting is on. It runs while Hunter is open."
                        if persist:
                            res = worker.install_service(ctx)
                            msg += " " + res["note"]
                elif action == "automatic_off":
                    schedule.disable(d)
                    app.state.autorun.stop()
                    msg = "Automatic hunting is off."
                    if worker.service_status(ctx).get("installed"):
                        msg += " " + worker.uninstall_service(ctx)["note"]
                elif action == "service_install":
                    if not approve:
                        msg = "Tick the box to allow the background task first."
                    else:
                        if not schedule.get(d).get("enabled"):
                            if schedule.get(d).get("frequency") == "manual":
                                schedule.set_frequency(d, "daily")
                            schedule.enable(d)
                        msg = worker.install_service(ctx)["note"]
                elif action == "service_remove":
                    msg = worker.uninstall_service(ctx)["note"]
                elif action == "set":
                    if frequency:
                        schedule.set_frequency(d, frequency, time_of_day or None)
                    if notify_mode:
                        schedule.set_notify_mode(d, notify_mode)
                    if timezone:
                        schedule.set_timezone(d, timezone)
                    msg = "Schedule settings saved."
                elif action in {"enable", "pause", "resume", "disable"}:
                    getattr(schedule, action)(d)
                    msg = {"enable": "Schedule on. It runs only while the worker is running.", "pause": "Paused.", "resume": "Resumed.", "disable": "Schedule off."}[action]
                else:
                    msg = "Nothing done."
            except ValueError as exc:
                msg = str(exc)
        finally:
            d.close()
        return RedirectResponse(f"/connections?msg={_q(msg)}#automatic", status_code=303)

    @app.post("/do/digest")
    def digest_action(request: Request, csrf: str = Form(...), action: str = Form(...), address: str = Form(None), notification_id: int = Form(None)):
        bad = guard(request, csrf)
        if bad:
            return bad
        d = db()
        try:
            try:
                if action == "address" and address:
                    notifications.set_recipient(d, address)
                    msg = f"Okay, job emails go to {address.strip()}. Send a test email to be sure."
                elif action == "use_suggested":
                    res = notifications.confirm_suggested_recipient(d)
                    msg = f"Okay, job emails go to {res['address']}. Send a test email to be sure."
                elif action == "test":
                    res = notifications.send_test(ctx, d)
                    msg = res.get("note") or res.get("error") or f"Test: {res['status']}."
                elif action == "send":
                    res = notifications.send_digest(ctx, d)
                    msg = f"Digest: {res['status']}. " + (res.get("note") or res.get("error") or "")
                elif action == "retry" and notification_id:
                    res = notifications.send_digest(ctx, d, force=True, notification_id=notification_id)
                    msg = f"Digest: {res['status']}. " + (res.get("note") or res.get("error") or "")
                elif action == "arrived" and notification_id:
                    msg = notifications.resolve(d, notification_id, "arrived")["note"]
                elif action == "not_arrived" and notification_id:
                    msg = notifications.resolve(d, notification_id, "not_arrived")["note"]
                elif action == "sync":
                    res = gmail.sync(ctx, d)
                    msg = f"Mailbox synced: {res['new']} new message(s) looked at."
                elif action == "disconnect_read":
                    msg = gmail.disconnect(ctx, d, "read")["note"]
                elif action == "disconnect_send":
                    msg = gmail.disconnect(ctx, d, "send")["note"]
                elif action == "model_enable":
                    consent.grant(d, "model_processing", {"scope": "job text and profile facts"})
                    d.set_setting("model.unattended_enabled", True)
                    msg = "Unattended Claude explanations enabled."
                elif action == "model_disable":
                    d.set_setting("model.unattended_enabled", False)
                    consent.revoke(d, "model_processing")
                    msg = "Unattended Claude explanations disabled."
                else:
                    msg = "Nothing done."
            except (notifications.DigestError, gmail.GmailError) as exc:
                msg = str(exc)
        finally:
            d.close()
        return RedirectResponse(f"/connections?msg={_q(msg)}", status_code=303)

    @app.post("/do/data/{action}")
    def data_action(request: Request, action: str, csrf: str = Form(...), confirm: str = Form("")):
        bad = guard(request, csrf)
        if bad:
            return bad
        from jobhunter.services import datamgmt

        d = db()
        try:
            try:
                if action == "export":
                    res = datamgmt.export_all(ctx, d)
                    msg = f"Exported to {res['path']}"
                elif action == "backup":
                    res = datamgmt.backup(ctx, d)
                    msg = f"Backup written to {res['path']}"
                elif action == "delete":
                    res = datamgmt.delete_all(ctx, d, confirm)
                    return templates.TemplateResponse(request, "deleted.html", {"request": request, "csrf": request.state.csrf, "demo": ctx.demo, "version": __version__, "navitems": [], "totals": {}, "attention_count": 0, "hunter_status": "Idle", "last_search": None, "flash": None, "result": res, "active": None, "d": None})
                else:
                    msg = "Nothing done."
            except ValueError as exc:
                msg = str(exc)
        finally:
            try:
                d.close()
            except Exception:
                pass
        return RedirectResponse(f"/profile?msg={_q(msg)}#data", status_code=303)

    return app


def _q(msg: str) -> str:
    from urllib.parse import quote

    return quote(msg[:400])
