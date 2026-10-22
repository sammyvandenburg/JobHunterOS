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

On the dashboard you can star a **Favorite**, **Save for later**, press **I applied** after you applied,
mark **Not interested**, and write a private note. Each change says "Saved to your dashboard" at once, and
"Waiting for Hunter" until Hunter's next run picks it up; then "Synced". If a save fails you see
"Couldn't save" and a **Retry** button; nothing you typed is lost. The page is also fine with two tabs open:
it never overwrites a note silently.

**Applying.** Open a job and press **Prepare application**. The request is saved on your page at once; Hunter
picks it up on its next run, or press **Continue in Claude now** to have it done right away (it copies a short
command for Hunter). Then the job page shows **My application**: which CV to send (your default is chosen;
Hunter may suggest another with one reason; you decide, for this job only), a short brief of the company and
the role with its sources, where to apply, your strongest case (what your profile proves and whether your CV
says it), draft answers you rewrite in your own words, an optional introduction or cover letter if you ask for
one, and a short checklist. Paste the form's questions and Hunter adds them; pay, visa, start date and personal
questions are always yours to answer. You apply on the employer's page yourself. Then press **I applied**: Hunter
records the date and asks which CV you sent (the prepared one, another, or not sure), and never guesses.
After that: **Remind me to follow up** (pick the date; Snooze, Done, Stop), **Find a contact** (only someone
the posting names, a public team page, or someone you know; Hunter never guesses addresses or messages anyone),
and **Prepare for interview**, which writes a hand-off file for InterviewOS.

**My CVs** (under your profile): keep several finished CVs, for example "Platform — English" and "DevOps —
Dutch". Give a file to Hunter in Claude and say what to call it. Hunter never rewrites a CV; every upload is
kept exactly as sent, a new version never replaces an old one, and each application remembers the version you
sent. Files stay on the computer where Hunter runs; ask Hunter in Claude to hand one back.

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
