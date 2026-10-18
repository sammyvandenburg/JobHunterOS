# Email: digests and reading your job-search mail

Two separate permissions, two separate setups. Neither needs the other.

## 1. "Email me jobs" (digest)
- Confirm the one address digests may go to: `uv run jobhunter digest address you@example.com` (or on the dashboard's Connections page). This address is stored as a consent record; the sending code refuses any other recipient, whatever a model, a web form, or a job page says.
- Choose a transport:
  - **SMTP with your own credentials:** `uv run jobhunter digest smtp --host smtp.example.com --port 587 --username you@example.com --from-address you@example.com` (password prompted, stored in the secret store; or set `JOBHUNTER_SMTP_PASSWORD` for the one command). Works with any provider that offers SMTP app passwords.
  - **Gmail send (OAuth):** `uv run jobhunter email connect gmail-send --client-secret client_secret.json`. Requests only `https://www.googleapis.com/auth/gmail.send` (a "sensitive" scope). It cannot read mail.
- `uv run jobhunter digest preview` shows the next digest; `digest send --confirm` sends it. With notification mode "daily email", the worker sends at most one digest per local day after the digest time, and only when there is something new. Every attempt is recorded (`digest history`); a transport error after the connection is marked `uncertain` and never blindly resent; an identical digest is never sent twice.
- Digests contain job and application links, not just a localhost link. Your CV is never attached.

## 2. "Read my job-search emails" (Gmail, read-only, optional)
- `uv run jobhunter email connect gmail-read --client-secret client_secret.json` opens Google's consent screen in your browser and requests only `https://www.googleapis.com/auth/gmail.readonly`. Hunter never asks for your mailbox password.
- **What the token really grants:** read access to the whole mailbox. Hunter applies a query (default: the past 30 days, job-related subjects and senders such as LinkedIn, Indeed, Greenhouse, Lever, Ashby, Workday) and an optional label on its side; the token itself is not limited to those. Change the filter with `uv run jobhunter email filter --query "..." --label "..." --since-days 30`.
- Sync (`uv run jobhunter email sync` or the Connections page): lists matching messages, reads headers and snippets, classifies deterministically (receipt, rejection, interview, offer, recruiter, job alert, other), fetches bodies only for relevant messages, stores a short extract and the message id. Hunter never archives, deletes, labels, or marks anything read.
- Linking: confident matches (employer plus role words, or a known job link) update the application pipeline with reversible events; a late automated receipt never moves a confirmed stage backwards; automated acknowledgements never count as recruiter replies or interviews. Ambiguous matches appear under "Needs attention" for you to confirm or ignore. `email unlink <id>` reverses an inferred update.
- Job alerts (LinkedIn, Indeed) found in synced mail are parsed into jobs with unknown posting dates; this is email import, not platform scraping.
- Disconnect: `uv run jobhunter email disconnect gmail-read` deletes the token locally, asks Google to revoke it, and revokes the consent record. History is kept; reconnecting later does not duplicate events (messages are keyed by Google's message id). You can also remove access at https://myaccount.google.com/permissions.
- No Gmail? `uv run jobhunter email import --file message.txt --sender ... --subject ...` imports a saved email with the same classification and linking.

## Google OAuth setup (what you need to know)
Hunter is a desktop app using the installed-app flow with a loopback redirect (handled by `google-auth-oauthlib`). You need an OAuth client of type "Desktop app" from a Google Cloud project with the Gmail API enabled; download its JSON as `client_secret.json` (keep it out of the repository; `.gitignore` already excludes `client_secret*.json`).

Current Google rules that matter (checked 2026-10-18 at developers.google.com/identity/protocols/oauth2 and the OAuth verification help):
- `gmail.readonly` is a **restricted** scope. A published ("In production") app that requests it must pass Google's OAuth verification, and apps that store or transmit restricted-scope data need an annual security assessment. Hunter processes mail locally and does not transmit mailbox contents to a server by default, but a shared KubeCraft client would still need verification before students outside a Testing user list could use it.
- A project whose consent screen is in **Testing** status issues refresh tokens that expire after 7 days and only works for listed test users (Google caps the test-user list). Expect to reconnect weekly in that mode; Hunter tells you when the token stopped working.
- There is a limit of 100 live refresh tokens per Google account per OAuth client; changing your Google password revokes tokens with Gmail scopes.
- `gmail.send` is a sensitive (not restricted) scope; verification still applies to a published app.

Practical paths: (a) each student creates their own Google Cloud project and Desktop OAuth client and uses Testing mode (weekly reconnect, no verification); (b) KubeCraft publishes one verified client and goes through verification and, for the read scope, the security assessment. Hunter supports both; it does not fake a connection when neither is done. Nothing in this build was tested against a live Google account because no OAuth client was available; the flow, token refresh, revocation, sync, classification, and linking are implemented and covered by fixture tests.

## Model processing of mail
Mailbox content is never sent to Claude by default. Classification and linking are deterministic. If you later enable model help for drafting, only minimized job and profile text is sent, not emails.
