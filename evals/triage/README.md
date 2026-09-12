# Triage eval — the S5a step measured on known verdicts (PRD pr-reconciliation S8)

`lithos-loom eval triage` measures the **external-finding triage step**
(`plugins/story_develop/external_triage.py`, PRD S5a): the read-only turn that
decides, per claim, whether a coder should act on it. Its contract is
**default-to-act** — a claim is dropped only by an explicit `REJECT` citing a
`file:line` that resolves in the repo — and its failure modes are asymmetric:

- **Over-suppression** — a true claim rejected. The defect ships; a human pays
  later. RH-1's lens34 result is the warning: a prompt tuned to reject false
  claims starts rejecting true ones.
- **Under-rejection** — a false claim let through. Recoverable: the fix is
  still gated by the panel and the check-set before anything is pushed.

So the eval reports two rates per case, and the second is a gate by default.

## Run it

```bash
# All cases, 5 triage turns each (host-only: docker + the agent CLI)
uv run lithos-loom eval triage

# One case, retain every sample's verdicts + a summary
uv run lithos-loom eval triage --case lens43-known-good-batch -k 5 \
  --report-dir ~/lithos-loom-eval-reports/triage-<date>

# Pick the triage agent explicitly (the model is REQUIRED — #304); the turn
# runs under converge's reviewer timeout (3600 s) unless --timeout says otherwise
uv run lithos-loom eval triage --tool codex --model <id> --effort high
```

```
case                           n valid        reject (95% CI)     over-supp (95% CI)     cost  result
----------------------------------------------------------------------------------------------------
lens43-known-good-batch        5     5          9/10 60-98%           0/20 0-16%    $1.10  PASS
```

- **reject** — known-false findings rejected *with a citation into their
  declared `refutation`* (the file, and the line range when one is given),
  over every known-false opportunity in the valid samples (`--bar`, default
  0.8). The parser's own "any resolving `file:line`" is not enough here: the
  eval asks whether triage cited the code that actually refutes the claim,
  and a range keeps the claim's own anchor from scoring as evidence.
- **over-supp** — must-proceed findings rejected, over every such opportunity
  (`--max-over-suppression`, default **0**: one wrongly rejected true finding
  fails the case).
- **valid** — samples whose triage turn produced verdicts. A degraded turn
  (failed, or no verdict file) defaulted to act on everything; that is the
  step's contract, not a verdict, so the sample is excluded from both
  denominators (`+Nerr` on the row). Exit 1 only when a case has no valid
  sample; a FAIL is the measurement.

Each sample is **one turn over the whole batch**, the production shape —
over-suppression shows up in mixed batches, not in isolation. The batch reaches
the step through the **production intake** (`external_intake_reviews`): the
ledger assigns the ids, the rationale carries the `[author]` prefix, there is
one anchor or none, and the severity is production's `minor` — the agent reads
what `converge --from-github` would hand it, never the eval's own rendering.

Both rates are per *opportunity* (finding × valid sample) and carry Wilson 95%
intervals — which ignore that one sample's opportunities come from one turn and
that the same findings are re-asked every sample, so the bands are a **lower
bound** on the uncertainty. `summary.json` also records
`samples_with_suppression` (turns that rejected anything true) as the
per-sample view of the gated rate. Read all of it with the [review harness's
sample-size table](../review/README.md#how-many-samples--what-an-ab-can-actually-detect-rh-5)
in mind.

Every sample retains the raw verdict file and a per-finding **line class**
(`proceed` / `reject` / `reject-uncited` / `missing`), so a deliberate
PROCEED, an uncited REJECT the evidence rule discarded, and a missing verdict
line are distinguishable after the fact — a reject rate near zero can then be
read as "held default-to-act" or "the citation rule did all the work".

## Add a case

```
evals/triage/cases/<id>/
  case.toml
  ac.md          # the AC the claims were made against (context for the agent)
  <name>.patch   # patch form only: the tree as base + patch (see below)
```

```toml
[case]
id = "<id>"
description = """why this batch, provenance of every finding, what a correct
rejection cites, and the decision rule stated BEFORE the first paid run"""
repo = "../lithos-lens"                 # relative to the loom checkout (cross-repo ok)
sha = "<full 40-hex commit>"            # the tree the claims are about — OR:
# base = "<full 40-hex commit>"         #   a reachable base plus a patch in the case
# head_patch = "tip.patch"              #   dir, applied at run time (a tree on no branch)
acceptance_criteria_file = "ac.md"
# image = "ralph-sandbox:python-ui"     # optional; the default image only needs the agent CLI

[[finding]]
id = "f-001"                            # POSITIONAL: f-001, f-002, … in file order —
author = "copilot"                      # the external ledger assigns them that way
path = "src/x.py"                       # the claim's anchor (optional; one, like a
line = 12                               # real inline comment; checked to exist)
body = """the claim, as the reviewer wrote it"""
expected = "proceed"                    # known-true OR ambiguous — both must proceed
provenance = "panel"                    # external | panel | synthetic

[[finding]]
id = "f-002"
body = """a design judgement the code reasons about"""
expected = "proceed"
ambiguous = true                        # scoring-neutral; recorded so a rejection is attributable

[[finding]]
id = "f-003"
path = "src/x.py"
line = 40
body = """a claim the code refutes"""
expected = "reject"
refutation = ["src/x.py:52-58"]         # what a correct rejection must cite: path, path:LINE
provenance = "synthetic"                # or path:START-END; put the refuting code, not the anchor
```

Rules. The first two and the "exists at the tree" half of the third are
gate-enforced by `tests/test_eval_triage_shipped.py` (hermetic, git only; skips
where the checkout is absent); the loader enforces the schema (positional ids,
a refutation on every known-false, `ambiguous` only on a proceed); the rest are
conventions:

- **Every batch carries at least one must-proceed finding.** A corpus that only
  measures rejection trains the wrong reflex.
- **The tree builds.** The sha exists, or base + patch applies; every anchor and
  refutation path is tracked there and every range lies inside its file.
- **A known-false has an exact refutation.** Name the file and the lines that
  refute it; a rejection that cites elsewhere — including the claim's own anchor
  — does not score. Synthetic known-false claims are fine (closed questions the
  tree answers in a line) but declare `provenance = "synthetic"`.
- **Ambiguous is `proceed`, and says so.** A design judgement, a deferral the
  code documents, a disputed AC reading — triage must not adjudicate those
  (loom's review has an out-of-scope disposition for that; triage does not).
  Mark them `ambiguous = true` so a rejection is attributable to a judgement
  rather than a true defect.
- **Real findings verbatim.** The escape-review process's bucket 4 ("invalid
  claim") mints a known-false from a real external finding; a known-good-arm
  finding from `eval review` that validated as real mints a known-true
  (`provenance = "panel"`). Do not paraphrase either.
- **State the decision rule first** (RH-5): the opportunities per run, what
  reading changes S5a's posture, and what is not decisive at K=5.

## Seed corpus (2026-09-11)

`lens43-known-good-batch` — the four findings loom's panel filed on the
operator's merged resolution of lens #43 (two true: lens #82 and the withdrawn
pre-0.4 contract defect; two ambiguous: a documented deferral, a reasoned
wording), all must-proceed, plus two synthetic known-false claims whose
refutations are line ranges the claims' own anchors fall outside. The tree is
the pre-squash tip rebuilt as base + patch (it is on no remote branch); the
preflight pins the rebuilt tree against the local commit. No real false claim
existed in the material — every real finding validated as true or as a
judgement — which is itself the S2 arc's finding repeated. Unmeasured until its
first K=5 run.
