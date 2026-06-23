# ADR 0005 — Review-correctness eval harness: a seeded-defect benchmark

- **Status:** Accepted
- **Date:** 2026-06-23
- **Deciders:** Dave Snowdon

> Tracking issue: **#183**. Builds on review-only mode (**#154** / [ADR 0004](0004-review-only-mode.md))
> as the execution primitive. Makes the #181 reviewer-prompt change measurable and
> is the prerequisite for evaluating the #182 variance-reduction options.

## Context

Confidence in the reviewer panel's correctness is a *vibe*, not a measurement.
#180 showed the panel can run 5 rounds and **approve** a change whose core defect
defeats the task. We hardened the prompts (#181) and filed a variance-reduction
design (#182), but every review-quality change so far has been *argued*, not
*measured* — we cannot tell the panel's miss-rate on real defects, whether #181
moved it, or whether #182's extra reviewer cost would buy a measurable lift. The
honest path to confidence is to make review correctness **measurable**, then
improve against the number.

The ad-hoc A/B that diagnosed #180 (assemble the real reviewer prompt, point a
reviewer at the buggy commit with the issue body as AC, score whether it surfaced
the defect) is the seed of this harness. This ADR productises it.

## Decision

A **seeded-defect benchmark** built on review-only mode (#154). The eval harness
*is* review-only mode + expected-findings scoring.

### Case format

A case is a directory under `evals/review/cases/<id>/` (data, repo-root, not
packaged) so adding one is a small, documented, code-free step:

- `case.toml` — `id`, `description`, `repo`, `base`, `head` (the defect's
  `base..head`), `personas` / `profile`, `acceptance_criteria_file`, one or more
  `[[expected]]` blocks (`file`, `keywords`, `min_severity`, `mechanism`), and an
  optional `[known_good]` (`base` / `head`) clean pair for the false-positive
  measurement.
- `ac.md` — the acceptance criteria the reviewer receives (the issue body).

A case may pair an **independent** defect diff and clean diff (different bases) —
the seed reviews the *removal* of the #180 fix as the defect and the fix itself
as the known-good.

### Matching method (expected → produced)

The cheapest method that does not reward vague findings:

1. **Structured (default, deterministic, hermetic):** a produced finding matches
   an expected defect when it touches the expected **file** AND mentions at least
   one expected **keyword** (over the rationale + files). Severity-correct when
   the matched finding is at or above `min_severity`.
2. **LLM-judge fallback (optional):** on a structural miss, a judge is asked
   whether any produced finding describes the expected **mechanism** prose. The
   matcher layer supports an injected judge; wiring an agent judge into the
   `eval review` CLI is a follow-up. The judge never *lowers* a structured match;
   it only rescues a correctly-worded-but-differently finding.

### Metrics, K, and the pass bar

Run the panel **K times** per case (default 5) and report, over the K runs:

- **catch-rate** — fraction of runs where every expected defect is surfaced;
- **severity-correctness** — among caught runs, the fraction at/above
  `min_severity`;
- **false-positive rate** — fraction of runs on the paired **known-good** head
  that wrongly trip the matcher.

Agents are stochastic, so a case **passes** at a rate bar (catch-rate ≥ 0.8 over
K, configurable) — never a single pass/fail.

### Cadence

**On-demand only — never part of `make check`.** A live run spends real tokens
(K × cases × reviewers) and needs the host sandbox + agent CLIs. The harness
*logic* (case loading, matching, rate aggregation) is unit-tested hermetically
with the review function stubbed; only `lithos-loom eval review` does live runs.

### Case curation & the overfitting risk

- Seed with the **#180 / #171** case (already in hand).
- **Every future escape becomes a regression case** — any defect that slips past
  review and is caught later (by a human, by the codex backstop, in prod) is
  added with its expected finding. The benchmark grows from real misses, not
  synthetic ones.
- **Overfitting:** do **not** tune prompts to the benchmark until it has enough
  *independent* cases. A small benchmark is a smoke test, not a target;
  prompt/severity changes are validated against held-out and newly-curated cases,
  and case independence (real, distinct escapes) is the guard.

## Consequences

- #181's lift becomes measurable: re-run the seed before/after.
- #182's options are validated against the number before paying K× reviewer cost;
  the cheap completeness-pass intervention is tried first.
- The first slice ships the #180/#171 seed + the `correctness` persona; the live
  run reports its catch-rate under the post-#181 prompts (the first real number).

## Deferred

- Wiring an **agent LLM-judge** into the `eval review` CLI (the matcher already
  supports it).
- A few **mutation-style synthetic** defects (off-by-one, swapped ordering,
  dropped error path) for breadth alongside the real-escape cases.
- Per-case **cost reporting** and a cheaper-than-full panel sampling mode.
