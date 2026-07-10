# Feature: Code-quality review strength — Review Profiles + multi-check deterministic gate

> Distilled from ADR-0003 (Proposed). Source: 0003-code-quality-review-strength.md — read it for full rationale.

## Decisions
| Decision | Reason | Rejected Alternative |
|----------|--------|----------------------|
| Unit of selection is a **Review Profile**: a named bundle of panel + check-set + blocking policy, binding panel and gate together; additively overridable | Stops incoherent *weakenings* (a "thorough" review with a tests-only gate) being casually expressible | Fully unbundle panel + gate (config sprawl, incoherent combos); status-quo per-project reviewer lists (no per-task dial) |
| Resolution precedence task > project > host > built-in `standard`; unset inherits silently, **set-but-unknown fails closed** (halt before any agent runs) | Silently substituting another profile for a typo'd quality dial defeats the dial's purpose | Silent substitution of any other profile on unknown |
| Each profile declares integer `strength_rank` with a load-time **monotonicity invariant** (higher rank ⊇ lower required checks + personas) | Rank that doesn't track strictness lets a high-ranked profile that drops SAST be picked as "strongest" | `strength_rank` as a bare ordering label |
| Ship 3 profiles `minimal`/`standard`/`thorough`, each stating a required quality floor; **`standard` is the default** | Full panel is real money × wall-clock × rounds × containers; maximal review is a deliberate escalation, not the default | `thorough`/full panel as default; coverage as a hard 80% gate |
| **Multi-check gate**: ordered named checks (format→lint→typecheck→sast→dep-audit→test→coverage→semgrep), each with a state (required/optional/informational/N-A) | Makes a profile's floor real not aspirational; the old gate ran one command with no static analysis in the loop | Chain SAST onto the test command (collapses per-check verdicts); probe-and-skip absent tools → informational (approves without the defining checks) |
| Separate **execution-success from finding-blocking**: adapter maps (exit_code, output) → (execution_outcome, findings); severity policy decides blocking | A tool's "non-zero on any hit" convention must not silently turn every minor finding into a merge blocker | Let each check's exit code decide approval |
| **Auto-format before review**: format after coder commit as a *separate* commit, then gate + panel review that exact tree; never format post-approval | Post-approval formatting invalidates what reviewers signed off; makes `format` required-but-non-blocking | (none recorded) |
| Deterministic findings get a **first-class ledger** (gate-owned, namespaced IDs, closure only by re-running green, reviewable suppression — not dispute) | Deterministic findings have different ownership/closure/dispute semantics than reviewer findings | Fold deterministic findings into the reviewer `FindingLedger` |
| Gate runs **before** the panel; its aggregate feeds **both** coder and reviewer prompts | So e.g. security spends budget on what tools can't catch instead of re-deriving SAST output | Feed the gate result to the coder only, on RED only (status quo) |
| Profiles are a **floor**; risk signals **auto-escalate** above it, never below; critical signals non-suppressible | Automated high quality needs risk to raise strength without a human remembering to | Profile as a fixed level (no escalation) |
| Canonical **one-dimension reviewer personas** on heterogeneous engines (correctness, security, architecture, test-quality, dep-hygiene) | Different engines have different blind spots | (none recorded — implicit single uniform `code-quality` reviewer) |
| **CI is the authoritative final gate**: consume the PR's check-runs; required set = branch-protection → declared contexts → else N-A + friction | Sandbox lacks CI's services/secrets/matrix, so a local-green run can hand over a CI-red branch | "Block on any failed CI check suite" when no branch protection (catches flaky/experimental, drives churn) |
| [#127] flat gate keys **subsumed** as shorthand over the active profile's `test` check; `develop_block_on_red` removed; whole-gate-off needs audited `allow_weaken_floor` | A convenience key must not backdoor the required-check floor | Keep `develop_test_gate` able to disable the whole gate |
| Calibrate on an **outcome basket** (reverts, CI failures, hotfixes, human-edit rate, FP/suppression rate), recorded per run | "Merged as-is" ≠ "good" — the human may have missed it | Calibrate on merge outcome alone |

## Constraints
- N/A applicability is **declared**, not inferred from absence; "expected-but-absent" (no tests in a code repo, tests deleted by the change) is a **blocking** finding, never auto-N/A.
- Checks resolve against the repo's **detected ecosystem(s)**; a required check with no ecosystem mapping **fails validation** (no Python-biased default).
- A **required** check whose tool is absent / errors / times out **fails preflight and blocks** — never silently downgraded to informational.
- Required tools must exist in `ralph-sandbox`; `pip-audit` / `semgrep` need egress — until egress lands they are informational / `thorough`-only.
- Overrides are **additive / escalating only**; `allow_weaken_floor` drops only *local* deterministic checks — never CI, critical-signal escalation, or the required panel.
- Deterministic-finding closure is only by re-running the check green; suppressions are reviewable diffs the panel can block.

## Open Questions
- [ ] Risk-signal **detection engine** — principle + signal list fixed (§7); the detector is a later phase, profiles floor-only/manual until then.
- [ ] **CI autonomous re-develop loop** (clear `loom_delivered`, same-PR-branch push, cumulative per-PR budget → human escalation) — reserved-shape; MVP ships the read+surface half only.
- [ ] **Calibration outcome-correlation** (signal basket + success-metric rollup) — reserved; MVP records run metadata only.
- [ ] Per-check **severity-mapping table** (tool levels → minor/major/critical) — ADR follow-up, reviewable/tunable.

## State
- [ ] ADR status: **Proposed** (3 review rounds + pre-implementation self-review)
- [x] #131 multi-check gate harness + `CheckState`/execution-outcome axes + `test`-check re-scope
- [x] #133 ecosystem detection + canonical-checks catalog + resolver (declared-N/A vs expected-but-absent)
- [x] #134 auto-format-before-review (isolated, network-none, clean-exit-only)
- [x] #132 deterministic-finding ledger (`GateFinding`/`GateLedger`) + ruff/bandit/pip-audit adapters
- [x] #139 profile resolution (fail-closed precedence) + `strength_rank` monotonicity validation
- [ ] #140 (in progress): profile→check-set live; profile→panel + required-floor-blocking partial; overrides, `allow_escalation`, coverage `--fail-under` remaining
- [ ] Phase 3 panel personas; Phase 5 CI read (MVP) / autonomous (later); calibration rollup
