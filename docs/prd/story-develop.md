# PRD: `story-develop` plugin — automated conversational code review

> **Status:** Approved design, not yet implemented.
> **Date:** 2026-06-11
> **Deciders:** Dave Snowdon
>
> Self-contained — no external document is required to implement this; all needed
> background is inlined.

## Context & background

Today, Dave gets materially better results by running a **coder** agent and a **reviewer**
agent side-by-side and hand-shuttling each review and each response between them: the agents
build shared understanding across rounds, disagreements get resolved by conversation, and
the "same wrong fix applied repeatedly" oscillation disappears. The cost is tedium —
switching terminal panes, copying output, pasting it into the other agent's prompt.

The predecessor tool, **Ralph++**, automated a *sequential, fire-and-forget* loop instead:
spawn a coder, wait, spawn a fresh reviewer, collect findings, spawn a fresh fixer with the
findings, re-review. Every turn destroys and recreates the process, so no agent remembers
what it already tried or objected to. There is no dialogue — the fixer can't ask "what did
you mean?", the reviewer can't refine a vague point, the coder can't push back on a finding
it disagrees with. Ralph++ detected oscillation (Jaccard similarity of findings) but could
not break out of it.

`story-develop` automates Dave's *conversational* model. It accepts a task, has a coder
implement it, runs one or more reviewers with **dialogue-based iteration**, and completes
when all reviewers approve (or stops safely). The coder is a **single persistent session**
that both implements and later fixes, so it keeps its own context across rounds — the whole
reason the conversational model beats the fire-and-forget one.

**Two operator requirements shaped this PRD beyond a naive port:** graceful degradation when
a coding agent hits a provider **usage limit**, and using **specialised reviewers** (e.g.
security) only for stories that warrant them — most don't.

**Reuse base (local copies in `~/agents/ralph-dev/`):**
- `ralph-sandbox` — a Docker sandbox that runs a coding tool (claude/codex) in a container
  with the project bind-mounted, the tool's config dir mounted (`/claude_config`,
  `/codex_config`), and a hardened profile (`cap_drop: ALL`, `no-new-privileges:true`). It
  is single-tool / single-container and compose-driven.
- `ralph-plus-plus` (`ralph_pp/`) — Python with reusable pieces: a CLI tool wrapper with
  exit-code capture, severity/LGTM parsing, `docker run` command building, disposable git
  worktree management, git helpers, and test-command auto-detection.

## Goals

- A **standalone CLI** that runs implement → review → dialogue → complete and hands back
  reviewed, ready-to-merge code on a branch — useful **today**, with no Loom daemon, event
  bus, or config infrastructure required.
- Reuse `ralph-sandbox` and salvage Ralph++ Python rather than writing net-new.
- Graceful **usage-limit** degradation; bounded, safe **autonomous** termination.
- A **zero-retrofit seam** into the Loom daemon: the same `develop()` core sits behind both
  the standalone CLI entry and the daemon's `--task-json/--work-dir/--result-file` contract.

## Non-goals (v1)

- The Loom daemon itself (SSE, claims, routes); we only add the `--task-json` entry later.
- A heuristic "router" agent that auto-selects reviewers (seam designed, not built).
- Daemon auto-re-dispatch of usage-limit-interrupted runs (plugin emits the signal; the
  daemon consuming it is a future feature).
- Building new agent container images beyond what `ralph-sandbox` provides.

## Decisions

1. **Scope = full develop cycle** from a task description. One **persistent coder session**
   implements *and* fixes, preserving in-session context across rounds.

