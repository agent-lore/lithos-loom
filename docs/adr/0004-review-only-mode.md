# ADR 0004 — Review-only mode: run the panel + gate on an existing change

- **Status:** Accepted
- **Date:** 2026-06-23
- **Deciders:** Dave Snowdon

> Tracking issue: **#154**. Execution primitive for the review-correctness eval
> harness (**#183**). Builds on the canonical personas (#137), Review Profiles
> (#139 / [ADR 0003](0003-code-quality-review-strength.md)), and the deterministic
> gate (#140).

## Context

The reviewer panel (canonical personas + the deterministic check-set / gate) is
reachable only via the full `story-develop` implement→review→fix→PR loop. There
is clear value in running **just the panel** against a change that already
exists — a PR loom or Claude authored outside story-develop, an external
contributor's PR, or the operator's own branch — and #183 (measuring review
correctness) *needs* "run the panel on an existing change, return structured
findings" as a callable primitive.

The hard part is not the panel (it is shipped) but the **inversion**: `develop()`
is wired to *produce* a change — `worktree.create()` branches fresh off a base
and the coder commits onto it, with `base = HEAD-at-creation`. Review-only must
*consume* a change: materialise a worktree **at the change's head** and resolve
its base separately. Everything downstream of the worktree (prompt assembly,
reviewer turns, finding ledger, gate) is reusable as-is.

## Decision

A `lithos-loom develop review <pr|range|branch>` CLI command wrapping a reusable
`review_change(config, change) -> ReviewReport` function. The function is #183's
in-process primitive; the CLI is the operator surface.

1. **One panel implementation.** `develop()`'s inline reviewer-panel loop was
   first extracted (PR #184) into a shared `run_panel_round(...)` that both
   `develop()` (every round) and `review_change()` (once, round 1) call. We do
   **not** copy the loop — a prompt / severity / lifecycle fix must never land in
   one review path and silently miss the other. The deterministic gate
   (`build_check_set` / `_run_check_set`) and the per-check block decision
   (`check_result_blocks`, factored out of `gate_floor_blocks`) are likewise
   shared verbatim.

2. **Worktree at head, detached.** A new `worktree.create_at(repo, ref, name)`
   does `git worktree add --detach <ref>` — HEAD positioned *at* the change, no
   branch created (reviewing a ref leaves no stray branch). The gate runs on the
   head sha via the existing `export_tree`.

3. **Input forms (all three in the first slice).** A GitHub PR (`#142` / `142` /
   PR URL, resolved via `gh pr view` + a `pull/N/head` fetch so fork PRs work);
   an explicit `base..head` range; a local branch / ref (base = merge-base with
   `main`, override with `--base`). Subprocess `git` / `gh` calls sit behind thin
   wrappers so resolution is unit-testable without a network round-trip.

4. **Acceptance criteria** (the reviewer's brief) precedence: `--ac-file` >
   `--ac` > the **PR body** (for PR input). A bare range / branch with **no** AC
   is rejected loudly — a reviewer with no criteria is near-useless. There is no
   coder, so the prompt's `{coder_summary}` slot says so plainly rather than
   fabricating one.

5. **Output: a local report only (first slice).** Structured **JSON** (`--json`,
   the stable contract #183 consumes) plus a human **markdown** summary to
   stdout. The exit code is non-zero when the review is **blocking** — the *same*
   floor `develop()` applies (any reviewer finding at/above threshold, an
   incomplete panel, or a required gate check blocking). Pure review: no coder
   fix pass.

## Consequences

- #183 builds directly on `review_change`: the eval harness is review-only mode +
  expected-findings scoring.
- The review-only worktree is **read-only** to reviewers (same RO mount as the
  develop loop); for an external/untrusted PR the only code execution is the
  deterministic gate, inside the existing hardened sandbox container.
- Host-only (needs `docker` + `claude`/`codex` + `gh`); never part of the
  hermetic `make check`.

## Deferred (own follow-ups)

- **Other output destinations:** post findings to a Lithos task finding; post
  inline + summary review comments to the PR via `gh`.
- **Optional single coder-fix pass** (pure review is the default).
- **#141 fold-in:** merge a PR's existing CI check-runs into the deterministic
  layer of the report.
