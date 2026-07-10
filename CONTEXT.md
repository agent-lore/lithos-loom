# Context — lithos-loom

Ubiquitous language for lithos-loom. Terms here are domain-meaningful (operator /
integrator facing), not implementation detail.

## Obsidian bridge

- **task line** — the single Obsidian Tasks-plugin markdown line that represents one
  Lithos task in the vault: ``- [ ] <title> 🆔 lithos:<id> [#project/<slug>]
  [#lithos/<route>] [⛔ lithos:<dep>]… [<priority-emoji>] [📅 <date>]`` (and the
  terminal ``- [x]`` / ``- [-]`` … ``✅``/``❌ <date>`` forms). The ``🆔 lithos:<id>``
  marker is the stable anchor other lines reference and the projection re-identifies a
  task by across re-writes; the trailing Tasks-plugin emoji (priority, ⛔ deps, 📅 date)
  are the only positions the plugin parses for sort/filter. The line's **grammar** — the
  priority-enum ↔ emoji bijection and the render/parse inverse pairs for the 🆔, priority,
  and 📅 markers — is owned by the ``task_line`` module, the single home shared by the
  projection writer (``render``), the fs-watcher reader (``obsidian_fs_watcher``), and the
  import parser (``task_line_parser``). Writer and reader are inverses under valid inputs,
  pinned by a round-trip property test rather than a per-table drift test.

## story-develop plugin

The plugin that automates Dave's manual conversational code-review workflow. Replaces
Ralph++'s sequential implement→review→fix (fresh process per turn) with a **persistent,
dialogue-based** cycle.

- **develop cycle** — the full lifecycle the plugin runs: *implement → review → dialogue
  → complete*. v1 starts from nothing (a task description), so the same coder session both
  implements the task and fixes review findings. Decided in PRD grilling 2026-06-10: the
  implement step is in-scope precisely because keeping implement+fix in one session
  preserves context.
- **coder** — the agent that writes and fixes code. One persistent session for the whole
  develop cycle.
- **reviewer** — an agent that examines the coder's work and produces structured findings
  or approves. There may be N reviewers, each with a distinct persona/focus (e.g.
  code-quality, security, architecture).
- **round** — one coder turn followed by all active reviewers' turns. Rounds are numbered
  (`round_01`, …). The cycle iterates rounds until approval or max-rounds.
- **handoff** — the structured-markdown "sign-off" file an agent writes to `.handoff/` to
  pass a turn. The *only* thing that crosses between agents; working noise stays inside the
  agent's own container / tee'd log (not a tmux pane — see ADR 0002). Doubles as the durable
  conversation record. Carries a machine-parseable **findings block**, not just prose.
- **finding** — an addressable review item inside a handoff: a stable `finding_id` (carried
  forward across rounds), `severity` (critical/major/minor), `status`
  (open/fixed/accepted/disputed/needs-clarification, plus superseded/merged for the
  plugin-enforced split/merge lifecycle), target `files`, the reviewer's
  `rationale`, and the coder's per-finding `coder_response`. Finding identity (not text
  matching) is what makes the stall and dispute guards reliable.
- **LGTM** — a reviewer's approval signal in its handoff `Status:` field. The cycle
  **completes** (is **approved**) only when *all* active reviewers signal LGTM in the same
  round, OR their highest open finding is below that reviewer's `block_threshold`.
- **session persistence** — each agent keeps its full conversation context across rounds.
  Mechanism (ADR 0002): a long-lived per-agent **container**, with each turn run as
  `docker exec … --resume <session-id> -p …` — context restored from the on-disk transcript,
  not a live tmux REPL. The core thesis and core technical risk; net-new (Ralph++ did not
  have it). Open spike: confirm Codex headless `--resume` + skills.
- **engine** — the per-tool adapter concentrating everything story-develop must know to run
  a coder/reviewer with a specific CLI tool (`claude` or `codex`): its identity + capabilities
  (`meters_cost_usd` — codex reports tokens not USD, #102; `mints_session_handle` — codex mints
  a `thread_id` on turn 1 while claude echoes the caller's uuid; `supports_effort` — codex depth
  is model-driven), how to provision its **container** (config mount + env var, auth files,
  skills), how to build one turn's CLI argv (bare — reused host-side, session-less, by the
  review-correctness eval judge — or wrapped in `docker exec`), how to parse that turn's
  structured output into a turn result, and where it writes its **session** transcript.
  One adapter per tool behind a registry (`get_engine`); a new tool is added in one place instead
  of branching on a `tool` string across the container / turn / transcript / config code. The
  capabilities *express* decisions ADR 0002 + #94 already made — the adapter does not re-decide
  them. Owned by the `engines` module.
- **run outcome / develop-run contract** — the on-disk contract by which a develop run's
  *fate* is communicated between the three processes that touch it: the plugin subprocess
  that runs it, the daemon that delivers its PR, and the `develop attach` CLI that observes
  it. The run leaves markers on disk — `state.json` (the dialogue verdict) and `delivery.json`
  (the PR-delivery deadline / failure marker) in its **run dir**, plus `result.json` (the final
  delivered outcome) in the **shared per-task dir**, bound to its run by `run_id` (#198) —
  and the *rules* for reading them are the run-outcome invariants: an approved verdict is not
  "done" until the PR is delivered (#171); a reaped run's outcome is recovered from the
  completion store (#196); each marker is bound to *its* run, not a prior one (#198); a failed
  delivery is not a clean success (#194). Owned by the `run_outcome` module — the single home
  for these invariants, read by the CLI and written by the plugin.
