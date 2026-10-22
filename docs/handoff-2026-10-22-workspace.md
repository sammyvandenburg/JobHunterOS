# Hand-off: the application workspace (2026-10-22)

Candidate commit: the head of `claude/quirky-cerf-lu1o4t` (see `git log`). Not merged into the default branch.

## What works (and how it was checked)
- **My CVs**: the existing CV became the default variant with no new onboarding; more finished CVs with the
  student's labels and tags; immutable versions (hash, file name, size); identical bytes refused; Make default,
  rename, Archive, delete (refused when a version was recorded as sent); per-job choice with a one-reason
  suggestion; the exact version recorded at "I applied"; review items for claims the profile does not show;
  original bytes served unchanged in a fresh process; a missing file says "Add this CV again" and the same file
  restores the same version by hash. `tests/test_cv_variants.py` (9 tests).
- **Prepare application → Continue application → View application** on the job page, with Overview, My
  application, People & notes, a sticky header (title, employer, short location, pay when stated, the main
  action, Open application always there, expand/narrow). `tests/test_workspace_page.py` in Chromium against
  the simulated runtime (6 tests, phone and desktop widths), `tests/test_board_page.py` (7, unchanged).
- **The page reaches Hunter**: the request is written to the page's database at once (never lost), its state is
  shown (requested, running, ready, needs input, failed), Hunter records it once, runs it, writes the result,
  and the same page updates. Live, on the pilot page (synthetic "Alex Demo" data): a request written to
  `actions/j061fd752dd08893a9bdc` exactly as the page writes it → `board import` saw `request:prepare` and
  `cv` → `work run` ended `ready` → `board export` → the batch wrote `work/j061fd752dd08893a9bdc` (version 1)
  next to `meta/*` and `board/*`; reading it back as a viewer returns nothing (owner-only rules hold). The
  page's own button was not clicked by a person in a browser on claude.ai in this build; its JavaScript was
  exercised in Chromium against the simulated runtime (`tests/fake_artifact.py`).
- **Direct line** (optional): with the `mcp` capability and `board channel routine trig_…` or `session
  session_…`, the page calls the viewer's Claude Code Remote connector with `/jobhunter continue <id>`; a
  refused line falls back to "Continue in Claude now". Tested with a stubbed connector in Chromium
  (`test_a_direct_line_sends_the_request_to_hunter_and_a_refused_line_falls_back_honestly`). Not enabled on the
  pilot page: it is a new permission and needs the owner's approval (see below).
- **Preparation content**: brief in three tiers with sources, checked times and uncertainty (rules from the
  posting; a Claude-written brief is validated: https sources, employer-site source for company lines, nothing
  for confidential employers; malicious page text changes nothing); vacancy and route (reference id only when a
  real one is named, other listings, last checked, "no longer listed" is not "closed"); Your strongest case
  (profile supports vs the chosen CV says it, evidence kind, gap; no score); answers in the person's wording
  (limits, Copy, pasted questions, sensitive questions never drafted, older revisions never overwrite newer);
  optional introduction and cover letter with Facts used, versions kept, edits never overwritten; checklist (not
  a gate); I applied with which CV and a snapshot; Needs review when the job, profile or CV version changed.
  `tests/test_workspace.py` (10 tests).
- **Contacts, follow-ups, hand-off**: Listed contact (only what the posting names, the address next to that
  name), Possible team contact (a public page a Claude session read, with URL), Your connection; "I couldn't
  confirm a contact" is a valid answer; draft message; "I contacted them" only by the person. Remind me to
  follow up (date, timezone, Snooze, Done, Stop; stopped by reply, interview, rejection, withdrawal; Needs
  attention on the page; email only after `digest followups on`). InterviewOS export with the brief's sources,
  the exact CV version sent or "unknown", confirmed answers vs drafts, contacts, follow-ups, notes, timeline, a
  copyable summary; a new export never rewrites an old one.
- **Fresh session**: the chosen CV, the answers, the note, the confirmed application and the follow-up are
  identical in a new Hunter process and in a new browser session on the same page; a restore into an empty
  database keeps the CV choice, the recorded version and the application, re-reads the note and answers from
  the page (an answer that arrives before its package exists is kept and applied later), and lacks only the CV
  file until it is added again. `tests/test_fresh_session.py` (3 tests).

## Walkthrough (student)
Open a job → Prepare application → (Continue in Claude now, or wait for Hunter's next run) → Continue
application: pick the CV, read the brief and the sources, open the application page, rewrite the answers in
your words, paste the form's questions, ask for an introduction if you want one → apply on the employer's
page → I applied: date and which CV → Remind me to follow up → Find a contact / Add someone you know →
Prepare for interview when it comes. My CVs is under your profile.

## Tests run
`uv run pytest` (whole suite, 240 passed, 1 skipped; 13 Chromium page tests among them) and `uv run ruff
check`. Live: the pilot page round trip above with the Artifact data tool, acting as the owner.

## Not verified in this build
- A person clicking the page's buttons on claude.ai (only the simulated runtime, and the live store round trip).
- The direct line against a real Routine or session (stubbed connector only).
- A Claude-written brief against real employer pages (the validator is tested with synthetic input).
- PDF/DOCX CV variants beyond the existing parsers (text files used in tests and on the pilot).

## Data and storage changes
Migration `0007_workspace.sql` (new tables `cv_variants`, `cv_revisions`, `requests`, `research`; new columns on
`applications`, `contacts`, `followups`); the page's database gains the `work` collection and new fields in
`actions/<key>` and `actions/_cvs`; the state file gains `inputs.cvs` (metadata only) and a `cv` block per
acted-on job. Settings: `board.channel`, `followups.email`, `work.pending`.

## Permissions that would be new (not enabled; the owner decides)
- The `mcp` capability on the page (`Claude Code Remote`: `fire_trigger`, `send_message`) and a poke-only
  Routine (or a kept session) for the direct line. Without them every request still works through the next sync
  or "Continue in Claude now".
- Nothing else: no new email, schedule, upload or external resource was created. The pilot page was
  republished at its existing URL with its existing capabilities (db owner-only, user).

## Update and rollback
`uv run jobhunter cloud update` (merge or patch; backup tag; `--rollback`). The migration adds tables and
columns only; older code ignores them. Rolling the code back keeps the data.

## Pilot readiness
Ready for a supervised pilot with one or two students on the simulated-runtime-tested page: the whole chain
works through the normal sync, and the student always sees the state of a request. Before a wider pilot: one
person-driven click-through on claude.ai, and a decision on the direct line.
