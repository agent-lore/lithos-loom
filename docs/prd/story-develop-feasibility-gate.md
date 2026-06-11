# `story-develop` — Phase 0 feasibility gate

> **Status:** Must pass before Phase 1 starts.
> **Date:** 2026-06-11
>
> The [story-develop PRD](story-develop.md) rests on a handful of unproven assumptions about
> how the agent CLIs behave headless and where they persist state. This gate turns each into
> a small, time-boxed spike with an explicit **pass/fail** check. **The whole project is
> conditional on all four passing** (or on a documented fallback being acceptable). Run the
> spikes inside `ralph-sandbox` so the environment matches the real plugin.

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

**On fail:** copy a minimal auth-only config into the per-run dir each run (accept the
credential-on-disk-per-run tradeoff), or revisit the daemon-resume design.

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
