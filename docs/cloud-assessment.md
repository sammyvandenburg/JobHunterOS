# Assessment: simple email and nightly hunting while the laptop is off (2026-10-19)

Scope: keep everything that exists (Claude Code skill, Python backend, matching engine, SQLite,
dashboard, Gmail OAuth). Decide how students get job emails without connecting Gmail, and how hunts
keep running when Claude Code is closed and the laptop is asleep or off. This is an assessment and a
proof-of-concept plan, not an implementation. Prices and limits below were checked against the
providers' public pages on 2026-10-19 and should be re-checked before anything is bought.

## 0. The one fact that shapes everything
When the laptop is off, nothing on it runs and nothing on it can be read. So a nightly hunt needs
three things somewhere else: (a) a copy of the student's *inputs* (approved profile, preferences,
country rules, dismissed and applied jobs), (b) a place to run the hunt and matching, (c) a way to
send the email and to hand the results back to the laptop later. Whatever runs in the cloud must be
the same deterministic Python that runs locally, so results and groups stay identical, and the local
dashboard must keep working with no cloud at all.

The good news: the inputs are small (tens of kilobytes of JSON; the CV itself never needs to leave
the laptop, only the approved, evidence-backed profile), the hunt is the same for every student
(the same employer boards and feeds), and the per-student part (matching, grouping, digest) is cheap.

## 1. Simple email notifications

### Option A: KubeCraft-managed sender (what students would see)
Settings → Connections → "Email me": type an address, Hunter sends a 6-digit code, the student types
it back, done. Digests arrive from `hunter@kubecraft.nl`. No Google screen, no Gmail, no SMTP.

How it works: a KubeCraft-run sending service (transactional email provider behind a tiny API).
The student's Hunter, local or cloud, calls `POST /v1/mail/send` with a per-student token obtained at
verification time; the service enforces "only to the verified address", rate limits, and keeps the
provider key. The local outbox, allowlist and dedupe stay exactly as they are; this is just a third
transport next to SMTP and Gmail (`digest.transport = "kubecraft"`).

- Cost: Resend (free tier 3,000 emails/month, 100/day; Pro US$20/month for 50,000) or Amazon SES
  (about US$0.10 per 1,000 emails plus a verified domain). 1,000 students × 1 email/day ≈ 30,000/month:
  US$20/month on Resend, about US$3/month on SES. Domain setup once (SPF, DKIM, DMARC on a
  subdomain such as `mail.kubecraft.nl`, so a bad day never hurts kubecraft.nl's own mail).
- Authentication: email-address verification code plus a per-student bearer token stored in the
  secret store. No Google involved. Tokens can be revoked server-side.
- Privacy: the digest body (job titles, employers, links, the student's address) passes through the
  KubeCraft service and the provider. No CV, no profile. Needs a line in the privacy doc and a
  processing agreement with the provider (both Resend and SES offer EU data residency; pick EU).
- Reliability: transactional providers are what every SaaS uses; deliverability is better than a
  Gmail-API send from a brand-new personal account, and far better than SMTP app passwords.
- Operational complexity: one small HTTPS service (or a few serverless functions) and one provider
  account. KubeCraft owns uptime and abuse handling (verification-code spraying, which the rate
  limit covers). If the cloud worker from part 2 exists, the sender lives inside it: no extra service.

### Option B: the existing Gmail OAuth sending
Already built and tested against a fake Google. Costs nothing per email. But: it only works after
KubeCraft finishes Google's brand verification for `gmail.send`; until then it is capped at 100
users with a scary warning and 7-day sign-in expiry in Testing status. Each student goes through a
Google consent screen. Every student's laptop holds a refresh token. When the laptop is off, the
*cloud* cannot send from the student's Gmail unless that refresh token is copied to the cloud, which
is exactly the kind of secret we do not want to centralize.

### Verdict for email
Use the KubeCraft-managed sender as the default ("type your address, enter the code"). Keep Gmail
OAuth as the optional "send from my own address" and "read my inbox" features it already is. The
Google verification work can then proceed at its own pace instead of blocking release. Cost is
negligible; the real cost is owning one small service, which part 2 needs anyway.

## 2. Nightly hunting while the laptop is off

Four candidates, measured against: setup a student must do, what KubeCraft must run, where private
data lives, reliability, cost at 1 and at 1,000 students, and how it fits the existing code.

### 2a. GitHub Actions scheduled workflow (per student)
Each student creates a private repo from a KubeCraft template; a workflow runs `jobhunter hunt` on a
cron, with the profile and preferences committed as JSON (or stored as repository secrets) and the
results committed back or uploaded as artifacts; the laptop pulls them with `jobhunter cloud sync`.

- Setup: GitHub account, "use this template", paste profile JSON, add 1–2 secrets, enable Actions.
  Fifteen minutes for a DevOps student; many small places to get wrong.
- KubeCraft runs: nothing. Each student's own GitHub minutes (2,000/month free on private repos;
  a nightly 3-minute run uses about 90).
- Data: approved profile and preferences in the student's private repo (GitHub hosts it; the student
  owns it). Jobs, notes, favorites, applications would have to be committed too for the cloud run to
  respect dismissals and avoid re-recommending applied jobs. Notes and application history in a git
  repo is more exposure than today.
