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

The command prints a per-case table and exits non-zero if any case falls below
its bar. Catch and FP are shown as a count over K plus a **Wilson 95% CI** (#182)
— so a rate is read with its sampling error, not as a bare point estimate:

```
case                           n       catch (95% CI)   sev          fp (95% CI)  result
----------------------------------------------------------------------------------------
180-attach-delivery           20        20/20 84-100%  100%     0/4 0-49% +16err  PASS
```

The CI is why a low-K run can't prove a clean panel: `5/5` still spans `57-100%`
(a miss-rate up to ~43%), and `0/4` known-good only bounds FP below ~49%.

A reviewer turn that **crashes** (a failed/short-circuited turn — `status`
`invalid` / `not-run`, e.g. a provider usage limit) produces no verdict. Such a
sample is **errored**: excluded from the catch / FP denominators and reported as
`+Nerr`, so agent flakiness never masquerades as a review miss (the `fp` above is
`0/4` valid + `16err`, not a misleading `0/20`). A genuine catch is still counted
even if a panel peer crashed.

- `--judge` / `--no-judge` (**default on**): the mechanism LLM-judge confirms each
  finding describes the case's *specific* defect, not just the same file/topic.
  Without it the structured matcher over-counts on same-topic changes (the first
  live run measured 100% FP on the seed). `--judge-tool` picks the agent
  (`claude` | `codex`).
- `--report-dir DIR`: write every run's report to `DIR/<case>/<variant>-<i>.json`
  (`variant` = `buggy` / `known-good`) so you can read the findings behind a number,
  plus a per-case `DIR/<case>/summary.json` (rates, per-sample booleans, CIs) so a
  costly K-sample run is re-analysable for variance without re-scoring.

## Add a case

Every defect that escapes review and is caught later (by a human, by the codex
backstop, in prod) should become a regression case. Create a directory under
`cases/<id>/`:

```
cases/<id>/
  case.toml             # the defect: base + head (sha OR patch), expected findings
  ac.md                 # the acceptance criteria the reviewer receives (issue body)
  head.patch            # (patch form, #193) the seeded change applied to base
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

Unknown values fail at load; omitting the field is allowed only for legacy
sha-era cases (new patch-form cases must declare it — gate-enforced).

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

A case is **caught** in a run iff *every* expected defect matches. Reported over K
runs: catch-rate, severity-correctness (among caught), and false-positive rate (on
the known-good head). A case **passes** at `catch-rate ≥ bar` (default 0.8).

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
