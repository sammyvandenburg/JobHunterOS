# JobHunterOS by KubeCraft

JobHunterOS is a private job-hunt companion by KubeCraft for its students and graduates looking for DevOps, Platform Engineering, SRE, Cloud, and Infrastructure work in North America and Europe. Hunter is the assistant inside it: the voice you talk to in Claude Code, the sender of your digest, the name on the dashboard's status line. The naming rules and the KubeCraft mark are in `docs/brand.md`.

The experience: **Learn about me. Find suitable jobs. Show me the best options. Help me act. Remember what happened.**

Hunter starts from your finished CV and LinkedIn profile. It does not rewrite, score, or tailor them. It builds a factual profile you approve, then runs a broad, explainable job hunt, keeps a dashboard and application tracker, and prepares (never submits) applications.

## For KubeCraft students: seven steps

1. **Make your own private copy of Hunter.** On GitHub, open KubeCraft's Hunter repository and press **Use this template** → **Create a new repository**; choose **Private** and a name like `hunter-yourname`. With the GitHub CLI it is one line: `gh repo create hunter-yourname --template <kubecraft>/JobHunterOS --private --clone`. Your copy holds the software and, later, one small state file. Nothing personal ever goes to KubeCraft's repository, and Hunter refuses to save state to a public one.
2. **Open your copy in Claude Code.** On claude.ai/code pick your repository (nothing to install), or clone it on your computer and open the folder in Claude Code.
3. **Type `/jobhunter`.** Hunter asks one thing at a time, in plain words.
4. **Give it your finished CV and your LinkedIn profile as a PDF** (on LinkedIn: More → Save to PDF). Drag both files into the chat. Hunter never rewrites them.
5. **Check your profile and answer the basics:** where you live, which countries you can work in, pay, remote, how often to look.
6. **Open your dashboard.** Hunter runs the first hunt, creates your private dashboard at claude.ai and gives you the link. Only you can open it, and it works on your phone. Favorites, saved jobs, private notes and "I applied" live there and are remembered.
7. **Optional: daily email and nightly hunting.** Say "email me the jobs": Hunter sends today's digest to the one address you confirm, through your own Gmail connector. Say "hunt every night": Hunter sets up a routine in your Claude account that hunts, emails and updates your dashboard while you sleep.

You need a Claude account with Claude Code, and the Gmail connector connected in claude.ai if you want email. Hunter never applies for you and never emails anyone but you.

## Without Claude Code (developers, or just looking)

