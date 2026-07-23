# ADR 0009 — On-demand PR review-convergence loop (`develop converge`)

- **Status:** Accepted
- **Date:** 2026-07-23
- **Deciders:** Dave Snowdon

> Realises [ADR 0003](0003-code-quality-review-strength.md) §9 "Shape 1" (re-dispatch
> development on the PR branch without resolving the `pr` gate) as the on-demand /
> human-triggered variant. Builds on review-only mode ([ADR 0004](0004-review-only-mode.md))
> and the parameterized develop loop (converge PR 2). Shipped as a 3-PR slice.

## Context

Across the operator's projects a recurring manual chore: take a review (codex,
plus the GitHub bots), paste it to the coding agent, tell it to fix, tell the
reviewer to look again, and iterate until every reviewer is satisfied — pure
courier + poll work, no human judgement. Loom already automates the *pre-PR*
codex/claude convergence inside `story-develop`, and already reviews an existing
PR read-only via `develop review` (#154 / ADR 0004), but nothing loops
*fix → re-review* on an existing PR. That gap is exactly ADR 0003 §9 "Shape 1",
reserved but unbuilt.

The intent asymmetry is the interesting part. Cold **reviewers** are *desirable*
— a reviewer with no coder summary to anchor on judges the change on its merits
(the influx-#239 failure mode was a reviewer anchored on a coder's rosy
summary). But a cold **fixer** is a hazard: picking up a PR it did not author, it
can "fix" a finding by undoing a deliberate decision the author made. The design
must give the fixer the author's intent without giving the reviewers a summary to
rubber-stamp.

## Decision

A host-only, project-agnostic command `lithos-loom develop converge <pr>` and a
reusable `converge_pr(config, change, *, no_push) -> ConvergeResult`. It loops:
intake review at the PR head → if blocking, coder fix loop on the PR branch →
until the panel LGTMs and the gate floor is clean → fast-forward-push the fixed
branch to the PR head, ready for the human merge gate.

Scope locked with the operator (2026-07-20):

1. **Definition of done = local panel only.** codex/claude in-container panel +
   check-floor gate, no GitHub round-trip. GitHub-bot ingestion
   (github-code-quality / Copilot) is a **later slice**.
2. **End state = pushed & green** (fast-forward push to the PR head ref;
   `--no-push` escape). Push only on approval; never `--force`.
3. **Fix autonomy = apply all, loop to consensus.** The coder addresses every
   finding; a genuine coder↔reviewer deadlock falls back to story-develop's
   existing dispute path and stops for the operator.
4. **Intent transfer = reconstruct from the PR (Tier 1).** The cold fixer's
   round-1 prompt reads the PR description + `git log base..head` + the code
   *before* changing anything, and **disputes** (not blindly obeys) a finding
   that conflicts with a deliberate decision. No dependency on how the PR was
   authored — the operator's intent lives in the PR body + commits. Explicit
   handoff (Tier 2 — a note in the PR body) and session continuity (Tier 3 —
   resume the author's session, only possible when loom authored the PR) are
   **deferred to β**.
5. **Any PR on any repo** — no Lithos task required; converge is pure git +
   GitHub host CLI. A **fork PR** is refused pre-loop (loom cannot push under
   origin credentials).

### Parameterize `develop()`, do not fork the loop

The core architectural decision: converge runs an intake review, then calls the
existing `develop()` with a guarded `entry: LoopEntry` override so the round loop
(`run_round`) — coder / panel / gate / dispute / stall / deadlock sequencing — is
reused **verbatim**, honouring ADR 0004 §1's "fixes can't diverge" principle and
the no-duplicate-impl rule. `LoopEntry` supplies (a) a committable worktree
factory positioning a fresh branch **at the PR head** (so the coder's commits
land on it and can be pushed), (b) the diff base = the PR merge-base, and (c) the
intake review that seeds round 1's cold-start coder. Every shared-code change is
guarded to be byte-for-byte the story-develop path when `entry is None`.

Load-bearing fact: `develop()` contains **no** `deliver` / `create_pr` / `push`
calls (delivery lives only in `__main__.py`); its callers are the two `__main__`
entry points. So `develop(entry=…)` reuses the loop with nothing to bypass, and
converge owns its own push epilogue (`push_to_pr_ref`, fast-forward only).

The rejected *compose-a-second-loop* design re-expresses `run_round`'s
termination ordering in a new module (its own prototype flagged the drift risk)
and needs ~4 extractions; parameterize needs **one** (`review_head`, the shared
intake) plus promoting the review CLI's input helpers to a shared public seam.

### Separate intake pass (accepted trade)

converge runs a review-only intake, then the loop re-spins reviewer containers
when a fix is needed — reviewers run twice on a converging PR. This is the better
trade for an on-demand re-check: the **common already-green case builds no coder
container at all** (intake short-circuits to `already_clean`). Folding intake
into a "review-first round 1" is a deferred optimization, not v1.

## Consequences

- The fix loop is single-sourced with story-develop; a prompt / severity /
  lifecycle / termination fix lands in both paths at once.
- The intake blocking rule is single-sourced too: `review_only.intake_blocks`
  backs both review-only's report and converge's already-clean short-circuit.
- **Reporting gotcha:** because converge enters at the PR head with base = the
  merge-base, `develop()`'s own `commits` span the PR's *original* commits plus
  the fixer's. The converge summary counts only the fixer's commits
  (`git.commits_since(head_sha)`); mechanics are unaffected (the push anchors on
  the PR head sha, not the commit list).
- **Intake artifact isolation.** The intake pass and the fix loop share nothing
  on disk: intake runs under a distinct `run_id` (`<run_id>-intake`) so their
  round-1 handoff dirs, gate tree exports (`gate_dir/round_01/tree`) and
  container names — all `run_id`-derived — cannot collide. `export_tree` also
  recreates its destination empty (it overlays via `tar -x` but never deletes),
  a defense-in-depth belt so a re-export into a shared dir can't leave a deleted
  file behind. Without this the intake's PR-head export or a stale reviewer
  handoff could bleed into the fixed-tree gate / panel and approve un-reviewed
  fixes.
- **Whole-command budget.** `--max-cost` bounds intake **plus** loop: the intake
  spend is carried into the loop's ceiling, and an intake that alone exhausts the
  budget stops before a coder is built. `--max-cost`/`--max-rounds` are validated
  before any container work.
- **Incomplete intake is `failed`, exceptions propagate.** An interrupted /
  invalid / absent intake panel yields no trustworthy review, so converge stops
  with `failed` rather than seeding the loop from a partial review. An
  *unexpected* exception while producing the intake is deliberately **not**
  caught — a traceback is the honest signal for an internal fault; `failed` is
  reserved for the expected incomplete/budget cases.
- **`already_clean` is a snapshot verdict.** It reports on the PR revision
  resolved before intake, not a live re-check of the remote head. It is
  non-mutating (no push), so a remote advance during intake is a report-freshness
  question, not a safety one; a live re-check would only introduce its own race.
- **v1 limit:** a round-1 coder that disputes every finding and commits nothing
  is gated on the unchanged head — it converges only if the head was already
  gate-green. Rare and acceptable; documented in the CLI reference.
- **α now, β later, one engine.** `converge_pr` is built so the daemon
  (github-watcher) can call it autonomously later, and bot-comment ingestion
  layers on top — no second implementation.

## Alternatives considered

- **Compose a second loop** (rejected — drift risk + ~4 extractions; see above).
- **Resolve the `pr` gate / re-develop via the daemon** (ADR 0003 §9 Shape 2/3):
  the autonomous variants. This ADR is the on-demand human-triggered entry; the
  autonomous path is deferred to β and reuses `converge_pr`.
- **Push on any terminal state** (rejected): pushing un-green code to a
  contributor's PR branch is worse than leaving the fixes in the local worktree
  for the operator to inspect. Push only on approval.
- **Ingest the GitHub bots in v1** (deferred): the local panel is the fast inner
  loop; bot comments are a slow async secondary channel best layered on later.
