# ADR 0003 — Code quality & review strength: selectable Review Profiles + a multi-check deterministic gate

- **Status:** Proposed (drafted from the 2026-06-20 planning session; supersedes nothing, composes [#92] capability profiles and [#127] gate keys)
- **Date:** 2026-06-20
- **Deciders:** Dave Snowdon

> Tracking issue: **#128**. Quick wins already filed: **#129** (dogfood ruff `S`
> on loom itself), **#127** (per-project gate keys — the foundation this
> generalises). An advisory "state of the art in automated code review" report
> informed this design but was treated as advisory only; the divergences are
> recorded under *Alternatives considered*.

## Context

`story-develop` is loom's implement→review→PR plugin. Its review machinery is
already strong: a `ReviewerSpec` panel with per-reviewer `tool` (claude/codex),
`model`, `effort`, `block_threshold`, `system_prompt` and `fallback_chain`; a
`FindingLedger` with monotonic IDs carried across rounds; stall/dispute guards;
and an **objective test gate** that re-runs the project's tests against a
`git archive` of each round commit in a throwaway hardened container, so agents
cannot fudge the result.

But review *strength* is implicit and uniform, and that is the gap:

- The **default panel is a single `code-quality` reviewer** (`BUILTIN_REVIEWERS`).
  A heterogeneous panel exists only as an example config.
- The **deterministic gate runs exactly one command** (the test command —
  `make test`→`pytest`/etc., or an explicit override). There is **no static
  analysis anywhere in the loop**: no lint, type-check, SAST, dependency audit,
  or coverage.
- The gate's result feeds **only the coder, and only on RED** (`_gate_note`).
  The reviewers never see it.
- There is **no way to dial review intensity**. "Give this trivial task the
  light treatment; give that security-sensitive one the full panel" is not
  expressible per project, let alone per task. Strength is hand-edited by
  rewriting a project's `develop_reviewers` list.

The operator goal driving this ADR: ensure code quality via a **heterogeneous
panel of reviewers combined with static deterministic tooling**, where the
**strength is selectable** — sometimes the full panel, sometimes not.

This needs a decision now (not incremental bolt-ons) because the organising
abstraction is load-bearing: get it wrong and we re-plumb both the gate and the
panel-config surface. A future reader will reasonably ask "why not just keep
adding reviewers to `develop_reviewers` and chain a linter onto the test
command?" — answered below.

## Decision

### 1. The unit of selection is a **Review Profile**

A Review Profile is a **named bundle** of three things:

