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
the seed reviews the *removal* of the `approved -> delivering` guard as the defect
and its *addition* (on otherwise-clean code) as the known-good. See *Update
(2026-06-24)* for the synthetic clean-mirror construction.

### Matching method (expected → produced)

> **Revised after the first live run.** The seed case returned `catch 100% / fp
> 100%`: the **structured matcher keys on the change's *topic*** (file + topic
> words), and the seed's known-good is the #180 fix — the *same topic* — so any
> finding on it matched. Worse, the original judge was a *miss-only fallback*, so
> it never ran on these false structural *hits*. The fix below makes the judge a
> **confirmer/veto keyed to the specific mechanism**, default on.

1. **Mechanism LLM-judge (default).** The judge is **authoritative**: it sees
   *every* produced finding and returns the finding ids that describe the
   **specific mechanism** (not the topic). So it both **vetoes** a finding that
   only matches the file/topic (a different defect) and **rescues** a correct
   finding worded without the keywords. Severity-correct = a matched finding at or
   above `min_severity`. The agent call is host-direct (a pure text Q&A — no repo,
   no container). `--no-judge` opts out.
2. **Structured (fallback, deterministic, hermetic):** a produced finding matches
   when it touches the expected **file** AND mentions ≥1 expected **keyword**.
   Cheap and agent-free, but *topic-loose* — it over-counts on a same-topic
   change, as the first live run showed. Use it for a quick pass, not a trusted
   number.

