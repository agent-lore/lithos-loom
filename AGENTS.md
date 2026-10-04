# AGENTS.md — lithos-loom

Workflow orchestration daemon for [Lithos](https://github.com/agent-lore/lithos) tasks. Three supervisor children, each with its own in-process `EventBus` and no inter-child IPC (Lithos is the bus between them): the **route-runner** (dispatches subprocess plugins for tasks matching TOML routes; `story-develop` is the one implement→review→PR path, `prd-decompose` is a stub), the **obsidian-sync** bridge (projects tasks into an Obsidian-Tasks inbox and pushes edits back), and the **github-watcher** (mirrors issues both ways and maintains delivered PRs behind their `pr` gates).

[`docs/SPECIFICATION.md`](docs/SPECIFICATION.md) is the operator and integrator reference. Code and tests are authoritative: when the spec lags reality, fix the spec in the same change.

## Commands

- `make check` — `ruff check` + `ruff format --check` + `pyright` + `pytest`. Mandatory before any PR; CI runs the same. (The B008 ignore for `main.py` and `cli/**` is intentional; `_optional_path` is overloaded on purpose.)
- `make diagrams` — regenerates `docs/generated/` (it is `pytest tests/guardrail/ -q`). `make test` rewrites it too, so commit the result after any test run; the CI job `diagrams` fails on drift.
- `make gate-exec` — executes every Python check-catalog command for real (`tests/test_check_catalog_exec.py`): the real gate path in the sandbox image, against a generated clean fixture (every check must run and pass) and a defective one (every check must block), uv-managed and bare. Host only: needs docker, the `ralph-sandbox` image (`LOOM_GATE_EXEC_IMAGE` overrides) and the network; a few minutes. **Not part of `make check` or CI** — see the gate-definitions rule below.
- `uv run lithos-loom run` — the daemon runs on the host, not in docker (README explains why). Restart with `lithos-loom drain` first so no in-flight run is killed; a plain SIGTERM is safe but costs the run.
- Config: `LITHOS_LOOM_ENVIRONMENT=dev` selects `config.dev.toml` from `./` then `$XDG_CONFIG_HOME/lithos-loom/`; `LITHOS_LOOM_CONFIG=/abs/path.toml` beats everything; `.env` in the CWD is loaded.

## Rules

- **Tests are hermetic — no live Lithos.** `make check` and the in-sandbox story-develop gate must pass without a server: stub the client (see `_FakeClient` in `tests/test_story_develop_daemon.py`); `conftest.py` clears `LITHOS_*` per test. A test that truly needs a live round-trip guards on `LITHOS_URL` and runs on the host or CI, never in the sandbox. Ephemeral per-run Lithos is deferred ([#148](https://github.com/agent-lore/lithos-loom/issues/148)).
- **Gate-definition changes run `make gate-exec` — nothing else will.** A diff touching `plugins/story_develop/` `check_catalog.py`, `profiles.py`, `gate_adapters.py`, `gate_findings.py`, `check_runner.py`, `check_set.py`, `test_gate.py`, or `runner/detection.py` must run `make gate-exec` on the host and paste its result into the PR. Why: a story-develop run gates with the daemon's *installed* catalog, not the candidate's edited one, and `make check` only asserts command strings — so a check command that cannot spawn, or that checks nothing, ships green (#173: `uv run coverage` could not spawn). The test skips itself unless `LOOM_GATE_EXEC=1`, so neither `make check`, CI nor the in-sandbox gate ever runs it; a run that cannot use the host's docker (a story-develop coder) says so in the PR and the operator runs it before merge.
- **Paired updates.** Plugin contract → `docs/result-schema.json` and `tests/test_plugin_runner.py`. Config schema → `examples/lithos-loom.toml` and `tests/test_config.py`. New plugin → `src/lithos_loom/plugins/<name>/__main__.py` plus an example route stanza. Any operator-visible change (CLI flag, projection rule, event name, finding prefix) → `docs/SPECIFICATION.md` in the same diff.
- **Removing an operator-visible concept:** sweep the whole repo for its name — `AGENTS.md`, `README.md`, `examples/`, `docs/SPECIFICATION.md` must stop describing it; archived PRDs and ADRs keep their history.
- **Finding prefixes** are machine-parseable breadcrumbs; the catalogue is SPEC §8. Add a fresh prefix rather than overloading an existing one (operators grep by prefix) and add its row to §8.
- **Task dependencies are edges, never metadata.** Lithos rejects `metadata.depends_on` / `blocked_on` (`invalid_metadata_key`) and `parallelizable` is gone; readiness is `lithos_task_ready`'s answer, not computed locally. Do not reintroduce any of them.
- **Gates are the guards** (SPEC §2.2, [ADR 0011](docs/adr/0011-pr-maintenance-invariants.md)). A delivered story sits behind a `pr` gate; a run that ends without delivering raises a loom `human` gate. There is no Loom-private "delivered" marker — do not add one. Never cancel a gate: complete it to re-dispatch, cancel the story to abandon. Read a story's gates from its `waits_on_gate` edges; `*_gate_id` metadata is provenance only.
- **Writes are atomic.** `result.json` and the per-round `state.json` use temp + fsync + rename (a partial file must never be observable). Vault writers use `.<filename>.tmp.<rand>` + `os.replace`; the dot prefix keeps Obsidian Sync from publishing temp files.
- **Project repos stay clean.** Loom config is host-local TOML; other projects' `AGENTS.md` / `CLAUDE.md` carry no Lithos or Loom references (ecosystem repos excepted).
- **Architecture guardrails.** `docs/architecture.toml` is the source of truth for components, tiers and metric limits; a new module, component or model must be mapped there or the orphan/completeness tests fail. When meeting a metric limit would worsen the design, keep the better design and raise the limit in the toml, explaining the tradeoff in the PR. Import direction Entrypoints → Core → Foundation is enforced by import-linter (`pyproject.toml [tool.importlinter]`); `tests/guardrail/test_run_outcome_leaf.py` pins `run_outcome` as a leaf. **Before editing anything under `tests/guardrail/`, read `tests/guardrail/AGENTS.md`** — the generators' contracts are not deducible from the code.
- **Vocabulary and decisions.** Use the terms in `CONTEXT.md`; check `docs/adr/` before working in an area and flag a contradiction rather than silently overriding it (`docs/agents/domain.md`).
- **Issues** live at `agent-lore/lithos-loom`; use `gh` (`docs/agents/issue-tracker.md`). Triage labels: `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix` (`docs/agents/triage-labels.md`).

## Non-obvious facts

- Lithos prerequisites: `task.metadata`, `lithos_write(id=…, expected_version=…)`, `note.*` SSE events, `lithos_task_create(metadata=…)`. `lithos-loom doctor` probes for them.
- Plugins are invoked as `<command> --task-json <p> --work-dir <p> --result-file <p>`; the runner reads only `status` from `result.json` (other fields are schema-validated but not applied). Validate plugin output against `docs/result-schema.json`.
- Serial admission caps a project's open delivered PRs (`max_open_delivered_prs`, backstop `max_open_delivered_prs_total`) and releases held stories by priority ([ADR 0012](docs/adr/0012-admission-release-order.md)); details in SPEC §2.2.
- Coder and reviewers are heterogeneous: each agent's `tool` is `claude` or `codex`, so mixed panels and engine-switching fallback chains both work (SPEC §5.5).
- Projection and note-push are idempotent by content hash; `note-push` hashes the body only, so frontmatter-only edits are absorbed (SPEC §7).
- Where the long stories live: dispatch, gates, escalation and resume — SPEC §2.2 / §2.3 / §5.5; the PR-maintenance sweep (landability, external reviews, re-gate, conflict resolution, reconciliation state) — SPEC §2.2 and [ADR 0011](docs/adr/0011-pr-maintenance-invariants.md).

## Where things are

| Path | What |
| --- | --- |
| `docs/SPECIFICATION.md` | Architecture, configuration (§3.1 full TOML), CLI (§4), plugin contract (§5), projection (§7), finding prefixes (§8), errors (§9) |
| `docs/result-schema.json` | Versioned JSON Schema for plugin `result.json` |
| `docs/cli/*.md` | One reference per `lithos-loom develop` / `project` subcommand |
| `docs/adr/` | Decisions; `CONTEXT.md` is the glossary |
| `docs/prd/` | Active PRDs: `review-convergence.md`, `prd-to-graph.md`, `unattended-duration.md`; `archive/` holds shipped ones as history |
| `evals/{review,triage,resolve}/README.md` | `lithos-loom eval …` benchmarks: on-demand, host-only, not part of `make check` |
| `docs/macros/README.md` | Templater macro install and behaviour |
| `tests/guardrail/AGENTS.md` | Generator contracts for `docs/generated/` — read before editing that directory |
