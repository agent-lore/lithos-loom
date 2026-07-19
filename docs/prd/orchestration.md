---
title: Lithos Loom — Orchestration Plan (post task-graph extension)
milestone: M1–M5
status: draft
target_version: 1.0.0
supersedes:
  - docs/prd/mvp.md (Track 2 MVP — orchestration spine shipped; framing now stale)
  - docs/prd/full.md (A1–A10 roadmap — folded in and reshaped below)
references:
  - docs/SPECIFICATION.md (implemented surface — architecture, plugin contract, event bus)
  - docs/prd/archive/story-develop.md (the canonical implement→review→PR plugin — shipped + archived)
  - docs/prd/archive/integration.md (Obsidian bridge PRD — shipped)
  - https://github.com/agent-lore/lithos/blob/main/docs/plans/task-graph-coordination-extension.md (the Lithos extension this plan assumes)
  - https://github.com/agent-lore/lithos/blob/main/docs/SPECIFICATION.md (Lithos task + knowledge surface)
labels: [needs-triage, lithos-loom, orchestrator, task-graph]
---

# Lithos Loom — Orchestration Plan (post task-graph extension)

> **Status (2026-06-13).** This plan replaces both `docs/prd/mvp.md` (the
> proof-of-concept) and `docs/prd/full.md` (the A1–A10 roadmap). It is written
> against a Lithos server that has the
> [task-graph coordination extension](https://github.com/agent-lore/lithos/blob/main/docs/plans/task-graph-coordination-extension.md)
> **fully in place** — `task_type`, `task_edges`, gates, and the
> `lithos_task_ready` / `lithos_task_blocked` / `lithos_task_spawn` /
> `lithos_task_children` / `lithos_task_edge_*` tool surface. The extension is a
> hard prerequisite, gated by `lithos-loom doctor` (US-1 below). There is **no
> migration/cutover concern in Loom**: the Lithos change ships and stabilises in
> prod first, then Loom is updated against it. Loom never has to straddle both
> worlds.
>
> Self-contained: everything needed to build against is inlined or linked to the
> two SPECIFICATION docs. No Lithos KB note is a prerequisite.

## Current-state baseline (what already shipped)

The "MVP proof of concept" framing is stale — most of it is built. As of this
plan:

- **Obsidian bridge** — projection (Lithos→vault `tasks.md`), status/priority/
  due-date push (vault→Lithos), project-context bidirectional sync, capture +
  project-create Templater macros, `project import` (Markdown → Lithos tasks),
  per-project task archive. Shipped.
- **Orchestration spine** — supervisor + in-process `EventBus` + sources
  (`LithosEventStream`, `LithosNoteStream`, FS watchers) + subscribers +
  plugin-runner + the atomic `result.json` contract (`docs/result-schema.json`).
  Shipped. The architecture is `sources → bus → subscribers`; the route-runner
  is a claim-bound subscriber that owns a task's lifecycle to `result.json`.
- **Route-runner** — one claim-bound `RouteRunner` per `[[routes]]` stanza;
  reacts to `lithos.task.created` / `lithos.task.released`, gates on
  `metadata.depends_on` (client-side `_deps_satisfied`), claims collision-safely,
  runs the plugin subprocess, renews the claim, applies `status`. Includes the
  T10 usage-limit re-dispatch (`interrupted` + `resume_after` → in-process
  re-claim) and the `completes_task = false` / `metadata.loom_delivered`
  PR-merge-wait path. Shipped.
- **GitHub issue watcher** — bidirectional issue ↔ task mirror with drift sync,
  per-project config in project-context metadata ([ADR 0001](../adr/0001-github-watch-config-storage.md)),
  reconciliation sweep. Shipped, and **already polls GitHub** — which makes it
  the natural home for the PR-gate resolver below.
- **`story-develop`** — the conversational implement → review → fix → approve
  plugin: one persistent containerised coder session + an N-reviewer panel,
  per-round commits, objective test gate in a throwaway container, usage-limit
  role-aware degradation, optional PR delivery with an autonomous Copilot review
  round, and full daemon-mode integration. Shipped (T1–T10), specced in
  [docs/prd/archive/story-develop.md](archive/story-develop.md).
- **Stubs:** `prd-decompose` (the surviving front-end — to be built here),
  `story-implement` + `story-review-human` (**to be retired** — `story-develop`
  supersedes them; see US-2 — this resolves the "supersede vs coexist" open
  question the story-develop PRD left deferred).

So the remaining work is: a decompose front-end, adopting the extension's graph
primitives in place of the hand-rolled `metadata.depends_on` machinery, turning
the PR-merge-wait hack into a real gate, and the reshaped A-layer roadmap.

## Problem (restated for the post-extension world)

The MVP and full PRDs were written when Lithos had only `tasks / claims /
findings` and dependency scheduling was pure convention in `task.metadata`. Loom
therefore **hand-rolled the scheduler**: `RouteRunner._deps_satisfied`,
`main._resolve_dep_statuses` + the dry-run `_route_outcome` / `_pending_deps`
mirror, the `task_graph.build_plan` indentation→`depends_on` builder, the
`metadata.depends_on` reads in `render.py`, `_obsidian_projection`'s `⛔`
decoration, and `_human_actionable`'s `include_blocked` gate. Cycle detection,
blocker-failed propagation, and "what's runnable now" were all Loom's problem,
Loom-private, and invisible to every other agent.

The extension makes the dependency graph **first-class in Lithos** and shared
across every agent. That collapses a whole class of Loom-private logic, gives a
deterministic resume point (`lithos_task_ready`), turns the PR-merge-wait hack
into a principled gate, and makes "review a 28-story PRD" tractable through
epic/child hierarchy. None of it changes Loom's identity: Lithos owns *what is
the state of the work graph and what is runnable*; Loom owns *route → claim →
run a plugin → react to the result*.

Everything the full PRD wanted Loom to do long-term still stands — front-of-
pipeline PRD generation, story review at scale, a knowledge feedback loop,
self-improvement, multi-host, low PR latency, brain-driven failure handling,
external triggers. The extension just lets several of those land smaller.

## Division of labour (post-extension)

| Concern | Owner |
|---|---|
| Task identity, lifecycle, claims, findings | Lithos (unchanged) |
| Dependency edges, ready/blocked computation, cycle rejection, hierarchy | **Lithos (new — extension)** |
| Gate *representation* (pr/ci/human/timer waits) | **Lithos (new — extension)** |
| Gate *resolution* (observing the PR merged / CI green / human approved) | **Loom** (Lithos never polls — §5.3 of the extension) |
| Route matching (tag → plugin), claim/renew/release, plugin subprocess + `result.json` | Loom (unchanged) |
| Worktrees, agent invocation, work-dir lifecycle, resume/re-dispatch | Loom (unchanged) |
| Concurrency / project-affinity / host-affinity enforcement | Loom (`priority` stays advisory until extension Phase 4) |
| `story-develop` internals (conversational review, containers, PR delivery) | Loom (unchanged) |

## How the extension reshapes Loom

Three concrete shifts drive the near-term stories.

### 1. The ready-queue replaces client-side dependency resolution

Today the runner reacts to a task event and then asks, per task, "are all
`metadata.depends_on` complete?" by calling `task_get` on each dependency. With
the extension, **Lithos answers readiness directly**: `lithos_task_ready(tags=
route.match.tags, project=…)` returns exactly the runnable frontier — open,
non-gate, no unresolved blocking edges, no unresolved gates, optionally
claim-filtered.

The shift: Loom stops *computing* readiness and starts *consuming* it.

- `RouteRunner._deps_satisfied`, `main._resolve_dep_statuses`,
  `_route_outcome` / `_pending_deps` (the dry-run mirror), and the
  `DependencyCycleError` path are **deleted**. Cycle rejection is now Lithos's
  (rejected on edge write).
- The bus stays. Loom remains event-driven for *latency* (an SSE
  `task.created` / `task.completed` is the nudge), but readiness is a *query*:
  on a relevant event, the runner confirms the task is in `lithos_task_ready`
  for its route's tags before claiming, rather than re-deriving the dep state.
  Double-evaluation is harmless because claim is collision-safe.
- `lithos_task_complete` can return the **newly-unblocked** tasks (extension
  §6.2). That is a direct, race-free "what just became runnable" feed — better
  than waiting for the next `task.updated`.
- Obsidian projection and `_human_actionable` read blocked-ness from
  `lithos_task_blocked` (with structured blocker reasons) instead of inspecting
  `metadata.depends_on`. The `⛔` decoration now reflects Lithos's authoritative
  blocked set, including gate and cycle blockers.

### 2. Gates replace `completes_task = false` / `loom_delivered`

Today, when `story-develop` approves a story and raises a PR, the runner marks
`metadata.loom_delivered = true`, releases the claim, and leaves the task open;
completion happens later via the GitHub watcher's close-mirror (for
github-linked tasks) or by hand. The never-built `story-review-human` poller was
meant to formalise this. It is a hack: "PR raised, awaiting merge" is encoded in
an ad-hoc metadata flag that only the runner understands, and the next story
unblocks only because the github watcher happens to complete the issue.

