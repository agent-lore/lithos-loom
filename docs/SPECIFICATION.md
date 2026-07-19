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
5. **Reopening a completed task.** Lithos exposes no `task_reopen` primitive. Untick (`[x] → [ ]`) posts a `[ReopenRequested]` finding instead.
6. **Plugin bodies for PRD decomposition, story implementation, story review.** Scaffolding exists in `src/lithos_loom/plugins/`; the implemented surface stops at the orchestration spine plus the Obsidian bridge. Generating PRDs, reviewing diffs, and brain-driven decisions are not implemented today.

### 1.3 Compatibility Policy (Pre-1.0)

1. **TOML schema evolves.** Field renames or removals require a documented migration step but are otherwise free.
2. **Event names are stable.** Subscribers depend on dotted event names (`lithos.task.created`, `obsidian.note.modified`); changing them is a breaking change.
3. **`result.json` schema is versioned.** Plugins ship a `schema_version` integer; incompatible changes bump it.
4. **Vault-projected file layout is stable.** `_lithos/tasks.md`, `_lithos/awaiting-review.md`, `_lithos/projects/<slug>/<file>.md`, `_lithos/conflicts/<slug>.<file>.<ts>.md` are documented locations operators query and grep against.

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
│  ┌────────────▼──────────────┐  ┌────────────▼─────────────┐  ┌───────▼───────────┐  │
│  │ route-runner child         │  │ obsidian-sync child       │  │ github-watcher    │  │
│  │  (enabled when [[routes]]) │  │  (enabled when           │  │  child (enabled   │  │
│  │                            │  │   [obsidian_sync]        │  │  when             │  │
│  │  Sources:                  │  │   is present)            │  │  [github_watcher] │  │
│  │   LithosEventStream        │  │                          │  │  enabled=true)    │  │
│  │                            │  │  Sources:                │  │                   │  │
│  │  Subscribers:              │  │   LithosEventStream      │  │  Sources:         │  │
│  │   one RouteRunner per      │  │   LithosNoteStream       │  │   LithosNoteStream│  │
│  │   [[routes]] stanza        │  │   ObsidianFSWatcher      │  │   GitHubIssue     │  │
│  │   (claim-bound)            │  │   ObsidianDirWatcher     │  │     Watcher       │  │
│  │                            │  │                          │  │                   │  │
│  │                            │  │  Subscribers (per        │  │  Subscribers      │  │
│  │                            │  │   configured action):    │  │   (auto-wired):   │  │
│  │                            │  │   obsidian-projection    │  │   github-issue-   │  │
│  │                            │  │   obsidian-status-       │  │     sync          │  │
│  │                            │  │     transition           │  │                   │  │
│  │                            │  │   obsidian-priority-     │  │                   │  │
│  │                            │  │     changed              │  │                   │  │
│  │                            │  │   obsidian-due-date-     │  │                   │  │
│  │                            │  │     changed              │  │                   │  │
│  │                            │  │   project-context-       │  │                   │  │
│  │                            │  │     projection           │  │                   │  │
│  │                            │  │   note-push              │  │                   │  │
│  │                            │  │   task-archive           │  │                   │  │
│  │                            │  │   noop                   │  │                   │  │
│  │                            │  │                          │  │                   │  │
│  │  In-process EventBus       │  │  In-process EventBus     │  │  In-proc EventBus │  │
│  └─────────────┬──────────────┘  └────────┬─────────────────┘  └─────────┬─────────┘  │
│                │                          │                              │            │
└────────────────┼──────────────────────────┼──────────────────────────────┼────────────┘
                 │                          │                              │
                 ▼                          ▼                              ▼
       ┌─────────────────┐         ┌────────────────┐           ┌────────────────────┐
       │ Lithos          │         │ Obsidian vault │           │ GitHub REST API    │
       │  /sse  /events  │         │  (fs)          │           │  api.github.com    │
       └─────────────────┘         └────────────────┘           └────────────────────┘
```

Each child runs its own EventBus instance. There is no inter-child IPC; all three children independently consume Lithos SSE. Restart safety relies on sources being re-authoritative (no persistent event log) and subscribers being idempotent.

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
`LithosEventStream` (running in the route-runner child) emits `lithos.task.*` events. Each `[[routes]]` stanza registers a claim-bound subscriber against `lithos.task.created`, `lithos.task.updated`, and `lithos.task.released` that requires every tag in `match.tags` to be present on the task (same semantic as the bus matcher and `is_human_actionable`). Since #86, editing a task's tags after creation re-triggers route pickup **without a daemon restart**: the tag edit arrives as `lithos.task.updated` (force-refreshed by the source into the full task envelope) and matches exactly as `created` would. `updated` is treated as "re-evaluate match," not "always run" — a plugin's own end-of-run `task_update` (e.g. `story-develop` writing `develop_*` metadata) fires `updated` but cannot self-trigger a re-run, because the task is already in the runner's in-process claim-dedup set; for `completes_task = false` routes the delivered story's `pr` gate additionally keeps it off `lithos_task_ready` across a restart (US11 retired the `loom_delivered` marker that used to do this). On match, the runner asks Lithos whether the task is actually dispatchable — `lithos_task_ready(tags=match.tags, project=…)`, the server-side ready frontier (**Epic G / US4**). A task behind an unsatisfied `blocks` predecessor, an unmet gate, or a cycle is simply absent from that frontier, and the runner defers; it no longer derives readiness itself from `metadata.depends_on`, so readiness (including the rule that a *cancelled* blocker keeps its dependents blocked) is computed once, server-side, and shared with every other agent. `lithos_task_ready` has no per-task filter, so the check is a membership test over a frontier narrowed to the route's tags and (when declared) the task's `metadata.project`, capped at `READY_QUERY_LIMIT` (500). A **full page** means the frontier was truncated, which makes absence from it meaningless — the runner then defers and logs a WARNING rather than trusting it. Deferring is the safe direction: the inverse mistake would dispatch a task whose blocker is still open, which is exactly what this gate exists to prevent. Once ready, the runner claims via `lithos_task_claim`, spawns the plugin subprocess, periodically renews the claim, and waits for `result.json`. It then reads only the `status` field:

- `succeeded` → `lithos_task_complete` — **unless** the route sets `completes_task = false` (§3.1), in which case the runner instead raises a first-class **`pr` gate** (**Epic H / US10**), records `metadata.pr_gate_id` on the story (provenance), and `lithos_task_release`s, leaving the task **open**. This is for PR-producing routes (e.g. `story-develop`) where success = "a reviewed branch + PR exist, awaiting human merge", not "done": completing on approval would close a github-linked issue for un-merged work. The gate is a `task_type="gate"` task carrying `metadata.gate_type="pr"` (repo / pr_number / pr_url / `required_state="merged"`), joined to the story by a `waits_on_gate` edge (from=gate, to=story), so the story is **structurally blocked** — absent from `lithos_task_ready` until the gate resolves, undispatchable with no Loom-private marker. The runner builds it from `result.json`'s `pr_url` (parsed with `parse_github_ref`) and, on success, records `metadata.pr_gate_id = <gate>` on the story so the merge resolver (§2.2 PR-gate resolver) owns the story's lifecycle. Gate creation is **best-effort** but load-bearing: if it fails, or the run is `succeeded` with no `pr_url`, no gate exists and the runner posts a loud `[Friction]` — there is no fallback (US11 retired `loom_delivered`), so a restart could re-develop the delivered story into a duplicate PR until a human merges the PR or creates the gate. Completion then happens on PR merge — github-issue-linked tasks via the issue close-mirror, and (since #87) non-issue tasks via the github-watcher's PR-gate resolver (see §2.2; otherwise the operator). The `pr` gate keeps a delivered story off `lithos_task_ready`, so a daemon restart's bootstrap (which re-emits every open task as `created`) does not re-develop it — the runner's readiness check defers it. (US11 replaced the old `loom_delivered` guard, which special-cased `completes_task = false` routes, with this single readiness guard for every route.) `lithos_task_complete` **returns the tasks whose last blocker this completion just cleared**, and the runner republishes each of them onto its bus as a synthetic `lithos.task.updated` (**Epic G / US6**) — so the next task in a chain dispatches off the completion itself instead of waiting for a Lithos round-trip. Routing the nudge through the bus (rather than re-entering the runner directly) means it reaches whichever route matches the unblocked task — usually a *different* route than the one that just finished — and it re-enters the same ready-check + collision-safe claim path, so a double-evaluation is harmless. Two boundaries: the nudge is confined to the route-runner child (children each run their own bus with no IPC), so a task completed elsewhere — e.g. by the github-watcher on PR merge — still relies on the event stream / restart bootstrap; and a failure to nudge is logged but never fails the already-completed task.
- `failed` → `lithos_task_release` + `[BlockerFailed]` finding (the error message is pulled from `error.message` if present).
- `interrupted` → `lithos_task_release`, no finding. When the result also carries a `resume` block (`resume_after` timestamp — e.g. a story-develop run that checkpointed on a provider usage limit), the runner additionally schedules an in-process re-dispatch: at `resume_after` it re-checks the task is still open, then re-claims and re-runs the plugin. Bounded at `MAX_RESUMES_PER_TASK` (3) re-dispatches per task per daemon process; on exhaustion the task stays open with a `[Friction]` finding. The schedule is in-memory only — a daemon restart loses it, but the event-stream bootstrap re-surfaces open tasks on startup anyway.
- Unknown / missing status → `lithos_task_release` + `[BlockerFailed]`.

Other `result.json` fields (`metadata_updates`, `artifacts`, `commits`, `spawned_tasks`, `exit_code`, `error.retriable`) are schema-validated but currently ignored. On plugin timeout, the runner sends SIGTERM with a grace period before SIGKILL.

**GitHub issue mirror (GitHub → Lithos).**
`GitHubIssueWatcher` (running in the github-watcher child) polls every repo flagged for watching on its `[github_watcher].poll_interval_seconds` cadence (default 60s). Watch eligibility is derived from project-context metadata: a doc with `github_watch_enabled = true` and a non-empty `github_repos` list enrols its slug → repo mappings (a project may map several repos, each polled independently). Discovery is one filtered call — `note_list(path_prefix="projects/", metadata_match={"github_watch_enabled": true})` — and each returned item carries its metadata, so the repo list and exclude filters are read without a follow-up per-doc fetch. The watcher subscribes to `lithos.note.{created,updated}` on the in-process bus so a `project enable-github <slug>` mid-run takes effect without a daemon restart. Per-repo `updated_at` cursors persist in a daemon-owned Lithos doc (default `projects/_lithos-loom-internal/github-watcher-state.md`, configurable) so cold restart doesn't re-walk every open issue. Coord-doc writes are CAS-protected: on `version_conflict` the watcher merges the just-observed cursor advances with the remote cursors (latest timestamp wins per repo) and retries, so concurrent writes don't lose progress. Per-repo polls split into two paths: **bootstrap** (no cursor yet for this repo) lists `state=open` with full `Link: rel="next"` pagination so every open issue surfaces in one cycle regardless of historical-closure volume; **incremental** (cursor present) lists `state=all` since the cursor with the same pagination so closes on previously-seen issues — their `updated_at` advances at close time — surface alongside fresh opens. Each issue surfaced this poll publishes one `github.issue.seen` event onto the in-process bus; the auto-wired `github-issue-sync` subscriber resolves an `<!-- lithos:<task_id> -->` linkage marker in the issue body, then takes one of these branches:

- Marker → open Lithos task: drift-sync only (title / body / labels — see below).
- Marker → open Lithos task, GH closed-completed: drift-sync + `lithos_task_complete`.
- Marker → open Lithos task, GH closed-not_planned: drift-sync + `lithos_task_cancel`.
- Marker → terminal Lithos task: drift-sync only (idempotent close mirror). If GH state transitioned from closed back to open and `metadata.github_state_snapshot != "open"` on the task, also post a `[ReopenRequested]` finding (de-duped via the snapshot field).
- Marker → missing task (operator force-deleted): create a fresh task; the marker writer overwrites the stale id.
- No marker, Lithos task carries `metadata.github_issue_url` for this URL: re-write the canonical marker on GitHub. No duplicate task.
- No marker, no matching task, GH open: `lithos_task_create` with `title=issue.title`, `description=issue.body`, `tags=issue.labels + ["github-issue"]`, `metadata={project, github_issue_url, github_issue_number, github_labels, github_state_snapshot=issue.state}`. Then write the canonical `<!-- lithos:<task_id> -->` marker into the issue body via `PATCH /repos/{owner}/{repo}/issues/{n}` — fetched fresh via `get_issue` immediately before the PATCH so an operator edit during the poll-to-PATCH window survives.
- No marker, no matching task, GH closed: skip (historic closures are not backfilled).

**Per-project exclude filters.** The watcher ships each event with the project's import-time filters, sourced from these metadata keys on the project-context doc (applied to every repo the project maps):

- `github_exclude_labels` (list) — drop the issue at import time if it carries any of these labels.
- `github_exclude_authors` (list) — drop if the GH author login matches (e.g. `dependabot[bot]`).

Filters apply only on the create branch (no marker + no matching URL + GH open). Already-linked tasks are unaffected if an exclude tag is added after import — the PRD explicitly locks "exclude is only at import time" so the operator never has a once-imported task quietly stranded.

**Dispatch contract (GH → Lithos).** The watcher source dispatches each issue inline to the `github-issue-sync` handler before advancing the persistent cursor — the bus path is reserved for tests that assert on queue contents. Cursor advancement is per-issue: the watcher walks GitHub's `updated_at`-ascending list, advances the in-memory cursor to each issue's timestamp only after dispatch succeeds, and halts the loop on the first exception so the next poll re-fetches starting from the failed boundary. Issues that failed dispatch are tracked in a `_stuck_issues: dict[str, set[int]]` map and retried by direct `github.get_issue` lookup at the top of the next poll — that path is independent of the cursor and the `state=` filter, so a bootstrap walk that's about to lose a closed-before-retry issue still gets it. The stuck set is persisted in the coord doc as `stuck:<owner>/<name>#<number>` rows alongside the cursor rows, so daemon restart between a partial reconciliation (e.g. `task_create` succeeded, marker PATCH failed) and the next retry preserves the repair record. CAS-write semantics protect both halves: deletion tombstones are tracked at function entry for both cursors and stuck rows so a `version_conflict` reload-then-merge doesn't resurrect locally-drained state.

