# JobHunterOS by KubeCraft: the short guide for students

Hunter is the assistant inside JobHunterOS. It finds DevOps, Platform, SRE, Cloud and Infrastructure jobs for
you, explains why each one fits, keeps your dashboard, and remembers what you did. It never rewrites your CV,
never applies for you, and never emails anyone but you.

## Before you start
- Finish your CV and your LinkedIn profile (JobMagnetOS, if you followed it). Hunter works from the finished
  versions and does not change them.
- Save your LinkedIn profile as a PDF: on your LinkedIn profile, **More → Save to PDF**.
- You need a Claude account with Claude Code. For daily emails you also need the Gmail connector connected in
  claude.ai (Settings → Connectors). Nothing else to install.

## Step 1: your own private copy (once)
1. On GitHub, open KubeCraft's JobHunterOS repository and press **Use this template → Create a new repository**.
   Choose **Private** and a name like `hunter-yourname`.
   (With the GitHub CLI: `gh repo create hunter-yourname --template <kubecraft>/JobHunterOS --private --clone`.)
2. Open the new repository in Claude Code: on claude.ai/code pick it from the list, or clone it on your computer
   and open the folder.

Your copy holds the software and, later, one small state file. Nothing about you ever goes to KubeCraft's
repository; Hunter refuses to save to a public one.

## Step 2: tell Hunter about you (about ten minutes)
Type `/jobhunter`. Hunter asks one thing at a time:

1. "Is your CV finished, and your LinkedIn profile too?" (If not, finish them first; Hunter waits.)
2. "Drop your CV and your LinkedIn PDF here." If a file cannot be read, Hunter says so and you can paste the text
   instead. Hunter never guesses what a scan says.
3. "Here is what I understand about you. Does this look right?" Say yes, or correct it in your own words, for
   example "I do not use AWS, but I do use Kubernetes and Terraform". Hunter repeats the change ("Remove AWS.
   Keep Kubernetes and Terraform. Correct?") and changes nothing until you say yes.
4. "Where should I look? What is the lowest pay you would be happy with? Remote, hybrid, or either?" You can
   skip pay. "Remote preferred, hybrid okay, never fully on-site" is a normal answer.

Hunter never asks about visas, citizenship or sponsorship. If a posting says something about work
authorization, you see it on the job, as the posting states it.

## Step 3: your dashboard
Right after the basics, Hunter runs the first search (a few minutes; it says what it is doing) and creates your
dashboard: a private page at claude.ai that only you can open, on your phone too. It shows:

- **Top picks** and the full list in three groups: Good matches, Worth a shot, Backup options. Each job says
  **Why** it fits ("They ask for Kubernetes and Terraform. Your CV shows both.") and what to **Check**
  ("They use Pulumi. Your profile shows Terraform, not Pulumi.").
- **Needs checking**: jobs whose location or kind of work is unclear. **Older**: good matches older than 14 days.
- Where the job is, the pay as the posting states it, the contract type, and work authorization as stated.

On the dashboard you can star a **Favorite**, **Save for later**, press **Mark applied** after you applied,
mark **Not interested**, and write a private note. Each change says "Saved to your dashboard" at once, and
"Waiting for Hunter" until Hunter's next run picks it up; then "Synced". If a save fails you see
"Couldn't save" and a **Retry** button; nothing you typed is lost. The page is also fine with two tabs open:
it never overwrites a note silently.

**Help me apply** copies a short command; paste it to Hunter in Claude Code and Hunter prepares the
application (facts from your CV, what to check, what only you can answer). You send it yourself. When you have,
tell Hunter or press Mark applied.

## Step 4: optional: daily email and nightly hunting
- Say "email me the jobs". Hunter asks for one address, confirms it, and sends today's digest through your own
  Gmail connector. One email a day at most, never twice the same jobs.
- Say "hunt every night". Hunter sets up a routine in your Claude account that searches early each morning,
  emails you when there is something new, and updates your dashboard. Hunter asks before it creates the
  routine; it uses your Claude plan.

## Every day after that
- Open your dashboard. Star, save, note, mark applied.
- Type `/jobhunter` and say "find jobs now", "show me the best matches", "help me apply", or "what did I
  do this week". Hunter reads your dashboard first, so a job you marked applied or not interested never
  comes back.
- Changed something? "Correct my profile: ..." or "Edit preferences" on the dashboard.

## Updates
When KubeCraft publishes a new version, say "update Hunter". Your state, favorites, notes and applications are
never touched; a backup is taken first and "undo the update" brings it back.

## Privacy, in one paragraph
Your CV, profile, jobs, notes and applications live on the computer where Hunter runs (`~/.jobhunteros`) and
in your dashboard's own private database. Your private copy on GitHub holds one small state file so nightly
runs and your computer agree: the profile facts you approved, your preferences, the jobs you acted on and
their flags, and which jobs were emailed. Never your CV, never the text of your notes, never a token. KubeCraft
sees nothing of it.

## If something goes wrong
Hunter says what happened and what to do next, in one sentence. A job site that could not be checked is
reported as "couldn't check", never as "no jobs". If the dashboard says "Hunter hasn't synced yet", ask Hunter
to sync. If a digest did not arrive, Hunter never resends by itself; it asks you under Needs attention.
