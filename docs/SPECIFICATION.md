# Lithos Loom — Specification

Version: 0.1.0
Date: 2026-05-29
Status: Aligned with Implementation

---

## 1. Goals

### 1.1 Primary Goals

1. **Project Lithos work into Obsidian.** Render open Lithos tasks the operator needs to see as Tasks-plugin-compatible lines in a vault file, so that an existing Obsidian daily-view workflow surfaces Lithos work alongside personal todos.
2. **Push vault-side edits back to Lithos.** Tick-to-complete, priority emoji edits, due-date edits, and project-context body edits made in Obsidian propagate to Lithos with optimistic locking.
3. **Capture Lithos work from inside Obsidian.** Templater macros for creating tasks and project-context docs let the operator stay in the editor.
4. **Adopt existing Obsidian project docs into Lithos.** A `project import` CLI extracts `- [ ]` task lines as real Lithos tasks with dependency edges derived from indentation.
5. **Run subprocess plugins against Lithos tasks.** A route-runner child claims tasks by tag, invokes a plugin subprocess with a small contract, and applies the result back to Lithos.
6. **Stay out of project repos.** Loom configuration is host-local TOML; project repo `AGENTS.md` / `CLAUDE.md` files carry no Lithos / Loom references.

### 1.2 Non-Goals

1. **Cloud sync, multi-tenant operation, web UI.** Single-operator, local-first. Obsidian Sync (or any other vault-sync layer) is the operator's choice.
2. **Real-time co-editing.** File-based, poll-driven; latency target is ≤500ms end-to-end for projection + push.
3. **Replacing Lithos as the source of truth.** Lithos owns the corpus and the task lifecycle. The vault is a projection plus a write surface for specific edits.
4. **Cross-host coordination.** The vault host is the only host running `obsidian-sync`; other hosts run headless route-runners. Loom does not coordinate across hosts via shared state.
5. **Reopening a completed task.** Lithos has no `task_reopen` primitive. Untick (`[x] → [ ]`) posts a `[ReopenRequested]` finding instead, until upstream ships the primitive.
6. **Generating PRDs, reviewing diffs, brain-driven decisions.** These are roadmap items (see `docs/prd/full.md`); the implemented surface stops at the orchestration spine plus the Obsidian bridge.

### 1.3 Compatibility Policy (Pre-1.0)

1. **TOML schema evolves.** Field renames or removals require a documented migration step but are otherwise free.
2. **Event names are stable.** Subscribers depend on dotted event names (`lithos.task.created`, `obsidian.note.modified`); changing them is a breaking change.
3. **`result.json` schema is versioned.** Plugins ship a `schema_version` integer; incompatible changes bump it.
4. **Vault-projected file layout is stable.** `_lithos/tasks.md`, `_lithos/projects/<slug>/<file>.md`, `_lithos/conflicts/<slug>.<file>.<ts>.md` are documented locations operators query and grep against.

---

## 2. Architecture

### 2.1 Component Overview

Loom is structured as `sources → bus → subscribers`. Sources publish typed events onto an in-process async bus; subscribers consume them. The route-runner is a special claim-bound subscriber that owns a task's lifecycle to `result.json`.

```
┌──────────────────── lithos-loom (one host process) ────────────────────┐
│                                                                        │
│  ┌──────────────────────────────────────────────────────────────────┐  │
│  │ Supervisor (lithos-loom run)                                     │  │
│  │  - Reads one TOML config                                         │  │
│  │  - Forks subprocess children per enabled category                │  │
│  │  - Propagates SIGTERM, waits on exit                             │  │
│  └────────────┬─────────────────────────────────────┬───────────────┘  │
│               │                                     │                  │
│  ┌────────────▼──────────────┐         ┌────────────▼─────────────┐    │
│  │ route-runner child         │         │ obsidian-sync child       │    │
│  │  (enabled when [[routes]]) │         │  (enabled when           │    │
│  │                            │         │   [obsidian_sync]        │    │
│  │  Sources:                  │         │   is present)            │    │
│  │   LithosEventStream        │         │                          │    │
│  │                            │         │  Sources:                │    │
│  │  Subscribers:              │         │   LithosEventStream      │    │
│  │   one RouteRunner per      │         │   LithosNoteStream       │    │
│  │   [[routes]] stanza        │         │   ObsidianFSWatcher      │    │
│  │   (claim-bound)            │         │   ObsidianDirWatcher     │    │
│  │                            │         │                          │    │
│  │                            │         │  Subscribers (per        │    │
│  │                            │         │   configured action):    │    │
│  │                            │         │   obsidian-projection    │    │
│  │                            │         │   obsidian-status-       │    │
│  │                            │         │     transition           │    │
│  │                            │         │   obsidian-priority-     │    │
│  │                            │         │     changed              │    │
│  │                            │         │   obsidian-due-date-     │    │
│  │                            │         │     changed              │    │
│  │                            │         │   project-context-       │    │
│  │                            │         │     projection           │    │
│  │                            │         │   note-push              │    │
│  │                            │         │   task-archive           │    │
│  │                            │         │   noop                   │    │
│  │                            │         │                          │    │
│  │  In-process EventBus       │         │  In-process EventBus     │    │
│  └─────────────┬──────────────┘         └────────┬─────────────────┘    │
│                │                                 │                      │
└────────────────┼─────────────────────────────────┼──────────────────────┘
                 │                                 │
                 ▼                                 ▼
       ┌─────────────────┐                ┌────────────────┐
       │ Lithos          │                │ Obsidian vault │
       │  /sse  /events  │                │  (fs)          │
       └─────────────────┘                └────────────────┘
```

Each child runs its own EventBus instance. There is no inter-child IPC; both children independently consume Lithos SSE. Restart safety relies on sources being re-authoritative (no persistent event log) and subscribers being idempotent.

### 2.2 Data Flow