**Dispatch contract (Lithos → GH).** The push direction uses the bus because `LithosEventStream` already serves multiple subscribers across child processes. The consumer loop classifies handler exceptions: permanent errors (`GitHubAuthError`, `GitHubRepoNotFoundError`) log `[Friction]` and drop without retry — retry won't help. Other `GitHubError` subclasses (transients — 5xx, network blips, rate-limit exhausted) retry with exponential backoff capped at 60s, up to 8 attempts (inter-attempt waits 2/4/8/16/32/60/60 s ≈ 3 minutes total before drop). Outages outlasting that budget are caught by the **periodic reconciliation sweep**: every `[github_watcher].reconcile_interval_minutes` (default 60) the child re-fetches open Lithos tasks plus completed + cancelled tasks resolved within `resolved_replay_days` (skipped entirely when `resolved_replay_days = 0`), filters to those carrying `metadata.github_issue_url`, and re-dispatches each one through the push handler. Terminal tasks dispatch both a synthetic `task.updated` (so title drift reconciles) AND the matching close event. The handler is idempotent (re-fetches GH before PATCH) so the sweep is a no-op in steady state. The sweep keeps recovery cadence within the configured interval even without a daemon restart; set to 0 to disable.

**PR-gate resolver (Epic H / US12+US13).** The same reconcile sweep, on the same `[github_watcher].pr_merge_poll_enabled` gate (default on), also resolves the first-class **`pr` gates** the runner raises (§2.2 above). For each open `task_type="gate"` task carrying `metadata.gate_type="pr"`, it reads the gate's `repo` / `pr_number`, finds the gated story via the `waits_on_gate` edge, fetches the PR, and acts on the state: **merged** → complete the **story first**, then the **gate** (both swallowing `task_not_found`), and post `[GateResolved]` on the story; **closed without merging** or **deleted (404)** → leave the gate **open** (never cancel — a cancelled gate is terminal and strands the story permanently unreachable through Loom) and post a one-shot `[DeliveredPRClosed]` on the story; **still open** → no-op; **transient `GitHubError`** → retry next sweep; a gate whose metadata won't parse → one-shot `[Friction]`. **Story-first ordering is non-negotiable:** completing the gate first would momentarily ready a story still tagged `trigger:story-develop`, and if story completion then failed the gate would be gone from the open set → story stranded open+ready+tagged → duplicate PR. The de-dup marker for the non-merged end-states (`metadata.develop_pr_merge_state` + `develop_pr_merge_url`, PR-scoped) is written **on the gate** (the durable node that stays open and would be re-swept), so a re-develop into a replacement PR (which updates the gate) re-evaluates. On **merge** the gate itself is completed, so no marker is needed. Completing all gated stories means the Lithos→GH push then closes any linked issue on merge (the push re-fetches before PATCH, so the ownership change is race-benign); `task_complete`'s `unblocked` return is ignored (story-first leaves nothing to dispatch, and the watcher runs no `RouteRunner`).

**Drift sync** (GH → Lithos, Slice 7.2). Every poll that matches a known Lithos task layers three checks on top of the close mirror:

- **Title drift** — `issue.title != task.title` → `task_update(title=issue.title)`.
- **Body drift** — `strip_marker(issue.body) != task.description` → `task_update(description=...)`. The `<!-- lithos:<id> -->` marker is never reflected into the Lithos task description.
- **Label diff** — read `metadata.github_labels` snapshot; compute `removed = old − new` and `added = new − old`; new tag set is `(task.tags − removed) | added`. Operator-added Lithos tags never in any GH snapshot survive untouched. The snapshot in metadata rolls forward to `issue.labels`.
- **State snapshot** — `metadata.github_state_snapshot` rolls forward to `issue.state` on every poll. Reopen detection compares the *prior* value before drift sync overwrites it.

All four drifts in one poll batch into a single `task_update` call. Steady-state polls (nothing changed) cost zero round-trips. Drift runs on **every** matched task, terminal or open — lithos#303 once forced a terminal-task skip, now lifted (#124), so a reopened terminal task's `github_state_snapshot` rolls forward in the same poll and the `[ReopenRequested]` finding stays one-shot.

**GitHub issue mirror (Lithos → GitHub, Slice 7.2).**
The `github-issue-push` subscription (auto-wired in the github-watcher child) consumes `lithos.task.{created,completed,cancelled,updated}` events from `LithosEventStream` on the in-process bus. The `task.created` event is the open-task snapshot replay surface at daemon startup — a Lithos rename that happened while the watcher was down only re-fires as `task.created` on restart, so the title branch consumes it identically to `task.updated`. The handler branches as follows:

- `lithos.task.completed` → fetch GH issue; if not already closed-as-completed, `PATCH state=closed state_reason=completed`.
- `lithos.task.cancelled` → same, with `state_reason=not_planned`.
- `lithos.task.updated` → if `task.title` differs from the current GH issue title, `PATCH title`.

Tasks without `metadata.github_issue_url` are filtered at the handler entry (the by-far-common case) and stay silent at INFO. Permanent GH errors (auth / repo-404) during the push surface as `[Friction]` log lines, not retries — a permanent failure shouldn't loop.

