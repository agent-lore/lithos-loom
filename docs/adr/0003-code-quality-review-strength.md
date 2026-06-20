# ADR 0003 — Code quality & review strength: selectable Review Profiles + a multi-check deterministic gate

- **Status:** Proposed (drafted from the 2026-06-20 planning session; composes [#92] capability profiles and [#127] gate keys)
- **Date:** 2026-06-20
- **Deciders:** Dave Snowdon

> Tracking issue: **#128**. Quick wins already filed: **#129** (dogfood ruff `S`
> on loom itself), **#127** (per-project gate keys — the foundation this
> generalises). An advisory "state of the art in automated code review" report
> informed this design but was treated as advisory only.
>
> **Revised 2026-06-20 after review round 1 (PR #130):** added fail-closed profile
> resolution, required/optional/informational/not-applicable check states, a
> first-class deterministic-finding ledger, risk-based escalation (profiles are a
> *floor*, not a fixed level), auto-format-before-review, CI-as-authoritative-final-gate,
> explicit ecosystem applicability, and a broadened calibration metric set.
>
> **Revised again after review round 2:** separated **check execution-success
> from finding-severity blocking** (§4/§5); closed the `develop_test_gate`
> floor backdoor (§10); specified the **CI lifecycle contract** + check-run
> semantics (§9); pinned the **auto-format commit/review sequence** (§4); added a
> per-profile **`strength_rank`** total order (§2); made **critical-signal
> escalation non-suppressible** (§7); clarified **required-where-applicable** (§4).

## Context

`story-develop` is loom's implement→review→PR plugin. Its review machinery is
already strong: a `ReviewerSpec` panel with per-reviewer `tool` (claude/codex),
`model`, `effort`, `block_threshold`, `system_prompt` and `fallback_chain`; a
`FindingLedger` with monotonic IDs carried across rounds; stall/dispute guards;
and an **objective test gate** that re-runs the project's tests against a
`git archive` of each round commit in a throwaway hardened container.

But review *strength* is implicit and uniform, and that is the gap:

- The **default panel is a single `code-quality` reviewer**.
- The **deterministic gate runs exactly one command** (the test command). There
  is **no static analysis in the loop** — no lint, type-check, SAST, dependency
  audit, or coverage.
- The gate's result feeds **only the coder, and only on RED**. Reviewers never
  see it.
- There is **no way to dial review intensity** per project or per task.
- loom approves **locally** and opens a PR — GitHub **CI runs after push** and is
  never fed back, so a locally-green run can hand over a CI-red branch.

The operator goal: ensure code quality via a **heterogeneous panel of reviewers
combined with static deterministic tooling**, where the **strength is
selectable** — sometimes the full panel, sometimes not — and, where the *floor*
is selected but risk can push strength higher automatically.

This needs a decision now because the organising abstraction is load-bearing:
get it wrong and we re-plumb both the gate and the panel-config surface.

## Decision

### 1. The unit of selection is a **Review Profile** (a coherent bundle, additively overridable)

A Review Profile is a **named bundle** of:

1. **Panel** — which reviewer personas run, with engine / model / effort /
   `block_threshold` (reusing `ReviewerSpec`; a persona may name a [#92]
   capability profile for its skills/MCP).
2. **Check-set** — which deterministic checks the gate runs, each tagged with a
   **state** (see §4).
3. **Blocking policy** — which severities block; which checks block.

A profile binds panel and gate **together** so incoherent *weakenings* (a
"thorough" review with a tests-only gate) are not casually expressible. The
bundle is the **default, recommended path**, but a task may **additively
override** — add specific checks or reviewers — for genuine edge cases (a
docs-only task wanting link/spell checks but no LLM panel is just the gate-only
`minimal` profile; a security patch wanting strict SAST + only security and
correctness reviewers is a `security` profile). Overrides are **additive /
escalating only** (see §7); they cannot silently drop a floor's required checks.

### 2. Profile selection — precedence, and **fail closed on an explicit unknown**

```
per-task    task.metadata.develop_review_profile      ← sets the floor, per task
   ▼ overrides
per-project develop_review_profile  (context-doc metadata)
   ▼ overrides
host        [story_develop].default_review_profile     (loom TOML)
   ▼ overrides
built-in    "standard"
```

Distinguish two cases:

- **Unset** at a layer → inherit the layer below. Normal, silent.
- **Set but unknown** (a typo'd or undefined profile name) → **fail closed.** The
  run does **not** proceed at a *lower* strength than the operator asked for. By
  default it **halts before any agent runs**, surfaced as a **blocking**
  `[Friction]` ("profile `thorogh` is not defined") for the operator to fix.

This is a deliberate exception to loom's usual "friction-not-fail" norm:
silently substituting *any* other profile for a quality-dial typo defeats the
dial's purpose. For hosts that must never block, a config switch
(`unknown_profile = "strongest"`) falls back to the **strongest configured**
profile + friction — **never a weaker one**.

**`strength_rank` makes "strongest" unambiguous.** Each profile declares an
integer `strength_rank` (built-ins: `minimal` 10, `standard` 20, `thorough` 30;
operators slot custom profiles like `security` or `fast-ci` in between). It is
the total order used by `unknown_profile = "strongest"`, by escalation (§7, "a
higher profile" = higher rank), and by the floor/override rules (an additive
override may not drop below the selected rank's required set). Without it,
"strongest configured" is undefined once operators add profiles.

### 3. Three canonical profiles ship, each stating a **quality floor**

Each profile declares not just *which tools it tries to run* but a **quality-floor
guarantee** — the set of **required** checks and panel personas that MUST pass
for approval (see §4 states). "Tries to run" is not a guarantee; "required and
passed" is.

| Profile | Required panel | Required checks | Informational | For |
|---|---|---|---|---|
| `minimal` | — (gate-only) | format†, lint, test | — | mechanical / trivial / docs |
| **`standard`** *(default)* | correctness + security | format†, lint, typecheck, sast, test | — | normal feature work |
| `thorough` | correctness + security + architecture + test-quality + dep-hygiene | + dep-audit, coverage‡ | semgrep | risky / security-sensitive / large |

† `format` is required-as-clean but satisfied by auto-format (§4), so it never
blocks a round on whitespace. ‡ `coverage` in `thorough` is required-present but
its *threshold* is informational input to the test-quality reviewer, not a hard
percentage gate.

**`standard` is the default, not `thorough`** — the full panel is real money ×
wall-clock × rounds × containers, so maximal review is a deliberate escalation.

### 4. The gate is a **multi-check gate** with explicit check **states**

Generalise the gate harness (`git archive` → throwaway hardened container — keep
it) from one command into an **ordered set of named checks**, each
`{command, state, ecosystem, verdict, output_tail}`, run independently (no
shell-chaining — that would collapse per-check verdicts and blocking):

```
format → lint → typecheck → sast → dep-audit → test → coverage → semgrep
```

Each check carries a **state**, which is what makes a profile's quality floor
real rather than aspirational:

- **required** — must **execute successfully** (run to completion, tool present).
  **A required check whose tool is absent — or that errors/times out — fails at
  preflight and blocks the run** (it does NOT silently downgrade to
  informational). "Required" governs *execution*, not findings (see below). A
  required check is required **where applicable**: one that resolves to
  `not_applicable` for the repo's ecosystem (e.g. `test` in a docs-only repo with
  no test ecosystem) is satisfied and recorded N/A — it does **not** violate the
  floor. (Operators may also ship a `docs` profile.)
- **optional** — runs if its tool is present; absence is fine.
- **informational** — runs if present; its findings never block; surfaced to
  coder + reviewers.
- **not_applicable** — inapplicable for this repo's ecosystem; recorded N/A (not
  a silent pass).

**Execution success and finding-blocking are separate axes.** A check's
*execution outcome* (`ran` / `absent` / `errored` / `timed_out` / `n_a`) is
distinct from whether its *findings* block approval. A required check must
**execute** (`ran`); whether the findings it produces block is governed solely by
the severity mapping + blocking policy (§5), **not** by the tool's raw exit code.
Each check has an adapter that turns `(exit_code, output)` into
`(execution_outcome, findings[])` — so Bandit's "non-zero on any finding" or a
test runner's "non-zero on a failing test" becomes structured findings, and
*policy* decides blocking rather than the exit code deciding policy by accident.
(For `test`, a failing-test finding is blocking under the default policy; for
SAST, only the mapped high/critical findings block.)

**Auto-format sequence (exact, per round).** Immediately after the coder's
commit: (1) run the formatter in the sandbox; (2) if it changed anything, commit
the formatting as a **separate commit** on the round; (3) run the full check-set
on **that formatted tree**; (4) the reviewers review **that exact formatted
tree**. Formatting therefore always precedes the gate and the panel. loom
**never** formats after approval (a post-approval format would invalidate what
the reviewers signed off; if it ever must, it re-runs the gate + review). Because
formatting is applied deterministically up front, `format` is a required-but-
non-blocking check — it should always already be clean by the time it runs.

**Ecosystem applicability (no Python-biased default).** Each check declares the
ecosystem(s) it applies to; a profile resolves its check-set against the repo's
detected ecosystem(s). `standard` for a JS/Rust/Go repo means *that ecosystem's*
required checks (eslint/tsc, clippy, vet, …), not a degraded Python set wearing
the same name. A repo whose ecosystem has **no mapping for a required check**
**fails validation** rather than pretending `standard` means the same thing
everywhere.

**Default blocking policy:** block on **lint**, **typecheck**, **test**, and
**SAST high-severity**; **coverage**, **semgrep**, **format** are informational —
*except* a deterministic finding whose mapped severity is **critical/high-confidence
blocks regardless of its check's default tier** (a high-confidence critical
semgrep hit blocks; a low-confidence one informs).

### 5. Deterministic findings get a **first-class ledger**, not a hand-wave

Static-tool output joins the review as **deterministic findings** with their own
lifecycle, parallel to (not folded into) the reviewer `FindingLedger`:

- **Stable IDs**, namespaced by check: `gate/sast-001`, `gate/lint-014`.
- **Owner = the gate**, not a reviewer. The coder cannot mark one "fixed" by
  assertion.
- **Severity mapping per check** — an explicit table maps each tool's native
  levels onto loom's `minor|major|critical` (e.g. bandit HIGH→critical,
  MEDIUM→major; a ruff error→major; pip-audit CVE by CVSS). The mapping is part
  of the ADR's follow-up, reviewable and tunable. **Whether a mapped finding
  blocks is the severity policy's call (§4), independent of the tool's exit
  code** — the check adapter never lets a tool's "non-zero on any hit" decide
  approval by itself.