The extension models this as a **`pr` gate**: a `task_type=gate` task with
`gate_type=pr`, `metadata={repo, pr_number, required_state=merged}`, joined to
the story by a `waits_on_gate` edge. The story (and everything downstream of it)
is now *structurally* blocked on the gate, visible in `lithos_task_blocked`,
queryable by every agent — not just inferable from a Loom-private flag.

Because **Lithos never polls**, Loom resolves the gate: when the PR merges,
something completes the gate task (`lithos_task_complete`) and the story's
dependents become ready. The resolver is the **GitHub watcher**, which already
polls GitHub — extended to check open `pr`-gate tasks against `gh pr view
<n> --json state` and complete the gate on merge. The A7 webhook receiver later
resolves the same gate in seconds instead of a poll interval. This **unifies**
three previously-separate things — the `loom_delivered` hack, the unbuilt
`story-review-human`, and the A7 webhook — onto one gate object.

`human` gates (PRD review approval, `every-n` story checkpoints) and `ci` gates
(`merge-stories`) use the same pattern: a gate task the operator or a plugin
completes when the condition is met; `timer` gates resolve at query time.

### 3. Spawn + epic/child hierarchy replace `metadata.depends_on` chaining

`prd-decompose` stops writing `metadata.depends_on` (rejected post-migration
anyway) and instead builds a real graph:

