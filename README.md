# lithos-loom

Workflow orchestration daemon for [Lithos](https://github.com/agent-lore/lithos) tasks. Three surfaces:

- **Obsidian bridge.** Projects open Lithos tasks into an Obsidian-Tasks-compatible inbox; pushes tick / priority / due-date / body edits back to Lithos; bidirectionally syncs project-context docs; Templater macros + CLI for capture, project create, and project import.
- **Route-runner.** Subscribes to Lithos's SSE event stream, matches `lithos.task.created`, `lithos.task.updated`, and `lithos.task.released` events against TOML-configured routes (a task must carry every tag in the route's `match.tags`), claims tasks collision-safely, and dispatches subprocess plugins. Plugin scaffolding for `prd-decompose` is in place; its body is a stub — `story-develop` is the one implement→review→PR path. Adding a route's trigger tag to an existing open task dispatches without a daemon restart ([#86](https://github.com/agent-lore/lithos-loom/issues/86)).
- **GitHub issue watcher.** Polls every project-context doc with `github_watch_enabled` metadata for new and updated open issues on each repo in its `github_repos` list (a project may map several), materialises each unseen issue as a Lithos task carrying `metadata.github_issue_url`, and keeps the two sides aligned both directions: GH→Lithos mirrors title / body / labels / close; Lithos→GH mirrors completion / cancellation (as `state_reason=completed` / `not_planned`) and title renames. Reopen on the GH side posts a one-shot `[ReopenRequested]` finding. Per-project repos are managed via `lithos-loom project add-github-repo`; per-host gate is `[github_watcher] enabled = true`.