**The mechanism-judge also rescues a *contaminated* known-good.** A known-good that
is itself a real change can carry unrelated defects: the original seed's known-good
(the #180 fix) surfaced two real residual gaps
([#188](https://github.com/agent-lore/lithos-loom/issues/188),
[#189](https://github.com/agent-lore/lithos-loom/issues/189)). Those are *different*
mechanisms, so the judge rejects them → the false-positive measurement stays
meaningful without a perfectly clean known-good. (The seed itself was later rebuilt
as a synthetic clean mirror so it no longer relies on this — see *Update
(2026-06-24)* — but the rescue remains a real property for cases whose known-good is
a genuine change.)

### Metrics, K, and the pass bar

Run the panel **K times** per case (default 5) and report, over the K runs:

- **catch-rate** — fraction of runs where every expected defect is surfaced;
- **severity-correctness** — among caught runs, the fraction at/above
  `min_severity`;
- **false-positive rate** — fraction of runs on the paired **known-good** head
  that wrongly trip the matcher;
- **noise rate** — fraction of known-good runs that reported *anything at all*
  (#310; see *Update (2026-08-15)* — the FP rate above is defect-specific and
  says nothing about what else a run reported).

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

## Update (2026-06-24): the seed is now a synthetic clean mirror

The first live re-run under the mechanism-judge gave `catch 100% / sev-ok 100% /
**fp 20%**`. The retained reports confirmed the residual FP was the **contaminated
known-good**: reviewing the real #180-fix commit still surfaced #188/#189 (the
judge vetoed them 4/5; the 1/5 leak conflated #188's pre-delivery-snapshot wording
with the seed's mechanism). The post-#181 reviewer prompts *demand* tracing the
lifecycle and AC#3 *requires* the PR url in the terminal summary, so a known-good
that is the real #180 fix can never be defect-free — its own bugs are reachable.

Building the clean mirror then **drove out a series of real escapes** the known-good
review kept finding in "clean" `main` — each a residual defect in the young
attach/delivery lifecycle: #194 (a failed PR delivery recorded as `succeeded`), #196
(`attach --wait` forever-hang on idempotency-replay/fast-reap + an incomplete reaped
summary), and #198 (best-effort reap/marker holes in `result.json` terminal
detection). Each was verified against the code and fixed — the eval working as
designed (every escape becomes a fix, then a regression case). With
#188/#189/#194/#196/#198 all landed, the seed was rebuilt as a **synthetic clean
mirror** off the hardened `main` (commit `a127361`, tag `eval/180-clean`):

- a one-commit off-branch fixture (`f14e220`, tag `eval/180-noguard`) removes
  **only** the `approved -> delivering` guard in `_run_phase`;
- **buggy** = `eval/180-clean .. eval/180-noguard` (guard removed → catch);
- **known-good** = `eval/180-noguard .. eval/180-clean` (guard added back on
  otherwise-clean code).

The rebuilt mirror measured **`catch 100% / fp 60%` under `--no-judge`** (down from
100%). Reading the residual known-good findings settled the matter: they are all
*different-mechanism* edge cases — the thorough post-#181 reviewers trace every
best-effort failure path in this intricate lifecycle, so a same-file mirror's
known-good carries a long tail of real-but-narrow findings (#194/#196/#198 closed
the run-bindable ones; the rest, e.g. the #189 timeout-as-terminal critique, are
design tradeoffs). **Driving `--no-judge` to 0 is an unbounded chase; the judge is
the trusted matcher** (it vetoes every different-mechanism finding → judge FP ≈ 0).
So the seed ships **judge-scored**: the rebuild removed the *gross* #188/#189-era
contamination and made the buggy catch unambiguous, while `--judge` provides the
trustworthy FP. The judge's *contaminated-known-good rescue* (below) is thus not a
crutch the seed outgrew but the standing mechanism. The richer **patch-in-case-dir**
authoring form shipped in **#193** (below).

## Update (#193): patch-based case authoring

The `180` rebuild proved that authoring a case by *pinning historical commit shas*
is painful — a genuinely-clean head usually needs a **synthetic off-branch commit
kept alive by a pushed tag** (done twice by hand for the `180` mirror). #193 adds a
cheaper, scalable form: a case declares `head_patch` (and optionally
`known_good_head_patch`) — a `.patch` file beside `case.toml` that the harness
applies to `base` in a throwaway worktree, commits as an **ephemeral** commit, and
reviews `base..<ephemeral>` (cleaned up after the run). Now **only `base`** must be
a real reachable commit (a `main` ancestor — no tags), and the seeded defect is a
**reviewable diff in the PR**. `load_case` enforces exactly one of `head` /
`head_patch`; the sha form still works. The first real escape (#194) ships as the
worked example (`cases/194-delivery-failure-status/`). This is the keystone for
*growing* the benchmark toward the independent-case count this ADR requires before
tuning the panel (#175 / #182).

## Update (#182): per-sample confidence intervals + `summary.json`

The original metrics report a **point** catch-rate (caught / K). Over K stochastic
runs that estimate has real sampling error, and the benchmark's grown cases all read
`100%` at `-k 5` — which a point number presents as certainty. It is not: `5/5` only
bounds the miss-rate below ~43%. So the harness now reports each rate (catch + FP) as
a count over K **plus a Wilson 95% confidence interval** (`stats.wilson_interval`),
and `CaseResult` carries the per-sample boolean tuples. With `--report-dir`, a per-case
`summary.json` (rates, per-sample booleans, CIs) is written beside the run reports so a
costly K-sample run is re-analysable **without** re-scoring (which would re-invoke the
paid judge). This is the measurement instrument #182's design depends on: it makes
single-pass review variance a *number with error bars* before any variance-reduction
mechanism is chosen.

**Errored samples (A3).** The first `-k 20` probe on `180` exposed a scoring hole: a
reviewer turn that **crashes** (status `invalid` / `not-run` — here a codex usage limit,
see #103) produces `findings=[]`, and the matcher (which ignored `status`) scored that as
a legitimate "0 findings" review — a *clean pass* on known-good and, dangerously, a *review
miss* on the buggy head, i.e. agent flakiness masquerading as the very variance being
measured. The harness now classifies a sample as **errored** when its caught/flagged
verdict is False *and* the report is incomplete (`match.review_incomplete`), excludes it
from the catch / severity / FP denominators, and reports an `errored` count; rates + CIs are
over the *valid* samples. A genuine catch is always trusted (a real flag/catch even with a
crashed peer is counted). The motivating `180` run reads honestly as **catch 20/20, FP 0/4
(+16 errored)** instead of a misleading "FP 0/20". Independent of #103 (needs only
"valid vs failed", not *why* the turn failed); retry/fallback of a limited reviewer is left
to #103.

The probe's outcome — catch **20/20**, no measurable single-pass variance — drove the #182
decision to **measure before reducing**: the eval can't yet *exhibit* the live #180 variance
(the synthetic mirror is ~100× smaller than the real change, the prompts are already
#181-hardened, and review-only strips the coder/multi-round context), so the first slice is
to build *harder* cases / live-loop measurement, not a panel change. See [ADR 0006](0006-review-variance-measure-before-reducing.md).

## Update (2026-08-11, RH-6): case tiers — frontier headline vs saturated floor

The 2026-08 baseline (10 cases × K=5, then 14 after the contract cases) showed
the benchmark had split into two populations: five legacy cases saturated at
5/5 — with panel prompts tuned in their presence during the #181 arc — plus
`lens27-screenshot-ac` (5/5, validating the #208 AC-checklist), versus eight
discriminating cases at 0–3/5 (the blind-spot and variance classes). Any
aggregate over all cases flatters every future A/B with ~30 free catches, and
"which cases count" lived only in convention.

The split is now tooling-enforced: `case.toml` declares `tier = "floor" |
"frontier"` (loader-validated like `ac_provenance`; optional mid-authoring,
gate-required on every shipped case, undeclared treated as frontier so a case
never opts into the floor silently). `eval review` reports a **pooled frontier
catch-rate** (per-sample catches summed across frontier cases, Wilson CI) as
the headline, renders floor rows as `ok`/`REGRESSED`, and **exits 1 iff a
floor case falls below the bar or a case has no valid samples** — a frontier
FAIL is the measurement, not a failure of the run, so the exit code now means
"regression" and is usable by A/B wrappers. The tiering criterion is
saturation, not age: a case moves to floor once it sits at 5/5 and prompt work
has happened in its presence. Per-case `summary.json` carries the tier.

## Update (2026-08-12, RH-7): the panel-override axis

The harness measured only the panel a case file declared, so comparing levers
(thorough profile, stronger model on one persona, engine swap) meant editing
case files or copying the cases dir — RH-2's temp-dir workaround, and a
blocker for RH-8's model-axis A/B. `eval review` now takes per-run overrides:
`--profile` (replaces the panel with the profile's personas AND sets its
check-set — what a live `develop_review_profile` run would field; gate-only
profiles rejected unless `--reviewer` names a panel), repeatable `--reviewer`
(explicit panel enumeration — add/remove; wins the panel), and repeatable
`--reviewer-override PERSONA.FIELD=VALUE` for `model` / `effort` / `tool`
(**apply-where-present**: a case whose panel lacks the persona runs
unmodified, so full-benchmark sweeps mix panels safely, while the persona
name is still registry-validated so typos fail closed **before any paid
run**). This is exposure, not capability — `ReviewerSpec.model/.effort` (#93)
and `.tool` (#94) already reach the agent in review-only mode; the CLI
resolves the effective panel once per case and closes over `live_review`'s
new `reviewers=`/`profile=` seam. Each case's `summary.json` now records the
**effective** profile + panel, which is what makes two report dirs comparable
arm-to-arm; a multi-arm sweep is N invocations, and a matrix orchestrator is
deliberately deferred until the manual RH-2/RH-8 arms prove tedious.

Engine capability crossings are normalised at resolution (review round 1):
effort is a claude-only knob (`CodexEngine.supports_effort = False` — depth is
model-driven), so an **explicitly overridden** effort on a no-effort engine is
rejected — the requested lever could never fire, and a paid arm would silently
run identical to control while `summary.json` claimed otherwise — while an
effort merely **inherited** across a `tool` swap is cleared, keeping the
recorded panel the *effective* runtime configuration. Every selected case's
panel resolves before the first paid run, preserving fail-before-work.

## Update (2026-08-12, RH-3 / #294): artifact cases — measuring the artifact-review pass

The live panel reviews on **two** surfaces — the diff, and the approval-hold
**artifact-review pass** (#283/#291) that shows reviewers the rendered-page
screenshots the gate collected — but the harness could only measure the first.
The lens22 baseline made the cost concrete: 0/5 with *zero findings*, because
a browser-level escape is structurally invisible in a diff; and the lens PR
#34 chips escape (unstyled plain text, visible in the captures the live pass
approved) had nowhere to become a regression case. An **artifact case** now
declares `artifacts_dir` (checked-in captures) + `artifact_provenance`
(`captured` — authentic renders of the materialised case head, documented in
the description — or `synthetic`; both-or-neither, mirroring `ac_provenance`'s
honesty rule). The harness seeds the files into the run's
`config.artifacts_dir` (the exact layout `render_artifacts_note` walks; the
seeder re-applies the collector's symlink hardening since it is the one other
sanctioned writer to a host-collector-only dir) and runs review-only in
**artifact-only** mode: no check-set, one `reviewer_artifacts.md` round via
the same `run_panel_round` primitive (`artifact_pass=True`, resume now
derived from whether a prior round minted a session, so a fresh panel starts
one instead of resuming a nonexistent id).

**One surface per case, deliberately:** `ReviewFinding` carries no pass
provenance, so a combined diff+artifact run could not attribute a catch to
the surface under measurement — the same no-op/mis-attribution poison RH-7
rejects. The A/B instrument is the **twin pair**: `lens22-artifact-prewrap`
(shipped with this update — real Playwright captures of the delivered head,
whose retained `white-space: pre-wrap` renders visible gaps between every
list item) shares base + patch + AC with the diff-form
`lens22-markdown-prewrap`. Scoring is unchanged (same matcher, same judge,
same same-file-negative discipline); `summary.json` records
`artifacts: {n_files, provenance}`. Two hardening precedents ride along:
committed screenshots are the repo's **first binaries** (gate-enforced 2 MB
per-case budget + `.gitattributes`), and `case.toml` now **rejects unknown
keys at every level** — a typo'd `artifacts_dir` would otherwise silently
run the case as a diff review measuring the wrong surface.

The #302 review hardened two edges of the initial cut. **Root escape (High):**
the first implementation validated only descendants, so an absolute, `..`, or
symlinked `artifacts_dir` root could expose arbitrary host files to the
reviewer container (reproduced by the reviewer). The root check now lives in
one shared `resolve_artifacts_root` + `iter_artifact_files` pair used by the
loader, the seeder, and the CLI summary count — relative-only, no `..`, root
must be a real non-symlink directory resolving inside the case dir, and the
seeder re-runs the full check so even an unvalidated `Case` cannot copy host
files into the reviewer-visible mount. **Catch-only (Medium):** `run_case`
reviews the known-good head with the *same* `Case`, so an artifact case with
`[known_good]` would show the fixed code the buggy captures — the FP number
would be meaningless. `[known_good]` is now rejected on artifact cases;
variant-specific captures (a known-good `artifacts_dir`) are the follow-up if
FP measurement on this surface is ever wanted. *(Superseded 2026-08-14 — see
the RH-1 update below: the pairing shipped.)*

## Update (2026-08-14, RH-1): paired captures make artifact FP measurable

The catch-only restriction above is lifted by declaring the captures **per
variant**: `[known_good] artifacts_dir` alongside `[known_good] head_patch`,
with the seeder picking the set that matches the head under review. Both halves
are required together (load-enforced) — the whole point is that reviewing the
fixed code against the buggy captures would measure the captures, not the
review — and the known-good root goes through the same shared checks.

The motivation is an A/B, not completeness. RH-1's step-0 diagnosis found the
artifact pass's 0-findings result is prompt-caused rather than a vision
capability gap (the reviewer demonstrably calls `view_image` on the captures and
still LGTMs), so the fix under test is a **sharper artifact prompt** — and a
sharper prompt is exactly the change that could raise catch-rate by making the
reviewer flag ordinary design as defective. Without a known-good render the
benchmark scores that failure mode as success.

The pairing carries a data-quality obligation the harness cannot enforce:
capture both variants in one session from the same source, so the only
difference is the defect. For `lens22-artifact-prewrap` the fix is the authentic
one lens shipped (`734e5ef`, dropping `white-space: pre-wrap` from
`.markdown-body`), and re-running the recipe reproduced the committed defect
captures byte for byte — evidence the recipe is deterministic and the pair is
genuinely matched.

## Update (2026-08-15, #310): noise beside the false-positive rate

The false-positive rate asks a known-good run one question: did it report **the
case's expected defect**? Everything else it reported is invisible to that
number — so a panel that files four unrelated findings on a clean head and
**blocks** still scores `fp 0/3`. Measured on `lens22-artifact-prewrap` while
choosing the RH-1 artifact prompt: three candidate arms with the *same* case,
persona and captures scored an identical `fp 0/3` while their known-good
behaviour ranged from silent to blocking every run. That is the wrong
sensitivity for a harness whose whole purpose is choosing between
configurations, and it flatters exactly the changes most likely to be harmful —
anything that raises catch-rate by lowering the bar for what counts as a defect.

So each run now also records the two raw observations its report already
carried: **how many findings** it produced, and whether it **held approval**
(`ReviewReport.blocking` — read from the report, so the eval can never disagree
with what the review decided). Aggregated per case:

- **noise rate** — the fraction of *valid* known-good runs that reported
  anything at all, over the **same denominator** as the FP rate so the two read
  side by side (`fp 0/3` `noise 3/3 blk3` describe the same three runs);
- per-sample finding counts + block flags on **both** arms.

The buggy arm is instrumented but deliberately **not rated**: an extra finding
on a defective head may be a real second defect (the benchmark has found several
— #295, lens#41), whereas on the known-good head it is at best a distraction.
Errored samples are excluded from the noise denominator for the same reason they
are excluded from catch and FP — an incomplete panel reports nothing, and blocks
*by definition* (`intake_blocks`), so counting a crash either way would convict
or flatter an arm for infra flakiness.

Making a blocked known-good head a **gate** (the way a floor regression fails a
run) is available as opt-in `--max-known-good-block-rate`, not a default. Once
requested it fails **closed**: a case whose known-good arm produced no valid
sample fails it too, since that arm runs *after* the buggy one and an exhausted
quota destroys exactly the evidence the gate weighs. Without the flag an
unmeasurable known-good arm remains a reporting gap, unchanged. No
baseline for these numbers exists yet — every report dir predating this change
has none — and gating on an unmeasured quantity is the mistake *Update (#182)*
already named. Re-deriving the numbers offline from retained reports needs no
judge and no tokens (it is pure counting off the report JSON), and doing so over
the existing report dirs immediately surfaced one thing the headline metric had
rendered invisible: the floor case `194-delivery-failure-status`, `ok` in every
table it has ever appeared in, went from `noise 0/5` at the 2026-08-09 baseline
to `noise 3/5 blk3` at the pinned 2026-08-13 baseline — three runs filing a
*critical* "this diff only adds a comment" finding against a known-good head
whose whole design is to be a comment-only no-op. Whether that is a fixture
weakness or a legitimate review is a judgement call; the point is that it was
unmeasurable before.

## Update (2026-08-15, #307) — the judge is an instrument, so it fails loudly

The judge is **authoritative** by this ADR's original design, which made it the
one component whose failures were indistinguishable from measurements. #307 caught
it giving different verdicts to two findings naming the same file, line, severity
and mechanism in different words (`4/5` judged vs `5/5` structured). Diagnosing
that first required removing three ways the code manufactured the same signature:

1. **A judge timeout or missing CLI returned `""`** — no matched ids, identical to
   a veto — and was *not* counted as errored (that flag keyed only on *reviewer*
   status), so it silently depressed catch-rate.
2. **A turn that did not succeed had its text parsed anyway.**
3. **A reply with no `MATCHED:` line was scanned wholesale for finding ids.** The
   prompt asks the model to reason before concluding, so this scored
   "f-001 does NOT describe this" as a match; `MATCHED: none of f-001, f-002`
   matched both ids. Both manufacture a catch out of a rejection.

So a verdict now carries a **status**: `ok` (a real answer, veto included),
`unparsed`, or `failed` (retried once). The latter two are an *absence* of a
verdict and are excluded from every denominator like a crashed reviewer (#182 A3),
but reported separately (`+Njerr`) so it is clear which half of the instrument
broke. The whole-reply fallback is deleted.

`TurnResult.succeeded` turned out to conflate two things, and the first review
round caught the cost of treating them alike. It means *both* "this turn worked"
and "it minted a resumable session handle". The handle exists so a later resume
turn can re-attach — meaningless for a one-shot host-direct Q&A — so gating the
scorer on the whole predicate would mean a CLI that stopped echoing a handle
turned every case into zero valid samples. But the first attempt at that
exemption demoted *every* `succeeded == False` to a benign anomaly, on the
assumption that a failed turn carries no `MATCHED:` line. That assumption was
wrong for both engines: a claude reply with `is_error: true` can still carry one,
and the codex stream **retains the last agent message even when a later
`turn.failed` arrives**. So a crashed turn could manufacture a catch out of
partial or stale output — the very defect the change set out to close.

`TurnResult` therefore now carries **`completed`** (the turn's own outcome)
alongside `succeeded` (`completed` *and* resumable). The judge treats a turn that
did not complete as `failed` — text retained for audit, retried once, never
parsed — and reserves the anomaly for the narrow missing-handle case. The
distinction lives in the engine adapter rather than in per-tool JSON knowledge
re-derived inside the judge, which ARCH-2.E5 deliberately removed.

Two consequences for auditability. The structured matcher is pure over the stored
findings, so **both** answers are now computed every run for free, and the catch
cell shows `struct N/M` whenever they disagree — the divergence #307 was found by
hand is now in the headline. And with `--report-dir`, each judged sample writes its
verdicts *and the judge's raw reply* to `<case>/judge/<variant>-<i>.json`: a
subdirectory, not a sibling file, because `<variant>-<i>.json` is the stable
`ReviewReport` contract that offline re-scoring globs.

What this update deliberately does **not** change is the judge's *policy*. Its
disagreements with the structured matcher were checked and are correct — it rescues
keyword-less catches (`lens27` 0→5, `289` 2→3, `lens34` 2→3) and both baseline
vetoes are right (`lens33`'s finding described the finiteness form its case
explicitly excludes; `291`'s described a different mechanism in the right file).
Only failure paths changed behaviour, so `baseline-pinned-2026-08-13` remains a
valid comparison target. Whether residual variance warrants repeat-and-vote is a
question for measurement, not assertion — #307's own suggestion 3, held until the
audit trail can answer it.

## Update (2026-08-15, #307 slice 2) — scoring separated from the paid run

The judge became auditable in slice 1, but the question #307 actually asks — *how
often does it answer differently?* — still needed a fresh paid sweep to answer,
which is why it went unanswered. `eval rescore` removes that cost.

The enabling observation is that `score_run` was already pure over
`(case.expected, stored report JSON, judge)`: no git, no worktree, no container.
Nothing about scoring ever needed the run. So a retained `--report-dir` can be
scored again for **judge calls alone** — 69 for the whole pinned baseline (70
reports, 85 judge sites, 69 with findings, since the judge short-circuits an empty
findings list) — against hours of container reviewer runs for a fresh sweep.

Two consequences beyond the immediate measurement. A judge-prompt change stops
invalidating a paid baseline and becomes a few dollars of re-scoring. And the
structured counterfactual over a whole corpus is free (`--no-judge`, zero tokens).

**Repeat 0 is authoritative under `--judge-repeats N`.** Taking a majority would
change the estimator in the same command that measures whether the estimator needs
changing; majority-of-N remains suggestion 3, conditional on this evidence.
Stability is reported over sites the judge *actually saw* — a sample that produced
no findings is not a unanimous verdict, and counting it would flatter a quiet arm.

**A judge error is not a verdict, and stability is defined so it can never pose as
one.** Slice 1 established that a timeout is an absence of an answer rather than a
miss; the same fact bites harder here, because a timeout and a veto both match
nothing, so a stability measure keyed on matched ids alone would report an
all-failed sweep as 100% stable — the exact false confidence this command exists to
remove. So a site is *stable* only when every repeat answered and they agreed;
two repeats that answered differently *flip*, counted even if a third errored (a
disagreement observed is a fact about the judge, and dropping the site for an
unrelated timeout would hide the thing being measured); anything else judged is
**unmeasured**, reported beside the ratio and never inside it. Each repeat's whole
verdict — status, matched ids, detail, raw reply — is serialised, because "why did
two identical asks differ?" is unanswerable from the ids alone. For the same reason
the catch spread is reported over **each universe's own valid denominator**: a
repeat where the judge errored has fewer scorable samples, and holding K fixed
would render missing data as a drop in catches.

**A measurement never sets the exit code.** A re-scored floor case reading
`REGRESSED` is a finding about the judge, not a failure of the command; a gate that
runs no reviewers would gate on the judge's mood.

**Fail-closed means the whole corpus, not the flags.** The command's usage errors
must all precede the first paid call, so retained reports are structurally
validated at load (every field the scorer reads — which means *enumerating* them,
since the guarantee is only as good as the shortest field list: a first pass
covered findings but not `reviewers[].status`, and an unhashable status still
crashed the scorer one paid request in) and the entire dir is loaded before any
case is judged — otherwise a malformed report in the last case surfaces only after
the first has been paid for. Not every gap crashes: `bool("false")` is `True`, so
a string `blocking` silently inverts the noise instrumentation instead, which is
why validation checks types rather than waiting for an exception to prove a field
mattered. The `--out` target is resolved and refused
up front for the same reason, including against every retained input: overwriting a
paid artefact with a re-score of it is the one irreversible thing this command
could do. And the printed cost is two numbers, not one — the verdict-request count
is exact as a count of questions, but a failed call retries once, so quoting it
alone would understate a flaky sweep by 2×.

**The bar comes from the run, not the module default.** Re-scoring at 0.8 a run
scored at `--bar 0.6` reports `REGRESSED` for a case whose numbers never moved: the
comparison would be measuring its own flag. `summary.json` records the bar; `--bar`
overrides and says so; a dir with none falls back loudly.

The comparability hazard is that a re-score reads the case from the tree as it
stands *now*. `summary.json` therefore records an **`expected_fingerprint`** over
exactly what the scorer consumes — the case id and its `[[expected]]` blocks, with
keyword and entry order normalised (neither changes scoring) but `mechanism` prose
not (a reflow changes the judge's prompt). The id is in so a case renamed in place
reads as changed rather than silently comparing across identities. `ac.md`,
personas and profile are deliberately out:
they change what the *reviewer* saw, which a re-score never revisits — that is
#309's question, and naming this key for the scorer's inputs alone leaves #309 free
to add its own. A mismatch aborts; an absent fingerprint warns and proceeds, since
every dir written before the field lands there and refusing them would make the
retained corpus — the entire point of the command — unrescorable.

## Deferred

- A genuinely **clean known-good** (a synthetic minimal mutation: the defect and
  its fix differing by *only* the defect) so the false-positive measurement is
  meaningful even under `--no-judge`. **Partly done, conclusion revised** (see
  *Update (2026-06-24)*): the seed *is* now a synthetic minimal mutation, but
  `--no-judge` FP is **not** 0 — thorough reviewers surface a long tail of
  different-mechanism edge cases on intricate lifecycle code, so the seed ships
  **judge-scored** rather than chasing `--no-judge` to 0. The drive nonetheless
  closed real defects (#194/#196/#198). The general patch-based authoring form
  shipped in #193 (see *Update (#193)*).
- A few **mutation-style synthetic** defects (off-by-one, swapped ordering,
  dropped error path) for breadth alongside the real-escape cases.
- Per-case **cost reporting** and a cheaper-than-full panel sampling mode.
