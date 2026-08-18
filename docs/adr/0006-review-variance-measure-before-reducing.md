# ADR 0006 — Review-panel variance: measure before reducing

- **Status:** Accepted (RH-5 closed it — see the 2026-08-18 update: **no mechanism is bought**)
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

## Update (2026-08-18, RH-5): the variance decision — buy nothing, and fix the instrument

This ADR deferred the choice of a reduction mechanism until RH-1 (prompt lenses) and RH-2 (the
`thorough` panel) reported, "since either lever may incidentally close it". Both have now reported,
and the deferred decision resolves: **no mechanism is bought, and the case that motivated the
question is retired as a variance probe.**

The whole decision came off **retained report JSON — no reviewer ran, no tokens were spent.** Scoring
is a pure function of `(case.expected, stored report, judge)`, so five paid sweeps already on disk
answered it (the `eval rescore` design, ADR 0005 / #307).

### The numbers this was blocked on

`291-artifact-verdict-file`, judged catch across every stored run of it:

| run | image / panel | judged | findings/sample |
|---|---|---|---|
| `baseline-2026-08-09` | old image, `correctness` (model unrecorded) | 3/5 | 1.2 |
| `post-299-floor-2026-08-11` | old image, `correctness` | 2/5 | 1.6 |
| `rh2-thorough-2026-08-12` (**RH-2**) | old image, 5 personas, mixed engines | 4/5 | 5.8 |
| `baseline-pinned-2026-08-13` (**canonical**) | new image, `correctness`/codex/`gpt-5.6-sol` | 0/5 | 2.0 |
| `rh8-armA-2026-08-13` (**RH-8**) | new image, `correctness`/codex/`gpt-5.6-sol` | 2/5 | 2.2 |
| **pooled** | | **11/25 — 44%, 95% CI 27–63%** | 2.56 |

**1. The spread is sampling noise, in both directions.** An exact multi-way homogeneity test —
conditioning on the 11/25 total, enumerating every allocation into five cells of five and summing the
probability of tables no more likely than the observed — gives **p = 0.170**: the five runs are
consistent with a *single* underlying rate. The two runs with an **identical resolved panel**
(`baseline-pinned` and `rh8-armA` — same day, same image, same model) read **0/5 and 2/5**, Fisher
p = 0.44. Even the widest pair (0/5 vs thorough's 4/5) is only p = 0.048, and that is the most extreme
of ten pairwise comparisons.

So two claims in the record are wrong and are corrected here: this task's premise that 291 "sits at
3/5" was an artifact of one sample, and the pinned re-baseline's reading that its drop to 0/5 meant
it was "no longer just variance" was the same artifact with the opposite sign. It is one rate near
44%, observed five times.

**2. K=5 cannot measure a lever on a case in this band.** Power of a two-sided Fisher A/B at the
pooled 44% baseline:

| true lift | K=5 | K=10 | K=20 | K=30 |
|---|---|---|---|---|
| 44% → 80% (a large lever) | **11%** | 20% | 56% | 76% |
| 44% → 90% (near-total fix) | 18% | 40% | 85% | 97% |
| 44% → 64% (real but modest) | 5% | 5% | 15% | 26% |

At K=5 an arm has an **89% chance of missing a large real improvement**. Every 291 arm ever run was
underpowered by roughly an order of magnitude, so their disagreement was never evidence about
anything. Detecting even a near-total fix at 80% power needs K ≈ 20 per arm — 40 paid runs for one
A/B, before any floor sweep.

This retroactively explains why RH-1 *did* work at K=5: all three of its shipped lenses moved a case
**near-totally** (0/5→5/5, 2/5→5/5, 4/5→5/5). K=5 is a fit instrument for that shape and for nothing
weaker — which was luck, not design, and is now written down as a precondition.

**3. The case measures ranking among real defects, not detection.** `ac.md` is a **conjunction of at
least four independent requirements**, and the head violates all four:

| # | AC requirement | how the head breaks it | declared? |
|---|---|---|---|
| a | the pass's verdict "must never be ignored in favour of an earlier assessment" | `review_file` is not forwarded into the initial `_review_turn`, so the pass parses the round's stale LGTM | **the one `[[expected]]`** |
| b | prompts "must enumerate the gate-collected rendered-page artifacts" | `_NOTE_FILES_PER_CHECK = 12` / `_NOTE_TOTAL_FILE_BUDGET = 36`, the rest collapsed to `+N more` | no |
| c | approval "must be HELD whenever the sealing round's candidate run collected artifacts no reviewer has seen" | unseen-ness is inferred by string-comparing two rendered notes that encode only directory names and counts | no |
| d | "that pass's verdict controls the outcome" | the pass is evaluated through `ReviewOutcome.passed` (a severity threshold) rather than its literal verdict | no |

(b) is verified against the patch and the AC text; it appears in nearly every sample. All four are
real, AC-grounded, `critical`-rated findings — not false positives.

`case.toml` declares **one** expected, and **all 25 stored samples blocked**. So the reviewer does
detect and hold this diff every single time; the case's catch-rate records *which* of four real
defects it ranked into a 1–3 finding budget. That is a lottery over genuine findings, not a
measurement of review quality — and it is the trap #310 named (a metric that rewards reporting
volume) wearing different clothes: the surest way to raise this number is to file more findings.

The observation is consistent with that reading — caught samples averaged 3.36 findings against
missed samples' 1.93 — but it is **not** established: the per-bucket rates are non-monotone (1 finding
→ 38%, 2 → 22%, 3+ → 75%) on 8/9/8 samples, and the gap is carried by the thorough arm. Treat it as
the hypothesis the structure predicts, not as a demonstrated mechanism.

