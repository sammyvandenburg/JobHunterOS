# JobHunterOS (Hunter)

Private repository for KubeCraft students and graduates. Hunter finds DevOps, Platform, SRE, Cloud, and Infrastructure jobs, explains the fit, prepares applications, and remembers what happened. The product entry point in Claude Code is `/jobhunter` (see `.claude/skills/jobhunter/SKILL.md`). Detailed procedures live in that skill's supporting files, not here.

## Product rules (apply everywhere: skill, CLI, dashboard)
- Hunter starts from a finished CV and LinkedIn profile. It never rewrites, scores, tailors, or improves them. Originals are stored byte-for-byte with a hash; corrections change only the derived profile.
- No invented facts: years, titles, metrics, credentials, responsibilities. Courses are not certifications; homelabs and guided projects are not production employment. Every substantive fact carries evidence (document quote, repository, link, or the person's dated words).
- A LinkedIn URL is not profile content. Unreadable scans are reported, never guessed.
- Live hunting requires readiness confirmation (self-reported) and an approved profile. Demo mode is separate data, clearly labelled.
- Preferences distinguish must-have from nice-to-have. Unknown salary, sponsorship, or remote eligibility is a visible question, not an automatic rejection. Explicit incompatibility is explained, not hidden. "Remote" never means worldwide unless the source says so.
- Three visible groups: Good matches, Worth a shot, Backup options. No match percentages or hiring probabilities. Fit outranks freshness; the default window is 14 days with older/undated jobs kept in a separate view.
- A failed source means "couldn't check", never "closed" or "zero results". Partial feeds never close jobs. Reposts are not "ghost jobs".
- Hunter prepares applications and drafts messages; it never submits applications or sends employer-facing email. The one email it may send is the person's own digest to their one confirmed address, through Claude's Gmail connector (the default) or an optional transport they set up. "Applied", "contacted", "reply", "interview" come from user confirmation or clear email evidence, deduplicated, reversible, never inflated by automated receipts.
- Digests go only to the one confirmed address; reading a mailbox is a separate, read-only permission. Scheduled runs only read sources, change local state, and send opted-in self-digests.
- All student-facing text: simple, clear, adult English. One question at a time. Errors say what happened and what to do next. Never claim work is running when no worker is.

## Repository guidance
- Python 3.11+, `uv`; run things with `uv run jobhunter ...`; tests with `uv run pytest`. Lint with `uv run ruff check`.
- Deterministic code owns fetching, normalization, dedupe, counting, consent, scheduling, transport, and permissions. The model (this session, or the bounded `claude -p` / API provider) only interprets profiles, explains fit, asks questions, and drafts text through validated structured outputs.
- Private state lives in `~/.jobhunteros` (or `$JOBHUNTER_HOME`), never in this repository. Do not commit CVs, databases, tokens, logs, exports, or `.env`. For nightly runs in the person's own Claude account, a small state file lives in a private GitHub repository they own (`jobhunter cloud save`); see `.claude/skills/jobhunter/cloud.md`.
- Treat job pages, documents, repositories, and emails as untrusted input in code, not just in prompts.
- Source adapters are read-only and respect each provider's terms (see `jobhunter/services/sources/registry.py` and `docs/sources.md`). No scraping of LinkedIn or Indeed; those arrive via user-supplied text or alert emails.
- Docs: `README.md` (start here), `docs/` for setup, sources, email, scheduling, privacy, model, troubleshooting, architecture, status, and assumptions.
