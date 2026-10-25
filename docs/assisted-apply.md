# Assisted Apply: what Hunter may do on an application form, and how

One lane, on the student's own computer, every step visible, one approval per submission. Nothing in this lane
runs in a cloud session (it has no browser of the student's) and nothing is ever submitted without the student's
single-use approval of the exact form Hunter read back.

## Capabilities (`jobhunter assisted capability <job>`)
| Capability | What Hunter does | Where |
|---|---|---|
| Prepare only | Prepares the pack; the student applies on the site | Every site Hunter does not know; the default |
| Assisted fill | Opens the form in a browser here, fills what the student confirmed, reads it back; the student checks and presses the site's own submit | Greenhouse job-board forms (verified on a simulated form only; see below) |
| Reviewed submit | As assisted fill, then the student approves the read-back with a one-time code and Hunter presses submit once, keeping the receipt | Only hosts the student allowed with `jobhunter assisted allow-host <host>`; none by default |
| Manual only | The pack is there to copy from; no automation | LinkedIn, Indeed (their terms) |

"Verified: simulated" means the form filler was exercised against a local form shaped like the vendor's
(`tests/test_assisted.py`), never against the vendor's live site and never with a real employer. No capability
in this build was verified on a live employer form.

## What goes into a form
- The identity the student confirmed (`jobhunter assisted identity --first … --last … --email … [--phone …]`);
  never a name or address guessed from the CV.
- The exact CV version chosen for the job, uploaded under its own file name, bytes verified against the
  recorded hash right before the upload.
- The student's own answers (by matching the form's question text to the questions they pasted and answered)
  and their own letter. Hunter's drafts are never sent. A question without their answer stays blank and is
  listed; a required one blocks the approval until they fill it on the form and Hunter reads the form again.
- Choices (selects, radios) are theirs. Consent boxes are listed in the review and ticked only after the
  approval that names them.

## The steps (`jobhunter assisted run <job>`)
1. **Preflight**: capability, local browser, confirmed identity, prepared pack, CV file at hand, no earlier
   attempt still open, not already applied. Any blocker stops it with the reason.
2. **Permission**: the run records what the student allowed (fill, upload this CV version) and when.
3. **Fill and read back**: the form opens in a visible browser; Hunter fills, then reads every field back from the
   page. The read-back (not the plan) is what the student reviews; a screenshot is kept privately.
4. **Review**: destination, every field with its source (your identity, your answer, your letter), the file with
   its hash, what stays blank and why, which boxes an approval would tick.
5. **Approval** (reviewed submit only): a one-time code bound to this run, this read-back hash and this
   destination, valid 15 minutes, shown once, stored only as a hash. A changed form, an expired or wrong code, or
   a used code never submits.
6. **Submit and receipt**: one click under a lease; the final URL, confirmation text and id and a screenshot are
   stored. A clear confirmation records **applied** with the exact CV version, the destination and the receipt
   as evidence. No clear confirmation leaves the run **uncertain**: nothing is recorded as applied, Today says
   "check the site or your inbox", and `jobhunter assisted resolve <job> submitted|not_submitted` closes it.
7. **Assisted fill sites**: the student presses submit on the site and types DONE; Hunter records "applied" with
   their word as the evidence and notes whether the page agreed.

## Duplicate and crash protection
A job with an attempt in `submitting` or `uncertain`, or already `submitted`, or already recorded as applied,
refuses a new attempt. A process that dies mid-submit leaves the run uncertain when its lease expires; Hunter
never clicks again by itself.

## Automation mode "Assisted Apply"
`jobhunter automation mode assisted_apply` can be chosen only after a preflight on this computer succeeded. It
means "prepare my best matches" plus the assisted lane offered on each ready pack. It never submits anything
on its own: every submission still needs the steps above, one at a time.
