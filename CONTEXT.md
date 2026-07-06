# Context — lithos-loom

Ubiquitous language for lithos-loom. Terms here are domain-meaningful (operator /
integrator facing), not implementation detail.

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
- **run outcome / develop-run contract** — the on-disk contract by which a develop run's
  *fate* is communicated between the three processes that touch it: the plugin subprocess
  that runs it, the daemon that delivers its PR, and the `develop attach` CLI that observes
  it. The run leaves markers in its run dir — `state.json` (the dialogue verdict), `result.json`
  (the final delivered outcome), `delivery.json` (the PR-delivery deadline / failure marker) —
  and the *rules* for reading them are the run-outcome invariants: an approved verdict is not
  "done" until the PR is delivered (#171); a reaped run's outcome is recovered from the
  completion store (#196); each marker is bound to *its* run, not a prior one (#198); a failed
  delivery is not a clean success (#194). Owned by the `run_outcome` module — the single home
  for these invariants, read by the CLI and written by the plugin.