**Deleted linked issue self-heals (#69).** When the linked issue is gone — the GET returns `None` (404), or the PATCH raises the issue-specific `GitHubIssueNotFoundError` (a deletion racing the GET; distinct from a repo-404 so a deleted issue is never mistaken for a deleted repo and never drops the repo from the watch list) — the handler posts a one-shot `[LinkedIssueGone]` finding and writes a `metadata.github_issue_gone_url` marker scoped to the gone url. The handler entry skips a task whose marker matches its current `github_issue_url`, so subsequent `task.*` events stop re-probing the dead link (vs. a `[Friction]`/skip per event forever). The marker write's own `task.updated` event carries the marker (the event stream re-enriches with current metadata), so it lands on the skip and the heal does not loop. Re-linking the task to a new issue changes `github_issue_url`, the stale marker no longer matches, and the new link is re-evaluated.

Pull requests are filtered at parse time (presence of GitHub's `pull_request` field on the row). A 404 on a watched repo drops it from the in-memory watch list with a `[Friction]` log line; the next bus-driven refresh re-adds the slug if the operator fixes the typo. GitHub rate-limit responses (403 with `X-RateLimit-Remaining: 0`) trigger a sleep until `X-RateLimit-Reset`; a 403 with non-zero remaining surfaces as auth/permission error rather than retried indefinitely.

### 2.3 Restart and Recovery

Loom has no persistent event log. On restart:

- `LithosEventStream` and `LithosNoteStream` bootstrap (re-list and re-emit) and then resume via `Last-Event-ID`.
- `ObsidianFSWatcher` and `ObsidianDirWatcher` re-scan their watched files on startup; sync-state baselines are rebuilt incrementally as projection events fire.
- `obsidian-projection` writes the full file from scratch on every flush. Idempotent re-runs are no-ops thanks to atomic-write + content-hash dedup.
- `project-context-projection` re-reads each doc and rewrites if the full-file hash differs.
- `note-push` and `obsidian-status-transition` pre-check Lithos state before mutating (re-firing an already-completed task is a no-op).
- The route-runner does NOT reclaim stale claims from a previous process at startup. Stale claims age out via Lithos's own claim-expiry mechanism; a future run picks the task back up when the claim TTL elapses.

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
max_concurrency        = 4                     # parsed but NOT YET enforced (#85); no runtime cap today — a single route runs serially, multiple routes do not contend
log_level              = "info"                # debug | info | warning | error
retain_failed_workdirs = true                  # keep failed work-dirs for triage

# ── Projects (host-local automation registry) ─────────────────────────
#
# Projects exist in Lithos when a project-context doc lives at
# `knowledge/projects/<slug>/`. This TOML registers host-local automation
# config for projects this host should act on. `repo` is the only
# required field. `claude_config` and `codex_config` are parsed and
# stored but not yet consumed by any shipped plugin body.

[projects.<slug>]
repo          = "/abs/path/to/repo"
claude_config = "/home/you/.claude-lithos"     # optional, parsed but unused today
codex_config  = "/home/you/.codex-lithos"      # optional, parsed but unused today

# ── Routes (claim-bound subscribers) ──────────────────────────────────
#
# Each [[routes]] stanza is a claim-bound subscriber that listens to
# lithos.task.created, lithos.task.updated, and lithos.task.released. A
# task matches when every tag in match.tags is present on the task. The
# runner claims matching tasks, invokes `command` as a subprocess, and
# reads only `status` from the resulting result.json to decide whether to
# complete or release (see §5). Other result.json fields are schema-
# validated but not yet applied. Adding a route's trigger tag to an
# existing open task arrives as task.updated and dispatches without a
# daemon restart (#86).
#
# Substitution tokens in `command`:
#   {{task_json}}    — path to the task envelope JSON (read-only)
#   {{work_dir}}     — per-task staging dir under orchestrator.work_dir
#   {{result_file}}  — path the plugin must atomically write
#   {{repo}}         — [projects.<slug>].repo for the task's metadata.project
#                      (one route serves all projects; unresolvable → finding)

[[routes]]
name = "prd-decompose"
command = "uv run python -m lithos_loom.plugins.prd_decompose --task-json {{task_json}} --work-dir {{work_dir}} --result-file {{result_file}}"
max_runtime_seconds = 1800
human_blocking = false  # if true, surfaced in Obsidian projection once claimed

[routes.match]
tags = ["trigger:prd-decompose"]  # task must carry ALL listed tags

# story-develop: {{repo}} resolves per task from [projects.<slug>].repo, so
# one route serves every project. Reviewer config comes from the
# project-context doc's develop_* metadata (§5.5). completes_task = false
# leaves an approved task OPEN for human merge (see §2.2).
[[routes]]
name = "story-develop"
command = "uv run python -m lithos_loom.plugins.story_develop --task-json {{task_json}} --work-dir {{work_dir}} --result-file {{result_file}} --repo {{repo}}"
max_runtime_seconds = 28800
completes_task = false

[routes.match]
tags = ["trigger:story-develop"]

# ── Subscriptions (fire-and-forget side effects) ──────────────────────
#
# Each [[subscriptions]] stanza is a fire-and-forget subscriber that
# consumes one or more event types, runs an `action` (a handler
# hand-wired in its hosting child), retries on failure with exponential
# or linear backoff, and posts a [Friction] finding on persistent failure
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
awaiting_review_file = "_lithos/awaiting-review.md"  # #113 PRs-awaiting-review note (relative)
resolved_ttl_days = 7                            # see §6.3 task-archive interaction
include_blocked   = true                         # project tasks Lithos reports as blocked
exclude_tags      = ["debug:trace"]              # suppress projection for these tags

# ── GitHub issue watcher (per-host gate) ──────────────────────────────
#
# Presence of this block AND `enabled = true` is the spawn gate for the
# github-watcher child. Only one host should have this enabled at a time
# (no Lithos-coordinated election; pick one host manually). The watcher
# uses `gh auth token` at startup to resolve a bearer token, so the host
# must have `gh` on PATH with the operator already logged in.
#
# Per-project enablement lives in metadata on the project-context doc;
# manage via `lithos-loom project add-github-repo <slug> <owner/name>` and
# `lithos-loom project enable-github <slug>` (§4).

[github_watcher]
enabled               = false                                  # spawn gate
poll_interval_seconds = 60                                     # incremental polls
coord_doc_path        = "projects/_lithos-loom-internal/github-watcher-state.md"
# Lithos doc the watcher uses to persist per-repo updated_at cursors.
# Must be a relative Lithos doc path (no leading `/`, no `..`).
resolved_replay_days  = 7
# How far back the embedded LithosEventStream replays resolved task
# events at bootstrap. A Lithos task that closes (or gets renamed) while
# the watcher is down is mirrored to GH on restart via the replay; the
# push handler is idempotent (refetches GH before PATCH) so a too-large
# window only costs harmless re-checks. Set to 0 to disable replay (the
# push handler then only fires for events that arrive live).
reconcile_interval_minutes = 60
# Cadence of the periodic Lithos→GH reconciliation sweep. Catches drift
# left over from outages longer than the in-memory retry budget — every
# interval the child scans Lithos for open + recently-resolved tasks
# carrying metadata.github_issue_url and replays each through the push
# handler. Set to 0 to disable the sweep.

# ── story-develop host-wide defaults (optional) ───────────────────────
#
# Per-tool default model for story-develop agents. The lowest-priority
# layer of the model-resolution precedence (§5.5): used when nothing more
# specific (per-agent metadata, per-task override, route-level CLI
# fallback) pins a model, keyed by each agent's resolved tool. Per-tool,
# not per-role, so a heterogeneous panel (#94) gives a claude coder and a
# codex reviewer each the right default. Daemon-mode only; standalone CLI
# runs pin models with their own flags. Model names are not validated
# (they drift) — a typo falls through to the agent's CLI default.

[story_develop]
# operator_github_login (#113): GitHub user to notify on PR delivery —
# requested as a reviewer, or assigned to the PR when they authored it.
operator_github_login = "your-github-login"

[story_develop.default_models]
claude = "opus"
codex  = "gpt-5.4"
```

### 3.2 Validation

`lithos-loom validate-config` parses, typechecks, and lists projects / routes / subscriptions. `validate-config --dry-run` additionally polls Lithos and prints which routes / subscriptions would fire for each currently-open task plus any orphans (tasks no route matches) and dead config (routes / subscriptions no task currently matches). Both forms exit non-zero on invalid TOML.

**Readiness in the dry-run (Epic G / US7).** The simulation reads the same server-side ready-queue the runner dispatches off (§2.2) — one `lithos_task_ready` sweep for the dispatchable frontier plus one `lithos_task_blocked` sweep for the reasons — rather than re-deriving readiness from `metadata.depends_on`, so the report cannot drift from runtime behaviour. A tag-matching task Lithos doesn't consider ready renders as `deferred (<kind>: <predecessor> (<status>))` using Lithos's own structured blocker vocabulary: **`task`** (predecessor still open — just waiting), **`gate`** (waiting on an unresolved gate), **`blocker_unsatisfiable`** (the predecessor was *cancelled* — this one needs operator intervention, not patience), or **`cycle`**. A task that is neither ready nor blocker-shaped (e.g. an `epic` / `gate` task, which is never dispatchable work) renders as `deferred (not on Lithos's ready frontier)`. Neither sweep has a per-task filter, so both are capped at `READY_QUERY_LIMIT` (500); if either comes back full the report prints a `⚠ … query limit` line, since the rows below may then be incomplete.

**Orphan / dead config vs deferred.** These are *routing* questions, so they key off whether a task's tags **matched** a route — not whether it would claim right now. A deferred task is routed and waiting, so it is **not** an orphan, and a route matching only deferred tasks is **not** dead config; reporting either would send the operator to fix routing that is already correct. Deferred tasks instead get their own informational summary section (`deferred tasks (N) — routed, waiting on Lithos's ready-queue`), with the per-task reasons in the table above.

`lithos-loom doctor` verifies the configured `vault_path` exists, `_lithos/` is creatable, and a probe write+read round-trip works. It probes the **Lithos task-graph extension** end to end (creates throwaway probe tasks — an `epic`, a blocker + dependent joined by a `blocks` edge, and a spawned follow-on — asserts `lithos_task_ready` / `lithos_task_blocked` honour the edge — including that a *cancelled* blocker keeps its dependent `blocker_unsatisfiable` — and that `task_type` / `lithos_task_spawn` round-trip; it then probes **gate** semantics (Epic H): a `pr` gate joined to a waiter by a `waits_on_gate` edge must withhold the waiter from `lithos_task_ready`, surface as a `kind="gate"` blocker in `lithos_task_blocked`, and — on `lithos_task_complete` — report and release its waiter; finally cancels the probe tasks). It also reads `lithos_list(path_prefix='projects/')` and warns about TOML `[projects.<slug>]` entries with no corresponding Lithos project-context doc. The same task-graph probe gates `lithos-loom run` (see §4.1).

---

## 4. CLI Reference

All commands accept `--config / -c <path>` to override discovery. JSON-emitting commands accept `--format / -f json|text`.

### 4.1 `lithos-loom run`

Starts the daemon: supervisor + per-domain children. Foregrounded process; SIGINT / SIGTERM trigger graceful shutdown — the supervisor signals children to stop, in-flight plugin subprocesses are cancelled, and the supervisor waits up to a timeout before SIGKILLing any child that didn't exit. Cancelled plugins that don't write a result file trigger the contract-violation release path; claims may also be left to age out via Lithos's claim TTL.

**Boot gate (Epic G).** Before starting the supervisor, `run` runs the same task-graph capability probe as `doctor` (a real Lithos round-trip) and **refuses to start** (exit non-zero) if the extension is missing / broken — the runner schedules dependencies via Lithos's server-side ready-queue, so an incompatible server must surface at boot, not mid-PRD. This means the daemon also won't start while Lithos is unreachable or mid-restart (the probe reports `lithos_unreachable`); re-run once Lithos is back.

```
lithos-loom run [-c config.toml]
```

Exit codes: `0` clean exit, non-zero on child crash before shutdown or SIGKILL after timeout, or when the boot gate refuses (task-graph extension unavailable).

### 4.2 `lithos-loom validate-config`

```
lithos-loom validate-config [-c config.toml] [--dry-run]
```

- Plain form: parse, validate, print `OK:` summary (agent_id, lithos_url, projects, routes, subscriptions).
- `--dry-run`: also fetch open tasks from Lithos and print routing / subscription dry-runs. Useful before introducing new routes. Each task's route row is `✓ (claim)` (would fire), `deferred (<reason>)` (tags match but Lithos doesn't consider it ready — see §3.2 for the reason vocabulary), or `—` (no match).

### 4.3 `lithos-loom doctor`

```
lithos-loom doctor [-c config.toml]
```

Probes vault writability, the Lithos task-graph extension (the `task_graph_extension` capability check the boot gate keys off — §4.1), and the Lithos project surface. Each check prints PASS/FAIL with an actionable message. Non-zero exit if any check fails.

### 4.4 `lithos-loom config`

```
lithos-loom config --show [-c config.toml]
```

Prints the merged effective config. Useful for verifying config discovery picked the right file.

### 4.4a `lithos-loom gates`

```
lithos-loom gates [-c config.toml]
```

Read-only inventory of open **`pr` gates** (Epic H — `task_type=gate`, `metadata.gate_type=pr`). For each open gate it prints the gate id, the watched PR (`owner/repo#number`), the story the gate blocks (its `waits_on_gate` waiter), the waiter's status, and a one-word **health** classifying the wiring the gate resolver (§4 github-watcher) depends on:

- `ok` — open waiter + parseable PR metadata (awaiting merge, as intended).
- `orphan` — no `waits_on_gate` edge, so the gate blocks nothing.
- `malformed` — PR metadata missing/ill-typed, so `parse_pr_gate` can't read a PR to watch; the waiter stays blocked forever.
- `waiter-gone` — the `waits_on_gate` edge points at a task that no longer exists.
- `waiter-resolved` — the waiter is already completed/cancelled while the gate is still open.

Precedence when several apply: `orphan` → `malformed` → `waiter-gone` → `waiter-resolved` → `ok`. The listing closes with a two-line summary footer: a headline count (`N open pr gates: H healthy, A need attention`) and a per-health breakdown (`by health: 2 ok, 1 orphan, …`) counting each health class present, in the order `ok` → `orphan` → `malformed` → `waiter-gone` → `waiter-resolved` (zero-count classes omitted). An empty inventory prints just `no open pr gates`. Non-mutating (one open-task sweep plus a per-gate edge/waiter read; no GitHub round trip). Exit codes: `0` on a successful listing regardless of gate health; `1` if the config can't load or Lithos is unreachable.

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
- Indented children build a task graph out of Lithos's first-class edges: the parent becomes an `epic` and each child is created with `parent_task_id` (a `parent_child` edge). Siblings are parallel by default — that is simply the absence of a `blocks` edge between them. A `[sequential]` token on the parent chains its children instead: each child is created with `depends_on = [previous sibling]`, one `blocks` edge apiece. Tasks are created in document order, which satisfies Lithos's "predecessor / parent must already exist" rule.

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

### 4.10 `lithos-loom project add-github-repo` / `remove-github-repo`

```
lithos-loom project add-github-repo    <slug> <owner/name> [-c config.toml]
lithos-loom project remove-github-repo <slug> <owner/name> [-c config.toml]
```

Map / unmap a GitHub repo for the issue watcher by editing the `github_repos` metadata list on the canonical project-context doc. A project may map several repos (call `add-github-repo` once per repo); each is polled independently. `add` validates `owner/name` against GitHub's rules at CLI time — a malformed value exits 2 before any Lithos write — and is idempotent if the repo is already present. `remove` is idempotent if the repo is absent; removing the last repo is allowed (the project is unmapped) and warns if watching is still enabled.

The watcher does not begin polling until `enable-github <slug>` sets `github_watch_enabled = true`.

### 4.11 `lithos-loom project enable-github`

```
lithos-loom project enable-github <slug> [-c config.toml]
```

Sets `github_watch_enabled = true` on the project-context doc, enabling polling. Requires a non-empty `github_repos` list (exit 2 if empty, with the actionable error pointing at `add-github-repo`).

### 4.12 `lithos-loom project disable-github`

```
lithos-loom project disable-github <slug> [-c config.toml]
```

Sets `github_watch_enabled = false`. The `github_repos` list is preserved so re-enabling later doesn't need `add-github-repo`. Disabling stops new polls for the project at most one poll interval later (in-flight events for that slug still drain).

### 4.13 `lithos-loom project migrate-github-tags`

```
lithos-loom project migrate-github-tags [--dry-run] [-c config.toml]
```

One-shot migration from the legacy tag-based scheme (`github-repo:` / `github-watch` / `github-exclude-*` tags) to the metadata keys above. Scans every project-context doc and, for any still carrying github tags, writes the derived metadata and strips the tags in one CAS write per doc (multiple legacy `github-repo:*` tags collapse into the `github_repos` list). Idempotent; `--dry-run` previews without writing. Exit 1 if any doc fails its CAS retries.

### 4.13 `lithos-loom obsidian-sync show`

```
lithos-loom obsidian-sync show [-f text|json] [-c config.toml]
```

Prints the resolved `[obsidian_sync]` block. Used by the capture-task macro to discover the configured `tasks_file` path at runtime, so vaults that customise it get the wikilink target right without editing the macro.

### 4.14 `lithos-loom develop list` / `attach` / `dump` / `prune`

```
lithos-loom develop list   [-f text|json] [-c config.toml]
lithos-loom develop attach <run-id|task-id> [--once|--wait|--stream] [-c config.toml]
lithos-loom develop dump   <run-id|task-id> [-c config.toml]
lithos-loom develop prune  [--dry-run] [-f text|json] [-c config.toml]
```

Mostly read-only observability over in-flight `story-develop` runs (#88) — `prune` is the one mutating command. Discovery is zero-state: it scans the orchestrator `work_dir` for the `<work_dir>/<task_id>/<run_id>/` layout the route-runner + plugin produce, reads the per-round `handoff/` files, and queries `docker` for the run's `loom-develop-<run_id>-*` agent containers (the **active** agent is the one with a live `claude`/`codex` process — a turn is one `docker exec`). The route-runner **reaps the work dir on success**, so these commands observe **in-flight + failed/interrupted** runs; a succeeded run's dir is gone (its outcome is the `[DevelopResult]` finding).

- **`list`** — table (or `json`) of inspectable runs: run id, task id + title, current round, the active agent (or `idle` / `done`), and an `updated` timestamp (the run's last on-disk activity — the newest mtime across the run dir, its `handoff/` dir + handoff files, and any terminal `conversation.md`, since a fresh round handoff bumps the `handoff/` dir, not the parent; this is also the newest-first sort key; local wall-clock in text, raw epoch `mtime` in `json`). The title is the one **this run started with** — story-develop snapshots the task envelope into the run dir at start, so an older retained run keeps its own title even after the task is re-dispatched (the shared per-task `task.json` is overwritten each dispatch); it falls back to the per-task file for in-flight runs predating the snapshot.
- **`attach <run-id|task-id>`** — follow a live run, printing each handoff as it lands plus the current round + active agent, until the run reaches a **terminal state**, then a one-line outcome summary. Following keys on terminal *state* — the recorded **outcome** (`state.json` with a status) — **not** agent liveness, so it spans both the **startup window** before the first container comes up (the old container-liveness check exited there instantly as "done") and the commit / test-gate / teardown after the last agent turn. An **approved** verdict is not yet terminal in daemon mode: PR delivery (branch push, Copilot round, `result.json` write) runs *after* the dialogue approves — `develop()` writes the approved `state.json` and returns, then `__main__` calls `deliver()` — so attach follows through a distinct **"delivering PR…"** phase rather than exiting on the bare verdict (which would re-open the false-done window). The real end of an approved run is its `result.json` landing (a `succeeded` status in the shared per-task dir, distinguishing it from a prior retained run) or the success reap. The terminal signal is deliberately the outcome and not `conversation.md`: the plugin writes the log *first* and `state.json` *after*, and it also force-removes the agent containers *before* either, so attach **grace-polls** through the window where the containers are gone but the outcome isn't written yet rather than declaring a crash (and `--wait` / the summary can always read the recorded status). On **success** the route-runner reaps the whole work dir (taking `state.json` with it), and a fast run can be written-and-reaped between two polls; attach then recovers the outcome from the plugin's host-persistent **completion store** (the idempotency record, written before the reap and never removed by the route-runner), located by this run's id via the recorded conversation-log path — so it is found regardless of whether the run used the default (task-id) idempotency key or an explicit `--idempotency-key`, and a missed `state.json` on an approved run is still reported as approved. The outcome summary reflects the recorded/recovered status (`approved`, `NOT approved (max rounds reached)`, `failed`, `interrupted (re-run to retry)`, `stopped (stalled|dispute…|cost…)`) — and, so the line answers "did it finish, and how?" (#188), names the **delivered PR url** for an approved run (read from this run's `result.json`, recovered from the completion store on a reap) and **why** a stopped run stopped (the failure reason from `state.json`) — notes a reaped run whose success couldn't be recovered (e.g. a non-default idempotency key), notes a crash only when the containers vanished and no outcome was ever recorded across the grace window, names that an approved run's **PR delivery failed** with its reason (#194) — when `deliver()` raised before a PR opened, the daemon marks the run's private `run_dir/delivery.json` failed, so attach reports it terminally at once (and `--wait` exits non-zero) instead of waiting out the delivery deadline — or notes that an approved run's **PR delivery did not complete** — the `"delivering PR…"` phase is bounded by the **delivery deadline the daemon records** before delivery starts (`run_dir/delivery.json` = now + its own `copilot_timeout` + `coder_timeout` budget; a generous flat fallback when no deadline was recorded). Delivery runs host-side *after* the agent containers stop, so their absence is not a crash signal, and the bound is the daemon's actual budget — so a healthy slow delivery (a long Copilot round + fix turn) is **never** falsely timed out, while a crash/reboot that leaves `result.json` unwritten is reported as delivery-incomplete rather than followed forever (#189). Handoff name/body are agent-writable (RW bind mount), so the text view strips terminal control/escape bytes and the read is bounded in both per-file size and per-poll file count (the JSON `--stream` path is escape-safe via `json.dumps`). Modes (mutually exclusive): `--once` prints a single snapshot and exits; `--wait` blocks **quietly** — first until the run *appears* (so it can be invoked immediately after dispatch, before the run dir is seeded) **or until the completion store shows it already finished without an observable run dir** (an idempotency replay writes `result.json` and exits *before* creating the dir; a fast success is reaped between two polls — either way `--wait` recovers the outcome from the store and reports it rather than hanging forever, #196), then through PR delivery to the terminal state — and prints **only** the outcome, exiting non-zero unless the run was `approved` **and fully delivered** (a delivery that timed out (#189) or failed (#194) exits non-zero too, so `attach --wait && gh pr view` can't race a PR that never opened) (a scripting primitive); `--stream` emits newline-delimited JSON events (`{"event":"state"…}` / `{"event":"handoff"…}` / a terminal `{"event":"outcome"…}`) for machine consumption. Read-only; `Ctrl-C` exits cleanly.
- **`dump <run-id|task-id>`** — print the assembled conversation log: `conversation.md` for a finished run, else the per-round handoffs assembled live (the plugin writes `conversation.md` / `state.json` only at run end). Keyed by run id or task id.
- **`prune`** — delete the on-disk run-state dirs of **finished** runs (the failed / interrupted dirs that accumulate, since succeeded runs are already reaped). A run is *finished* once it has written its terminal `conversation.md` (the plugin does this only after the agent containers stop). Container state alone is not used as the signal: agent containers run with `--rm`, so a finished run and a run still in its **startup window** (handoff dir seeded, containers not yet started) both show zero containers — pruning on that would delete a live run out from under the daemon. Every in-flight run is therefore left untouched. Emptied per-task dirs are reaped too. `--dry-run` lists what would be removed without deleting; `-f json` reports each run with a `pruned` flag (and an `error` field when a deletion fails). A failed deletion is reported as an error, never as success, and exits non-zero.

Live `docker exec` transcript streaming (watch the agent think token-by-token) is a deferred follow-up; `attach --stream` emits handoff/state/outcome *events*, not the raw agent token stream. When `docker` is absent the file-based views still work; the active agent shows as `—`.

### 4.15 `lithos-loom develop review` — review-only mode (#154)

```
lithos-loom develop review <pr|range|branch>
    [-p|--profile standard] [--reviewer NAME ...]
    [--ac TEXT | --ac-file PATH] [--base REF]
    [--repo PATH] [--json PATH] [--keep-worktree] [-c config.toml]
```

Runs *just* the reviewer panel + deterministic gate against a change that **already exists** — no coder, no fix loop — and emits a consolidated report. Where `develop()` *produces* a change (worktree off a base, coder commits onto it), review-only *consumes* one: it materialises a **detached** worktree at the change's head, resolves the base separately, runs the resolved profile's check-set once on that tree, runs each reviewer once (round 1), and reports. It drives the **same** `run_panel_round` primitive the develop loop uses, so the two review paths can never diverge. See [ADR 0004](adr/0004-review-only-mode.md) and [`docs/cli/review.md`](cli/review.md).

- **Change input** (auto-detected from the argument):
  - a **GitHub PR** — `#142`, bare `142`, or a PR URL — resolved via the typed GitHub client (`GitHubClient.get_pull_request`: head sha, base branch, title, body — one seam shared with the watcher family, ADR 0008); the base is derived as the merge-base of the base branch and the head (the true diff base, avoiding spurious deletions when the base advanced), and the PR head + base refs are fetched so both commits are local. Works for fork PRs (`pull/N/head`). The PR *number* selects the PR against the local checkout's origin (a bare number or a URL both resolve there).
  - an explicit **`base..head` ref range**.
  - a **local branch / ref** — base is its merge-base with `main` (override with `--base`).
- **Acceptance criteria** (the reviewer's brief) precedence: `--ac-file` > `--ac` > the **PR body** (for PR input). A bare range / branch with no AC is rejected — a reviewer with no criteria is near-useless.
- **Panel / profile.** `--profile` selects the persona panel + check-set (default `standard`); `--reviewer NAME` (repeatable) overrides the personas. The deterministic gate runs the profile's full check-set (fast + candidate, since review is a one-shot) on the head tree.
- **Output.** A human-readable markdown summary (grouped by reviewer, plus a gate line) to stdout; `--json PATH` writes the structured report (the stable contract the review-correctness eval harness, #183, consumes). No GitHub / Lithos side effects in this slice — posting findings to a PR / Lithos task is a deferred follow-up. The worktree is removed on exit unless `--keep-worktree`.
- **Exit code** is non-zero when the review is **blocking** (any reviewer finding at/above its threshold, an incomplete panel, or a required gate check blocking — the *same* floor `develop()` applies).

Needs `docker` + the agent CLIs (`claude`/`codex`) + `gh` on the host, like a develop run; it is host-only, not part of the hermetic `make check`.

### 4.16 `lithos-loom eval review` — review-correctness eval harness (#183)

```
lithos-loom eval review [--case ID] [-k N] [--bar FLOAT] [--cases-dir PATH] [--report-dir PATH]
```

A **seeded-defect benchmark** that measures how reliably the reviewer panel catches real defects — review correctness as a *number*, not a vibe. Each **case** (a directory under `evals/review/cases/<id>/` with a `case.toml` + `ac.md`) pairs a known-buggy `base..head`, the acceptance criteria the reviewer receives, and the expected finding(s) a correct review must surface. The harness runs review-only mode (§4.15) against the case's head **K times** (default 5) and reports, over the K runs: **catch-rate** (every expected defect surfaced), **severity-correctness** (caught at/above the expected band), and **false-positive rate** (on the paired known-good head). Catch and FP are each shown as a count over K plus a **Wilson 95% confidence interval** (#182), so a rate is read with its sampling error — a low-K run cannot prove a clean panel (`5/5` still spans ~`57-100%`). A sample whose reviewer turn **crashed** (no verdict — `status` `invalid` / `not-run`, e.g. a provider usage limit) is **errored**: excluded from the catch / FP denominators and reported as `+Nerr`, so agent flakiness never masquerades as a review miss (a genuine catch is still counted even if a panel peer crashed). A case **passes** at `catch-rate ≥ --bar` (default 0.8) over the *valid* samples — agents are stochastic, so it's a rate, not a single pass/fail; a case with zero valid samples cannot pass. Matching defaults to a **mechanism LLM-judge** (`--judge`, on by default): it confirms each finding describes the case's *specific* defect mechanism — vetoing a same-topic false hit and rescuing a keyword-less correct catch — because a purely structured match (file + ≥1 keyword) over-counts when the known-good shares the defect's topic (the first live run measured 100% FP that way); `--no-judge` falls back to the cheap structured matcher. `--report-dir DIR` retains every run's report at `DIR/<case>/<variant>-<i>.json` plus a per-case `DIR/<case>/summary.json` (rates, per-sample booleans, CIs) so a costly K-sample run is re-analysable for variance without re-scoring. Seeded with the **#180/#171** case; every future escape becomes a regression case. **On-demand only — never part of `make check`** (it spends real tokens and needs the host sandbox + agent CLIs); the harness *logic* is unit-tested hermetically with the review function stubbed. See [ADR 0005](adr/0005-review-correctness-eval-harness.md) and [`evals/review/README.md`](../evals/review/README.md).

---

## 5. Plugin Contract

Plugins are subprocesses invoked by a route-runner. They receive a small CLI surface and write an atomic `result.json`.

### 5.1 Invocation

```
<command> --task-json <path> --work-dir <path> --result-file <path>
```

- `--task-json`: read-only JSON file. Today its contents are `{"task": <event-payload>}` — the bus event's payload (a Lithos task envelope) wrapped under a single `task` key. The resolved project entry from the local TOML is **not** included in the file; a plugin that needs the project's on-disk repo path uses the `{{repo}}` command token (below) or loads the TOML itself.

  **`{{repo}}` substitution.** Beyond the three path tokens, a route `command` may carry a `{{repo}}` token. The runner resolves it from `[projects.<slug>].repo` keyed by the claimed task's `metadata.project`, before the plugin forks — so one route serves every registered project, and the repo a plugin acts on is derived from the task's own project rather than hard-coded per route. A `{{repo}}` route whose task has no `metadata.project`, or whose slug isn't in `[projects.*]` on this host, is released with a `[BlockerFailed]` finding (`route misconfigured: …`) and never run. Routes without the token don't require a project.
- `--work-dir`: per-task staging directory at `<orchestrator.work_dir>/<task_id>/`. The plugin owns the tree; the runner reads only the result file.
- `--result-file`: path the plugin must write atomically (temp file + fsync + rename). Partial files must never be observable.

Substitution tokens (`{{task_json}}`, `{{work_dir}}`, `{{result_file}}`) in the route's `command` are filled in by the runner before fork.

### 5.2 Result Schema

The full schema is at `docs/result-schema.json` (JSON Schema Draft 2020-12). Required fields: `schema_version` (const 1), `task_id`, `status`, `exit_code`.

```json
{
  "schema_version": 1,
  "task_id": "uuid",
  "status": "succeeded",
  "exit_code": 0,
  "started_at": "2026-05-29T10:00:00Z",
  "finished_at": "2026-05-29T10:05:00Z",
  "worktree": "/abs/path or null",
  "artifacts": { "key": "rel/path or /abs/path" },
  "commits": ["40-char-sha"],
  "pr_url": "https://github.com/o/r/pull/170",
  "spawned_tasks": ["task_id"],
  "metadata_updates": { "pr_url": "https://..." },
  "error": null
}
```

For a failed run, replace `status` with `"failed"` (or `"interrupted"`) and set `error` to an object with the required keys `category` (one of `config`, `environment`, `input`, `agent`, `git`, `github`, `lithos`, `delivery`, `usage_limited`, `internal`) and `message`, plus the optional boolean `retriable`. No other `error` keys are accepted. (`delivery` (#194) flags an approved dialogue whose PR delivery failed before a PR opened — see §5.5.)

An `interrupted` result may additionally carry a `resume` object marking the interruption as retryable:

```json
{
  "resume": {
    "resume_after": "2026-06-12T15:00:00+00:00",
    "run_id": "abc12345",
    "coder_session": "uuid",
    "reviewer_sessions": { "code-quality": "uuid" }
  }
}
```

`resume_after` (required within the block) is the earliest instant a re-run is expected to succeed — the provider's parsed reset time, or a fixed fallback delay when no hint was parseable. The session ids let a future run resume its on-disk transcripts from the retained work dir.

**What the runner does with each field today:**

| Field | Status |
|---|---|
| `schema_version`, `task_id`, `status` | Required by schema; `status` drives the runner's branch (see §2.2). |
| `error.message` | Used as the `[BlockerFailed]` finding text when `status == "failed"`. |
| `resume.resume_after` | Schedules the in-process re-dispatch on `status == "interrupted"` (see §2.2). |
| `pr_url` | Optional. The PR an approved run delivered (story-develop, #188). On a `completes_task = false` success the **runner** reads it to raise the `pr` gate that blocks the delivered story (Epic H / US10, §2.2) — a success with no `pr_url` creates no gate and posts a loud `[Friction]` (no fallback since US11). It is also read (offline) by `develop attach` to name the PR in the terminal summary, and recorded under the idempotency key so a reaped success still surfaces it. |
| `rounds` | Optional. The implement/review round count the run reached (story-develop, #196). The **runner** ignores it; `develop attach` reads it (offline / from the completion store) so a reaped or idempotency-replayed run — whose `state.json` is gone — still names the round count in its terminal summary. |
| `run_id` | Optional. The run id that produced this result (story-develop, #198). The **runner** ignores it; `develop attach` uses it to bind the SHARED per-task `result.json` to **this** run for terminal detection — so a prior run's leftover `succeeded` (a best-effort reap left it behind) or `failed` (a best-effort delivery-marker write that failed) result can't be mistaken for the current run's delivery. |
| `exit_code`, `started_at`, `finished_at`, `worktree`, `artifacts`, `commits`, `rounds`, `run_id`, `spawned_tasks`, `metadata_updates`, `error.category`, `error.retriable`, `resume.run_id`, `resume.coder_session`, `resume.reviewer_sessions` | Schema-validated but **currently ignored** by the runner. Plugins may populate them; they have no effect on Lithos today. |

### 5.3 Runner Lifecycle

The route-runner enforces `max_runtime_seconds` (per-route config). On timeout, it sends SIGTERM and waits a grace period; if the plugin hasn't exited, it sends SIGKILL. Result-file absence after exit is treated as a contract violation: the runner posts `[BlockerFailed] route <name>: plugin contract violation: <detail>` and releases the claim.

`retain_failed_workdirs = true` keeps the work directory for triage on failure; on success the work-dir is removed.

### 5.4 Bundled Plugins (scaffolded)

`prd-decompose` is present under `src/lithos_loom/plugins/` as a Python module with a prompt file. Its body is a stub; it does not yet produce real `result.json` output. The route-runner code path is the load-bearing piece exercised by tests. (The former `story-implement` / `story-review-human` stubs were removed — `story-develop` is the one implement→review→PR path; see §5.5.)

### 5.5 story-develop (shipped)

`story-develop` runs the full implement → review → fix → approve loop with containerised agents (one persistent coder session + an N-reviewer panel; per-round commits, an objective multi-check deterministic gate (an ordered check-set run in throwaway containers; the default set is the single `test` check — #131/ADR 0003 §4) whose per-check result + a `git diff --stat` are injected into **both** the coder and the reviewer prompts each round (#136/ADR §6), usage-limit reactions, optional PR delivery with an autonomous Copilot review round). The full design is `docs/prd/archive/story-develop.md` (shipped + archived); the standalone CLI surface is `python -m lithos_loom.plugins.story_develop --help`.

**Daemon mode.** Passing `--task-json` (with `--work-dir` and `--result-file`) switches the plugin to the route-runner contract:

```
uv run python -m lithos_loom.plugins.story_develop \
    --task-json {{task_json}} --work-dir {{work_dir}} --result-file {{result_file}} \
    --repo {{repo}}
```

- `--repo` takes the runner's `{{repo}}` token (§5.1), resolved per task from `[projects.<slug>].repo` keyed by `metadata.project` — so one route serves every registered project. (An absolute path also works if you want a route pinned to one checkout.)
- The task (title, body, `metadata.acceptance_criteria`) comes from `task.json`; `--description` / `--task-id` / `--no-lithos` / `--complete-on-approval` / `--reviewer` / `--develop-config` are rejected in daemon mode.
- **Idempotency short-circuit (US-18).** `--idempotency-key KEY` dedups a daemon run (default: the task id from `task.json`). The first run under a key that ends `succeeded` records its `result.json` in a host-persistent store; a later run under the same key **replays that recorded payload verbatim and exits 0 without re-running** — no second agent loop, no second PR (checked before config resolution, the agent loop, and PR delivery). A record short-circuits only when it passes **all three** gates: (1) it claims success (`status == "succeeded"` **and** `exit_code == 0`); (2) it validates against the full `result.json` schema (`docs/result-schema.json`) — a record missing required fields like `schema_version` / `task_id`, or otherwise off-contract, is rejected even when it claims success, since replaying it would hand the runner an invalid result; (3) its `task_id` matches the task being run — so a reused key (or a tampered store) never replays one task's result into another's. A failed, interrupted, or malformed record is ignored so the task stays retriable (and only a succeeded run is ever recorded, so a failed/interrupted run leaves no marker). The recorder keys off the **explicit** `--idempotency-key` when given, not the task id. Store: one `<sha256(key)>.json` file per key under `$LITHOS_LOOM_IDEMPOTENCY_DIR`, else `$XDG_STATE_HOME/lithos-loom/story-develop/idempotency`, else `~/.local/state/lithos-loom/story-develop/idempotency` — on disk (not the per-task `work_dir`, which the runner overwrites each dispatch) so the short-circuit survives invocations and daemon restarts. The store is bounded: each write prunes it back to the newest `LITHOS_LOOM_IDEMPOTENCY_MAX_RECORDS` (default 10000) records, and evicting an old record just means a later dispatch under that key re-runs. **Trust boundary:** the store is plain JSON at operator-home privilege with no cryptographic integrity — write access to that directory is a trust boundary (the `task_id` binding blocks cross-task replay, but a local process that can write the dir can suppress/redirect a single named task's development). *Out of scope (follow-ups):* in-flight concurrent dedup (locking, while a first run is still going); uniform application across all plugins via the plugin SDK (US-14); the A2A-race path (US-37).
- **Config lookup.** Reviewer config is resolved from the project-context doc's metadata at `projects/<slug>/<slug>-project-context.md` (slug from `task.metadata.project`; fallback: lexicographically-smallest `project-context`-tagged doc under `projects/<slug>/`). Keys: `develop_reviewers` (pool of `{name, tool, block_threshold?, system_prompt?, fallback_chain?, model?, effort?}`), `develop_default_reviewers` (names that run when the task doesn't override), `develop_coder` (`{tool?, model?, effort?}`), `develop_fallback_chain`, `develop_max_rounds`, `develop_max_cost_usd`, `develop_image` (the sandbox container image for **every** agent + the test gate, e.g. `"ghcr.io/acme/dev:2026-06"`; default `ralph-sandbox:latest`), `develop_test_command` (test-gate command, **trusted as-is** — no auto-detection or tool-probe), `develop_test_gate` (bool; `false` excludes the **`test` check only** from the per-round check-set — it is a *test* escape hatch, not a whole-gate kill switch, so the deterministic `lint` floor still runs — #131/#132/ADR 0003 §10); `develop_review_profile` (the selected Review Profile name — #139; **also governs whether the `test` check blocks** — all canonical profiles declare it required, so a red test gate blocks by default. The legacy `develop_block_on_red` key is **removed** as of #140 — a lingering key is inert and emits a one-shot deprecation `[Friction]`). Per-task override: `task.metadata.reviewers` (names from the pool); `task.metadata.develop_model` / `task.metadata.develop_effort` (override the **coder's** model / reasoning effort for that one task — reviewer models stay project policy); `task.metadata.develop_image` (override the project's sandbox image for that one task — e.g. a task needing a heavier toolchain); `task.metadata.develop_test_command` / `develop_test_gate` (override the test gate for that one task); `task.metadata.develop_review_profile` (the per-task Review Profile — highest precedence, #139). With **no explicit reviewer selection** — no slug, no doc, no `develop_default_reviewers` / `task.metadata.reviewers`, or a populated pool without a default selection (pool membership does not auto-run) — the run's panel comes from the **resolved Review Profile** (#140 slice 2): the default `standard` yields its correctness + security personas, `thorough` all five (#137), and gate-only `minimal` keeps the built-in single `code-quality` reviewer + a `[Friction]` until the overrides slice wires a true zero-reviewer panel (the deterministic floor itself shipped in #140's floor slice). The one case that still degrades to the built-in single `code-quality` reviewer + a `[Friction]` is an **explicit** selection that resolves to **no known reviewers** (a name matching neither the pool nor a canonical persona) — a typo must not silently escalate to the profile panel. An explicit selection that *does* resolve wins outright.
- **Model + reasoning effort (#93).** `model` (e.g. `"opus"`, `"sonnet"`, or a full id) and `effort` (a reasoning-effort **level**: `low` / `medium` / `high` / `xhigh` / `max`) are configurable per agent: project-wide on `develop_coder` and per-reviewer in the `develop_reviewers` pool; per-task on the coder via `task.metadata.develop_model` / `develop_effort`. `effort` is **not** a token budget (`MAX_THINKING_TOKENS` is legacy and ignored by current adaptive-reasoning models) and there is **no universal cross-tool effort vocabulary** — Loom adopts **Claude's `--effort` levels as canonical**; each wired tool maps the canonical level onto its own mechanism. Codex (#94) has no effort flag — depth follows the model choice, so `effort` is **ignored for codex agents** (`model` is still honoured via `codex -m`); a not-yet-wired OpenCode would use `--variant high/max/minimal`. Both default to **the agent's default** (no `--model`, no `--effort`) — the plugin deliberately does not hard-pin a model string, so an agent upgrade is picked up without a code release. Standalone flags: `--coder-model` / `--coder-effort` and `--reviewer-model` / `--reviewer-effort` (the latter apply to every `--reviewer`; per-reviewer values need `--develop-config`). In daemon mode the standalone `--coder-model` / `--coder-effort` flags act as a route-level fallback that project/task metadata overrides. Below all of these, daemon-mode runs consult one final layer: a **host-wide, per-tool default model** in the loom TOML's `[story_develop].default_models` table (§3.1), keyed by each agent's resolved `tool` (`claude` / `codex`) — the lowest-priority `model` source, just above the agent CLI's own default. It is per-tool, not per-role, so a heterogeneous panel (#94) gives a codex reviewer and a claude coder each the default for their own tool; a tool with no configured default leaves that agent on its CLI default. There is no global-default counterpart for `effort`. Invalid-value handling differs by surface: **standalone CLI** flags **error** (non-zero exit) on bad input (empty/whitespace model, or an off-canonical effort level); **project/task metadata and the daemon-mode CLI fallback** drop an invalid value with a `[Friction]` finding and continue — daemon-mode config resolution never fails the run (so the effort flags are validated by `parse_effort`, not argparse `choices`, which would otherwise reject a bad route fallback at parse time). A malformed route fallback is surfaced with friction **even when metadata already supplies that field** (so a route-config typo isn't silently masked); the valid fallback value is *applied* only where metadata left the field unset.
- **Agent tools (#94).** Each agent's `tool` is `claude` or `codex`, declarable independently per coder (`develop_coder.tool` / `--coder`) and per reviewer (`develop_reviewers[].tool`), so a heterogeneous panel (e.g. `code-quality = codex`, `security = claude`) and an engine-switching `fallback_chain` both work. Codex runs as `codex exec --json --dangerously-bypass-approvals-and-sandbox` (first turn) / `codex exec resume <thread_id> …` (later turns) inside the same hardened per-turn container model (ADR 0002). Two codex specifics: (1) the session handle is **minted by the tool** — the `thread_id` from the first turn's `thread.started` `--json` event is captured and reused for resumes (claude's is a caller-supplied uuid); (2) the per-run config/transcript dir is `CODEX_HOME` (under the work-dir, never `/tmp`) with a single `auth.json` bind-mounted from `~/.codex`, and codex has no skills concept (it honours the worktree `AGENTS.md`). **Cost gap:** codex reports token usage, not USD, so codex turns contribute `0.0` to the `max_cost_usd` ceiling — for codex agents the ceiling is unenforced (a run stays bounded by `max_rounds` + per-turn timeout); a token→USD pricing map is a follow-up. When `max_cost_usd` is set **and** any participating tool (coder, a reviewer, or a reachable `fallback_chain` entry) can't meter USD, story-develop logs a one-time startup WARNING naming exactly those tools — so an operator who set a ceiling learns it only bounds the USD-reporting participants. The message is **capability-driven** off the Engine adapter's `meters_cost_usd` (ARCH-2/E4/#102), so a future non-metering engine is named automatically without a code edit; the ceiling checks themselves are untouched. Codex usage-limit *strings* are not yet captured (feasibility-gate G4), so a codex limit currently classifies as a generic `agent_error` rather than `usage_limited` — an engine `fallback_chain` still switches *to* codex on a claude limit, but switching *off* codex on a codex limit awaits the captured strings.
- **Engine adapter (ARCH-2).** Everything the plugin must know to run a specific tool — its capabilities (`meters_cost_usd` / `mints_session_handle` / `supports_effort`), container provisioning (config mount + env var, auth files, skills dir), the per-turn CLI argv (`cli_argv`, plus its `docker exec` wrapper `build_exec_argv`), the unified turn parse (`parse_turn`), and the session-transcript layout — lives on a per-tool **`Engine`** behind a registry (`engines.get_engine(tool)` / `is_supported` / `supported_tools`), so adding a tool is one adapter rather than new `tool`-string branches across the container / turn / transcript / config code. The capabilities **express** the decisions ADR 0002 + #94 already made; the adapter does not re-decide the session mechanism ([ADR 0002 addendum](adr/0002-story-develop-session-mechanism.md#addendum-arch-2--the-engine-adapter-expresses-this-mechanism-it-does-not-change-it)). The **bare** host-side argv `cli_argv(session_id=None)` (no `docker`, no session flags) is what the review-correctness eval judge (§4.16) runs off-container, so the in-container turn path and the host-direct judge call share one implementation of "invoke tool X and read its result."
- **Ecosystem-aware gate applicability (#133 / ADR 0003 §4).** When no explicit `develop_test_command` is set, the `test` check's command is auto-detected per ecosystem (Python / Node / Rust / Go, by marker file). Applicability is **declared, not inferred from absence**: a markerless / docs-only repo declares the gate not-applicable (so there is simply no gate), but a repo whose detected ecosystem *expects* a check that has **no runnable command** is treated as **expected-but-absent** — a *required* check there **blocks approval** (the coder prompt names it `EXPECTED BUT ABSENT`) instead of silently passing, so deleted or missing tests in a code repo cannot slip the gate. The per-ecosystem catalog of the canonical checks (`format` / `lint` / `typecheck` / `test` / `sast` / `dep-audit`) and the resolver that turns a *desired* set into concrete commands ship here as the foundation a Review Profile (#139) selects against; a *required* check with no mapping for the detected ecosystem fails resolution with an operator-actionable error rather than silently degrading. The first-class deterministic-finding ledger (stable ids, per-tool severity mapping) over these checks is #132.
- **Deterministic-finding ledger + live lint (#132 / ADR 0003 §5).** Finding-producing checks — today a live **`lint` (ruff)** check added as an extra **informational** check on every Python repo where ruff is present (it runs **regardless of `develop_test_gate`**, which scopes the `test` check only — disabling tests must not silently drop the lint floor) — have their JSON output parsed into a per-run **gate finding ledger**: each finding gets a stable `gate/<check>-NNN` id (a violation keeps its id across rounds; one that vanishes on a clean re-run is closed `fixed`; identity is `check + rule + file/line + package`, so two packages sharing a CVE stay distinct), a severity **mapped from the tool's native level** (ruff `W`→minor else major; bandit HIGH→critical / MEDIUM→major / LOW→minor; pip-audit→major), and is **gate-owned** — the coder cannot mark a `gate/*` finding fixed (closure is re-running the check green). These structured findings are injected into the coder + reviewer prompts (in place of a raw output tail) and into the `[DevelopResult]` finding, and persisted to `gate_ledger.json` (survives a resume). A finding-producing check runs in a JSON / don't-fail-on-findings mode so the tool's exit code never decides blocking (its severity does, ADR §5). The `lint` check began as **informational** (#132); per-profile check-set membership is #139 and the **required floor went live in #140's floor slice** — `lint` now blocks `standard` when the ledger holds a `major`+ ruff finding (see Review Profiles below). `bandit` / `pip-audit` adapters + severity tables ship too and light up once a profile selects those checks (the tools live in `ralph-sandbox` as of its SAST update).
- **Review Profiles (#139 / ADR 0003 §1–§3).** A **Review Profile** is a named bundle of {panel personas, deterministic check-set (per-check state + stage), blocking policy} — the dial for review strength. Three canonical profiles ship: **`minimal`** (`strength_rank` 10; gate-only panel; required checks `format` / `lint` / `test`), **`standard`** (rank 20, the **default**; panel correctness + security; required `format` / `lint` / `typecheck` / `test`, informational `sast`), **`thorough`** (rank 30; panel correctness + security + architecture + test-quality + dependency-hygiene; required + `sast` + `dep-audit` + `coverage`, informational `semgrep` — the expensive checks staged to the approval-candidate round). `sast` (bandit) is informational on `standard` (kept so per Option A; its first-party bandit baseline is triaged as of #173, so promoting it to required is now a deliberate option) and required only on `thorough`, an explicit opt-in (#140 floor slice, Option A) — monotonicity holds because `standard`'s required floor stays a subset of `thorough`'s. Selection precedence: per-task `task.metadata.develop_review_profile` › project-context `develop_review_profile` › host `[story_develop].default_review_profile` › built-in `standard`; an unset layer inherits the one below, silently. An **explicit-but-unknown** profile name **fails closed** — the run halts before any agent runs with a blocking `[Friction]` (the deliberate exception to the friction-not-fail norm: silently substituting a profile would defeat the dial) — unless the host sets `[story_develop].unknown_profile = "strongest"`, which falls back to the strongest configured profile + friction (never weaker). A **load-time `strength_rank` monotonicity invariant** requires a higher-ranked profile's required checks **and** required personas to be a superset of every lower-ranked profile's (non-monotonic → a `ConfigError` at load). #139 **resolves + validates** the profile; **#140 slice 1** applies it to the **deterministic check-set**: the resolved profile selects which checks run, with real per-image tool probing and **per-check staging** — fast checks (`lint` / `typecheck` / `sast` / `test`) run every round for tight coder feedback, while candidate checks (`dep-audit` / `coverage` / `semgrep`) run only on the **approval candidate** (the round that would otherwise pass). A **fast** check runs before the panel each round, so it feeds the coder + reviewer prompts + the gate ledger (ADR §6); a **candidate** check runs only on the approval candidate, so its result reaches the gate ledger + the `[DevelopResult]` finding and, when it blocks, holds approval so a follow-up round surfaces it to the coder/panel. `format` is not run as a standalone gate check; instead the **auto-format pass (#134, ADR §4)** runs each detected ecosystem's formatter in **write** mode (`ruff format` / `prettier --write .` / `cargo fmt` / `gofmt -w .`, image-probed once per run, image-global — never `uv run`-wrapped) immediately after the coder's commit, and commits any change as a **separate** commit on the round so the gate **and** the reviewer panel see that exact formatted tree (loom never formats after approval). The formatter is treated as **untrusted** (a repo-controlled formatter config / plugin can run arbitrary code): each runs against an **isolated `git archive` export** of the coder's commit — never the live worktree — in a hardened container with `--network none` and a cache **separate** from the deterministic gate's, so it cannot reach the `.handoff` orchestration channel (worktree-only, absent from the export), poison the gate's package cache, or egress. Changes are applied back to the worktree's tracked files only when a formatter **exits clean** (success-gated): a nonzero / timed-out run may have left a half-rewritten tree, so its edits are discarded rather than reach the gate / panel. The pass is **best-effort** — an absent / erroring / nonzero formatter is skipped, never fatal — and because formatting is applied deterministically up front, the read-only `format` check should always already be clean, so it stays required-but-non-blocking. **#140 slice 2** applies the profile to the **reviewer panel** (*replace-default-only*): when no reviewers are explicitly selected (no `develop_default_reviewers` / task `reviewers` / `--reviewer` / `--develop-config`), the profile's personas become the panel — `standard` → correctness + security (up from the single `code-quality` reviewer), `thorough` → all five (#137) — and each persona still picks up the route/host model + effort layers (a persona's own explicit effort, e.g. `security`'s `xhigh`, is respected, not blanket-downgraded). An explicit reviewer selection still **wins** (the escalate-only floor — you cannot select *below* the profile's persona floor — is the overrides slice). **#140 floor slice** makes each profile's **required floor actually block** (it is no longer informational-only — the deterministic gate now gates approval, not just the panel): a *required* check blocks when its tool is **expected-but-absent** or **times out**, or — for a finding-producing tool (ruff / bandit / pip-audit) — when the **gate ledger holds an open finding at or above `major`** for it (severity, not raw exit, decides for a finding-producing tool, ADR §5) **or** when it exited **red with no open findings** — a `--exit-zero` adapter that exits non-zero failed to run, so an empty-ledger red blocks rather than silently passing (#167 floor-liveness); a no-adapter tool (pyright / pytest / coverage / semgrep) reads its raw exit code. **Informational** checks never block, even RED. **Env-dependent** checks — `typecheck` / `coverage` (+ `test`), whose tool runs *inside* the project venv (pyright resolves third-party imports; pytest / coverage run the code) — run via `uv run` on a uv-managed repo (a `uv.lock` is present) so they resolve inside the project venv materialised in the gate container, exactly as the `test` check already does; bare, `pyright` sees the throwaway container's empty environment and false-positives (#165). The static-analysis checks (ruff / bandit / semgrep — AST/source only) **and** `dep-audit` stay **bare** and image-global (`pip-audit` is an *external auditor*, not a project dependency, so it is never `uv run`-wrapped). `dep-audit` audits the project's **resolved** deps — `uv export --no-emit-project --format requirements-txt | pip-audit -r /dev/stdin` (#167), not the container's ambient env; `command_tool` resolves the pipe's consumer (`pip-audit`) as the adapter tool so the floor reads its severity ledger (and a failed run blocks via floor-liveness). Per **Option A** the default `standard` blocks only on `lint` + `typecheck` + `test` (exactly what `make check` already enforces — zero new false-positive surface), with `sast` (bandit) informational on `standard` (its first-party baseline is now triaged — #173 — but promotion stays a deliberate choice); `thorough` additionally blocks `sast` / `dep-audit` / `coverage`. `minimal` is gate-only, but a true zero-reviewer panel is the overrides slice, so the floor slice keeps the built-in reviewer + a `[Friction]` for `minimal` for now. As of **#173** the `thorough` candidate-stage gates are runnable on a uv project: `sast` scopes off `.venv`/vendored deps (a bare `bandit -r .` scanned third-party code in the materialised venv), `coverage` runs `coverage run -m pytest && coverage report` against a real `[tool.coverage] fail_under` (with `coverage` provisioned as a dev-dependency, since it is venv-resident), and the repo's first-party bandit baseline is triaged via per-site `# nosec`. Still to come: additive escalate-only per-task/project **overrides** + `allow_escalation` (audited), making `minimal` truly gate-only and the persona floor non-weakenable.
- **Canonical reviewer personas (#137 / ADR 0003 §8).** A built-in registry of one-dimension reviewer personas, **opt-in by name** — name any of them in `develop_default_reviewers` (or a task's `metadata.reviewers`) and it resolves to a ready-made spec with its engine, severity floor, reasoning effort, and a focused `system_prompt` baked in; no need to redefine the prompt in `develop_reviewers`. An explicit `develop_reviewers` entry of the same name **overrides** the canonical (so a project can re-tune one). As of **#140 slice 2** the zero-config default follows the **`standard`** profile — a project that selects nothing runs `standard`'s **correctness + security** panel (replacing the former single generalist `code-quality` reviewer); the dial is the Review Profile (#139), an explicit reviewer selection still wins (a typo'd one falls back to the single `code-quality` reviewer), and gate-only `minimal` keeps that single reviewer + a `[Friction]` until the overrides slice wires a true zero-reviewer panel (the floor itself shipped in #140's floor slice). The personas use **heterogeneous engines on purpose** (diverse engines → diverse blind spots, #94):

  | persona | engine | threshold | focus (one dimension) |
  |---|---|---|---|
  | `correctness` | codex | major | boundaries, off-by-one, races, error handling / propagation, idempotency, resource cleanup |
  | `security` | claude (xhigh) | minor | OWASP Top 10 (2025) + CWE: injection, broken access control / IDOR, secrets, SSRF, deserialization, crypto misuse |
  | `architecture` | codex | major | module boundaries per `AGENTS.md`, abstractions / coupling, public surface; reviews the full `base..HEAD` |
  | `test-quality` | codex | minor | edge cases, mocks that hide behaviour, determinism, AC↔test mapping |
  | `dependency-hygiene` | claude | minor | new-dependency justification, supply-chain reputation, pinning, license |

  Each persona prompt is held to **one dimension** with an explicit "NOT your job" deferral; the base reviewer templates add a **"stay strictly within this focus"** discipline line (only when a focus is set, so the generalist default is untouched) and a shared **severity-calibration** rubric (critical / major / minor definitions) so the panel calibrates consistently before the orchestrator applies each persona's `block_threshold`. Personas leave `model` unset (inherit the route / project default — no hard-pinned, ageing model id); an operator may still pin a cheaper model per persona. *Deferred:* the test-quality persona's "coverage tail" (ADR §8) awaits a gate that captures coverage — bundled with the SAST gate work (#135 / #132).
- **Status mapping.** `approved` → `succeeded`; with the route's `completes_task = false` (§2.2) the runner then leaves the task **open** for human merge — raising a `pr` gate that structurally blocks the story (Epic H / US10) and releasing — rather than completing it, so an approved run never closes a github-linked issue for un-merged work. `interrupted` (usage-limit pause budget exhausted) → `interrupted` with `error.category="usage_limited"` and a `resume` block (the runner schedules a re-dispatch, §2.2); every other stop (`max_rounds`, `stalled`, `disputed`, `cost_exceeded`, `failed`) → `failed`. An approved dialogue whose **PR delivery fails** before a PR opens (e.g. `push_branch()` / `gh pr create` raises) → `failed` with `error.category="delivery"` carrying the reason (#194), **not** `succeeded` — no PR exists, so the task stays open/retriable and the run is not recorded under the idempotency key; the daemon also drops a private `run_dir/delivery.json` failure marker so `attach` reports it at once (§4.14). The **standalone CLI** (`--open-pr`) shares this exact path via `pr_delivery.deliver_guarded` (ARCH-1.S3): a delivery failure there likewise **skips `--complete-on-approval`** (an approved dialogue with no PR is not done) and **exits non-zero**, and writes the same private `delivery.json` markers (so the on-disk delivery contract is now symmetric across both surfaces) — closing a latent gap where standalone marked the task completed and exited `0` on a failed delivery. The markers use the identical format `develop attach` reads; note that `attach`'s run-dir *discovery* targets the daemon `<work_dir>/<task_id>/<run_id>` layout, so a standalone run under its default `<work_dir>/<run_id>` layout is **not** auto-discovered by `attach <run-id>` — the marker gives on-disk contract parity, not turnkey standalone attach (which would need discovery support for the standalone layout).
- The plugin still owns its Lithos round-trip directly (the `[DevelopResult]` finding + `develop_*` metadata, same as `--task-id` mode); `result.json` carries `status` for the runner, so there is no double-application.
- **Review-metadata record (#139 / ADR 0003 §11).** Every run's metadata patch records, alongside `develop_status` / `develop_branch` / `develop_run_id` / `develop_rounds` / `develop_cost_usd` (+ `develop_pr_url` when delivered), the per-run review signal for later outcome-correlation: `develop_review_profile_used` (the **resolved** profile that ran — an output-only key, kept distinct from the operator's `develop_review_profile` *input* selection so recording the run never pins a task's profile), `develop_review_panel` (the reviewer names in the final panel), `develop_findings_by_severity` (the final panel's findings counted by severity, canonical `critical` / `major` / `minor` zero-filled), and `develop_test_gate_verdict` (`GREEN` / `RED` / `TIMEOUT`, when a gate ran). The same record (profile + panel + findings) also lands in the durable run-state `state.json`. The basket of post-merge outcome signals + success-metric rollup it correlates against is a later phase (ADR §11, reserved-shape).
- **Coder prompt discipline (plan-first + pragmatic test-first).** Both coder templates (`coder_init.md` implement turn, `coder_fix.md` fix turn) carry an explicit working discipline: understand the task + surrounding code and plan the approach before editing, make the **smallest change** that satisfies the acceptance criteria (or resolves the finding), and add a test that would **fail without the change** for each acceptance criterion / real bug fix **and run that targeted fast test** (red→green) — pragmatically, matching the project's existing test layout rather than manufacturing ceremony tests for trivial code. The run is scoped to the **targeted fast test**, not the full suite, so it never relaxes the single-turn / no-background-and-wait rules. This complements the test-quality persona (which judges whether the tests actually protect the behaviour) and the objective gate; it does not relax the single-turn / no-background-and-wait rules (#115) — the gate still owns the authoritative test run.
- **Coder handoff salvage (#114).** The coder gets one non-interactive turn per round and must end by writing its handoff file; a turn that ends cleanly *without* one normally fails the round. To avoid discarding completed work when an agent strands its turn (the observed case: it backgrounds a slow suite and stops before the handoff step), the orchestrator re-prompts the coder **once** — only when the turn exited **cleanly** and left **uncommitted** changes in the worktree (there is real work to save) — to write just the handoff, then re-checks. Recovered → the round proceeds normally (commit → gate → review); still missing → it fails as before (`no coder handoff file`). Exactly one extra turn, always on, no config. (The coder prompt already forbids backgrounding-and-waiting (#115); this net recovers the cases that slip through.)
- **Operator notification on delivery (#113).** When `[story_develop].operator_github_login` (§3.1) is set in daemon mode (or `--notify-github-login` is passed standalone), a delivered PR requests that user as a GitHub reviewer so native email/web/mobile notifications fire. If they **authored** the PR — the usual case, since loom runs under the operator's own `gh` auth and GitHub rejects a self review-request with HTTP 422 — it falls back to **assigning** the PR to them (allowed for self, still notifies). The run summary records "requested review from / assigned to `<login>`". Best-effort: a failure is noted, never fatal. Unset → Copilot review only (the prior behaviour).

**Worked example — model + effort.** Project default in the project-context doc's frontmatter (a reviewer that out-models the coder, plus a strict security pass):

```yaml
develop_image: ghcr.io/acme/dev:2026-06   # project-wide sandbox image (optional)
develop_test_command: make check          # gate runs this verbatim, trusted as-is (optional)
develop_review_profile: thorough          # selects the check-set + panel; governs test blocking (optional)
develop_coder:
  model: sonnet
  effort: medium
develop_reviewers:
  - name: code-quality
    model: opus
    effort: high
  - name: security
    model: claude-opus-4-8        # full id — pinned for reproducibility
    effort: xhigh
    block_threshold: minor        # blocks on minor+ (code-quality stays major+)
    system_prompt: "Focus on authz, injection, secrets, SSRF."
develop_default_reviewers: [code-quality, security]
```

Or skip the pool entirely and select **canonical personas** by name (#137) — each carries its own engine, threshold, effort, and focused prompt:

```yaml
develop_default_reviewers: [correctness, security, architecture]
```

Per-task override on a Lithos task's `metadata` (coder-only; reviewers stay project policy):

```jsonc
{ "develop_model": "haiku", "develop_effort": "low" }   // a trivial task — go cheap
{ "develop_model": "opus", "develop_effort": "max" }    // a gnarly task — go deep
{ "reviewers": ["security"] }                            // run only this reviewer from the pool
{ "develop_image": "ghcr.io/acme/dev-cuda:2026-06" }    // this task needs a heavier image
{ "develop_test_command": "make check" }                // run the full check as this task's gate
```

Standalone CLI (per-reviewer model/effort require `--develop-config`; the blanket `--reviewer-*` flags apply to every `--reviewer` and **error** if combined with `--develop-config`):

```bash
# blanket: cheap coder, rigorous single reviewer
python -m lithos_loom.plugins.story_develop --repo ~/proj --task-id <uuid> \
    --coder-model haiku --coder-effort low --reviewer-model opus --reviewer-effort xhigh

# per-reviewer: a [[reviewers]] table per reviewer in the config file
python -m lithos_loom.plugins.story_develop --repo ~/proj --task-id <uuid> \
    --develop-config panel.toml
```

Resolution precedence (first match wins; "agent default" = no flag passed, so the tool's own default applies):

| Knob | Order |
|------|-------|
| coder model | `task.develop_model` → `develop_coder.model` → `--coder-model` (route fallback) → `[story_develop].default_models[<coder tool>]` → agent default |
| coder effort | `task.develop_effort` → `develop_coder.effort` → `--coder-effort` (route fallback) → agent default |
| reviewer model | `develop_reviewers[].model` → `--reviewer-model` (fills only what's unset) → `[story_develop].default_models[<reviewer tool>]` → agent default |
| reviewer effort | `develop_reviewers[].effort` → `--reviewer-effort` (fills only what's unset) → agent default |
| sandbox image | `task.develop_image` → `develop_image` (project) → `--image` (route fallback) → `ralph-sandbox:latest` |
| test-gate command | `task.develop_test_command` → `develop_test_command` (project) → `--test-command` (route fallback) → auto-detection |
| test-gate on/off | `task.develop_test_gate` → `develop_test_gate` (project) → `--no-test-gate` (route fallback) → on |
| test blocks on red | the resolved Review Profile's `ProfileCheck("test", …)` state (#140; all canonical profiles require it, so a red test gate blocks). The legacy `develop_block_on_red` key is removed (inert + deprecation `[Friction]`). |

The `[story_develop].default_models` layer (§3.1) is a **host-wide, per-tool** default model read from the loom TOML in **daemon mode only**: it fills any agent (coder or reviewer) the layers above left unset, keyed by that agent's resolved `tool`, just above the agent CLI's own default. It is per-tool rather than per-role so a heterogeneous panel (#94) picks the right default for each agent (a codex reviewer and a claude coder draw from different keys). There is no analogous global default for `effort` — Claude's level already defaults sensibly and codex has no effort knob.

---

## 6. Event Bus Contract

### 6.1 Event Schema

```python
@dataclass(frozen=True)
class Event:
    type: str                   # dotted name, e.g. "lithos.task.created"
    timestamp: datetime         # UTC; when the source published the event
    payload: Mapping[str, Any]  # event-type-specific; see §6.4
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

Payloads are dicts; the exact key set depends on the source. Task-event payloads are the Lithos task envelope as returned by `lithos_task_list` / `task_status` (fields like `id`, `title`, `status`, `tags`, `metadata`, `claims`, and lifecycle timestamps such as `resolved_at`). Note bootstrap events are intentionally minimal (`{id, title, path}`); subscriptions that need the full body or tags re-fetch via `lithos_read`. Subscriptions should treat payloads as opaque dicts and look up specific keys defensively — additional fields may be present and field absence depends on the underlying Lithos response.

| Event type | Source | Payload notes |
|---|---|---|
| `lithos.task.created` | LithosEventStream | Lithos task envelope. |
| `lithos.task.updated` | LithosEventStream | Lithos task envelope (post-edit). |
| `lithos.task.claimed` | LithosEventStream | Lithos task envelope; `claims` lists the active claim. |
| `lithos.task.released` | LithosEventStream | Lithos task envelope after release. |
| `lithos.task.completed` | LithosEventStream | Lithos task envelope; `resolved_at` populated. |
| `lithos.task.cancelled` | LithosEventStream | Lithos task envelope; `resolved_at` populated. |
| `lithos.note.created` | LithosNoteStream | Bootstrap: `{id, title, path}`. Subscriptions that need more re-fetch via `lithos_read`. |
| `lithos.note.updated` | LithosNoteStream | Same shape as `created`. |
| `lithos.note.deleted` | LithosNoteStream | `{id, path}`. |
| `obsidian.task.status_changed` | ObsidianFSWatcher | Carries the prior and new status markers (`[ ]`, `[x]`, `[-]`, `[/]`, `[>]`) and the task id parsed from `🆔 lithos:<id>`. |
| `obsidian.task.priority_changed` | ObsidianFSWatcher | Carries prior and new priority (one of `highest|high|medium|low|lowest|null`). |
| `obsidian.task.due_date_changed` | ObsidianFSWatcher | Carries prior and new `YYYY-MM-DD` date strings (either side may be absent). |
| `obsidian.note.modified` | ObsidianDirWatcher | Carries the doc id parsed from frontmatter, the modified body, and the local `lithos_version`. |
| `github.issue.seen` | GitHubIssueWatcher | `{slug, repo, number, title, body, state, state_reason, labels, author, html_url, updated_at}`. One per issue per poll. The subscription decides create / update / close from the marker + state combo. |

### 6.5 Sources

Sources are async coroutines spawned by their owning child. They consume external input (Lithos SSE, filesystem polls) and publish events.

| Source | Spawned by | Bootstrap | Reconnect |
|---|---|---|---|
| `LithosEventStream` | route-runner + obsidian-sync + github-watcher (independently) | `lithos_task_list(status='open', with_claims=true)` → re-emit `lithos.task.created` per task. | Exponential backoff with `Last-Event-ID` resume. Cursor persisted to `<work_dir>/<child>/sse_cursors.json` so restarts resume from the last drained event. |
| `LithosNoteStream` | obsidian-sync (when `project-context-projection` is configured) + github-watcher | `lithos_list(path_prefix='projects/', tags=['project-context'])` → re-emit `lithos.note.created` per match. | Exponential backoff with `Last-Event-ID` resume. Cursor persisted alongside `LithosEventStream` in the same `sse_cursors.json`. |
| `ObsidianFSWatcher` | obsidian-sync | Polls `<vault>/<tasks_file>` on a 250ms cadence; emits when a line diverges from the last-known state. | n/a (polling). |
| `ObsidianDirWatcher` | obsidian-sync (when `note-push` is configured) | Walks `<vault>/<projects_dir>/**/*.md` on the same cadence; computes body-only hashes. | n/a. Excludes files ending in `-done.md` (the per-project archive). |
| `GitHubIssueWatcher` | github-watcher | Reads `note_list(path_prefix='projects/', metadata_match={'github_watch_enabled': true})` to build the slug → repos watch list (a project may map several repos); loads per-repo `updated_at` cursors from `coord_doc_path`. | n/a (polling). Per-repo 404 drops the repo with a `[Friction]` log; 403 + `X-RateLimit-Remaining: 0` sleeps until `X-RateLimit-Reset`. |

### 6.6 Subscription Action Registry

A subscription's `action` field names a handler that the hosting child hand-wires (constructs by name with its runtime dependencies) and feeds to `build_runners`. The known action names live in one catalog, `subscriptions.SUBSCRIPTION_ACTIONS`; `validate-config --dry-run` validates config actions against it. There is no entry-point discovery — handlers carry dependencies a zero-arg lookup can't supply (see [ADR 0007](adr/0007-subscription-registration-hand-wired.md)). The actions:

| Action | Module | Consumes | Effect |
|---|---|---|---|
| `noop` | `_noop` | any | Logs at DEBUG. Useful for tracing. |
| `obsidian-projection` | `_obsidian_projection` | `lithos.task.*` | Rewrites `<vault>/<tasks_file>`. |
| `obsidian-awaiting-review` | `_awaiting_review` | `lithos.task.*` | Rewrites `<vault>/<awaiting_review_file>` (#113): open tasks carrying `develop_pr_url`, as a clickable PR-link reference list (empty → a "none" placeholder). Read-only — no `sync_state`. |
| `obsidian-status-transition` | `_obsidian_status_transition` | `obsidian.task.status_changed` | `[ ]→[x]` calls `lithos_task_complete`; `[ ]→[-]` calls `lithos_task_cancel`; `[x]→[ ]` posts `[ReopenRequested]` finding; `[/]` / `[>]` are no-op (logged). |
| `obsidian-priority-changed` | `_obsidian_priority_changed` | `obsidian.task.priority_changed` | `lithos_task_update(metadata={priority: ...})`. |
| `obsidian-due-date-changed` | `_obsidian_due_date_changed` | `obsidian.task.due_date_changed` | `lithos_task_update(metadata={scheduled_for: ...})`. |
| `project-context-projection` | `_project_context_projection` | `lithos.note.*` | Re-fetches via `lithos_read`, writes `<vault>/<projects_dir>/<slug>/<filename>.md` atomically. |
| `note-push` | `_note_push` | `obsidian.note.modified` | `lithos_write(id, content, expected_version)`; on conflict, runs the conflict resolver. |
| `task-archive` | `_task_archive` | `lithos.task.completed` / `lithos.task.cancelled` | Appends a Tasks-plugin line to `<vault>/<projects_dir>/<slug>/<slug>-done.md` (O_APPEND); marks the task as archived so the projection evicts it on next flush. |
| `github-issue-sync` | `_github_issue_sync` | `github.issue.seen` | Auto-wired by the github-watcher child (not declared in `[[subscriptions]]`). Resolves the `<!-- lithos:<id> -->` marker, then creates / closes / no-ops + GH → Lithos drift (title / body / labels) per §2.2. Reopen on a terminal task posts `[ReopenRequested]` once (de-duped via `metadata.github_state_snapshot`). |
| `github-issue-push` | `_github_issue_push` | `lithos.task.created` / `lithos.task.completed` / `lithos.task.cancelled` / `lithos.task.updated` | Auto-wired by the github-watcher child. Mirrors Lithos terminal status to a GH close with the matching `state_reason`; mirrors title renames from Lithos → GH on `task.updated` and (for bootstrap-replayed open tasks) `task.created`. Idempotent re-fetch dodges redundant PATCHes when the GH → Lithos path already converged. Consumer retries transient GH failures with exponential backoff capped at 60s, up to 8 attempts before dropping `[Friction]`. |

Each handler receives an `Event` and a `SubscriptionContext` carrying a shared `LithosClient`, a scoped `logging.Logger`, and the orchestrator's `agent_id`. A new handler is added by wiring its factory in the hosting child and listing its action in `SUBSCRIPTION_ACTIONS` — there is no out-of-tree plugin registry (see [ADR 0007](adr/0007-subscription-registration-hand-wired.md)).

---

## 7. Obsidian Projection

### 7.1 File Layout

```
<vault_path>/
├── <tasks_file>                              # default: _lithos/tasks.md
├── <projects_dir>/                           # default: _lithos/projects/
│   ├── <slug>/
│   │   ├── <slug>-project-context.md         # canonical project doc (per Lithos KB convention)
│   │   ├── <other-file>.md                   # any additional project-context-tagged doc
│   │   └── <slug>-done.md                    # task-archive's append-only history (vault-only)
│   └── _unassigned/
│       └── _unassigned-done.md               # archive bucket for tasks with missing metadata.project
└── _lithos/conflicts/                        # note-push conflict snapshots
```

All writes use a dot-prefixed temp file (`.<filename>.tmp.<rand>`) plus `os.replace` for atomicity. The dot prefix matters: Obsidian Sync (and Dropbox-style observers) skip dotfiles, avoiding a publish noise.

### 7.2 Tasks-Plugin Line Shape

Open-task line:

```markdown
- [ ] <title> 🆔 lithos:<id> [#project/<slug>] [#lithos/<route-name>] [⛔ lithos:<dep_id>...] [🔺⏫🔼🔽⏬] [📅 YYYY-MM-DD]
```

Resolved-task line (completed / cancelled):

```markdown
- [x] <title> 🆔 lithos:<id> [#project/<slug>] ✅ YYYY-MM-DD
- [-] <title> 🆔 lithos:<id> [#project/<slug>] ❌ YYYY-MM-DD
```

The renderer always emits fields in this exact order. Priority, deps, and due-date markers are dropped on resolved lines; the resolved-date marker is always last so the Tasks plugin's `sort by done date` / `done after` filters parse correctly. Operator-side tags from `task.tags` are NOT rendered today.

| Token | Meaning | Source | Direction |
|---|---|---|---|
| `[ ]` / `[x]` / `[-]` | Status: open / completed / cancelled | `task.status` | Bidirectional. `[/]` and `[>]` are detected on read, no-op on write. |
| `🆔 lithos:<id>` | Task ID | `task.id` | One-way (identity; never edited by operator). |
| `#project/<slug>` | Project tag | `task.metadata.project` | One-way. |
| `#lithos/<route-name>` | Active human-blocking claim's route | route lookup based on the active claim | One-way; surfaces while a human-blocking route holds the claim. |
| `⛔ lithos:<blocker_id>` | One marker per Lithos blocker naming another task (US8) | `lithos_task_blocked` | One-way (Lithos canonical). |
| `🔺⏫🔼🔽⏬` | Priority (highest / high / medium / low / lowest) | `task.metadata.priority` | Bidirectional. Absent emoji = no priority. |
| `📅 YYYY-MM-DD` | Due date | `metadata.scheduled_for` if set; else `today` for human-blocking tasks; else absent | Bidirectional via `metadata.scheduled_for`. |
| `✅ YYYY-MM-DD` | Completed date | `task.resolved_at` | One-way; only rendered for `[x]` lines within TTL. |
| `❌ YYYY-MM-DD` | Cancelled date | `task.resolved_at` | One-way; only rendered for `[-]` lines within TTL. |

### 7.3 Projection Filter

A task is projected when `is_human_actionable(task, routes)` returns true:

- The task is `open`, AND
- It is **not a non-`human` gate** — a `pr` / `timer` / `ci` / `external_task` gate is resolved by an automated poller, not the operator, so a `task_type="gate"` task projects **only** when `metadata.gate_type == "human"` (the one gate kind whose resolution *is* a human ticking a box). A `pr` gate must not land in `tasks.md` as a `- [ ]`, because ticking it from the obsidian-sync child would complete the gate and re-develop merged work. An **`epic`** is deliberately *not* filtered — unlike a gate, ticking an epic is the intended manual roll-up (epic completion stays manual until the extension's Phase 4), so it stays projected via the orphan rule below. AND
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
lithos_updated_at: <ISO 8601>   # omitted when the note has no updated_at
slug: <directory-name>          # omitted when the note has no slug
status: <whatever Lithos returned>   # omitted when null; common values: active, archived, quarantined
tags:                           # omitted when empty
  - project-context
  - ...
---
# <title>

<body>
```

Key order is stable (`lithos_id` → `lithos_version` → `lithos_updated_at` → `slug` → `status` → `tags`). Optional rows are omitted entirely rather than rendered as `null` or `[]`. `status` is whatever Lithos returned for the note — Loom passes it through verbatim. The body below the frontmatter is the Lithos doc body, prefixed with `# <title>`. Frontmatter is daemon-managed; operator edits to frontmatter fields are not pushed back. Body edits are pushed via `note-push` (see §7.5).

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

- **`include_blocked`** (default `true`): when `false`, tasks **Lithos reports as blocked** are not projected.

**Blocked-ness in the projection (Epic G / US8).** The ⛔ markers and the `include_blocked` gate read Lithos's authoritative blocked set via `lithos_task_blocked`, not `metadata.depends_on`. That list records what a task *declared*, so it stayed true forever — a ⛔ persisted long after its dependency completed, and `include_blocked = false` hid a task that nothing was actually waiting on. The set is swept **once per flush cycle** (the restart bootstrap re-emits every open task, and a burst renders into one file write anyway), so a change landing mid-burst is picked up on the next cycle. A sweep failure degrades to "nothing blocked" and logs — the projection is a display, and a missing marker beats a missing task. `lithos_task_blocked` has no per-task filter and no cursor, so the sweep is capped at `READY_QUERY_LIMIT` (500) and a **full page** means the set is truncated: tasks beyond the cut render without ⛔ and slip past `include_blocked = false`, and a WARNING says so. This sweep is unnarrowed (it spans every project), so it is the likeliest of the three to truncate. The degradation deliberately *differs* from the runner's (§2.2): there, one direction is plainly unsafe (dispatching a task whose blocker is still open), whereas here both are display errors — so the tiebreak is recoverability. Hiding an actionable task makes it vanish from the vault with nobody able to notice; showing a blocked task is visible and ignorable. Hence: **hide only on positive evidence of blocked-ness, never on inferred unblocked-ness.** Markers are emitted only for blockers naming *another* task: ⛔ references another line's 🆔, so a `cycle` blocker (which names the task itself) has nothing valid to point at. Gate blockers — invisible to the old metadata mirror — now render.
- **`exclude_tags`** (default `[]`): tasks carrying any listed tag are not projected. Useful for suppressing automation noise (e.g. `["influx:run", "influx:backfill"]`).
- **`resolved_ttl_days`** (default `7`): how long resolved tasks linger in `tasks_file` when `task-archive` is NOT configured, OR (when `task-archive` IS configured) the bootstrap-replay window the archiver looks back over on restart.

---

## 8. Finding Prefixes

Loom posts findings with stable prefixes so operators (and `lithos-lens`) can grep machine-parseably. The prefixes emitted today:

| Prefix | Posted by | Meaning |
|---|---|---|
| `[Friction]` | any subscription | Persistent failure of a side effect (retry exhausted) OR a notable operator-visible event (e.g. note-push conflict). |
| `[ReopenRequested]` | `obsidian-status-transition` and `github-issue-sync` | An operator unticked a completed task (Obsidian) OR reopened a closed GH issue linked to a terminal Lithos task. Lithos has no reopen primitive, so this signals the intent. |
| `[BlockerFailed]` | route-runner | Plugin failed, timed out, violated the contract, or returned an unknown status. The claim was released. |
| `[GateResolved]` | github-watcher (PR-gate resolver, Epic H) | A story's `pr` gate saw its PR **merge**. The story was completed (story-first), then the gate; the finding is posted on the story. |
| `[DeliveredPRClosed]` | github-watcher (PR-gate resolver) | A delivered PR was closed without merging, or deleted. The `pr` gate is left **open** (never cancelled) and the finding is posted on the story, which stays blocked; a human decides whether to abandon or re-develop into a replacement PR. |
| `[LinkedIssueGone]` | `github-issue-push` (#69) | A task's linked GitHub issue was deleted (404). The Lithos→GH link is orphaned; a `metadata.github_issue_gone_url` marker suppresses further pushes to it until the task is re-linked. |

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

- **`unknown project '<slug>'`** (exit 2, `task create`): the `--project` value isn't in the union of (a) slugs from `lithos_list(path_prefix='projects/', tags=['project-context'])` and (b) the TOML `[projects.<slug>]` registry. The TOML side lets a host run capture against a slug that hasn't yet been promoted to a project-context doc in Lithos.
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
| `lithos_task_create(task_type=…, parent_task_id=…, depends_on=…)` | `project import` builds its epic / child / sequential-chain graph in one call per task (§4.8). |
| `lithos_task_create(task_type='gate', metadata.gate_type='pr')` + `lithos_task_edge_upsert(type='waits_on_gate')` + `lithos_task_complete(gate)` | Epic H `pr` gates: raise a gate blocking a delivered story, resolve it on PR merge (§2.2). Rejected `not_a_gate` if the `waits_on_gate` `from_task` isn't a gate. |
| `lithos_finding_post` | `[Friction]` / `[ReopenRequested]` / `[BlockerFailed]` breadcrumbs. |
| `lithos_write(id=..., expected_version=...)` | Note push with optimistic locking; `version_conflict` envelope drives the conflict resolver. |
| `lithos_read`, `lithos_list(path_prefix=...)`, `lithos_delete` | Project-context projection + CLI surface. |
| `task.metadata` field on tasks | All `metadata.*` references throughout (priority, scheduled_for, project, etc.). Note `depends_on` / `blocked_on` are **rejected** metadata keys — dependencies are task edges. |
| `task.updated` event (minimal `{task_id}` payload) | Cache-invalidation signal; `LithosEventStream` force-refreshes via `task_list(status='open')` to pick up the new field values. Other task events are served from cache where possible. |
| `note.created` / `note.updated` / `note.deleted` events on `GET /events` SSE | Project-context projection. |

Slug = directory name under `knowledge/projects/<slug>/`. Lithos enforces uniqueness with a `slug_collision` envelope; Loom relies on this rather than a frontmatter field.

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

## 12. Not Implemented

The following are absent from the current surface. Some are explicit non-goals; others are queued in `docs/prd/`. Listed here so readers don't go looking.

- **Plugin body for `prd-decompose`.** Scaffolding under `src/lithos_loom/plugins/`; no real logic.
- **Application of `result.json` fields beyond `status`.** `metadata_updates`, `artifacts`, `commits`, `spawned_tasks`, `exit_code`, `error.retriable` are schema-validated but not used by the runner today (§5.2).
- **`orchestrator.max_concurrency` enforcement.** Parsed and stored but never read at runtime — there is no global cap on concurrent plugin runs. A single route runs its tasks serially; multiple routes run concurrently without contending. Tracked in [#85](https://github.com/agent-lore/lithos-loom/issues/85).
- **Resolved project entry in `task.json`.** Plugins receive `{"task": <payload>}` only.
- **Startup reclaim of stale claims.** Claims age out via Lithos's own TTL; the route-runner does not actively release them on startup.
- **Hot-reload of TOML config.** Operator restarts the daemon.
- **Persistent event log.** Restart relies on source re-authority + subscriber idempotency.
- **Containerised daemon.** Loom runs as a host process; Lithos and adjacent services may run in docker.
- **Other planned work** (`prd-generate`, agent-driven reviews, brain, `merge-stories`, A2A endpoint, GitHub issue watcher, multi-host PRD-affinity, docker sandbox, cost tracking). See `docs/prd/` for PRDs.

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