- **Closure only by re-running the check green** — a `gate/*` finding is "fixed"
  when the next round's gate no longer reports it, never by the coder's word.
- **No human-dispute arbitration; instead, suppression.** A subjective reviewer
  finding can be disputed → escalated. A deterministic finding that is a **false
  positive** is handled by a **reviewable suppression** (an inline `# noqa`/ignore
  or a checked-in baseline) — which is itself a diff the panel sees — not by the
  coder "disagreeing." This keeps the deterministic signal honest.

### 6. Deterministic tooling feeds the LLM reviewers, not just the coder

The gate runs **before** the panel each round; its aggregate (all checks +
deterministic findings) is injected into **both** the **coder** prompt
(generalise `_gate_note` to summarise all checks, green and red) and the
**reviewer** prompt (a new `reviewer_round.md` section) so e.g. security spends
its budget on what tools can't catch instead of re-deriving SAST output.

### 7. Profiles are a **floor**; risk signals **escalate** above it

The selected profile is the **minimum** strength, not a fixed level. loom MAY
**auto-escalate** above the floor (to a higher profile, or by additively adding
checks/reviewers) when **risk signals** fire in the change:

> touched auth/authz or security-sensitive paths · DB migrations · public-API /
> exported-surface changes · new or bumped dependencies · deleted or weakened
> tests · coverage drop · large diff · repeated RED gates across rounds · weak /
> missing acceptance criteria.

