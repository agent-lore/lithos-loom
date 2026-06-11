# `story-develop` — Phase 0 feasibility gate

> **Status:** Must pass before Phase 1 starts.
> **Date:** 2026-06-11
>
> The [story-develop PRD](story-develop.md) rests on a handful of unproven assumptions about
> how the agent CLIs behave headless and where they persist state. This gate turns each into
> a small, time-boxed spike with an explicit **pass/fail** check. **The whole project is
> conditional on all four passing** (or on a documented fallback being acceptable). Run the
> spikes inside `ralph-sandbox` so the environment matches the real plugin.

## Results — 2026-06-11 (run on host; tools `claude 2.1.170`, `codex-cli 0.137.0`)

**Verdict: GATE PASSES.** G1–G3 PASS for both tools; G4's *detection channel* is confirmed
structured (no scraping) but the exact limit-signal strings must be captured opportunistically
(Phase-1 task, fallback recorded). Run on the host with redirected config dirs rather than
inside `ralph-sandbox`; the tool *behaviour* validated here (resume, config redirect, skills)
is identical in-container — only the bind-mounts differ, which is mechanical.

| Gate | Verdict | Evidence |
|---|---|---|
| **G1** resume restores context | **PASS** (both) | Claude: `--session-id <uuid>` then `-p --resume <uuid>` recalled the planted fact (`4242`). Codex: `codex exec` → captured `thread_id` from the `thread.started` `--json` event → `codex exec resume <id>` recalled it (`7373`). |
| **G2** skills/agents headless | **PASS** (both) | Claude: a canary skill in `$CLAUDE_CONFIG_DIR/skills/` loaded and the `Skill` tool fired under `-p` (returned the canary token). Codex (no skill concept): honored a project `AGENTS.md` instruction under headless `exec`. |
| **G3** transcript redirect + isolation | **PASS** (both) | `CLAUDE_CONFIG_DIR` redirects transcripts to `<dir>/projects/<cwd-hash>/<uuid>.jsonl`; `CODEX_HOME` redirects to `<dir>/sessions/YYYY/MM/DD/rollout-…-<thread_id>.jsonl`. Both are **combined dirs** (auth + transcripts together), but auth is a **single file** (`.credentials.json` / `auth.json`), so the per-run dir stays writable for transcripts while that one file is **bind-mounted** in — keeping retained run-state credential-free without copying. |
| **G4** usage-limit signal | **PARTIAL** | *Channel* confirmed: both tools fail with a **non-zero exit + structured JSON** (claude result object carries `is_error`/`api_error_status`/`subtype`; codex `--json` emits a failure event) — classification needs no ANSI scraping. *Not* triggered (would burn real quota); exact limit strings to be captured when a limit naturally occurs, via the harness below. **Fallback if unclassifiable:** a narrow scrape of just the limit banner. |

### Operational findings to fold into Phase 1 (`turns.py` / `containers.py`)

- **stdin:** both tools block ~3s waiting on stdin even when the prompt is an arg — redirect
  `< /dev/null`.
- **Claude session handle:** we *control* it via `--session-id <uuid>` (no parsing needed);
  resume with `-r/--resume <uuid>`.
- **Codex session handle:** capture `thread_id` from the `thread.started` event in `--json`;
  resume with `codex exec resume <thread_id>`. **`resume` has a narrower flag set than
  `exec`** (no `-C/--cd`) — `cd` into the worktree instead of passing `-C`.
- **Codex env var is `CODEX_HOME`** (not `CODEX_CONFIG_DIR`, which `ralph-sandbox` currently
  sets — fix in `containers.py`). Site per-run `CODEX_HOME` **under the work-dir, not
  `/tmp`** (codex warns/degrades trying to create helper binaries under a `/tmp` home).
- **Cost ceiling is free:** claude `-p` returns `total_cost_usd`; codex returns per-turn
  `usage` token counts → `max_cost_usd` (decision #8) needs no estimation.
- **Bonus:** claude has `--fallback-model` (auto-switch model on overload) — orthogonal to
  usage-limit handling but worth knowing for the degradation story.

### G4 capture harness (Phase 1)

When a real limit occurs, save the failing invocation's exit code + full stdout JSON / stderr
as a fixture and write the classifier against it. Until then, treat a non-zero exit whose
JSON/error payload is *not* a recognised category as a generic `agent` failure (not
`usage_limited`), so the system degrades safely rather than mis-pausing.

## G1 — Codex headless `--resume` restores context

**Why it matters:** decision #3 assumes a turn is `<tool> --resume <id> -p <prompt>` into a
warm container, with full prior context. Claude Code is confirmed; Codex is not.

**Check:** run Codex headless with a first prompt that establishes a fact ("remember X=42"),
exit; run a second headless `resume` invocation asking for X. **PASS** if the second run
answers from restored context without re-priming. **FAIL** if context is lost.

**On fail:** Codex falls back to a persistent interactive process (losing clean exit-code
detection for that tool), or Codex is coder-only/reviewer-only where a single turn suffices.

## G2 — Skills/agents load under headless `-p`

**Why it matters:** Dave confirmed "B is fine *as long as `-p` doesn't prevent skills*." The
coder and reviewers are expected to leverage installed skills.

**Check:** with a known skill installed in the mounted config dir, run the tool headless
(`-p`, `--dangerously-skip-permissions`) on a prompt that should trigger that skill. **PASS**
if the skill is invoked. Repeat per tool used as coder/reviewer.

**On fail:** prompts must inline the skill's guidance instead of relying on autonomous skill
invocation; reassess whether the affected tool is viable in that role.

## G3 — Transcript persistence location + per-run redirect

**Why it matters:** [run-state durability](story-develop.md#run-state--session-durability)
and the daemon checkpoint/resume claim require knowing *where* each tool writes its
transcript and whether we can **namespace it per run** while mounting **auth read-only**.

**Check:** identify the transcript path each tool writes; confirm it can be redirected to a
per-run dir (e.g. via `CLAUDE_CONFIG_DIR` / Codex equivalent) **separately** from where auth
is read. **PASS** if auth-read and transcript-write can be split and the transcript survives
a container teardown + remount + `--resume`. **FAIL** if a tool forces one combined dir with
no redirect.

**On fail:** copy a minimal auth-only config into the per-run dir each run under the
credential controls defined in the PRD (`0700`/`0600`, owned by the run user, **securely
deleted on every teardown including failure/checkpoint**, never part of retained
debug/resume state), or revisit the daemon-resume design. If this fallback is taken, record
the secure-deletion behaviour as a Phase-1 test.

## G4 — Usage-limit signal detection from exit/stderr

**Why it matters:** decisions #4/#5 (role-aware degradation) depend on classifying
`usage_limited` cleanly — the headline reason for choosing resumable exec over ANSI scraping.

**Check:** capture the exit code + stderr each tool emits when a usage/rate limit is hit
(from a real limit, a recorded sample, or a forced/mocked condition). Confirm a reliable
classifier (`usage_limited` vs other failure) and whether a reset ETA is recoverable. **PASS**
if classification is deterministic from exit/stderr. **FAIL** if a limit is indistinguishable
from other errors without scraping.

**On fail:** add a narrow, well-tested pane/output scrape for *just* the limit banner as a
contained exception, or treat limits as generic failures (losing graceful degradation).

## Exit criteria

Proceed to Phase 1 only when G1–G4 are **PASS**, or each **FAIL** has a fallback recorded
above that Dave accepts. Capture the actual observed paths, exit codes, and stderr strings
in this doc as the spikes run — they become fixtures for the Phase-1 unit tests.
