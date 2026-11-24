---
title: Lithos Loom — Review miss diagnosis
milestone: M-RMD
status: draft
references:
  - docs/SPECIFICATION.md §5.5 (story-develop — the reviewer handoff, the `## Criteria` map and the panel round this PRD extends; the contract this document must not contradict) and §4.15–§4.17 (`develop review`, `eval review`, `eval rescore`)
  - docs/adr/0005-review-correctness-eval-harness.md (the seeded-defect harness D1 extends)
  - docs/adr/0006-review-variance-measure-before-reducing.md (measure before buying a mechanism; the minimum-detectable-effect precondition D2 and D3 are held to)
  - docs/prd/review-convergence.md (sibling — its non-goal "raising the panel's catch rate" and its rule that no coverage candidate is dispatched on a hunch; this PRD is the measurement that would justify one)
  - evals/review/README.md (case tiers, the escape-review process, the in-session-control and power rules)
  - Sashiko, https://github.com/sashiko-dev/sashiko — `designs/DESIGN_PARALLEL_POST_VERIFICATION.md`, `docs/benchmarking.md` (read at commit 4058863, v0.9.5)
  - ReviewBench, https://github.com/review-bench/ReviewBench — `docs/METHODOLOGY.md`, `docs/JUDGING_INPUT.md`, `docs/OFFLINE_EVAL.md`, `AGENT_CONTRACT.md` (read 2026-11-24)
labels: [lithos-loom, story-develop, review, eval, planning]
---

# Review miss diagnosis

> **Status (2026-11-24).** Draft for Dave's review. Three stories, none with
> a Lithos task yet: tasks are filed after this document merges, tagged
> `prd:review-miss-diagnosis`. Nothing here has been built or run. The
> thresholds marked *proposed* are mine and are listed again under *Open
> questions*.

## Summary

When the panel misses a defect, loom cannot say why. A reviewer writes
findings or `LGTM`; whatever it looked at and decided was fine leaves no
trace. So a miss where the reviewer never looked at the defective path and a
miss where it looked, suspected the defect and argued itself out of it are the
same row in the eval table, and they call for opposite remedies: the first is
coverage, the second is verification.

This PRD buys two instruments and holds one mechanism behind them:

- **D1** makes the reviewer write down what it ruled out, and makes
  `eval review` split each miss into *dismissed* or *absent*.
- **D2** scores loom's review on ReviewBench, an external benchmark of 219
  real pull requests, to get the statistical power the 23-case harness lacks
  for mechanism comparisons.
- **D3** audits the dismissals that rest on code the reviewer did not read,
  in a fresh session. It is built only if D1's reading says the dismissed
  share is worth recovering, and it ships only if D2 and the frontier cases
  show it earns its cost.

It is not a proposal to adopt another review tool, and it does not claim the
panel's catch rate will rise. ADR 0006's stance holds: measure first.

## Evidence

### Sashiko: a measured miss class loom cannot currently see

Sashiko is an open-source agentic reviewer for the Linux kernel (Google,
donated to the Linux Foundation; about 1,200 reviews a day on public kernel
lists). Its pipeline is a planner, up to eight parallel single-concern
analysis stages, a verification stage and per-finding post-verification.
Every analysis stage must emit two lists: `concerns[]` and
`dismissed_concerns[]`, the candidates it ruled out, each with the code that
rules it out.

Its design note for the verification redesign reports, on a 99-entry
benchmark of real kernel bugs that had passed human review:

- In **6 of 37 misses** a stage identified the exact ground-truth bug and
  talked itself out of it on an unverified assumption (an unseen caller frees
  the pointer; an inner spinlock stands in for the socket lock). The bug was
  recorded only as a dismissal and nothing audited it.
- A single verifier handling 5–15 candidates in one session silently dropped
  valid secondary findings.

The fix classifies every concern and every dismissal by its evidence. A
dismissal that rests on unread code goes to its own fresh session with one
question and a symmetric rule: neither the bug claim nor the dismissal is
accepted without code that proves it across all call sites. Their before and
after:

| Suite | Exact detection before | After | Findings |
|---|---|---|---|
| 99 entries (97 reviewed) | 55.7% | 62.9% | 333 → 326 |
| 999 entries (986 reviewed) | 46.0% | 47.4% | 4,164 → 3,889 (−6.6%) |

Two things follow. The mechanism is real but modest: a point and a half on
the full suite, with less noise. And the small suite overstated the gain
about fivefold, which is loom's own K=5 lesson from RH-5 arriving from an
independent project. The 75.8% detection rate Sashiko publishes is a later
model and version and says nothing about this mechanism.

