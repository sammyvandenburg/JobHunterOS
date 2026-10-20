# The personal dashboard (a private Claude artifact page)

Every student gets one page at claude.ai that only they can open: their profile, Hunter's recommended
jobs in the three groups, Favorites, Saved, Applied, Not interested, private notes, and application
tracking. It works in the browser and on the phone, wherever Hunter ran (their computer or a nightly
run in their Claude account). Python writes the documents; you move them with your Artifact tools, acting
as the student. You never type job data by hand and never edit the page's HTML for one student.

Commands: `uv run jobhunter board status|page|link <url>|export --out DIR|import --in DIR` (add `--json`).
Tools: `Artifact` (publish the page once), `ArtifactData` (read and write its database; in some sessions
it appears as the Artifact tool's `read_db` / `write_db`).

## Create it once (after the profile is approved and the basics are saved)
1. `uv run jobhunter board status --json`. If `linked` is true, skip to "Sync".
2. `uv run jobhunter board page --json` gives the page file and the capabilities to declare. Publish it:
   `Artifact` publish with `file_path` = that file, `icon` "briefcase", `capabilities` exactly
   `{"db": {"rules": [{"path": "", "read": "owner", "write": "owner"}]}, "user": {}}` (owner-only: nobody
   else can read the student's data, even with the link), description "Your private Hunter dashboard".
3. `uv run jobhunter board link <the url the tool returned>`.
4. Sync (below), then give them the link: "Your dashboard: <url>. Only you can open it. Bookmark it;
   it also works on your phone."
If the Artifact tool is not available in this session, say so and offer the local dashboard instead
(`uv run jobhunter dashboard`, http://127.0.0.1:8765/). Never pretend a page exists.

## Sync (after every hunt, when they ask for the dashboard, and in every nightly run)
A sync is three steps. Do them in this order so what they did on the page counts before you rewrite it.
1. **Read their actions.** `ArtifactData` list, collection `actions`, `query.limit` 1000, `out_dir`
   `<DIR>/in` (follow `next_cursor` until it is gone). Then `uv run jobhunter board import --in <DIR>/in --json`.
   It applies favorites, saves, "not interested", "applied" and notes that are new since last time, once.
   Tell them in one line what came in ("I saw 2 favorites and a note from your dashboard.").
2. **Write what Hunter knows.** `uv run jobhunter board export --out <DIR>/out --json`. The result is a
   manifest: `writes` (files to `set`, in order), `delete_if_present` (leftover shard names). Documents
   that already exist need their current version: `ArtifactData` list collection `meta` (limit 10) and
   collection `board` (limit 40); each line shows `version N`. Then one `ArtifactData` batch with a `set`
   per file (`file_path` = the file, `if_version` = that version; omit `if_version` only for a document
   the lists did not show), plus a `delete` (with its version) for every `delete_if_present` name the
   `board` list did show. If the batch would exceed 1 MiB, split it: shards first, `meta/run` last.
   If a write is refused because a version changed, list again and set again, once.
3. Say what the page shows now: how many recommended, and the link.

Never write under `actions/` (that is the page's), never republish the page with a student's data in it,
never put the CV, notes, drafts or tokens in any document (the export command refuses that on its own).

## What the page does on its own
- Shows profile, recommended jobs grouped, Favorites, Saved, Applied, Not interested, All.
- Favorite, Save, Mark applied, Not interested, and a private note per job: saved immediately, marked
  "Hunter: next run" until the next sync applies them.
- Survives closing and reopening: the data lives in the page's database, not in the browser.
- Works for the owner only. Signed-out visitors see a sign-in message; others see nothing.

## If something is off
- "Hunter hasn't synced yet" on the page: run a sync.
- The page shows old jobs after a Hunter update: run a sync; `board export` rematches with the current rules.
- A new page was published by mistake: `uv run jobhunter board link <old url>` points Hunter back; nothing is lost
  on the old page. `board unlink` forgets the page.
