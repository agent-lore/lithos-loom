# `lithos-loom develop converge` — reference

Converge an **existing PR** to review-green. converge runs the story-develop reviewer **panel + deterministic gate** against a PR, and if that blocks, runs a **coder fix loop on the PR branch** — implement→review→fix rounds until the panel LGTMs **and** the gate floor is clean — then **fast-forward-pushes** the fixed branch back to the PR head, ready for the human merge gate.

```
lithos-loom develop converge <pr> [flags]
```

This automates the operator's manual chore — take a review, hand it to the coder, tell the panel to look again, iterate until every reviewer is satisfied — as one command. It realises [ADR 0003](../adr/0003-code-quality-review-strength.md) §9 "Shape 1" (re-dispatch development on the PR branch without resolving the `pr` gate) as the on-demand / human-triggered variant. See [ADR 0009](../adr/0009-converge-pr-loop.md) and [`SPECIFICATION.md`](../SPECIFICATION.md) §4.15a.

converge does **not** re-implement the develop loop: it runs an intake review (the same primitive `develop review` runs), then calls `develop()` with a `LoopEntry` override so the coder/panel/gate/dispute/stall/termination sequencing is **single-sourced** with story-develop. Round 1 is a cold-start turn that reconstructs the PR author's intent before changing anything.

---

## TL;DR

```bash
# Converge a PR with the standard panel, then push the fixed branch
lithos-loom develop converge #142

# Thorough panel, codex as the coder, cap the loop at 4 rounds
lithos-loom develop converge #142 --profile thorough --coder codex --max-rounds 4

# Converge locally without pushing (inspect first)
lithos-loom develop converge #142 --no-push

# Supply the intent explicitly (overrides the PR body) and save the JSON summary
lithos-loom develop converge #142 --ac "the leak must close the handle on error" --json /tmp/c.json
```

---

## What it does

