# The application workspace

One workspace per job, inside the job page of the dashboard (and the same facts on the command line). The
homepage stays about finding jobs; the work of applying starts from a job.

## The journey
1. **Prepare application** (one main action on the job page). The page saves a request in its own database at
   once and shows its state: *Saved, Hunter does this on its next run* with **Continue in Claude now** (copies
   `/jobhunter continue <request id>` and opens Claude), or *Sent to Hunter in your Claude account* when the
   page has a direct line. Hunter records the request once, runs it, and writes the result back; the same page
   updates. States: requested, running, ready, needs input (what to add), failed (what happened).
2. **Continue application**: the job page now holds, in order:
   - the CV for this job (default preselected; Hunter may suggest another with one short reason; the choice is
     for this job only; a changed default never changes a job that chose);
   - the company and role brief: *The vacancy says* / *The company describes itself* / *Hunter's reading*, with
     a **Sources** disclosure (URL, checked time, certainty) and the uncertainty line; **Refresh the brief**;
   - the vacancy and where to apply: status, last checked, reference id, the employer's own listing when
     confirmed, other listings; "no longer listed where Hunter found it" is never "closed";
   - **Your strongest case**: three requirements, whether the profile supports each (and the kind of evidence),
     whether the chosen CV says it in so many words, the gap, two or three examples to tell; no score;
   - **Application answers**: Hunter's draft (facts only) with Copy, your wording with Save my wording, a
     character count and the form's limit; paste the form's questions (`(500)` sets a limit); pay, visa, start
     date and personal questions are never drafted;
   - the optional short introduction (about 60 to 100 words) and cover letter (about 180 to 250), only on request,
     with **Facts used**; a new draft never overwrites your version; versions are kept;
   - **Before you send**: a checklist, not a gate; **Open application** is always available.
3. **I applied**: the date and which CV you sent (the prepared one preselected, another CV, not sure). Hunter
   records the exact version and a snapshot (vacancy, package version, your confirmed answers). Preparing,
   copying and opening never count. **View application** from then on.
4. After applying: **Remind me to follow up** (date, Snooze 3 days, Done, Stop; stops by itself on a reply,
   interview, rejection or withdrawal; shown under Needs attention; in the email only by preference),
   **Draft follow-up**, **Prepare for interview** (the InterviewOS hand-off).
5. **People & notes**: **Find a contact** (Listed contact from the posting, Possible team contact from a public
   page Hunter read, Your connection you named), with source and checked date, a public route only when
   published, a short draft message, **I contacted them**; and the private notes.
6. **My CVs** under Profile: label, tags, version, default, whether the file is on Hunter's computer; Make
   default, Archive, rename; **Preview / download** asks Hunter for the original bytes (hash checked), which
   Hunter hands over in Claude; a file that is not on that computer gets "Add this CV again".

## The page reaches Hunter
Published-page action → the request is in the page's database (never lost) → Hunter reads it in its next sync
(`board import`), runs it (`work run`), writes the result (`board export`) → the same page updates (its `work`
collection). The direct line (optional, with the student's approval): the page is published with the `mcp`
capability for the `Claude Code Remote` connector (`fire_trigger`, `send_message`) and Hunter names a poke-only
Routine or a session (`board channel`); the page then calls it with `/jobhunter continue <request id>` as the
viewer, with the viewer's own credentials. A refused or missing line falls back to **Continue in Claude now**.
Nothing is ever claimed as running when no worker is.

## Today and automation modes
The page opens with **Today**: Ready to review, Needs your answer, Follow-up due, Something changed, Needs
attention, one action each, computed by Hunter (`jobhunter today`) and written as `meta/today`. Modes
(`jobhunter automation mode`): **on request** (default), **prepare my best matches** (after a search, the best
unprepared good matches are prepared through the same request chain as the page's button, at most 3 a day and 5
unopened, one digest a day; nothing is submitted or sent), **assisted apply** (refused until the assisted lane
exists). Opening a prepared application on the page writes `opened_at` into its action document; Hunter
records it once, and the queue limit counts only unopened preparations. Each prepared application carries a
readiness label: *Ready for review* (something open: form questions without your wording, a CV file not on this
computer, no known place to apply, a change since) or *Ready to submit*. The label never gates anything.
Earlier answers you wrote for another job are offered for the same question when they name no employer and have
not expired (90 days for pay, notice and start answers; never for personal or legal ones), as a suggestion with
its origin; nothing is filled in by itself.

## Needs review
A package is tied to the job text, the approved profile, the chosen CV version and the policy version. When any
of those changes, the page says **Needs review** and names what changed; **Prepare again** makes a new version
and keeps your wording.

## What is stored where
- Hunter's database: CV variants and versions (metadata; the originals in the data folder), applications with
  the chosen CV, the recorded version and the snapshot, requests with their states, research versions, contacts,
  follow-ups, packages with your answers.
- The page's owner-only database: your edits (answers, texts, CV choice, I applied, contacts, follow-ups,
  requests) in the job's action document, My CVs edits in `actions/_cvs`, and Hunter's `work/<key>` results.
- The state file in your private repository: CV metadata (keys, labels, versions, hashes) and per-job CV state;
  never files, text, notes, answers, drafts, research or contacts.

## Intake and email updates
`jobhunter intake` takes a link, a pasted description, a job-alert email, a recruiter message or a referral and
makes the same vacancy record the hunt makes (board links carry the board's identity; the same job merges, a
different one stays distinct; source differences stay as observations). Recruiters and referrers become contacts
with their provenance; the message joins the timeline and the hand-off says how the job reached Hunter.
`jobhunter email policy` decides what an email may change by itself (receipts only, by default); everything else
is a suggestion on Today with confirm, dismiss and undo (`email review`, `email confirm|reject|unlink`).

## Assisted Apply
See [assisted-apply.md](assisted-apply.md): the capability per site is shown on the job page under "The vacancy
and where to apply"; the lane itself runs on the student's computer (`jobhunter assisted run`).

## Known limits
- The direct line needs the `mcp` capability on the page and a Routine or session in the student's account;
  both are created only with their approval. Until then every request still works through the next sync or
  **Continue in Claude now**.
- CV files are only on the computer where they were added. A cloud session cannot preview or download one; it
  says so. The same file added again restores the same version (hash proven).
- The rule-based brief reads the posting only. A richer brief with company sources comes from a Claude session
  that really read the employer's pages (`work research --file`), validated by code.
- Your answers and drafts live on your page and in each Hunter database that read them, not in Git. After a
  restore into an empty database Hunter re-reads the page and keeps an answer until its package exists there.