> **There is no `subtask` task_type.** Lithos has `task` / `epic` / `gate` only —
> a child is a plain `task` that carries a `parent_task_id`. Earlier drafts of
> this plan said `epic|subtask`; US9 corrected it against the live schema. Don't
> reintroduce it.

- The decompose task (or the PRD) is the **`epic`**. Each story is a plain
  **`task`** created with `parent_task_id=<epic>` (→ `parent_child` edge) and
  `depends_on=[predecessor ids]` (→ `blocks` edges). Strict-sequential =
  chained `blocks` edges; parallel siblings = *no* edges between them (they fall
  out of `lithos_task_ready` together). "Parallelizable" stops being a metadata
  flag and becomes the structural absence of a `blocks` edge.
- Provenance uses `lithos_task_spawn(source=<decompose task>, relation_type=
  "discovered_from")` so each story carries a typed `discovered_from` edge back
  to the run that produced it — replacing the ad-hoc `parent_task_id` metadata.
- `lithos_task_children(epic)` gives PRD progress for free — the founding
  problem of the MVP ("reviewing a 28-story PRD as a single PR is impossible")
  is answered by hierarchy + the per-story `pr` gate, surfaced in lithos-lens.
- The same applies to the **bulk `project import`** path: `task_graph.build_plan`
  already computes the parent/child + sequencing structure from indentation; it
  now emits `task_type` + `parent_task_id` + `depends_on=` on `task_create`
  instead of `metadata.depends_on` / `metadata.parallelizable` (US9, shipped).

