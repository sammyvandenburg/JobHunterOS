# Email: "email me what you find" and, separately, "read my job-search mail"

Two separate permissions, two separate connections. Neither needs the other. Both are Google's
standard sign-in; Hunter never asks for a mailbox password.

## For students: about one minute
On the dashboard, open **Settings → Connections**.

1. Press **Connect Gmail**. Google's own screen opens, asks you to sign in and to allow Hunter to
   *send email as you*. That is the only permission it asks for; it cannot read your mail.
2. You land back in Hunter. It shows the Gmail address you connected and asks: *Should I send your
   job emails there?* Press **Yes, send them there** (or type another address). That one address is
   the only place Hunter will ever send to.
3. Press **Send test email**. A short email arrives from your own address. If it does, you are done.
4. Choose how often Hunter looks (every day at a time you pick, or every 2 / 4 / 6 hours) and tick
   **Email me what you find (once a day)**. Press **Save**. Automatic hunting now runs while Hunter is
   open. If you also want it to run when Hunter is closed, tick the approval box and press
   **Keep hunting when Hunter is closed**: Hunter adds a small login task on your computer
   (launchd on macOS, a systemd user service on Linux, Task Scheduler on Windows) and removes it again
   from the same page.

The status line at the top says what is true right now: **Connected**, **Sending enabled**,
**Automatic hunting: running / off / installed, waiting**, and **Needs attention** when something
stopped working (for example, Google ended the sign-in; press **Connect Gmail again**).

**Disconnect Gmail** deletes Hunter's permission locally and asks Google to revoke it. Your email
history and your address choice stay. **Turn off automatic hunting** stops the schedule and removes
the login task if one was installed. You can also remove Hunter at
https://myaccount.google.com/permissions at any time.

The same steps from a terminal: `uv run jobhunter email connect gmail-send`, `digest use-connected-address`
(or `digest address you@example.com`), `digest test`, `schedule set daily --time 08:00`,
`schedule notify daily_email`, `schedule automatic on` (add `--keep-running-when-closed` for the login task).

## What Hunter sends, and how it keeps that safe
- Only digests and test emails, only to the one confirmed address, only when you asked for email.
  The address is a consent record; the sending code refuses any other recipient, whatever a model,
  a web form, or a job page says. Connecting or disconnecting a Gmail account never changes it.
- Every send is written to an outbox row before anything leaves your computer. The same digest to the
  same address goes out at most once; a click and the scheduler at the same moment cannot both send
  it. A timeout or crash during sending leaves the row **uncertain**, and uncertain digests are never
  resent automatically: they appear under Needs attention with "It arrived" and "Send again".
- With "daily email", the scheduler sends at most one digest per local day, after your digest time,
  and only when there is something new. Everything attempted is in **Email history** on the
  Connections page and in `uv run jobhunter digest history`.
- Digests contain job and application links. Your CV is never attached. Nothing is ever sent to an
  employer; Hunter prepares applications and drafts, it does not submit or send them.

## "Read my job-search emails" (optional, read-only, separate)
- **Connect Gmail (read-only)** on the Connections page (or `uv run jobhunter email connect gmail-read`)
  asks Google for `gmail.readonly` only. A read-only connection can never send; a sending connection
  can never read. Hunter checks the granted scopes and refuses a connection that mixes them.
- **What the token really grants:** read access to the whole mailbox. Hunter applies a query (default:
  the past 30 days, job-related subjects and senders such as LinkedIn, Indeed, Greenhouse, Lever,
  Ashby, Workday) and an optional label on its side; the token itself is not limited to those.
  Change the filter with `uv run jobhunter email filter --query "..." --label "..." --since-days 30`.
- Sync (`uv run jobhunter email sync` or the Connections page) reads headers and snippets, classifies
  deterministically (receipt, rejection, interview, offer, recruiter, job alert, other), fetches bodies
  only for relevant messages, and stores a short extract and the message id. Hunter never archives,
  deletes, labels, or marks anything read.
- Linking: confident matches update the application pipeline with reversible events; a late automated
  receipt never moves a confirmed stage backwards; automated acknowledgements never count as recruiter
  replies or interviews. Ambiguous matches wait under "Needs attention". `email unlink <id>` reverses
  an inferred update.
- Job alerts (LinkedIn, Indeed) found in synced mail are parsed into jobs with unknown posting dates;
  this is email import, not platform scraping. The HTML part is read with its links kept, so the
  "View job" buttons still yield the vacancy link and the job card around it. Only tracking
  parameters are removed.
- No Gmail? `uv run jobhunter email import --file message.txt --sender ... --subject ...` imports a
  saved email with the same classification and linking.
- Mailbox content is never sent to Claude. Classification and linking are deterministic.

## Advanced: SMTP instead of Gmail
Not the default, and not shown unless you open **Advanced** on the Connections page. Any provider
with SMTP app passwords works:
`uv run jobhunter digest smtp --host smtp.example.com --port 587 --username you@example.com --from-address you@example.com`
(the password is prompted and stored in the secret store, or set `JOBHUNTER_SMTP_PASSWORD` for that
one command). The recipient rule, the outbox and the history are the same as with Gmail.

## How the Gmail connection works (for KubeCraft and the curious)
- Hunter is a desktop app. It runs Google's standard authorization-code flow with PKCE (S256) and a
  loopback redirect back to the dashboard (`http://127.0.0.1:8765/connect/gmail/callback`), from the
  Connections page. The terminal command uses the same client through `google-auth-oauthlib`.