**Task projection (Lithos → vault).**
`LithosEventStream` connects to `<lithos_url>/events` filtered to `task.*` events. It bootstraps once on connect by calling `lithos_task_list(status='open', with_claims=true)` and re-emitting `lithos.task.created` for every open task, then streams live events with `Last-Event-ID` resume. `obsidian-projection` filters via `is_human_actionable(task, routes)` and rewrites `<vault>/<tasks_file>` atomically.

**Status push (vault → Lithos).**
`ObsidianFSWatcher` polls `<vault>/<tasks_file>` (default 250ms), parses line-by-line, and emits `obsidian.task.status_changed`, `obsidian.task.priority_changed`, or `obsidian.task.due_date_changed` when a line diverges from the last-known state. Self-write suppression compares mtime + content hash against the projection's last write. Three subscriptions consume these events and call `lithos_task_complete` / `_cancel` / `_update` against Lithos.

**Project-context projection (Lithos → vault).**
`LithosNoteStream` connects to `/events` filtered to `note.*` events; bootstrap calls `lithos_list(path_prefix='projects/', tags=['project-context'])` and re-emits `lithos.note.created` for each match. `project-context-projection` re-fetches via `lithos_read` (events are summaries; tags need verification post-fetch), then writes `<vault>/<projects_dir>/<slug>/<filename>.md` with a frontmatter envelope.

**Body push (vault → Lithos).**
`ObsidianDirWatcher` polls `<vault>/<projects_dir>/**/*.md` (default 250ms), computes body-only hash, and emits `obsidian.note.modified` when divergent. `note-push` calls `lithos_write(id=..., expected_version=...)`. On `version_conflict`, the conflict resolver moves the operator's body to `<vault>/_lithos/conflicts/<slug>.<file>.<ts>.md`, pulls canonical to the original path, and logs a `[Friction]` WARNING.

**Task lifecycle (route-runner).**
`LithosEventStream` (running in the route-runner child) emits `lithos.task.*` events. Each `[[routes]]` stanza registers a claim-bound subscriber that matches by tag intersection and claims via `lithos_task_claim`. On claim, the runner spawns the plugin subprocess, waits for `result.json`, applies `metadata_updates` via `lithos_task_update`, uploads any artifacts, posts findings, and completes or releases the task. On plugin timeout, the runner sends SIGTERM with a 5s grace before SIGKILL.

### 2.3 Restart and Recovery

Loom has no persistent event log. On restart:

- `LithosEventStream` and `LithosNoteStream` bootstrap (re-list and re-emit) and then resume via `Last-Event-ID`.
- `ObsidianFSWatcher` and `ObsidianDirWatcher` re-scan their watched files on startup; sync-state baselines are rebuilt incrementally as projection events fire.
- `obsidian-projection` writes the full file from scratch on every flush. Idempotent re-runs are no-ops thanks to atomic-write + content-hash dedup.
- `project-context-projection` re-reads each doc and rewrites if the full-file hash differs.
- `note-push` and `obsidian-status-transition` pre-check Lithos state before mutating (re-firing an already-completed task is a no-op).
- The route-runner reclaims any stale claims at startup via `lithos_task_release` for claims owned by the current `orchestrator.agent_id` whose plugin is no longer running.

Subscriptions are idempotent by construction; replay is safe.

---

## 3. Configuration

Loom reads one TOML file per process. Discovery order:

1. `LITHOS_LOOM_CONFIG=/abs/path/to/config.toml` (explicit, beats everything)
2. `LITHOS_LOOM_ENVIRONMENT=<env>` selects `config.<env>.toml` from `./` then `$XDG_CONFIG_HOME/lithos-loom/`
3. Plain `config.toml` from the same locations

`python-dotenv` loads `.env` from the current working directory at startup, primarily for `LITHOS_URL`.

### 3.1 Full TOML Reference

```toml
# ── Required ──────────────────────────────────────────────────────────

[orchestrator]
agent_id      = "lithos-orchestrator-<host>"  # claim attribution; must be unique per host
lithos_url    = "http://localhost:8765"        # Lithos MCP-over-SSE endpoint
work_dir      = "/tmp/lithos-loom"             # per-task staging tree
max_concurrency        = 4                     # global concurrent plugin runs
log_level              = "info"                # debug | info | warn | error
retain_failed_workdirs = true                  # keep failed work-dirs for triage

# ── Projects (host-local automation registry) ─────────────────────────
#
# Projects exist in Lithos when a project-context doc lives at
# `knowledge/projects/<slug>/`. This TOML registers host-local automation
# config for projects this host should act on. `repo` is the only
# required field; `claude_config` and `codex_config` are read by Track 2
# plugins.

[projects.<slug>]
repo          = "/abs/path/to/repo"
claude_config = "/home/you/.claude-lithos"     # optional, Track 2
codex_config  = "/home/you/.codex-lithos"      # optional, Track 2

# ── Routes (claim-bound subscribers) ──────────────────────────────────
#
# Each [[routes]] stanza is a claim-bound subscriber: it matches by tag
# intersection on `lithos.task.created` / `_updated` / `_claimed`, claims
# the task, invokes the command as a subprocess, parses the resulting
# result.json, applies metadata updates + artifacts + findings, and
# completes or releases.
#
# Substitution tokens in `command`:
#   {{task_json}}    — path to the task envelope JSON (read-only)
#   {{work_dir}}     — per-task staging dir under orchestrator.work_dir
#   {{result_file}}  — path the plugin must atomically write

[[routes]]
name = "story-implement"
command = "uv run python -m lithos_loom.plugins.story_implement --task-json {{task_json}} --work-dir {{work_dir}} --result-file {{result_file}}"
max_runtime_seconds = 7200
human_blocking = false  # if true, surfaced in Obsidian projection once claimed

[routes.match]
tags = ["trigger:story-implement"]  # task must carry ALL listed tags

# ── Subscriptions (fire-and-forget side effects) ──────────────────────
#
# Each [[subscriptions]] stanza is a fire-and-forget subscriber that
# consumes one or more event types, runs an `action` registered as a
# Python entry-point handler, retries on failure with exponential or
# linear backoff, and posts a [Friction] finding on persistent failure
# (default; set to "ignore" to suppress).

[[subscriptions]]
name = "obsidian-tasks"
on = [
  "lithos.task.created",
  "lithos.task.updated",
  "lithos.task.claimed",
  "lithos.task.released",
  "lithos.task.completed",
  "lithos.task.cancelled",
]
match.tags = []                  # optional: structural superset filter
where      = ""                  # optional: Python expression with `task` and `event` in scope
action     = "obsidian-projection"
on_persistent_failure = "friction"  # or "ignore"
[subscriptions.retry]
attempts = 5
backoff  = "exponential"          # or "linear"
initial_delay_seconds = 0.5
max_delay_seconds     = 30.0

# ── Obsidian sync (vault-host only) ───────────────────────────────────
#
# Presence of this block is the spawn gate for the obsidian-sync child.
# Omit on hosts without a vault.

[obsidian_sync]
vault_path        = "/home/you/Obsidian/Vault"   # absolute
tasks_file        = "_lithos/tasks.md"           # relative to vault_path
projects_dir      = "_lithos/projects"           # relative to vault_path
resolved_ttl_days = 7                            # see §6.3 task-archive interaction
include_blocked   = true                         # project tasks with metadata.depends_on
exclude_tags      = ["debug:trace"]              # suppress projection for these tags
```

