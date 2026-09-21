# `lithos-loom develop deliver` — reference

Turn a **stopped** story-develop run's branch into a delivered, monitored PR:
push it, open (or adopt) its PR, raise the `pr` gate, and complete the
needs-human gate the stop raised. Zero agent tokens — nothing is implemented or
reviewed here; the work already exists on the branch.

This is the operator's **third choice** on a stopped run. The other two are the
needs-human gate's own pair: complete the gate → loom re-develops the story
*from scratch* on a fresh branch (discarding the rounds that landed), or cancel
the story → abandon it. `deliver` keeps what the run produced.

## TL;DR

```bash
# See what it would do — no push, no gh call, no Lithos write
lithos-loom develop deliver de459d10 --dry-run

# Deliver it: push, open the PR, swap the needs-human gate for a pr gate
lithos-loom develop deliver de459d10

# By story id instead of run id (its newest run), recording the result
lithos-loom develop deliver ac1380c1 --json /tmp/deliver.json

# The work dir is gone (retain_failed_workdirs = false): name the branch
lithos-loom develop deliver --branch loom/story-ac1380c1-4f2a --story ac1380c1

# A PR only — no gate, no merge tracking (an UNMONITORED hand-off)
lithos-loom develop deliver de459d10 --no-gate
```

## What it does

Five steps, in this order, each idempotent — a second invocation after a
partial first pass finishes the job and changes nothing else.

1. **Push, append-only.** The branch is classified against `origin` — absent
   → create; already equal → nothing; an ancestor of the local branch → a
   fast-forward; **diverged → refused** (exit 1) naming both shas. `deliver`
   never force-pushes: the divergence may be a collaborator's commit, and
   re-developing the story is the operator's other lever. The classification
   happens **under the delivery claim, immediately before the push**, and the
   push sends that exact **commit** (`<sha>:refs/heads/<branch>`), not the
   symbolic ref — a local process that advances or rewrites the branch in
   between can never make the command classify one commit and deliver another,
   or report a sha it did not send.
2. **Open or adopt the PR** — but only *this branch's own* PR. `gh pr list
   --head` matches on the head **branch name** alone, so a PR opened from a
   fork whose branch carries the same name looks identical; adopting one would
   point the `pr` gate, merge tracking, review ingestion and the story's
   eventual completion at a third party's work while retiring the story's
   escalation. A candidate is adopted only when it is **same-repository** and
   its **head is the sha just pushed** (and its base matches `--base` when
   given); anything else is refused (exit 1) naming what was found. Otherwise
   a PR is opened through the same `pr_delivery` seam story-develop uses on
   approval, onto `--base` or the repo's default branch. The body is the
   generated one — what / acceptance criteria / review / `Closes #N` for an
   issue-linked story — plus a **`## Provenance`** section naming the run, its
   stop *classification*, the branch, and the coder's final handoff summary.
   `[story_develop] operator_github_login`, when set, is asked for review
   exactly as on a daemon delivery.

   Every `gh` call is **pinned** to `--repo <owner/name>` resolved from the
   checkout's `origin` — the remote step 1 pushed to. Letting `gh` infer the
   target would, for a fork checkout, resolve to the *parent* repository: the
   PR, the review request and the gate's `repo` metadata would land somewhere
   the branch was never pushed.
3. **Raise the `pr` gate** on the story and record it: `pr_gate_id`, and — on
   the same write — a per-key delete of the stop's failed-attempt marker and
   its `needs_human_gate_id` provenance. This is literally the write the
   daemon's own delivering exit makes (`record_delivery_on_story`), so a
   hand-delivered story is indistinguishable from a daemon-delivered one to
   every later sweep. The story write is made whenever the **live** story does
   not already say it, so a first pass that created the gate but lost the
   metadata write is *repaired* by the next run rather than skipped.

   A gate is adopted only when it watches **this** PR. An open `pr` gate
   pointing at a *different* PR — **even beside one that does match** — means
   the story is already behind another delivery: the command refuses (exit 2),
   leaves every gate alone and leaves the needs-human gate open, rather than
   letting the story's merge semantics stay hostage to a PR that does not
   contain this branch. The gate decision is taken on a **fresh** read (the
   initial one predates the push), and the whole delivery runs under a
   `deliver` claim on the story whose lifetime exceeds the command's entire
   external budget and which is **renewed immediately before the gate work**,
   so two concurrent invocations cannot both decide "no gate yet" and raise
   one each.
4. **Complete the stop's loom `human` gate(s)** — found from the story's
   incoming `waits_on_gate` **edges**, never from the `needs_human_gate_id`
   key (provenance only: it can be stale, and a story may carry several
   gates). This happens **after** step 3 so the story is never momentarily on
   the ready frontier: a story behind a `pr` gate is absent from
   `lithos_task_ready`, so the runner's readiness check defers it and the
   completion cannot trigger a duplicate run. If no `pr` gate could be raised,
   the human gate is deliberately **left open** — it is then the only thing
   standing between the story and a re-dispatch.
5. **Post `[ManualDelivery]`** on the story: the run, the PR (opened or
   adopted), the delivered sha (always — an audit that cannot be checked later
   is no audit), the `pr` gate that now holds it, and the gates retired. Any
   degradation rides along as `[Friction]` text in the same finding. The
   finding is made one-shot by a `metadata.manual_delivery` marker written on
   the **story** *after* the post (finding-then-mark, as the subscriptions
   do): nothing changed *and* the marker is present → nothing posted; a
   delivery whose post failed or died re-posts on the next run instead of
   being computed away as "unchanged". The marker lives on the story, not the
   gate, so `--no-gate` gets the same guarantee and a gate the merge sweep
   completes cannot take the record with it. A crash between the post and the
   marker costs at most one duplicate finding — the same at-least-once trade
   every `post_finding_then_mark` caller in loom makes, and the safe direction
   (the alternative loses the audit entirely).

