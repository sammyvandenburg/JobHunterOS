# Assumptions and decisions recorded during the build

1. **Empty repository → new Python stack.** FastAPI + Jinja2 server-rendered pages, SQLite, argparse CLI, uv-managed dependencies. No Docker, no hosted database.
2. **State directory** defaults to `~/.jobhunteros`; demo uses a `demo` subfolder; tests use temp dirs. Chosen over XDG paths for predictability across platforms.
3. **Readiness is self-reported.** There is no authorized course-verification integration in the repository, so none was invented.
4. **Regional endpoints.** Lever's documented EU base is supported per board (`region: eu`). Greenhouse documents one base URL; an EU host was unreachable from the build network and is not used by default.
5. **Discovery feeds.** Remotive (remote jobs, attribution and rate-limit terms honoured) and Arbeitnow (Europe/UK) were chosen because they are documented, keyless, and live today. Adzuna is included as an optional keyed provider. HN "Who is hiring" and others were left out to keep the surface small.
6. **LinkedIn and Indeed** come in only through alert emails or pasted text, in line with their terms.
7. **Dedupe is conservative.** Same employer + same normalized title + compatible location + no conflicting requisition id merges; different requisition ids or different countries never merge; identical canonical links merge. Very short descriptions are treated as "same" when the rest agrees.
8. **Matching thresholds** (60/42/28) and component weights are a transparent first policy, versioned (`POLICY_VERSION`). Entry-level candidates are excluded from senior/staff titles with an explanation, shown mid-level titles as a stretch, and never shown internships below their aim unless they have no experience.
9. **Rule-based explanations** are labelled "no AI" and remain when Claude is off or fails. Claude explanations are validated (fact ids, numbers, credentials).
10. **Unattended model path** uses `claude -p` with the person's own Claude Code login (non-bare mode, because bare mode does not use the subscription login), all tools disabled, JSON schema output, and a dollar cap. The direct API path uses the official `anthropic` SDK with `claude-opus-5-5` and structured output; the server-side refusal fallback option was not combined with `messages.parse` because that combination is not documented.
11. **Secrets.** OS keyring first; file fallback encrypted only when a passphrase is provided, and clearly labelled otherwise.
12. **Gmail scopes.** `gmail.readonly` for reading (metadata-only would not allow query search or bodies), `gmail.send` for sending, requested on separate OAuth connections so neither implies the other. Google's Testing-mode 7-day token expiry and restricted-scope verification are documented rather than worked around.
13. **Scheduler.** A plain long-running worker with file locks and heartbeats was implemented and tested first; OS service templates are printed for the person to install. Cloud mode is explicitly not provided.
14. **Digest recipient** is the single confirmed address stored as a consent record; enforced in the transport layer.
15. **Counts** are lifetime and event-based; no conversion rates are shown.
16. **InterviewOS** hand-off is a Markdown export; no curriculum content or integration is claimed.
17. **Licensing.** No licence added to this private repository; third-party packages keep their own.
18. **Not done in this build:** installing a real system service, sending a real email, connecting a real Google account, running Adzuna live, or testing on macOS/Windows hardware. Each is implemented and fixture-tested where possible and documented in docs/status.md.