### 3.2 Validation

`lithos-loom validate-config` parses, typechecks, and lists projects / routes / subscriptions. `validate-config --dry-run` additionally polls Lithos and prints which routes / subscriptions would fire for each currently-open task plus any orphans (tasks no route matches) and dead config (routes / subscriptions no task currently matches). Both forms exit non-zero on invalid TOML.

`lithos-loom doctor` verifies the configured `vault_path` exists, `_lithos/` is creatable, and a probe write+read round-trip works. It also reads `lithos_list(path_prefix='projects/')` and warns about TOML `[projects.<slug>]` entries with no corresponding Lithos project-context doc.

---

## 4. CLI Reference

All commands accept `--config / -c <path>` to override discovery. JSON-emitting commands accept `--format / -f json|text`.

### 4.1 `lithos-loom run`

Starts the daemon: supervisor + per-domain children. Foregrounded process; SIGINT / SIGTERM trigger graceful shutdown (children stop, claims for in-flight tasks are released, supervisor exits).

```
lithos-loom run [-c config.toml]
```

Exit codes: `0` clean exit, non-zero on child crash before shutdown or SIGKILL after timeout.

### 4.2 `lithos-loom validate-config`

```
lithos-loom validate-config [-c config.toml] [--dry-run]
```

- Plain form: parse, validate, print `OK:` summary (agent_id, lithos_url, projects, routes, subscriptions).
- `--dry-run`: also fetch open tasks from Lithos and print routing / subscription dry-runs. Useful before introducing new routes.

### 4.3 `lithos-loom doctor`

```
lithos-loom doctor [-c config.toml]
```

Probes vault writability + the Lithos project surface. Each check prints PASS/FAIL with an actionable message. Non-zero exit if any check fails.

### 4.4 `lithos-loom config`

```
lithos-loom config --show [-c config.toml]
```

Prints the merged effective config. Useful for verifying config discovery picked the right file.

### 4.5 `lithos-loom task create`

```
lithos-loom task create --project <slug> --title <text>
                        [--brief <text>] [--scheduled YYYY-MM-DD]
                        [--priority highest|high|medium|low|lowest]
                        [--tags a,b,c]
                        [--target-file <path> | --no-insert]
                        [-c config.toml]
```

Creates a Lithos task and emits its projected line. Used by the capture-task Templater macro.

Output modes (mutually exclusive):

- **Default**: print the projected `- [ ]` line to stdout. Useful for redirect/pipe.
- **`--target-file PATH`**: append the line to PATH (creates parent dirs). Used by "create task and write the line into next week's daily note" flows.
- **`--no-insert`**: print just the task_id to stdout; the projected line is discarded. Used by the capture macro (which inserts a wikilink instead).

Exit codes: `0` success, `1` Lithos call failed, `2` validation failure (unknown project, unknown priority, mutually-exclusive output flags).

### 4.6 `lithos-loom project list`

```
lithos-loom project list [--source lithos|toml] [-f text|json] [-c config.toml]
```

- `--source lithos` (default): merges Lithos's project-context-doc list with the local TOML overlay. Three columns: slug, status (`active`/`archived` from the canonical doc), repo path.
- `--source toml`: lists only the TOML's `[projects.<slug>]` slugs.
- `-f json`: emits a JSON array of slug strings (what the capture macro consumes).

Canonical-doc picker: for each slug, prefers `projects/<slug>/<slug>-project-context.md`; falls back to lex-min path when no canonical doc exists.

### 4.7 `lithos-loom project create`

```
lithos-loom project create --title <text>
                           [--slug <slug>] [--tags a,b]
                           [--body <text> | --body-file <path>]
                           [-f text|json] [-c config.toml]
```

Creates a new Lithos project-context doc at `projects/<slug>/<slug>-project-context.md`. Slug defaults to slugified `--title` when not given. `project-context` tag is always added (plus any operator-supplied tags, deduped).

Output: vault path of the projected file (text) or `{id, slug, vault_path}` (json).

Exit codes: `0` success, `1` slug collision or Lithos call failure, `2` invalid slug.

### 4.8 `lithos-loom project import`

```
lithos-loom project import <source> [--slug <slug>] [--tags a,b]
                                    [--tasks-only] [--no-tasks]
                                    [--force-tasks] [--yes]
                                    [--dry-run] [-f text|json]
                                    [-c config.toml]
```

