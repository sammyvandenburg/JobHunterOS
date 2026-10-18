# Hunting and presenting results

## Run a hunt
`uv run jobhunter hunt run --json` (can take a minute or two; it reads every enabled employer board and feed). Then say, in this order:
1. What worked and what did not: "I checked N sources; M could not be checked (name them). Your other searches still worked."
2. How many new jobs, how many relevant, how many recommended.
3. The best few matches (see below). Offer the rest, the older/undated list, and "what I left out and why" (`jobs list --view excluded`).

Never say "no new jobs" when `status` is `failed`; say the search failed and why.

## Show matches
`uv run jobhunter jobs list --json` (default view `new`, 10 per page; `--page`, `--group good|worth_a_shot|backup`, `--query`, `--sort newest`). Present each job with:
- title and company; group ("Good match", "Worth a shot", "Backup option");
- location and the real remote rule (for example "Remote, but only in Germany");
- salary as stated, or "Not listed";
- freshness ("Posted 3 days ago" / "Posting date unknown") and verification status;
- two or three reasons that point at approved facts, then gaps or open questions;
- links: employer listing when verified, otherwise "I couldn't confirm the original listing"; where it was found;
- one next step.

Fresh strong matches first; fit outranks freshness. Do not fill groups that are empty.

## Explanations by you
Rule-based explanations are labelled as such. To write better ones: `uv run jobhunter match pending --json` → write `{"explanations": [...]}` following the schema in the output, every reason citing fact ids, no numbers or credentials that are not in the facts, no match percentages → `uv run jobhunter match submit --file <path> --json`. Rejected explanations stay rule-based; say which and why.

## Importing jobs the person brings
- A LinkedIn or Indeed link: Hunter cannot read those pages. Ask them to paste the vacancy text: `uv run jobhunter jobs import --url <link> --text "<pasted>" --company "<name>"`.
- A Greenhouse/Lever/Ashby job link: `uv run jobhunter jobs import --url <link>` does an allowed lookup on the employer's board.
- A job-alert email saved to a file: `uv run jobhunter jobs import-alert --file <path>`.
Say clearly that imports come from the text they gave, not from scraping.

## Actions
`uv run jobhunter act open|favorite|unfavorite|save|unsave|dismiss|undismiss|applied|note|notes|undo ...`. Favorites (star) and Saved (bookmark) are independent lists; `act note <id> your words here` keeps a private note that stays with the job even when it is found again, `act notes <id>` lists them. Dismiss with a reason when they give one (`--reason consultancy|too_senior|too_junior|location|salary|employer|other`). After three dismissals with the same reason, Hunter asks whether to adjust; apply with `uv run jobhunter feedback apply <key> yes|no` only when they answer.

## Sources and discovery
`uv run jobhunter sources list` shows every source and its health; `sources add --kind greenhouse|lever|ashby --token <board> --name <employer>` adds an employer board the person knows; `sources check <id>` tests it. Feeds (Remotive, Arbeitnow) introduce employers outside the starter list automatically; Adzuna is optional and needs free keys. LinkedIn and Indeed only arrive through imports.
