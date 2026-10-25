# Competitive upgrade round: founder report, checklist and tests (2026-10-25)

Branch `claude/quirky-cerf-lu1o4t`, from the audited baseline `e0e92d0` (plus `951c10b`). Six focused commits:
Gate A (two), Slice B, Slice C, Slice D, Slice E. Release candidate `0.2.0rc1` (pyproject). Not merged into the
default branch; no live schedule, page, mailbox or repository was changed in this round.

## 1. Founder report

### What Hunter now does for the student automatically
- Opens the dashboard on **Today**: Ready to review, Needs your answer, Follow-up due, Something changed, Needs
  attention. One action per item. Empty means empty.
- With "Prepare my best matches" on: after every search, prepares the best unprepared good matches (at most three
  a day, five unopened), each with a brief, the route, the strongest case with proof examples, draft answers,
  the right CV, a readiness label (Ready for review / Ready to submit). Preparations are never applications.
- Keeps the student's own words apart from Hunter's drafts and offers their earlier answers again where they
  fit, with origin and expiry.
- Turns anything pasted (a link, a description, an alert email, a recruiter message, a referral) into the same
  vacancy record a search makes; recruiters and referrers become contacts with provenance.
- Records material changes on the jobs they follow (pay, place, remote rule, title, text, closing date,
  availability, link) and shows them, old and new, on Today and in the digest; never from a failed check.
- Lets an automated application receipt mark a job applied by itself (undoable); suggests every other email
  status change for confirmation.
- Stops follow-up reminders on a reply, interview, rejection or withdrawal; writes an InterviewOS hand-off from
  the exact submission snapshot.
- Says one plain thing a week from their own numbers, with a small-sample guard.

### What the student still approves or does
- Confirms readiness and the profile; chooses the automation mode (default: on request).
- Writes the answers in their own words, pastes the form's questions, chooses the CV, applies on the employer's
  page and presses "I applied". With the assisted lane on their own computer: confirms their identity once,
  completes the choices Hunter never makes (selects, consent), reviews the read-back, types the one-time code.
- Confirms or dismisses suggested email updates; acknowledges changes; names companies to watch.
- Creates and owns every external resource: the private state repository, the published page, the Routine,
  the Gmail connector. Confirms the digest address once.
- Sends every message to a person themselves.

### Verified versus unavailable or unverified in this account
- Verified by the suite on the final tree (synthetic data, local fakes, a real Chromium against local pages):
  282 tests, lint clean. Gate A's eighteen guarantees have a per-item record in `docs/gate-a-2026-10-25.md`.
- Simulated only: the Assisted Apply lane (a local Greenhouse-shaped form). No live employer form was touched;
  reviewed submit is allowed on no host by default.
- Not exercised live in this round: a push to a real private GitHub repository with the new destination check, a
  real Artifact batch followed by `board published`, a real digest send, a real mailbox read, the Routine. The
  earlier pilot (synthetic "Alex Demo", page `https://claude.ai/artifact/1GbKMW4nQcvAPo9nVs9ZUh`) predates this
  round and is not evidence for it.
- Not available: assisted fill for Lever, Ashby, Workable and other forms (prepare only); the `assisted_apply`
  mode until a preflight on the student's computer succeeds; closing dates only when the posting states one.
- The audit's own A01–A20 evidence file was not in the package; Gate A follows the brief's eighteen numbered
  points instead and says so.

### How a new student starts, and pilot readiness
1. `/jobhunter` in Claude Code with a finished CV and LinkedIn PDF: readiness, profile approval, preferences.
2. The dashboard page (published once, private), the first search, Today.
3. Optional, each with its own yes: the digest address, "Prepare my best matches", nightly hunting in their own
   Claude account with their private state repository, the assisted lane on their computer (`jobhunter assisted
   identity`, then `assisted run <job>`).
Pilot readiness: ready for a supervised pilot with synthetic or volunteer data once the four live checks in the
next section are done by a person with the right accounts. Not ready for an unsupervised cohort until a real
push, a real page write with confirmation, a real digest and one real (volunteer) assisted fill have been
watched end to end.

## 2. Approvals and live checks still needed (grouped)
- **GitHub**: one push of state to a student-owned private repository with `cloud save` on this build; expected:
  the destination check passes with the token, or asks for `cloud link --confirm-private` and refuses otherwise.
- **Artifact**: one `board export`, the batch write, `board published`; expected: `plan` shows the publish step
  done only after the confirmation; the page shows Today first.
- **Gmail**: one digest through the connector with no new jobs but one prepared pack or one change; expected:
  the subject names it; `digest record` closes the row.
- **Routine**: no change requested. If nightly runs are enabled later, the routine prompt must include
  `work run`, the auto-prepare note and `board published` (cloud.md already does).
- **Assisted Apply on a volunteer's computer**: `assisted identity`, `assisted run` on a Greenhouse form of a
  job the volunteer really wants, assisted fill only (no `allow-host`); expected: a filled form read back, the
  volunteer presses submit, `user_submitted` with the page's confirmation. No test applications to real
  employers.

## 3. Phased checklist (resume here without redefining scope)
- [x] Gate A: items 1–18 fixed and tested; record written; lint clean.
- [x] Slice B: Today, automation modes and limits, readiness, opened marker, proof selection, answer library,
      digest lines; page Today section; docs and skill.
- [x] Slice C: capability registry, assisted lane (preflight, permit, fill, read-back, re-read, review, approve,
      submit, receipt, resolve), CLI, page capability line, `docs/assisted-apply.md`.
