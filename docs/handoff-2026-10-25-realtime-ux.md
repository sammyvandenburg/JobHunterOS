# Real-time UX and job detail overhaul: report, open approvals, tests (2026-10-25)

Branch `claude/quirky-cerf-lu1o4t`, on top of the dashboard UX redesign (`11da669`). No backend redesign, no new
queue or agent: the page and the running `/jobhunter` session now behave as one application, and the job detail
is one screen with one home per action. Not merged; no live page, schedule, mailbox or repository was changed.

## What changed

### Interaction
- `/jobhunter` binds its own session at startup (`get_session` → `uv run jobhunter board channel session
  <id>`; `board channel none` when the tool is missing). The page reaches that session through its `mcp`
  capability (`Claude Code Remote`: `send_message`, with `fire_trigger` for a poke-only Routine as a stand-in).
- A click stores the request in the page's database first, then hands the request id to the session at once.
  The page shows what Hunter is doing (Preparing…, Researching…, Drafting…, Finding contacts…), then the result
  (Review application, View research, Review cover letter, View contacts). A failed task says "Couldn't
  complete this. Retry". When the session cannot be reached the request is kept and the page says "Hunter is
  offline" with Retry; the banner says "Hunter is offline. Reopen /jobhunter in Claude Code to reconnect." with
  a Reconnect button that re-sends every kept request.
- Status chips: "Hunter online" / "Hunter is offline", "Last job search …", "Nightly on" only when a routine
  really runs, "N changes failed to save — Retry" only for real save errors. Simple changes say "Saved".
- Removed from the page, the skill files and the docs: "next run", "Continue application", "Continue this
  request in Claude", "Direct line", "Waiting for Hunter", "Sent to Hunter in your Claude account", request
  ids, "/jobhunter continue" as a thing to copy, "changes waiting".
- `continue <request id>` arriving in the session is treated as a page message: run that request first,
  write the page (that job's `work/<key>` document first), no question back to the person.

### Job detail and workspace
- Header: role icon, title, employer with its mark, short place and work mode, match badge; a compact line
  (type · age · still listed · pay); Favorite, Save, Close at the top (phone: a sticky Back · Favorite · Save).
- "Application help" card: one sentence, one main action named by state (Get application help → Working… →
  Answer question / Review changes / Review application → View application), Open application whenever a link
  exists, four chips (Research company, Write cover letter, Draft answers, Find contacts), one status row
  ("Application status: Not applied · I applied"; "Applied 20 Oct 2026 · CV sent: … · View submitted
  materials"). The duplicate lower application card is gone.
- Draft answers with no form questions shows "Add the questions from the application form." and a paste box;
  no invented questionnaire. A cover letter or message request prepares the package first when none exists
  (`workspace.run_request`); the student's own letter text is never overwritten.
- Job overview is a tight grid ("London, UK + 3 locations · View all"; work mode deduplicated). Fit shows at
  most three evidence lines and three things to check, then Show more; a generic line is dropped when
  specific evidence exists. The description is folded. Notes: a textarea, Save note, "Saved".
- Workspace in focused mode: ← Back to job / Application — <title> / employer; Your CV (suggested, Choose
  another, Preview), Your strongest case (two or three evidence lines, a gap, the brief folded under it with
  the Research company chip), Application questions (the form's real ones, editable, Copy), Optional (Write
  cover letter / Write short introduction / Find contacts → Review …), Ready to apply? (checklist, Open
  application or Review filled application, I applied). No command-line text anywhere.
- Rows: mark, title, employer · place, role icon and tags, age, match badge, subtle ★ / Saved / Applied /
  Ready / Needs answer. No "In progress".

### Company marks
- `jobhunter/services/logos.py` + migration `0014_logos.sql`: during a search Hunter fetches, at most 15 per
  run, the favicon of employers whose own domain is verified (never an ATS or job-board host, never guessed
  from a name), keeps a small private data URI (16 KB cap, 600 KB document budget), remembers failures for 30
  days. `board export` writes `meta/logos`; the page renders it and never contacts an employer site.

## Tests (all synthetic, simulated runtime, stubbed connector)
- `tests/test_live_ux.py`: live dispatch (Research company → request saved → `send_message` to the bound
  session at once → Researching… → View research → brief in the workspace); offline (request kept, "Hunter is
  offline", Retry, no Continue, no next-run wording); cover letter (dispatched at once, the student's own text
  preserved, Hunter's draft underneath); favorite immediate and "Saved" only; Get application help → Working…
  → Review application, never "Continue application"; the nightly run with no session still finds jobs and
  writes the board with the logos document; a letter request prepares first; logos fetched once from verified
  domains, ATS hosts skipped, refusals remembered.
- Updated: `tests/test_workspace_page.py` (offline/Retry, routine stand-in and refusal, Review application,
  Find contacts), `tests/test_dashboard_ux.py` (Application help card, chips, one CTA, phone Back row, role
  icons in rows), `tests/test_board_page.py` ("Saved"), `tests/test_board.py` (logos document).
- Full suite and `ruff check` on the final tree: see the commit message for the count.

## Needs your approval before anything live (nothing below was done)
1. **Republish the pilot page** (`https://claude.ai/artifact/1GbKMW4nQcvAPo9nVs9ZUh`, synthetic "Alex Demo")
   with the new page and the `mcp` capability declared next to `db` and `user`. The viewer will be asked once
   to allow the Claude Code Remote connector on that page.
2. **One real interactive test** on that page with this Claude Code session: bind this session
   (`board channel session <this session's id>`), export and write the page's documents, open the page, click
   Research company, and verify that this session receives `/jobhunter continue <id>` immediately, that the
   page shows "Researching…" and then "View research" after Hunter writes the work document. No email, no
   employer application, no Routine created or changed, no student data.
   If the artifact runtime cannot address this session (the `mcp` capability is refused, or `send_message`
   from a page is not allowed), the round stops there with the exact limitation written down; the page never
   falls back to "next run".

## Not changed
Matching, sourcing, CV facts, the evidence model, ranking, the application-history model, Gmail, nightly
hunting, the state repository model, branding, the role taxonomy. Backend changes are limited to what the live
interaction needs: the `brief` field on the work document, dependency-aware letter/intro/message requests, the
logo cache and its export, the `mcp` capability in `board page`.