Escalation **never lowers** below the floor. Manual selection sets the floor.

`allow_escalation = false` is an opt-out for cost-sensitive cases, but it is
**bounded**, not a blanket kill-switch:

- It silences only **soft signals** (diff size, coverage drop, repeated RED).
- **Critical signals — auth/authz, secrets, DB migrations, new/bumped
  dependencies — are non-suppressible**: their escalation fires regardless of
  `allow_escalation`. A "force `minimal` on a touches-auth change" is therefore
  *not expressible*; the floor still rises for the auth signal.
- Any opt-out **requires a recorded reason**, emits a **stable auditable
  finding**, and is logged to calibration (§11) — so a habit of opting out is
  visible, not silent.

The **risk-signal detection engine is a follow-up phase**; this ADR fixes the
*principle* (floor + escalation + non-suppressible criticals) and the signal list
so the schema reserves room for it now.

### 8. Reviewer personas are one-dimension-each, with prompt discipline

Canonical personas, heterogeneous engines on purpose (different blind spots —
[#94]):

| Persona | Focus | Threshold | Engine |
|---|---|---|---|
| correctness | boundaries, off-by-one, races, error handling, idempotency; no style | major | claude |
| security | OWASP + CWE#, blast radius, secrets, injection, SSRF, IDOR, deser. | minor (strict) | claude (xhigh) |
| architecture | module boundaries per `AGENTS.md`, abstractions; sees `base..HEAD` | major | codex/sonnet |
| test-quality | edge cases, mocks that hide behaviour, determinism, AC coverage; fed the coverage tail | minor | codex |
| dependency-hygiene | new-dep justification, supply-chain reputation, pinning | minor | sonnet |

The base `reviewer_round.md` template gains **"stay strictly within your focus"**,
a project **severity-calibration table**, and a pre-injected `git diff --stat`.

### 9. CI is the **authoritative final deterministic gate**

The local sandbox gate is **necessary but not sufficient** — the sandbox lacks
CI's services/secrets/matrix, so local-green can still be CI-red. After PR open,
loom **consumes the PR's CI check-runs** as a deterministic gate. Local checks
catch most issues cheaply and early; CI is the final word.

**Check-run semantics (so "CI RED" is implemented consistently).** loom treats as
RED a failure of the repo's **branch-protection required checks** when configured,
else any failed check suite on the head SHA. `pending` is awaited up to a
timeout, then surfaced (never merged); `cancelled` / `skipped` / `neutral` are
**non-blocking**; a repo with **no CI** makes the local gate final (the CI gate is
`not_applicable`, not a block).

**Lifecycle contract (reconciled with delivery).** Today, on approval loom marks
`loom_delivered`, releases the claim, and opens the PR; the watcher owns
merge/close ([#87]). A delivered-but-CI-red PR is reconciled by **the same watcher
sweep**, following loom's existing marker pattern (cf. `develop_pr_merge_state`,
[#87]/[#69]):

- it posts a `gate/ci-*` deterministic finding on the task and **clears
  `loom_delivered`**, re-opening the task for development — **only while the PR is
  open and unmerged** (a concurrent human merge wins; the sweep no-ops);
- re-development pushes **to the same PR branch** (append commits), so there is
  one PR, not a fork;
- de-duped via a **CI-state marker scoped to the head SHA**, so a given red result
  re-triggers once, and a new push re-evaluates.

This keeps exactly one owner of the task at a time and prevents duplicate / raced
runs. The CI re-open consumes rounds against the same `max_rounds` / cost ceilings.

### 10. Composition with [#92] and [#127] — no double-building

- **[#92] capability profiles supply per-persona skills/MCP.** Review Profile =
  *who + how strict*; capability profile = *what each agent can do* (incl. a
  reviewer's codegraph MCP for cross-file context). Only the cross-file slice
  hard-depends on [#92].
- **[#127] gate keys are subsumed, not reworked, and cannot backdoor the floor** —
  flat `develop_test_command` / `develop_block_on_red` / `develop_test_gate`
  become **shorthand over the active profile's `test` check only** (its command /
  block-flag / on-off). Critically, **`develop_test_gate = false` disables only
  the `test` check — never the floor's required lint/type/SAST/format.** Disabling
  the *whole* gate is a floor-weakening that requires an explicit
  `allow_weaken_floor = true` and emits an **audited** `[Friction]` + deterministic
  finding — it is never a side effect of a convenience key. (Migration note: [#127]
  ships before the profile model, where `develop_test_gate` toggles the only gate
  — the §4 slice re-scopes it to the `test` check when profiles land.) One
  gate-config truth, with the flat keys as a thin convenience layer.

### 11. Calibration: outcomes, not just "merged"

"Merged as-is" is a weak quality signal (the human may have missed it). Record
per run (in `[DevelopResult]` / run-state) the profile, panel, findings-by-severity,
gate verdicts, disputes **and suppressions**, then correlate against a **basket of
outcome signals**: post-merge **CI failures**, **reverts**, **hotfix/defect
follow-up tasks**, **reopened issues**, **human edit rate** on the delivered branch,
**human review comments**, **accepted-vs-dismissed** reviewer findings, and the
deterministic **false-positive (suppression) rate**. Target success metrics:
revert rate, CI-failure rate, human-edit rate, FP rate, post-merge defect rate.
This is the evidence that lets a noisy persona be pruned or a threshold relaxed
deliberately — guarding the adoption failure mode (false positives train the
operator to dismiss the tool).

### 12. External PR-level tools are out of core

Greptile / CodeRabbit are **not** core to loom's in-sandbox model; the cross-file
gap is addressed in-system via [#92] reviewer MCP context. They remain an
optional, later, GitHub-side spike alongside the Copilot review loom already
requests — not a dependency of this design.

## Consequences

- **A real strength dial with a safe floor.** Per-task selection sets the floor;
  risk escalates above it (criticals non-suppressibly); explicit-unknown fails
  closed; required checks can't silently vanish; the legacy `develop_test_gate`
  key can't disable the floor. Typoing the dial, missing a tool, or flipping a
  convenience key cannot quietly weaken a run.
- **Tool exit codes don't set policy.** Separating execution-success from
  severity-blocking means a linter/SAST tool's "non-zero on any hit" convention
  can't silently turn every minor finding into a merge blocker; the severity map
  + policy decide, per check.
- **CI failures don't strand a branch.** The lifecycle contract re-opens a
  delivered-but-CI-red task on the same PR, with merge-race and duplicate-run
  guards reusing the existing watcher marker pattern.
- **One honest quality signal.** Static tooling and the panel reinforce each
  other and share a finding model — but deterministic findings have their *own*
  ledger (IDs, gate-ownership, tool-closure, suppression) rather than being
  bolted onto reviewer semantics they don't fit.
- **One gate-config truth.** The profile owns the check-set; [#127]'s keys are a
  convenience layer.
- **Cost is a lever ([#102]).** Profile + escalation has a direct cost
  consequence; surface estimated cost where the heterogeneous-agent cost measure
  allows.
- **Image + egress work.** Required check tools must exist in `ralph-sandbox`
  (coordinate [#116] cache); `pip-audit`/`semgrep` need egress — tie to the [#92]
  Phase-2 allowlist. Until egress lands, they are `informational`/`thorough`-only,
  and a profile that marks them **required** fails preflight on a no-egress host
  (honest, per §4) rather than pretending to have run them.
- **CI feedback closes the local↔remote gap** but adds a post-PR loop with its own
  latency and cost budget (a CI re-open consumes rounds) — bounded by the same
  `max_rounds`/cost ceilings.
- **Risk-escalation is deferred mechanism, decided principle.** The schema
  reserves the floor/escalation shape now; the detector ships later, so early
  versions are floor-only (manual) without a breaking change when escalation lands.

## Alternatives considered

- **Per-project `develop_reviewers` lists only (status quo).** No per-task dial,
  no gate↔panel binding. Rejected.
- **Chain SAST onto the test command** (`ruff && pyright && pytest`). Collapses
  per-check verdicts/blocking, muddies RED. Rejected for the structured check-set.
- **Probe-and-skip absent tools → informational** (the first draft). Lets a
  profile approve without the checks that define it. Rejected in favour of
  required/optional states with preflight failure on a missing *required* tool.
- **Fold deterministic findings into the reviewer ledger.** Rejected — they have
  different ownership, closure (tool re-run, not coder word), and dispute
  (suppression, not arbitration) semantics; conflating them corrupts both.
- **Fully unbundle panel and gate.** Rejected — reintroduces incoherent combos
  and config sprawl; additive overrides on coherent profiles cover the real cases.
- **Profile as a fixed level (no escalation).** Rejected — for "automated high
  quality," risk signals should be able to raise strength without a human
  remembering to; the floor model keeps the operator's dial while adding this.
- **Full panel as default; coverage as a hard 80% gate.** Rejected — cost, and
  coverage gates are brittle for agent PRs (informational input instead).
- **Calibrate on merge outcome alone.** Rejected — "merged" ≠ "good"; broadened to
  the revert/CI/edit/FP/defect basket.
- **Let each check's exit code decide approval.** Simplest, but conflates "the
  tool executed" with "its findings should block" — Bandit's non-zero-on-any-hit
  would silently override the severity policy. Rejected for the
  execution-outcome / finding-blocking split.
- **Keep `develop_test_gate` able to disable the whole gate.** Convenient
  backward-compat, but a backdoor around the required-check floor. Rejected — it
  scopes to the `test` check; whole-gate-off requires an explicit, audited
  floor-weaken.
- **External bots (Greptile/CodeRabbit) as core.** Rejected — out-of-sandbox,
  SaaS-coupled; the gap is closable in-system via [#92].

## Follow-up work (implementation slices to be filed from this ADR)

| Phase | Slice | Depends on |
|---|---|---|
| 2 — det. gate | generalise gate → ordered check-set with required/optional/informational/N-A states + preflight; per-check `(exit_code, output)` → `(execution_outcome, findings)` adapter; re-scope `develop_test_gate` to the `test` check | [#127] |
| | per-ecosystem check mappings + ecosystem-applicability validation | |
| | auto-format-before-review pass | |
| | bake ruff/bandit/pip-audit into `ralph-sandbox` + cache | [#116] |
| | deterministic-finding ledger (IDs, severity-map, tool-closure, suppression) | |
| | aggregate gate → coder note + reviewer-prompt injection + diff-stat | |
| 3 — panel | canonical personas + tightened `system_prompt`s; `reviewer_round.md` discipline | |
| | architecture/delta reviewer sees `base..HEAD` | |
| | reviewer codebase-context via MCP/skill | [#92] |
| 4 — the dial | review-profile resolution (precedence + fail-closed); ship the 3 profiles with quality floors | |
| | additive per-task overrides; `allow_escalation` opt-out | |
| | wire profile → panel + check-set in `DevelopConfig` | |
| | **risk-based auto-escalation** detector (signal list in §7) | |
| 5 — CI + calibration | consume PR CI check-runs (branch-protection-aware) as a `gate/ci-*` gate; lifecycle contract: re-open delivered-but-red on the same PR with merge-race + SHA-marker dedup | [#87] |
| | record review metadata; build the outcome-signal basket + success metrics | [#87] |

Each slice is an independently grabbable tracer-bullet issue, linked back to #128.

[#92]: https://github.com/agent-lore/lithos-loom/issues/92
[#94]: https://github.com/agent-lore/lithos-loom/issues/94
[#102]: https://github.com/agent-lore/lithos-loom/issues/102
[#116]: https://github.com/agent-lore/lithos-loom/issues/116
[#127]: https://github.com/agent-lore/lithos-loom/issues/127
[#128]: https://github.com/agent-lore/lithos-loom/issues/128
[#87]: https://github.com/agent-lore/lithos-loom/issues/87