- Reliability: cron on GitHub is best-effort (runs can be delayed by tens of minutes or dropped at
  busy hours), scheduled workflows are disabled after 60 days without repository activity, and a
  failed run is only an email from GitHub. Source fetches happen 1,000 times a night from GitHub's IP
  ranges, once per student, which is the opposite of what the boards want and what our rate limits
  were designed for.
- Email: still needs a sender (option A's token, or SMTP secrets in the repo).
- Code fit: good; the CLI runs unchanged. Needs an export/import of state and a "headless" hunt mode.
- Verdict: fine as a personal hack, wrong as the product: fragile schedule, every student fetches
  the same boards, private data spread over 1,000 repos, support burden lands on KubeCraft anyway.

### 2b. Claude Code cloud Routines
A routine (research preview; Pro/Max/Team) runs a prompt on Anthropic's cloud on a schedule, with a
fresh clone of a repository and the student's claude.ai connectors (Gmail included, sending allowed
without prompts). It cannot read the laptop. So the state problem is the same as 2a (a private repo
holding profile, preferences and results), plus: every run spends the student's subscription usage,
the minimum interval is hourly, the run is model-driven (a prompt asking Claude to run the CLI and
commit results) rather than a deterministic job, and it is a preview product whose limits and
availability can change. Failure notification is whatever the routine's run log shows.
- Verdict: not for the nightly job. Useful later for what the model is good at (writing the fit
  explanations for the next day's digest, if a student opts in), never as the scheduler.

### 2c. A small KubeCraft-hosted worker ("Hunter Cloud")
One multi-tenant service run by KubeCraft:
- fetches every source **once** per night (the same adapters, catalogue, rate limits and terms as
  today; one shared job table instead of 1,000 crawls),
- runs the existing matching engine per student against their uploaded profile and preferences,
- sends the digest through the option-A sender,
- exposes a small API the laptop talks to: `PUT /v1/me/inputs` (profile, preferences, dismissed and
  applied job keys; sent by the dashboard when the student turns cloud hunting on and whenever these
  change), `GET /v1/me/results?since=` (new jobs, groups, explanations), `POST /v1/mail/verify`.
- Setup for the student: one toggle, "Also hunt for me at night when my laptop is off", a KubeCraft
  invite code (or sign-in with the email address they just verified), done. The dashboard uploads the
  inputs and shows "Cloud: last hunt 02:10, 4 new jobs; synced". The local dashboard keeps working
  exactly as now, with or without the cloud.
- Data: approved profile and preferences (not the CV, not notes, not drafts) per student, encrypted
  at rest, under a KubeCraft privacy notice with retention and deletion ("Turn off cloud hunting"
  deletes the student's inputs and results server-side; the local copy is authoritative). Job notes,
  favorites and application history stay local; the cloud only receives job keys to exclude
  (dismissed, applied) so it never re-recommends them. GDPR: KubeCraft becomes a processor of
  minimal profile data; needs a short DPA with the hosting provider and an EU region.
- Reliability: a real scheduler (systemd timer or the platform's cron) with retries, a run log per
  student, and a failure email to KubeCraft when the nightly fetch fails; the student sees "cloud
  hunt did not run last night" in the dashboard instead of silence.
- Cost: 1 student and 100 students fit on the smallest paid VM or container (about US$5–10/month on
  Fly.io or Hetzner, plus the email provider). 1,000 students: nightly fetch is the same; matching
  1,000 profiles against a few thousand jobs is seconds; storage is megabytes. Postgres becomes
  worth it somewhere around several hundred students for concurrent API writes; until then one SQLite
  file per student (the same schema the laptop uses) is enough. Budget US$20–40/month all-in.
- Operational complexity: one service to deploy, monitor, back up and patch. This is the real cost.
  It is also the only option where KubeCraft can see that hunting works for everyone, and fix a
  broken source once instead of answering 1,000 messages.
- Code fit: very good. The fetch, normalize, dedupe, match, digest and outbox code are shared
  already; what is new is a multi-tenant run loop, the inputs/results API, and a `cloud` sync module
  on the laptop. Everything else stays.

### 2d. Hybrid: cloud results, local everything else
Not a separate option; it is how 2c should be built. The cloud produces *results* (jobs and
groups); every action (favorite, note, applied, prepare, drafts) stays local and private. Sync is
one-way for inputs (laptop → cloud) and one-way for results (cloud → laptop), which avoids
conflict handling entirely.

### Comparison

| | GitHub Actions | Claude Routines | KubeCraft worker |
|---|---|---|---|
| Student setup | GitHub repo, secrets, workflow | repo plus routine plus subscription | one toggle plus a code |
| KubeCraft runs | nothing (support only) | nothing | one small service |
| Private data location | student's private GitHub repo | student's private repo plus Anthropic cloud | KubeCraft (EU), minimal inputs, deletable |
| Schedule reliability | best-effort cron, auto-disables | hourly minimum, preview | real scheduler with retries |
| Source load | 1 crawl per student | 1 crawl per student | 1 crawl total |
| Email | needs a sender anyway | student's Gmail connector | built in (option A) |
| Cost at 1,000 | student minutes; support time | student subscription usage | about US$20–40/month |
| Fits existing code | yes (CLI unchanged) | partly (prompt-driven) | yes (shared modules) |

### Recommendation
Build the small KubeCraft-hosted worker, with the managed email sender inside it, as the one piece
of KubeCraft infrastructure. It is the only option that gives the smooth experience ("type your
email, enter the code, flip one switch") and the only one KubeCraft can keep reliable for a thousand
students. Keep the laptop fully functional offline, keep Gmail OAuth optional, and reject GitHub
Actions and Routines for the nightly job on reliability and data-spread grounds. Scale path: start
with one VM and per-student SQLite, move to Postgres when the student count makes it worth it.

## 3. Proof of concept (about two weeks, one person)

Goal: one real student account, one verified email address, one nightly cloud hunt that arrives as
an email and appears in the local dashboard the next morning, with the laptop closed overnight.
Nothing in the current local system is removed or changed in behaviour.

1. **Inputs and results format (day 1–2).** `jobhunter cloud export-inputs` produces a signed JSON
   bundle: approved profile revision, preferences, country rules, policy version, and the keys of
   dismissed and applied jobs. Results format: the existing `recommendations` rows plus job records,
   versioned. Both are plain data already present in SQLite; this is serialization plus tests.
2. **Headless multi-tenant run (day 3–5).** A `jobhunter cloud run-all` command in the same codebase:
   fetch once with the existing hunt pipeline into a shared jobs store, then for each tenant load the
   inputs and run the existing matching engine and digest composer. Deterministic, no model calls.
   Tested with two synthetic tenants: same inputs locally and in the cloud run give identical groups.
3. **Sender (day 5–6).** The `kubecraft` transport: `POST /v1/mail/verify` (code to the address),
   `POST /v1/mail/confirm`, `POST /v1/mail/send` (only to the confirmed address, rate-limited).
   Provider: Resend or SES in the EU region. Local outbox and dedupe unchanged.
4. **Service (day 6–9).** FastAPI app with the three mail endpoints plus `PUT /v1/me/inputs` and
   `GET /v1/me/results`, per-student token, per-student SQLite file, nightly timer running step 2,
   run log, admin failure email. Deployed on one small EU VM or container with HTTPS and daily
   backups. No dashboard, no accounts system beyond the token.
5. **Laptop side (day 9–11).** Connections page gains two cards in the same style as today:
   "Email me" (address plus code, becomes the default transport) and "Hunt at night when my laptop is
   off" (toggle, uploads inputs, shows last cloud hunt, pulls results on dashboard start and on
   "Find jobs now"). Pulled jobs enter the local database through the existing import path, so
   favorites, notes, drafts and applied all work on them. "Turn off" deletes server-side data.
6. **Proof (day 12–14).** Browser journey test against a local instance of the service (the fake
   Google pattern already used); then one real night with a real address and a closed laptop.
   Document what was live-verified.

Out of scope for the PoC, decided later: accounts and invite codes beyond a shared KubeCraft code,
Postgres, inbox reading in the cloud (never: that token stays on the laptop), model-written
explanations in the cloud digest.

## 4. Risks and how the PoC answers them
- **Privacy drift**: the cloud must never receive the CV, notes, drafts or email tokens. The export
  function is the only path and its tests assert exactly which fields leave.
- **Two sources of truth**: avoided by one-way sync. The cloud never edits anything the student did.
- **Source terms**: one shared crawl respects the boards' limits better than today, not worse; the
  catalogue, delays and attribution rules apply unchanged.
- **KubeCraft becomes an operator**: yes, of one small service. The alternative is 1,000 students
  each operating a fragile one, with the support load landing on KubeCraft regardless.
- **Cost surprise**: all components have flat or near-zero pricing at this scale; the only variable
  is email volume, capped by "one digest a day per student".