Imports an existing local Markdown file as a Lithos project, extracting `- [ ]` task lines as real Lithos tasks. Two modes:

- **Greenfield (default)**: creates the project doc + tasks. Refuses if slug exists; error message points at `--tasks-only` as the alternative.
- **`--tasks-only` + `--slug`**: skips doc creation; just adds tasks to an existing project.

Task extraction is on by default. `--no-tasks` skips it. `--force-tasks` cancels all open tasks for the slug before importing (gated by interactive y/N unless `--yes`). `--dry-run` prints the full plan without Lithos writes; output is framed with `NO CHANGES MADE` markers at start and end.

Slug derivation (when `--slug` not given): `--title` frontmatter → file stem with a leading `project-` prefix stripped. The strip is flagged in dry-run output.

Task extraction parses:
- Tags matching `#[A-Za-z0-9_/-]+` (all-digit tokens like `#123` excluded).
- Priority emoji `🔺⏫🔼🔽⏬` mapped to `metadata.priority`.
- Cross-project `#project/<other-slug>` tags refuse the import (exit 2).
- Indented children become `metadata.depends_on` from parent → children; siblings are `metadata.parallelizable = true` by default; `[sequential]` token on parent flips children to a chain.

Exit codes: `0` success, `1` Lithos call failure / slug collision / missing project / partial-import failure, `2` input validation failure.

Full reference: `docs/cli/project-import.md`.

### 4.9 `lithos-loom project regenerate-done`

```
lithos-loom project regenerate-done --slug <slug>
                                    [--dry-run] [--yes]
                                    [-f text|json] [-c config.toml]
```

Rebuilds `<vault>/<projects_dir>/<slug>/<slug>-done.md` from Lithos by writing every resolved (completed + cancelled) task for the slug as a Tasks-plugin line. Replaces the file outright (no merge). Sorted ascending by `resolved_at`, ties broken by task id. Confirmation prompt fires when the file already exists; `--yes` bypasses.

Differs from the live `task-archive` subscription: the archive subscription only records tasks the operator surfaced in `tasks.md`; `regenerate-done` writes all resolved tasks (a complete-history superset).

Full reference: `docs/cli/project-regenerate-done.md`.

### 4.10 `lithos-loom obsidian-sync show`

```
lithos-loom obsidian-sync show [-f text|json] [-c config.toml]
```

Prints the resolved `[obsidian_sync]` block. Used by the capture-task macro to discover the configured `tasks_file` path at runtime, so vaults that customise it get the wikilink target right without editing the macro.

---

## 5. Plugin Contract

Plugins are subprocesses invoked by a route-runner. They receive a small CLI surface and write an atomic `result.json`.

### 5.1 Invocation

```
<command> --task-json <path> --work-dir <path> --result-file <path>
```

- `--task-json`: read-only JSON file containing the full `lithos_task_status(task_id)` payload plus the resolved project entry from the local TOML (the `[projects.<slug>]` block matched by `task.metadata.project`).
- `--work-dir`: per-task staging directory at `<orchestrator.work_dir>/<task_id>/`. The plugin owns the tree; the runner reads only the result file.
- `--result-file`: path the plugin must write atomically (temp file + fsync + rename). Partial files must never be observable.

Substitution tokens (`{{task_json}}`, `{{work_dir}}`, `{{result_file}}`) in the route's `command` are filled in by the runner before fork.

### 5.2 Result Schema

The full schema is at `docs/result-schema.json` (JSON Schema Draft 2020-12). Required fields: `schema_version` (const 1), `task_id`, `status`, `exit_code`. Optional: `started_at`, `finished_at`, `worktree`, `artifacts`, `commits`, `spawned_tasks`, `metadata_updates`, `error`.

```json
{
  "schema_version": 1,
  "task_id": "uuid",
  "status": "succeeded" | "failed" | "interrupted",
  "exit_code": 0,
  "started_at": "2026-05-29T10:00:00Z",
  "finished_at": "2026-05-29T10:05:00Z",
  "worktree": "/abs/path or null",
  "artifacts": { "key": "rel/path or /abs/path" },
  "commits": ["40-char-sha", ...],
  "spawned_tasks": ["task_id", ...],
  "metadata_updates": { "pr_url": "https://..." },
  "error": { "code": "...", "message": "...", "retriable": false }
}
```

### 5.3 Exit Code Convention

| Code | Meaning | Runner behaviour |
|---|---|---|
| `0` | Succeeded. `status` must be `succeeded`. | Apply metadata updates, upload artifacts, post findings, complete task. |
| `1` | Generic failure. Consult `error.retriable`. | Post `[BlockerFailed]` finding, release claim. |
| `20` | Bad input / config. | Treat as non-retriable. Post `[BlockerFailed]`, release. |
| `30` | Interrupted by signal. | Release claim, leave task open. No finding. |

### 5.4 Runner Lifecycle

The route-runner enforces `max_runtime_seconds` (per-route config). On timeout, it sends SIGTERM and waits 5 seconds; if the plugin hasn't exited, it sends SIGKILL. Result-file absence after exit is treated as a contract violation: the runner posts `[BlockerFailed] route <name>: plugin contract violation: did not write <path>` and releases the claim.

`retain_failed_workdirs = true` keeps the work directory for triage on failure; on success the work-dir is removed.

### 5.5 Bundled Plugins (scaffolded)

`prd-decompose`, `story-implement`, `story-review-human` are present under `src/lithos_loom/plugins/` as Python modules with prompt files. Their bodies are not part of the implemented surface; the scaffolding lets the routes parse and the plugin contract be exercised end-to-end with stub `result.json` files. The route-runner code path is the load-bearing piece; plugin bodies land later.

---

## 6. Event Bus Contract

### 6.1 Event Schema

```python
@dataclass(frozen=True)
class Event:
    type: str            # dotted name: "lithos.task.created", "obsidian.note.modified"
    payload: dict        # event-type-specific; see §6.4
    source: str          # source identity for trace
```