### Loom today (verified 2026-11-24)

- `prompts/FORMAT.md` defines `## Status`, `## Summary`, `## Findings` and
  `## Criteria`. Nothing records a concern considered and rejected. The
  reviewer prompts say an issue mentioned only in summary prose is invisible
  to the pipeline; a dismissal is not mentioned anywhere.
- The structured review report (`review_report.py`, the contract `eval
  review` consumes) carries findings per reviewer and nothing else a miss
  could be diagnosed from.
- `eval review` reports catch, severity, false positive and noise per case.
  It has 23 cases: 6 floor, 17 frontier. A miss has no cause column.
- The prior on the cause is single-pass variance plus anchoring on the
  coder's summary (the influx #239 dogfood), and RH-2 and RH-8 found that
  neither a stronger profile nor a different model is the operational fix. A
  reviewer that saw the defect and rationalised it away is one concrete form
  that variance could take. Loom has no measurement of it either way.

### The harness cannot power a mechanism comparison

RH-5 established that K=5 has about 11% power at a mid-band catch rate, so
only near-total moves on near-zero or near-saturated cases are readable.
Sashiko's own gain on its full suite was 1.4 points. A lever of that size is
invisible on 17 frontier cases at any K loom would pay for.

### ReviewBench

ReviewBench (GitHub, MIT) is 219 public pull requests from 187 repositories
with 2,565 golden findings; a 25-PR test set carries 342. TypeScript is 31%
of the full set and Python 19%. It runs offline with no registration, and it
has a judge-only path: findings produced by any runner, written as
`{file, start_line, end_line, message}` per PR, are scored by `npm run
judge`. Loom findings already carry `files: ["path:line"]`.

Published leaderboard readings (high-severity slice, 2026-10) give reference
points on the engines loom already drives: Codex GPT-5.6 Sol at 70.2 F1
(96.3 precision, 55.3 recall), a single claude-opus-5-5 agent at 69.3, the
top entry at 83.0. Precision is 90–97% for almost every entry; recall is what
separates them.

What it does not measure, stated up front because it bounds every reading:

- **No acceptance criteria.** The brief is the PR title and body. Loom's
  criteria map and its tracing of the original failure mode are not
  exercised.
- **The golden set is about 98% machine-produced**: 54% from LLM review
  agents, 44% from Copilot code review, 1.5% from human review comments.
  Grounded recall means "found what other LLM reviewers found".
- **The judge and matcher are LLMs** (Claude Sonnet 5 on the leaderboard).
- **Answers are public.** The upstream PR discussions and fix commits are on
  GitHub; ReviewBench's own testing found a network-restricted Codex reviewer
  still read upstream review comments through its GitHub connector on about
  half the PRs.

## Problem statement

1. A miss has **no recorded cause**, so every coverage candidate is argued
   from anecdote.
2. A reviewer's **dismissal is never checked**. A finding at least meets the
   coder, who may dispute it; a dismissal meets nobody.
3. The eval harness has **no power for small effects**, and an external
   instrument that has it is not wired up.

## What you can and cannot do when this lands

With D1:

- Read why a frontier case was missed: `uv run lithos-loom eval review
  --case lens34-truncation --report-dir DIR` prints, beside the catch count,
  how many misses were *dismissed* and how many *absent*.
- Re-derive that split offline from a retained report directory with
  `eval rescore`, at judge cost only.
- See a reviewer's ruled-out concerns in the `develop review` report and in a
  run's review handoff.

With D2:

- Produce a ReviewBench findings directory for any panel:
  `evals/reviewbench/run --set test --reviewer code-quality --out RUN_DIR`
  (script name proposed), then score it with ReviewBench's own judge.
- Compare two panels on 342 golden findings instead of 17 cases.

With D3, only if built:

- Turn the audit on per project (`develop_review_dismissal_audit`, name
  proposed) and see each audited dismissal's outcome, `upheld` with the
  quoted code or `promoted` to a finding.

Not in scope, and not possible after this PRD:

- Auditing *findings* before they cost a coder round (see *Measurement
  alongside*).
- A ReviewBench leaderboard entry. That needs a registered container image
  and three full runs.
- Running ReviewBench's 219-PR full set as a routine. D2 is the test set;
  the full set is a decision taken on D2's reading.
- Per-path reviewer notes in the manner of Sashiko's maintainer notes. A
  candidate, not carried.

## Stories

