# Onboarding flow

Run `uv run jobhunter setup next --json`. It returns exactly one step. Ask that question in the given wording (you may shorten it). Store the answer with `uv run jobhunter setup answer <key> "<answer>"` unless the step needs a command:

| key | how to store |
|---|---|
| readiness | `setup answer readiness yes` / `no`. On "no", stop kindly: Hunter works from finished materials; progress is saved; offer the demo. |
| cv | Ask for a file path. `uv run jobhunter docs add --kind cv <path>`. If the result says the text could not be read (scan, password), say exactly that and ask for a text-based file or pasted text. Never pretend it was read. |
| linkedin_url | `setup answer linkedin_url <url>` |
| linkedin_content | `uv run jobhunter docs linkedin --file <path>` or `--text "<pasted>"`. Explain how to export (LinkedIn → Settings → Data privacy → Get a copy of your data, or profile → More → Save to PDF). |
| github | Optional. `uv run jobhunter evidence github <owner/repo> ...` or `setup answer github skip`. Ownership is asked later, never assumed. |
| profile_build | `uv run jobhunter profile build --json`, then refine (below). |
| profile_approve | Show the summary: "Here is what I understand about you. Does this look right?" On yes: `uv run jobhunter profile approve`. On corrections: `uv run jobhunter profile correct "<text>"` then show again. |
| current_location, work_countries, sponsorship, relocation, timezone | `setup answer <key> "<value>"`. "Not sure" is allowed for countries, sponsorship, relocation. |
| salary_min, remote | Optional. "skip" is fine. If they give a number, ask whether it is a must or a nice-to-have only if they did not say. |
| hunt_frequency | daily (suggested), every 2/4/6 hours, only when asked. Explain: nothing runs in the background until a worker is started. |
| notify_mode | dashboard only (default), daily email, email when asked. Daily email needs a confirmed address: `uv run jobhunter digest address <email>`. |

## Refining the profile (you are the model here)

1. `uv run jobhunter profile show --json` gives the rule-based draft, warnings, and open questions.
2. `uv run jobhunter docs text <id>` shows the extracted text of a document. Read the CV (and LinkedIn text) yourself.
3. Build a better draft that follows `uv run jobhunter profile schema`:
   - roles with start/end (YYYY-MM), employer, `experience_kind` (professional only for paid work), a document quote as evidence;
   - skills with the line they appear on; certifications only when actually earned (otherwise `course`);
   - projects as personal/guided/educational/team; GitHub repos as `repository` evidence;
   - `needs_clarification` for anything conflicting or unsupported; questions for the person.
   Do not invent years, metrics, titles, or credentials. Do not double-count overlapping jobs (the command recomputes years from merged date ranges anyway).
4. Save it to a temporary file outside the repository (for example in the system temp directory) and run `uv run jobhunter profile submit --file <path> --json`. Read the warnings; tell the person what needs confirmation.
5. Ask the open questions one at a time: `uv run jobhunter profile answer <key> "<answer>"`.
6. Ask for approval. Only after `profile approve` is live hunting allowed.

A new CV later creates a new version and reopens approval; the old file is never changed.
