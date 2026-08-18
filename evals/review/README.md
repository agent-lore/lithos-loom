# Review-correctness eval harness (#183)

A **seeded-defect benchmark** that measures how reliably the story-develop
reviewer panel catches real defects. It runs review-only mode (#154) against
known-buggy changes K times and reports catch-rate, severity-correctness, and
false-positive rate. See [ADR 0005](../../docs/adr/0005-review-correctness-eval-harness.md).

**On-demand only — not part of `make check`.** A live run spends real tokens and
needs the host sandbox (docker) + the agent CLIs (`claude` / `codex`). The
harness *logic* is unit-tested hermetically; only the live run below calls agents.

## Run it

```bash
# All cases, 5 runs each (host, from the loom checkout) — judge ON by default
uv run lithos-loom eval review

# One case, 8 runs, a stricter bar, retain each run's report for inspection
uv run lithos-loom eval review --case 180-attach-delivery -k 8 --bar 0.9 \
  --report-dir /tmp/eval-reports

# Quick, cheap, agent-free pass (topic-loose — not a trusted number)
uv run lithos-loom eval review --no-judge
```

The command prints a per-case table plus two tier roll-up lines (see
[Case tier](#case-tier--floor-vs-frontier)): the headline pools catches over
**frontier** cases; **floor** cases report `ok`/`REGRESSED`. It exits non-zero
iff a floor case falls below the bar or a case has no valid samples
(all-errored infra failure) — a frontier FAIL is the measurement, not a failure
of the run. Catch and FP are shown as a count over K plus a **Wilson 95% CI**
(#182) — so a rate is read with its sampling error, not as a bare point
estimate:

```
case                         tier       n       catch (95% CI)   sev          fp (95% CI)        noise  result
--------------------------------------------------------------------------------------------------------------
180-attach-delivery          floor     20        20/20 84-100%  100%     0/4 0-49% +16err   1/4 blk1  ok
lens33-confidence-crash      frontier  20          2/20  3-30%  100%                   0%            —  FAIL
frontier: 2/20 pooled catch (95% CI 3-30%) over 1 case
floor: OK (1 case at bar)
```

The CI is why a low-K run can't prove a clean panel: `5/5` still spans `57-100%`
(a miss-rate up to ~43%), and `0/4` known-good only bounds FP below ~49%.

A reviewer turn that **crashes** (a failed/short-circuited turn — `status`
`invalid` / `not-run`, e.g. a provider usage limit) produces no verdict. Such a
sample is **errored**: excluded from the catch / FP denominators and reported as
`+Nerr`, so agent flakiness never masquerades as a review miss (the `fp` above is
`0/4` valid + `16err`, not a misleading `0/20`). A genuine catch is still counted
even if a panel peer crashed. The infra-failure exit gate keys on the **buggy-side**
samples only: a known-good pass whose samples all errored is reported as `+Nerr`
with no trustable FP number, but deliberately does not affect the exit code — the
pass definition gates on catch-rate, and an unavailable FP measurement is a
reporting gap, not (yet) a run failure. The one exception is an explicitly
requested `--max-known-good-block-rate`, which fails closed on an unmeasurable
known-good arm — see [Noise](#noise-what-the-fp-rate-cannot-see-310).

- `--judge` / `--no-judge` (**default on**): the mechanism LLM-judge confirms each
  finding describes the case's *specific* defect, not just the same file/topic.
  Without it the structured matcher over-counts on same-topic changes (the first
  live run measured 100% FP on the seed). `--judge-tool` picks the agent
  (`claude` | `codex`).
- `--report-dir DIR`: write every run's report to `DIR/<case>/<variant>-<i>.json`
  (`variant` = `buggy` / `known-good`) so you can read the findings behind a number,
  plus a per-case `DIR/<case>/summary.json` (rates, per-sample booleans, CIs,
  the noise instrumentation, and the effective profile + panel) so a costly
  K-sample run is re-analysable for variance without re-scoring.
- `--profile NAME` / `--reviewer NAME` / `--reviewer-override PERSONA.FIELD=VALUE`:
  the panel-override axis — see below.
- `--max-known-good-block-rate RATE`: turn the `blk` count into a gate — see
  [Noise](#noise-what-the-fp-rate-cannot-see-310). Off by default.

### Re-scoring a stored report dir (#307)

`--report-dir` output can be scored **again**, offline, without running a single
reviewer — the only cost is judge calls:

```bash
# free: the structured counterfactual, zero tokens
uv run lithos-loom eval rescore ~/lithos-loom-eval-reports/baseline-pinned-2026-08-13 --no-judge

# see what it will cost and stop
uv run lithos-loom eval rescore REPORT_DIR --judge-repeats 5 --dry-run

# the measurement #307 exists for: the same question, five times, identical input
uv run lithos-loom eval rescore REPORT_DIR --judge-repeats 5 --case lens22-artifact-prewrap
```

Above one repeat the table gains two columns:

| column | reads | means |
|---|---|---|
| `flip` | `FLIPPED/MEASURED +Njerr` | judged sites whose verdicts disagreed, over the sites that could be measured at all; `+Njerr` counts **every** site that hit a judge error, including one that flipped and stayed in the denominator. Sites where the run produced no findings are excluded — the judge never saw them, and counting free unanimity as stability would flatter a quiet arm. |
| `spread` | `MIN-MAX/VALID` | catch count under each of the N universes, over each one's own valid denominator. `4-5/5` says the arm could have reported either. |

**A judge error is not a verdict.** A timeout and a veto both match nothing, so a
site is *stable* only when every repeat answered and they agreed; two repeats that
answered differently *flip* (counted even if a third errored); anything else judged
is **unmeasured** and reported beside the ratio, never inside it — an all-timed-out
measurement reads `UNMEASURED`, never `100% stable`. A repeat where the judge
errored also has fewer scorable samples, which is why `spread`'s denominator moves
with it rather than sitting at K.

**Repeat 0 is authoritative.** The command measures variance; it does not quietly
re-estimate while measuring (majority-of-N is #307's suggestion 3, held until this
says whether it is needed).

`rescore.json` lands at the report-dir root (`--out` redirects it; it is refused if
it names a retained report or `summary.json`, since that would overwrite a paid
input with a re-score of it, or any other name inside a case dir, since the loader
would then reject that dir on the next run) and reuses `summary.json`'s field names so drift
compares field-for-field. Per-site verdicts are recorded even at one repeat, each
with its status and raw reply: a site whose findings were produced and whose verdict
matched nothing *is* a veto, named — and the status is what separates it from a call
that never answered. Everything fails closed before the first judge call, including
a structural check of every retained report the scorer reads — a reviewer must
carry a real `status` (`LGTM` / `FINDINGS` / `invalid` / `not-run`), since a typo
would read as a clean review and inflate the valid denominator. The printed count is
the number of **verdict requests**; a failed call retries once, so the same line
gives the ceiling (`up to 2N agent invocations`).

`--bar` defaults to **the bar the run recorded**. Re-scoring at 0.8 a run scored at
0.6 would report `REGRESSED` for a case that never moved — that is the flag talking,
not the judge. Dirs with no recorded bar say so and fall back to the default.

Report dirs written before #307 have no `expected_fingerprint`, so they warn
("case identity unverifiable") and proceed — refusing them would make the whole
retained corpus unrescorable. A fingerprint that is present and *differs* aborts:
the case's `[[expected]]` changed, so a re-score would answer a different question
than the run did (`--allow-changed-cases` to proceed anyway).

### Noise: what the FP rate cannot see (#310)

The `fp` column asks a known-good run **one** question: did it report *the case's
expected defect*? Anything else it reported is invisible to that number — so a
panel that files four unrelated findings on a clean head and **blocks** still
scores `fp 0/3`. That is not hypothetical: three candidate arms of the RH-1
artifact prompt, same case and persona, scored an identical `fp 0/3` while their
known-good behaviour ranged from silent to blocking every single run.

The `noise` column reads `NOISY/VALID blkN`:

- **NOISY/VALID** — known-good runs that reported **anything at all**, over the
  same valid denominator as `fp`, so the two describe the same runs. `fp 0/3`
  beside `noise 3/3` means: it never mistook a clean head for the seeded defect,
  and it never shut up either.
- **blkN** — how many of those runs **held approval** (the report's own
  `blocking` flag: any reviewer below its threshold, an incomplete panel, or a
  blocking deterministic floor). This is the operational cost — a config with
  `blk3` would hold a clean PR every time.
- `—` for a case with no known-good arm (nothing to measure).

`summary.json` carries `noise_rate` + CI, `known_good_findings_per_sample`,
`known_good_blocked_per_sample`, `known_good_blocked`, and the same
`findings_per_sample` / `blocked_per_sample` for the **buggy** arm — recorded but
deliberately **not** rated, since an extra finding on a defective head may be a
real second defect (this benchmark has found several).

`--max-known-good-block-rate RATE` fails the run (exit 1, like a floor
regression) when a case blocks its known-good head more often than `RATE`. It is
**off by default**: no baseline for these numbers exists yet, and gating an
unmeasured quantity is how a lever gets picked blind. Cases with no known-good
arm are never convicted by it.

Once requested it fails **closed**: a case whose known-good arm ran but produced
**no valid sample** (all errored) fails the gate too. That arm runs *after* the
buggy one, so an exhausted quota destroys exactly the evidence a clean-head gate
weighs — "no evidence" must not read as "no violation". Without the flag an
unmeasurable known-good arm stays a reporting gap, as documented above.

Both rate options (`--bar`, `--max-known-good-block-rate`) must be finite and
within `[0, 1]`, checked before any paid run. An out-of-range rate does not error
on its own — it silently redefines the run: `--max-known-good-block-rate 10` (a
typo for `0.10`) disables the very gate it asks for, and `--bar 0` retires the
floor regression gate.

Because the numbers are pure counting off the retained report JSON — no judge,
no tokens — an **existing** `--report-dir` can be re-derived offline:

```python
from lithos_loom.evals.review.match import finding_count, review_blocked
n_findings, blocked = finding_count(report), review_blocked(report)
```

### Panel overrides (RH-7)

The eval can vary the panel **per run**, without editing case files or persona
definitions — the lever axis behind the RH-2 (thorough) and RH-8 (model) A/Bs:

```bash
# Thorough A/B: the full thorough panel (5 personas) + its deeper check-set
uv run lithos-loom eval review --case lens33-confidence-crash --profile thorough

# Model A/B: a stronger model on the correctness reviewer, prompts unchanged
uv run lithos-loom eval review --reviewer-override correctness.model=<model-id>

# "standard plus one": add/remove reviewers by enumerating the panel
uv run lithos-loom eval review --reviewer correctness --reviewer security \
  --reviewer dependency-hygiene

# Engine swap on one persona (mixed-panel levers)
uv run lithos-loom eval review --reviewer-override security.tool=codex
```

Semantics:

- **`--profile NAME` replaces the panel** with the profile's personas AND sets its
  check-set — exactly what a live run with `develop_review_profile = NAME` would
  field. A gate-only profile (`minimal`) is rejected unless `--reviewer` supplies
  the panel (then `--profile` is check-set-only).
- **`--reviewer NAME` (repeatable) wins the panel**: explicit enumeration of
  canonical personas (dedup, order preserved) — this is how you add or remove
  reviewers relative to a case/profile panel.
- **`--reviewer-override PERSONA.FIELD=VALUE`** (repeatable; `FIELD` ∈ `model` |
  `effort` | `tool`) then adjusts personas **where present** in the effective
  panel: a case whose panel lacks the persona runs unmodified (full-benchmark
  sweeps mix panels), while the persona *name* is still validated against the
  registry so a typo fails closed. Later duplicates win.
- **Effort is a claude-only knob** (codex depth is model-driven —
  `supports_effort=False`): an explicit `PERSONA.effort=` override whose
  effective engine has no effort knob is **rejected** — the requested lever
  could never fire, so the paid arm would silently run identical to control.
  An effort merely *inherited* from a persona across a `PERSONA.tool=codex`
  swap is **cleared**, so `summary.json` always records the *effective*
  runtime configuration, never a recorded-but-ignored setting.
- Everything validates **before any paid run** (exit 2, no containers) —
  including the capability check above, which resolves every selected case's
  panel up front.
- Each case's `summary.json` records the **effective** profile + panel
  (`name/tool/model/effort/block_threshold` per reviewer), so two report dirs are
  comparable arm-to-arm. A sweep is N invocations with distinct `--report-dir`s —
  a matrix orchestrator is deliberately a follow-up.

There is no coder to override: `eval review` is review-only mode. The judge's
engine is `--judge-tool`.

**Explicit models (#304).** After override resolution the harness applies the
loom TOML's `[story_develop.default_models]` (tool → model) to every panel
member still on `model=None`, and a reviewer left without an explicit model
aborts the whole invocation pre-paid. Rationale: the fallback was the sandbox
image CLI's builtin default — recorded nowhere, drifting with image rebuilds —
which made arms silently incomparable (#303 found every historical run pinned
to whatever the image shipped). `summary.json` therefore always records the
real model of every reviewer, and the per-case stderr line prints the resolved
panel on every run, not only under override flags.

## Add a case

Every defect that escapes review and is caught later (by a human, by the codex
backstop, in prod) should become a regression case. Create a directory under
`cases/<id>/`:

```
cases/<id>/
  case.toml             # the defect: base + head (sha OR patch), expected findings
  ac.md                 # the acceptance criteria the reviewer receives (issue body)
  head.patch            # (patch form, #193) the seeded change applied to base
  artifacts/            # (artifact case, RH-3) rendered-page captures — see below
```

### Patch form (#193, preferred)

A case's head can be a **`.patch` applied to `base` at runtime** instead of a
pinned sha — so a case needs **no off-branch commit + tag**: only `base` is a real
reachable commit (a `main` ancestor), and the seeded defect is a reviewable diff in
the case dir. Author it by introducing the defect on top of `base` and capturing a
plain `git diff`:

```bash
git worktree add --detach /tmp/seed <base-sha>
cd /tmp/seed && <edit files to introduce the defect>
git diff > <case-dir>/reintroduce-defect.patch
cd - && git worktree remove --force /tmp/seed
```

```toml
[case]
id = "<id>"
description = "..."
repo = "."
base = "<base sha>"                       # a real reachable commit (the only sha)
head_patch = "reintroduce-defect.patch"   # applied to base -> the buggy head
personas = ["correctness"]                # validated at load (a typo fails closed)
profile = "standard"                      # selects the check-set; validated at load
acceptance_criteria_file = "ac.md"
ac_provenance = "replay"                  # what ac.md IS — see below
tier = "frontier"                         # floor | frontier — see below

# Optional clean pair for the false-positive measurement — its own patch (an
# independent clean change), or a sha (`head` / `base`), or omit for catch-only.
[known_good]
head_patch = "clean-change.patch"

[[expected]]
file = "path/to/file.py"               # the finding must touch this file
keywords = ["delivery", "approved"]    # ...and mention >= 1 keyword
min_severity = "critical"              # ...at or above this band
mechanism = "prose describing the defect (the LLM-judge keys on this)"
```

`load_case` enforces **exactly one** of `head` / `head_patch` (and likewise for the
known-good); a patch file must exist in the case dir (fail-closed at load). See
`cases/194-delivery-failure-status/` for a worked example.

Four pairing shapes are in use, tightest first — pick the tightest one the
escape allows, and say in the `description` which it is:

| shape | example | what the `fp` number means |
|---|---|---|
| **clean mirror** — known-good is the exact reverse of the defect pair | `180-attach-delivery` | FP is meaningful even without the judge: the two heads differ by the guard alone |
| **defect + its authentic fix** — the same change plus the hunk that fixed it | `lens22-artifact-prewrap`, `lens33-confidence-crash` | the defect is the only difference that matters, though the patch may be large |
| **defect head vs merged head** — the feature as first pushed vs after review closed it | `289-symlink-artifacts` | valid, but **not minimal**: the heads also differ by everything else the review changed, so a finding about code that exists only at the known-good head is not automatically a false positive |
| **synthetic minimal fix** — a hand-written patch closing the seeded defects and nothing else | `lens34-truncation` | minimal by construction, so **both `fp` and `noise` are meaningful** — but the known-good is not a historical artefact, and a mistake in the hand-written fix becomes reviewer findings that inflate this case's noise |

Reach for the **synthetic** shape only when no authentic fix is usable — for
`lens34` the delivered head is pre-rebase, so the real fix commits are not its
descendants and the only authentic pair spans 34 files of unrelated review
work. When you do, the fix must close each expected **as its `mechanism` text
describes it**, not merely change the behaviour: lens34's overlap expected
requires that the ambiguity stop being resolved *silently*, so reordering the
branches would not have closed it — explicit detection plus an operator-visible
message does. Verify the fix in the target repo (lint, typecheck, full suite)
before generating the patch; its correctness is now part of the benchmark.

Two traps when generating a `known-good.patch` from a worktree: `git diff`
**omits untracked files**, so a defect patch that ADDS a file (lens34 adds
`frontier.py`) silently produces a known-good missing exactly the file you
edited — stage with `git add -A` and use `git diff --cached`. And a defect head
may ship a test that **asserts the defect**, which the fix must then correct
(lens34's truncation test named the limit but never reached it), so run the
target repo's suite rather than assuming a green fixture.

Whatever the shape, the known-good must *actually* be known-good. The loader and
the runtime only enforce that the two heads build **different** content — "it
applied" is not "it is fixed". A case whose validity rests on specific hunks
pins them in `tests/test_eval_review_patch.py` (see the `lens22`, `289` and
`lens33` fixture tests), so re-generating a patch from the wrong ref fails
`make check` rather than silently corrupting every later FP number.

**"Known-good" means clean *of the measured defect*, not globally clean** — which
is why `fp` is defect-specific (§ [Noise](#noise-what-the-fp-rate-cannot-see-310)).
A real fix commit is rarely a defect-free tree, and two shipped cases prove it:
289's merged head still carries a TOCTOU on its symlink guards ([#319](https://github.com/agent-lore/lithos-loom/issues/319))
which every known-good sample found independently, and lens33's fix head still
has the unrelated namespace-derivation and access-scope bugs the baseline panel
filed. Such a case can therefore run high on `noise` / `blk` **and** score a clean
`fp` at the same time, which is correct rather than a scoring bug: the panel is
finding real defects that are simply not the seeded one. 289 measured exactly
that — `fp 0/5` beside `blk 5/5` in both arms of the RH-1 A/B. (lens33's arm is
newly paired and unmeasured; expect the same shape, but it is a prediction until
it is run.) Two consequences when using such a case as a control — read `fp` as
the FP number (it is the only one that isolates the seeded defect), and do
**not** use "the known-good arm got no noisier" as a ship criterion where `blk`
is already saturated; compare `known_good_findings_per_sample` arm-to-arm
instead.

Keywords are substring-matched (case-insensitive) against the finding's
rationale + files, so keep them **discriminative**: prefer exact identifiers
(`with_claims`, `frontier_limit`) and multi-word phrases over generic terms
(`default`, `incoming` — both burned us in review), and watch substring traps
(`"inf"` ⊂ "insufficient"). Every new case should ship scoring tests in
`tests/test_eval_review_match.py` including at least one **same-file negative**
per expected — a plausibly-wordable unrelated finding that must NOT match —
since a topic-adjacent structured match silently inflates `--no-judge` numbers.
The LLM judge (the default, authoritative scorer per ADR 0005) keys on
`mechanism` prose instead; keyword precision matters for `--no-judge` runs and
offline re-scoring of stored reports.

### AC provenance — say what the criteria ARE

A case's *patch* should always be the authentic historical diff, but its `ac.md`
may not be the authentic review input, and that changes what a catch/miss on the
case measures. Declare it with `ac_provenance`:

- **`replay`** — ac.md is the authentic criteria the original review context had
  (e.g. the real Lithos task body).
- **`trimmed`** — an authentic source edited to isolate the measured escape; the
  case `description` must document **every** trim and why (each trimmed clause
  is one the head genuinely didn't satisfy, so leaving it in would let a
  reviewer be scored a miss while reporting a real unmet criterion). Findings
  against trimmed-away criteria are fixture noise, not signal.
- **`synthetic`** — written for the fixture, typically because no authentic AC
  existed (e.g. a hand-developed PR that never went through a panel).

Unknown values fail at load. The loader keeps the field optional (a case dir
mid-authoring outside the shipped set may omit it), but **every shipped case
must declare it** — gate-enforced with no allowlist, so a future case can't
silently regress to undocumented provenance. All pre-2026-08 cases are
`synthetic`: they replay escapes from hand-developed loom PRs that never had a
panel AC, with problem statements written for the fixture.

### Case tier — floor vs frontier

The benchmark's headline number must only count cases that still discriminate.
The five legacy loom cases saturated at 5/5 — and the panel prompts were tuned
in their presence (the #181 arc) — so any aggregate including them flatters
every future A/B with free catches. Declare each case's role with `tier`
(RH-6):

- **`floor`** — saturated: the panel reliably catches it (and its presence may
  reflect prompt tuning rather than general skill). It contributes a
  **regression gate only**: the row reads `ok`/`REGRESSED`, and a floor case
  below the bar is a **hard failure** of the whole run (exit 1) regardless of
  frontier gains.
- **`frontier`** — discriminating: the headline **pooled catch-rate** (per-sample
  catches summed across frontier cases, with a Wilson CI) is measured over
  these only. A frontier FAIL is expected while the case discriminates and
  does not affect the exit code.

The criterion is **saturation, not age**: a case moves to floor once it has
been at 5/5 and prompt work has happened in its presence (that's why
`lens27-screenshot-ac` is floor alongside the legacy five, while the eight
2026-08 blind-spot/variance cases are frontier). Unknown values fail at load;
the loader keeps the field optional mid-authoring, but **every shipped case
must declare it** (gate-enforced, no allowlist) and the CLI treats undeclared
as frontier — a case never opts into the floor silently.

Naming note: this "floor" is unrelated to the **check-set floor** in
`profiles.py` (the required checks a review profile always runs).

### Artifact cases — measuring the artifact-review pass (RH-3, #294)

The live panel has **two** surfaces: the diff review and the approval-hold
**artifact-review pass** (#283/#291) that shows reviewers the rendered-page
screenshots the gate's checks collected. Diff-form cases can't measure the
second surface — lens22's escape is *browser-level* (every HTML-string test
passes; only the rendered page is wrong) and baselined 0/5 exactly because the
defect is structurally invisible in a diff. An **artifact case** carries the
captures and measures that pass instead:

```toml
artifacts_dir = "artifacts"          # case-dir-relative; PNGs checked in
artifact_provenance = "captured"     # captured | synthetic — see below
```

Semantics — one surface per case, deliberately:

- The harness seeds `artifacts/` into the run's artifacts dir (the exact
  layout the live pass renders) and runs review-only in **artifact-only**
  mode: no check-set, one `reviewer_artifacts.md` round (`artifact_pass`),
  the same panel primitive `develop()` uses. The diff panel does **not** run —
  findings carry no pass provenance, so a combined run could not attribute a
  catch to the surface under measurement. Pair an artifact case with its
  diff-form twin (same base, same patch) to A/B the two surfaces:
  `lens22-artifact-prewrap` / `lens22-markdown-prewrap` are that pair.
- `[[expected]]` scores identically (structured match + judge); write the
  `mechanism` in terms of what the captures *show*.
- **False positives need paired captures** (RH-1). The harness reviews the
  known-good head with the *same* case, so a `[known_good]` head must bring its
  own renders — otherwise the fixed code would be shown the buggy captures and
  the FP number would measure the captures, not the review. #302 rejected the
  pairing outright for that reason; declaring both variants is what lifts it:

  ```toml
  [known_good]
  head_patch = "known-good.patch"           # the defect patch + its fix
  artifacts_dir = "known-good-artifacts"    # the SAME pages at that head
  ```

  The seeder picks the variant matching the head under review. Both variants
  share one `artifact_provenance` (same pages, same recipe, two heads), and the
  known-good captures go through the identical root/symlink/empty-file checks.
  Omitting either half fails closed at load.

  The **pairing invariants** are load-enforced too, because each way of
  breaking them yields a false-positive rate that measures the fixtures rather
  than the review: the two heads must not be the same sha or the same patch
  file (and, once materialised, must not resolve to the same commit or the same
  **tree** — different patches can build byte-identical code); the two capture
  roots must be **distinct and non-nested** (an outer root's recursive walk
  would swallow the inner variant's files); they must hold the **same relative
  paths** (two defect viewports against one known-good viewport compares
  different stimuli); and at least one corresponding capture must **differ in
  bytes** (identical captures mean the measured surface cannot tell the heads
  apart at all). An artifact case without
  `[known_good]` stays catch-only, which is fine for a case whose defect has no
  fix to render — but a prompt or panel change that *sharpens* artifact review
  cannot be told apart from one that merely makes the reviewer trigger-happy
  unless the case is paired.
- `artifact_provenance` mirrors `ac_provenance`'s honesty rule: **`captured`**
  = authentic renders of the case head (materialise the head, serve it, take
  real screenshots — document what/when/how in the `description`);
  **`synthetic`** = hand-made renders. Required whenever `artifacts_dir` is
  set; both-or-neither is load-enforced.
- Validation is fail-closed at load AND at seed time (one shared root check +
  walk, so the two can't drift): `artifacts_dir` must be a real, non-symlink
  directory **inside the case dir** (absolute paths and `..` rejected — an
  escaping root would expose arbitrary host files to the reviewer container),
  with ≥1 non-empty regular file and no symlinks anywhere; and unknown
  `case.toml` keys now fail everywhere (a typo'd `artifacts_dir` would
  silently measure the wrong surface).
- **Byte budget:** committed screenshots are the repo's only binaries;
  gate-enforced ≤ 2 MB total per case (`_ARTIFACTS_BUDGET_BYTES`). Downscale
  and prefer a short page over cropping the defect out.

Capture recipe (what produced the lens22 twin's PNGs):

```bash
git -C <customer-repo> worktree add /tmp/head <base-sha>
cd /tmp/head && git apply <case-dir>/feature-with-defect.patch
# serve the head (against live backing services if the app needs them), then:
npx playwright screenshot --viewport-size=768,1200 --full-page <url> \
  <case-dir>/artifacts/<page>-768.png     # repeat per width
```

Capture **both variants in one session**, from the same source data and the
same recipe, so the pair differs only by the defect: serve each head on its own
port and shoot the same URL at the same widths. Anything else that moves
between the two captures (fixture content, fonts, browser version, viewport)
becomes a signal the reviewer could pick up on instead of the defect. A useful
check that the recipe is deterministic: re-capturing the *defect* head should
reproduce the committed PNGs byte for byte (it did for lens22).

Runs use the normal CLI unchanged — every axis (K, judge, tier roll-ups,
RH-7 panel overrides) composes; `summary.json` gains
`"artifacts": {n_files, provenance}` — plus `known_good_n_files` when the case
is paired, so a report says whether it measured false positives at all — and
the running line notes `[artifact pass; N file(s)]`.

### Cross-repo cases (escapes from customer projects)

`repo` resolves relative to the loom checkout's cwd, so a case can target a
sibling customer checkout — e.g. `repo = "../lithos-lens"` for the lens escapes.
This leans on the same host-only assumption the eval already makes (docker, agent
CLIs): the sibling checkout must exist and contain the case's `base` commit
(a `main` ancestor of that repo). Patch form keeps the seeded defect
self-contained in the case dir, so nothing in the customer repo needs tags or
kept-alive commits.

### Preflight: patches are materialised in the gate

`test_shipped_patch_cases_materialise` (tests/test_eval_review_patch.py) applies
every shipped patch case at its pinned base as part of `make check`, so a
drifted patch or missing base fails the gate rather than the paid live run. It
skips — with a reason — wherever it *can't* run for real: no `.git` (the
in-sandbox gate tree), a shallow clone missing the base (loom CI checks out with
`fetch-depth: 0` so same-repo cases do run there), or an absent sibling checkout
for cross-repo cases. Before a paid eval run on a new host, preflight with:

```bash
uv run pytest tests/test_eval_review_patch.py -k shipped -v
```

and treat any SKIPPED cross-repo case as "this host can't run that case".

### Sha form (when history already isolates the defect)

```toml
[case]
base = "<base sha>"             # the defect diff is base..head
head = "<buggy head sha>"
# Optional clean pair; may use its own base so the known-good is an independent
# clean diff, not the empty diff.
[known_good]
base = "<clean base sha>"
head = "<clean head sha>"
```

The sha form needs each head to be a reachable commit — a synthetic clean head
that isn't on any branch must be kept alive by a pushed tag (see the `180`
seed). The patch form (above) avoids that.

## Scoring (how a finding matches)

- **Mechanism LLM-judge (default, `--judge`):** authoritative. Given the reviewer's
  findings and the expected `mechanism`, it returns which findings describe *that
  specific* defect — so it both **vetoes** a same-topic false hit and **rescues** a
  correctly-worded finding that shares no `keyword`. Severity-correct when a matched
  finding is at/above `min_severity`.
- **Structured (`--no-judge`):** a produced finding matches when it touches the
  expected `file` AND mentions ≥1 `keyword`. Cheap and agent-free, but over-counts
  when the known-good shares the defect's topic (the first live run measured 100% FP
  this way) — useful for a quick pass, not a trusted number.

The structured answer is computed on **every** run, judge or not — it is pure over
the stored findings, so it costs nothing. When it disagrees with the judge the catch
cell gains a trailing `struct N/M`; when they agree it stays silent (the `+Nerr`
precedent). `summary.json` always carries `structured_caught_per_sample`.

### Auditing a judge verdict (#307)

The judge answers with a **status**, because an empty match used to mean three
different things:

| status | meaning | scored as |
|---|---|---|
| `ok` | a real answer — matched ids, or an explicit `MATCHED: none` veto | the measurement |
| `unparsed` | the reply had no readable `MATCHED:` line | **excluded** (`+Njerr`) |
| `failed` | no usable reply — timeout, missing CLI, or a turn that did not complete (retried once first) | **excluded** (`+Njerr`) |

Excluded samples leave the denominators exactly as a crashed reviewer does (#182 A3),
so a judge timeout can no longer masquerade as a review miss. They are reported
separately from `+Nerr`, so you can tell which half of the instrument broke — and
`+Njerr` appears on the arm it happened to, beside `catch` or beside `fp`.

A turn that did not *complete* (non-zero exit, claude `is_error`, codex
`turn.failed`) is `failed` and its text is never parsed: it may be partial or
stale, and the codex stream retains the last agent message even when a later event
fails the turn. A turn that completed but minted no resumable session handle is
still scored — that handle only matters for resume turns, which a one-shot judge
call never does.

With `--report-dir`, every judged sample writes its verdicts — including the judge's
**raw reply** — to `<case>/judge/<variant>-<i>.json`. To find every veto in a run:

```bash
jq -r 'select(.caught==false and .structured_caught) |
       "\(.case) \(.variant)-\(.sample): \(.expected[0].reply)"' \
   REPORT_DIR/*/judge/*.json
```

That is the shape #307 was filed on: the structured matcher accepted a finding the
judge rejected. Report dirs written before #307 have no `judge/` directory.

A case is **caught** in a run iff *every* expected defect matches. Reported over K
runs: catch-rate, severity-correctness (among caught), and false-positive rate (on
the known-good head). A case is at bar when `catch-rate ≥ bar` (default 0.8) —
but what that *means* depends on its [tier](#case-tier--floor-vs-frontier):
below-bar is `REGRESSED` (hard failure, exit 1) for a floor case and an
informational `FAIL` for a frontier case, whose signal is the pooled headline
catch-rate.

## Seed case

`180-attach-delivery` — the #180 / #171 defect: `develop attach` exits on the
`approved` verdict before PR delivery (the false-done window). It is a **synthetic
clean mirror** built off the hardened `main`: the buggy head (`eval/180-noguard`)
removes only the `approved -> delivering` guard from clean code, and the known-good
reviews the reverse (adding the guard back). **Judge-scored** (the default): the
rebuild removed the gross #188/#189-era contamination (the original seed paired the
real #180-fix commit with its pre-fix parent and measured 100% `--no-judge` FP), and
building it drove out a series of real escapes that had to be fixed first
(#194/#196/#198). `--no-judge` FP is still **not** 0 — the thorough post-#181
reviewers surface a long tail of *different-mechanism* edge cases on this intricate
lifecycle that the mechanism-**judge** vetoes — so the trustworthy FP comes from
`--judge`. See [ADR 0005](../../docs/adr/0005-review-correctness-eval-harness.md).

### Keeping synthetic-case commits alive

A case may diff against a commit that is **not on any branch** — e.g. the
`180-attach-delivery` buggy head is a one-line fixture committed on top of `main`,
not part of any merge. Git would garbage-collect such a commit once nothing points
at it. **Pin each off-branch fixture commit with a pushed annotated tag** (the seed
uses `eval/180-noguard` for the fixture and `eval/180-clean` for its clean base);
`case.toml` references the resolved **commit sha**, and the tag is the reachability
anchor that survives `gc` and lets a fresh clone fetch it (`git fetch --tags`). The
live eval is host-only, so only the host running it needs the tags — they are not
required by `make check`.
