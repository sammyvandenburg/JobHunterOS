# Setup

## Supported platforms
Linux, macOS, and Windows with Python 3.11+ and `uv`. The dashboard runs on http://127.0.0.1:8765/ and only answers to localhost. Tested in this build on Ubuntu 24.04 with Python 3.13; macOS and Windows paths are covered by the service templates and `pathlib` but were not exercised on real machines here.

## First run
1. `uv sync` (add `--extra dev` for tests, `--extra browser` for the Playwright smoke test, `--extra api` to use a direct Anthropic API key).
2. `uv run jobhunter doctor` checks Python, dependencies, the data folder, secret storage, the Claude CLI, and internet access to a job API. Each failed check prints a fix.
3. Start in Claude Code with `/jobhunter`, or run `uv run jobhunter setup next` and answer one step at a time, or open `uv run jobhunter dashboard`.

Setup is four short steps: (1) "Have you finished your CV and LinkedIn profile in JobMagnetOS?" (2) give two files: your CV and your LinkedIn profile saved as PDF (More → Save to PDF on your profile); the profile is built automatically; (3) "Here is what I understand about you. Does this look right?"; (4) one combined question: where you live, which countries you can work in, lowest salary (optional), remote (must / nice / don't mind), how often to look, how to tell you. Timezone is detected from the computer. Visa and relocation are never asked; they are under "More options" for people who want them. Answered steps are never asked again.

## Documents
Supported: PDF, DOCX, Markdown, plain text, up to 10 MB. Originals are stored byte-for-byte under `~/.jobhunteros/documents/<sha256>.<ext>` and never modified. Scanned PDFs without a text layer are reported as unreadable; Hunter asks for a text-based file instead of guessing. A new CV creates a new version and reopens profile approval.

## Where state lives
`~/.jobhunteros` by default; set `JOBHUNTER_HOME` to move it. Demo mode uses `~/.jobhunteros/demo`. Subfolders: `documents`, `derived` (extracted text), `exports` (digests, data exports, InterviewOS hand-offs), `backups`, `logs`, `locks`, and the SQLite database `jobhunter.sqlite3`. Secrets are in the OS keyring when available, otherwise `secrets.enc` (see docs/privacy.md).

## Updating
`git pull` then `uv sync`. Database migrations run automatically on the next command. Your data folder is outside the repository, so updates never touch it. Make a backup first if you like: `uv run jobhunter data backup`.

## Environment variables (all optional)
See `.env.example`. `JOBHUNTER_HOME`, `JOBHUNTER_DEMO`, `JOBHUNTER_DASHBOARD_HOST/PORT`, `JOBHUNTER_SECRETS_BACKEND`, `JOBHUNTER_SECRETS_PASSPHRASE`, `ADZUNA_APP_ID/KEY`, `ANTHROPIC_API_KEY`.

## Browser smoke test
`uv sync --extra browser`, then `uv run playwright install chromium` (downloads a browser) or point `JOBHUNTER_CHROMIUM` at an existing Chrome/Chromium binary, then `uv run pytest tests/test_browser.py`. The test starts the demo dashboard on a free local port, clicks through the main pages, saves a job, and checks keyboard focus.