1. **Resolves the PR** to a `base..head` pair, the pushable head branch, a fork flag and a merged flag (via the typed GitHub client — the same seam `develop review` uses). Two PRs are refused here, *before* any container runs. A **merged PR** → `merged`: it has already landed, so there is nothing to converge and no fix commit pushed to its branch could reach the base. (Reviewing a merged PR stays legitimate — `develop review` does not refuse one; only converge, which pushes, does.) A **fork PR** → `fork_unsupported`: loom pushes under origin credentials and cannot push to a fork. The merged check runs first — "push it from your fork" is useless advice for a PR that already merged.
2. **Intake review.** Runs the resolved profile's deterministic gate + the reviewer panel once at the PR head, under a **distinct `run_id`** (`<run_id>-intake`) so its round-1 artifacts never collide with the fix loop's. If the panel is **incomplete** (interrupted / invalid) → `failed` (nothing trustworthy to seed the loop from). If the intake spend alone meets `--max-cost` → `failed` (checked before the clean/blocking split, so a clean intake can't bypass the budget). If it does **not** block → `already_clean`: no coder is built, nothing is pushed, exit 0 — this reports on the PR *snapshot resolved before intake*, not a live re-check, and is the cheapest path for the common re-check.
3. **Fix loop.** If the intake blocks, enters `develop()` on a committable worktree at the PR head (base = the PR merge-base), seeded from the intake findings + the PR's own commit log. Round 1's coder is a **cold-start** turn: *"you are picking up a PR you did not author — read the acceptance criteria, the commit history, and the code to reconstruct intent, then address the findings to satisfy that intent; dispute (don't silently comply with) a finding that undoes a deliberate decision."* Rounds ≥2 are the normal `coder_fix` path. Termination is `develop()`'s own — `approved` / `disputed` / `stalled` / `cost_exceeded` / `max_rounds`.
4. **Push epilogue.** On **approval** (and unless `--no-push`), pushes the fixed branch onto the PR head ref **only if the PR head is still exactly the resolved head** — an atomic lease (`git push --force-with-lease=<ref>:<expected>`) plus a local append-only ancestry check. A head **deleted**, **advanced**, or **force-rewound** mid-run is refused as `merge_race` (never silently recreated or overwritten), while a successful update stays a pure fast-forward — not a blind `--force`. A fork ref (absent on origin) is refused; auth / hook / branch-protection failures stay generic errors.

## Two intake modes

**Default (local panel).** converge converges against loom's **in-container codex/claude panel + check-floor** — fast, local, no GitHub round-trip. The intake reviewers are **cold** by design (no coder-summary to anchor on); only the fixer is given the PR's intent.

**`--from-github` (external findings — PRD S2 slice B).** Instead of the local-panel intake, converge ingests the PR's **external review material** — reviews and inline comments left by bots or humans:

- **Trust line (ADR 0011 d8):** allowlisted bot logins (`[github_watcher] trusted_bots`) and humans with **write/admin** on the repo may seed the coder. Everyone else's findings are printed (`untrusted (reported only)`) and never placed on a prompt path. Roots already proven handled by an authenticated `Fixed in <sha>` reply are skipped.
- **Triage (S5a):** one cheap read-only container turn checks each claim against the code before any fixing. It may **REJECT only with cited evidence** — a `file:line` citation (a bare filename or dotted version token is not one); anything short of that — including a failed triage turn — **proceeds** (actioning a false positive is recoverable; suppressing a true one is not). All findings rejected → status `triage_rejected`, exit 0, no coder built.
- **Injection:** surviving findings seed the coder directly via the same `LoopEntry` seam, **bypassing the local intake** — dispatching converge on an external finding without injection would return `already_clean` from the very panel that missed the defect (ADR 0011 d1/d7). The loop's own panel and check-set then judge the *result*: the external reviewer proposes, loom's gate disposes.
- **Thread replies:** after the run, each comment-backed finding gets a reply — `Fixed in <sha>` **only when the coder explicitly acknowledged that finding as `FIXED` in its handoff's mandated `## External findings` section, the loop approved, and the branch was actually pushed** (loop approval alone is tree-level evidence, so an id the coder omits stays `unaddressed` and its thread gets no reply — a silent partial fix can never earn a false claim), `Not changed — triage: <evidence>` for rejections, the coder's reasoning for disputes. Summary-only reviews (no inline comment) have no thread and appear in the rendered summary instead.
- A finding written against an older head sha is injected with a re-anchor note (verify before re-fixing); severities enter at `minor` (external reviewers state none).

## Acceptance criteria (the reviewer's + fixer's brief)

Precedence: `--ac-file` > `--ac` > the **PR body**. A PR with no body and no `--ac` / `--ac-file` is rejected — a reviewer with no criteria is near-useless. converge requires a PR, so a bare range / branch is rejected up front (use `develop review` for a read-only review of an arbitrary range).

## Flags

| Flag | Meaning |
|------|---------|
| `<pr>` | The PR to converge (positional): `#142` / `142` / a PR URL. A range / branch is rejected. |
| `-p`, `--profile` | Review profile — selects the persona panel + check-set (default `standard`). |
| `--reviewer NAME` | Override the panel personas (repeatable). |
| `--ac TEXT` | Acceptance criteria text. |
| `--ac-file PATH` | Read acceptance criteria from a file (wins over `--ac`). |
| `--base REF` | Override the diff base (default: the PR merge-base). |
| `--check-command NAME=CMD` | Override a gate check's command (repeatable), e.g. `--check-command typecheck='make typecheck'`. Runs the repo's own command **verbatim** — raw exit code, no `uv`-wrap, no tool-probe, and **no finding-adapter rewriting** even for `ruff`/`bandit`/`pip-audit` (so an overridden check opts out of structured findings) — instead of the catalog default that over-scopes vs the repo's real policy and forces extra fix rounds (bare `uv run pyright` scanning a test tree full of pre-existing type debt). Overridable: `lint` / `typecheck` / `sast` / `dep-audit` / `coverage` / `semgrep` (the `test` check uses `--test-command`; `format` is the autoformat pass). An unknown / non-overridable check fails closed. |
| `--check-state NAME=STATE` | Override a gate check's blocking state (repeatable): `required` / `informational` / `off`, e.g. `--check-state sast=off`. `off` **drops** the check cleanly (runs nothing, records nothing) — distinct from a required-but-tool-absent check, which still blocks. Generalizes `--no-test-gate` (`--check-state test=off`). Stateable: `lint` / `typecheck` / `test` / `sast` / `dep-audit` / `coverage` / `semgrep` (`format` is the autoformat pass). Wins over the legacy `test_gate` for the `test` check. |
| `--test-command CMD` | Command for the `test` gate check (overrides auto-detection). The `test` check has bespoke detection, so it takes this dedicated flag rather than `--check-command`. |
| `--parity-command CMD` | The repo's **aggregate** verification command (e.g. `make check`), run once as a required `repo-parity` gate check so the converged tree passes whatever CI enforces beyond the structured check-set (diagram/codegen drift, docs lint, …) — closing "gate-green ≠ CI-green". Runs verbatim (raw exit, no adapter) regardless of ecosystem — the **primary** gate for a repo the catalog can't model (C/C++). See [ADR 0010](../adr/0010-aggregate-repo-parity-check.md). |
| `--image REF` | Sandbox container image for the agents **and** the gate (default `ralph-sandbox:latest`). converge does not read the project-context doc, so a project pinning `develop_image` must pass it here — otherwise the gate runs in an image that may lack the tooling its checks need (a browser for an e2e parity command, say) and can never pass. |
| `--artifacts-path PATH` | Repo-relative dir a gate check writes rendered output to (the project's `develop_artifacts_path`). Enables the artifact review pass; without it converge reviews the diff only. |
| `--coder claude\|codex` | Coder engine for the fix turns (default: the config's coder). |
| `--max-rounds N` | Cap the implement→review→fix rounds (validated `≥ 1`). |
| `--max-cost USD` | **Soft** phase-boundary ceiling on whole-command spend (intake + loop): converge stops before the next phase once recorded spend reaches it (validated finite and `> 0`). Not a hard cap — an in-flight turn may overshoot and a same-round approval is delivered even if it crossed the ceiling. |
| `--test-timeout N` | Max seconds for one gate check run — the `test` check, other check-set checks, and autoformat (default 900, validated `≥ 1`). Raise it for a repo whose non-integration suite exceeds the default; otherwise the gate floor never clears and converge **stalls** with green reviewers. |
| `--no-push` | Converge locally but do not push to the PR branch. |
| `--from-github` | Ingest the PR's **external review findings** instead of running the local-panel intake (see "Two intake modes"): trusted findings are triaged (S5a) and, if they survive, seed the fix loop directly; untrusted authors are printed but never fed to an agent; thread replies are posted afterwards. |
| `--repo PATH` | Repository to converge in (default: current directory). |
| `--json PATH` | Write the structured JSON summary. |
| `-c`, `--config` | Host config path. |

## Output

- **Plain-text summary** to stdout: the status line, the message, the round + fixer-commit count, and (on a push) the pushed sha → PR branch.
- **JSON** (`--json`): a stable object — `status`, `head_ref`, `head_branch`, `base_sha`, `head_sha`, `rounds`, `develop_status`, `fixer_commits` (only the coder's commits, PR head → HEAD — **not** the PR's original commits), `pushed`, `pushed_sha`, `intake_cost_usd` (in external mode: the triage spend), `total_cost_usd`, `message`, `deferred_findings`, and `external_outcomes` (external mode: per injected finding — `finding_id`, `author`, `source`, `comment_id`, `thread_url`, `disposition` `rejected`/`disputed`/`fixed`/`unaddressed`, `detail`; `fixed` requires the coder's per-id `FIXED` acknowledgement **and** the loop's approval — anything less reports `unaddressed`).
- **Statuses / exit codes:** `already_clean` (intake didn't block; reports the *pre-intake snapshot*), `converged`, and `triage_rejected` (external mode: every injected finding rejected with cited evidence; no coder built, rejection replies posted) → **0**; `not_converged` (loop stopped unapproved), `merge_race` (PR head advanced remotely), and `failed` (incomplete intake panel, or pre-loop spend — intake review or triage — exhausted `--max-cost`) → **1**; `fork_unsupported` and `merged` (the PR already landed) → **2**.

> **Intake exceptions propagate.** An *unexpected* error while producing the intake review (e.g. a container crash, a bad config) is raised, not silently mapped to `failed` — a traceback is the honest signal for an internal fault. `failed` is reserved for the *expected* incomplete-review and budget-exhausted cases.

## v1 limit — dispute-all round 1

If round 1's coder disputes *every* finding and commits nothing, the deterministic gate still runs on the unchanged head. Such a round converges only if the head was already gate-green. This is rare (the coder is told to fix, not dispute-all) and acceptable for v1.

## Requirements

Host-only, like a develop run: `docker` + the agent CLIs (`claude` / `codex`) + `gh` (for PR resolution and the push). Not part of the hermetic `make check`.
