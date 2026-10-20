# Hunting and presenting results

## Run a hunt
If a dashboard is linked, read its actions first ([board.md](board.md)); `hunt run` refuses otherwise.
`uv run jobhunter hunt run --json` reads every enabled employer board and feed; the first matches are
compared as soon as each site answers, so you can show results while it is still running (a slow site is
given up on after the time budget and counted as "couldn't check"). Then say, in this order, in very few words:
1. The best three matches (see below): title, company, where, why it fits, next step. That is the news.
2. One line of totals: "N new jobs, M worth a look." No source statistics unless they ask.
3. Only if something failed: "I couldn't check X and Y this time; the rest worked." If `status` is `failed`,
   say the search failed and why (for example the comparison step failed: the jobs are saved; `uv run jobhunter match run` or search again).
Offer the rest, the older list and "what I left out and why" (`jobs list --view excluded`) only as a question.
Then sync the dashboard ([board.md](board.md)).

Never say "no new jobs" when `status` is `failed`. If `hunt run` says a search is already running, it is
alive (Hunter clears dead ones itself); say so and try again in a minute.

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
