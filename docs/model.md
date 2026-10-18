# How Claude is used

## In Claude Code (the normal case)
`/jobhunter` runs in your own Claude Code session, so there is no second subscription or API key. Claude reads your extracted documents, writes a better profile draft and job explanations, and drafts application answers. All of it goes through the `jobhunter` CLI, which validates structured outputs:
- `profile submit` checks every fact's evidence (document quotes must exist in the stored text; unsupported roles and certifications are downgraded to "needs clarification"; projects never become professional experience without your word).
- `match submit` rejects explanations that cite unknown facts, cite nothing, or contain numbers or credentials that appear in neither the job nor your facts.
- Draft answers to sensitive screening questions are stripped and handed back to you.

Claude never edits the database or files directly and only sees the minimized text the CLI prints.

## Unattended (worker and dashboard), optional
Off by default; explanations are rule-based and labelled "Rule-based explanation (no AI)". Turn it on with `uv run jobhunter model enable` (records a consent) after checking `uv run jobhunter model status`, which reports:
- **claude-cli**: `claude -p` from Claude Code 2.1.x, logged in with your own account. Hunter runs it with every tool disabled (`--tools ""`), a JSON schema for the output, `--permission-prompts none`, no session persistence, user settings only, no MCP servers, no slash commands, and a dollar budget cap per run (default 0.50, `model set --budget`). Usage counts against your Claude subscription or API billing exactly as any Claude Code use does; Hunter does not assume unlimited access and stops when the cap is hit. Verified in this build with a tiny live call.
- **anthropic-api**: the official `anthropic` SDK (`uv sync --extra api`) with `ANTHROPIC_API_KEY`, default model `claude-opus-5-5`, structured output via `messages.parse`. Billed per token to your API account; prices are on Anthropic's pricing page and are not repeated here.

Bounds: at most 12 explanations per run, 1,500 characters of description per job, the profile summary and short fact list; results are cached by job content, profile version, and matching-policy version, so unchanged jobs are not sent again. Failures keep the fetched jobs and show the rule-based text; nothing is invented.

What leaves your machine when this is on: job titles, employers, locations, salary text, truncated descriptions, your profile summary and fact list. Never your CV file, documents, emails, tokens, or the database. Local storage does not make these calls local; the Connections page says so.

Never done: spawning an unrestricted Claude coding session from inside another, permission-bypass flags, or sending raw vacancies repeatedly.