Events are passed by reference through the in-process bus. Subscribers must not mutate the payload.

### 6.2 Filter Language

Each `[[subscriptions]]` stanza may carry both filters; an event passes when both hold (logical AND):

- **Structural `match.<key>` tables.** A `match.tags = ["X", "Y"]` requires the event payload's task / note to carry every listed tag (superset semantics). Other `match.*` keys check equality on the named payload field.
- **`where = "<python-expression>"`.** The expression is evaluated in a restricted scope exposing `event` (the Event object) and `task` (= `event.payload` for task events). Only safe builtins are available; no imports, no attribute lookups outside the allowed names.

`match` runs first as a cheap structural filter; `where` runs on the survivors.

### 6.3 Dispatch Semantics

- **Concurrent fire-and-forget.** Each subscription has a bounded async queue; events dispatched to a full queue increment a drop counter and emit a WARNING log line. Slow subscribers do not block fast ones.
- **Per-subscription retry.** Failure raises an exception; the runner sleeps `initial_delay` then retries with `exponential` (or `linear`) backoff up to `max_delay`. After `attempts` exhausted, `on_persistent_failure = "friction"` posts a `[Friction]` finding to the related task; `"ignore"` suppresses.
- **Idempotency is the handler's responsibility.** Handlers must be safe under replay (cold-start re-emission, network reconnect with `Last-Event-ID`, daemon restart). Pre-check before mutating Lithos state.

### 6.4 Event Catalog

| Event type | Source | Payload (shape) |
|---|---|---|
| `lithos.task.created` | LithosEventStream | `{id, title, status, tags, claim, metadata, created_at, updated_at, ...}` (full task envelope) |
| `lithos.task.updated` | LithosEventStream | full task envelope (post-edit) |
| `lithos.task.claimed` | LithosEventStream | full task envelope + `claim.{agent_id, route_name, claimed_at, expires_at}` |
| `lithos.task.released` | LithosEventStream | full task envelope, `claim = null` |
| `lithos.task.completed` | LithosEventStream | full task envelope + `resolved_at` |
| `lithos.task.cancelled` | LithosEventStream | full task envelope + `resolved_at` |
| `lithos.note.created` | LithosNoteStream | `{id, path, title, tags, status, version, updated_at}` (note summary; full body fetched via `lithos_read` on demand) |
| `lithos.note.updated` | LithosNoteStream | note summary |
| `lithos.note.deleted` | LithosNoteStream | `{id, path}` |
| `obsidian.task.status_changed` | ObsidianFSWatcher | `{task_id, prior_status, new_status, line_number}` where status is one of `[ ]`, `[x]`, `[-]`, `[/]`, `[>]` |
| `obsidian.task.priority_changed` | ObsidianFSWatcher | `{task_id, prior_priority, new_priority}` where priority is `highest|high|medium|low|lowest|null` |
| `obsidian.task.due_date_changed` | ObsidianFSWatcher | `{task_id, prior_date, new_date}` |
| `obsidian.note.modified` | ObsidianDirWatcher | `{doc_id, slug, path, body, body_hash, local_version}` |

### 6.5 Sources

Sources are async coroutines spawned by their owning child. They consume external input (Lithos SSE, filesystem polls) and publish events.

| Source | Spawned by | Bootstrap | Reconnect |
|---|---|---|---|
| `LithosEventStream` | route-runner + obsidian-sync (independently) | `lithos_task_list(status='open', with_claims=true)` → re-emit `lithos.task.created` per task. | Exponential backoff with `Last-Event-ID` resume. |
| `LithosNoteStream` | obsidian-sync (when `project-context-projection` is configured) | `lithos_list(path_prefix='projects/', tags=['project-context'])` → re-emit `lithos.note.created` per match. | Exponential backoff with `Last-Event-ID` resume. |
| `ObsidianFSWatcher` | obsidian-sync | Polls `<vault>/<tasks_file>` on a 250ms cadence; emits when a line diverges from the last-known state. | n/a (polling). |
| `ObsidianDirWatcher` | obsidian-sync (when `note-push` is configured) | Walks `<vault>/<projects_dir>/**/*.md` on the same cadence; computes body-only hashes. | n/a. Excludes files ending in `-done.md` (the per-project archive). |

### 6.6 Subscription Action Registry

Subscriptions resolve their `action` field against the `lithos_loom.subscriptions.handlers` Python entry-point group:

| Action | Module | Consumes | Effect |
|---|---|---|---|
| `noop` | `_noop` | any | Logs at DEBUG. Useful for tracing. |
| `obsidian-projection` | `_obsidian_projection` | `lithos.task.*` | Rewrites `<vault>/<tasks_file>`. |
| `obsidian-status-transition` | `_obsidian_status_transition` | `obsidian.task.status_changed` | `[ ]→[x]` calls `lithos_task_complete`; `[ ]→[-]` calls `lithos_task_cancel`; `[x]→[ ]` posts `[ReopenRequested]` finding; `[/]` / `[>]` are no-op (logged). |
| `obsidian-priority-changed` | `_obsidian_priority_changed` | `obsidian.task.priority_changed` | `lithos_task_update(metadata={priority: ...})`. |
| `obsidian-due-date-changed` | `_obsidian_due_date_changed` | `obsidian.task.due_date_changed` | `lithos_task_update(metadata={scheduled_for: ...})`. |
| `project-context-projection` | `_project_context_projection` | `lithos.note.*` | Re-fetches via `lithos_read`, writes `<vault>/<projects_dir>/<slug>/<filename>.md` atomically. |
| `note-push` | `_note_push` | `obsidian.note.modified` | `lithos_write(id, content, expected_version)`; on conflict, runs the conflict resolver. |
| `task-archive` | `_task_archive` | `lithos.task.completed` / `lithos.task.cancelled` | Appends a Tasks-plugin line to `<vault>/<projects_dir>/<slug>/<slug>-done.md` (O_APPEND); marks the task as archived so the projection evicts it on next flush. |

