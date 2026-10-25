# Privacy and security

## Where your data is
Everything is on your computer under `~/.jobhunteros` (or `JOBHUNTER_HOME`): the SQLite database, your original documents (byte-identical, hashed), extracted text, exports, backups, logs, locks. Nothing is uploaded to KubeCraft; there is no telemetry or cohort analytics. The repository's `.gitignore` excludes databases, documents, exports, logs, `.env`, tokens, and client secrets as a safety net, but the real protection is that the data folder is outside the repository.

## What can leave your machine
- Read-only requests to the job sources you enabled (board tokens and feed queries only; never your profile).
- Public GitHub metadata for repositories you listed.
- With your consent: minimized job and profile text to Claude (docs/model.md); your digest to your confirmed address; OAuth exchanges with Google.
- When you press Connect Gmail and no client is configured locally: one HTTPS download of KubeCraft's public OAuth client details (`https://jobhunter.kubecraft.nl/google-oauth-client.json`). Nothing about you is sent with it.
`uv run jobhunter consent list` shows every permission you granted and when.

## Secrets
Tokens and passwords go to the operating-system credential store (macOS Keychain, Windows Credential Locker, Linux Secret Service) through `keyring` when one is available. Otherwise they go to `secrets.enc` in the data folder, encrypted with a key derived from `JOBHUNTER_SECRETS_PASSPHRASE` when that variable is set (PBKDF2 + Fernet). Without the passphrase the file is protected only by owner-only file permissions, which is **not encryption**; `jobhunter doctor` and the Connections page say which mode you are in. Threat model for the file backend: it defends against casual copying of the folder or backups (with passphrase) and against other local users; it does not defend against code running as you or anyone who can read the worker's environment. Secrets are never put in model prompts, logs, subprocess arguments, or exports.

## Untrusted input
Job pages, documents, repositories, and emails are data. In code: HTML is stripped (scripts, styles, tracking links removed), documents are parsed without executing embedded content (pypdf; DOCX via zip + XML with size limits), model outputs are validated against schemas and evidence, the digest recipient is enforced by the transport layer from the consent record, and scheduled runs cannot change settings. Prompt text repeats these rules but does not rely on them.

## Network safety
All fetches go through one client: HTTPS only, public hosts only (private, loopback, link-local, and metadata addresses are refused, on every redirect hop), bounded redirects, size caps, timeouts, bounded retries. User document paths are handled separately from URLs and only via the CLI; the dashboard accepts uploads, never paths.

## Dashboard
Binds to 127.0.0.1 only, rejects other `Host` headers, checks `Origin`/`Referer` on every state-changing request, uses a per-installation CSRF token tied to a session cookie, sets a strict Content-Security-Policy and other security headers, exposes no file-read or raw-query endpoints, and limits uploads by size and type. No public tunnel. Binding to another address prints a warning and is your deliberate choice; it has no authentication layer for remote use in this version.

## The application workspace
The dashboard's database is private to its owner (owner-only read and write rules). It holds the student's own
application drafts, answers, contacts, follow-ups and CV names, because that is where they edit them; it never
holds a CV file or its text, nor a token. CV files live only in Hunter's data folder on the computer where they
were added; the state file in the student's private repository carries CV names, versions and hashes, never
files, text, notes, answers or drafts. Requests from the page carry only a kind, an id and bounded parameters.

## Assisted Apply
The form lane runs only on the student's own computer, in a browser they can see, with the identity they
confirmed, the exact CV version they chose and their own answers. The read-back, the approval hash, the receipt
and two screenshots are stored privately in the data folder; the one-time code is never stored. Nothing is sent
to an employer without the student's approval of the exact read-back, once per submission.

## Backups, export, deletion
`uv run jobhunter data backup` copies the database (copy the documents folder too for a full backup). `data export` writes JSON per table plus your original documents; secrets are excluded. `data delete --confirm DELETE` removes secrets first, then the database, documents, derived text, exports, cache, logs, and locks; backups are kept on purpose and listed so you can remove them yourself. Copies you exported or emailed, and anything held by Google or your mail provider, are outside Hunter's reach; the deletion screen says so. "CV immutability" means Hunter never modifies originals in normal use; you can always delete them.

## Memory across restarts
Approved profile, preferences (with history and undo), dismissals, applications, events, notifications, and consents persist in the database and survive restarts and code updates (migrations are versioned). Learning comes only from explicit feedback: after repeated dismissals with the same reason Hunter asks one question and applies a reversible preference only on a yes.
