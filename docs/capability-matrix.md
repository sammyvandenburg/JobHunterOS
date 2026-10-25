# Capability matrix (release candidate 0.2.0rc1, 2026-10-25)

Three columns matter: what Hunter does by itself, what the student approves or does, and how the capability was
verified in this build. "Fixture" means the test suite with synthetic data and local fakes (`uv run pytest`,
282 tests on the final tree). "Simulated browser" means a real Chromium against a local page or form served by
the test, never a vendor's live site. "Live" means exercised on a real account during an earlier round; nothing
in this round was run against a real GitHub repository, a real Artifact page, a real mailbox or a real
employer form. "Not available" means the lane exists but is switched off or cannot be chosen yet.

| Capability | Hunter does by itself | The student approves or does | Verified |
|---|---|---|---|
| Finding jobs (Greenhouse, Lever, Ashby boards, Remotive, Arbeitnow, pasted alerts) | Reads sources with conditional requests, honours Retry-After, dedupes into one vacancy identity, matches and explains | Readiness confirmation, approved profile, preferences | Fixture; sources live-read in earlier rounds |
| Three groups, Today first | Computes Ready to review, Needs your answer, Follow-up due, Something changed, Needs attention; one action each | Opens items; answers the one question asked | Fixture + simulated browser (`tests/test_slice_b.py`, `tests/test_workspace_page.py`) |
| Preparation of an application pack | Brief from the posting (sourced brief when Claude read the employer's pages), route, strongest case, proof selection, draft answers, checklist, readiness label | Chooses the CV, writes answers in their words, pastes the form's questions | Fixture (`tests/test_gate_a.py`, `tests/test_workspace.py`) |
| Bounded auto-preparation ("Prepare my best matches") | After a search prepares the best unprepared good matches: at most 3 a day, 5 unopened, through the page's own request chain | Chooses the mode; opens or dismisses packs | Fixture; off by default |
| Answer library | Offers the student's own earlier wording for the same question with its origin and expiry; never personal, never employer-specific, never filled in by itself | Presses "Use my earlier answer" | Fixture |
| CV files | Keeps every version byte for byte with its hash; hands the exact file back; says "add it again" when the file left the computer | Adds the file again on a new computer | Fixture + simulated browser |
| Assisted Apply: capability per site | Names the lane per application page: prepare only (default), assisted fill (Greenhouse-shaped forms), reviewed submit (only hosts the student allowed), manual only (LinkedIn, Indeed) | Allows a host for reviewed submit | Fixture |
| Assisted Apply: fill and read-back | On the student's computer: opens the form, fills the confirmed identity, the exact CV version and the student's own answers and letter, reads every field back | Confirms identity once; completes choices and required fields on the form | Simulated browser (`tests/test_assisted.py`); not verified on any live employer form |
| Assisted Apply: reviewed submit | Presses submit once, under a lease, after a one-time code bound to the read-back hash and destination; stores the receipt; "uncertain" is never "applied" | Reviews the read-back, types the code | Simulated browser; allowed on no host by default; not verified live |
| Intake by hand | Turns a link, a description, an alert email, a recruiter message or a referral into the same vacancy record; board links carry the board's identity; recruiters and referrers become contacts | Pastes it; names the person when Hunter cannot | Fixture (`tests/test_intake.py`) |
| Email status updates | Under the policy (receipts by default): an automated receipt may mark applied, undoable; rejections, interviews, offers, recruiter replies are suggested | Confirms or dismisses on Today; undoes | Fixture (`tests/test_pipeline_email.py`, `tests/test_intake.py`); mailbox reading not used in this build |
| Watches and change detection | Records material changes seen by the ordinary refresh (pay, place, remote rule, title, text, closing date, contract, availability, link); shows them on Today and in the digest for followed jobs and named companies; never from a failed check | Acknowledges; names companies | Fixture (`tests/test_watches.py`) |
| Weekly observation | One fact from the student's own records with a small-sample guard, once a week | Reads it | Fixture |
| Follow-ups | Reminders on Today; stop by themselves on a reply, interview, rejection or withdrawal | Sets, snoozes, stops; sends any message themselves | Fixture |
| InterviewOS hand-off | Snapshot of the vacancy, the exact CV sent, the confirmed answers, messages, timeline, how the job reached Hunter; never rewritten | Asks for it; attaches the CV | Fixture |
| Digest email | One a day at most, to the one confirmed address, through Claude's Gmail connector; names prepared packs, reminders and changes; never a send to anyone else | Confirms the address once; Claude sends and records | Fixture; live sends in earlier rounds (2026-10-2x pilot) |
| Dashboard page | Hunter writes documents; the page shows Today, the lists, the workspace; `board published` confirms the write | Opens the page; acts on it | Fixture + simulated runtime; live page published in earlier rounds |
| Nightly run in the student's Claude account | Restore, read page, requests, hunt, auto-prepare, digest, save, publish, confirm | Creates the Routine and the private copy; approves each external resource | Fixture (`tests/test_workflow.py`, `tests/test_cloud_repo.py`); the Routine in this account was not changed in this round |
| State in a private repository | Commits exactly two state files; proves the push destination is private and pushable, or refuses | Links the repository; confirms privacy when the token cannot prove it | Fixture with local bare repositories; not pushed to GitHub in this round |

## Never, by design
Hunter never rewrites a CV, invents a fact, submits without the one-time approval of the exact read-back, emails
an employer or a recruiter, reads a mailbox without a separate read permission, or claims work is running when
no worker is.
