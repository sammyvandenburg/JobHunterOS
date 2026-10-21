# Setup flow (four steps, nothing asked twice)

Run `uv run jobhunter setup next --json`. It returns one step. Ask it in simple words.

| key | what to do |
|---|---|
| readiness | Ask: "Have you finished your CV and LinkedIn profile in JobMagnetOS?" → `uv run jobhunter setup answer readiness yes` (or `no`: stop kindly, progress is saved). |
| documents | Ask for both files in one message: "Please give me two files: your CV, and your LinkedIn profile saved as PDF. To get the PDF: open your LinkedIn profile, click 'More' under your name, then 'Save to PDF'. Drag both files into this window." Then `uv run jobhunter docs add --kind cv <file>` and `uv run jobhunter docs add --kind linkedin <file>`. If a file cannot be read (scan, protected), say so plainly and ask for a text-based file. If macOS blocks a folder, say: "Drag the file into this window instead." The profile is built automatically once both files are readable. |
| profile_approve | Refine the draft (below), then show the summary in a few short lines: "Here is what I understand about you. Does this look right?" On yes → `uv run jobhunter setup answer profile_approve yes`. On a correction → `uv run jobhunter profile correct "<their words>" --json`: it changes nothing yet and returns a `confirmation` line such as "Remove AWS. Keep Kubernetes and Terraform. Correct?". Show that line word for word, wait for a clear yes, then run the same command with `--yes`; on no, ask what to change instead. Then show the profile again. |
| basics | Ask all six at once, exactly as the step's `question` text. Then one command: `uv run jobhunter setup basics --location "..." --countries "..." --salary "..." --remote "..." --frequency "..." --notify "..."`. Pass only what they answered; "skip" is fine for salary and remote. Timezone is detected automatically (`detected_timezone`); only ask if they say it is wrong. |

Never ask about visas, sponsorship, or moving. Never ask for the LinkedIn link; it is optional (`--linkedin`).

When the basics are saved, do not stop at "All set": run the first hunt (`uv run jobhunter hunt run --json`, see [hunting.md](hunting.md)), then create their dashboard and fill it ([board.md](board.md)), and hand over the link with the best two or three matches in one short message. Email and nightly hunting come later, one offer at a time.

## Refining the profile (you are the model here)

1. `uv run jobhunter profile show --json` shows the rule-based draft, warnings, and open questions. `uv run jobhunter docs text <id>` shows the text of a document. Read the CV and LinkedIn text yourself.
2. Write a better draft following `uv run jobhunter profile schema`: paid roles with YYYY-MM dates and a quote from the document as evidence; skills with the line they appear on; certifications only when earned (otherwise `course`); projects as personal, guided, or team; anything unclear as `needs_clarification` with a question.
   Never invent years, titles, metrics, or credentials. The command recomputes years from merged dates.
3. Save it to a temporary file outside the repository and run `uv run jobhunter profile submit --file <path> --json`. Read the warnings.
4. Ask open questions one at a time, in plain words: `uv run jobhunter profile answer <key> "<answer>"`. Usually there is just one: which direction they want most (DevOps, Platform, SRE, Cloud, Infrastructure).
5. Ask for approval.

A new CV later creates a new version and asks for approval again. The old file is never changed.
