# PRD: `story-develop` plugin — automated conversational code review

> **Status: SHIPPED + ARCHIVED (2026-06-13).** Implemented across T1–T10 (walking
> skeleton → daemon integration); see SPECIFICATION.md §5.5 for the live operator
> surface. Archived here as design history. Residual follow-ups did **not** ship
> with v1 and are tracked as issues, not as open PRD scope:
> [#94](https://github.com/agent-lore/lithos-loom/issues/94) codex tool support
> (coder + reviewers; schema-ready, runtime-unbuilt — only `claude` turns are wired
> today, so the `fallback_chain` cannot switch to codex),
> [#93](https://github.com/agent-lore/lithos-loom/issues/93) configurable agent
> model / thinking-level, [#92](https://github.com/agent-lore/lithos-loom/issues/92)
> project-declared sandbox agent config + the egress-allowlist hardening this PRD's
> security section deferred, [#91](https://github.com/agent-lore/lithos-loom/issues/91)
> Copilot review comment-lag race, and
> [#88](https://github.com/agent-lore/lithos-loom/issues/88) an operator
> observe/attach CLI (the "attach escape hatch" is a printed hint today, not a real
> affordance). The post-extension reframing of the surrounding pipeline lives in
> [docs/prd/orchestration.md](../orchestration.md).
> **Date:** 2026-06-11
> **Deciders:** Dave Snowdon
>
> Self-contained — no external document is required to implement this; all needed
> background is inlined.
>
> **Gating:** the whole project is conditional on the
> [feasibility gate](story-develop-feasibility-gate.md) passing first.

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
  bus, or claim infrastructure required. (It does need a minimal local config — see
  decision #6 — not zero config.)
- Reuse `ralph-sandbox` and salvage Ralph++ Python rather than writing net-new.
- Graceful **usage-limit** degradation; bounded, safe **autonomous** termination.
- A **daemon seam** where the same `develop()` core sits behind both the standalone CLI and
  the daemon's `--task-json/--work-dir/--result-file` contract. Getting it *running* under
  the daemon is small wiring; **graceful auto-resume of usage-limit interruptions is a real
  contract change**, scoped honestly in Phase 3 — not "zero-retrofit".

## Non-goals (v1)

- The Loom daemon itself (SSE, claims, routes); we only add the `--task-json` entry later.
- A heuristic "router" agent that auto-selects reviewers (seam designed, not built).
- Daemon auto-re-dispatch of usage-limit-interrupted runs (plugin emits the signal; the
  daemon consuming it is a Phase-3 contract change, below).
- Building new agent container images beyond what `ralph-sandbox` provides.

## Decisions

1. **Scope = full develop cycle** from a task description. One **persistent coder session**
   implements *and* fixes, preserving in-session context across rounds.

2. **Execution = per-agent long-lived Docker containers.** One container **per agent**
   (1 coder + N reviewers), each started once at run begin and **kept alive across all
   rounds** so worktree state, warm caches, and each agent's session persist. The container
   is **not** cycled per turn. Separate containers are required, not optional: coder mounts
   the worktree **read-write** while reviewers mount it **read-only**; each holds an
   independent session; agents may use different tools/images; and a reviewer tool-switch
   (see #4) replaces only that one container. Reuses `ralph-sandbox`'s **image and security
   profile**, but **not** its config-dir mount: ralph-sandbox bind-mounts the operator's whole
   `~/.claude` / `~/.codex` (RW) into its single container, which doesn't survive going
   multi-agent — a reviewer is RO on the code yet must *write* its own transcript, same-tool
   agents and concurrent runs would race on shared mutable config (`.claude.json`), and the
   whole-dir mount needlessly exposes the operator's entire cross-project history + tokens.
   Instead each agent gets a **per-run, per-agent config dir** with only its **auth file**
   bind-mounted in (see [Run-state & session durability](#run-state--session-durability)).
   The multi-container orchestration on top is **net-new**. Model-provider network egress
   stays open (it must); the **push/PR capability is withheld from agents** and performed
   host-side instead. Full mount/credential/network posture:
   see [Security & threat model](#security--threat-model).

3. **Session mechanism = resumable per-turn exec into the live container.** A turn is
   `docker exec <agent-container> claude --resume <session-id> -p "<prompt>"` (Codex
   equivalent) — a fresh process inside the **living** container, context restored from the
   on-disk session transcript. NOT a live tmux REPL driven by `send-keys`. This keeps the
   container warm (per #2) **and** gives **clean per-turn detection** of completion /
   usage-limit / malformed-handoff from **process exit code + stderr** — no ANSI scraping.
   Rationale and trade-offs: [ADR 0002](../../adr/0002-story-develop-session-mechanism.md).
   Where the transcript physically lives, how it is namespaced per run, and how it survives
   teardown: see [Run-state & session durability](#run-state--session-durability) — this is
   the project's biggest technical risk and a feasibility-gate item.
   - **Consequence:** there is no filesystem watch-loop, no filename-based routing, and no
     **handoff-forgery surface** — authorship is by *which container was exec'd*, not by
     trusting a filename. A silent agent is a subprocess timeout; a malformed handoff means
     validate the one expected file, then re-prompt that agent.

4. **Usage-limit reaction = role-aware hybrid.** Classify `usage_limited` from exit code +
   stderr. **Coder** → pause-and-wait for the window reset (cap `max_pause_minutes`, then
   switch-with-reseed or checkpoint). **Reviewer** → switch to the next tool in a per-project
   `fallback_chain` immediately; pause only if no alternate exists. Switching the coder is
   the last resort because its in-session context is the thing we are protecting. A
   **replacement reviewer starts cold**, so it is reseeded with a full payload, not just
   handoff text — see [Reviewer replacement payload](#reviewer-replacement-payload).

5. **Pause = mode-dependent.** **Standalone:** block with a live countdown, keep the coder
   container warm, resume the same session on reset. **Daemon:** checkpoint-and-exit with
   `status:"interrupted"`, `error.category:"usage_limited"`, `resume_after:<ts>` + session
   ids; tear down (frees the slot). Today the runner treats `interrupted` as a plain release
   that a future run re-picks-up; the `resume_after`/re-dispatch behaviour is a Phase-3
   contract change (below). Resume state is ~free — session ids + handoffs are on disk.

6. **Reviewer selection = explicit list + default, config in metadata with per-task
   override.** Default: **one** `code-quality` reviewer. Specialised reviewers opt-in:
   - **Standalone:** a minimal local **`--develop-config <file>`** (TOML/YAML) holding the
     reviewer list, tools, images, prompts, and `fallback_chain`; plus `--reviewer <name>`
     (repeatable) for quick one-offs. v1 is *low*-config, not *zero*-config.
   - **Daemon:** the project's **available reviewer pool (`develop_reviewers`) and its default
     subset (`develop_default_reviewers`) live in project-context doc metadata** (consistent
     with [ADR 0001](../../adr/0001-github-watch-config-storage.md)). A **per-task override**
     (`metadata.reviewers` / tags) selects which run for that task; absent → the project
     default subset; absent *that* → the single built-in `code-quality` reviewer (never the
     whole pool). Because `--task-json` does not carry the resolved project config today, the
     daemon-mode plugin **loads this config itself** (Phase-3 contract note). Full rules:
     [Daemon config lookup contract](#daemon-config-lookup-contract).
   - A future router agent populates the same list.

7. **Approval = severity-thresholded, per reviewer.** A reviewer passes a round if it
   signals **LGTM** *or* its highest open finding is below its `block_threshold` (default
   `major`; security typically `minor`). Sub-threshold findings are **recorded** but
   non-blocking. The run is **approved** when *all* active reviewers pass in the **same**
   round. Reuses Ralph++ `parse_max_severity` / `severity_at_or_above`, applied over the
   structured finding records (see [Handoff schema](#handoff-schema--finding-identity)).

8. **Termination = guarded, keyed off finding identity.** `max_rounds` + a **`max_cost_usd`**
   ceiling + a **stall guard** + a **dispute** affordance. The guards key off the **canonical
   finding identity** (`finding_id`), not fuzzy text matching:
   - *Stall* = the coder's round commit is empty **or** the set of *blocking* `finding_id`s
     with unchanged `status` is identical across 2 consecutive rounds → stop.
   - *Dispute* = a finding the coder marks `disputed` (with rationale) that the reviewer
     keeps `open`/`blocking` for 2 rounds → emit a `[ReviewDispute]` human breadcrumb and
     stop, rather than grind to max.
   On any stop-without-approval: **standalone** offers the operator an attach/intervene
   escape hatch; **daemon** writes `failed` + the conversation log + the finding.

9. **Delivery = per-round commits; branch + log always; PR opt-in.** The coder commits **per
   round** (locally; **push is host-side only**, never from an agent). Per-round commits are
   intentional: Dave squash-merges branches into `main`, so the noisy round history never
   reaches `main`, and the per-round commits make in-flight reverts easy. Always produced:
   the committed branch + an ordered **conversation log** (all handoffs) + a summary.
   Standalone `--open-pr` pushes the branch and opens a PR via host `gh` (default **off**).
   Daemon: write `result.json` + post a findings summary back to Lithos; PR-opening and
   retag-for-human governed by config.

10. **Tests = coder runs them; objective gate runs in a fresh throwaway container.** The
    coder (RW) runs the project's auto-detected tests and **records results in its handoff**.
    Reviewers mount **read-only** and review the diff + recorded output. The plugin's
    objective gate re-runs tests on each round commit in a **fresh, throwaway container**
    checked out at that commit — *host-orchestrated, container-executed* (running untrusted
    repo tests on the bare host would defeat the sandbox). A specialised reviewer that must
    execute a tool declares a command run the same way.

11. **Lithos I/O = plugin owns it directly.** With `--task-id` (and not `--no-lithos`) the
    plugin fetches the task itself and posts findings + a conversation-log summary back
    (`lithos_finding_post` / `task_update`). This is *deliberately* plugin-side so it needs
    **no daemon change** — the runner does not apply `result.json` side effects today, so
    there is no double-application. `result.json` still carries `status` for the daemon.
    Standalone gets a full Lithos round-trip **today**. Honors `--no-lithos`.

12. **Acceptance criteria = shared, optional.** Optional `--acceptance-criteria <text|file>`;
    with `--task-id`, pull AC from the task body/metadata; else fall back to `--description`.
    Whatever "definition of done" exists is injected into the coder **and every reviewer**
    prompt as one source of truth (and into the reviewer-replacement payload, #4).

## Handoff schema & finding identity

The single-file-per-turn handoff must carry **structured, addressable findings** — a prose
blob would collapse the "conversation" back into a telephone game. Each handoff is markdown
with a machine-parseable findings block:

```markdown
## Status: FINDINGS | LGTM
## Summary
<one paragraph; coder turns also include test results>

## Findings
- finding_id: f-001               # PLUGIN-assigned; reviewers reference, never invent
  severity: critical | major | minor
  status: open | fixed | accepted | disputed | needs-clarification | superseded | merged
  files: ["path:line", ...]
  rationale: <reviewer's reason, or refinement of a prior vague finding>
  coder_response: <coder's reply: what changed, or why disputed/needs-clarification>
  supersedes: [f-007]             # only when splitting an existing finding
  merged_into: f-002              # only when merging into another
```

- The coder threads replies **per finding** (`status` + `coder_response`), not as one lump —
  this is the "dialogue" the design promises.
- `is_lgtm` / `parse_max_severity` operate over this block, not a free-text scan.
- Validation rejects a handoff missing the block, carrying unknown `status`/`severity`, or
  (see lifecycle) inventing/dropping ids; the plugin re-prompts that same agent (cheap, since
  detection is exit-code-based).

### Finding lifecycle (plugin-enforced)

Leaving id stability to prompt discipline would let a rephrase silently reset the
stall/dispute guards. Instead the **plugin owns id assignment and carries findings forward**:

- **Ids are plugin-assigned**, monotonic per run (`f-001`, `f-002`, …) — never a
  reviewer-computed hash, so rewording cannot change an id.
- Each round the plugin **injects the reviewer's prior open findings (id + text + status)**
  into its prompt. The reviewer must, for each, either **update the existing `finding_id`'s
  status** or leave it; it may not silently drop one (a dropped prior finding is reconciled
  as still-`open` and flagged).
- A **genuinely new** issue is returned without an id and the **plugin assigns** the next id.
- **Split:** reviewer marks the old id `status: superseded` and returns new findings each
  carrying `supersedes: [<old-id>]` (plugin assigns their ids). **Merge:** reviewer marks
  the absorbed ids `status: merged` with `merged_into: <kept-id>`.
- The stall/dispute guards (#8) operate over this **reconciled, id-stable** set — a
  `superseded`/`merged` chain counts as continuity, not as "resolved then reappeared".

## Run-state & session durability

The persistence claim in decision #3 only holds if the transcript has a concrete, isolated
home. Design (**validated by the [feasibility gate](story-develop-feasibility-gate.md) G3**):

- **Per-run, per-agent state dir** on the host under the work-dir:
  `<work_dir>/<run_id>/agents/<agent_name>/` holding that agent's session transcript(s),
  keyed by the tool's `session-id`. Mounted into the agent's container at the tool's config
  path via `CLAUDE_CONFIG_DIR` (claude) / **`CODEX_HOME`** (codex — *not* `CODEX_CONFIG_DIR`,
  which ralph-sandbox sets and codex ignores). Site it **under the work-dir, not `/tmp`**
  (codex degrades trying to create helper binaries under a `/tmp` home). G3 confirmed both
  tools redirect their transcripts to this dir.
- **Auth = bind-mount the single auth file, no copy.** Both tools keep auth in *one file*
  (`~/.claude/.credentials.json`, `~/.codex/auth.json`) inside an otherwise-combined config
  dir. So the per-run dir stays writable for transcripts while **only that one auth file is
  bind-mounted in from the operator's real config** — the credential is never copied into,
  and never persists in, retained run-state. Mount it **read-write to the real file** (not
  read-only): both tools periodically refresh their OAuth token by rewriting this file, and a
  RW bind-mount lets the refresh propagate back to the operator's real login without ever
  placing a credential in the run-state dir. (A RO mount would break long-run token refresh.)
- **Combined-config fallback (last resort, credential controls).** Only if some future tool
  *cannot* be pointed at a separate dir at all would we copy a minimal auth-only config per
  run; that copy would then be governed explicitly — dir `0700` / files `0600`, owned by the
  run user, **securely deleted on every teardown including failure and daemon
  checkpoint-and-exit**, never part of retained debug/resume state, with fresh auth
  re-injected on resume. **Not needed for claude or codex** (G3 passed); recorded for
  completeness.
- **Isolation:** `run_id` namespacing means two concurrent runs (or two tasks) never share a
  transcript; **GC** is "delete `<work_dir>/<run_id>/` after a retention window".
- **Survival across teardown:** because the dir is on the host work-dir, end-of-run teardown
  and daemon checkpoint-and-exit both preserve it; a later resume re-mounts it and
  `--resume <session-id>` reloads context.
- **Reviewer replacement** (tool switch) does **not** inherit the limited tool's transcript
  (different tool); it gets a fresh state dir + the reseed payload below.

## Reviewer replacement payload

When a reviewer is switched out (usage limit, #4), the replacement starts cold and is seeded
with, not just prior handoffs: the **current diff** (round commit range), the **acceptance
criteria**, the **full prior findings list with `status`** (so accepted/fixed items aren't
re-litigated), and the **outgoing reviewer's latest rationale**. This keeps judgments
consistent across the switch rather than restarting review from scratch each round.

## Security & threat model

This automation runs arbitrary coding agents against local repos, so the posture is explicit:

- **Mounted into agent containers:** the worktree (coder RW, reviewers RO); the per-run
  transcript dir (RW); and **only the single auth file** bind-mounted from the operator's
  real config (RW, so token refresh propagates — see run-state) — *not* the whole `~/.claude`
  / `~/.codex`. The provider API token is the primary exfil-sensitive item and is the main
  reason egress matters.
- **Deliberately *not* available to agents:** `gh`/push credentials and SSH keys (push/PR is
  host-side only); the operator's home beyond the auth config; any host path outside the
  worktree + per-run dirs. `cap_drop: ALL`, `no-new-privileges:true`.
- **Network:** agents are LLMs and *require* egress to their model provider — they cannot be
  fully network-isolated, reviewers included. v1 allows open egress, which is **strictly
  better than the status quo** (today these agents run on the bare host, unsandboxed).
  **Phase-2 hardening:** an egress allowlist restricted to provider domains.
- **Test execution** runs in a fresh throwaway container (#10), never on the bare host, so
  untrusted repo code is never executed in the host context.
- **Credential-at-rest:** none. Auth is a **bind-mounted file**, so the writable run-state dir
  never holds a credential — there is nothing to shred for claude/codex. The copy-then-shred
  controls in [Run-state & session durability](#run-state--session-durability) apply only to
  the hypothetical last-resort tool that can't be pointed at a separate dir.
- **RW auth-file tradeoff (explicit):** mounting the auth file **read-write** (for token
  refresh) gives the agent **write** access to the operator's real credential file — so the
  *integrity/availability* of that file is in the agent's blast radius (it could corrupt or
  delete it, forcing a re-login). We accept this: **secrecy is not reduced further** (the
  agent already legitimately reads and uses that token to make API calls), and an
  integrity/availability hit is recoverable by re-authenticating. If that tradeoff ever
  becomes unacceptable, the fallback is a RO mount + accepting periodic refresh failures, or a
  short-lived scoped token minted per run.
- Residual risk to accept explicitly: a malicious/compromised agent with a mounted provider
  token + open egress could exfiltrate that token *while running*. The allowlist (Phase 2) is
  the mitigation; v1 ships with eyes open because it does not widen the risk Dave already
  takes manually.

## Daemon config lookup contract

How a daemon-mode run resolves its reviewer config from project-context metadata (decision
#6). This reuses the convention Loom already relies on — a task carries
`task.metadata["project"]` (a slug; see `render.py`, `_task_archive.py`), and the canonical
doc is `projects/<slug>/<slug>-project-context.md` (mirroring
`_project_import_bulk._resolve_context_doc`):

1. **Slug:** read `task.metadata["project"]`. **Absent →** use built-in defaults (single
   `code-quality` reviewer), proceed, and post a `[Friction]` breadcrumb. A missing link
   must **not** block development.
2. **Doc:** `lithos_read("projects/<slug>/<slug>-project-context.md")`; on miss, fall back to
   the lexicographically-smallest `project-context`-tagged doc under `projects/<slug>/`
   (same fallback the importer uses). **No doc →** defaults + `[Friction]`, as above.
3. **Config keys** (typed JSON metadata, ADR-0001 style):

   | Key | Type | Meaning |
   |---|---|---|
   | `develop_reviewers` | `list[obj]` | Available pool: `{name, tool, image?, system_prompt?, block_threshold?}` |
   | `develop_default_reviewers` | `list[str]` | Names from the pool that run when a task doesn't override. **Absent → the single built-in `code-quality` reviewer** (never "all of the pool"). |
   | `develop_coder` | `obj` | `{tool, image?}` (default `claude`) |
   | `develop_fallback_chain` | `list[str]` | Tool names for usage-limit switching |
   | `develop_max_rounds` / `develop_max_cost_usd` | `int` / `number` | Ceilings (optional) |

4. **Per-task override:** `task.metadata["reviewers"]` (`list[str]`) selects which reviewers
   run for that task (from the pool); **absent → `develop_default_reviewers`, and if that too
   is absent → the single built-in `code-quality` reviewer.** An unknown name → `[Friction]` +
   skip that name (don't fail the run).
5. **Stale link:** if the doc exists but carries no `develop_*` keys, treat as "built-in
   defaults for this project" (single `code-quality` reviewer; not an error) — so enabling
   `story-develop` on a project is purely additive. Note that a populated `develop_reviewers`
   pool **without** `develop_default_reviewers` still defaults to the single built-in reviewer,
   never the whole pool — opting a reviewer *into the pool* does not auto-run it.

## Architecture

`__main__.py` (argparse) detects mode by presence of `--task-json`, builds a resolved
`DevelopConfig`, and calls the shared `develop()` core.

`develop()`:
1. Resolve config + acceptance criteria; create a per-task git worktree off `--branch`
   (default `main`); create the per-run state tree.
2. Seed `.handoff/` with `FORMAT.md`.
3. **Start long-lived containers:** one coder container (RW worktree) + one per reviewer
   (RO worktree), each launched idle with the tool available for `docker exec`, the single
   auth file bind-mounted RW (token-refresh; see run-state), per-run transcript dir mounted RW.
4. **Round loop** (explicit orchestration — no watch loop):
   - **Coder turn:** `docker exec` the coder (`--resume` after round 1); it implements/fixes,
     runs tests, commits, writes `round_NN_coder_done.md`. Validate the handoff; on malformed
     → re-prompt the same container.
   - **Reviewer turns:** `docker exec` each reviewer (RO) → `round_NN_review_<name>.md`.
   - Evaluate: severity-threshold approval, stall/dispute guards (by `finding_id`),
     round/cost/time ceilings, usage-limit reactions (pause coder / switch reviewer).
   - Objective test gate on the round commit (throwaway container).
5. **Terminate:** success (commit / log / PR-or-result / Lithos post) | failed | interrupted,
   per mode. Tear down all containers; preserve the conversation log + per-run state.

Per-agent container commands are built by an adapted Ralph++ `docker run` builder (per-agent,
**not** compose). Turns are sequential within a run; multiple concurrent runs each get their
own worktree, containers, `run_id`, and state tree.

## File layout

```
src/lithos_loom/plugins/story_develop/
    __main__.py     # argparse, dual-mode detect, build DevelopConfig -> develop()
    develop.py      # core: worktree, run-state, container lifecycle, round loop, termination
    turns.py        # docker exec a turn; resumable session ids; exit-code/limit parsing
    containers.py   # per-agent long-lived container launch/exec/teardown (adapt ralph_pp)
    handoff.py      # FORMAT.md; structured-finding parse/validate; reuse base.py severity/LGTM
    limits.py       # usage_limited classification + role-aware reaction (pause/switch)
    lithos_io.py    # fetch task / post findings+summary (gated by --no-lithos)
    config.py       # DevelopConfig, reviewer config, --develop-config + metadata loaders
    prompts/        # FORMAT.md, coder_init.md, reviewer_round.md, coder_fix.md
src/lithos_loom/runner/   # fill existing stubs: worktree.py, git.py
```

## Salvage map (from `~/agents/ralph-dev`)

- `ralph_pp/tools/base.py` → `ToolResult`, `is_lgtm`, `parse_max_severity`,
  `severity_at_or_above` (adapted to the structured finding block).
- `ralph_pp/tools/cli_tool.py` → subprocess invocation, ARG_MAX/stdin handling, exit-code
  capture → basis for `turns.py` (adapted to `docker exec`).
- `ralph_pp/sandbox.py` + `ralph_pp/steps/sandbox.py` → `docker run` command building.
- `ralph_pp/steps/worktree.py` → disposable worktree create/cleanup → `runner/worktree.py`.
- `ralph_pp/steps/_git.py` → base SHA / commits-since / dirty → `runner/git.py`.
- `ralph_pp/detection.py` → `detect_test_commands` for the test gate.
- `ralph-sandbox` `docker-compose.*.yml` → `cap_drop: ALL` / `no-new-privileges` security
  model and image only. **Not** its config mount: replace the whole-`~/.claude` bind with a
  per-run dir + single auth-file mount, and use `CODEX_HOME` (ralph-sandbox's `CODEX_CONFIG_DIR`
  is ignored by codex).

## Phased build

- **Phase 0 — feasibility gate** ([doc](story-develop-feasibility-gate.md)). **CONDITIONALLY
  PASSED 2026-06-11:** the blocking gates G1 (resume restores context), G2 (skills/agents
  headless) and G3 (transcript redirect + per-run isolation) all PASS for both claude and
  codex; G4 (usage-limit signal) is **DEFERRED** — structured exit-code/JSON detection channel
  confirmed, but exact limit strings are captured in Phase 1 under a safe-default fallback
  (unrecognised failure → generic `agent` error, not `usage_limited`). Operational findings
  (session-handle control, `CODEX_HOME`, stdin redirect, free `total_cost_usd`) feed
  `turns.py`/`containers.py`.
- **Phase 1 — core loop (standalone, single reviewer).** Standalone flags + `--develop-config`
  in `__main__`; run-state tree + container lifecycle + `develop()` round loop; `turns.py` +
  `containers.py`; `handoff.py` with the structured finding block + `FORMAT.md`; per-round
  commit + throwaway-container test gate; fill `runner/worktree.py` + `runner/git.py`.
  Deliverable: `python -m lithos_loom.plugins.story_develop --repo X --description Y` spins
  up coder + reviewer containers, iterates to approval, leaves a branch.
- **Phase 2 — multi-reviewer + resilience + polish.** N reviewers (consolidated default);
  per-reviewer severity thresholds; usage-limit role-aware reaction + countdown + reviewer
  container-replace + reseed payload; stall/dispute guards by `finding_id` + `max_cost_usd`;
  `--acceptance-criteria`; `--open-pr`; `lithos_io.py`; egress allowlist hardening; operator
  attach escape hatch.
- **Phase 3 — daemon integration (contract changes, not "trivial").** Required changes:
  - `--task-json/--work-dir/--result-file` entry; atomic `result.json`.
  - **`result-schema.json`:** add a `resume_after` (and session-id) surface for
    `usage_limited` interruptions.
  - **`route_runner._apply_result`:** teach `interrupted` + `resume_after` to schedule a
    re-dispatch instead of a plain release.
  - **Config loading:** daemon-mode plugin loads its reviewer config from project-context
    metadata itself (since `--task-json` doesn't carry resolved project config).
  - Route stanza in `examples/lithos-loom.toml`; plugin-runner integration test; update
    `docs/SPECIFICATION.md` + `docs/result-schema.json` + `tests/`.

## Open questions (deferred, non-blocking)

- Whether `story-develop` supersedes the `story-implement` / `story-review-human` stubs or
  coexists as the conversational path.
- Project test suites needing secrets/env inside the throwaway test container (passthrough
  policy).

## Verification

- **Gate (Phase 0):** the four pass/fail spikes above; the project does not proceed until
  they pass.
- **Unit:** structured-handoff parse/validate (incl. malformed + unknown status); severity
  -threshold approval; `usage_limited` classification from sample stderr; stall/dispute by
  `finding_id`; reviewer-selection resolution (metadata + per-task override). Port Ralph++
  `test_lgtm.py`, `test_severity.py`, `test_worktree.py`.
- **Integration (standalone):** real run against a throwaway repo + trivial task; assert a
  branch with per-round commits, a conversation log, approval, and a clean throwaway-container
  test gate; a forced-limit case (mock stderr) exercising coder-pause and reviewer-switch +
  reseed; a resume-after-teardown case asserting transcript survival.
- **Pre-merge:** `make check` (ruff + ruff format + pyright + pytest) green; update
  `docs/result-schema.json` + `tests/test_plugin_runner.py` on contract changes;
  `examples/lithos-loom.toml` + `tests/test_config.py` on config-schema changes; update
  `docs/SPECIFICATION.md` for the new operator surface; ship ADR 0002.