Third-party handlers can be registered via Python entry points. Each handler receives an `Event` and a `SubscriptionContext` (shared `LithosClient`, filesystem helpers, retry-aware sleep, scoped logger).

---

## 7. Obsidian Projection

### 7.1 File Layout

```
<vault_path>/
├── <tasks_file>                              # default: _lithos/tasks.md
└── <projects_dir>/                           # default: _lithos/projects/
    ├── <slug>/
    │   ├── <slug>-project-context.md         # canonical project doc (per Lithos KB convention)
    │   ├── <other-file>.md                   # any additional project-context-tagged doc
    │   └── <slug>-done.md                    # task-archive's append-only history (vault-only)
    ├── _unassigned/
    │   └── _unassigned-done.md               # archive bucket for tasks with missing metadata.project
    └── _lithos-loom-internal/                # daemon-owned coordination docs (deferred)
└── _lithos/conflicts/                        # note-push conflict snapshots
```

All writes use a dot-prefixed temp file (`.<filename>.tmp.<rand>`) plus `os.replace` for atomicity. The dot prefix matters: Obsidian Sync (and Dropbox-style observers) skip dotfiles, avoiding a publish noise.

### 7.2 Tasks-Plugin Line Shape

```markdown
- [ ] <title> [⏫] 🆔 lithos:<id> [⛔ lithos:<dep_id>...] [📅 YYYY-MM-DD] #project/<slug> [#lithos/<route-name>] [#<tag>...]
```

Field order is operator-readable; the Tasks plugin parses positionally-flexibly.

| Token | Meaning | Source | Direction |
|---|---|---|---|
| `[ ]` / `[x]` / `[-]` | Status: open / completed / cancelled | `task.status` | Bidirectional. `[/]` and `[>]` are detected on read, no-op on write. |
| `🔺⏫🔼🔽⏬` | Priority (highest / high / medium / low / lowest) | `task.metadata.priority` | Bidirectional. Absent emoji = no priority. |
| `🆔 lithos:<id>` | Task ID | `task.id` | One-way (identity; never edited by operator). |
| `⛔ lithos:<dep_id>` | One marker per `metadata.depends_on` entry | `task.metadata.depends_on[]` | One-way (Lithos canonical). |
| `📅 YYYY-MM-DD` | Due date | `metadata.scheduled_for` if set; else `today` for human-blocking tasks; else absent | Bidirectional via `metadata.scheduled_for`. |
| `✅ YYYY-MM-DD` | Completed date | `task.resolved_at` | One-way; only rendered for `[x]` lines within TTL. |
| `❌ YYYY-MM-DD` | Cancelled date | `task.resolved_at` | One-way; only rendered for `[-]` lines within TTL. |
| `#project/<slug>` | Project tag | `task.metadata.project` | One-way. |
| `#lithos/<route-name>` | Active claim's route | `task.claim.route_name` | One-way; surfaces while the claim holds. |
| `#<tag>` | Lithos task tags | `task.tags` (excluding `trigger:*` route tags) | One-way (Lithos canonical). |

### 7.3 Projection Filter

A task is projected when `is_human_actionable(task, routes)` returns true:

- The task is `open`, AND
- Either (a) no `[[routes]]` matches the task's tags, OR (b) a route matches AND that route has `human_blocking = true` AND the route currently holds the claim.

Dependency-blocked tasks still project (with the `⛔` marker); the Tasks plugin's own queries decide whether to surface or hide them.

Tasks with terminal status (`completed` / `cancelled`) project with `[x]` / `[-]` and the corresponding `✅` / `❌` date marker, lingering until either (a) the `task-archive` subscription evicts them on the next flush after archiving, or (b) `resolved_ttl_days` elapses since `resolved_at`.

### 7.4 Project-Context Projection

Each `project-context`-tagged note under `projects/` projects to one file at `<vault>/<projects_dir>/<slug>/<filename>.md`, where:

- `<slug>` = the directory name under Lithos's `knowledge/projects/<slug>/`.
- `<filename>` = the slug of the doc's `title` (Lithos slugifies title → filename).

Frontmatter envelope:

```yaml
---
lithos_id: <uuid>
lithos_version: <int>
lithos_updated_at: <ISO 8601>
slug: <directory-name>
status: active | archived
tags:
  - project-context
  - ...
title: <title>
---
# <title>

<body>
```

The body below the frontmatter is the Lithos doc body. Frontmatter is daemon-managed; operator edits to frontmatter fields are not pushed back. Body edits are pushed via `note-push` (see §7.5).

Filename migration: if Lithos changes the doc's title (changing the slug), the projection writes the new path first, then unlinks the old path. Order matters — a failed new write leaves both copies on disk rather than losing the content.

### 7.5 Bidirectional Note Push

`ObsidianDirWatcher` polls projected files. When the body-only hash diverges from the projection's last write, it emits `obsidian.note.modified` with the operator's body and the current local `lithos_version`. `note-push`:

1. Fetches canonical via `lithos_read(id)` for current title / tags / status.
2. Calls `lithos_write(id, content=body, expected_version=local_version)`.
3. On `status=updated`: re-fetches via `lithos_read` to refresh the local frontmatter (`lithos_version`, `lithos_updated_at`).
4. On `status=version_conflict`: invokes the conflict resolver — moves the operator's body to `<vault>/_lithos/conflicts/<slug>.<file>.<ts>.md`, writes canonical to the original path, logs `[Friction]` WARNING.
5. On `status=duplicate`: no-op (Lithos detected the body is identical to canonical).

**Frontmatter-only edits are silently absorbed.** The watcher hashes the body only; adding a custom YAML field (e.g. a Dataview field) does not trigger a push. Custom fields persist until the next projection rewrite, at which point the renderer reconstructs frontmatter from scratch and the custom field is lost. This is by design — frontmatter is daemon-managed.

