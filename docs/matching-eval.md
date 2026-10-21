# Matching evaluation set and baseline

`tests/fixtures/eval_vacancies.json` holds twenty vacancies, each labelled by hand for four synthetic candidates
(a beginner, a career changer, a mid-level and a senior engineer; the CVs live in `tests/test_matching_matrix.py`
and `tests/test_matching_candidates.py`). The labels say what a careful recruiter would do with the job for that
person: `recommend` (a visible match), `check` (Needs checking only), `out` (left out, with a reason). They were
written by reading the vacancy and the CV, not by running the code; where the code and a label disagreed, the
label was re-read first and changed only when it was wrong (three cases below), otherwise the rules were changed.

`uv run pytest tests/test_matching_eval.py -s` prints, per candidate, how many of the twenty are right and two
numbers that are kept apart on purpose:

- **false positives**: recommended although the label says `out` (the costly mistake: a student applies to a job
  that was never for them);
- **missed**: left out although the label says `recommend` or `check` (the quiet mistake: a fair chance never shown).

A `check` label accepts Needs checking or a recommendation, never `out`.

## Baseline (policy 2026.10-6, 2026-10-21)

First run, with the labels as first written:

| Candidate | Right | False positives | Missed |
|---|---|---|---|
| beginner | 17/20 | 0 | 3 |
| career changer | 17/20 | 0 | 3 |
| mid | 19/20 | 1 | 0 |
| senior | 18/20 | 0 | 2 |

What the mismatches were, and what changed:

- v05 (Security Operations Engineer), v07 (Platform Engineer whose work is React and Java), v08 (Machine Learning
  Platform Engineer on GPU clusters): the first labels said `check` for everyone. On re-reading, a SOC job, a product
  development job and a GPU platform job asking for Terraform and on-call are honestly `out` for a beginner and a
  career changer, and the React job is `out` for everyone. The labels were corrected; the rules were not touched.
- v18 (Staff Platform Engineer, "10+ years") reached Backup options for the mid-level engineer (about four years).
  That was a false positive, so the rule changed: a job two or more levels up that also asks for more than four
  years beyond the CV is left out with the reason ("They ask for about 10 years and a staff level. Your CV shows
  about 4 years. Not a fair shot right now.").

After the corrections:

| Candidate | Right | False positives | Missed |
|---|---|---|---|
| beginner | 20/20 | 0 | 0 |
| career changer | 20/20 | 0 | 0 |
| mid | 20/20 | 0 | 0 |
| senior | 20/20 | 0 | 0 |

Twenty vacancies is a small set; it is a regression guard, not a measure of real-world accuracy. Add a vacancy with
its labels whenever a real posting is matched wrongly, then fix the rule, and keep both numbers at zero.

## What the rules read (policy 2026.10-6)

- **Exact tools, related tools.** Datadog is not Prometheus, Flux is not Argo CD, Proxmox is not VMware, Pulumi is
  not Terraform. A related tool counts for half and is named as such: "They use Pulumi. Your profile shows
  Terraform, not Pulumi." A family word ("monitoring", "GitOps", "CI/CD", "virtualisation") is satisfied by any
  member; a posting that names the member asks for the member.
- **Requirement kinds.** A tool under Requirements or What you'll do is essential; under Nice to have (or with
  "a plus", "ideally", "familiarity with") it is preferred and only ever a soft note; in About us, benefits, or a
  sentence like "we moved off VMware" it is incidental and ignored; "No Azure experience required" negates it.
- **Evidence kinds.** The explanation says where each matching tool comes from: paid work (a role's bullets),
  project work (a homelab, a guided course project), a course or certification, or the skills list only. A
  retracted skill ("I do not use AWS") never comes back as evidence, whatever a CV bullet says.
- **The work decides, not the title.** "Software Engineer, Infrastructure" with infrastructure responsibilities is
  infrastructure work; a full-stack role on a "Platform" team is not; PLC, SCADA and control-systems jobs are
  industrial automation, not IT infrastructure; a leadership title is a stretch only with paid team-leading
  evidence.
- **Languages and contract type** are visible checks ("They ask for fluent Dutch. Your profile does not say which
  languages you speak."; "This is a contract role, not permanent employment."), never silent filters.
- **Work authorization** is shown as the posting states it ("The posting says: no visa sponsorship." or "Not stated
  in the posting.") and is never inferred from a location.
