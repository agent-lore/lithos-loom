# ADR 0002 — `story-develop` session mechanism: live container + resumable exec, not a live REPL

- **Status:** Accepted
- **Date:** 2026-06-11
- **Deciders:** Dave Snowdon

## Context

`story-develop` runs a multi-round conversation between a persistent **coder** agent and one
or more **reviewer** agents (see [PRD](../prd/story-develop.md)). Its whole advantage over
Ralph++'s fire-and-forget loop is **session persistence**: each agent keeps its context
across rounds, so the coder remembers what it tried and the reviewer remembers what it
objected to.

The original design sketch achieved persistence by keeping each agent as a **live
interactive process** in a tmux pane and injecting each new prompt with `tmux send-keys`.
That choice is load-bearing — almost everything downstream (how turn completion is detected,
how usage limits are detected, how malformed handoffs are caught, whether tmux is needed at
all) hangs off it — and it is hard to reverse once the orchestration, prompt-injection
escaping, and watch-loop are built around it. A future reader will reasonably ask "why
*didn't* you keep the agents as live REPLs, given the goal is a live conversation?"

Two forces push against the live-REPL model:

- **Detection.** Agents emit enormous working output (exploration, thinking, tool calls,
  progress bars, ANSI). Detecting *anything* from a live pane — turn completion, a provider
  **usage-limit** banner, a malformed handoff — means scraping that output, which is fragile
  (partial lines, escape codes, false positives). A usage-limit graceful-degradation
  requirement (a hard requirement for this plugin) makes reliable detection non-negotiable.
- **Reuse.** Ralph++ already ships a CLI tool wrapper that captures **exit code + stderr**
  per invocation, plus severity/LGTM parsing. A live REPL reuses none of it.

A separate operator requirement is that the agent **container be kept alive between turns**
so in-container state (worktree, warm caches, the agent's own session store) persists rather
than paying cold-start each round.

## Decision

Keep one **long-lived container per agent**, started once at run begin and alive for the
whole run, and execute each turn as a **fresh process inside that living container**:

```
docker exec <agent-container> claude --resume <session-id> -p "<prompt>"
```

(and the Codex equivalent). Context is restored from the agent tool's **on-disk session
transcript** via `--resume`; the container staying alive preserves worktree state and warm
caches on top. This is **not** a tmux live REPL — there is no `send-keys`, and no
long-running interactive agent process.

Turn boundaries are driven by **handoff files** (each agent writes a structured-markdown
sign-off to `.handoff/`), and the plugin **orchestrates turns explicitly** (run coder → read
its handoff → run each reviewer → read theirs → decide). It does not watch the filesystem or
route by filename.

The validation spike that must pass before building on this: confirm that **Codex** headless
`--resume` (or its equivalent) actually restores context, and that skills/agents load under
headless `-p`. Claude Code is confirmed (the Skill tool is available in headless `--print`
mode and resolves from the mounted config dir). If `--resume` proves insufficient for an
agent tool, the fallback for that tool is a persistent interactive process — accepting the
loss of clean exit-code detection.

## Consequences

- **Clean detection, no scraping.** Turn completion is process exit; a usage limit is a
  non-zero exit + a recognisable stderr signal (classified `usage_limited`); a malformed
  handoff is "the one expected file failed validation" → re-prompt that same container. None
  of these require reading the agent's terminal output.
- **No watch-loop, no filename trust, no forgery surface.** Because the plugin runs exactly
  one known agent per turn and reads exactly the handoff it expects, authorship is by *which
  container was `exec`'d*, not by trusting a filename a different agent could have written.
  The `fswatch`/`inotify` machinery and filename-based routing are removed entirely.
- **Reuse.** `turns.py` adapts Ralph++'s `tools/cli_tool.py` (exit-code/stderr capture) to
  `docker exec`, and `handoff.py` reuses `tools/base.py` (`is_lgtm`, `parse_max_severity`,
  `severity_at_or_above`) ≈ verbatim.
- **Recoverable checkpoints.** Because context lives in the transcript (not only in a live
  process), daemon mode can checkpoint-and-exit on a usage-limit pause (`status:"interrupted"`,
  `resume_after`, session ids), free the slot, and later resume from the transcript after the
  container was torn down. The live container is a within-run fidelity/perf boost; the
  transcript is the durable context.
  - **This only holds if the transcript has a concrete, isolated home.** Decision (validated
    by the Phase-0 feasibility gate, G1/G3): a per-run, per-agent state dir on the host
    work-dir (`<work_dir>/<run_id>/agents/<agent>/`), mounted into the agent's container at
    the tool's config path (`CLAUDE_CONFIG_DIR` / `CODEX_HOME`). This **replaces**
    ralph-sandbox's whole-`~/.claude` mount, which is single-tenant: reviewers are RO on code
    yet must write a transcript, same-tool/concurrent agents race on shared mutable config,
    and the whole-dir mount over-exposes the operator's history + tokens. **Auth is the single
    auth file bind-mounted in (RW, so OAuth token refresh propagates back to the operator's
    real login), not copied** — so the writable run-state never holds a credential. `run_id`
    namespacing isolates concurrent runs; GC = delete `<work_dir>/<run_id>/`; teardown
    (end-of-run *or* daemon checkpoint) preserves it because it is on the host. A reviewer
    tool-switch gets a fresh state dir (different tool, no transcript inheritance) + an
    explicit reseed payload. The gate confirmed both claude and codex redirect transcripts and
    resume cleanly; the copy-then-shred path is a last resort only for a hypothetical tool
    that cannot be pointed at a separate dir. See the PRD's *Run-state & session durability*
    and *Reviewer replacement payload* sections.
- **Cost.** The operator loses "attach and watch the agent think in real time." A tee'd
  `stream-json` log per agent recovers most of it; on stop-without-approval, standalone mode
  still offers an attach/intervene escape hatch.
- **Risk.** The decision depends on `--resume` restoring context per tool. Mitigated by the
  Phase-1 spike and the per-tool persistent-process fallback above.

## Alternatives considered

- **Live tmux REPL + `send-keys`** (original sketch). Most faithful to a real-time
  conversation, and supports easy operator attach. Rejected as the default because it forces
  ANSI-scraping for the three things the plugin most needs to detect reliably (completion,
  usage limits, malformed handoffs), can leave an agent wedged mid-turn on a limit, and
  reuses none of Ralph++'s wrapper. Retained only as a per-tool fallback if `--resume` is
  insufficient.
- **Fresh container per turn (`docker run` each time), no live container.** Simplest
  lifecycle and still gets exit-code detection, but pays cold-start every round and discards
  warm in-container state — rejected per the keep-container-alive requirement. `--resume`
  still does the context restoration, so this remains the de-facto behaviour *across* a
  daemon checkpoint/resume boundary.