**Cold-start divergence.** If the daemon was down while the operator edited a projected file, the bootstrap projection detects the local-vs-canonical body diff and routes through the same conflict resolver (operator's body preserved in `_lithos/conflicts/`, canonical pulled to the original path).

### 7.6 Per-Project Task Archive

When `task-archive` is configured, the `obsidian-projection` handler also marks tasks as "surfaced" in an in-memory map whenever it writes a task line. On `lithos.task.completed` / `lithos.task.cancelled`, the `task-archive` handler:

1. Skips tasks that were never surfaced (background / route-claimed-only work).
2. Resolves the target file from `task.metadata.project`; falls back to `_unassigned-done.md` if missing or unknown.
3. Renders one Tasks-plugin line (terminal-status drops priority + due-date markers; `✅` or `❌` carries `task.resolved_at`).
4. Dedups against existing lines in the file (lazy-read on first event per project).
5. O_APPEND writes the line.
6. Marks the task as archived so the projection evicts it from `tasks_file` on next flush.

The done file is **vault-only and append-only** — the dir-watcher excludes the `-done.md` suffix so operator edits are inert. Deleting a done file can be recovered with `project regenerate-done` (which rebuilds from Lithos, all-resolved-tasks superset).

### 7.7 Filter Knobs

- **`include_blocked`** (default `true`): when `false`, tasks with non-empty `metadata.depends_on` are not projected.
- **`exclude_tags`** (default `[]`): tasks carrying any listed tag are not projected. Useful for suppressing automation noise (e.g. `["influx:run", "influx:backfill"]`).
- **`resolved_ttl_days`** (default `7`): how long resolved tasks linger in `tasks_file` when `task-archive` is NOT configured, OR (when `task-archive` IS configured) the bootstrap-replay window the archiver looks back over on restart.

---

## 8. Finding Prefixes

Loom posts findings with stable prefixes so operators (and `lithos-lens`) can grep machine-parseably:

| Prefix | Posted by | Meaning |
|---|---|---|
| `[Friction]` | any subscription | Persistent failure of a side effect (retry exhausted) OR a notable operator-visible event (e.g. note-push conflict). Always WARNING-level. |
| `[ReopenRequested]` | `obsidian-status-transition` | An operator unticked a completed task; Lithos has no reopen primitive yet, so this signals the intent. |
| `[BlockerFailed]` | route-runner | Plugin failed; the claim was released. Surfaces in lithos-lens "needs attention" filters. |
| `[Plan]` | reserved (Track 2) | A plugin's pre-flight summary of what it intends. |
| `[Drift]` | reserved (Track 2) | A plugin's post-run comparison of what it built vs the brief. |
| `[Recovery]` | reserved (Track 2) | Crash-recovery breadcrumb pointing at the last checkpoint. |
| `[ReviewPending]` / `[ReviewMerged]` / `[ReviewRejected]` | reserved (Track 2) | PR-review lifecycle on `story-implement` work. |
| `[Cost]` | reserved (Track 2) | Per-task cost / token / turn count. |

The `reserved` rows are claimed by plugins not yet implemented; operators should not invent collisions.

---

## 9. Errors and Exit Codes

CLI commands use a unified exit code convention:

| Code | Meaning |
|---|---|
| `0` | Success (or clean user abort at a `--yes`-gateable prompt). |
| `1` | Operational failure — Lithos call failed, config load failed, slug collision, missing project, partial-import failure, network unreachable. |
| `2` | Input validation failure — invalid flag combination, unknown project, unknown priority, malformed task lines, cross-project tag, empty parent, `lithos_id` / `--slug` mismatch, unreadable source file. |

`lithos-loom run` exits `0` on clean shutdown, non-zero on child crash or SIGKILL after timeout.

### 9.1 Common Validation Failures

- **`unknown project '<slug>'`** (exit 2, `task create`): the `--project` value isn't in Lithos. Returned by `task create`'s pre-flight `lithos_list(path_prefix='projects/<slug>/')` lookup.
- **`unknown priority '<value>'`** (exit 2, `task create`): `--priority` must be one of `highest|high|medium|low|lowest`.
- **`--target-file and --no-insert are mutually exclusive`** (exit 2, `task create`).
- **`slug '<X>' already exists at doc <id>`** (exit 1, `project create`): refuses to overwrite. Use `project import --tasks-only --slug <X>` if you wanted to add tasks instead.
- **`no project at slug '<X>'`** (exit 1, `project import --tasks-only`): includes near-miss suggestions when typo distance ≤ 2.
- **`lithos_id resolves to project Y; --slug=X; refusing`** (exit 2, `project import --tasks-only`): the source file's frontmatter `lithos_id` points at a different project than `--slug`.
- **`obsidian_sync.vault_path must be a non-empty path string`** (exit 1, validate-config): the `[obsidian_sync]` block is malformed.
- **`tasks_file must be relative to vault_path`** (exit 1, validate-config): absolute paths in `tasks_file` are rejected.

### 9.2 Runtime Failures

- **`plugin <pid> did not honour SIGTERM within 5.0s; sent SIGKILL`** (route-runner WARNING): the plugin exceeded `max_runtime_seconds` and didn't shut down. The claim is released and `[BlockerFailed]` is posted.
- **`plugin contract violation: did not write <path>`** (route-runner WARNING): the plugin exited but no `result.json` exists. `[BlockerFailed]` posted; claim released.
- **`note-push conflict for doc=<id>`** (note-push `[Friction]`): operator and Lithos both edited the doc; the operator's body is preserved at `_lithos/conflicts/<slug>.<file>.<ts>.md`, canonical was pulled to the original path.
- **`obsidian-projection: skipped <slug>: no project-context doc in Lithos`** (doctor): TOML registers a slug Lithos doesn't know. Either create the doc in Lithos or remove the TOML stanza.

---

## 10. Lithos Prerequisites

Loom requires a Lithos server exposing the MCP-over-SSE surface plus these primitives:

| Surface | Used for |
|---|---|
| `lithos_task_list(status='open', with_claims=true)` | Source bootstrap. |
| `lithos_task_status`, `_create`, `_complete`, `_cancel`, `_update`, `_claim`, `_release` | Task lifecycle. |
| `lithos_task_create(metadata=...)` | Single-shot create with metadata (post `agent-lore/lithos#295`). |
| `lithos_finding_post` | `[Friction]` / `[ReopenRequested]` / `[BlockerFailed]` breadcrumbs. |
| `lithos_write(id=..., expected_version=...)` | Note push with optimistic locking; `version_conflict` envelope drives the conflict resolver. |
| `lithos_read`, `lithos_list(path_prefix=...)`, `lithos_delete` | Project-context projection + CLI surface. |
| `task.metadata` field on tasks | All `metadata.*` references throughout (priority, scheduled_for, project, depends_on, parallelizable, etc.). |
| `task.updated` event with full envelope | The projection consumes the post-edit envelope directly without re-fetching. |
| `note.created` / `note.updated` / `note.deleted` events on `GET /events` SSE | Project-context projection. |

Slug = directory name under `knowledge/projects/<slug>/`. Lithos enforces uniqueness with a `slug_collision` envelope; Loom relies on this rather than a frontmatter field.

Pending upstream: `lithos_task_reopen` (`agent-lore/lithos#243`) would replace the `[ReopenRequested]` finding workaround.

---

## 11. Multi-Host Deployment

The vault host (typically a workstation with Obsidian) runs the full daemon — the supervisor spawns both route-runner and obsidian-sync children. Other hosts (additional workstations, headless servers) run with `[obsidian_sync]` omitted; the supervisor spawns only the route-runner child.

There is no inter-host coordination. Each host:

- Registers as `lithos-orchestrator-<host>` via `orchestrator.agent_id`.
- Reads its own TOML config (different `[projects.<slug>]` registry, different routes).
- Claims tasks competitively via `lithos_task_claim` (Lithos guarantees collision safety).

Per-project automation (the `[projects.<slug>].repo` field) is host-specific. If host A doesn't have the repo checked out, it can't claim tasks for that project. Project existence is a Lithos fact (the project-context doc); project automation is a host fact (the TOML entry).

Obsidian Sync (the app) handles delivering the vault to the operator's secondary devices (laptop, phone). Loom doesn't see those devices.

---

## 12. Out of Scope

The following are explicitly not part of the implemented surface:

- **Track 2 plugin bodies** (`prd-decompose`, `story-implement`, `story-review-human`). Scaffolding is present; plugin logic is not.
- **A1–A10 roadmap items**: plugin SDK, `prd-generate`, agent-driven reviews, brain (`decide-next`), crash recovery beyond source-replay, `merge-stories`, A2A endpoint, multi-host PRD-affinity, GitHub webhooks, docker sandbox. See `docs/prd/full.md` for details.
- **GitHub issue watcher.** Inbound mirror of GH issues into Lithos tasks is queued; see `docs/prd/github-issue-watcher.md`.
- **`task_reopen`.** Awaiting upstream Lithos primitive.
- **Hot-reload of TOML config.** Operator restarts the daemon.
- **Persistent event log.** Restart relies on source re-authority + subscriber idempotency.
- **Cost / token tracking.** Reserved finding prefix exists; emission does not.
- **Hardened concurrency semantics across hosts.** Claim-based competition via Lithos is sufficient for the current scale.
- **Containerised daemon.** Loom runs as a host process. Lithos and adjacent services may run in docker; Loom's host coupling (worktrees, CLI auth in `~/`, Templater macro on Obsidian Desktop's PATH) makes containerisation unhelpful.

