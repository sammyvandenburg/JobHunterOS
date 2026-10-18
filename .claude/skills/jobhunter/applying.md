# Help me apply

1. `uv run jobhunter apply prepare <job id> --json`. The package contains: the unchanged approved CV (path, version, hash), project links, draft answers to common questions with fact ids, a checklist of what only the person can answer, and validation problems.
2. You may improve the answers yourself using only the approved facts (ids in `uv run jobhunter profile show --approved --json`). Keep the person's own wording where possible; no generic filler; no invented numbers, credentials, or responsibilities. Never fill in work authorization, visa, salary expectation, availability, or equal-opportunity/demographic answers: list them for the person.
3. Present the checklist first, then the drafts. Remind them: Hunter prepares, they submit. Opening a form or copying a draft is not an application.
4. When they tell you they applied: `uv run jobhunter act applied <id> [--when YYYY-MM-DD]`.

## Contacts and outreach (optional)
- Only use contacts the person names or public professional pages they point you to. Never guess email addresses, imply a referral, or say someone owns a vacancy without evidence.
- `uv run jobhunter drafts outreach <job id> --to "<name>"` or `drafts followup <job id>` creates a local draft for them to edit and send. When they say they sent it: `uv run jobhunter drafts sent <draft id>` (this records "contacted", never "applied").

## Follow-ups
`uv run jobhunter apply followup <job id> --days 7` sets a reminder tied to the real application date. Reminders stop after a reply, rejection, or withdrawal. Due reminders show in `uv run jobhunter attention`.

## Pipeline
`uv run jobhunter act stage <job id> applied|replied|interview|offer|rejected|withdrawn [--when ...]`. Email-derived updates never move a confirmed stage backwards; `act undo <event id>` reverts any event.

## Interview hand-off
When an interview is confirmed: `uv run jobhunter apply handoff <job id> [--include-messages]` writes a Markdown file (path in the output) with the vacancy snapshot, the exact CV version if known, what was actually sent versus drafts, evidence and gaps, and the timeline. It is a plain export, not an integration with any other product.
