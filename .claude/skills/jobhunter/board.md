# The personal dashboard (a private Claude artifact page)

Every student gets one page at claude.ai that only they can open: their profile, Hunter's recommended
jobs in the three groups, Favorites, Saved, Applied, Not interested, private notes, and application
tracking. It works in the browser and on the phone, wherever Hunter ran (their computer or a nightly
run in their Claude account). Python writes the documents; you move them with your Artifact tools, acting
as the student. You never type job data by hand and never edit the page's HTML for one student.

Commands: `uv run jobhunter board status|page|link <url>|export --out DIR|import --in DIR` (add `--json`).
Tools: `Artifact` (publish the page once), `ArtifactData` (read and write its database; in some sessions
it appears as the Artifact tool's `read_db` / `write_db`).

## Create it once (right after the profile is approved and the basics are saved, before the first hunt)
1. `uv run jobhunter board status --json`. If `linked` is true, skip to "Sync".
2. `uv run jobhunter board build --out <DIR>/hunter-board.html --json` writes the page to publish (the KubeCraft
   mark inlined from `brand/KubeCraft-Cog-3.png`; never publish the raw template path). Publish it:
   `Artifact` publish with `file_path` = that written file, `icon` "briefcase", `capabilities` exactly
   `{"db": {"rules": [{"path": "", "read": "owner", "write": "owner"}]}, "user": {}}` (owner-only: nobody
   else can read the student's data, even with the link), description "Your private Hunter dashboard".
3. `uv run jobhunter board link <the url the tool returned>`.
4. Sync (below), then give them the link: "Your dashboard: <url>. Only you can open it. Bookmark it;
   it also works on your phone."
If the Artifact tool is not available in this session, say so and offer the local dashboard instead
(`uv run jobhunter dashboard`, http://127.0.0.1:8765/). Never pretend a page exists.

## Sync (around every hunt and digest, when they ask for the dashboard, and in every nightly run)
Order matters: **read their actions, then hunt or email, then write the page.** Hunter enforces it: once a
dashboard is linked, `hunt run`, `digest prepare` and `board export` refuse (status `blocked`, or an error
that says so) until the actions have been read since Hunter last wrote to the page. That is what keeps a
job they marked applied or not interested from being recommended or emailed again. If the actions cannot
be read (tool missing, error), say so and stop; never pass `--without-actions` or `--without-dashboard` to
get past it on their behalf.
1. **Read their actions.** `ArtifactData` list, collection `actions`, `query.limit` 1000, `out_dir`
   `<DIR>/in` (a new, empty folder each time; follow `next_cursor` until it is gone and add up the counts).
   Then `uv run jobhunter board import --in <DIR>/in --listed <total> --json`, where `<total>` is the number
   of documents the finished listing reported (`--listed 0` when the collection is empty, which is fine the
   first time). The command refuses when the folder is missing, a file is unreadable, or the count differs:
   that is a failed read, not an empty one, so list again rather than guessing. It applies favorites,
   saves, "not interested", "applied" and notes that are new since last time, once, each together with the
   revision that records it; replaying the same files is harmless.
   If the command prints nothing at all (seen once while a search was running), run it again: applying is safe to repeat.
   Tell them in one line what came in ("I saw 2 favorites and a note from your dashboard.").
   **Then do what the page asked for:** `uv run jobhunter work run --json` runs every request the page recorded
   (Prepare application, a contact, a draft, the hand-off, a CV file); each ends ready, needs_input or failed, and
   the page shows that state after step 2. See [applying.md](applying.md) for what each kind means and when you
   write the company brief yourself.
   Then run the hunt and/or prepare the digest if that is what this sync is for.
2. **Write what Hunter knows.** `uv run jobhunter board export --out <DIR>/out --json`. The result is a
   manifest: `writes` (files to `set`, in order), `delete_if_present` (leftover shard names). Documents
   that already exist need their current version: `ArtifactData` list collection `meta` (limit 10),
   collection `board` (limit 40) and collection `work` (limit 300); each line shows `version N`. Then one `ArtifactData` batch with a `set`
   per file (`file_path` = the file, `if_version` = that version; omit `if_version` only for a document
   the lists did not show), plus a `delete` (with its version) for every `delete_if_present` name the
   `board` list did show. If the batch would exceed 1 MiB, split it: shards first, `meta/run` last.
   If a write is refused because a version changed, list again and set again, once.
   **Confirm it landed:** when the batch result reports every write as done, run
   `uv run jobhunter board published --manifest <DIR>/out/manifest.json --json`. Until then the run contract
   says "written to disk, not published", `plan` keeps the publish step open, and the status says the page was
   not synced. Never run it after a refused or partial batch; fix the batch first.
3. Say what the page shows now: how many recommended, and the link.

Never write under `actions/` (that is the page's), never republish the page with a student's data in it,
never put a CV file or its text, or a token, in any document. The `work/<key>` documents do carry the student's
own application drafts and answers: the page's database is owner-only, and that is where their edits live.

## A direct line from the page to Hunter (optional, with their approval)
Without one, a button on the page saves the request and shows "Continue in Claude now" (it copies
`/jobhunter continue <request id>`). With one, the page reaches Hunter at once through the viewer's own
Claude Code Remote connector: publish the page with the extra capability
`"mcp": {"servers": [{"server": "Claude Code Remote", "tools": ["fire_trigger", "send_message"]}]}` next to
`db` and `user`, and tell Hunter where to knock: `uv run jobhunter board channel routine trig_…` (a poke-only
Routine whose prompt is "/jobhunter continue" followed by the text it receives, in their own copy) or
`board channel session session_…` (a session that stays open). Both are the student's own resources: ask
before creating a Routine or republishing with the new capability, and say that the page will ask them once
to allow the connector. `board channel none` removes it. Never claim the line works until a request made on
the page came back `ready` on the page.

## What the page does on its own
- Four destinations in a dark rail (Today, Jobs, Applications, Profile; Settings opens the profile's
  settings): the job list on the left, the selected job beside it on a desktop, full screen on a phone.
- **Today** opens first: Top picks (three to five, best first, different employers first among equals),
  Needs your attention (only real items from `meta/today`: ready to review, a question only they can answer,
  a follow-up due, a saved job that changed, a failed task) and a line of recent activity.
- **Jobs**: search and one filter (All recommended, Good matches, Worth a shot, Backup options, Needs
  checking, Older, Favorites, Saved, Applied, Not interested, Everything). Each row: the employer's mark (its
  own favicon from a verified domain, else initials), title, employer, a short place and work-mode label,
  posting age, the match badge and two to four role and tool tags. No matching paragraphs in the list.
- **A job**: role icon, title, employer, place, match badge; Favorite, Save and Close together at the top;
  then "Get help with this application" with one main action (**Prepare my application** → **Continue
  application** → **View application**) and **Open application** beside it, visible without scrolling; Job
  overview and "Why this could be a good fit" side by side (reasons from evidence, things to check); the
  description folded; "Your application" (state, CV sent, follow-up, **I applied**); My notes. No tabs.
- **Continue application** opens the focused workspace: 1 Your CV, 2 Your strongest case (with the brief),
  3 Application questions, 4 Optional extras (introduction, cover letter, people), 5 Ready? (checklist,
  destination, Open application). Back to job returns; Escape closes.
- **Applications**: one pipeline (Preparing, Ready, Applied, Interview, Closed) with the CV used, dates,
  the follow-up and the next action. **Profile**: the approved profile, Preferences (writes `actions/_prefs`),
  My CVs (writes `actions/_cvs`), connected services and Hunter settings as they really are.
- Favorite, Save, a private note, I applied, follow-ups, answers, CV choice and every request are saved to
  the page's own database at once and marked as waiting until the next sync applies them. A request shows
  "Continue this request in Claude" only when the page has no direct line to Hunter.
- Its status chips say when Hunter last synced, how many changes wait, and a running search, never more.

## If something is off
- "Hunter hasn't synced yet" on the page: run a sync.
- The page shows old jobs after a Hunter update: run a sync; `board export` rematches with the current rules.
- A new page was published by mistake: `uv run jobhunter board link <old url>` points Hunter back; nothing is lost
  on the old page. `board unlink` forgets the page.