- Tokens go to the secret store (OS keychain, or the encrypted file; see docs/privacy.md), never into
  the database, logs, exports, or model prompts. Access tokens are refreshed with Google when needed;
  when Google refuses the refresh (sign-in expired or revoked), the account is marked and the dashboard
  says **Connect Gmail again** instead of failing quietly.
- One KubeCraft-managed OAuth client serves every student; no student creates a Google Cloud project.
  Hunter finds the client id and secret in this order: the environment
  (`JOBHUNTER_GOOGLE_CLIENT_ID` / `JOBHUNTER_GOOGLE_CLIENT_SECRET`), the file
  `~/.jobhunteros/google_client.json` (either `{"client_id": ..., "client_secret": ...}` or the JSON
  Google lets you download), or KubeCraft's published copy at
  `https://jobhunter.kubecraft.nl/google-oauth-client.json` (override with `JOBHUNTER_GOOGLE_CLIENT_URL`),
  which it caches into that file. Google documents that a "Desktop app" client secret is not a
  confidential value (an installed app cannot keep secrets; PKCE and the per-user consent screen are
  the protection). It is still never committed to this repository; `.gitignore` excludes
  `client_secret*.json` and `google_client.json`, and a test fails if a Google secret pattern appears
  in the code or docs.
- Until the client is available, the Connections page says *Google sign-in is not available yet* and
  hides the button. Nothing pretends to be connected.

## Google-side setup KubeCraft must complete before release
Checked against Google's OAuth documentation and verification help on 2026-10-18; Google changes
these rules, so re-check before publishing.

1. **Google Cloud project** with the **Gmail API** enabled.
2. **OAuth consent screen**, user type **External** (students use personal Google accounts).
   Fill in: app name ("Hunter by KubeCraft"), user support email, developer contact, app logo
   (optional but part of branding review), **authorized domain** (for example `kubecraft.nl`),
   **home page** and **privacy policy** URLs on that domain (docs/privacy.md is a good basis), and
   terms of service if you have them.
3. **Scopes**: add `https://www.googleapis.com/auth/gmail.send` (a *sensitive* scope). Add
   `https://www.googleapis.com/auth/gmail.readonly` (a *restricted* scope) **only if** you want to
   offer the optional mailbox reading to students outside a test-user list. Hunter asks for each
   scope in a separate connection, never both at once.
4. **OAuth client** of type **Desktop app**. Download its JSON when you create it (Google now shows
   the secret only at creation time). No redirect URI needs registering for Desktop clients; Google
   accepts loopback addresses such as `http://127.0.0.1:<port>/...`.
5. **Publish the client to students** in one of the ways above. Hosting the downloaded JSON (or
   `{"client_id": ..., "client_secret": ...}`) at `https://jobhunter.kubecraft.nl/google-oauth-client.json`
   over HTTPS makes the Connect Gmail button work with no setup on the student's side. Serving it
   from a different URL means setting `JOBHUNTER_GOOGLE_CLIENT_URL`, or shipping the file in the
   student's data folder.
6. **Publishing status and verification.** This is the part that takes time:
   - In **Testing** status the app works for up to **100 listed test users**, and Google **expires
     refresh tokens after 7 days**, so each student must press Connect Gmail again every week. Hunter
     handles that (Needs attention → Connect Gmail again), but it is not a release experience.
   - Moving to **In production** with `gmail.send` requires Google's **brand (app) verification**:
     the consent-screen details above, a justification for the scope ("sends the user's own job
     digest to the user's own address, on the user's request"), and a short **demo video** of the
     sign-in flow and how the permission is used. Until verification is granted, students see the
     "Google hasn't verified this app" warning and Google caps an unverified sensitive-scope app at
     **100 users**.
   - Offering `gmail.readonly` in production additionally requires Google's **restricted-scope
     review** and an annual **CASA security assessment** by an authorized lab. Hunter processes mail
     locally and sends none of it anywhere, which helps the justification, but it does not remove the
     assessment. Recommendation: release with sending only, and keep mailbox reading in Testing (or
     per-student clients) until KubeCraft decides the assessment is worth it.
7. **Operational limits to know:** 100 live refresh tokens per Google account per client (older ones
   are revoked silently), a Google password change revokes Gmail-scoped tokens, and Google may
   revoke tokens unused for six months. Hunter treats all of these as "sign in again".

## Why Hunter does not reuse Claude's Gmail connector
Claude Code's Gmail connector (claude.ai) is only available inside a Claude session. Its tokens are
held by claude.ai and cannot be exported to a local program; the connector does not support local
OAuth from Claude Code; headless `claude -p` only loads connectors under a personal claude.ai login,
which Anthropic's terms do not allow a distributed app to rely on; and cloud Routines cannot read the
student's private `~/.jobhunteros` folder. Scheduled digests from a student's own computer therefore
need Hunter's own Google permission, which is what the Connect Gmail button sets up. Inside a Claude
Code session you can of course still ask Claude to mail you something using its own connector; that is
a separate permission with a separate audit trail.

## What was tested here
The full journey (Connect Gmail → confirm address → test email → choose daily email → restart Hunter
→ scheduled hunt and digest → disconnect) runs in the test suite against a fake Google served on
localhost, both through the HTTP layer and in a real browser (`tests/test_email_setup.py`,
`tests/test_email_journey.py`). The fake implements Google's redirects, PKCE check, token exchange,
refresh, revocation, profile lookup and `messages/send`. No live Google account or OAuth client was
available in this environment, so the last step, a real Google consent screen, is still a manual check.
