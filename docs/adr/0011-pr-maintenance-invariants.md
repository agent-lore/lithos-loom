# ADR 0011 — PR-maintenance invariants: one engine, one writer, state in Lithos, additive-only pushes

- **Status:** Accepted
- **Date:** 2026-08-24
- **Deciders:** Dave Snowdon

> Extracted from [`docs/prd/pr-reconciliation.md`](../prd/pr-reconciliation.md),
> which plans the PR-maintenance state machine. Four of that plan's decisions are
> architectural rather than plan-scoped: they constrain code that will outlive the
> plan, and a PRD gets archived once delivered. Clarifies the push language in
> [ADR 0009](0009-converge-pr-loop.md) §2 and extends the `pr` gate from
> [epic H](../prd/orchestration.md).

## Context

Loom delivers a PR and then stops looking at it, except to poll "has it merged?".
The 2026-08-22 lithos-lens rollout made the cost visible: five delivered PRs, and
every one needed manual intervention to land — stale bases, real conflicts,
external review findings dropped by a self-starving inline round, and a green CI
result that was a statement about an eight-day-old base.

Closing that turns the `pr` gate into something with a lifecycle: it keeps
branches merge-ready, ingests and acts on external reviews, attempts conflict
resolution, and escalates the residue. Four questions arise that are not really
about *this* plan, and answering them inconsistently later would be expensive:

1. **How many fix loops does loom have?** `story-develop` has one, `converge`
   has one, `story-fix` is planned ([orchestration.md](../prd/orchestration.md)
   §A3), and PR maintenance wants one. [ADR 0004](0004-review-only-mode.md) §1
   already single-sources the fix loop; a fourth would quietly undo that.
2. **Where does "what is the state of this PR right now?" live?** Findings are
   append-only history and cannot answer it. Loom logs are invisible to the
   operator and to Lens.
3. **What stops two writers racing that state?** Lithos offers no optimistic
   concurrency on task updates.
4. **What may an automated path do to a delivered branch?** The operator is the
   reviewer of these PRs and may be mid-read.

## Decision

### 1. `converge` is the single pre-merge remediation engine

Every pre-merge fix path — external review findings, conflict resolution,
re-gating after a base move — runs through the convergence loop. Not a new
`pr-maintain` plugin, and not a second loop inside the reconciliation sweep.

Consequences:

- `converge` grows an **external-findings input** that seeds the coder directly.
  This is required, not cosmetic: `converge_pr` runs its own local-panel intake
  and returns `already_clean` **without starting a coder** when that panel finds
  nothing (`converge.py:230`) — and that is the panel that missed the defects an
  external reviewer then found. Triggering converge without injecting the
  findings reliably does nothing.
- `story-fix` is scoped to **post-merge** failures, or reimplemented as a caller
  of this loop. It does not grow its own.
- The autonomous (sweep-driven) and on-demand (operator-driven) entry points
  share the loop and differ only in trigger and budget.

### 2. Reconciliation state is persisted in Lithos, on the `pr` gate

The gate carries the current state; claimed maintenance subtasks carry in-flight
work; findings remain history.

States: `awaiting_review` · `reconciling` · `behind` · `resolving_conflict` ·
`gate_failed` · `needs_human` · `ready_to_merge`.

This follows the three-system split — Lithos is the spine, Lens is the console,
loom is the actor. It also means the operator's view of stuck work is a Lithos
query, not a log grep.

**Lithos requires no schema change for this**, which was verified rather than
assumed:

- `gate_type="human"` is already a valid gate type
  (`GATE_TYPES = {human, timer, ci, pr, external_task}`, `coordination.py:59`).
- Arbitrary metadata keys are accepted; the only rejects are `depends_on` /
  `blocked_on` (`FORBIDDEN_METADATA_KEYS`, `coordination.py:73`), the scheduling
  keys epic G retired.
- `lithos_task_update` applies metadata as an **additive per-key merge**, so
  writing one state key preserves every other.
- `task.updated` fires on any successful update, so Lens gets transitions live
  over SSE.

`needs_human` is therefore a **loom convention** (a metadata value), not a Lithos
concept. Accepted deliberately: it is queryable via `metadata_match`, and
promoting it to a first-class Lithos notion is a change we can make later if a
second tool needs it.

### 3. Loom is the single writer of a gate's reconciliation state

`lithos_task_update` takes no `expected_version` — unlike `lithos_write` for
notes, which does. There is no compare-and-swap, so two writers can lose an
update on the same key even under the per-key merge.

