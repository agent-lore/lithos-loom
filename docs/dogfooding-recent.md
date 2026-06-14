# Dogfooding runbook — recent changes (2026-06)

A point-in-time runbook for validating the batch landed in 2026-06 against a
live host. Delete or archive once this batch has soaked.

**In scope**
- **#86** — route-runner dispatches on `task.updated` (tag an open task → it runs, no daemon restart).
- **#94 / #93** — codex coder + mixed claude/codex reviewer panel + per-agent model/effort.
- **#87** — github-watcher auto-closes non-issue tasks on PR merge.
- **#103 Part A** — codex failure events captured to the run's `failures/` dir.

**Approach.** One end-to-end **spine** chains the features (tag → codex develop run → PR → merge-closes-task), backed by **cheap isolated smoke tests** for the risky/hard-to-observe bits. Use a low-stakes throwaway project first; soak before relying on it unattended.

---

## Phase 0 — prerequisites & de-risking smoke tests (do first)

- [ ] **0.1 Environment.** Lithos + Influx up; `lithos-loom doctor` passes; `gh auth token` works; `~/.claude/.credentials.json` and `~/.codex/auth.json` present. Set `log_level = "debug"` in the loom config for the dogfood.
- [ ] **0.2 🚩 Codex-in-container smoke test (the gating unknown).** story-develop runs agents *inside* `ralph-sandbox`, not on the host — confirm the image has `codex` and can auth:
  ```bash
  docker run --rm -e CODEX_HOME=/codex_home \
    -v ~/.codex/auth.json:/codex_home/auth.json \
    ralph-sandbox:latest codex exec --json \
    --dangerously-bypass-approvals-and-sandbox "say hi"
  ```
  Expect a `{"type":"thread.started",...}` line then `turn.completed`. **If `codex` isn't in the image, all codex dogfooding is blocked** until it's rebuilt with codex installed. Find this out now.
- [ ] **0.3 Standalone codex develop (bypass the daemon).** Exercise the codex container plumbing / session-handle threading / resume in isolation on a throwaway repo:
  ```bash
  python -m lithos_loom.plugins.story_develop \
    --repo /path/to/throwaway --description "trivial: add a comment to README" \
    --coder codex   # claude reviewer by default
  ```
  Inspect `<work_dir>/<run_id>/`: coder transcript under `agents/coder/.../sessions/…rollout-*.jsonl` (codex layout), `result.json`, empty `failures/`. Proves #94's hardest part with no daemon/GitHub in the loop.

## Phase 1 — #86 dispatch on `task.updated` (cheap, daemon-level)

The no-restart property *is* the thing under test, so the **daemon must already be running** before you add the tag. Use the **`echo` route** (zero-cost; see `examples/lithos-loom.toml`) so dispatch is validated without an agent run:

- [ ] Enable the `echo` route (uncomment its stanza; `[routes.match] tags = ["trigger:echo"]`) and `lithos-loom run`.
- [ ] Create an **open task without** `trigger:echo`.
- [ ] Add the tag via `lithos_task_update(task_id, tags=[…,"trigger:echo"])` (or edit `_lithos/tasks.md`).
- [ ] **Observe:** within seconds the runner logs `RouteRunner echo: claimed <id>` then `completed <id>` — **no restart**. (`echo` just writes `status="succeeded"`.)
- [ ] **No self-trigger:** confirm a second, unrelated `task_update` on the now-completed task does not re-run it (the `_processed_tasks` guard).

## Phase 2 — the e2e spine: #86 → #94/#93 → #87

- [ ] **Config:** a `story-develop` route with `completes_task = false`; the project-context doc metadata sets `develop_coder.tool = "codex"` (or a mixed panel `code-quality = codex`, `security = claude` + `develop_fallback_chain = ["codex"]`); low `develop_max_rounds`; and **lower `[github_watcher] reconcile_interval_minutes` to ~3** so #87 doesn't make you wait up to 60 min.
- [ ] Start the daemon; create a task in the throwaway project **without** the trigger tag.
- [ ] **Add `trigger:story-develop`** → *(#86)* the codex develop run kicks off → *(#94/#93 — watch the codex reviewer container start, model/effort resolution in logs)*.
- [ ] Run delivers a PR; task stays **open** with `metadata.loom_delivered` + `metadata.develop_pr_url`.
- [ ] **#87 merged path:** merge that PR on GitHub → within the reconcile interval the watcher completes the task and sets `develop_pr_merge_state = merged`.
- [ ] **#87 closed path:** on a *second* delivered task, **close its PR unmerged** → expect a one-shot `[DeliveredPRClosed]` finding, task left open, marker set — and confirm it does **not** re-post on the next sweep.

## Phase 3 — codex resilience (opportunistic)

- [ ] **#103 Part A:** when a codex turn fails (or you hit a real usage limit) during Phase 2, confirm a fixture lands in `<run_dir>/failures/` containing the `turn.failed` / `error` event JSON. **Keep a copy** — it's the input to #103 Part B (classification).
- [ ] **Fallback switch:** if you can induce a claude usage limit with a codex fallback configured, confirm the claude→codex switch fires (the reviewer container is rebuilt with `CODEX_HOME`).

---

## Observability cheat-sheet

| Want to see… | Look at |
|---|---|
| dispatch / claim / complete | daemon log (`log_level=debug`): `RouteRunner … claimed/completed`, `develop-pr-merge: completed …`, tool-switch lines |
| run outcome / friction | findings, grepped by prefix: `[DevelopResult]`, `[DeliveredPRClosed]`, `[BlockerFailed]`, `[Friction]` |
| task lifecycle markers | task metadata: `develop_pr_url`, `loom_delivered`, `develop_pr_merge_state` / `develop_pr_merge_url` |
| per-run artifacts | `<work_dir>/<run_id>/`: `result.json`, `agents/*/`, `handoff/`, `failures/` |

## Caveats most likely to bite

- **The image must contain codex** — Phase 0.2 gates everything.
- **codex cost is unenforced.** `max_cost_usd` sees `0.0` for codex turns (cost-measure design is #102), so bound runs with `max_rounds` + `max_runtime_seconds`, not cost. `effort` is also ignored for codex (depth is model-driven).
- **#86:** the daemon must be up *before* you add the tag — that's the property under test.
- **#87:** the de-dup marker is scoped to the PR URL, so re-developing into a *replacement* PR correctly re-evaluates rather than stranding the task.
