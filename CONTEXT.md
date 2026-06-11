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
  pass a turn. The *only* thing that crosses between agents; working noise stays in the
  agent's tmux pane. Doubles as the durable conversation record.
- **LGTM** — a reviewer's approval signal in its handoff `Status:` field. The cycle
  **completes** (is **approved**) only when *all* active reviewers signal LGTM in the same
  round.
- **session persistence** — each agent keeps its full conversation context across rounds.
  Mechanism (ADR 0002): a long-lived per-agent **container**, with each turn run as
  `docker exec … --resume <session-id> -p …` — context restored from the on-disk transcript,
  not a live tmux REPL. The core thesis and core technical risk; net-new (Ralph++ did not
  have it). Open spike: confirm Codex headless `--resume` + skills.
