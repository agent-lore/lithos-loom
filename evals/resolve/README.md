# Resolve eval — the S5 conflict-resolution path measured on real merges (PRD pr-reconciliation S8)

`lithos-loom eval resolve` measures **S5's autonomous conflict resolution**
(`converge --resolve-conflicts`, the CLI half; the github-watcher's conflict
resolver dispatches the same run) on a **real conflicting merge** with an
**executable oracle**. The question it answers is the one PRD S8 asks before
S5 is trusted: when loom resolves a delivered PR's conflict with its moved
base and the panel approves the composed tree, is the merge commit S5 pushes
*correct* — not merely marker-free and gate-green?

The [review harness](../review/README.md) scores a panel on a seeded diff;
`lens43-composed-projects` there showed the panel *can* review a composed
tree (5/5). This harness runs the **whole path** — coder, check-set, panel,
rounds — and reads the result against a property the case declares, so the
number is about what would have been pushed.

## Run it

```bash
# All cases, 5 S5 runs each (host-only: docker, the agent CLIs, and the
# target project's toolchain for the probes)
uv run lithos-loom eval resolve

# One case, retain every sample's score + diffs + conversation log
uv run lithos-loom eval resolve --case lens43-projects-merge -k 5 \
  --report-dir ~/lithos-loom-eval-reports/resolve-<date>

# Pick the coder explicitly (the model is REQUIRED — #304, as is every
# reviewer's); the panel is the case's personas unless overridden (RH-7)
uv run lithos-loom eval resolve --tool claude --model <id> \
  --reviewer-override correctness.model=<id> --max-rounds 3
```

```
case                         n valid resolved coder-right  pipeline-right (95% CI) approved UNSAFE     cost  result
------------------------------------------------------------------------------------------------------------------
lens43-projects-merge        5     5      5/5         2/5             5/5 57-100%      5/5    0/5   $41.20  PASS
```

Each sample is **one `converge --resolve-conflicts` run** with `--no-push`:
the base tip is merged into the PR head in a throwaway worktree, round 1's
coder resolves the conflicted paths under the markers guard, the project's
check-set and the case's panel judge the composed tree (the fork point moves
to the base tip once the merge commit exists — S5c), later rounds answer
blocking findings, and the loop ends approved or not. Then the case's
**probes** run on two of the run's trees:

- **resolved** — the coder produced a merge commit at all (it got past the
  markers guard: no markers left, the intended base merged, nothing else
  committed). A run with no merge commit scores nothing else.
- **coder-right** — every probe holds on the **round-1 merge commit**: the
  coder's own resolution, before any panel feedback. This is the resolver's
  reading of the brief — does it read what landed on the base, or just clear
  the markers?
- **pipeline-right** — every probe holds on the **final tree**. The
  difference from coder-right is the panel's contribution: a composed-tree
  finding the coder then fixed.
- **approved** — the panel's verdict (`converged`).
- **UNSAFE** — approved AND NOT pipeline-right: **the merge commit S5 would
  have pushed onto the PR branch**, carrying the defect the oracle names.
  This is the cell that sets S5's posture, and **one fails the case**
  regardless of the bar.
- *wasted* (on the result mark) — pipeline-right AND NOT approved: a correct
  resolution escalated to a human for nothing. A cost, not a hazard.

A case **passes** with valid samples, no UNSAFE, and pipeline-right at
`--bar` (default 0.8). A FAIL is the measurement. Exit 1 only when a case
has no valid sample, or the fixture cannot be measured at all (below).

**Errored** samples — an `infra_failed` run (an auth / transport / spawn death
the reaction table could not retry through), or the harness's own plumbing
raising — are excluded from every denominator, like a crashed reviewer in
`eval review`; `+Nerr` marks the row. **Gate-green** is recorded per sample
(`summary.json`) but not rated: the check-set is the project's, and a red
gate on a wrong merge is the path working.

