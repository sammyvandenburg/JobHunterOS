# Your Hunter state (private)

In your own private copy of Hunter this folder holds two small files that `jobhunter cloud save`
writes and commits: `hunter-state.json` (approved profile facts, preferences, your confirmed digest
address, the keys of jobs you dismissed or applied to, the jobs you were emailed, the email history)
and `seen_jobs.json` (the emailed job keys). Nightly runs in your Claude account read and update them.

Never put a CV, notes, drafts, tokens or anything else here. KubeCraft's shared repository keeps this
folder empty; a check in its CI fails if a state file is ever committed there. `jobhunter cloud update`
merges KubeCraft's newer Hunter into your copy and never changes this folder.
