# ADR 0010 — Aggregate repo-parity gate check (`make check`)

- **Status:** Accepted
- **Date:** 2026-08-05
- **Deciders:** Dave Snowdon

> Extends [ADR 0003](0003-code-quality-review-strength.md) §4 (the deterministic
> check-set). Third and final slice of the #273 check-set-repo-parity arc, after the
> per-check command override (slice 1) and the per-check 3-state (slice 2).

## Context

The deterministic gate runs a **structured, per-ecosystem check-set** (format / lint /
typecheck / test / sast / dep-audit / coverage / semgrep) selected by the Review
Profile. It is deliberately *not* a mirror of a repo's CI: it knows a fixed catalog of
canonical checks per known ecosystem (Python / Node / Rust / Go).

Two gaps surfaced repeatedly in the converge soaks:

1. **Gate-green ≠ CI-green.** A repo whose CI enforces checks *beyond* the catalog —
   diagram/codegen freshness, schema drift, docs lint, license checks — can converge
   green and still fail CI. On the kc-agent #29 soak, converge pushed a gate-green tree
   that CI then failed on a "diagram drift" job the check-set has no notion of.
2. **Unmodeled ecosystems.** Detection knows only Python / Node / Rust / Go. A C/C++
   (CMake) repo detects as *nothing*, so every structured check is N/A and the gate
   collapses to whatever `test` command is provided — there is no meaningful floor.

## Decision

Add an **optional aggregate repo-parity check**. When the operator sets
`parity_command` (project-context `develop_parity_command` metadata, per-task override,
or `--parity-command` on `develop` / `develop review` / `develop converge`), the gate
appends a `repo-parity` check that runs that command — the repo's own aggregate
verification, e.g. `make check` — **verbatim** (trusted as-is, raw exit, no adapter),
**required**, at the **candidate stage** (once on the approval candidate, not every
round — it is the expensive belt to the structured checks' braces). It runs regardless
of detected ecosystems, so for an unmodeled ecosystem it is the **primary** gate.

This is the **hybrid** the arc converged on: keep the structured per-check gate (finding
ledger, fast/candidate staging, the `--profile` strength dial, per-check override +
3-state) where the catalog has coverage, and add one repo-owned aggregate check that
guarantees the pushed tree passes whatever the repo enforces beyond it. The repo-side
contract reduces to a single, easy-to-state requirement: **`parity_command` must be a
superset of what CI runs.**

## Alternatives considered

- **Collapse the whole gate to one `make check` (ADR 0003 §4 "Option B").** Rejected: it
  kills the strength dial (standard vs thorough would be identical at the gate), flattens
  the per-check finding ledger the fixer relies on into one opaque exit code, and loses
  fast/candidate staging. The structured gate carries real value; the aggregate check
  supplements it, it does not replace it.
- **Auto-probe / auto-run a `make check` target when present.** Rejected for v1:
  silently running an arbitrary, possibly slow or destructive aggregate command on every
  repo that happens to have a `check:` target is a surprising, hard-to-audit behaviour
  change. Parity is **explicit opt-in**. A Makefile-target probe is a possible future
  convenience, gated behind its own decision.
- **Model every compiled ecosystem in the catalog (add C/C++).** Orthogonal and larger
  (tracked separately, #157). The parity check makes the gate meaningful for an
  unmodeled ecosystem *without* first modelling it — graceful degradation.

## Consequences

- `repo-parity` reads its **raw exit code** (like a per-check command override, #278) —
  it opts out of the structured finding ledger; its raw output tail feeds the coder.
- It is **candidate-stage**, so it does not slow tight per-round iteration; it gates the
  approval candidate and, on failure, holds approval so a follow-up round surfaces it.
- The delivery regression gate keys only on `test`, so parity does not run at delivery
  (it is a pre-approval candidate check).
- Parity is not part of the per-check 3-state (`check_states`) — to disable it, unset
  `parity_command`; when set it is always a required candidate check.
- A failing raw-exit parity check leaves no ledger finding, so the epilogue names it
  explicitly in the run message + the `[DevelopResult]` finding + the standalone summary
  (via `DevelopResult.blocking_checks`) — otherwise a final-round parity-only failure
  (reviewers pass, parity red) would be invisible in the operator-visible outcome and
  survive only as the round's `output_repo-parity.txt` artifact.

## Precedence and disabling

`parity_command` resolves with the standard project → task → route precedence shared by
every scalar `develop_*` field (`develop_test_command`, `develop_image`, …): task
metadata beats project metadata beats the route-level `--parity-command` flag. A layer
may **replace** a lower layer's command but **cannot disable** it — metadata `None` /
absent means "inherit", and a blank string is rejected, so there is deliberately no
"set to null to suppress the route default" escape. This is a uniform property of the
scalar resolver, not a parity-specific choice; special-casing parity to honour an
explicit-null disable would be a surprising asymmetry across the `develop_*` surface.
The consequence an operator must know: **a route-level `--parity-command` is a per-route
policy floor — every project that route serves runs it.** If a repo served by that route
has no suitable aggregate target, scope the parity flag to a narrower route rather than
expecting a project to opt out. (A cross-cutting presence-aware "disable a lower default"
mechanism, if ever wanted, belongs to all scalar fields at once — out of scope here.)