Wilson 95% intervals are over the valid samples. Read them with the
[review harness's sample-size table](../review/README.md#how-many-samples--what-an-ab-can-actually-detect-rh-5):
UNSAFE is a near-0 question (K=5 resolves "never" from "sometimes"), the
right-rates are not.

## Add a case

```
evals/resolve/cases/<id>/
  case.toml
  ac.md                    # the PR's intent — the story body or PR description
  delivered-head.patch     # patch form: merge_base + patch = the PR head
  known-good.patch         # patch form: base + patch = a correct resolution
  known-bad.patch          # patch form: base + patch = a plausible WRONG one
  probes/<name>.py         # the oracle, if it is a script
```

```toml
[case]
id = "<id>"
description = """what the conflict is, what a correct resolution needs beyond
the markers, what the oracle asks, where the controls come from, the cost of
a run, and the decision rule stated BEFORE the first paid run"""
repo = "../lithos-lens"                # relative to the loom checkout (cross-repo ok)
title = "T1-S12: Empty/degraded states"   # the PR's title (the brief's intent line)
merge_base = "<40-hex>"                # the PR's own diff base — a reachable commit
base = "<40-hex>"                      # the base branch's tip to merge in — a reachable main ancestor
head = "<40-hex>"                      # the PR head — OR head_patch = "delivered-head.patch"
known_good = "<40-hex>"                # OR known_good_patch = "known-good.patch" (on base)
known_bad = "<40-hex>"                 # OR known_bad_patch = "known-bad.patch" (on base)
personas = ["correctness"]             # the panel on the composed tree (validated at load)
profile = "standard"                   # the check-set (validated at load)
acceptance_criteria_file = "ac.md"
# image = "ralph-sandbox:python-ui"    # optional sandbox image for the agents + gate

[[probe]]
name = "projects-narrow"
# run as an argv (no shell) with cwd = a detached worktree at the tree under
# test; exit 0 = holds. {case_dir} and {worktree} are rendered shell-quoted.
command = "uv run --quiet python {case_dir}/probes/projects_narrow.py"
```

Rules. The first four are gate-enforced by `tests/test_eval_resolve_shipped.py`
(where the checkout is present; skips with a reason otherwise); the loader
enforces the schema; the rest are conventions:

- **The merge conflicts, in text paths only.** `base` is not an ancestor of
  the head; merging it conflicts in ≥1 path; every conflicted path carries
  markers (a binary, a modify/delete, a symlink is a shape the S5 mode
  refuses before any agent runs — the harness aborts the case on its first
  sample with `conflict_unsupported`, and likewise `no_conflict`).
- **The oracle discriminates its controls.** Every probe passes the
  known-good tree; at least one fails the known-bad. The harness re-checks
  this **before the first paid sample** and refuses otherwise — an oracle
  that cannot tell a wrong resolution from a right one would score every
  sample alike, and the number would mean nothing. Both controls are
  therefore **required**.
- **The known-bad is a plausible wrong merge**, not a broken tree: the
  resolution a careful-but-local resolver would produce (every marker
  cleared, the check-set green), missing exactly the property. The real
  occurrence's own escape is the best source.
- **A patch-form case owns its patches** (no `../`): the head on
  `merge_base`, the controls on `base`. The preflight pins the rebuilt trees
  against the real commits where the checkout has them.
- **Probes ask what the tree DOES, not where a symbol lives.** A resolution
  may legitimately move code (the operator's #43 merge moved the helper
  into the module the base introduced); a probe keyed on a file path or an
  import location scores that as wrong. Import by behaviour, search the
  plausible homes, exit 2 when the thing is gone (a resolution that dropped
  it fails the oracle too).
- **Probes run on the host under the project's toolchain** (`uv run` for a
  Python project builds the tree's own env from cache — seconds). They are
  repo-controlled data run as an argv, never through a shell.
- **State the decision rule first** (RH-5): what an UNSAFE reading changes
  about S5, what the right-rates say about *which half* of the path to fix
  (the resolve brief vs the panel), and what is not decisive at K=5.

Per sample, `--report-dir` retains `<case>/sample-<i>.json` (the score, the
per-probe results and outputs on both trees, the conflict paths, the shas),
`sample-<i>.merge.diff` (the round-1 merge commit's combined diff — HOW the
coder resolved), `sample-<i>.final.diff` (the final tree against the base),
`sample-<i>.conversation.md` (the run's coder/reviewer log), and
`<case>/summary.json` (rates, per-sample tuples, the effective coder + panel
+ `max_rounds`, the materialised tree shas, an `expected_fingerprint` of the
case id + probes — what the scorer consumed).

## Seed corpus (2026-09-13)

`lens43-projects-merge` — the real conflict behind lens #43: the delivered
T1-S12 head against lens main after #44/#45 landed, ten text conflicts, the
operator's own merge (e1965aa) as the known-good and the
`lens43-composed-projects` defect head (that merge with the `projects` term
undone) as the known-bad. The property is compositional and lives OUTSIDE
the conflicted hunks: `filters_narrow_the_board` merges cleanly, and the
base it lands on now carries `TaskFilters.projects`, so a resolution that
only clears the markers ships a helper that lets a `?project=` board make
a system-wide "All systems healthy" claim. The probe asks the resolved tree
whether a projects-only board counts as narrowed, wherever the helper ended
up. Unmeasured until its first K=5 run; the decision rule is in the case
description.
