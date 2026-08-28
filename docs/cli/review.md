# `lithos-loom develop review` — reference

Run the story-develop reviewer **panel + deterministic gate** against a change that already exists — a PR, a ref range, or a local branch — with **no coder and no fix loop**. Emits a consolidated report (markdown to stdout, structured JSON via `--json`).

```
lithos-loom develop review <pr|range|branch> [flags]
```

This is **review-only mode** (#154). It composes the shipped panel (canonical personas / Review Profiles, #137/#139) and the deterministic gate (#140) behind an entrypoint that *consumes* a change instead of *producing* one. It drives the same `run_panel_round` primitive the develop loop uses, so the two review paths never diverge. See [ADR 0004](../adr/0004-review-only-mode.md) and [`SPECIFICATION.md`](../SPECIFICATION.md) §4.15. It is also the execution primitive the review-correctness eval harness (#183) drives.

---

## TL;DR

```bash
# Review an external contributor's PR with the standard panel
lithos-loom develop review #142

# Review a local branch with the thorough panel, base = merge-base with main
lithos-loom develop review my-feature --profile thorough

# Review an explicit commit range; supply the intent; save the JSON report
lithos-loom develop review abc123..def456 --ac "attach must wait for delivery" --json /tmp/r.json

# Just the correctness reviewer, keep the worktree to inspect
lithos-loom develop review #142 --reviewer correctness --keep-worktree
```

---

## What it does

1. **Resolves the change** to a `base..head` commit pair (and the head sha):
   - **PR** (`#142`, bare `142`, or a PR URL) via `gh pr view` — `base/headRefOid`, plus the PR title + body. The PR head (`pull/N/head`, so fork PRs work) and base ref are fetched so both commits are local.
   - **`base..head` range** — `base`/`head` resolved directly.
   - **local branch / ref** — `head` is the ref; `base` is its merge-base with `main` (override with `--base`).
2. **Materialises a detached worktree at the head** (`git worktree add --detach`) — no branch is created.
3. **Runs the deterministic gate once** on the head tree (the resolved profile's full check-set — fast + candidate, since review is a one-shot).
4. **Runs each reviewer once** (round 1, no coder turn).
5. **Reports** per-reviewer findings + the gate outcomes + an overall `blocking` verdict. The worktree + reviewer containers are torn down on exit (keep with `--keep-worktree`).

## Acceptance criteria (the reviewer's brief)

The panel needs the change's *intent*. Precedence: `--ac-file` > `--ac` > the **PR body** (PR input only). A bare range / branch with **no** acceptance criteria is rejected — pass `--ac` / `--ac-file`.

## Flags

| Flag | Meaning |
|------|---------|
| `<pr\|range\|branch>` | What to review (positional): `#142` / `142` / PR URL, `base..head`, or a branch / ref. |
| `-p`, `--profile` | Review profile — selects the persona panel + check-set (default `standard`). |
| `--reviewer NAME` | Override the panel personas (repeatable). |
| `--ac TEXT` | Acceptance criteria text. |
| `--ac-file PATH` | Read acceptance criteria from a file (wins over `--ac`). |
| `--base REF` | Override the base ref (default: merge-base with `main`). |
| `--check-command NAME=CMD` | Override a gate check's command (repeatable), e.g. `--check-command typecheck='make typecheck'`. Runs the repo's own command **verbatim** — raw exit code, no `uv`-wrap, no tool-probe, and **no finding-adapter rewriting** even for `ruff`/`bandit`/`pip-audit` (so an overridden check opts out of structured findings) — instead of the catalog default that over-scopes vs the repo's real policy (bare `uv run pyright` scanning a test tree full of pre-existing type debt). Overridable: `lint` / `typecheck` / `sast` / `dep-audit` / `coverage` / `semgrep` (the `test` check uses `--test-command`; `format` is the autoformat pass). An unknown / non-overridable check fails closed. |
| `--check-state NAME=STATE` | Override a gate check's blocking state (repeatable): `required` / `informational` / `off`, e.g. `--check-state sast=off`. `off` **drops** the check cleanly (runs nothing, records nothing) — distinct from a required-but-tool-absent check, which still blocks. Generalizes `--no-test-gate` (`--check-state test=off`). Stateable: `lint` / `typecheck` / `test` / `sast` / `dep-audit` / `coverage` / `semgrep` (`format` is the autoformat pass). Wins over the legacy `test_gate` for the `test` check. |
| `--test-command CMD` | Command for the `test` gate check (overrides auto-detection). The `test` check has bespoke detection, so it takes this dedicated flag rather than `--check-command`. |
| `--parity-command CMD` | The repo's **aggregate** verification command (e.g. `make check`), run once as a required `repo-parity` gate check so the reviewed tree passes whatever CI enforces beyond the structured check-set (diagram/codegen drift, docs lint, …). Runs verbatim (raw exit, no adapter) regardless of ecosystem — the **primary** gate for a repo the catalog can't model (C/C++). See [ADR 0010](../adr/0010-aggregate-repo-parity-check.md). |
| `--image REF` | Sandbox container image for the reviewers **and** the gate (default `ralph-sandbox:latest`). `review` does not read the project-context doc, so a project pinning `develop_image` must pass it here — otherwise the gate runs in an image that may lack the tooling its checks need (a browser for an e2e parity command, say) and can never pass. Mirrors the same flag on [`develop converge`](converge.md). |
| `--artifacts-path PATH` | Repo-relative dir a gate check writes rendered output to (the project's `develop_artifacts_path`). Enables the artifact review pass; without it the review sees the diff only. |
| `--test-timeout N` | Max seconds for one gate check run — the `test` check, other check-set checks, and autoformat (default 900, validated `≥ 1`). Raise it for a repo whose non-integration suite exceeds the default; otherwise the gate floor times out and the report blocks. |
| `--repo PATH` | Repository to review in (default: current directory). |
| `--json PATH` | Write the structured JSON report (the #183 harness contract). |
| `--keep-worktree` | Keep the review worktree for inspection. |
| `-c`, `--config` | Host config path. |

## Output

- **Markdown** to stdout: grouped by reviewer (status + findings with severity / files / rationale), plus a `## Gate` line per deterministic check.
- **JSON** (`--json`): a stable object — `head_ref`, `base_sha`, `head_sha`, `profile`, `blocking`, `reviewers[]` (each with `findings[]`), `gate[]`.
- **Exit code** is non-zero when the review is **blocking** (any reviewer finding at/above its threshold, an incomplete panel, or a required gate check blocking — the same floor `develop()` applies).

No GitHub / Lithos side effects in this slice — posting findings to a PR / Lithos task is a deferred follow-up (ADR 0004).

## Requirements

Host-only, like a develop run: `docker` + the agent CLIs (`claude` / `codex`) + `gh` (for PR input). Not part of the hermetic `make check`.