From there the PR is a first-class PR-maintenance object (PRD
[`pr-reconciliation.md`](../prd/pr-reconciliation.md)): landability
(`[PRConflicted]`), external-review ingestion and `converge --from-github`
remediation, the base-move re-gate, the conflict resolver, merge → story
completed + dependents nudged, and it counts against the project's S6
admission cap.

**The repo, not the worktree.** `state.json` names the branch, and the branch
ref lives in the project's own checkout whether or not the run's worktree
survived a salvage. So the run dir is used only to *find* the branch, the
story, and the run's provenance (rounds from `state.json`, cost + test-gate
verdict from its `result.json` `escalation.brief`, the final coder handoff
from `handoff/`); every write is `git -C <repo>` + `gh` + Lithos. The checkout
is the story's project: `metadata.project` → `[projects.<slug>].repo`.

**What the PR body carries, and how.** It names **why** the run stopped — the
status *and* its reason — but the reason is **redacted and bounded** first: it
is not a curated label (for the reason-bearing statuses it is the first line
of the agent CLI's error text, the subprocess stderr, or the tail of unparsed
agent stdout), and a PR body is world-readable on a public repo. So urls,
absolute / home paths and credential-shaped runs become `<url>` / `<path>` /
`<redacted>`, the text is capped, and the story keeps the untouched original
(its `[NeedsHuman]` finding, the gate brief, and `--dry-run`, which are all
host-side or operator-only).

The coder's final handoff travels as a **fenced block**, never as inline
prose: it is written by the coder agent into a read-write mount, and a PR
description is live markup — GitHub honours closing keywords anywhere in it
and notifies every `@name`. Both are also rewritten so they read the same but
bind nothing. The same reader refuses to follow a symlink out of the handoff
directory and opens only regular files (`O_NOFOLLOW` + an `fstat` check), so a
planted link cannot choose what a host-privileged process reads and a planted
FIFO cannot hang the command.

Provenance the run never recorded (a reaped run's rounds or cost) renders as
`unknown`, never as a confident zero.

## Flags

| Flag | Meaning |
|------|---------|
| `RUN` | The stopped run: a run id, or a task id (its newest run) — the keys `develop list` / `attach` / `dump` take. Omit only with `--branch` **and** `--story`. |
| `--branch NAME` | Deliver this branch instead of the one `state.json` names. With `--story`, it needs no run dir at all — the fallback for a host with `retain_failed_workdirs = false` (the PR body then carries no run provenance). |
| `--story TASK_ID` | The story this branch implements (default: the run dir's task id). Read live from Lithos: title, description, `acceptance_criteria`, `project`, `github_issue_url`. |
| `--base REF` | Base branch for a newly opened PR (default: the repo's default branch, via `gh repo view`). Ignored when a PR is adopted. |
| `--no-gate` | Open the PR only. No `pr` gate is raised and the needs-human gate is left open, so the PR is **UNMONITORED** — nothing tracks its merge, ingests reviews on it, or re-gates it when the base moves. The finding says so. |
| `--dry-run` | Print the five steps with every fact resolved (remote state, the would-be title, the gates that would be completed) and write nothing: no push, no `gh` call, no Lithos write. |
| `--json PATH` | Write the structured record. |
| `--config` | Host config path. |

## Output

- **Text**: the PR url, the push outcome, opened-vs-adopted, the `pr` gate, the
  human gates completed, and one `[Friction]` line per degradation.
- **JSON** (`--json`): `run_id`, `story_id`, `branch`, `pushed`, `pushed_sha`,
  `pr_url`, `pr_number`, `adopted`, `pr_gate_id`, `human_gates_completed[]`,
  `gate_complete`, `complete`, `changed`, `notes[]`. `complete` is what the
  exit code follows: false whenever anything is owed, including a record that
  could not be written (the delivery still stands — `pr_url` says so).

## Exit codes

| Exit | Meaning |
|------|---------|
| `0` | Delivered (or adopted with nothing left to do; or a `--dry-run` plan printed). |
| `1` | **Refused, and this run wrote nothing**: a diverged remote branch, an unknown run, a branch absent from the checkout, a run that already delivered its PR, a story that is not open (without `--no-gate`), a project with no `[projects.<slug>]` mapping, an `origin` that is not a GitHub repository, another `deliver` holding the story's claim, an unreachable Lithos — or a `gh` failure / unadoptable PR **when the branch was already on `origin`**, so nothing of this run's is outside the host. |
| `2` | **Partial — something is committed and something is owed.** The branch was pushed but no PR could be opened or adopted; or the PR is open but the gate half did not complete (no `pr` gate, a gate watching another PR, a needs-human gate that would not close, a lost story write, a `[ManualDelivery]` that would not post); or the `--json` record the operator asked for could not be written. Whatever landed is printed and the `[Friction]` says what is owed; re-running finishes it. The classification follows what has been **committed**, not which step raised — once anything is outside the host, this command never claims it wrote nothing. |

## Requirements

A checkout of the story's project whose `origin` is the PR's repository, `gh`
authenticated, and a reachable Lithos. No Docker, no agent CLIs — `deliver`
spends nothing.

## What it is not

- **Not a resume.** It delivers what the run committed; it does not restart the
  coder/reviewer loop. Re-reviewing the delivered PR (under revised acceptance
  criteria, say) is `lithos-loom develop converge <pr> --story <id> --ac-file …`
  — see [`converge.md`](converge.md).
- **Not a rescue for a run with no commits.** A branch with nothing on it
  delivers an empty PR; check `develop dump <run>` first.
- **Never destructive.** It does not force-push, rewrite a branch, cancel a
  gate, or delete a run dir.
