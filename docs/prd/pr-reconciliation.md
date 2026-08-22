---
title: Lithos Loom — Post-delivery PR reconciliation
milestone: M-PR
status: draft
target_version: 0.9.0
references:
  - docs/SPECIFICATION.md (implemented surface — pr gates, github-watcher, story-develop delivery)
  - docs/prd/orchestration.md (epic H — the `pr` gate this PRD extends)
  - docs/prd/archive/story-develop.md (T9 — the inline Copilot round this PRD retires)
  - docs/adr/0009-converge-pr-loop.md (`develop converge` — the paid loop this PRD feeds)
labels: [needs-triage, lithos-loom, orchestrator, github]
---

# Post-delivery PR reconciliation

> **Status (2026-08-22).** Written from a live failure: the lithos-lens T1
> rollout delivered four PRs in one day, and **every one of them needed manual
> intervention to land**. Three distinct mechanisms were involved, none of them
> the one that looked obvious. This PRD proposes one subscriber that covers all
> three.

## Summary

A delivered PR enters a `pr` gate and loom stops looking at it, except to ask
"has it merged?". Everything else that happens to a PR between delivery and
merge — the base moving underneath it, conflicts appearing, an external
reviewer filing real defects — is invisible to loom and lands on the operator.

The proposal is to widen the existing `pr`-gate sweep from a merge poll into a
**reconciliation sweep**: same cadence, same PR fetch, three more questions.

1. **Is it still landable?** (behind / conflicted / would-break-on-merge)
2. **Did anyone review it?** (Copilot, code-quality bots, humans)
3. **Does it still pass its own gate against *current* main?**

And, per the operator's steer, **stop triggering Copilot inline**. Loom
currently requests Copilot during delivery and blocks on it under a shared
budget. Checking asynchronously is strictly better: no deadline to starve, and
it picks up reviews loom never asked for.

## Evidence — the 2026-08-22 lithos-lens rollout

Four stories delivered, four PRs, four manual interventions. All numbers below
are from the run metadata, the PRs, and a reproduced rebase.

| PR | story | rounds | cost | panel findings | gate | outcome |
|---|---|---|---|---|---|---|
| #44 | T1-S10 | — | — | — | — | merged after manual update |
| #45 | T1-S9 | — | — | — | — | manually re-based onto #44 |
| #43 | T1-S12 | 5 | $66.45 | **0 critical / 0 major / 0 minor** | GREEN | conflicted + 3 real defects fixed by hand |
| #46 | T1-S5 | 5 | $54.07 | **0 / 0 / 0** | GREEN | still conflicted |
| #47 | T1-S3 | 4 | $55.49 | **0 / 0 / 0** | GREEN | open |

### Failure 1 — the inline Copilot round starves itself

T1-S12's `[DevelopResult]`:

```
copilot round: 0 comment(s); no code change; 0 repl(ies) posted — INCOMPLETE
note: Copilot review did not settle: expected 2 comment(s), 0 arrived within 45s
      — the rest may be unaddressed; review the PR or re-trigger Copilot
```

Loom polled, **received Copilot's review, parsed that it claimed 2 comments**,
then had 45 seconds to wait for them to materialise, and gave up. Both were
real:

- `frontier.py:575` — a frontier read failure leaves rows in "Not classified",
  whose own banner explains it as frontier-limit overflow. **An outage renders
  as truncation.** It violates `docs/REQUIREMENTS.md:1267` — the §14 resilience
  contract the slice was implementing.
- `tasks.py:336` — "All systems healthy" renders while open epics and gates are
  invisible, because `classify_open_tasks` skips non-`task` rows.

**Mechanism** (`pr_delivery.py:838-862`): `copilot_timeout` is one budget shared
by the review-summary wait and the comment-materialisation wait. Copilot posted
its summary 553s after the PR opened; 47s of the 600s budget remained; the
settle window got 45s. The comment is explicit that this is deliberate — but the
consequence is an **anti-correlation**: the slower Copilot is, the less time its
findings get to arrive. A late review is exactly the case where the round is
guaranteed to fail.

Cost of the miss: $66.45 and five review rounds produced **zero findings at any
severity**, and the two defects that did exist were found by an external
reviewer nine minutes later and fixed by a human nine hours after that.

### Failure 2 — the reviewed artifact is not the landed artifact

All four PRs share merge-base `61993963` — lens main as of 2026-08-14. Rebasing
them onto current main produces genuine content conflicts:

```
#43 → docs/generated/metrics.{json,md}, frontier.py,
      tests/test_frontier.py, tests/test_tasks_mvp.py
#46 → docs/generated/{metrics.json,metrics.md,domain_model.md,components/Tasks.md},
      tasks.py, web.py, templates/tasks/dashboard.html,
      tests/test_frontier.py, tests/test_tasks_mvp.py
```

19 of #43's 23 files are also touched by the merged pair.

