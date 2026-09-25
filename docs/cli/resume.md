# `lithos-loom develop resume`

Continue a story-develop run whose **host** died mid-loop, on its own branch.

```
lithos-loom develop resume <run-id|task-id>
    [--repo PATH] [--story TASK_ID] [--no-story-settings] [--description TEXT]
    [--profile NAME] [--max-rounds N] [--max-cost USD] [--image IMAGE]
    [--base BRANCH] [--dry-run] [--config config.toml]
```

## Why it exists

Every round of a develop run ends as a **commit on a local branch** in the run's
worktree. When the host kills the run mid-loop — an OAuth token revoked under it,
a coder container that vanished — those commits survive, and since slice C of the
needs-human arc (task `5dbeb0c8`) the run also records where it stood at the last
round boundary: the round, the branch, the head, the fork point and the spend
(the `checkpoint` block in the run's `state.json`).

The daemon uses that by itself: completing the run's needs-human gate
re-dispatches the story, and a dispatch whose failed-attempt marker says the last
attempt died for a **host** reason continues the branch instead of developing from
scratch. This command is the same entry for a run nobody is going to
re-dispatch — a standalone `python -m lithos_loom.plugins.story_develop` run, a
run whose route no longer matches its task, or one the operator wants to continue
by hand.

Two infra deaths since slice B cost $4.64 (lens #82, round 2) and $26 (the
2026-09-13 resolve eval's sample 4, round 5); the rate is structural (an OAuth
credential's lifetime against a T2-scale run's length) and it selects for the
long, expensive runs.

## What it continues

- a **fresh committable branch at the dead run's head** — its own branch is still
  checked out in its worktree (which you may still want to read), and git allows
  one checkout per branch;
- the **fork point it recorded** as the review range, so the panel reviews the
  branch's own work and not the base's landed commits;
- the **last review round's handoffs** as the cold-start coder's intake — the
  round is the checkpoint's own `reviewed_round` and the files are the configured
  panel's (that dir is an agent-writable mount, so loom never lets it choose the
  round, discovery is only the fallback, reviewer names must be plain tokens, and
  the listing, the files read and the rendered text are all capped) — rendered
  from `resume_coder_init.md`: the work is the coder's own earlier work, so the
  prompt tells it to read the branch and the commit history before changing
  anything and to build on those commits, not restart them. (Cold-start from the
  handoffs rather than the session transcript is deliberate: a revoked token or a
  dead container means the session is gone, while the branch and the handoffs are
  durable — and the `develop converge` entry proves the shape works.)
- the **remainder** of the branch's budgets. `--max-rounds` / `--max-cost` are the
  ceilings for the WHOLE branch: a run that landed 5 of 8 rounds and spent $26 of
  $30 resumes with 3 rounds and $4. The resumed run's own checkpoints record the
  branch totals too, so a second death does not silently reset the budget.

Settings are the story's: the project / task `develop_*` layering the daemon
applies, keyed by `--story` or (by default) the run dir's own task id, with the
host's model policy on top. `--no-story-settings` skips the Lithos read entirely
for a host that cannot reach it. The repo comes from the checkpoint (`--repo`
overrides) and the task text from the run dir's `task.json` snapshot
(`--description` overrides).

## Refusals

Each exits 2 with the sentence saying which, having started nothing:

- an unknown run key;
- a `develop converge` run — its rounds belong on the PR it was converging, so
  that is [`develop converge-push`](converge-push.md)'s business;
- a run with **no checkpointed committed round**: it died before its first round
  finished (so there is nothing to continue) or it predates checkpointing;
- a head the repo no longer has;
- a branch whose rounds or spend already **meet** the ceiling — resuming would
  buy nothing;
- no repo recorded and no `--repo`.

`--dry-run` resolves everything and prints what would be continued (branch, head,
rounds and budget left, the intake round) without starting a container.

## Afterwards

The resumed run's branch is local. `lithos-loom develop resume` writes nothing to
Lithos and opens no PR; [`develop deliver <run>`](deliver.md) is what pushes the
branch, opens (or adopts) its PR and raises the `pr` gate.

## Scope

Only a **host** verdict resumes — `infra` and `resume_exhausted` on the daemon
path, and this command on the operator's say-so. `max_rounds` / `stalled` /
`disputed` / `needs_decision` are verdicts on the *work*; whether "edit the
acceptance criteria, complete the gate" should continue the branch rather than
start over is a separate, bigger question that slice C deliberately does not
answer.

Host-only (`git` + docker + the agent CLI's credentials); not part of the
hermetic `make check`.