1. **D1 — Reviewers record what they ruled out, and a miss gets a cause.**
   As the operator, I want every reviewer handoff to carry a `## Dismissed`
   section listing each concern the reviewer considered and rejected, with
   the candidate defect, the code that rules it out (`path:line`), whether
   the reviewer read that code or assumed it (`basis: read | assumed`), and
   the severity it would have had; and I want `eval review` to ask its judge,
   for every sample that did not catch the seeded defect, whether a dismissed
   entry describes it, and to report misses as *dismissed* or *absent* per
   case and pooled over the frontier, so that the next coverage candidate is
   chosen on a count.
   - Observability only. An empty section is valid ("none"); a missing or
     malformed one is recorded and never fails or re-prompts a run. This is
     deliberately weaker than the `## Criteria` contract.
   - The section reaches the structured review report, the `develop review`
     markdown, and the retained eval reports, so `eval rescore` can derive
     the split without re-running agents.
   - Behind a switch that defaults off outside the eval, because a prompt
     change is a lever: the six floor cases must stay `ok` with it on before
     any project enables it.
   - **The reading**, fixed before the run: one arm over the 17 frontier
     cases at K=5 with the section on. Report the pooled count of misses,
     the dismissed share with its Wilson interval, and the cases the
     dismissed misses fall in. The frontier headline of this run is not
     compared with stored baselines; they were not run in its session.
   - Paired updates: `prompts/FORMAT.md`, the reviewer prompts, SPEC
     §5.5, `docs/cli/review.md`, `evals/review/README.md`, and
     `docs/result-schema.json` only if the section reaches `result.json`.

2. **D2 — A ReviewBench baseline for loom's review.** As the operator, I
   want an on-demand adapter under `evals/reviewbench/` that, for each PR in
   ReviewBench's 25-PR test set, checks out the recorded base and head from
   the review-bench mirror, runs `lithos-loom develop review base..head` with
   the PR title and body as the acceptance text and every gate check off
   (`--check-state NAME=off`; the repositories are third-party and in over a
   dozen languages), and writes loom's findings in ReviewBench's judging input
   format; and I want a first reading of two arms run in one session, the
   cold generalist (`--reviewer code-quality`) and the `standard` panel, so
   that there is a powered instrument for generic bug-finding and a number
   for what the personas add over a bare engine.
   - Host-only, not part of `make check` or CI, like the other evals. The
     mapper (loom finding → ReviewBench finding) is unit-tested hermetically.
     ReviewBench is cloned beside loom, never vendored; the reading records
     its commit, the judge provider and model, and loom's resolved panel.
   - `out-of-scope` findings are written to a separate directory and left
     out of the primary score, matching the eval harness's rule that a
     deferral is an escape.
   - **Answer leakage is controlled, not assumed.** Web search and hosted
     connectors are off for the reviewers; each run's transcripts are checked
     for requests to GitHub or a search tool, and a PR whose reviewer fetched
     its upstream discussion is dropped from both arms and counted.
   - **Priced before it is paid for.** A three-PR smoke gives the cost of
     one arm; the README states it and the minimum detectable effect before
     the 25-PR run. The eval README's recorded cost for a persona arm on
     loom's own cases is roughly $10–20 per case.
   - **Scoring.** Primary metric: grounded recall over all severities, with
     an interval from a bootstrap over PRs, because findings cluster by PR.
     The high-severity slice (35 findings in the test set) is reported and
     not read. Both arms are judged in one judge session; loom#307 found the
     judge stable within a session and drifting between them. A second judge
     pass with `--exclude-producer` for the reviewer's own engine family is
     recorded as a diagnostic.
   - Expected resolution, to be replaced by the bootstrap figure: about 11
     points of recall between two arms on 342 findings before clustering,
     about 4 on the full set.