Resolving them is not mechanical. From the operator's own resolution of #43:
`filters_narrow_the_board` had to start counting `projects` as narrowing,
because T1-S9 added a project filter *after* that helper was written — without
it, a `?project=` board feeds the healthy stripe and **turns one project's rows
into a system-wide claim**. Both PRs are individually correct; the composition
is not. No panel ever saw that code, because it did not exist until the conflict
was resolved.

**Note on `docs/generated/`.** Every story regenerates `metrics.json`,
`metrics.md`, `domain_model.md` and `components/*.md`, so **any two concurrent
stories conflict there even when their source changes are disjoint**. That is
the architecture-guardrail kit working against the orchestrator, and it is
independently fixable.

### Failure 3 — a green PR is a statement about an old main

lens `.github/workflows/ci.yml` triggers on `pull_request` with a default
`actions/checkout@v7`, so CI tests `refs/pull/N/merge` — the PR merged into the
base **as it was when the run was triggered**. GitHub re-runs on push, *not*
when the base moves. #43's last green before the operator touched it was
computed against main from eight days earlier.

The operator found this the expensive way, twice:

> a module-budget breach can be invisible to every PR's own CI and appear only
> on the merge. It happened on #45 (`tasks.py` 821) and again here.

The budget check is not special — it is simply the first thing that happened to
be **additive** across slices. Anything that composes rather than conflicts is
equally invisible: two PRs each adding a dependency, each adding a route, each
growing a file toward its ceiling.

### What was *not* the cause