- [x] Slice D: intake, mail policy and review queue with undo, best route, interview stops reminders, hand-off
      provenance; docs and skill.
- [x] Slice E: change detection, watches, company watches, digest and Today lines, weekly observation; docs.
- [x] Final: capability matrix, this report, version `0.2.0rc1`, README index.
- [ ] Live checks in section 2 (need the accounts).
- [ ] Merge decision for the default branch (KubeCraft's call).
- [ ] Next slices if wanted: assisted fill for Lever and Ashby forms (fixture forms first); mailbox read
      (separate permission) feeding the same review queue; a matched, permitted benchmark set for the metrics
      in the brief (none was run here, so no competitor claim is made).

## 4. Acceptance journeys and the exact tests
| Journey | Tests | Status |
|---|---|---|
| A. CV A → prepare → edit, custom question, letter → choose B → regenerate → confirm B → later edit, vacancy change → empty restore | `tests/test_gate_a.py::test_user_texts_survive_a_cv_change_and_a_regeneration_without_saving_again`, `::test_the_cv_confirmed_is_the_exact_version_shown_never_a_substitute`, `::test_submission_snapshot_is_immutable_and_the_handoff_uses_it_alone`, `tests/test_fresh_session.py` | Fixture |
| B. Duplicate vacancy from employer, board and email is one opportunity; distinct requisitions stay distinct; title changes keep actions and contacts | `tests/test_intake.py::test_a_link_an_alert_and_the_board_itself_are_one_job_and_a_different_job_stays_distinct`, `tests/test_gate_a.py::test_job_identity_survives_a_title_change_and_contacts_have_portable_keys` | Fixture |
| C. Two state clients change salary and location concurrently; both survive; old matches cannot undo new preferences | `tests/test_gate_a.py::test_failed_pull_is_not_a_restore_and_preferences_merge_without_losing_either_side`, `::test_imported_matches_keep_their_fingerprints_and_stale_ones_are_dropped`, `tests/test_cloud_repo.py` | Fixture (local bare repositories) |
| D. Auto-preparation respects fit, chosen jobs, budgets and the unopened backlog; hunts create no endless letters or duplicate packages | `tests/test_slice_b.py::test_auto_preparation_is_off_by_default_bounded_and_uses_the_same_requests`, `::test_a_search_runs_the_bounded_preparation_and_reports_it`, `tests/test_workspace.py` (package idempotency) | Fixture |
| E. Research keeps stronger sourced evidence, labels unknowns, never turns employer claims into candidate achievements | `tests/test_gate_a.py::test_sourced_research_outlives_timestamp_refreshes_and_rule_changes`, `tests/test_workspace.py` (research validation), `tests/test_claims.py` | Fixture. Fetching the employer's pages is done by the Claude session, validated by code; no automated fetch exists |
| F. A supported form receives the correct CV and answers; a changed form invalidates the approval; double click, crash after submit and a missing receipt create no false or repeated submission | `tests/test_assisted.py` (all five) | Simulated browser. CAPTCHA not simulated: a CAPTCHA stops the lane like any required field Hunter cannot fill |
| G. Recruiting emails update the correct job with reversible evidence; a receipt cannot create recruiter interest; no inbox permission means no read calls | `tests/test_intake.py::test_mail_updates_follow_the_policy_with_a_review_queue_and_undo`, `tests/test_pipeline_email.py` (acc20–acc27) | Fixture |
| H. Follow-up-only digest; real replies stop obsolete reminders; InterviewOS uses the original submission history | `tests/test_gate_a.py::test_publication_is_confirmed_separately_and_status_keeps_its_distinctions`, `tests/test_intake.py` (interview stops the reminder), `tests/test_pipeline_email.py::test_followups_stop_after_reply_or_rejection` | Fixture |
| I. Malicious source or job content cannot send data, change recipients, obtain secrets or approve submissions | `tests/test_pipeline_email.py::test_acc26_recipient_cannot_be_changed_by_model_or_content`, `tests/test_schedule_security_exports.py`, `tests/test_worker_secrets.py`, `tests/test_workspace.py` (untrusted research input); the approval code is bound to the run, the read-back hash and the destination (`tests/test_assisted.py`) | Fixture |
| J. Page controls survive refresh, failed saves, new browser sessions and an empty runtime; keyboard and phone widths; owner-only data | `tests/test_board_page.py`, `tests/test_workspace_page.py` (including `test_today_…` and the width test), `tests/test_fresh_session.py`, `tests/test_action_recovery.py` | Simulated runtime; owner-only rules are the page's published capability (not re-published in this round) |

## 5. Exact commands on the final tree
```bash
uv sync --extra dev --extra browser
uv run ruff check                       # clean
uv run pytest                           # 282 tests (two skips without a browser)
uv run pytest tests/test_gate_a.py tests/test_slice_b.py tests/test_assisted.py tests/test_intake.py tests/test_watches.py
```

## 6. Migrations and rollback
`0008`–`0013` only add tables and columns (contact keys and request leases; user texts and refresh history;
opened marker and request origin; assisted runs; intakes; job changes). Reverting the code to `e0e92d0` leaves
them unused. The one-time move of answers into `user_texts` keeps the old copies inside the package JSON, so an
older build still shows them.

## 7. Metrics
No baseline-versus-candidate measurement on a reviewed fixture set was run in this round, and no competitor
benchmark exists; the brief asks for a matched, permitted one before any superiority claim. What can be counted
today from the records: preparations per day and unopened packs (`jobhunter today`), confirmed submissions with
exact CV versions (`applications`), suggested versus automatic email updates (`jobhunter email review`), changes
detected (`jobhunter watches changes --all`).
