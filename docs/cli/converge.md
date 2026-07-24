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

1. **Resolves the PR** to a `base..head` pair, the pushable head branch, and a fork flag (via the typed GitHub client — the same seam `develop review` uses). A **fork PR** is refused here, *before* any container runs: loom pushes under origin credentials and cannot push to a fork.
2. **Intake review.** Runs the resolved profile's deterministic gate + the reviewer panel once at the PR head, under a **distinct `run_id`** (`<run_id>-intake`) so its round-1 artifacts never collide with the fix loop's. If the panel is **incomplete** (interrupted / invalid) → `failed` (nothing trustworthy to seed the loop from). If the intake spend alone meets `--max-cost` → `failed` (checked before the clean/blocking split, so a clean intake can't bypass the budget). If it does **not** block → `already_clean`: no coder is built, nothing is pushed, exit 0 — this reports on the PR *snapshot resolved before intake*, not a live re-check, and is the cheapest path for the common re-check.
3. **Fix loop.** If the intake blocks, enters `develop()` on a committable worktree at the PR head (base = the PR merge-base), seeded from the intake findings + the PR's own commit log. Round 1's coder is a **cold-start** turn: *"you are picking up a PR you did not author — read the acceptance criteria, the commit history, and the code to reconstruct intent, then address the findings to satisfy that intent; dispute (don't silently comply with) a finding that undoes a deliberate decision."* Rounds ≥2 are the normal `coder_fix` path. Termination is `develop()`'s own — `approved` / `disputed` / `stalled` / `cost_exceeded` / `max_rounds`.
4. **Push epilogue.** On **approval** (and unless `--no-push`), pushes the fixed branch onto the PR head ref **only if the PR head is still exactly the resolved head** — an atomic lease (`git push --force-with-lease=<ref>:<expected>`) plus a local append-only ancestry check. A head **deleted**, **advanced**, or **force-rewound** mid-run is refused as `merge_race` (never silently recreated or overwritten), while a successful update stays a pure fast-forward — not a blind `--force`. A fork ref (absent on origin) is refused; auth / hook / branch-protection failures stay generic errors.

## Local panel only (v1)

converge converges against loom's **in-container codex/claude panel + check-floor** — fast, local, no GitHub round-trip. It does **not** yet ingest the GitHub review bots' comments (github-code-quality / Copilot); that is a deferred slice. The intake reviewers are **cold** by design (no coder-summary to anchor on); only the fixer is given the PR's intent.

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
| `--coder claude\|codex` | Coder engine for the fix turns (default: the config's coder). |
| `--max-rounds N` | Cap the implement→review→fix rounds (validated `≥ 1`). |
| `--max-cost USD` | **Soft** phase-boundary ceiling on whole-command spend (intake + loop): converge stops before the next phase once recorded spend reaches it (validated finite and `> 0`). Not a hard cap — an in-flight turn may overshoot and a same-round approval is delivered even if it crossed the ceiling. |
| `--no-push` | Converge locally but do not push to the PR branch. |
| `--repo PATH` | Repository to converge in (default: current directory). |
| `--json PATH` | Write the structured JSON summary. |
| `-c`, `--config` | Host config path. |

## Output

- **Plain-text summary** to stdout: the status line, the message, the round + fixer-commit count, and (on a push) the pushed sha → PR branch.
- **JSON** (`--json`): a stable object — `status`, `head_ref`, `head_branch`, `base_sha`, `head_sha`, `rounds`, `develop_status`, `fixer_commits` (only the coder's commits, PR head → HEAD — **not** the PR's original commits), `pushed`, `pushed_sha`, `intake_cost_usd`, `total_cost_usd`, `message`.
- **Statuses / exit codes:** `already_clean` (intake didn't block; reports the *pre-intake snapshot*) and `converged` → **0**; `not_converged` (loop stopped unapproved), `merge_race` (PR head advanced remotely), and `failed` (incomplete intake panel, or intake spend exhausted `--max-cost`) → **1**; `fork_unsupported` → **2**.

> **Intake exceptions propagate.** An *unexpected* error while producing the intake review (e.g. a container crash, a bad config) is raised, not silently mapped to `failed` — a traceback is the honest signal for an internal fault. `failed` is reserved for the *expected* incomplete-review and budget-exhausted cases.

## v1 limit — dispute-all round 1

If round 1's coder disputes *every* finding and commits nothing, the deterministic gate still runs on the unchanged head. Such a round converges only if the head was already gate-green. This is rare (the coder is told to fix, not dispute-all) and acceptable for v1.

## Requirements

Host-only, like a develop run: `docker` + the agent CLIs (`claude` / `codex`) + `gh` (for PR resolution and the push). Not part of the hermetic `make check`.