**[#288](https://github.com/agent-lore/lithos-loom/issues/288) — "story-develop
bases worktrees on stale local main" — did not fire here.** It is a real latent
bug and should still be fixed, but at all three dispatch times (09:47, 11:56,
13:08) `61993963` *was* the tip of main; nothing merged until 19:02. A
`git fetch` at run start would have handed all three the identical base.

The actual precondition is [#285](https://github.com/agent-lore/lithos-loom/issues/285)'s
world: stories dispatched from the ready frontier before their siblings land.
That is the steady state, not an anomaly — so "PRs in flight against a moving
base" is a permanent condition to be managed, not a bug to be eliminated.

## Problem statement

The `pr` gate models **"awaiting human merge"**. It does not model
**"awaiting human merge, and by the way this no longer applies, its gate is
stale, and a reviewer left two criticals on it."**

Three consequences, all currently absorbed by the operator:

- A delivered PR rots silently. Discovery is by opening it.
- External review findings are lost when they arrive later than a fixed budget.
- A PR's own green tells you nothing about whether it will break main.

## Design

One new subscriber concern, hung off the sweep that already exists.
`_develop_pr_merge.py:153` fetches the PR object **every sweep** and classifies
`merged` / `closed_unmerged` / `gone` / `still_open`. The `still_open` branch is
currently empty. That is where this lives.

### S1 — PR landability

`PullRequest` (`github_client.py:168`) gains `mergeable: bool | None` and
`mergeable_state: str` from the same `GET /pulls/{n}` response already being
fetched. On `still_open`, classify:

- `clean` — nothing to do.
- `behind` — the base moved, no conflict. Offer S3's re-gate; optionally
  `PUT /pulls/{n}/update-branch` (`allow_update_branch` is already on for lens).
- `dirty` — real conflict. Post `[PRConflicted]` on the story naming the
  conflicting paths.

**Caveat that must be in the implementation, not discovered later:** GitHub
computes `mergeable` lazily and returns `null` on a cold fetch — reproduced
while researching this PRD. A `null` is "ask again", never "clean". De-dup via a
gate-side marker scoped to `(pr_url, base_sha)` so a finding is posted once per
base move, not once per sweep — the same shape as
`metadata.develop_pr_merge_state`.

### S2 — external review ingestion, and no more triggering

**Stop requesting.** Delete the `request_copilot` call from the delivery path
and the blocking `wait_for_copilot` / `fetch_copilot_comments_settled` round
with it. Delivery ends when the PR is open.

**Start checking.** The reconciliation sweep reads the PR's reviews and
review-comments each pass and surfaces anything new as a finding on the story:

- Copilot (`copilot-pull-request-reviewer[bot]`) — the existing
  `fetch_copilot_comments` already filters replies correctly and can move
  wholesale.
- `github-code-quality` and any other reviewing bot — an allowlist in config,
  not a hardcoded login.
- Human reviewers — currently invisible to loom entirely.

De-dup on comment id in a gate-side marker. Post as `[ExternalReview]` (a fresh
prefix; do not overload `[DevelopResult]`, which is a terminal run record).

**Why this is strictly better than the inline round:**

| | inline round (today) | sweep (proposed) |
|---|---|---|
| deadline | 600s shared budget, self-starving | none — polls until merge |
| late review | lost, flagged INCOMPLETE | picked up next sweep |
| reviews loom didn't request | never seen | seen |
| human reviews | never seen | seen |
| blocks delivery | yes | no |
| cost when nothing to do | a delivery-time stall | one API call already being made |

**Open question for the operator.** Copilot reviewed all four lens PRs, but loom
requested each one, so this data cannot tell us whether lens has GitHub's
automatic Copilot code review enabled at the repo level. If it does not, ceasing
to request means no Copilot review at all. **Before S2 ships, confirm the repo
setting and record it as an operator prerequisite** — with a config escape hatch
(`request_copilot = true`) for repos without the automatic rule.

### S3 — re-gate against current main

The piece that closes Failure 3. On a base move, in a throwaway worktree:
`git merge origin/<base>` (or rebase), then run the story's **check-set** on the
merge result.

- Clean + green → record it; the PR's own CI green is now meaningful.
- Clean + red → `[MergeGateFailed]` naming the failing check. This is the module
  budget case, caught before the operator merges instead of after.
- Conflict → S1 already reported it; no gate run.

Zero tokens — it is the existing deterministic check-set on a different tree.

### S4 — prevention

Neither of these is loom code, both reduce how often S1–S3 must fire.

- **`blocks` edges between stories on one surface.** T1-S5/S9/S10/S12 are four
  slices of one dashboard. They had no edges, so Lithos's ready queue offered
  them all. `project import`'s `[sequential]` marker already writes exactly the
  edges that would have serialised them. This is a planning fix, not a tooling
  one — and it is the cheapest intervention available.
- **Take `docs/generated/` out of the per-story diff** in repos using the
  guardrail kit — regenerate on main post-merge, or a union merge driver. It
  would not have saved #43 (which conflicts in real source too), but it is the
  difference between "clean rebase" and "conflict" for every genuinely disjoint
  pair of stories.

## Decisions

1. **The sweep, not the delivery path, owns everything post-PR.** Delivery ends
   at "PR is open". Anything with unbounded external latency belongs to a poll
   loop, not a budget.
2. **Report before acting.** S1 and S3 only post findings. Auto-`update-branch`
   is opt-in; auto-rebase-and-force-push is explicitly **not** in this PRD — it
   rewrites a branch a human may be mid-review on.
3. **Conflict resolution stays human or `converge`.** The evidence says these
   conflicts need judgement (`filters_narrow_the_board`), not a merge driver.
   When automation is wanted it belongs to `develop converge`
   ([ADR 0009](../adr/0009-converge-pr-loop.md)) as a rebase mode, operator-
   triggered — not to this sweep.
4. **The gate is never auto-resolved by this work.** Only a merge completes a
   `pr` gate, exactly as epic H specifies.
5. **A fresh finding prefix per concern** — `[PRConflicted]`,
   `[ExternalReview]`, `[MergeGateFailed]` — per the house rule against
   overloading existing prefixes, since operators grep by prefix.

## Non-goals

- Auto-merge, merge queues, or branch protection. Lens has no protection today;
  a merge queue serialises landing but does not resolve a conflict, and it is a
  repo-policy decision, not an orchestrator feature.
- Rewriting delivered branches without an operator asking.
- Reviewing the merge resolution with a panel. Worth doing, plausibly the next
  PRD, but it is paid work and needs its own scope.
- Fixing #288. Independent, still real, tracked separately.

## Testing

- Sweep classification is a pure function of a fetched `PullRequest` plus the
  stored marker — table-driven, no network, including the `mergeable == null`
  case (must re-ask, must never read as clean).
- Marker scoping: a finding fires once per `(pr_url, base_sha)`, and **re-fires
  when the base moves again**. The failure mode here is a marker that suppresses
  forever, so it needs a negative test.
- External-review de-dup: the same comment id across two sweeps posts once; a
  new comment on an already-reported PR posts.
- S3 runs the real check-set against a real merge result in a temp repo —
  per the [ADR 0005](../adr/0005-review-correctness-eval-harness.md) posture,
  hermetic and no live Lithos.
- Retiring the inline round deletes `pr_delivery`'s Copilot tests; the delivery
  budget (`delivery_budget_seconds`) loses its `copilot_timeout` term and its
  docstring contract test must be updated in the same diff.

## Slices

| # | slice | ships | cost |
|---|---|---|---|
| 1 | S1 landability + `[PRConflicted]` | two fields, one branch, one marker | none |
| 2 | S2 ingestion + retire the inline round | delivery gets faster and simpler | none |
| 3 | S3 re-gate on base move | the merge-blindness fix | none |
| 4 | S4 prevention | graph edges + generated-file policy | none |

Slice 2 is the one with an operator prerequisite (the repo-level Copilot
setting). Slices 1 and 3 are independent of it and can land first.

## Open questions

1. Does lithos-lens have automatic Copilot code review enabled at the repo
   level? Gates slice 2's default.
2. Should `[PRConflicted]` mark the story **needs-attention** in the Obsidian
   projection, or is a finding enough? The gate already blocks the story; the
   question is whether a stale PR should be visually distinct from a healthy one
   awaiting merge.
3. Sweep cadence for re-gating. S1 and S2 are one API call; S3 runs a check-set,
   so it should fire on base-change detection only, not every sweep.