---

## Appendix A: Worked Example — Capture a Task from Obsidian

1. Operator highlights "Review staging deploy" in any note, fires the capture-task hotkey.
2. The Templater macro shells out to `lithos-loom project list --format json` to populate the project dropdown, and `lithos-loom obsidian-sync show --format json` to learn the configured `tasks_file` path.
3. Modal opens; operator selects project `lithos-loom`, optionally fills priority and tags, submits.
4. Macro shells out to `lithos-loom task create --project lithos-loom --title "Review staging deploy" --no-insert`; CLI prints the new task_id.
5. Macro inserts a wikilink at cursor: `[[_lithos/tasks.md|Review staging deploy]] 🆔 lithos:<id>`.
6. Meanwhile: Lithos broadcasts `task.created` via SSE → the daemon's `LithosEventStream` receives it → `obsidian-projection` re-renders `<vault>/_lithos/tasks.md` with the new line.
7. Total elapsed: ~250–500ms from submit to projected line landing.

## Appendix B: Worked Example — Bidirectional Project-Context Edit

1. Operator opens `<vault>/_lithos/projects/lithos-loom/lithos-loom-project-context.md` in Obsidian and edits the body.
2. Saves. `ObsidianDirWatcher` polls every 250ms; on next tick, the body hash diverges from `sync_state.note_body_hashes[doc_id]`.
3. Watcher emits `obsidian.note.modified` with the operator's body and the local `lithos_version`.
4. `note-push` calls `lithos_write(id, content=body, expected_version=local_version)`.
5. Lithos returns `status=updated` with the bumped version.
6. `note-push` calls `lithos_read(id)` to refresh `lithos_version` and `lithos_updated_at` in the local frontmatter.
7. The dir-watcher detects the post-write file change but matches it against its last-known mtime + content hash → suppresses as a self-write.

If a separate agent had pushed a body change between steps 1 and 4, step 5 would return `status=version_conflict`. The resolver moves the operator's body to `_lithos/conflicts/lithos-loom.lithos-loom-project-context.<ts>.md`, writes canonical to the original path, and posts a `[Friction]` WARNING. The operator can diff the two files to recover their edit.