1. **Panel** — which reviewer personas run, with their engine / model / effort /
   `block_threshold` (reusing `ReviewerSpec`; a persona may also name a [#92]
   capability profile for its skills/MCP).
2. **Check-set** — which deterministic checks the gate runs, and which **block**
   vs merely **inform**.
3. **Blocking policy** — which finding severities block approval; whether a RED
   check blocks.

A profile binds the panel and the gate **together**, deliberately: a "thorough"
review with a tests-only gate, or a "minimal" review with a five-engine panel,
are incoherent combinations we do not want to be expressible by accident.

### 2. Profiles are selected by a precedence chain (mirrors `develop_image` / [#127])

```
per-task    task.metadata.develop_review_profile      ← the dial, per task
   ▼ overrides
per-project develop_review_profile  (context-doc metadata)
   ▼ overrides
host        [story_develop].default_review_profile     (loom TOML)
   ▼ overrides
built-in    "standard"
```

"Unset" at a layer inherits the layer below. An unknown profile name resolves to
an operator-actionable `[Friction]` and falls through — it never fails the run,
consistent with every other daemon-resolution path. The **per-task override is
the primary dial-down knob**: a single `task.metadata.develop_review_profile =
"minimal"` is how you say "not the full panel this time."

### 3. Three canonical profiles ship; operators extend

| Profile | Panel | Check-set | For |
|---|---|---|---|
| `minimal` | gate-only, or 1 correctness reviewer | format + lint + test | mechanical / trivial / docs |
| **`standard`** *(default)* | correctness + security (2, heterogeneous engines) | format + lint + type + SAST + test | normal feature work |
| `thorough` | correctness + security + architecture + test-quality + dependency-hygiene (5, mixed claude/codex) | + dep-audit + coverage + semgrep (informational) | risky / security-sensitive / large |

**`standard` is the default, not `thorough`.** The full panel is real money ×
wall-clock × rounds × containers; the dial exists *because* maximal review is
expensive, so escalation must be deliberate rather than ambient.

### 4. The gate becomes a **multi-check gate**, not a chained command

Generalise the existing gate harness (`git archive` → throwaway hardened
container — keep it; it is already the right shape) from a single command into an
**ordered set of named checks**, each `{command, blocking, tool-probe, verdict,
output_tail}`:

```
format → lint → typecheck → sast → dep-audit → test → coverage
```

Each check is probed for tool availability (as the test gate already probes),
runs independently, and yields its own verdict reusing the `GateResult` shape.
We do **not** shell-chain checks into one command (`ruff && pyright && pytest`):
chaining throws away per-check verdicts, per-check blocking, and a clean RED
signal, and muddies what we can tell the coder and reviewers.

**Default blocking policy:** block on **lint**, **typecheck**, **test** failures
and **SAST high-severity**; **coverage**, **semgrep**, and **format** are
**informational** (surfaced, never block). Format is informational so a whitespace
drift never burns a whole review round; coverage is informational because a hard
percentage gate is brittle for agent-authored PRs. A profile may override any
check's block flag.

Tooling, mostly zero-new-dependency via `uv run`/`npx`, the rest baked into
`ralph-sandbox`: ruff (with the `S` security family), `ruff format --check`,
`pyright`, `bandit -ll`, `pip-audit`, `coverage --cov-branch`, `semgrep
--config=auto`, with per-ecosystem analogues for non-Python repos.

### 5. Deterministic tooling feeds the LLM reviewers, not just the coder

The gate runs **before** the panel each round, and its aggregate result is
injected into **both** prompt surfaces:

- the **coder** prompt — generalise `_gate_note` to summarise *all* checks (green
  and red), replacing the RED-test-only note;
- the **reviewer** prompt — a new section in `reviewer_round.md` so the security
  reviewer *sees* the bandit/ruff-`S` output and spends its budget on what tools
  cannot catch, instead of re-deriving it.

Deterministic findings **participate in the existing severity/blocking model** —
a blocking SAST-high blocks approval exactly like a reviewer `major`. Static
tooling and the panel become one quality signal, not two disconnected ones.

### 6. Reviewer personas are one-dimension-each, with prompt discipline

Canonical personas, heterogeneous engines on purpose (different blind spots —
proven in the [#94] mixed-panel work):

| Persona | Focus | Threshold | Engine |
|---|---|---|---|
| correctness | boundaries, off-by-one, races, error handling, idempotency; no style | major | claude |
| security | OWASP + CWE#, blast radius, secrets, injection, SSRF, IDOR, deserialization | minor (strict) | claude (xhigh) |
| architecture | module boundaries per `AGENTS.md`, right abstractions; sees `base..HEAD`, not just `HEAD` | major | codex/sonnet |
| test-quality | edge cases, mocks that hide behaviour, determinism, AC coverage; fed the coverage tail | minor | codex |
| dependency-hygiene | new-dep justification, supply-chain reputation, pinning | minor | sonnet |

The base `reviewer_round.md` template gains: **"stay strictly within your focus
— do not comment outside it"**, a project **severity-calibration table**, and a
pre-injected `git diff --stat` for orientation. The literature is consistent
that single-dimension "check ONLY X" passes beat monolithic "review everything"
prompts on signal-to-noise, and that a high false-positive rate is what trains
operators to dismiss the tool.

### 7. Composition with [#92] and [#127] — no double-building

- **[#92] capability profiles supply per-persona skills/MCP.** Review Profile =
  *who is on the panel and how strict*; capability profile = *what each agent can
  do*. The security persona's OWASP skill, or a reviewer's codegraph MCP for
  cross-file context, comes from its [#92] profile. Only the reviewer
  cross-file-context slice hard-depends on [#92]; everything else here proceeds
  without it (reviewers run credentials-only, as today).
- **[#127] gate keys are subsumed, not reworked.** [#127] ships now with flat
  project-metadata keys; under this model they become **shorthand over the active
  profile's gate**: `develop_test_command` overrides the `test` check's command,
  `develop_block_on_red` sets the `test` check's block flag, `develop_test_gate =
  false` disables the whole gate. So [#127] is absorbed cleanly — there is exactly
  one gate-config truth (the profile), with the flat keys as a convenience layer
  on top.

### 8. Calibration: record review outcomes, correlate with merge

Record per run (in `[DevelopResult]` / run-state) the profile used, the panel,
findings-by-severity, gate verdicts, and disputes — to later correlate against
the **PR-merge outcome** the github-watcher already tracks ([#87]: merged-as-is /
edited / closed-unmerged). This is the evidence that lets a noisy persona be
pruned or a threshold relaxed deliberately, rather than by vibes. Without it,
review *strength* silently degrades into review *noise*.

### 9. External PR-level tools are out of core

Greptile (cross-file indexing) and CodeRabbit (convention-learning) are **not**
core to loom's in-sandbox model. The cross-file gap they fill is addressed
in-system by giving reviewers codegraph/lithos MCP via [#92]. They remain an
optional, later, GitHub-side spike alongside the Copilot review loom already
requests on PR open — explicitly not a dependency of this design.

## Consequences

- **A real strength dial.** Per-task `develop_review_profile` makes intensity a
  one-line choice; the default stays modest; risk escalates deliberately.
- **One quality signal.** Static tooling and the panel reinforce each other
  (deterministic-feeds-LLM) instead of running blind to one another, and share a
  single severity/blocking model.
- **One gate-config truth.** The Review Profile owns the check-set; [#127]'s keys
  become a convenience layer, so there is no competing second way to configure the
  gate.
- **Cost is now a lever, tied to [#102].** Profile selection has a direct cost
  consequence; the resolved profile should surface an estimated cost where the
  heterogeneous-agent cost measure ([#102]) allows.
- **Sandbox image + egress work.** The check tools must exist in `ralph-sandbox`
  (coordinate with the [#116] cache work), and `pip-audit` / `semgrep` need
  network egress — tie this to the [#92] Phase-2 egress allowlist; until then they
  are `thorough`-only and may be skipped when egress is unavailable (probe-and-skip,
  as the gate already does for absent tools).
- **Per-ecosystem mapping.** The canonical check-set is Python-shaped; non-Python
  repos need per-ecosystem command mappings (eslint/tsc, cargo clippy, go vet,
  …). The check-set schema must carry these, and a check whose tool is absent is
  skipped (informational), never a false RED.
- **Backward compatible.** With nothing declared, behaviour is the `standard`
  profile's gate over today's auto-detected test command plus a 2-reviewer panel
  — a stronger default than today's single reviewer, so this is a deliberate (and
  documented) default-strength increase, mitigated by the dial for projects that
  want less.
- **Risk — adoption.** If the SAST checks or extra reviewers produce noise, they
  train the operator to dismiss findings (the ICSE adoption failure mode). The
  one-dimension prompts, informational-by-default coverage/semgrep, and the
  calibration loop (decision 8) are the mitigations.

## Alternatives considered

- **Keep per-project `develop_reviewers` lists only (status quo).** No per-task
  dial, and nothing binds the deterministic gate to the panel. Rejected: the
  thing the operator wants to vary — intensity, per task — has no home, and the
  gate stays tests-only.
- **Chain SAST onto the test command** (the advisory report's first suggestion:
  `ruff check && ruff format --check && pytest`). Cheapest to wire, but collapses
  distinct checks into one verdict, loses per-check blocking, and muddies the RED
  signal the coder/reviewer prompts depend on. Rejected in favour of a structured
  check-set.
- **Full heterogeneous panel as the default** (the report's recommendation).
  Rejected: the dial exists precisely because the full panel is expensive;
  `standard` (2 reviewers) is the default and `thorough` (5) is opt-in.
- **Coverage as a hard gate** (`--cov-fail-under=80`). Rejected as a default —
  too brittle for agent-authored PRs; coverage is informational input to the
  test-quality reviewer instead.
- **External bots (Greptile/CodeRabbit) as core.** Rejected for the core design:
  out-of-sandbox, SaaS-coupled, and the cross-file gap is closable in-system via
  [#92] reviewer MCP context. Kept only as an optional PR-side complement.

## Follow-up work (implementation slices to be filed from this ADR)

| Phase | Slice | Depends on |
|---|---|---|
| 2 — det. gate | generalise gate → ordered check-set (per-check verdict + block) | [#127] |
| | bake ruff/bandit/pip-audit into `ralph-sandbox` + cache | [#116] |
| | aggregate gate → coder note + reviewer-prompt injection + diff-stat | |
| | deterministic findings participate in approval/blocking per profile | |
| 3 — panel | canonical personas + tightened `system_prompt`s as reusable reviewers | |
| | `reviewer_round.md`: "check ONLY X" + severity table + gate summary | |
| | architecture/delta reviewer sees `base..HEAD` | |
| | reviewer codebase-context via MCP/skill | [#92] |
| 4 — the dial | review-profile resolution (host→project→task); ship the 3 profiles | |
| | wire profile → panel + check-set in `DevelopConfig` | |
| 5 — calibration | record review metadata in `[DevelopResult]`/run-state | |
| | reviewer-effectiveness rollup vs merge outcome | [#87] |

Each slice is an independently grabbable tracer-bullet issue, linked back to #128.

[#92]: https://github.com/agent-lore/lithos-loom/issues/92
[#94]: https://github.com/agent-lore/lithos-loom/issues/94
[#102]: https://github.com/agent-lore/lithos-loom/issues/102
[#116]: https://github.com/agent-lore/lithos-loom/issues/116
[#127]: https://github.com/agent-lore/lithos-loom/issues/127
[#128]: https://github.com/agent-lore/lithos-loom/issues/128
[#87]: https://github.com/agent-lore/lithos-loom/issues/87