- A computer you control (Linux, macOS, or Windows), Python 3.11 or newer and [uv](https://docs.astral.sh/uv/getting-started/installation/).
- Internet access to the public job boards (no paid search service needed).

```bash
git clone <your private copy> && cd <it>
uv sync                      # installs locked dependencies into .venv
uv run jobhunter doctor      # checks your environment and explains any fix
```

Then either:

- **In Claude Code:** open this folder and run `/jobhunter`. Hunter asks one question at a time, builds your profile, and finds jobs.
- **In a browser:** `uv run jobhunter dashboard` opens http://127.0.0.1:8765/ (local only). The left menu has Dashboard, New jobs, Favorites, Saved, Applied, Finished, Needs attention, and Profile & settings. Every job has its own page (`/job/<id>`) with a small section menu: Overview, Why it fits, Help me apply (what they need, your proof, what to check, your next step, and draft answers you can edit, save in your own words, and copy), Job description, My notes, History. "Find jobs now" runs in the background and shows its progress; new jobs appear while it runs. Settings are plain controls: where you live, countries, salary, timezone, and work arrangement (remote only / remote preferred / don't mind, never in the office, no hybrid). Jobs whose location cannot be resolved wait under "Needs checking" instead of mixing into your list.
- **Just to look around:** `uv run jobhunter --demo demo seed` then `uv run jobhunter --demo dashboard`. Demo data is synthetic and lives in a separate folder; every page says so.

Your private data lives in `~/.jobhunteros` (change with `JOBHUNTER_HOME`). Nothing personal is ever written into this repository.

## What Hunter does

| You say | Hunter does |
|---|---|
| "Let's get set up" | Four steps: finished? → your CV and LinkedIn PDF → "does this look right?" → three quick basics in one go (where you live and can work, pay, remote or hybrid). |
| "Find jobs" | Reads 85 validated employer job boards (Greenhouse, Lever, Ashby) plus discovery feeds (Remotive, Arbeitnow; Adzuna optional) that introduce new employers, removes duplicates across sources, checks dates, and matches against your approved profile. |
| "Show me the best options" | Good matches, Worth a shot, Backup options, only in the countries you can work in. Each with the real remote rule, pay as stated, freshness, plain reasons tied to your CV, things to check, links, and one next step. No percentages. |
| "Help me apply" | Shows who they are looking for, how well you fit and why, how to present yourself, draft answers from your CV, and what only you can answer. You apply yourself. |
| "I applied" / "they replied" | Tracks the pipeline from your confirmations or from job-search emails you let it read (Gmail, read-only, optional). Automated receipts never count as replies. |
| "Look every day" | Automatic hunting runs inside Hunter on your schedule (or, after your explicit approval, as a small login task when Hunter is closed) and, if you ask, emails a digest to one confirmed address. Dashboard-only is the default. |
| "Interview confirmed" | Exports an InterviewOS hand-off (Markdown) with the vacancy snapshot, the exact CV version, what was actually sent versus drafts, evidence, and gaps. |

## Commands you will actually use

```bash
uv run jobhunter next                 # next setup question or next useful actions
uv run jobhunter hunt run             # find jobs now
uv run jobhunter jobs list            # best new matches (views: new, favorites, saved, applied, finished, old, excluded)
uv run jobhunter jobs show <id>
uv run jobhunter apply prepare <id>   # application package (nothing is submitted)
uv run jobhunter act applied <id>     # you applied; Hunter records it
uv run jobhunter prefs set remote "remote preferred, hybrid ok, no office"   # or: prefs set work_modes remote,hybrid
uv run jobhunter act favorite <id>    # star a job (act save <id> bookmarks it; act note <id> your words here keeps a private note)
uv run jobhunter dashboard            # local dashboard (no Claude needed)
uv run jobhunter board status         # your private dashboard page at claude.ai (Claude publishes and syncs it)
uv run jobhunter email connect gmail-send   # Connect Gmail (the dashboard button does the same)
uv run jobhunter digest test                # a test email to your confirmed address
uv run jobhunter schedule automatic on      # automatic hunting (--keep-running-when-closed adds a login task)
uv run jobhunter schedule status            # what is running it, next run, worker health
uv run jobhunter status               # honest lifetime totals
uv run jobhunter --help               # everything else; add --json to any command
```

## Documentation

- [docs/setup.md](docs/setup.md): environment checks, platforms, first run, updating.
- [docs/sources.md](docs/sources.md): every job source, its access method, terms, limits, and verification date.
- [docs/email.md](docs/email.md): Connect Gmail in a minute, what the permissions really grant, the Google-side setup KubeCraft completes, SMTP as the advanced fallback.
- [docs/scheduling.md](docs/scheduling.md): automatic hunting inside Hunter, the optional login task, host-awake limits, safe disabling.
- [docs/model.md](docs/model.md): how Claude is used in-session and (optionally) unattended; authentication and cost.
- [docs/privacy.md](docs/privacy.md): what leaves your machine, storage, backups, deletion, security model.
- [docs/troubleshooting.md](docs/troubleshooting.md).
- [docs/cloud-routine.md](docs/cloud-routine.md): the default Claude-connected path: digest through Claude's Gmail connector, nightly hunting in the student's own Claude account, state in their private GitHub repository; what was proven live and what remains.
- [docs/release-checklist.md](docs/release-checklist.md): what to verify on a real Mac, Windows and Linux machine and with a live Google account before students get Hunter; what the tests cover versus what was live-verified.
- [docs/architecture.md](docs/architecture.md), [docs/status.md](docs/status.md) (implemented / fixture-tested / live-tested / not available), [docs/assumptions.md](docs/assumptions.md).

## Tests

```bash
uv sync --extra dev
uv run pytest                          # unit, integration, and dashboard tests (synthetic fixtures, no network)
uv sync --extra browser && uv run playwright install chromium && uv run pytest tests/test_browser.py   # browser smoke test (skipped when no browser; set JOBHUNTER_CHROMIUM=/path/to/chrome to use an existing one)
JOBHUNTER_LIVE=1 uv run pytest tests/live    # a handful of read-only live source checks, on purpose only
```

## Not in this version

Automatic application submission, browser form automation, LinkedIn or Indeed scraping, a cloud always-on mode, multi-user hosting, billing, or any InterviewOS curriculum content. Where something is not connected, Hunter says so instead of pretending.

Private repository. No open-source licence is granted; third-party packages keep their own licences (see `uv.lock`).
