# ADR 0006 — Review-panel variance: measure before reducing

- **Status:** Proposed
- **Date:** 2026-06-25
- **Deciders:** Dave Snowdon

> Tracking issue: **#182**. Builds on the eval harness (**#183** / [ADR 0005](0005-review-correctness-eval-harness.md))
> and its #182 hardening (per-sample Wilson CIs, PR #204; errored-sample exclusion, PR #205).
> Relates to **#181** (reviewer-prompt method fix), **#103** (codex usage-limit detection),
> **#175** (AC-completeness). Supersedes the single-anecdote premise #182 was filed on.

## Context

#182 proposes mechanisms to **reduce** review-panel variance — N-sample per dimension, dual-engine
per dimension, a candidate-stage completeness pass, profile-gated. Its entire evidence base was **one
anecdote**: the pre-#181 #180 miss (a `develop attach` lifecycle defect a live panel approved), plus a
manual A/B. ADR 0005 named the prerequisite explicitly: *"#182's options are validated against the
number before paying K× reviewer cost"* — i.e. measure the miss-rate before buying a reduction.

So we measured first (the operator's call). We built the instrument the measurement needs — each rate
reported as a count over K with a **Wilson 95% CI** (PR #204), and crashed/incomplete reviewer turns
**excluded** from the denominators rather than scored as clean passes / misses (PR #205) — then ran the
highest-value probe: `eval review --case 180-attach-delivery -k 20` (the documented production miss).

**Result: catch 20/20 (95% CI 84–100%), FP 0/4 (+16 errored).** Single-pass review-only mode does not
miss this defect. (The 16 errored known-good samples were a codex usage limit — orthogonal, → #103.)

## Decision

**Re-scope #182: do not choose a variance-reduction mechanism yet. Make the variance _measurable_
first.** The 20/20 result is not "the panel has no variance" — it is "the eval, as built, cannot
*exhibit* the variance #182 is about." We establish why, then commit the first slice to closing that
gap.

### Why the eval cannot reproduce the #180 gap (and the sandbox is not the reason)

The reviewer runs in the **identical docker sandbox** as a live story-develop run — same
`_build_run_cmd` → `containers.build_run_command` path, same isolation flags, same image default, same
host auth (which is *why* the probe drew on the real codex quota). The acceptance criteria are also
comparable — the eval hands the reviewer the same #171 failure-mode narrative the live reviewer had.
The non-reproduction comes from **how the review is run and what it reviews**, in order of impact:

1. **The synthetic mirror is ~100× easier than the real change.** The `180` case diffs a one-commit
   fixture that removes *only* the `approved→delivering` guard — **1 file, +1/−10**, a single hunk
   whose entire content *is* the defect. The live miss happened reviewing the real #180 feature:
   **+1039/−69 across 5 files (491 lines in `cli/develop.py` alone)**, where the bug was a subtle
   **absence** (a guard that should exist, doesn't) buried among the additions. The mirror was built
   (ADR 0005) to make the catch *unambiguous* — and in doing so engineered out the difficulty that
   caused the miss. Both paths feed the reviewer a `diff_stat` and let it read the real diff from the
   worktree, so the difference is the diff's *content*, not its presentation.
2. **Post-#181 prompts (a confound).** The original miss used the **pre-#181** reviewer prompts; #181
   hardened them specifically to trace lifecycle/method gaps, and its A/B showed *all* arms catch this
   defect afterward. The eval runs the current prompts — measuring a panel already fixed for this class
   against the defect it was fixed for. "20/20" is substantially "the #181 fix works."
3. **No coder handoff, single-pass vs multi-round.** Review-only feeds a fixed placeholder where the
   coder's narrative would be (*"authored outside the develop loop… review on its own merits"*). The
   live reviewer saw the coder's account of what it had just built — which can steer attention toward
   the additions and away from a missing guard — and the real variance played out across a multi-round
   loop. The eval is one clean round-1 pass with none of that framing.

### First slice — build difficulty, not just count

Make the benchmark able to *exhibit* a miss-rate, validated against the now-trustworthy instrument
(CIs + errored exclusion):

- **Realistic-difficulty cases.** Author cases where the seeded defect is embedded in a *larger,
  representative* diff (the bug as a subtle absence among real changes), not isolated in a minimal
  mirror — so the panel has a partial-catch zone to measure. Patch-based authoring (#193) already
  supports an arbitrary `head_patch`; the work is curating *hard* ones.
- **And/or live-loop variance instrumentation.** Measure on the multi-round `develop()` loop with a
  real coder handoff, not single-pass `review_change`, if (1)–(3) above prove that single-pass review
  fundamentally cannot reproduce the live phenomenon.

Only once a case lands in the partial-catch zone (a real, CI-bounded miss-rate) does a reduction
mechanism have a lift to measure.

### Reduction-mechanism menu (recorded, **deferred** until measurable)

For when a measured miss-rate justifies the K× reviewer cost. Leading candidate first:

| Option | Mechanism | Cost | Notes |
|---|---|---|---|
| **3 (lead)** | **Candidate-stage completeness pass** — a fresh "what did the panel miss?" reviewer that runs **once at the approval candidate**, re-tracing each AC + the original failure mode | +1 reviewer turn on `thorough` approvals only | Targets the locked-in round-1 miss directly; reuses the existing `ProfileCheck.stage="candidate"` seam (`profiles.py`), which today applies to *checks* — this extends staging to a **persona** |
| 1 | N-sample per dimension (round-1 only) | K× that dimension | Catches variance directly; costliest |
| 2 | Dual-engine per dimension (codex *and* claude) | 2× that dimension | Diversity catches failure modes redundancy can't; #94 already supports heterogeneous engines |

- **Profile gating:** any mechanism is `thorough`-only; `standard`/`minimal` unchanged; preserves the
  `strength_rank` superset invariant (ADR 0003).
- **Finding merge/dedup:** options 1/2 union reviewer findings across samples/engines — reuse the
  `gate_findings.py` fingerprint model (a `(reviewer, severity, file, line)`-style key). The
  completeness pass (option 3) appends to the existing `FindingLedger` — no new merge.
- **Cost envelope (#102):** thorough already runs $8–35/run; option 3 adds one turn at approval
  (bounded by `max_cost_usd`), options 1/2 multiply a dimension. This is *why* the choice waits for a
  measured lift.

## Consequences

- The benchmark's next growth is toward **difficulty**, not just case count — a deliberate shift from
  "every escape becomes a case" (which yielded easy, 100%-catch mirrors) to "cases hard enough to have
  a miss-rate worth measuring."
- The instrument is now trustworthy: rates carry CIs and exclude agent flakiness (a crashed reviewer
  no longer reads as a review miss), so any future before/after is honest.
- **No panel change ships from this ADR** → no cost increase, no operator-visible surface change yet.
  ADR 0003's profiles/personas and the `develop()` loop are untouched until the first reduction slice.
- #103 (codex usage-limit detection) is the orthogonal reliability fix: until it lands, a codex-limited
  reviewer crashes (now surfaced as `errored`, not silently mis-scored) rather than failing over.

## Alternatives considered

1. **Ship a reduction mechanism now (the issue's literal ask).** Rejected: with no measurable miss-rate,
   any mechanism's lift is unmeasurable, and we'd be paying K× reviewer cost (and optimizing) against a
   benchmark that *cannot show the effect*. ADR 0005's own guard ("validate against the number before
   paying K× cost") forbids it.
2. **Declare the panel variance-free.** Rejected: absence of evidence isn't evidence of absence — the
   live #180 miss was real. The eval simply can't exhibit it yet (reasons 1–3).
3. **Tune prompts further.** Rejected: #181 already hardened the lifecycle-tracing prompts, and with no
   case in the partial-catch zone we can't measure any further lift to justify it.

## Follow-up work

- **First slice (own issue):** author N realistic-difficulty cases (defect embedded in a large diff)
  and/or a live-loop variance harness; report the per-case miss-rate + CI.
- **Then:** re-evaluate this menu against the measured miss-rate; if non-trivial, the candidate-stage
  completeness pass (option 3) is the first reduction slice to scope.
- **#175** (AC-completeness) becomes validatable once a hard case exhibits the AC-completeness miss it
  targets.
- **#103** (codex usage-limit capture/classify) — Part A (`turns.parse_codex_result` retains the
  failure event) is independently actionable so the next codex limit is capturable.