2. **Execution = per-agent long-lived Docker containers.** One container **per agent**
   (1 coder + N reviewers), each started once at run begin and **kept alive across all
   rounds** so worktree state, warm caches, and each agent's session persist. Separate
   containers are required, not optional: coder mounts the worktree **read-write** while
   reviewers mount it **read-only**; each holds an independent session; agents may use
   different tools/images; and a reviewer tool-switch (see #4) replaces only that one
   container. Reuses `ralph-sandbox`'s image, config-dir mounts, and security profile;
   the multi-container orchestration on top is **net-new**. Model-provider network egress
   stays open (it must); the **push/PR capability is withheld from agents** and performed
   host-side instead.

3. **Session mechanism = resumable per-turn exec into the live container.** A turn is
   `docker exec <agent-container> claude --resume <session-id> -p "<prompt>"` (Codex
   equivalent) — a fresh process inside the **living** container, context restored from the
   on-disk session transcript. NOT a live tmux REPL driven by `send-keys`. This keeps the
   container warm (per #2) **and** gives **clean per-turn detection** of completion /
   usage-limit / malformed-handoff from **process exit code + stderr** — no ANSI scraping.
   `--resume` is also what makes daemon checkpoint-and-exit (#5) recoverable across a
   container teardown, so the transcript is the durable context and the live container is
   the within-run fidelity/perf boost. Rationale and trade-offs: see
   [ADR 0002](../adr/0002-story-develop-session-mechanism.md).
   - **Consequence:** there is no filesystem watch-loop, no filename-based routing, and no
     **handoff-forgery surface** — authorship is by *which container was exec'd*, not by
     trusting a filename. A silent agent is a subprocess timeout; a malformed handoff means
     validate the one expected file, then re-prompt that agent.
   - **Validation spike (Phase 1, do first):** confirm Codex headless `--resume`/equivalent
     restores context AND that skills/agents load under headless `-p`. Claude Code confirmed
     (Skill tool available headless; resolves from the mounted config dir). If `--resume`
     proves insufficient, fall back to a persistent interactive process (costing exit-code
     detection).

4. **Usage-limit reaction = role-aware hybrid.** Classify `usage_limited` from exit code +
   stderr. **Coder** → pause-and-wait for the window reset (cap `max_pause_minutes`, then
   switch-with-reseed or checkpoint). **Reviewer** → switch to the next tool in a per-project
   `fallback_chain` immediately (replace its container, reseed from handoff history); pause
   only if no alternate exists. Switching the coder is the last resort because its in-session
   context is the thing we are protecting.

5. **Pause = mode-dependent.** **Standalone:** block with a live countdown, keep the coder
   container warm, resume the same session on reset. **Daemon:** checkpoint-and-exit with
   `status:"interrupted"`, `error.category:"usage_limited"`, `resume_after:<ts>` + session
   ids; tear down (frees the slot). Resume state is ~free — session ids + handoffs are on
   disk. Daemon auto-re-dispatch is a future feature; until then `interrupted` = operator
   re-runs.

6. **Reviewer selection = explicit list + default.** Default: **one** `code-quality`
   reviewer. Specialised reviewers opt-in per run: standalone `--reviewer <name>`
   (repeatable) / `--develop-config <file>`; daemon per-project available pool + per-task
   `metadata.reviewers` (or tags) choosing which run; absent → project default. A future
   router agent populates the same list.

7. **Approval = severity-thresholded, per reviewer.** A reviewer passes a round if it
   signals **LGTM** *or* its highest open finding is below its `block_threshold` (default
   `major`; security typically `minor`). Sub-threshold findings are **recorded** (summary /
   PR / Lithos finding) but non-blocking. The run is **approved** when *all* active reviewers
   pass in the **same** round. Reuses Ralph++ `parse_max_severity` / `severity_at_or_above`.
   This kills nitpick non-convergence and lets security be strict while code-quality is
   lenient.

8. **Termination = guarded.** `max_rounds` + a **`max_cost_usd`** ceiling + a lightweight
   **stall guard** (the coder's round commit makes no change, OR a reviewer's blocking
   findings are unchanged, across 2 consecutive rounds → stop) + a coder **`disputed`**
   affordance (a finding marked disputed-with-rationale that persists 2 rounds → emit a
   `[ReviewDispute]` human breadcrumb and stop, rather than grind to max). On any
   stop-without-approval: **standalone** offers the operator an attach/intervene escape
   hatch; **daemon** writes `failed` + the conversation log + the finding.

9. **Delivery = per-round commits; branch + log always; PR opt-in.** The coder commits **per
   round** (locally; **push is host-side only**, never from an agent). Always produced: the
   committed branch + an ordered **conversation log** (all handoffs) + a summary. Standalone
   `--open-pr` pushes the branch and opens a PR via host `gh` (default **off**). Daemon:
   write `result.json` + post a findings summary back to Lithos; PR-opening and
   retag-for-human governed by config. Per-round commits also feed the stall guard (#8) and
   give round-over-round diffs for free.

10. **Tests & mounts = coder runs tests, reviewers read-only, host gate.** The coder (RW)
    runs the project's auto-detected tests and **records results in its handoff**. Reviewers
    mount **read-only** and review the diff + recorded output. The plugin optionally re-runs
    tests **host-side on each round commit** as an objective, agent-free gate (cheap,
    trustworthy). A specialised reviewer that must execute a tool declares a command run
    against a throwaway clone.

11. **Lithos I/O = plugin owns it directly.** With `--task-id` (and not `--no-lithos`) the
    plugin fetches the task itself and posts findings + a conversation-log summary back
    (`lithos_finding_post` / `task_update`). `result.json` still carries `status` for the
    daemon (which does not apply side effects yet), so there is no double-application.
    Standalone therefore gets a full Lithos round-trip **today**; daemon reuses the identical
    path. Honors `--no-lithos` for pure-offline runs.

12. **Acceptance criteria = shared, optional.** Optional `--acceptance-criteria <text|file>`;
    with `--task-id`, pull AC from the task body/metadata; else fall back to `--description`.
    Whatever "definition of done" exists is injected into the coder **and every reviewer**
    prompt as one source of truth, so reviewers judge against the AC (taste-level notes fall
    below `block_threshold`).

## Architecture

`__main__.py` (argparse) detects mode by presence of `--task-json`, builds a resolved
`DevelopConfig`, and calls the shared `develop()` core.

`develop()`:
1. Resolve config + acceptance criteria; create a per-task git worktree off `--branch`
   (default `main`).
2. Seed `.handoff/` with `FORMAT.md`.
3. **Start long-lived containers:** one coder container (RW worktree) + one per reviewer
   (RO worktree), each launched idle with the tool available for `docker exec`.
4. **Round loop** (explicit orchestration — no watch loop):
   - **Coder turn:** `docker exec` the coder (`--resume` after round 1); it implements/fixes,
     runs tests, commits, writes `round_NN_coder_done.md`. Validate the handoff; on malformed
     → re-prompt the same container.
   - **Reviewer turns:** `docker exec` each reviewer (RO) → `round_NN_review_<name>.md`.
   - Evaluate: severity-threshold approval, stall guard, dispute escalation,
     round/cost/time ceilings, and usage-limit reactions (pause coder / switch reviewer).
   - Optional host-side test gate on the round commit.
5. **Terminate:** success (commit / log / PR-or-result / Lithos post) | failed | interrupted,
   per mode. Tear down all containers; preserve the conversation log.

Per-agent container commands are built by an adapted Ralph++ `docker run` builder (per-agent,
**not** compose). Turns are sequential within a run; multiple concurrent runs each get their
own worktree, containers, and `run_id`.

### Handoff format

Each agent writes a structured-markdown sign-off to `.handoff/round_NN_<role>[_<name>].md`:

```markdown
## Status: FINDINGS | LGTM

## Summary
Brief description of what was reviewed/changed (coder also: test results).

## Findings (if status is FINDINGS)
### Finding 1: [severity: critical|major|minor]
Description...
```

The coder may additionally mark a reviewer finding `disputed` with a rationale (feeds #8).

## File layout

```
src/lithos_loom/plugins/story_develop/
    __main__.py     # argparse, dual-mode detect, build DevelopConfig -> develop()
    develop.py      # core: worktree, container lifecycle, round loop, termination
    turns.py        # docker exec a coder/reviewer turn; resumable session ids; exit parsing
    containers.py   # per-agent long-lived container launch/exec/teardown (adapt ralph_pp)
    handoff.py      # write FORMAT.md; parse/validate handoff; reuse base.py severity/LGTM
    limits.py       # usage_limited classification + role-aware reaction (pause/switch)
    lithos_io.py    # fetch task / post findings+summary (gated by --no-lithos)
    config.py       # DevelopConfig, reviewer config, --develop-config loader
    prompts/        # FORMAT.md, coder_init.md, reviewer_round.md, coder_fix.md
src/lithos_loom/runner/   # fill existing stubs: worktree.py, git.py
```

## Salvage map (from `~/agents/ralph-dev`)

- `ralph_pp/tools/base.py` → `ToolResult`, `is_lgtm`, `parse_max_severity`,
  `severity_at_or_above` (≈ verbatim).
- `ralph_pp/tools/cli_tool.py` → subprocess invocation, ARG_MAX/stdin handling, exit-code
  capture → basis for `turns.py` (adapted to `docker exec`).
- `ralph_pp/sandbox.py` + `ralph_pp/steps/sandbox.py` → `docker run` command building.
- `ralph_pp/steps/worktree.py` → disposable worktree create/cleanup → `runner/worktree.py`.
- `ralph_pp/steps/_git.py` → base SHA / commits-since / dirty → `runner/git.py`.
- `ralph_pp/detection.py` → `detect_test_commands` for the test gate.
- `ralph-sandbox` `docker-compose.*.yml` → mount/cap_drop/no-new-privileges model mirrored
  into `containers.py`.

## Phased build

- **Phase 1 — core loop (standalone, single reviewer).** Validation spike (Codex headless
  `--resume` + skills) FIRST. Standalone flags in `__main__`; container lifecycle +
  `develop()` round loop; `turns.py` + `containers.py`; `handoff.py` + `FORMAT.md`;
  per-round commit + host test gate; fill `runner/worktree.py` + `runner/git.py`.
  Deliverable: `python -m lithos_loom.plugins.story_develop --repo X --description Y` spins
  up coder + reviewer containers, iterates to approval, leaves a branch.
- **Phase 2 — multi-reviewer + resilience + polish.** N reviewers (consolidated default);
  per-reviewer severity thresholds; `--develop-config`; usage-limit role-aware reaction +
  countdown + reviewer container-replace; stall guard + dispute escalation + `max_cost_usd`;
  `--acceptance-criteria`; `--open-pr`; `lithos_io.py` (fetch/post); operator attach escape
  hatch.
- **Phase 3 — daemon integration (trivial).** `--task-json/--work-dir/--result-file`; atomic
  `result.json` conforming to `docs/result-schema.json`; checkpoint-and-exit interrupted
  path; route stanza in `examples/lithos-loom.toml`; plugin-runner integration test.

## Open questions (deferred, non-blocking)

- Daemon-mode reviewer-config home: Loom TOML `[projects.*.develop]` vs **project-context-doc
  metadata** to match [ADR 0001](../adr/0001-github-watch-config-storage.md) (github-watcher
  precedent). Decide in Phase 3.
- Whether `story-develop` supersedes the `story-implement` / `story-review-human` stubs or
  coexists as the conversational path.
- Project test suites needing secrets/env inside the sandbox (egress/env passthrough policy).

## Verification

- **Spike:** in `ralph-sandbox`, run `codex` headless resume across two invocations; confirm
  context restoration + a skill invocation both work; same for `claude -p --resume`.
- **Unit:** handoff parse/validate; severity-threshold approval; `usage_limited`
  classification from sample stderr; stall guard (unchanged round commit); reviewer-selection
  resolution. Port Ralph++ `test_lgtm.py`, `test_severity.py`, `test_worktree.py`.
- **Integration (standalone):** real run against a throwaway repo + trivial task; assert a
  branch with per-round commits, a conversation log, approval, and a clean host test gate;
  a forced-limit case (mock stderr) exercising coder-pause and reviewer-switch.
- **Pre-merge:** `make check` (ruff + ruff format + pyright + pytest) green; update
  `docs/result-schema.json` + `tests/test_plugin_runner.py` on contract changes;
  `examples/lithos-loom.toml` + `tests/test_config.py` on config-schema changes; update
  `docs/SPECIFICATION.md` for the new operator surface; ship ADR 0002.