One reconciler owns a gate's state. Other triggers — webhooks, CLI commands, a
second daemon — **enqueue**; they do not write.

Chosen over adding CAS upstream to Lithos because it is the smaller change, has
no cross-repo dependency, and is sufficient: the contention is between loom's own
triggers, not between independent systems. If a genuine second writer ever
appears, `expected_version` on `lithos_task_update` is the upstream fix, and this
decision should be revisited rather than worked around.

### 4. Additive work is automatic; destructive work needs the operator

**The invariant is a property of the ref, not of a git flag: an automated path
may push only a tip that descends from the ref it replaces, and must prove that
before pushing.**

- **Additive → automatic, no prompt.** Merge commits, fix-up commits,
  fast-forward pushes. Nothing is destroyed: the reviewer's line anchors survive,
  the per-round dialogue commits survive, history is only appended to.
- **Destructive → never without explicit approval.** A non-fast-forward push or
  history rewrite, however spelled. Surfaced as a decision for the operator.
- **On a non-fast-forward, stop and report.** Never escalate to a rewrite.

**This clarifies, and does not contradict, [ADR 0009](0009-converge-pr-loop.md)
§2's "never `--force`".** That phrasing named a command; the invariant is about
the ref. `push_to_pr_ref` (`pr_delivery.py:360`) uses `--force-with-lease` and is
**correct**: it first proves ancestry with `git merge-base --is-ancestor`, raising
`MergeRaceDetected` when the reviewed head does not descend from the expected
remote head, then uses the lease as an atomic compare-and-swap pinning origin's
ref, closing the `ls-remote`→push TOCTOU window. A deleted, advanced or rewound
ref is rejected as stale rather than overwritten.

Recorded because it was got wrong once: an earlier draft of the PRD asserted that
`src/` contained no force pushes and proposed a guardrail banning
`--force-with-lease`. That guardrail would have **deleted a race protection in
the name of safety**. Enforce the ancestry proof, never the flag spelling.

## Consequences

- One fix loop to maintain, test and tune. A regression in it is a regression
  everywhere, which is the point — the alternative is a fix that lands in one
  path and silently not the other.
- The `pr` gate becomes a state machine rather than a boolean. Its metadata is
  now load-bearing, and illegal transitions need rejecting rather than applying.
- A Lens slice is needed to render the states. Until it exists the states are
  still queryable, so the work is useful before the console lands.
- Single-writer discipline constrains the planned webhook path
  ([orchestration.md](../prd/orchestration.md) §A7): webhooks wake the reconciler,
  they do not update gates directly. Polling stays the recovery path so a missed
  webhook degrades to "slower", never to "never".
- Rebase is not available as a landing strategy for automation. A merge achieves
  the same landability additively, and it is what the operator chose unprompted
  when resolving lithos-lens#43 (*"kept as a merge so the per-round
  story-develop commits survive"*).
- A guardrail test must assert every push site in `src/` is ancestry-guarded —
  that a `--force`/lease push is preceded by an `--is-ancestor` check on the same
  refs. Tested negatively: a push site added without the guard fails the
  contract. **Not** a grep for a flag name.

## Alternatives considered

**A separate `pr-maintain` plugin.** Cleaner separation between the autonomous
and on-demand paths, and it would let the sweep-driven loop evolve without
destabilising the operator's tool. Rejected because it means two fix loops to
keep in sync, which ADR 0004 §1 explicitly single-sources against; the trigger
and budget differ, but the work does not. Revisit if the two genuinely diverge.

**Reconciliation state in a loom-local store.** Ships faster, no cross-repo
dependency. Rejected because the state would be invisible to Lens and to the
operator except through loom's logs — close to the problem this work exists to
fix.

**Findings as the state model.** Zero new machinery. Rejected because an
append-only stream cannot answer "what is the state now"; it can only be replayed
and interpreted, and every consumer would have to implement the same fold.

**Adding `expected_version` to `lithos_task_update`.** The principled fix for
concurrency, and the right one if a second independent writer ever appears.
Deferred: it is an upstream change with its own review cycle, and single-writer
discipline is sufficient while loom is the only actor.

**Auto-rebase with `--force-with-lease`.** Would produce cleaner history than
merge commits. Rejected under decision 4 — it rewrites a branch the operator may
be mid-review on, and the lease protects against *races*, not against surprising
a human reader.
