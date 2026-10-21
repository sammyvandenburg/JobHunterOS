# Job sources

Hunter uses two paths: watching known employers' own job boards, and discovery feeds that bring in employers it did not know. Every adapter is read-only, uses timeouts, bounded retries, a politeness delay per host, response size caps, pagination where the API offers it, and schema checks. One failing source never stops the others, and a partial response is never treated as a complete snapshot (so it never closes jobs).

The provider registry in code is `jobhunter/services/sources/registry.py`; `uv run jobhunter sources registry` prints it. Verification dates below are when the documentation and live behaviour were checked during this build.

| Provider | Access | Docs | Coverage | Limits / terms | Status (2026-10-18) |
|---|---|---|---|---|---|
| Greenhouse Job Board API | Public JSON per board token, no key | docs.greenhouse.io/job-board.html | Any Greenhouse employer | None documented for reads; applying needs the employer's key (not used) | Implemented; live-checked against 55 boards |
| Lever Postings API | Public JSON per site, no key; `api.eu.lever.co` for EU data residency | github.com/lever/postings-api | Any Lever employer | Only POST limits documented; skip/limit pagination | Implemented; live-checked against 12 boards |
| Ashby Public Job Posting API | Public JSON per board, no key | developers.ashbyhq.com/docs/public-job-posting-api | Any Ashby employer | None documented; one request per board | Implemented; live-checked against 29 boards |
| Remotive | Public JSON feed | github.com/remotive-com/remote-jobs-api | Remote jobs (US, worldwide, some Europe) | Credit Remotive and link back; listings delayed 24 h; max 2 requests/min, advised ≤4/day (Hunter reads it at most every 6 h) | Implemented; live-checked |
| Arbeitnow | Public JSON feed, paginated | arbeitnow.com/blog/job-board-api | Europe (Germany first) and a UK feed | "Do not abuse"; link back | Implemented; live-checked |
| Adzuna (optional) | JSON search API, needs free `app_id`/`app_key` | developer.adzuna.com/docs/search | Per-country (gb, us, ca, de, nl, fr, at, ...) | Account rate limits; attribution | Implemented; fixture-tested only (no key here) |
| LinkedIn, Indeed | Imports only: alert emails (Gmail sync or saved file) or pasted vacancy text | linkedin.com/legal/user-agreement, indeed.com/legal | Whatever you import | Their terms forbid automated collection; Hunter does none | Implemented (import path) |

Greenhouse's official documentation lists a single base URL; a regional EU host is not documented and was not reachable from the build environment, so Hunter only uses the documented host (a custom `api_base` per board is possible in the source config but unverified).

## Starter catalogue
`catalog/employers.yaml` lists 90 employers (52 Greenhouse, 10 Lever, 28 Ashby) across North America and Europe, including the Dutch boards of Adyen, Nebius, bol.com, Catawiki, Mollie and Weaviate (each read once on 2026-10-21) with their ATS, board token, careers URL, and the date each board answered a read-only request. "Checked" means the board returned a list of postings that day; it says nothing about current hiring for your kind of role. Hunter syncs the catalogue into its database on first use (`uv run jobhunter sources sync-catalog` re-syncs without touching your changes). Add your own: `uv run jobhunter sources add --kind greenhouse --token acme --name "Acme"` then `sources check greenhouse:acme`.

## Which boards are searched
Boards are skipped when the employer's hiring regions (from the catalogue) do not overlap the person's countries (North America vs Europe); boards with unknown regions and the Remotive feed are always searched; the Arbeitnow (Europe) feed is skipped for people who can only work in North America.

## Discovery
Jobs from Remotive, Arbeitnow, or Adzuna create employer records with origin `discovered`. When such a job links to a public Greenhouse, Lever, or Ashby board, Hunter reads that board once; if it answers, the board becomes a watched source (at most five new boards per hunt). The dashboard's Search health page shows how many employers were discovered this way.

## Query expansion
Hunter does not match on titles alone. Relevance looks at title families (DevOps, Platform, SRE, Cloud, Infrastructure, Systems/Linux, Production, Release/Build, Observability, Network) and at the description (Kubernetes, Terraform, Ansible, CI/CD, GitOps, observability, IaC, cloud providers, on-call...). A job with a broader title but infrastructure work is kept as "uncertain" and shown under advanced views.

## Original listing lookup
For jobs found through a feed or an alert, Hunter tries to find the employer-hosted listing: a link that already points at an ATS counts as verified; otherwise it compares company, title, and location against the employer's board. Title similarity alone is not enough. When nothing confirms it, the job says "I couldn't confirm the original listing" and no URL is invented.

## Freshness and availability
Stored separately: source posting date (with the field it came from), source update date, first seen by Hunter, last seen, last successfully checked. Default window 14 days; older or undated jobs have their own view. A job disappears from a complete employer-board snapshot → "No longer listed on <source> on <date>". A failed check → "Couldn't verify". Closing a vacancy never erases an ongoing application.
