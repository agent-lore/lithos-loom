# AGENTS.md — lithos-loom

## What this is

A workflow orchestration daemon that turns [Lithos](https://github.com/agent-lore/lithos) tasks into executable work. The daemon polls Lithos for open tasks, matches them by tag against TOML-configured routes, claims them collision-safely, and dispatches **subprocess plugins** that produce artifacts back into Lithos.

Bundled MVP plugins:

- `prd-decompose` — decompose a Pocock-shaped PRD doc into per-story Lithos docs and ordered story tasks; create the per-PRD integration branch.
- `story-implement` — per-task worktree off the integration branch, run Claude in delegated mode against PRD + story brief + project AGENTS.md, open a GitHub PR.
- `story-review-human` — poll `gh pr view`; on merge, complete the task and unblock the next dependent story.

This package **replaces** [Ralph++](https://github.com/snarktank/ralph) as the user's coding orchestration approach; it does not extend it. Useful Ralph++ pieces (worktree creation, agent subprocess runner with stream-json, commit detection) are salvaged into `src/lithos_loom/runner/`.

## Non-obvious things to know

- **Loom runs on the host, not in docker.** Lithos and Influx are services (stable protocols, no host coupling) and run in docker; Loom is an orchestrator with deep host integration (worktrees, `claude`/`codex`/`gh` CLI auth in `~/`, plugin subprocesses) so containerizing it would just bind-mount most of `~/`. Run via `uv run lithos-loom run` in tmux/foreground; systemd `--user` unit is a deferred polish item.
- **Per-environment configs.** `LITHOS_LOOM_ENVIRONMENT=dev` selects `config.dev.toml` from `./` and `$XDG_CONFIG_HOME/lithos-loom/`. Explicit `LITHOS_LOOM_CONFIG=/abs/path.toml` beats everything. `python-dotenv` loads `.env` from CWD.
- **Plugin contract = subprocess + atomic `result.json`.** Plugins are invoked as `<command> --task-json <p> --work-dir <p> --result-file <p>`. Schema is checked in at [`docs/result-schema.json`](docs/result-schema.json); validate plugin output against it. Atomic write uses temp + fsync + rename — partial files must never be observable.
- **Lithos `task.metadata` is a hard prerequisite** (`agent-lore/lithos#215`). `lithos-loom doctor` probes for it on first run.
- **Task dependencies live in `task.metadata.depends_on` (not Lithos edges).** Lithos's `edges.db` is doc-only; tasks are SQLite rows with no edge surface. Default ordering produced by `prd-decompose` is strict-sequential (each task lists the previous task ID); `metadata.parallelizable: true` allows concurrent execution among siblings.
- **Stories are first-class Lithos docs.** `prd-decompose` writes `note_type: task_record` with `derived_from_ids: [prd_id]`, then creates one task per story carrying `metadata.story_doc_id` and `metadata.prd_doc_id`. Story content is searchable in the KB; `lithos_related` surfaces lineage.
- **Per-PRD integration branch.** `prd-decompose` creates `loom/<prd-slug>` off `main`; every `story-implement` task branches off that and PRs against it. The integration branch merges to `main` as one PR via `merge-stories` (deferred — operator merges by hand in MVP).
- **Stable finding prefixes** for machine-parseable breadcrumbs: `[Plan]`, `[Drift]`, `[Recovery]`, `[Friction]`, `[ReviewPending]`, `[ReviewMerged]`, `[ReviewRejected]`, `[NoProgress]`, `[BlockerFailed]`, `[Cost]`, `[BrainDecision]`. These are the conveyor-style breadcrumbs the system reads back on restart and that lithos-lens displays.
- **Project files stay clean.** Loom config is machine-local TOML; project repo `AGENTS.md` / `CLAUDE.md` files contain no Lithos / Loom references (except for projects in the Lithos ecosystem itself, which may).

## Specifications

| Doc | Purpose |
|-----|---------|
| [`docs/PLAN.md`](docs/PLAN.md) | Locked design decisions, plugin list, plugin contract, build order, ambitious roadmap A1–A10 |
| [`docs/prd/mvp.md`](docs/prd/mvp.md) | MVP PRD — 35 user stories, ~4 days of focused work, target: end-to-end run on lithos-lens M1 PRD |
| [`docs/prd/full.md`](docs/prd/full.md) | Full system PRD — 75 user stories spanning A1–A10 |
| [`docs/result-schema.json`](docs/result-schema.json) | Versioned JSON Schema for the plugin `result.json` contract |

Read PLAN + the relevant PRD before changing any FR-level behaviour. The locked-decisions table in PLAN.md is the source of truth when PRD wording is ambiguous.

## Pre-merge checks (mandatory)

```bash
make check
```

Runs:

- `ruff check` + `ruff format --check` (style + lint; per-file Typer B008 ignore in `main.py` and plugin `__main__.py` is intentional)
- `pyright` (typecheck — `_optional_path` uses overloads to keep callers' return types non-optional when a non-None default is passed)
- `pytest` (unit + integration; auto-clears `LITHOS_*` env per test via `conftest.py`)

All three must pass. CI runs the same on every PR.

When changing the plugin contract, update `docs/result-schema.json` AND `tests/test_plugin_runner.py`. When changing config schema, update `examples/lithos-loom.toml` AND `tests/test_config.py`. When adding a new plugin, ship it under `src/lithos_loom/plugins/<name>/` with a `__main__.py` entry point and add an example route stanza to `examples/lithos-loom.toml`.