3. **D3 — Audit the dismissals that rest on unread code.** *Built only if
   D1's reading meets the rule below.* As the operator, I want each
   dismissed entry whose basis is `assumed`, and whose severity would have
   blocked, to be handed to a fresh read-only session with no persona and no
   coder summary, on a different engine from the reviewer where the panel has
   one, carrying only that entry and one question: quote the code that makes
   this failure impossible across every caller, or state the path on which it
   happens; with `upheld` requiring the quoted code and `promoted` becoming
   an ordinary finding from an `audit` reviewer under the project's severity
   threshold, so that a defect the panel saw and rationalised away costs one
   short session instead of an escape.
   - One entry per session, capped per reviewer round (default proposed: 5,
     highest severity first). Sashiko's evidence against a shared verifier is
     the reason for one-per-session.
   - Inside the shared panel-round primitive, so story-develop, `converge`,
     `develop review` and `eval review` all exercise the same code.
   - Per-project switch, default off.
   - **Build rule** (*proposed*, fixed before D1's run): dismissed misses are
     at least 15% of pooled frontier misses, and at least 3 samples across at
     least 2 cases. Sashiko's 16% is the only prior. Below that the audit has
     too little to recover and the candidate is closed with the reading.
   - **Ship rule** (*proposed*, fixed before the A/B): default-on for a
     project only if, with both arms in one session, ReviewBench grounded
     recall rises by at least the minimum detectable effect D2 stated, with
     grounded precision down by no more than 2 points, and no floor case
     regresses. Otherwise it stays an off-by-default switch or is removed.
     The added cost per review is reported with the result.
   - The audit trusts the reviewer's own `basis:` label. If D1 shows
     reviewers label assumed dismissals as `read`, the selection moves to a
     classifier pass, as Sashiko's does; that is a design change to raise in
     the story's brief, not a silent extension.

**Measurement alongside, not a story.** The mirror question is how often a
*finding* is wrong and what it costs. Zero tokens: over retained run
directories, count findings that closed `accepted` after a coder dispute, and
the rounds each run spent on them. If false positives turn out to buy whole
rounds, a fresh-session check of blocking findings before the coder sees them
becomes a candidate with a number behind it. Today it has none.

## Proposed decisions

1. **Sashiko is not adopted, as a replacement or an addition.** Its projects
   are a compile-time Rust enum (linux, sashiko, gcc, qemu, zephyr,
   e2fsprogs) and its stage instructions are Rust constants per project; the
   prompt-directory override does not reach them. There is no Python or
   TypeScript profile and no published result outside C (75.8% on the
   kernel, 58.5% on GCC). It has no acceptance-criteria concept, finding
   lifecycle or gate. Its false-positive rate, under 20% by its maintainers'
   sampling at about four findings a patch, suits a mailing list with a
   human reader and not a loop where a blocking finding buys a coder round.
   If it is ever wanted as a second opinion, the slot is the trusted-bot
   external-review path, since its GitHub mode posts PR comments.
2. **Two mechanisms are ported as ideas, in order**: the dismissal record
   (D1), then its audit (D3). Per-path notes and a stage planner are not
   carried.
3. **ReviewBench is a second instrument, not a target.** It measures generic
   bug-finding. `eval review` stays the instrument for acceptance-criteria
   audit and for loom's own blind-spot classes, and its floor stays the
   regression gate.
4. **No default changes on this PRD's authority.** D1's section and D3's
   audit are switches that default off; turning either on for a project is a
   decision taken on the readings.

## Sequencing

| Order | Story | Why here |
|---|---|---|
| 1 | D1 | the cheapest reading, and D3's build rule needs it |
| 2 | D2 | independent of D1; can run in parallel; D3's ship rule needs it |
| 3 | D3 | only on D1's reading, measured on D1's cases and D2's set |
| any time | the false-positive count | zero tokens |

Success for the milestone: the frontier table has a cause column; there is a
ReviewBench reading for two loom panels with a stated resolution; and D3 is
either shipped against its rule or closed with the number that closed it.

## Non-goals

- Raising the catch rate by prompt or persona changes. Any such change still
  goes through the escape-review process.
- Changing which findings block, or the coder's dispute path.
- Replacing the judge. Both instruments keep their own.

## Open questions

1. Are the build rule (15%, 3 samples, 2 cases) and the ship rule (the
   stated effect, 2 points of precision) the right bars?
2. Should D1's switch be a project-context key like `develop_brief_review`,
   or eval-only until D3 is decided? The section costs every reviewer turn
   some output whether or not anything reads it.
3. A loom finding can name several files; ReviewBench matches within a file.
   One entry per file named, or one for the first? The proposal is one per
   file, with the choice recorded in the reading.
4. Which judge key and model for D2. The judge calls a provider API directly,
   so it is metered spend and not a CLI subscription.
5. Does the cold-generalist arm stand in for "bare engine", or should D2 add
   a third arm with no loom prompt at all?

## Provenance

| Story | From | Date |
|---|---|---|
| D1, D3 | Sashiko's `dismissed_concerns[]` and symmetric proof bar; Dave's question on what loom can learn from it | 2026-11-24 |
| D2 | Dave's follow-up on ReviewBench | 2026-11-24 |
| false-positive count | the review of Sashiko's per-finding verification | 2026-11-24 |