It is the orchestration layer that connects Lithos to coding agents — replacing the [Ralph++](https://github.com/snarktank/ralph) approach with a fine-grained, fault-tolerant pipeline whose state lives in Lithos and whose hot state lives on the host filesystem.

## Documentation

- **[`docs/SPECIFICATION.md`](docs/SPECIFICATION.md)** — architecture, full TOML reference, CLI reference, plugin contract, event bus, projection rules, finding prefixes, error codes. Start here.
- [`docs/cli/project-import.md`](docs/cli/project-import.md) — `project import` full reference.
- [`docs/cli/project-regenerate-done.md`](docs/cli/project-regenerate-done.md) — `project regenerate-done` full reference.
- [`docs/cli/project-github-repos.md`](docs/cli/project-github-repos.md) — `project add-github-repo` / `remove-github-repo` / `enable-github` / `disable-github` / `migrate-github-tags` reference.
- [`docs/macros/README.md`](docs/macros/README.md) — Templater macro install + behaviour notes.
- [`docs/result-schema.json`](docs/result-schema.json) — plugin `result.json` JSON Schema.
- [`docs/prd/`](docs/prd/) — PRDs for queued work (Track 2 plugin bodies, A1–A10 roadmap, GitHub watcher, capture-macro tag parsing). Shipped PRDs are under [`docs/prd/archive/`](docs/prd/archive/).

## Requirements

- Python 3.12
- [`uv`](https://docs.astral.sh/uv/) (recommended)
- A reachable Lithos server exposing MCP-over-SSE at `<LITHOS_URL>/sse`. Requires `task.metadata`, single-shot `lithos_task_create(metadata=...)`, `lithos_write(id=..., expected_version=...)`, and `note.*` events on `GET /events`.
- `claude` and/or `codex` CLI authenticated (route-runner plugin bodies only)
- `gh` CLI authenticated (route-runner plugin bodies only)
- `git` with worktree support
- **For the Obsidian bridge:** Obsidian Desktop with [Templater](https://github.com/SilentVoid13/Templater) for the capture macros, and the [Tasks](https://publish.obsidian.md/tasks/Introduction) plugin for the daily-view queries that read projected lines.

## Install

```bash
uv sync
```

This installs the package in editable mode plus the dev dependencies. `lithos-loom` becomes available on the venv's PATH (`uv run lithos-loom …`).

To put a `lithos-loom` binary on your own PATH (`~/.local/bin/`), install the tool from the checkout — the package is not published to PyPI:

```bash
uv tool install .            # from the repo root; add --editable to track the checkout
```

Once it is published, `uv tool install lithos-loom` will do the same without a checkout.

### CLI on PATH for the Templater macros

The Templater macros shell out to `lithos-loom task create` / `project create` / `project list` / `obsidian-sync show` via Node's `child_process`. Obsidian Desktop inherits its PATH from the **desktop launcher session** (launchd on macOS, the systemd user session on Linux), **not** from your shell rc. Confirm Obsidian can find the binary:

```bash
# In Obsidian: Developer Console (Cmd-Opt-I / Ctrl-Shift-I) → Console tab:
require("child_process").execSync("which lithos-loom").toString()
```

If that errors, either:

- Install via `uv tool install .` from the checkout (puts the binary in `~/.local/bin/`, typically in the launcher PATH on Linux; on macOS use `launchctl setenv PATH ...` or symlink into `/usr/local/bin/`).
- Or hardcode the absolute path in the macro's `execFileSync` call (see [`docs/macros/README.md`](docs/macros/README.md)).

## Quick start

### 1. Bring up a Lithos server

Loom needs a running Lithos to talk to. Start it however you normally do (docker, host process, etc).

### 2. Drop a config in place

```bash
mkdir -p ~/.config/lithos-loom
cp examples/lithos-loom.toml ~/.config/lithos-loom/config.toml
$EDITOR ~/.config/lithos-loom/config.toml  # adjust paths to your machine
```

### 3. Set `LITHOS_URL` (and optionally pick an environment)

```bash
cp .env.example .env
$EDITOR .env  # set LITHOS_URL
```

`python-dotenv` loads `.env` from the current working directory automatically when Loom starts. You can also `export LITHOS_URL=...` in your shell rc.

### 4. Validate the config

```bash
uv run lithos-loom validate-config
uv run lithos-loom doctor
```

### 5. Run the daemon

```bash
uv run lithos-loom run
```

Loom runs as a foreground process. For background operation, use `tmux`, `nohup`, or a `systemd --user` unit.

To restart it without losing a run in flight, drain first:

```bash
uv run lithos-loom drain   # children finish their in-flight runs and exit; returns when the daemon has
uv run lithos-loom run
```

### 6. Install the Obsidian macros (optional)

If you want to create tasks or project-context docs from inside Obsidian, follow [`docs/macros/README.md`](docs/macros/README.md). Summary: copy `docs/macros/capture-task.md` and `docs/macros/create-project.md` verbatim into your vault's Templater Template Folder, register them, then bind hotkeys to **`Templater: Insert capture-task`** / **`Templater: Insert create-project`**. Verify the CLI-on-PATH check above first.

## Per-environment configuration

Loom supports multiple parallel configurations on the same workstation via `LITHOS_LOOM_ENVIRONMENT`. Useful when you want a `dev` Loom pointing at a local Lithos and a `prod` Loom pointing at your real KB, or when you want a sandbox config for testing new routes.

Search order:

1. `LITHOS_LOOM_CONFIG=/abs/path/to/cfg.toml` — explicit path, beats everything
2. If `LITHOS_LOOM_ENVIRONMENT=dev` is set, look for `config.dev.toml` in:
   - `./config.dev.toml` (CWD — useful for project-local overrides)
   - `~/.config/lithos-loom/config.dev.toml`
3. Otherwise, look for `config.toml` in the same locations

```bash
# Run against dev config
LITHOS_LOOM_ENVIRONMENT=dev uv run lithos-loom run

# Run against prod config
LITHOS_LOOM_ENVIRONMENT=prod uv run lithos-loom run

# Or pin an explicit path
uv run lithos-loom run --config /tmp/experimental.toml
```

## Why Loom doesn't run in docker

Lithos and Influx run as docker services because they're services — long-lived, stable protocols, no host filesystem coupling. Loom is different: it creates git worktrees you can `cd` into, invokes `claude` / `codex` / `gh` CLIs that authenticate against per-user dotfiles, and spawns plugin subprocesses that need the same access. The Templater macros additionally require the CLI to be on Obsidian Desktop's PATH. Containerizing Loom would require bind-mounting `~/.claude/`, `~/.codex/`, `~/.config/gh/`, every project repo's parent dir, the vault directory, and `/var/run/docker.sock`, which defeats containerization. Loom runs as a host process.

## Subcommands

| Command | Purpose |
|---------|---------|
| `lithos-loom run` | Start the daemon: supervisor + per-domain children. |
| `lithos-loom drain [--timeout S]` | Ask the running daemon to finish its in-flight runs, then exit (SIGUSR1 via the pidfile). |
| `lithos-loom doctor` | Verify the vault is writable and Lithos has the required surface. |
| `lithos-loom validate-config` | Typecheck the TOML, list configured projects / routes / subscriptions. |
| `lithos-loom validate-config --dry-run` | Also poll Lithos and print which routes / subscriptions would fire for each open task. |
| `lithos-loom config --show` | Print the merged effective config. |
| `lithos-loom gates [--shadow]` | Read-only inventory of every open gate and each gate's waiter health. |
| `lithos-loom task create --project X --title Y …` | Create a Lithos task and emit its projected line. Used by the capture macro. |
| `lithos-loom project list [--source lithos\|toml] [--format text\|json]` | List projects with status overlay. |
| `lithos-loom project create --title T [--slug S] …` | Create a new Lithos project-context doc. Used by the `create-project` macro. |
| `lithos-loom project import <source> [flags]` | Import an existing Markdown file as a Lithos project; extract `- [ ]` lines as Lithos tasks. See [`docs/cli/project-import.md`](docs/cli/project-import.md). |
| `lithos-loom project regenerate-done --slug S [flags]` | Rebuild `<slug>-done.md` from Lithos (all resolved tasks). See [`docs/cli/project-regenerate-done.md`](docs/cli/project-regenerate-done.md). |
| `lithos-loom project add-github-repo <slug> <owner/name>` / `remove-github-repo <slug> <owner/name>` | Map / unmap a GH repo for the issue watcher (a project may map several). See [`docs/cli/project-github-repos.md`](docs/cli/project-github-repos.md). |
| `lithos-loom project enable-github <slug>` / `disable-github <slug>` | Toggle GH issue watching for a project (preserves the repo list). |
| `lithos-loom project migrate-github-tags [--dry-run]` | One-shot migration of legacy tag-based github config to metadata. |
| `lithos-loom obsidian-sync show [--format text\|json]` | Print the resolved `[obsidian_sync]` block. Used by the capture macro. |
| `lithos-loom develop list` | List inspectable story-develop runs (in-flight + finished). |
| `lithos-loom develop attach <key>` | Follow a live run until it reaches a terminal state. |
| `lithos-loom develop dump <key>` | Print the assembled conversation log for a run. |
| `lithos-loom develop prune` | Remove the on-disk run-state dirs of finished runs. |
| `lithos-loom develop resume <run>` | Continue a run that died mid-loop, on its own branch. See [`docs/cli/resume.md`](docs/cli/resume.md). |
| `lithos-loom develop review …` | Run the reviewer panel + deterministic gate against an existing PR / ref range / branch — no coder, no fix loop. See [`docs/cli/review.md`](docs/cli/review.md). |
| `lithos-loom develop converge …` | Converge an existing PR to review-green (panel + gate, then a coder fix loop on the PR branch). See [`docs/cli/converge.md`](docs/cli/converge.md). |
| `lithos-loom develop merge-gate …` | Trial-merge a PR into its current base and run the check-set on the result. See [`docs/cli/merge-gate.md`](docs/cli/merge-gate.md). |
| `lithos-loom develop deliver <run>` | Push a stopped run's branch, open its PR, and swap the story's gate. See [`docs/cli/deliver.md`](docs/cli/deliver.md). |
| `lithos-loom develop converge-push <run>` | Report an exhausted converge run's unpushed rounds, and push them on request. See [`docs/cli/converge-push.md`](docs/cli/converge-push.md). |
| `lithos-loom develop brief-review <story>` | Draft a story's brief-review addendum and print it (dry run). See [`docs/cli/brief-review.md`](docs/cli/brief-review.md). |
| `lithos-loom eval review …` | Measure the reviewer panel's catch-rate on the seeded-defect benchmark (not part of `make check`). |
| `lithos-loom eval rescore <report-dir>` | Re-score a retained eval report dir — judge calls only, no reviewer runs. |
| `lithos-loom eval triage …` | Measure triage on batches of findings with known verdicts (PRD S8). |
| `lithos-loom eval resolve …` | Measure S5 conflict resolution on real conflicting merges (PRD S8). |

Full CLI reference (every flag, every exit code) is in [`docs/SPECIFICATION.md`](docs/SPECIFICATION.md) §4.

## Development

```bash
uv sync          # create the venv and install deps
make check       # ruff + ruff format + pyright + pytest
```

`make check` is the mandatory pre-merge gate; all four stages must be green. See [`AGENTS.md`](AGENTS.md) for the non-obvious project facts and rules of engagement.

After changing code or models, regenerate the architecture docs (component
diagram, domain model, metrics, and per-component pages under
[`docs/generated/`](docs/generated/README.md)):

```bash
make diagrams   # = pytest tests/guardrail/ -q; commit the result
```

`make test` runs the guardrail tests too, so a normal test run also rewrites
`docs/generated/`. CI's `diagrams` job fails if the committed views drift from the
code.

**Changing the gate definitions?** If you touch the deterministic gate's check
catalog (`src/lithos_loom/plugins/story_develop/check_catalog.py`, `profiles.py`,
`gate_adapters.py`, `check_runner.py` and friends — the full list is in
[`AGENTS.md`](AGENTS.md)), also run

```bash
make gate-exec   # host only: docker + the ralph-sandbox image + network
```

It executes every Python catalog command in the sandbox image against a clean
fixture (each check must pass) and a defective one (each must block). `make check`
and CI never run it — they only assert the command strings — and a story-develop
run gates with the daemon's installed catalog rather than the edited one, so a
broken check command otherwise ships green.

## Configuration model

| Layer | What it sets | When you change it |
|-------|--------------|--------------------|
| Defaults baked in | `log_level`, `resolved_ttl_days`, etc | Almost never. |
| TOML config | `orchestrator.*`, project registry, route table, subscriptions, `obsidian_sync` | Per-machine, per-environment. Hot-reload is not implemented; restart the daemon. |
| `.env` (CWD) or shell rc | `LITHOS_URL`, `LITHOS_LOOM_CONFIG`, `LITHOS_LOOM_ENVIRONMENT` | Per-shell session. |
| CLI flags (`--config`, `--dry-run`, …) | One-off overrides | Per invocation. |

Full TOML reference is in [`docs/SPECIFICATION.md`](docs/SPECIFICATION.md) §3.1; an annotated example lives at [`examples/lithos-loom.toml`](examples/lithos-loom.toml).

## License

MIT — see [LICENSE](LICENSE).
