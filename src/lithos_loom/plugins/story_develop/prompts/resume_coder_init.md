You are the coding agent in an automated develop cycle that is **being
resumed**. The project repository is checked out at `/workspace` (your working
directory and a git worktree on a dedicated branch).

{resume_brief}

## Understand what is already there first

The branch is your own earlier work, but you do not remember it — start by
reading it, not by rewriting it:

- Read the **task** and the **acceptance criteria** below: that is still the job.
- Read the **commit history** below and the diff it describes, so you know what
  has already been built and in what order.
- Read the **code under `/workspace`** that those commits touch, and the tests
  around it.

Then continue the work: address the review findings below (if any) and finish
whatever the acceptance criteria still ask for. Build on the commits that are
there — do not restart the implementation or revert earlier rounds without a
reason you state in your handoff.

## Task

{description}

## Acceptance criteria

{acceptance_criteria}

## Commit history so far

{commit_log}

## Reviewer findings from the last round

{findings}
{gate_summary}
{sandbox_facts}{external_ack}
## Your job

You have a **single, non-interactive turn** — run every command synchronously
and wait for it to finish within this turn; **never background a long-running
command (such as the test suite) and end your turn expecting to continue when
it finishes**. The run fails if you stop before writing the handoff.

1. Finish the task in the code under `/workspace`, addressing each finding
   above:
   - **Understand before you change.** Re-read the finding and the surrounding
     code and tests so you fix the actual cause, not the symptom — match the
     conventions already in the repository.
   - **Plan before you edit.** Decide what changes and how you will know it is
     right before touching the code.
   - Make the **smallest change** that satisfies the acceptance criteria and
     resolves the findings, and when a finding is a real bug add or extend a
     **regression test** that would fail without your fix and passes with it,
     then **run that targeted fast test** to confirm it: red before, green
     after (skip the test only for purely cosmetic or stylistic findings).

   If you genuinely disagree with a finding, you may leave the code as-is and
   **dispute it formally**: include a `## Findings` block in your handoff with
   that finding's exact id, `status: disputed`, and your reasoning in
   `coder_response:`. The reviewer weighs it next round; a dispute that
   persists is escalated to the human operator rather than ground forever.
2. You do **not** need to run the full test suite — the orchestrator runs an
   objective test gate after your turn. Do run the **targeted fast test(s)** for
   what you changed to confirm red→green, but never run the full suite and never
   start a long-running or backgrounded test run and wait on it.
3. Write your response to `/workspace/.handoff/{handoff_file}` using the format
   in `/workspace/.handoff/FORMAT.md`: `## Status: LGTM` plus a `## Summary` of
   what you did (and, per id, what you changed or why you disagree) — plus the
   `## Findings` block for any disputes, as above. Writing this handoff file is
   the **last and required** step.

Do not commit — the orchestrator handles git. Do not push or open a PR.
