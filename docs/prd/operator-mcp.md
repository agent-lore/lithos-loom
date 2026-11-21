---
title: Lithos Loom — Operator MCP
milestone: M-MCP
status: draft
references:
  - docs/SPECIFICATION.md §4.14 (`develop list` / `attach` / `dump` / `prune` — the readers this PRD reuses), §4.4a (`gates`), §2.2 "Reconciliation state (PRD S7)" (the snapshot precedent)
  - docs/adr/0011-pr-maintenance-invariants.md (§2 state lives in Lithos and the loom-local store was rejected; §3 one writer — the constraints a status snapshot must keep)
  - docs/prd/unattended-duration.md (U6 drop counters, U7 the service unit, decision 4 "no second journal")
  - AGENTS.md (gates are the guards; no Loom-private delivered marker; writes are atomic)
  - ../lithos-coding/docs/requirements.md (the sibling MCP, considered as this server's home and rejected — decision 1)
labels: [lithos-loom, operator, observability, mcp, planning]
---

# Operator MCP

> **Status (2026-10-16).** Draft. Written after the brief-review phase-2
> follow-ups (#455, #458), where most operator questions were answered by
> joining Lithos, run dirs, gate-keyed files and the daemon's terminal by
> hand. Not scheduled; no tasks filed.

## Summary

An operator agent (Claude Code or Codex, in an operator session) asks loom
the same few questions every day: is that run finished and how did it end,
what did it cost, did the watcher pick up the reviewer's comment, why is that
story not running. Loom can answer most of them, but each answer is spread
across four places — story and gate metadata in Lithos, the run dir's
`state.json` and handoffs, gate-keyed files under `work/github-watcher/`, and
log lines on the daemon's terminal — and the agent reaches them by guessing
paths, parsing text tables and reading a tmux pane. Some answers are recorded
nowhere: what the route-runner is holding and why, the watcher's per-PR
decisions, a converge run's verdict in its own run dir. Some are recorded and
then deleted: a delivered run's handoffs. One reads wrong: `develop list`
says `done` for a run that stalled.

The proposal has two halves. First, make the records complete and durable:
every run says how it ended and what it cost, a delivered run keeps its
record, and each daemon child publishes what it holds in memory as an atomic
status snapshot. Second, `lithos-loom mcp`: a read-only stdio MCP server that
an agent session launches, calling the same readers as the CLI and returning
bounded, structured answers. It is its own process, not part of the daemon,
so it answers while the daemon is down or draining — when diagnosis matters
most.

Lithos stays the source of truth for what it holds. A status snapshot carries
only what lives in process memory, is never read by loom's own decisions, and
is not a second journal.

## Evidence

| Observation | Where |
|---|---|
| `develop list` showed converge run 6ac7617c as `done`; it had stalled at round 4 and pushed nothing | `active` is `done` whenever no container runs (`cli/develop.py:527-537`); the verdict `stalled` is only `state.json`'s top-level `status`; "unpushed" is inferred from a `git rev-parse` in the worktree (`:321-339`) |
| A converge run's own verdict (`not_converged`, `pushed: false`, $29.45) is not in its run dir | `ConvergeResult.to_json()` goes to stdout and the `--json` file only (`cli/converge.py:566-569`); for a watcher run that file is `github-watcher/remediation-<gate_id>.json`, keyed by gate and unlinked at the gate's next dispatch (`subscriptions/external_remediation.py:816-822`) |
| "Did the watcher pick up the external comment?" took four sources | `[ExternalReview]` findings, the gate's `external_remediation` and `reconciliation_*` metadata (`subscriptions/remediation_budget.py:44-123`, `reconciliation_state.py:75-89`), and the converge run dir; the dispatch decision (`deferred_busy`, `no_trusted`, `held_infra`, …) is a log line only (`subscriptions/_develop_pr_merge.py:410`); activity skipped for the automated marker leaves no trace (`subscriptions/external_reviews.py:404-414`) |
| The daemon's log is a terminal | stdlib logging to stderr, no file handler (`main.py:126-133`, `children/_boot.py:69-92`); the live supervisor's fd 2 is `/dev/pts/13`, a tmux pane; a converge subprocess's output is buffered until it exits (`subscriptions/_subprocess.py:53-72`) |
| What the route-runner holds is invisible | admission's deferred, in-flight and FIFO state and the ready re-checker's sleepers are process memory (`subscriptions/admission.py:231-256`, `ready_recheck.py:59-149`); a `limit` or `queued` hold is an INFO line; Lithos hears of a hold only at the total cap (`[AdmissionHeld]`, `admission.py:719-753`); the in-flight task id is only an argument of `_run_claimed_task` (`route_runner.py:717`) |
| A delivered run's handoffs are deleted | success removes the whole `<work_dir>/<task_id>` (`subscriptions/route_runner.py:1255-1261`); the completion store keeps the `result.json` payload, without cost or handoffs (`plugins/story_develop/idempotency.py`); the phase-2 tally needed a detached copier during every run and still lost K2-S1's rounds and K2-S3's last round |
| Cost reads lower than spend | codex turns count $0 (`plugins/story_develop/engines.py:350`, `meters_cost_usd = False`, the #102 boundary); `state.json` keeps only the total, not coder vs reviewers (`plugins/story_develop/develop.py:733`); 6ac7617c's correctness reviewer was codex; a converge that stops at triage records no cost (4ff6b1d4) |
| `prune` keeps converge checkpoints forever | 4f06f92d and b2d3f6e1 classify `resumable`, but nothing resumes a converge run (`cli/develop.py:1138-1255`) |
| The readers are private to the largest module | `_iter_run_dirs`, `_run_info`, `_display_round`, `_agent_state` are `_` helpers in `cli/develop.py` (2047 lines; the stop-loss is 2100) |
| No liveness record for the children | child pids appear only in a log line (`supervisor.py:161`); `sse_cursors.json` changes only when an event arrives (`cursor_store.py:52-131`) |
| This session's Lithos MCP connection was refused while the daemon's own client worked | every Lithos read went through a scratch script; an operator surface on one more network endpoint inherits that failure |

## Problem statement

1. **Every answer is a join.** Each operator question is a hand-built join
   over Lithos, run dirs, gate-keyed files and a terminal, rebuilt in every
   session.
2. **Some facts are never recorded**: the route-runner's holds and in-flight
   run, the watcher's per-PR decisions, a converge run's verdict in its run
   dir, cost by agent.
3. **Some records are deleted while still wanted**: a delivered run's handoffs
   and state; a gate's last remediation result at its next dispatch.
4. **Some readings mislead**: `done` means "no containers", not "finished"; a
   USD total silently leaves out codex.
5. **There is no typed surface.** An agent re-derives paths, parses text
   tables through a shell (a permission prompt each time), and cannot reach
   the daemon's log at all.

## Stories

1. **O1 — A run's record says how it ended and what it cost.** As the
   operator, I want every run dir's `state.json` to carry its final verdict
   and spend — for converge, a `converge_result` block (status, pushed,
   pushed sha, fixer commits, total cost) written at every exit after the run
   dir exists; for every run, coder and reviewer cost separately plus the
   names of agents whose engine does not meter USD; for a converge that stops
   at triage, its triage cost — so that one file answers "how did it end, what
   did it cost" without the gate-keyed remediation file or a worktree
   `git rev-parse`. Written through `run_outcome` (the pinned leaf),
   atomically. Acceptance: a fixture converge run that stalls with a codex
   reviewer records `converge_result.status: not_converged`, `pushed: false`,
   its total, and `unmetered: ["correctness"]`; every `ConvergeStatus`
   reachable after intake writes the block.
2. **O2 — One reader, two renderers.** As the operator, I want the run
   readers moved out of `cli/develop.py`'s private helpers into a public
   reader module that both `develop list` and the MCP call, and `develop list`
   to show `status` (the verdict, or `running`), `cost` and `containers` in
   place of the single `active` column, so that a stalled run never reads
   `done` and the CLI and the MCP cannot disagree. Acceptance: 6ac7617c lists
   as `stalled`, unpushed, $29.45 plus one unmetered agent; a run in its
   startup window lists `running`; `--format json` carries the same fields;
   SPEC §4.14 and `CONTEXT.md` (run record) updated.
3. **O3 — A delivered run keeps its record.** As the operator, I want success
   to reap a run's heavy parts (worktree, `agents/`, `test_gate/` caches) and
   keep its record (`state.json`, `handoff/`, `conversation.md`, `task.json`,
   `result.json`) for `retain_succeeded_days` (default 14), with `prune`
   removing records past that age and treating a converge checkpoint as
   finished, so that handoffs survive delivery without a copier racing the
   reaper. Risk: any reader that takes a task dir's existence to mean "in
   flight" or "resumable". The story enumerates every reader (resume pointer,
   `prune`, `attach`, `list`, the completion-store fallback) and tests that a
   kept success is neither. Acceptance: after a delivered fixture run,
   `handoff/` and `state.json` exist and the worktree does not; `prune
   --dry-run` past the age marks it finished; a 4f06f92d-shaped converge
   checkpoint classifies finished.
4. **O4 — Each daemon child publishes what it holds in memory.** As the
   operator, I want each child to write `<work_dir>/<child>/status.json`
   atomically on every change and at least every 30 s, and `lithos-loom
   status` to print it, so that "why is this story not running" and "what did
   the watcher decide for this PR" are answered without the terminal.
   - *route-runner*, per route: the in-flight task (and its run id once the
     run dir exists), held stories with the admission verdict and detail, the
     ready re-checker's sleepers (why, attempts, next check), armed
     usage-limit resumes, queue depth and dropped events (U6's counters).
   - *github-watcher*: last sweep start and end and gates seen, next sweep,
     the single-flight remediation in flight (gate, PR, since), and for each
     open `pr` gate the last decision labels — remediation (`consider` and
     dispatch labels), merge-gate, conflict-resolve — including review
     activity skipped for the automated marker, each with its time.
   - *obsidian-sync*: liveness and last projection and push.

   Every snapshot carries `schema`, `pid`, the pidfile's boot identity and
   `written_at`; a reader that finds its writer dead reports the snapshot
   `stale` with its age, never as current. Observability only: a guardrail
   test fails if any dispatch, admission, gate or remediation module imports
   the snapshot reader, and nothing Lithos holds is copied in beyond ids.
   ADR 0013 records the rule; `CONTEXT.md` gains "status snapshot".
   Acceptance: a story held at `max_open_delivered_prs` appears in the
   route-runner snapshot with reason `limit` within one write; a comment
   carrying the automated marker shows as skipped in the watcher snapshot;
   after `kill -9` of the daemon, `lithos-loom status` reports every child
   stale.
5. **O5 — `lithos-loom mcp`.** As the operator agent, I want a stdio MCP
   server (FastMCP, from the `mcp` package loom already depends on) that my
   session launches with the CLI's config discovery and that exposes the read
   tools below, so that a question is one typed tool call instead of a path
   guess and a table parse.
   - **Read-only by construction.** The server holds a read-only Lithos view
     (only get, list and query methods), never writes to disk, never signals
     the daemon, never pushes. A test runs every tool against a recording
     fake and asserts zero writes.
   - **Untrusted text stays data.** Handoff bodies, finding bodies, PR review
     text and task descriptions are written by agents or outside reviewers.
     They are returned fenced (the `fence_untrusted` convention) in their own
     fields and never spliced into the tool's own prose (cf. #459).
   - **Bounded.** Every result has a size cap, with `truncated: true` and a
     cursor or a narrower query for the rest; a handoff read is one round and
     one agent, capped below `read_handoff`'s 1 MiB.
   - **Validated input.** Run ids, task ids, round numbers and agent names
     are checked against their formats and resolved inside `work_dir`; no raw
     input is joined into a path.
   - **Degrades, never hangs.** Lithos calls time out quickly. Without
     Lithos, disk tools still answer and Lithos tools return `{status:
     "error", code: "lithos_unreachable"}`. Without docker, `containers` is
     `unknown`. Without the daemon, `daemon_status` says so and gives each
     stale snapshot's age.
   - **Registered at user scope** (`claude mcp add --scope user …`, Codex
     `[mcp_servers]`), so project repos stay clean; README documents both.
   - **Mapped.** A new `mcp_server` package in the Entrypoints tier beside
     `children`, with its `architecture.toml` entries and import-linter rows.
     The expected growth of `cross_component_edges` (+2 to 4, now at its
     limit of 72) is raised with the reason recorded. SPEC §4.4c.

   Acceptance: each tool in the table below has a test over fixture run dirs
   and a fake Lithos; a live session answers the five questions in the
   summary for #458's runs without a shell.
6. **O6 — The daemon log is readable (only on evidence).** As the operator,
   I want an optional `orchestrator.log_file`, rotated, that the supervisor
   and children write to, and a bounded `daemon_log(contains, child, since,
   limit)` tool — built only if, after O4, questions still need the
   terminal. If U7's service unit lands first with journald, the tool reads
   that instead.

## Tool surface (O5)

| Tool | Answers | Reads |
|---|---|---|
| `daemon_status()` | Is the daemon up; what is each child doing; what is held and why | pidfile lock (`runner/pidfile.py:160`), O4 snapshots, Lithos reachability |
| `list_runs(kind?, task?, since?, limit?)` | Which runs exist, how each ended, what each cost | run dirs, through the O2 reader |
| `get_run(run_id)` | One run: verdict, failure reason, rounds, cost by agent, open findings, blocking checks, test gate, branch and head, converge facts, the handoff index | `state.json`, `result.json`, `delivery.json`, `external.json`, the `handoff/` listing |
| `read_handoff(run_id, round, agent)` | One round's coder or reviewer handoff, round 0 (triage, brief review) included | `handoff/`, through `read_handoff` |
| `get_story(task_id)` | Loom's view of a story: status, `develop_*` metadata, last attempt and resume records, brief-review records, gates (from `waits_on_gate` edges), recent loom findings, its runs on disk, any hold | Lithos, run dirs, the route-runner snapshot |
| `get_pr(pr_url or gate_id)` | What loom is doing for a delivered PR: reconciliation state, remediation budget and last result, the run in flight, merge-gate and conflict records, the watcher's last decisions | `pr` gate metadata, `github-watcher/*.json`, the watcher snapshot, converge run dirs |
| `list_gates(shadow?)` | Open gates and their waiters' health | `collect_gate_rows`, `collect_shadow_rows` |
| `brief_reviews(project?, since?)` | Brief-review records | `collect_brief_reviews` |

## Decisions

1. **A separate stdio process in the loom package** (2026-10-16). Not in the
   daemon: its children share nothing in memory, a server inside one would
   go down with it, and a separate process still answers while the daemon is
   down or draining. Not in lithos-coding: that package is coder-side Lithos
   KB access inside one task and has no loom dependency by design (its
   NFR-1), so serving loom would mean importing loom or re-implementing
   loom's on-disk readers in a second repo. Not HTTP: no port, no auth, no
   lifecycle tied to daemon restarts.
2. **Read-only.** `deliver`, `converge-push`, gate completion and `drain`
   stay on the CLI, where the operator sees them happen. Revisit only with
   evidence that a write tool would remove real work.
3. **Each fact has one home.** Lithos for what Lithos holds (task and gate
   metadata, findings); the run dir for a run's record; a status snapshot
   only for process memory. The server joins them and copies nothing.
4. **Snapshots, not following.** No blocking wait tool: MCP clients time out
   tool calls, and following a run is `develop attach` or a polling loop.
   Revisit if polling `get_run` proves costly.
5. **Cost is reported with its gaps**: metered USD plus the names of
   unmetered agents, never a silent total (the #102 boundary).

## Sequencing

O1 and O2 first. They fix the records and the CLI straight away, touch only
plugin and CLI code (which load from the checkout, so no daemon restart), and
give O5 its reader. O5 next, with the run, story, PR, gate and brief-review
tools. O3 and O4 change the route-runner and the children, so each needs a
drain and restart; O4 brings `daemon_status`. O6 only on evidence.

Success: for a week of normal operation, the operator agent answers "is it
finished, how did it end, what did it cost, did the watcher pick it up, why
is it not running" from tool calls, with no shell over `work/` and no
terminal.

## Non-goals

- Write or control tools (decision 2).
- An HTTP or SSE service mode.
- Replacing Lens: Lens is the human dashboard over Lithos; this is the
  agent's query surface over loom (open question 1 covers what Lens might
  gain).
- Following a run live (decision 4).
- CLI `--json` parity for its own sake: the server calls the collectors, not
  the CLI.
- Dollar-accurate codex cost.
- Serving Lithos facts loom does not own; the Lithos MCP serves those.
- Coder agents in the sandbox calling the server.

## Open questions

1. Should admission holds also be mirrored onto the story in Lithos, as
   reconciliation state is on the `pr` gate, so Lens can show "held: limit"?
   O4 keeps them in the snapshot only; a Lithos mirror is a write per hold
   change and needs ADR 0011's one-writer rule applied to it.
2. Bring the guardrail kit's tool-catalog adapter (`tests/guardrail/AGENTS.md`
   describes it; `_tool_catalog.py` is not in this repo) so `docs/generated/`
   lists the MCP tools and drift fails CI? `architecture.toml`'s comment that
   loom "is an MCP client" goes stale either way.
3. O3's default age and what it keeps: `agents/` holds the transcripts — the
   only record of codex token use — but is the largest part after the
   worktree.
4. O6: a log file, or journald through U7's unit?

## Provenance

| Story | From | Carried on |
|---|---|---|
| O1, O2 | The #458 follow-up (2026-10-15): 6ac7617c read `done`, and its verdict was found only in the gate-keyed remediation file | 2026-10-16 |
| O3 | The phase-2 tally (10cc6310): detached handoff copiers; K2-S1's and K2-S3's rounds lost | 2026-10-16 |
| O4, O6 | "Did the external reviewer's comment get picked up?" (2026-10-15) and the route-runner's silent queue | 2026-10-16 |
| O5 | Operator discussion, 2026-10-16: an MCP in the loom repo, separate from the daemon; lithos-coding considered and rejected | 2026-10-16 |