**The mixed-engine hypothesis has no support in this data.** RH-5 was filed expecting the
union-of-panels argument (different personas catch different real issues) to favour a second-engine
reviewer. But thorough's 4/5 sits inside the single-persona band (p = 0.048 at best, before
multiplicity), costs 5× a standard panel, and its structurally-matching findings come from
`correctness`, `test-quality` and `architecture` — **all codex**, the engine `standard` already runs.
Attribution can go no further: this run predates the #313 judge sidecars, so which finding the judge
actually credited is unrecoverable, and the structured matcher is topic-loose on a diff where words
like "artifact" are everywhere. Nothing here isolates engine diversity as the lever.

### Decision

1. **Buy no variance-reduction mechanism.** Options 1–3 in the menu above stay recorded and deferred.
   Nothing in RH-1 / RH-2 / RH-8 justifies K× or 2× reviewer cost. **No config change ships**: the
   persona registry, `standard` / `thorough` profiles and the `develop()` loop are untouched, and no
   default-panel change is made for loom-ecosystem projects.
2. **The 291 class is reclassified.** It is neither a panel blind spot nor a variance defect. It is an
   **under-declared case**: a realistic multi-defect diff with a single declared expected.
3. **291 is retired as an A/B target and as *the* variance probe.** It stays `frontier` — it does
   discriminate — but no arm may be justified by, or measured on, its rate until it is re-declared.
4. **Minimum-detectable-effect precondition** (generalises this ADR's own "measure before reducing"):
   before an arm is paid for, state the case's current rate and the lift the lever must produce, and
   check K supports it. K=5 licenses only near-total movements; a case in the 30–70% band needs
   K ≈ 20–30 per arm, which must be budgeted explicitly or the case is not an A/B target.
5. **Case-authoring rule:** a case's `[[expected]]` set must cover the defects its diff actually
   contains. Where a realistic diff breaks several AC requirements, declare them all (the harness's
   all-expecteds-must-match rule then measures coverage, which is what a conjunctive AC asks for) or
   document the case as a ranking probe. Recorded in `evals/review/README.md`.

### Consequences

- This ADR's first slice ("build difficulty, not just count") is **done, and partly misfired.**
  Difficulty was delivered — but difficulty arrived with *multiplicity*: a realistic diff carries
  several real defects, and a single-expected case over such a diff silently changes what is being
  measured from detection to ranking. That is the lesson the difficulty push actually bought.
- The benchmark's mid-band cases are **not** cheap A/B targets. The affordable arms are on cases that
  are near-0 or near-saturated, where a lever's effect is near-total; that is a real constraint on
  what the harness can be asked, and it now has a number attached.
- **#182's question is answered, negatively and on evidence** rather than left open: with the panel's
  own miss-rate on the motivating case indistinguishable across five configurations, there is no
  measured lift for a reduction mechanism to buy.
- Nothing about the panel got worse: the floor tier remains the regression gate, and the RH-1 lenses
  that shipped did so on their own near-total evidence, not on this case.

### Follow-up work

- **Re-declare `291-artifact-verdict-file`** — add (b)/(c)/(d) as expecteds, or split the diff into
  sibling cases with one expected each. Note the trade: with all four declared, catch requires all
  four and the case reads ~0 for a long time (honest, and genuinely hard); split cases measure each
  defect independently at 4× the run cost for one diff.
- **Teach the harness to state its own detectable effect** — a case's stored rate plus K implies the
  MDE; surfacing it (in the table, or as a pre-paid warning when an arm is requested on a mid-band
  case at low K) would have prevented every underpowered 291 arm.
- The `struct`-vs-judge divergence seen here (the structured matcher firing on three extra personas
  from topic-looseness alone) is the same problem as **#326**; this is another instance for it.
