# Troubleshooting

Every error Hunter shows says what happened and what to do next. This page covers the ones you may meet.

| What you see | Why | What to do |
|---|---|---|
| `uv: command not found` | uv is not installed | Install from https://docs.astral.sh/uv/getting-started/installation/ and reopen the terminal. |
| `jobhunter doctor` fails a check | Missing dependency, no internet, unwritable data folder | Each failed check prints its fix; usually `uv sync`. |
| "Please confirm first that your CV and LinkedIn profile are finished" | Live hunting is gated behind readiness | Answer the readiness question (`jobhunter setup answer readiness yes`), or try the demo (`--demo demo seed`). |
| "Your profile is not approved yet" | No approved profile version | `jobhunter profile build`, review, `jobhunter profile approve`. |
| "A new document was added. Please review..." | A new CV/LinkedIn version reopened approval | Check `jobhunter profile show`, then approve again. |
| "This PDF has no readable text layer" | Scanned PDF | Export a text-based PDF or DOCX from your editor, or paste the text (`docs linkedin --text`, dashboard paste box). |
| "I couldn't check this source. Your other searches still worked." | One board or feed failed (network, 404 after an employer moved ATS, rate limit) | Nothing urgent. `jobhunter sources check <id>` retries; `sources disable <id>` if an employer no longer uses that board. Jobs from it are marked "couldn't verify", never closed. |
| Hunt says `failed` | Every source failed | Check your internet and `jobhunter doctor`. The empty job list does not mean there are no jobs. |
| "A hunt is already running" | Another process holds the hunt lock | Wait; stale runs are cleared after 20 minutes without a heartbeat. |
| Dashboard shows "This dashboard only answers to localhost" | You opened it with another hostname | Use http://127.0.0.1:8765/. |
| "Form expired or invalid" | CSRF token mismatch (old tab, cleared cookies) | Reload the page and try again. |
| "Automatic hunting: on, but nothing is running it" | Hunter is closed and no login task is installed | Open Hunter (the schedule runs inside it), or tick the approval box and press "Keep hunting when Hunter is closed" on the Connections page. |
| Digest `failed: No email transport` | Gmail is not connected (and no SMTP fallback) | Press Connect Gmail on the Connections page (or `jobhunter email connect gmail-send`). Preview still works. |
| "Google sign-in is not available yet" | Hunter could not get KubeCraft's Google sign-in details | Try again with internet; if it keeps failing, KubeCraft has not published the OAuth client yet (docs/email.md), or set `JOBHUNTER_GOOGLE_CLIENT_ID/SECRET` or drop the client JSON in `~/.jobhunteros/google_client.json`. |
| Digest `uncertain` | The mail server errored after accepting the connection | Check your inbox; Hunter will not resend that digest automatically. `digest send --confirm` sends a new one only if there is new content. |
| "Your Gmail connection stopped working" / Needs attention | Google ended the sign-in (Testing-mode tokens expire after 7 days) or you removed access | Press "Connect Gmail again" on the Connections page. Your address and history stay. See docs/email.md for the Google rules. |
| Gmail: 403 | The granted scope does not allow the call | Reconnect; make sure you accepted the read-only permission. |
| "Secret storage: file-plain ... not encryption" | No OS keyring available | Set `JOBHUNTER_SECRETS_PASSPHRASE` (also in the worker's environment) or install a keyring backend. |
| Claude explanations say "unavailable" | No `claude` login and no API key | Fine: results are rule-based. In Claude Code you can write explanations in-session (`/jobhunter`). |
| `claude -p` costs | Unattended explanations use your account | `jobhunter model set --budget 0.20` lowers the per-run cap; `model disable` turns it off. |
| Jobs you dismissed come back | They changed materially, or you asked to reconsider | Dismissals are per job; another role at the same employer is still shown on purpose. |
| Totals look wrong | Counts come from recorded events | `jobhunter act history <job id>` shows every event; `act undo <event id>` reverts one. |
| Want to start over | | `jobhunter data export` (optional), then `jobhunter data delete --confirm DELETE`. |

Logs: the worker prints to its terminal or to the paths in the service template; the CLI prints errors directly. Nothing personal is logged beyond what you see.

## "Hunter cannot read its secret store"
The file that holds your email tokens is encrypted with the passphrase in `JOBHUNTER_SECRETS_PASSPHRASE`. If that passphrase changed or the file is damaged, Hunter stops and changes nothing: it never replaces your tokens with an empty file. Set the passphrase you used before, or run `uv run jobhunter data reset-secrets`, which moves the unreadable file aside (it is kept, not deleted) and lets you connect email again.

## "An email digest may or may not have arrived"
When the email provider does not confirm delivery, Hunter records the digest as uncertain and never sends it again by itself. Under Needs attention you can say "It arrived" or "Send again". Sending again can mean you get the same email twice; Hunter says so before it does it.
