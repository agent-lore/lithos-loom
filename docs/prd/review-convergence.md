---
title: Lithos Loom — Review convergence
milestone: M-RC
status: draft
references:
  - docs/SPECIFICATION.md §5.5 (story-develop — the round loop, review profiles and escalation reasons this PRD extends; the contract this document must not contradict)
  - docs/prd/archive/pr-reconciliation.md (S3 merge-gate and S5 composed-tree review — R3 and R4 are its carried follow-ons)
  - docs/prd/prd-to-graph.md (sibling — the slice schema R5's checklist reads)
  - docs/prd/unattended-duration.md (sibling — what the daemon needs before the merge-policy dial means anything)
  - docs/adr/0005-review-correctness-eval-harness.md, docs/adr/0006-review-variance-measure-before-reducing.md (the measurement stance this PRD keeps)
  - docs/adr/0011-pr-maintenance-invariants.md (per-story PRs to `main` under a human `pr` gate — the model M1 measures against)
  - Lithos `plans/2026-09-review.md` (e63b0217) §2 and §4 — the five-run evidence
labels: [lithos-loom, story-develop, review, planning]
---

# Review convergence

> **Status (2026-09-29).** Drafted in the consolidation pass of 2026-09-28/29
> (the interim "accumulator" document, deleted once this PRD and its two
> siblings, [`prd-to-graph.md`](prd-to-graph.md) and
> [`unattended-duration.md`](unattended-duration.md), carried everything it
> held). Customer: **lens T3**, decided 2026-09-29 — R1 lands before T3's
> first slice dispatches. Six stories, every one with a Lithos task; nothing
> dispatched yet. Loom's own stories carry no `trigger:story-develop` tag and
> are dispatched by hand.

## Summary

The PR-maintenance series made every non-delivering run announce itself, and
the restart family removed the largest cause of stops. What the week of
2026-09-21 then showed is a review loop that cannot *finish*: five
story-develop and `converge` runs went to `max_rounds` or a dispute, cost
$495 between them, and in none of them did the coder take the
`needs-decision` exit that would have ended the run with one sentence from
the operator.

The proposal is not a better reviewer. It is a loop that knows what it is
judging against (R1), knows when it has stopped converging (R2), judges only
what it was asked to (R3), can act on a red merge-gate instead of stopping
(R4), and whose approval means the story is *done* rather than that the diff
is *sound* (R5) — and, on top of that, a zero-token record of when loom
would have merged each PR against when the operator did (M1), so that the
autonomy dial in the sibling PRD is turned on a number.

The August review-hardening epic asked what the panel *misses*; the escape
corpus (#401, #402, #409) now measures that and its readings are
engine-confounded with no arm dominating. This PRD is about the loop failing
to *stop*, which is a different disease with a different instrument.

## Evidence — the week of 2026-09-21

From the two stories Dave filed on 2026-09-25 (34bb82c4, a3f17c21) and
their 2026-09-26 addenda:

| Run | Story | Correctness tool | Outcome | Cost |
|---|---|---|---|---|
| d9287814 | f78e6223 `develop deliver` | codex | disputed, 4 rounds | $89 |
| c2e44d61 | #427 remediation | codex | disputed, 4 rounds | $67 |
| 86613f8e | fd71001f `converge-push` | codex | max_rounds, 5 | $112 |
| 8e9ac9c8 | #431 remediation | codex | max_rounds, 5 | $125 |
| a1817376 | 307ac035 resume, #433 remediation | codex | max_rounds, 5 | $102 |

Two shapes recur. The reviewer treats any constructible interleaving as a
defect because nothing states the operational model (single operator,
hand-run commands, Lithos reachable within a claim TTL), and the coder fixes
rather than asking, so each fix grows protocol and the next round's blocking
ids are new. And the loop has no notion of "not converging": it runs to
`max_rounds` and hands the operator a verdict instead of the question. The
a1817376 run adds the twist that even *in-model* lifecycle findings did not
converge, so the fix is not "lifecycle findings are minor" but a model
precise enough to tell in-model from out-of-model, with `needs-decision` as
the exit for either when the criteria do not settle it.

A third piece of evidence is older. The #173 dogfood (2026-06-22) shipped a
gate-command change that did not work, in one round, both reviewers LGTM,
with an explicit acceptance criterion unmet. Its issue (#175, task
77064874) stayed open for three months in the review-hardening epic because
it was filed as a coverage gap. It is not: it is the approval meaning the
wrong thing, and it is what makes M1's record trustworthy or not.

## Problem statement

1. Reviewers judge against an **unstated operational model**, so a product
   question ("is a second operator invocation in scope?") is argued as a
   defect across rounds instead of asked once.
2. The round loop has **no stop condition but the budget**; a churning
   review reaches the operator as a verdict at `max_rounds`, not as the
   disagreement while it is still legible.
3. In `--resolve-conflicts` mode the panel **blocks on pre-existing story
   code** the operator already accepted, because the scope of judgement is
   unstated there too.
4. A red merge-gate is **terminal**: its finding carries the output tail, not
   the reason, and nothing feeds it to the remediation loop an external
   review would get.
5. **Approval does not mean done.** The per-criterion checklist shipped in
   the reviewer prompts (ba38f815), but a change to the gate's own check
   definitions still runs no check, and nothing verifies the checklist was
   honoured.
6. There is **no measurement of loom's merge judgement** against the
   operator's, so the merge-policy dial cannot be turned on evidence.

## Stories

Independently grabbable, each with the evidence that makes it worth doing.
Sequences, fixtures and acceptance for R1 and R2 are on their tasks as Dave
filed them; the others cite theirs.

1. **R1 — Reviewers judge against a stated operational model** (task
   34bb82c4). As the operator, I want a project-level `develop_review_scope`
   block (Markdown, in the project-context doc, host default optional)
   rendered under the acceptance criteria in every reviewer prompt and the
   triage prompt, and the coder prompts to route a finding that needs an actor
   or condition neither the criteria nor the block names to `needs-decision`
   ("is <actor> in scope for this command?") rather than a fix or a bare
   dispute, with a reviewer `contest` conceding unless it cites the line that
   names that actor, so that a product question costs one round and one
   sentence from me instead of $100 of protocol. Acceptance and fixtures as
   filed, including the in-model triple-failure case from a1817376. **Lands
   before T3's first slice**: T3 is write paths behind a trusted-network
   boundary, exactly the shape that churned five reviews.
2. **R2 — A churning review stops with the question** (task a3f17c21). As
   the operator, I want the shared round loop (story-develop, `converge`,
   `--from-github`, `--resolve-conflicts`) to compare each round's blocking
   finding ids with the previous round's from round 3 on, treat a round that
   opened at least one blocking id and carried none over as churned, and end
   the run after two consecutive churned rounds (`develop_review_churn_rounds`,
   default 2, 0 = off) with `escalation.reason = review_not_converging` and a
   brief listing per round which ids opened and closed, so that the
   disagreement reaches me while it is still legible and a round or two of
   spend is saved per stalled run.
3. **R3 — Composed-tree review blocks only on the conflicted hunks** (task
   d48caecd, #374). As the operator, I want the panel in `--resolve-conflicts`
   mode to block on defects inside the merge's conflicted hunks and report
   anything in pre-existing story code as non-blocking, so that a correct
   resolution is not held for a defect the story already shipped and the
   operator already accepted. The same disease as R1 in resolve mode.
   Verified open 2026-09-29: `review_only.review_change` takes no scope;
   findings are not filtered by conflicted paths.
4. **R4 — Merge-gate red has a fix path and keeps its reason** (task
   7bd2696b, #391, from lens #85). As the operator, I want a
   `[MergeGateFailed]` to carry the failing check's reason (not only its
   output tail) and to feed the same remediation loop an external review does,
   bounded by the same budget, so that a base move that breaks a delivered PR
   is fixed by loom when it can be and escalated with a legible cause when it
   cannot. Verified open 2026-09-29: `conflict_resolve_dispatch` returns
   `no_conflict` for a red gate; `merge_gate_outcome` builds the finding from
   name, command and exit code only.
5. **R5 — Approval means the task is done, not that the diff is sound**
   (task 77064874, #175). As the operator, I want the panel's approval to
   require that every acceptance criterion in the story maps to evidence in
   the diff or its tests — the per-criterion checklist now in the reviewer
   prompts, verified rather than assumed, or a coder handoff that maps each
   criterion to the test or change that closes it, which the reviewers
   check — with an unmet criterion blocking approval or surfaced as an
   explicit disposition, so that "approved" can be read by a person or a
   machine as "delivered". The gate-code half of #175 rides along: a change
   to check definitions must execute the affected checks, and a broken
   catalog command must be caught (today `test_story_develop_check_catalog`
   asserts command strings only). The verification approach that
   [`prd-to-graph.md`](prd-to-graph.md) P1 adds to each slice is the input
   this checklist reads. **Precondition for M1**: a shadow record built on an
   approval that means only "locally sound" measures the wrong thing.
6. **M1 — Shadow auto-merge recording** (task 664d84c4). As the operator, I
   want the reconcile sweep to record, on each delivered PR's `pr` gate, the
   first moment loom *would* have merged it — `reconciliation_state =
   ready_to_merge`, a recorded panel approval on the delivered head, no open
   external-review finding — and, when the gate closes, what I actually did
   (merged at that head; merged after further pushes, and whose; closed
   unmerged; merged although loom never reached would-merge) with the delta,
   reported by `lithos-loom gates` or an eval summary, so that the
   merge-policy dial is turned on a measured agreement rate rather than a
   feeling. Zero tokens: every input already exists on the gate. The escape
   corpus already holds recorded external-review verdicts for 21 lens PRs and
   is the retrospective half of the same measurement. Decision 2026-09-29:
   "if it is cheap, get it in early."

**Measurement alongside, not a story:** the engine-control run for the
escape-corpus readings (task 322be40d) — swap engines with prompts held
fixed, same seeds — so that R1's effect on the codex correctness reviewer
can be read apart from the engine itself. The eval harness already records
per-reviewer model explicitly (3b38e86b).

## Decisions

1. **Convergence, not catch rate** (2026-09-28). Every slice of the
   PR-maintenance series is on main, including S8's three instruments; the
   review-hardening epic (61a2bd00) was about what the panel misses and the
   escape corpus now measures that. The new evidence is the loop failing to
   stop. The epic is retained as the container for its children and tagged
   `prd:review-convergence`; its charter ("close the 2026-08 baseline blind
   spots") is superseded by this PRD and the escape-review process.
2. **R5 is promoted out of "coverage gaps"** (2026-09-29). The orchestration
   PRD's `[Drift]` finding (US38) had a live half — nothing checks at approval
   time that the story's criteria were met — and that half is 77064874, not a
   new finding type. The `[Plan]` half is redundant with the run checkpoint and
   the S0 PR body; over-delivery has cost no intervention. A4's decide-next
   brain is not carried: every action it would take is built deterministically
   (escalate, retry), is `converge` (batch-fix), or is the operator's (merge,
   cancel).
3. **M1 comes early** (2026-09-29), with R5 as its precondition; it was
   parked under the merge-policy dial and pulled forward.
4. **The merge-policy dial is designed after M1 has a number.** It is not a
   story here; see *Beyond this PRD*.
5. **Mutation testing is not carried** (2026-09-29, from the LRA
   comparison). It earns a place only if the escape corpus shows an escape a
   test existed for and missed; that check has not been made.

## Re-cut of the open review items

27 of the open loom tasks on 2026-09-28 touched review. Their fate:

| Cluster | Tasks | Fate |
|---|---|---|
| Termination and scope | 34bb82c4, a3f17c21, d48caecd, 7bd2696b | **R1–R4**, the first slices |
| Approval meaning | 77064874 | **R5** |
| Reviewer trust in claims | c7b1adee, 4db7f60b, 32347e77 + 4b6a1565 (sandbox disclosure) | candidate, second wave: a reviewer that accepts an unverified environment claim is the mirror of one that blocks on an unstated actor; both are scope statements the panel lacks |
| Panel coverage gaps | f78669ae spec-conformance persona, 8c1f33e0 visual UI evaluation, b15f937c cross-file context (blocked on #92) | candidate, ordered by what the escape corpus shows the panel actually misses; none is dispatched on a hunch |
| Measurement | 23db4be6 RH-4, cb29b6af RH-9, 322be40d engine control, e46200f9, 4777ca14, and the eval-hygiene cluster (#303 #309 #312 #326 #330 #214) | the escape corpus is the instrument (RH-9's question is partly answered: engine-confounded, no arm dominates); engine control is the one measurement this PRD runs; the rest are harness hygiene, kept as issues |
| Engine robustness | 5061554c (#411), fe400fb5 (#420) | reliability, not review quality — [`unattended-duration.md`](unattended-duration.md) |
| Elsewhere | f8b5ff7c shared reviewer package, 21d4a59e page capture, 54fa7c3e provenance labels | candidate, unchanged |

## Sequencing

| Order | Story | Why here |
|---|---|---|
| 1 | R1 | before T3's first slice dispatches (with lithos-core bd66d57c, T3's own precondition) |
| 2 | R5 | M1's precondition; the checklist half is already in the prompts |
| 3 | M1 | zero tokens; starts recording as soon as T3 delivers PRs |
| 4 | R2 | the next run that churns is the acceptance test |
| as they bite | R3, R4 | each has a known trigger (a conflicted resolution; a base move that breaks a delivered PR) |

Success for the milestone: no T3 run reaches `max_rounds` without either a
`needs-decision` or a `review_not_converging` escalation naming the
question; M1 has an agreement rate over T3's PRs.

## Non-goals

- Raising the panel's catch rate. That is the escape-corpus process and the
  candidate rows above.
- The merge-policy dial itself (below).
- Automated `loom-improve` candidate generation from recurring findings:
  the month-end review and the escape-corpus process are the manual version
  until M1 has data (LRA comparison, 2026-09-29).
- A pre-implementation research phase: no failure has yet been traced to
  missing knowledge.

## Beyond this PRD — the merge-policy dial

Carried from the orchestration PRD's A3 (tasks e87010bf US24, f05e76ac
US25), gated on M1, designed once M1 has a number; recorded here so it is
not lost and not scheduled:

As the operator, I want each project to declare how a delivered PR is
merged — `human` (today's `pr` gate), `shadow` (loom records what it *would*
have merged and why, the gate stays human), `canary` (loom merges in a named
project when the merge-gate is green, the PR's CI check-runs are green
(#141, task 0e544b14 — today loom reads no CI result at all), and the panel
and external review agree), with an `every-n` human checkpoint available
under the last two — so that autonomy is turned up per project on evidence
rather than switched on globally. Its confidence measurement is M1.
[`unattended-duration.md`](unattended-duration.md) is what the daemon needs
before any setting above `human` is safe to leave running.

## Open questions

1. Does the `develop_review_scope` block belong in the project-context doc
   only, or does the slice list from [`prd-to-graph.md`](prd-to-graph.md)
   carry a per-slice scope line too? R1 ships the project-level block; decide
   at the first T3 slice whether a story-level override earns its keep.
2. RH-9's remaining axis (persona × tool × model): does the engine-control
   run answer enough of it to close cb29b6af, or does it stay a candidate?

## Provenance

| Story | From | Carried on |
|---|---|---|
| R1, R2 | Dave's stories of 2026-09-25 (34bb82c4, a3f17c21) | 2026-09-28 |
| R3, R4 | pr-reconciliation.md follow-ons (S5, S3) | 2026-09-28 |
| R5 | #175 (77064874), promoted from the review-hardening epic; orchestration US38's drift half | 2026-09-29 |
| M1 | the perpetual-daemon discussion of 2026-09-09; parked under A3's dial, pulled forward | 2026-09-29 |
| engine control | the 2026-09-26 report's re-cut of review hardening | 2026-09-29 |
