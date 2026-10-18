# Troubleshooting (say what happened, then the next useful action)

| Symptom | What to say / do |
|---|---|
| `hunt run` refuses: readiness or approval missing | Explain in one sentence; continue setup or offer the demo (`uv run jobhunter --demo demo seed`, then `--demo jobs list`). |
| A source shows "I couldn't check this source" | The other sources still worked. `uv run jobhunter sources check <id>` retries once; if it keeps failing, leave it and move on. Jobs from that source are "couldn't verify", never "closed". |
| "A hunt is already running" | Wait a minute; if it persists for 20 minutes the stale run is cleared automatically. |
| Dashboard not reachable | Run `uv run jobhunter dashboard` in its own terminal; it serves http://127.0.0.1:8765/ only on this computer. |
| Schedule on but nothing happens | A worker must run: `uv run jobhunter worker run` (keep the terminal open) or install a service from `uv run jobhunter schedule install --print`. Closing the browser does not stop a worker; turning the computer off does. |
| Email digest fails | Needs a confirmed address (`digest address`) and a transport (`digest smtp ...` or `email connect gmail-send`). Reading the mailbox is a separate permission and is not needed for digests. |
| Gmail token expired after a week | Google projects in Testing status expire tokens after 7 days; reconnect with `email connect gmail-read`. See docs/email.md. |
| Document could not be read | Scanned PDF or protected file: ask for a text-based PDF, DOCX, or pasted text. Never guess the content. |
| Claude explanations unavailable | Results stay rule-based and are labelled that way. You can write explanations in this session via `match pending` / `match submit`. |
| Environment problems | `uv run jobhunter doctor` lists checks with fixes. |
| Person wants their data gone | `uv run jobhunter data export` first if they want a copy, then `uv run jobhunter data delete --confirm DELETE`. Backups and copies elsewhere are not touched; say so. |
