# Troubleshooting (say what happened, then the next useful action)

| Symptom | What to say / do |
|---|---|
| `hunt run` refuses: readiness or approval missing | Explain in one sentence; continue setup or offer the demo (`uv run jobhunter --demo demo seed`, then `--demo jobs list`). |
| A source shows "I couldn't check this source" | The other sources still worked. `uv run jobhunter sources check <id>` retries once; if it keeps failing, leave it and move on. Jobs from that source are "couldn't verify", never "closed". |
| "A hunt is already running" | Wait a minute; if it persists for 20 minutes the stale run is cleared automatically. |
| Dashboard not reachable | Run `uv run jobhunter dashboard` in its own terminal; it serves http://127.0.0.1:8765/ only on this computer. |
| Schedule on but nothing happens | Automatic hunting runs while Hunter (the dashboard) is open. To keep it running when Hunter is closed, the student ticks the approval box and presses "Keep hunting when Hunter is closed" on the Connections page (or `uv run jobhunter schedule automatic on --keep-running-when-closed`). `uv run jobhunter schedule status` says what is running it. |
| A scheduled email failed | It shows under Needs attention with "Send again" and "Dismiss"; Hunter retries by itself after about 30 minutes (up to three times) once Gmail works again, never twice for the same digest. Uncertain sends wait for the student to say whether they arrived. |
| Email digest fails | Needs Gmail connected (Connections page → Connect Gmail, or `email connect gmail-send`), the address confirmed ("Yes, send them there" / `digest use-connected-address`) and a test email (`digest test`). SMTP is an advanced fallback. Reading the mailbox is a separate permission and is not needed for digests. |
| "Your Gmail connection stopped working" | Google ended the sign-in (Testing-status projects expire tokens after 7 days). Press "Connect Gmail again" on the Connections page. See docs/email.md. |
| "Google sign-in is not available yet" | KubeCraft's shared OAuth client is not published or not reachable. See the KubeCraft checklist in docs/email.md. |
| Document could not be read | Scanned PDF or protected file: ask for a text-based PDF, DOCX, or pasted text. Never guess the content. |
| Claude explanations unavailable | Results stay rule-based and are labelled that way. You can write explanations in this session via `match pending` / `match submit`. |
| Environment problems | `uv run jobhunter doctor` lists checks with fixes. |
| Person wants their data gone | `uv run jobhunter data export` first if they want a copy, then `uv run jobhunter data delete --confirm DELETE`. Backups and copies elsewhere are not touched; say so. |