> **Dependency on the Lithos edit.** The extension as written treats a `blocks`
> edge as *resolved* when the blocker is merely *not open* — so a **cancelled**
> blocker would make its dependents `ready`, which is wrong for a strict-
> sequential PRD. Loom's current `_deps_satisfied` requires the blocker be
> `completed`. This plan depends on the companion Lithos edit
> ([task-graph extension, cancelled-blocker semantic](https://github.com/agent-lore/lithos/blob/main/docs/plans/task-graph-coordination-extension.md))
> that makes a non-`completed` terminal blocker keep dependents blocked,
> surfaced via `lithos_task_blocked` with a `blocker_unsatisfiable` reason. With
> that in place, the MVP's `[BlockerFailed]` propagation (old US-10) becomes a
> thin surfacing layer over Lithos rather than Loom-owned logic.

## User Stories

Vertical-slice, ordered by build sequence. Friction-first: the daily-friction
reducers (ready-queue adoption, the PR gate) land before the roadmap layers.
Each is independently grabbable. Findings keep the established stable prefixes
(`[DevelopResult]`, `[ReviewDispute]`, `[Friction]`, `[BlockerFailed]`,
`[ReopenRequested]`); new prefixes below get fresh names, never overloaded.

### G — Graph adoption (the core reshape)

1. As the operator, I want `lithos-loom doctor` to additionally probe the
   task-graph extension — write a probe `blocks` edge between two probe tasks,
   assert `lithos_task_ready` / `lithos_task_blocked` round-trip, and assert
   `task_type` and `lithos_task_spawn` exist — and refuse to run against a
   Lithos without it, so that an incompatible server surfaces at boot, not
   mid-PRD. (Replaces the old `task.metadata` probe, which the extension makes a
   strict subset of.)
2. As a maintainer, I want the `story-implement` and `story-review-human` stub
   plugins, their route stanzas in `examples/lithos-loom.toml`, and their
   docstring references removed, so that `story-develop` is unambiguously the
   one implement→review→PR path and there is no dead scaffolding pointing at a
   deleted PRD.
3. As the daemon, I want the `LithosClient` to gain typed wrappers for
   `task_edge_upsert`, `task_edge_list`, `task_ready`, `task_blocked`,
   `task_spawn`, `task_children`, and the `task_type` / `parent_task_id` /
   `depends_on` arguments on `task_create`, so that every other story talks to
   the graph through one tested module (mirroring the existing client shape:
   one method per tool, error envelopes → typed exceptions).
4. As the daemon, I want route dispatch to consult `lithos_task_ready(tags=
   route.match.tags)` instead of the client-side `_deps_satisfied` check, so
   that readiness (deps, gates, cycles) is computed once, server-side, and
   shared with every other agent. The bus event remains the latency nudge; the
   ready-query is the gate before claim.
5. As the daemon, I want `RouteRunner._deps_satisfied`,
   `main._resolve_dep_statuses`, the dry-run `_route_outcome` / `_pending_deps`
   dependency mirror, and `errors.DependencyCycleError` deleted, so that Loom no
   longer carries a parallel scheduler that can drift from Lithos's.
6. As the daemon, I want `lithos_task_complete` to surface newly-unblocked tasks
   and the runner to re-evaluate route matching for them immediately, so that
   the next story in a chain dispatches without waiting for a `task.updated`
   round-trip.
7. As the operator, I want `--dry-run` / `validate-config --dry-run` to render
   readiness from `lithos_task_ready` / `lithos_task_blocked` (showing each
   blocked task's structured blocker reasons: which predecessor, which gate, or
   a cycle), so that the dry-run reflects exactly what the runtime would dispatch
   and *why* something is deferred.
8. As the operator, I want the Obsidian projection and `is_human_actionable` to
   derive blocked-ness from `lithos_task_blocked` rather than
   `metadata.depends_on`, so that the vault's `⛔` decoration and the
   `include_blocked` filter reflect Lithos's authoritative blocked set
   (including gate and cycle blockers), not a metadata heuristic.
9. As the operator, I want `project import` (and `task_graph.build_plan`) to
   create the imported tree as an `epic` per parent with its children carrying
   `parent_task_id`, and `depends_on=` (→ `blocks` edges) for `[sequential]`
   chains, instead of `metadata.depends_on` / `metadata.parallelizable`, so that
   imported projects are scheduler-aware the same way decomposed PRDs are, and
   the indentation→graph logic has one representation.
   *(Shipped — and a live bug fix, not just a refactor: Lithos had begun
   rejecting `metadata.depends_on` with `invalid_metadata_key`, so importing any
   doc with indented children was failing at the first parent. See §3 above for
   the type vocabulary this settled.)*

### H — Human-merge gate (retire the `loom_delivered` hack) — ✅ SHIPPED

> US10–US13 shipped (PRs #261/#262/#263 + the US11 removal PR). US11 deleted the
> `loom_delivered` marker + guard and the legacy `develop_pr_url` merge sweep; a
> delivered story's `pr` gate is now the sole "awaiting merge" state and the sole
> re-dispatch guard (via the runner's `lithos_task_ready` check). The user
> stories below are kept as the design record.

10. As the operator, I want `story-develop`, on approval + PR delivery, to
    create a `pr` gate task (`gate_type=pr`, `metadata={repo, pr_number,
    required_state=merged}`) and a `waits_on_gate` edge from the gate to the
    story, so that "awaiting human merge" is a first-class, queryable blocker
    instead of a `metadata.loom_delivered` flag only the runner understands.
11. As the daemon, I want the route-runner's `completes_task = false` path to be
    replaced by gate creation: on `succeeded` for a PR-producing route, the
    story task is completed *only when its `pr` gate resolves*; the runner
    releases the claim and the gate carries the wait, so that a daemon restart
    needs no `loom_delivered` marker (the gate is the durable state) and an
    approved story never closes a github-linked issue for un-merged work.
12. As the operator, I want the GitHub watcher to resolve open `pr` gates — each
    poll, for `gate`-typed tasks with `gate_type=pr`, check the PR's merge state
    and `lithos_task_complete` the gate on merge (which unblocks the story and
    its dependents) — so that merging a PR auto-advances the chain, using the
    poller that already runs, and replacing the unbuilt `story-review-human`.
13. As the operator, I want gate resolution to post a `[GateResolved]` finding on
    the gated story (gate type, PR/run reference, resolving agent), so that the
    task history reconstructs why a story unblocked without reading watcher
    logs.

### A1 — Plugin SDK + bash-runner

14. As a plugin author, I want a `lithos_loom.plugin_api` library exposing the
    `Plugin` base class and helpers for emitting findings, reading task
    metadata, opening worktrees, launching coding agents, writing `result.json`
    atomically, and **graph helpers** (`spawn_followup`, `create_gate`,
    `link_edge`), so that a new plugin is a short script and graph-aware
    behaviour is one call, not a re-derivation.
15. As a plugin author, I want plugins installable as separate uv packages via
    Python entry points, so that I can ship a plugin from another repo without
    forking Loom.
16. As an operator, I want a built-in `bash-runner` plugin that wires a route's
    shell command's stdout/stderr/exit code into a `result.json`, with optional
    `outputs` globbing + Lithos upload, so that simple plugins need no Python.
17. As a plugin, I want to optionally write a JSONL event stream at
    `{work_dir}/{task.id}/events.jsonl` (`step.started`, `agent.turn`,
    `commit.detected`, `pr.opened`, `gate.created`), so that lithos-lens can
    follow live progress instead of waiting for `result.json`.
18. As an operator, I want plugins to accept an `--idempotency-key` (default
    `task.id`) and short-circuit with the prior `result.json` if a run with that
    key already completed, so that duplicate triggers (manual re-run, an A2A
    race with the ready-poller) don't double-act.

### A2 — `prd-generate` + PRD review (front of pipeline)

19. As the operator, I want a `prd-generate` plugin that turns a free-text
    feature description (a task tagged `trigger:prd-generate`) into a
    Pocock-shaped PRD knowledge doc (`tags: [prd, project:<x>, draft]`) and
    spawns a follow-on review task via `lithos_task_spawn(discovered_from)`, so
    that I can start from a paragraph rather than hand-writing the PRD.
20. As the operator, I want a `prd-review-agent` plugin that runs an agent over a
    draft PRD and posts one `[PRDReview]` finding per issue with
    `recommendation: revise | approve`; on `revise` it spawns a `prd-fix` task,
    so that obvious problems heal before I see the PRD.
21. As the operator, I want `prd-review-human` implemented as a **`human` gate**:
    it creates a `gate` task (`gate_type=human`, `reason`, `approval_required_
    from`) linked to the PRD's decompose task and posts `[ReviewPending] PRD
    ready: <doc-link>`; I resolve it by completing the gate (a tick in Obsidian /
    a CLI call), so that approval is a first-class wait, not tag-transition
    polling. Resolving it unblocks `prd-decompose`.
22. As the operator, I want a `prd-decompose` plugin that reads an approved PRD,
    runs one structured-output Claude turn (Pocock `to-issues` shape), writes one
    `task_record` story doc per story (`derived_from_ids: [prd_id]`), creates the
    `loom/<prd-slug>` integration branch, and creates the story **DAG**: the
    decompose task as `epic`, each story a plain `task` with `parent_task_id` +
    `depends_on=` (→ edges), default strict-sequential, retrying once on
    schema-invalid output, so that handing Loom a Pocock PRD yields a runnable,
    hierarchical pipeline.
23. As the operator, I want approving a PRD (resolving its `human` gate) to retag
    it `trigger:prd-decompose` and clear `draft`, so that decomposition kicks off
    automatically once the gate clears.

### A3 — story review policy + `story-fix`

24. As the operator, I want each project to declare `review_policy =
    "always-human" | "always-agent" | "every-n" | "brain-decide"` (in
    project-context metadata, ADR-0001 style), so that critical projects stay
    tightly controlled and low-stakes ones run more autonomously. (`story-develop`
    already performs the *agent* review pass internally; this selects whether a
    **human** `pr` gate is also required and how often.)
25. As the operator, I want `every-n` to attach a `human` gate after every Nth
    story in an epic (instead of a `pr` gate that auto-resolves on merge), so
    that I keep spot-check oversight without reviewing every diff.
26. As the operator, I want a `story-fix` plugin that, given a story branch + the
    reviewer's structured findings, runs the coder to land a fix-up commit,
    retrying up to `max_fix_attempts` (default 3) before spawning a
    `story-needs-human` task via `lithos_task_spawn`, so that minor issues heal
    without backing out and loops are bounded. (`story-develop`'s own dialogue
    loop is the first line of defence; `story-fix` is for post-merge / review-
    gate rejections.)

### A9 — `lithos-coding-mcp` (knowledge feedback loop)

27. As a coding agent inside `story-develop`, I want a small `lithos-coding-mcp`
    tool surface — `get_implementation_context`, `get_architecture_decisions`,
    `write_adr`, `log_finding`, `report_contradiction`, plus
    `spawn_followup_task` (a thin `lithos_task_spawn(discovered_from)` wrapper) —
    so that I can pull related ADRs on demand, write new ones back with correct
    provenance, and record discovered work as a real graph node instead of a
    prose finding.
28. As the operator, I want `lithos-coding-mcp` published as a uvx-installable
    package with `CLAUDE-LITHOS.md` / `AGENTS-LITHOS.md` skill files and a
    `launch <agent>` subcommand for ad-hoc KB-aware sessions, and `story-develop`
    to inject `LITHOS_TASK_ID` / `LITHOS_PRD_ID` / `LITHOS_AGENT_ID` /
    `LITHOS_URL` so writes attribute correctly, so that the integration works
    both inside Loom and standalone, with project repos staying Lithos-free.

### A4 — `decide-next` brain

29. As the operator, I want a `decide-next` plugin that reads an epic's state via
    `lithos_task_children(epic, recursive=true)` + `lithos_task_blocked` +
    recent findings (no metadata scraping), runs a structured-output Claude turn,
    and emits one of `escalate_to_human` (create a `human` gate),
    `retry_failed`, `batch_fix(scope)`, `merge_now`, `cancel_remaining`, so that
    non-trivial workflow decisions are delegated to a model with the graph as
    input.
30. As the operator, I want `decide-next` invocable both as a route handler and
    as a sub-call from other plugins (e.g. `story-fix` after max attempts), with
    a per-project decision prompt and a `[BrainDecision]` finding (prompt +
    structured output + chosen action) on every call, so that the brain is
    reusable and auditable.

### A5 — crash recovery + `loom-improve`

31. As the operator, I want plugins to write `{work_dir}/{task.id}/progress.json`
    checkpoints and an exit hook that salvages uncommitted worktree changes
    (`loom: salvage WIP from <task_id>`) on mid-turn death, and startup
    orphan-claim cleanup to post `[Recovery]` findings pointing at the last
    checkpoint / salvage SHA, so that a crash mid-run leaves recoverable
    breadcrumbs. (`story-develop`'s on-disk session transcripts already give it
    resume; this generalises recovery to all plugins.)
32. As the operator, I want a scheduled `loom-improve` task that aggregates
    `[Friction]` findings since last run, classifies them by theme, and **spawns**
    improvement tasks (`lithos_task_spawn`, tagged `improvement`) linked back to
    the friction sources, so that pain feeds back into the queue as real,
    provenance-linked work reviewable in lithos-lens.

### A8 — `merge-stories`

33. As the operator, I want a `merge-stories` plugin as the terminal task on an
    epic that runs the project's `make ci` behind a **`ci` gate** (`gate_type=ci`,
    `provider`, `run_id`, `required_status`) — the plugin creates the gate and
    runs CI, resolving the gate on green — so that integrated-stories-play-
    together is a first-class wait, fail-fast on red spawns one `story-fix` task
    per failing test (via `lithos_task_spawn`), and green opens the final PR to
    `main` with a synthesised changelog, tagged with the project + linked to the
    epic. Epic roll-up (extension Phase 4) closes the epic when all its children
    resolve.

### A6 — A2A endpoint

34. As Agent Zero / Hanuman, I want a FastA2A-compatible endpoint (default port
    9100) exposing `run task <id>`, `status`, `cancel <id>`, `reload config`,
    `list routes`, and **`ready [project]`** (a thin `lithos_task_ready`
    passthrough), so that strategic agents can trigger work, see what's running,
    and ask "what's runnable now?" through the same graph the daemon dispatches
    from — without waiting for a poll interval.

### A7 — multi-host, PRD-affinity, GitHub webhooks

35. As the operator, I want each Loom to bind a host-identified agent ID and its
    own project registry (paths resolvable only on that host), and `prd-decompose`
    to stamp each story `metadata.host_affinity = <host>` (releasable once the
    integration branch merges to `main`), so that two workstations coexist,
    worktrees stay on the host that owns an epic, and new PRDs balance across
    hosts. Host-affinity is a Loom claim-filter on top of `lithos_task_ready`
    (the ready-queue is host-agnostic; affinity is execution policy).
36. As the operator, I want a `lithos-loom webhook` mode (signed GitHub
    `pull_request` events) that **resolves `pr` gates** in seconds instead of the
    poll interval, falling back to the watcher's polling when no event arrives,
    so that review-to-unblock latency on a 28-story PRD drops from minutes to
    seconds — reusing the gate object from US-10, not a parallel mechanism.
37. As the operator, I want each Loom to subscribe to Lithos's SSE
    `task.created` / `task.updated` / `task.completed` (already the spine) and
    re-evaluate readiness on each, idempotent against the existing flow (claim is
    collision-safe), so that interactive task creation (e.g. A2A from Agent Zero)
    dispatches immediately.

### Cross-cutting

38. As the operator, I want `story-develop` (and implementation-shaped plugins)
    to post a `[Plan]` finding before the agent runs (framing: brief excerpt,
    integration branch, base SHA, acceptance criteria) and a `[Drift]` finding
    after (built vs. acceptance criteria via a short structured call), with
    `[Drift]` queryable as a class for `loom-improve`, so that under-/over-
    delivery is visible without me reading the diff.
39. As the operator, I want token/cost/turn metrics parsed from stream-json and
    posted as a `[Cost]` finding, a `lithos-loom dashboard` CLI (in-flight tasks
    per host, recent findings, 24h cost — fed by `lithos_task_children` +
    `lithos_task_ready`), a `lithos-loom replay <task_id>`, OpenTelemetry traces
    matching Lithos's config, and a `systemd --user` unit, so that the daily-
    glance and operations surfaces exist once the daemon is trusted.

## Implementation Decisions

**Modules unchanged from the shipped spine:** supervisor, EventBus, sources,
plugin-runner, result-file IO, TOML config, worktree/agent/git runner helpers,
`story-develop` internals. The `result.json` contract is unchanged (the
`resume`/`resume_after` surface added for T10 stays).

**Modules deleted (subsumed by the extension):**

- `RouteRunner._deps_satisfied`, `main._resolve_dep_statuses`, the dry-run
  `_route_outcome` / `_pending_deps` dependency mirror, `errors.DependencyCycleError`.
- The `completes_task = false` / `metadata.loom_delivered` path in the runner
  (replaced by gate creation, US-10/11).
- The `story-implement` / `story-review-human` plugin packages + routes (US-2).

**New / changed deep modules:**

- **Graph client** — typed `LithosClient` methods for the new tool surface
  (US-3). Deep module: one method per tool, error envelopes → typed exceptions,
  invariant across plugins.
- **Ready-dispatch** — the route-runner consults `lithos_task_ready` and reacts
  to newly-unblocked tasks (US-4/6). Pure-ish over Lithos state; testable with a
  stub client.
- **Gate lifecycle** — create-gate (plugins) + resolve-gate (GitHub watcher /
  webhook). Deep module: `create_gate(type, metadata, gated_task)` and
  `resolve_gate(gate_task)`; isolates the gate shape from `story-develop` and
  `merge-stories`.
- **Plugin SDK** (`lithos_loom.plugin_api`) including graph helpers (US-14).
- **Decision prompt runner** (used by `prd-review-agent`, `story-fix`,
  `merge-stories` classifier, `decide-next`, `loom-improve`, `[Drift]`).
- **Webhook receiver / SSE readiness** (US-36/37) — additive sources.

**Task metadata (orchestration-only) after the extension:**

- `project`, `integration_branch`, `prd_doc_id`, `story_doc_id` — unchanged.
- `depends_on` / `blocked_on` — **gone** (rejected by Lithos; expressed as
  `blocks` edges).
- `parent_task_id` — superseded by `parent_child` edges (set via `task_create`'s
  `parent_task_id` arg, which creates the edge).
- `parallelizable` — **gone** (US9). Superseded by the structural absence of a
  `blocks` edge; nothing writes it and nothing ever read it. Concurrency policy
  is `max_concurrency` (#85) until extension Phase 4 promotes priority/
  parallelism to first-class.
- `host_affinity`, `review_policy_override`, `friction_count`, `cost_total_usd`
  — as in the old full PRD (A7/A3/A5/cost).
- Gate-bearing tasks carry the extension's gate metadata (`gate_type` + per-type
  fields); Loom reads/writes these via the gate-lifecycle module.

**Config schema additions** (over the shipped `[orchestrator]` / `[projects.*]`
/ `[[routes]]` / `[[subscriptions]]` / `[obsidian_sync]` / `[github_watcher]`):

- `[orchestrator]` gains `mode = "polling" | "webhook"`, `webhook_port`,
  `a2a_port`; `max_concurrency` finally enforced (#85).
- `[projects.<name>]` gains `review_policy`, `claude_config` (consumed once A9
  lands), `host_affinity` override.
- `[[routes]]` gains optional `next_route` chaining, `[routes.match.conditions]`
  metadata gating, `decide_via_brain`, `idempotency_key` template.
- New `[loom_improve]` with `schedule_cron`, `friction_lookback_hours`,
  `max_themes`.

**Routing/chaining:** tag handoffs remain the default; `next_route` makes
sequential chains (prd-generate → review → decompose) rename-robust; the brain is
invoked only on `decide_via_brain` / `review_policy = "brain-decide"`. Gates,
not tags, carry *waits* (human/pr/ci/timer).

**Concurrency:** `max_concurrency` per host; `max_concurrent_tasks` per project
per host; `decide-next` single-instance per epic. The ready-queue is the source
of *eligibility*; these caps are Loom's source of *admission*.

## Testing Decisions

Test philosophy unchanged: external behaviour, not implementation; recorded LLM
responses (vcrpy-style) gated behind `LITHOS_LOOM_REFRESH_FIXTURES=1` for any
plugin that calls a model.

**Mandatory unit coverage:**

- Graph client — happy path + each documented error code; assert the right tool
  is called with the right args and the envelope normalises.
- Ready-dispatch — table-driven: task in `ready` → claimed; task absent from
  `ready` (blocked/gated/claimed) → skipped; newly-unblocked from
  `task_complete` → re-evaluated; cycle blocker surfaced, not dispatched.
- Gate lifecycle — `create_gate` shapes the gate task + `waits_on_gate` edge;
  `resolve_gate` completes only on the satisfied condition; idempotent under
  re-poll; `pr` gate maps merge-state correctly.
- Plugin SDK — synthetic plugin exercises claim → emit findings → spawn → write
  result.
- Decision prompt runner, webhook receiver (signature valid/invalid/malformed,
  pull_request open/closed/merged → gate resolved), host-affinity resolver,
  stream-json metrics parser — as in the old full PRD.

**Integration coverage:**

- `prd-decompose` against the lithos-lens M1 PRD (live Claude): asserts 8–28
  stories, each ≥80-word brief + ≥2 acceptance criteria, the epic/child
  hierarchy exists (`lithos_task_children` returns them), `blocks` edges chain
  per the emitted dep list, integration branch created.
- `story-develop` daemon happy path → asserts the `pr` gate + `waits_on_gate`
  edge are created on approval and the claim released (no `loom_delivered`).
- GitHub-watcher gate resolution: mocked `gh pr view` sequence → asserts the
  `pr` gate completes on merge and `lithos_task_blocked` no longer lists the
  dependent.
- `decide-next` against fixture epic states (mixed children, all-done, dead
  branch) → asserts the right action; `merge-stories` green/red CI gate paths;
  `loom-improve` aggregation → spawned improvement tasks with provenance edges.
- A2A `ready` command returns the same set as a direct `lithos_task_ready`.

**Manual acceptance (not automated):** feature-description→merged-PR end-to-end
against a real project + real Claude/Codex; two-workstation concurrent epics.

## Out of Scope

Unchanged from the old full PRD, plus extension-specific exclusions:

- Web UI (CLI dashboard only); cloud / multi-tenant; >2-host coordination;
  real-time co-editing; replacing the coding agents; non-GitHub forges
  (possible via `bash-runner`); cost-optimisation model routing; cross-PRD
  dependency tracking as a first-class feature.
- **Re-implementing graph scheduling in Loom.** Readiness, cycle rejection,
  hierarchy, and ranking live in Lithos. Loom does not add a second scheduler.
- **Making Lithos poll.** Gate *resolution* (observing PR/CI/human state) stays
  Loom's job; Lithos remains a passive MCP server.
- **First-class priority/parallelism ranking** — deferred to the extension's own
  Phase 4; Loom keeps `metadata.priority` advisory until then.
- **The docker sandbox as a separate A10 deliverable** — `story-develop` already
  runs agents in hardened per-agent containers; the remaining hardening (egress
  allowlist, codex tool support, configurable agent model) is tracked as issues
  (#92, #94, #93), not re-litigated here.

## Further Notes

- **Why this is mostly subtraction.** The headline change is deleting Loom's
  hand-rolled scheduler and the `loom_delivered` hack and consuming Lithos
  primitives instead. Less Loom-private code, shared semantics, a deterministic
  resume point (`lithos_task_ready`), and the PR-wait, the unbuilt
  `story-review-human`, and the A7 webhook all collapse onto one gate object.
- **`story-develop` already delivered A10 and much of A3.** Containerised
  agents (A10) and the agent-review pass (A3's `story-review-agent`) ship inside
  `story-develop`. This plan keeps it canonical and builds the *graph* around it
  rather than re-deriving review/sandbox layers.
- **The GitHub watcher is the gate resolver.** It already polls GitHub and owns
  issue↔task mirroring; resolving `pr` gates is the same machinery pointed at PR
  merge-state. The A7 webhook is a latency upgrade to the same resolution, not a
  new path.
- **Dependency on the Lithos cancelled-blocker edit.** US-5/8/H rely on the
  companion edit to the extension so a cancelled blocker keeps dependents
  blocked (surfaced via `lithos_task_blocked`) rather than spuriously ready.
  That edit is being raised separately against the Lithos extension proposal.
- **Sequencing.** G (graph adoption) → H (PR gate) → A1 → A2 → A9 → A4 → A5 →
  A8 → A6 → A7 → cross-cutting. G and H are the daily-friction reducers and ship
  first; the rest follows the old full PRD's high-leverage-first order, adjusted
  because `story-develop` already absorbed A10 and part of A3.
- **Manual escape hatches preserved.** Any task can be hand-edited, re-tagged,
  and re-claimed; any gate can be completed by hand to force an unblock; failed
  runs retain their work-dirs. Built to fail safely, not to be unfailable.
- **Loom still runs on the host, not in docker** — worktrees, `claude` / `codex`
  / `gh` auth in `~/`, plugin subprocesses, and the gate-resolving `gh` calls all
  need host integration. Unchanged from the MVP rationale.
