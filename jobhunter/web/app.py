"""The local dashboard: server-rendered pages over the same services as the CLI."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from jobhunter import __version__
from jobhunter.config import MAX_DOCUMENT_BYTES, AppContext, dashboard_secret
from jobhunter.db import connect
from jobhunter.services import (
    actions,
    consent,
    drafts,
    feedback,
    hunt,
    interviewos,
    notifications,
    onboarding,
    schedule,
    stats,
    views,
)
from jobhunter.services import documents as docs
from jobhunter.services import preferences as prefs_svc
from jobhunter.services import profile as profile_svc
from jobhunter.services.email import gmail, linking, transport
from jobhunter.services.matching import engine
from jobhunter.services.model import provider as model_provider
from jobhunter.services.secrets import SecretStore
from jobhunter.web.security import LocalOnlyMiddleware, check_csrf

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(HERE / "templates"))

NAV = [("new", "New jobs"), ("saved", "Saved"), ("applied", "Applied"), ("interviews", "Interviews"), ("finished", "Finished")]


def create_app(ctx: AppContext) -> FastAPI:
    app = FastAPI(title="Hunter", docs_url=None, redoc_url=None, openapi_url=None)
    secret = dashboard_secret(ctx.paths)
    app.add_middleware(LocalOnlyMiddleware, secret=secret)
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    def db():
        return connect(ctx.paths.db_path)

    def render(request: Request, name: str, **kw):
        d = db()
        try:
            base = {
                "request": request,
                "csrf": request.state.csrf,
                "demo": ctx.demo,
                "version": __version__,
                "nav": NAV,
                "totals": stats.totals(d),
                "attention_count": views.attention(d)["count"],
                "flash": request.query_params.get("msg"),
            }
        finally:
            d.close()
        base.update(kw)
        return templates.TemplateResponse(request, name, base)

    def guard(request: Request, token: str | None):
        if not check_csrf(request, token):
            return PlainTextResponse("Form expired or invalid. Go back and try again.", status_code=403)
        return None

    # ---- pages ------------------------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    def home(request: Request):
        d = db()
        try:
            nxt = onboarding.next_step(d)
            if nxt and not ctx.demo and nxt["key"] in {"readiness", "cv", "linkedin_url", "linkedin_content"}:
                return render(request, "welcome.html", step=nxt, status=onboarding.status(d))
            return _jobs_page(request, d, "new")
        finally:
            d.close()

    @app.get("/jobs/{view}", response_class=HTMLResponse)
    def jobs_view(request: Request, view: str):
        if view not in views.VIEWS if hasattr(views, "VIEWS") else view not in {v for v, _ in NAV} | {"all", "dismissed", "excluded", "uncertain", "old", "closed"}:
            return RedirectResponse("/", status_code=303)
        d = db()
        try:
            return _jobs_page(request, d, view)
        finally:
            d.close()

    def _jobs_page(request: Request, d, view: str):
        q = request.query_params
        page = int(q.get("page", "1") or 1)
        group = q.get("group") or None
        query = q.get("q") or None
        sort = q.get("sort") or "best"
        res = views.list_jobs(d, view=view, group=group, query=query, page=page, per_page=10, sort=sort)
        last = hunt.last_run(d)
        sched = schedule.describe(d)
        return render(request, "jobs.html", view=view, res=res, last_run=last, sched=sched, old_count=views.list_jobs(d, view="old", per_page=1)["total"] if view == "new" else 0)

    @app.get("/job/{job_id}", response_class=HTMLResponse)
    def job_page(request: Request, job_id: int):
        d = db()
        try:
            j = views.job_detail(d, job_id)
            if not j:
                return PlainTextResponse("Job not found", status_code=404)
            return render(request, "job.html", job=j)
        finally:
            d.close()

    @app.get("/attention", response_class=HTMLResponse)
    def attention_page(request: Request):
        d = db()
        try:
            return render(request, "attention.html", att=views.attention(d))
        finally:
            d.close()

    @app.get("/profile", response_class=HTMLResponse)
    def profile_page(request: Request):
        d = db()
        try:
            row = profile_svc.current_draft(d) or profile_svc.approved(d)
            pv = profile_svc.profile_view(d, row) if row else None
            prefs = prefs_svc.get(d)
            return render(request, "profile.html", profile=pv, prefs=prefs, pref_lines=prefs_svc.describe(prefs), documents=docs.list_documents(d), setup=onboarding.status(d), next_step=onboarding.next_step(d), consents=consent.summary(d), feedback=feedback.summary(d), linkedin_url=d.get_setting("linkedin.url"))
        finally:
            d.close()

    @app.get("/connections", response_class=HTMLResponse)
    def connections_page(request: Request):
        d = db()
        try:
            return render(request, "connections.html", gmail=gmail.describe(d), digest={"recipient": notifications.recipient(d), "transport": transport.describe(d)}, model=model_provider.describe(ctx, d), secrets=SecretStore(ctx.paths).describe(), sched=schedule.describe(d), pending=linking.pending_confirmations(d), notifications=notifications.history(d, 10))
        finally:
            d.close()

    @app.get("/health", response_class=HTMLResponse)
    def health_page(request: Request):
        d = db()
        try:
            runs = d.fetchall("SELECT id, trigger, status, started_at, finished_at, stats_json, error FROM hunt_runs ORDER BY id DESC LIMIT 10")
            for r in runs:
                r["stats"] = json.loads(r.pop("stats_json") or "{}")
            return render(request, "health.html", sources=hunt.source_health(d), runs=runs, sched=schedule.describe(d), gmail=gmail.describe(d), employers=d.fetchone("SELECT COUNT(*) AS n, SUM(CASE WHEN origin='discovered' THEN 1 ELSE 0 END) AS discovered FROM employers"))
        finally:
            d.close()

    @app.get("/digest/preview", response_class=HTMLResponse)
    def digest_preview(request: Request):
        d = db()
        try:
            jobs = notifications.pending_jobs(d)
            dg = notifications.compose(d, jobs) if jobs else None
            return render(request, "digest.html", digest=dg, recipient=notifications.recipient(d), transport=transport.describe(d))
        finally:
            d.close()

    @app.get("/api/status")
    def api_status():
        d = db()
        try:
            return JSONResponse({"totals": stats.totals(d), "attention": views.attention(d)["count"], "schedule": schedule.describe(d)})
        finally:
            d.close()

    # ---- actions (all POST + CSRF) --------------------------------------
    @app.post("/do/job/{job_id}/{action}")
    def job_action(request: Request, job_id: int, action: str, csrf: str = Form(...), reason: str = Form(None), note: str = Form(None), when: str = Form(None), stage: str = Form(None)):
        bad = guard(request, csrf)
        if bad:
            return bad
        d = db()
        msg = ""
        try:
            if action == "open":
                res = actions.open_job(d, job_id)
                url = res.get("url")
                if url and (url.startswith("https://") or url.startswith("http://")):
                    return RedirectResponse(url, status_code=303)
                msg = "No link is available for this job."
            elif action == "save":
                actions.save_job(d, job_id, True)
                msg = "Saved."
            elif action == "unsave":
                actions.save_job(d, job_id, False)
                msg = "Removed from saved."
            elif action == "dismiss":
                res = actions.dismiss_job(d, job_id, reason or None, note or None)
                msg = "Okay, I won't show this one again." + (" I have a question for you on the attention page." if res.get("suggestions") else "")
            elif action == "undismiss":
                actions.dismiss_job(d, job_id, undo=True)
                msg = "Back in your list."
            elif action == "applied":
                actions.mark_applied(d, job_id, applied_at=when or None)
                msg = "Recorded: you applied."
            elif action == "prepare":
                drafts.prepare(ctx, d, job_id)
                msg = "Application materials prepared below."
            elif action == "stage" and stage:
                actions.set_stage(d, job_id, stage, actor="user", occurred_at=when or None, allow_backwards=True)
                msg = f"Stage set to {stage}."
            elif action == "note" and note:
                actions.add_note(d, job_id, note)
                msg = "Note added."
            elif action == "followup":
                drafts.schedule_followup(d, job_id)
                msg = "Follow-up reminder set for 7 days after you applied."
            elif action == "outreach":
                drafts.outreach(d, job_id, None)
                msg = "Outreach draft created below. Review and send it yourself."
            elif action == "handoff":
                res = interviewos.export(ctx, d, job_id)
                msg = f"InterviewOS hand-off written to {res['path']}"
            elif action == "lookup":
                from jobhunter.services import jobs as jobs_svc
                from jobhunter.services import original_lookup

                res = original_lookup.resolve(d, jobs_svc.get_job(d, job_id))
                msg = "Original listing: " + res["status"] + ". " + res["how"]
            else:
                msg = "Unknown action."
        except (actions.ActionError, drafts.DraftError, interviewos.HandoffError) as exc:
            msg = str(exc)
        finally:
            d.close()
        return RedirectResponse(f"/job/{job_id}?msg={_q(msg)}", status_code=303)

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
                return RedirectResponse(f"/?msg={_q('Demo mode uses synthetic data; there is nothing to fetch.')}", status_code=303)
            try:
                st = hunt.run_hunt(ctx, d, trigger="manual")
                msg = f"Hunt {st['status']}: {st['jobs_new']} new jobs, {st['jobs_relevant_new']} relevant; {st['sources_failed']} source(s) failed."
            except hunt.HuntInProgress as exc:
                msg = str(exc)
        finally:
            d.close()
        return RedirectResponse(f"/?msg={_q(msg)}", status_code=303)

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
        target = "/profile" if key not in {"readiness", "cv", "linkedin_url", "linkedin_content"} else "/"
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
                    res = profile_svc.save_draft(d, draft, built_by="rules")
                    msg = f"Draft built with {res['facts']} facts. Review it, then approve."
                elif action == "approve":
                    profile_svc.approve(d, summary_override=summary or None)
                    consent.grant(d, "profile_approved", {})
                    msg = "Profile approved."
                elif action == "answer" and key and answer:
                    profile_svc.answer_question(d, key, answer)
                    msg = "Thanks, noted."
                elif action == "skip" and key:
                    profile_svc.skip_question(d, key)
                    msg = "Skipped."
                elif action == "correct" and text:
                    profile_svc.add_user_fact(d, text)
                    msg = "Correction added to your profile (your documents are unchanged)."
                elif action == "rematch":
                    res = engine.run_matching(ctx, d)
                    msg = f"Re-matched: {res.get('computed', 0)} job(s) updated." if res.get("status") == "ok" else res.get("reason", "Not matched.")
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

            class A:  # minimal shim to reuse CLI logic
                pass

            a = A()
            a.demo, a.home, a.json = ctx.demo, str(ctx.paths.home), True
            a.prefs_cmd, a.key, a.value, a.strength, a.reason = "set", key, [value], strength, "dashboard"
            try:
                import contextlib
                import io

                with contextlib.redirect_stdout(io.StringIO()):
                    cmd_prefs(a)
                msg = f"Saved {key}."
            except CliError as exc:
                msg = str(exc)
        finally:
            d.close()
        return RedirectResponse(f"/profile?msg={_q(msg)}", status_code=303)

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
    def schedule_action(request: Request, csrf: str = Form(...), action: str = Form(...), frequency: str = Form(None), time_of_day: str = Form(None), notify_mode: str = Form(None), timezone: str = Form(None)):
        bad = guard(request, csrf)
        if bad:
            return bad
        d = db()
        try:
            try:
                if action == "set":
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
        return RedirectResponse(f"/connections?msg={_q(msg)}", status_code=303)

    @app.post("/do/digest")
    def digest_action(request: Request, csrf: str = Form(...), action: str = Form(...), address: str = Form(None)):
        bad = guard(request, csrf)
        if bad:
            return bad
        d = db()
        try:
            try:
                if action == "address" and address:
                    notifications.set_recipient(d, address)
                    msg = "Digest address confirmed."
                elif action == "send":
                    res = notifications.send_digest(ctx, d)
                    msg = f"Digest: {res['status']}. " + (res.get("note") or res.get("error") or "")
                elif action == "sync":
                    res = gmail.sync(ctx, d)
                    msg = f"Mailbox synced: {res['new']} new message(s) looked at."
                elif action == "disconnect_read":
                    res = gmail.disconnect(ctx, d, "read")
                    msg = res["note"]
                elif action == "disconnect_send":
                    res = gmail.disconnect(ctx, d, "send")
                    msg = res["note"]
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
                    return render(request, "deleted.html", result=res)
                else:
                    msg = "Nothing done."
            except ValueError as exc:
                msg = str(exc)
        finally:
            try:
                d.close()
            except Exception:
                pass
        return RedirectResponse(f"/profile?msg={_q(msg)}", status_code=303)

    return app


def _q(msg: str) -> str:
    from urllib.parse import quote

    return quote(msg[:400])
