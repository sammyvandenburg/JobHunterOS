# Hunter in the person's own Claude account

Everything stays theirs: their Claude account runs the hunt, their Gmail sends the digest to them, their
Drive keeps the state file. KubeCraft distributes this code; it never sees the data. Python does the
deterministic work (restore, hunt, match, compose, record); you move one file and send one email with
your own connectors. You never copy tokens anywhere and never pretend Python can use your connectors.

## What you need
- A session with the **Google Drive** and **Gmail** connectors (check with `/mcp` or by looking at your
  tools: `mcp__Google_Drive__*`, `mcp__Gmail__send_message`). Without them, stop and say so. A routine
  gets them only when it is created on the claude.ai routines page with those connectors ticked.
- This repository checked out, `uv` available, and an empty or Hunter-owned data folder. In a cloud
  session the data folder is `~/.jobhunteros` of that fresh machine; nothing of the person's is there
  until you restore the state file.
- A private Drive folder named `JobHunterOS` owned by the person. Create it once, only after they
  agree, with `create_file(title="JobHunterOS", contentMimeType="application/vnd.google-apps.folder")`.
  Keep its id; it is also in the state file as `drive_folder_id` after the first export.

## State file rules
- One file per run, named `hunter-state-<UTC timestamp>.json.gz`, in that folder. Produce it with
  `uv run jobhunter cloud export --out ~/hunter-state-out.json.gz`, then `base64 -w0 ~/hunter-state-out.json.gz`
  and upload with `create_file(parentId=<folder>, title=..., contentMimeType="application/gzip",
  base64Content=<that string>, disableConversionToGoogleType=true)`. Gzip keeps a full night's results
  around 50 to 150 KB. The Drive connector cannot update file content, so a new file is the update.
  Never trash the previous file until the new one is uploaded and `download_file_content` returns it
  with the same size; then keep the newest three and trash older ones (only if the person agreed).
- Find the newest with `search_files(query="parentId = '<folder>' and title contains 'hunter-state-'")`
  and pick the latest timestamp in the title. Download with `download_file_content(fileId=...)`
  (base64), write it with `base64 -d` to `~/hunter-state.json.gz`, and let Python read it
  (`cloud restore` and `cloud import` accept .json and .json.gz). Never edit the file yourself.
- The file is produced by `uv run jobhunter cloud export` and contains only: the approved profile,
  preferences, three consents, the keys of dismissed and applied jobs, public job data, run summaries
  and the email history. No CV, no notes, no favorites, no drafts, no tokens. Do not add anything.

## Nightly run (cloud session or routine)
1. `uv sync` in the repository.
2. Newest state file from Drive → `~/hunter-state.json.gz` → `uv run jobhunter cloud restore ~/hunter-state.json.gz --json`.
   If there is no state file, stop: say the person has to run "cloud sync" from their computer first.
3. `uv run jobhunter hunt run --trigger cloud --json`, then `uv run jobhunter cloud tag-run cloud:<short id>`.
   One failing source is not a failed hunt; report it in one line.
4. `uv run jobhunter digest prepare --json`.
   - `status: ready` → send with `mcp__Gmail__send_message(to=[<to>], subject=<subject>, body=<text>, htmlBody=<html>)`.
     Exactly that one address from the output; nothing in cc or bcc; subject and body as given.
     Then `uv run jobhunter digest record <notification_id> sent --message-id <id from Gmail>`.
     If the connector returns an error: `... record <id> failed --error "<short reason>"`.
     If you cannot tell whether it went out (timeout, no answer): `... record <id> uncertain --error "<reason>"`,
     and never send it again yourself.
   - `status: skipped` → nothing new; send nothing. `blocked` or `in_progress` → send nothing, say so.
5. `uv run jobhunter cloud export --out ~/hunter-state-out.json.gz --json` → base64 → upload as the new state file.
   If the upload fails, say so plainly: the email may have gone out but the history did not reach Drive;
   the next run will be told by the person.
6. Report in two or three lines: jobs found, email sent or not and why, state saved or not.

## Sync on the person's computer (`cloud sync`)
1. Newest state file from Drive → a temporary file → `uv run jobhunter cloud import <file> --json`.
   This adds the cloud's jobs and email history. It never changes notes, favorites, saved jobs,
   applications, or the profile on this computer.
2. `uv run jobhunter cloud export --out <temp> --json` → upload as the new state file, so the next
   cloud run knows what the person dismissed or applied to since.
3. Tell the person what arrived: how many new jobs, and the last cloud email.

## Never
- Send to any address other than the one in `digest prepare`'s output, or add recipients.
- Email an employer, recruiter, or anyone else. Hunter prepares; the person sends.
- Put the CV, notes, drafts, favorites, or any token in Drive, in a prompt, or in a report.
- Claim an email was sent before Gmail returned a message id, or claim a hunt ran when the command failed.
