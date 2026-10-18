# Help me apply

1. `uv run jobhunter apply prepare <job id> --json`. The package has: `fit` (who they want, what you match, what you miss, a fit line, how to present yourself), the unchanged CV (path, version, hash), project links, draft answers with fact ids, a checklist of what only the person can answer, and validation problems.
2. Present it in this order, in short simple sentences:
   - **Who they are looking for** (3 to 5 lines).
   - **How well you fit, and why** (the fit line, then what matches and what is missing, each tied to the CV).
   - **How to present yourself** (3 short tips: what to lead with, how to cover a gap honestly, one example to tell).
   - **Your drafts** (improve them yourself using only approved facts; keep the person's own wording; no filler, no invented numbers or credentials).
   - **Only you can answer** (work country, pay, availability, personal questions). Never fill these in.
3. End with: "Hunter prepares, you apply. When you have sent it, tell me and I will record it." Then `uv run jobhunter act applied <id> [--when YYYY-MM-DD]`.

Encouraging, never pushy. A gap is something to explain, not a reason to give up.

## Contacts and outreach (optional)
Only contacts the person names or public pages they point to. Never guess email addresses or imply a referral. `uv run jobhunter drafts outreach <job id> --to "<name>"` or `drafts followup <job id>` writes a draft for them to send themselves; when they say they sent it: `uv run jobhunter drafts sent <draft id>` (records "contacted", never "applied").

## Follow-ups and pipeline
`uv run jobhunter apply followup <job id> --days 7` sets a reminder (stops after a reply, rejection, or withdrawal). `uv run jobhunter act stage <job id> applied|replied|interview|offer|rejected|withdrawn`. `act undo <event id>` reverts any event.

## Interview hand-off
When an interview is confirmed: `uv run jobhunter apply handoff <job id>` writes a Markdown file with the vacancy, the exact CV version, what was actually sent versus drafts, evidence and gaps, and the timeline.
