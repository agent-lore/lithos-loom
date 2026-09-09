# `lithos-loom develop merge-gate` — reference

Trial-merge a delivered PR into its base's **current** tip and gate the result
with the project's **current** check-set (PRD
[`pr-reconciliation.md`](../prd/pr-reconciliation.md) S3). Zero agent tokens:
no coder, no panel — a deterministic check-set on a different tree. This is
the command the github-watcher sweep drives as a subprocess on every base
move (the watcher half of S3); on its own it answers the operator's question
*"will the PR's current base break if I merge this now?"* before they press the button.

## TL;DR

```bash
# Gate PR #352 against origin/main as it is right now; push the update if green
lithos-loom develop merge-gate 352 --repo ~/projects/lithos-loom

# Gate only — never touch the PR branch
lithos-loom develop merge-gate 352 --no-push

# Keep the merged tree to poke at, and save the record
lithos-loom develop merge-gate 352 --no-push --keep-worktree --json /tmp/mg.json
```

## What it does

1. **Resolve the PR** the way `review` / `converge` do: the head sha, the base
   branch, and a fetch of both so `origin/<base>` is local. A **fork** PR is
   refused before any git work (`fork_unsupported`, exit 2): its head would
   have to be fetched into the operator's checkout, and the update could not
   be pushed under origin credentials anyway.
2. **Throwaway worktree** on a fresh branch at the PR head. If the base tip is
   already an ancestor of the head the PR is **up to date** and the head itself
   is gated; otherwise the base tip is merged `--no-ff`, so a base move yields
   a real merge commit whose parents are the head and the base — never a
   rewrite of which commit the head *is*.
3. **Conflict** → the unmerged paths are reported (`conflict`, exit 3) and the
   merge is aborted. This is the **only** source of that path list — GitHub's
   API says `dirty` and nothing more — and it is the input S5's resolver needs.
   No check-set runs on a tree that does not exist.
4. **Check-set** on the merge result: the Review Profile's checks (the story's
   via `--story`, else `--profile`, else the host default) with the same
   overrides `converge` takes. The verdict is the same **ledger-aware floor** the
   develop loop uses: an adapter-backed required check (ruff / bandit, run with
   `--exit-zero`) blocks through its findings at the configured threshold, never
   through its exit code. A required check that could not *execute* is
   `errored`, not green — nobody reviews here, and an unverified merge is never
   pushed. Each check
   runs in its own throwaway container off a fresh export of the committed
   tree (#282). `green` / `red` (exit 0 / 1) by the blocking verdict;
   `errored` (exit 1) when the set could not run at all; `no_checks` (exit 0)
   when the profile resolves to nothing — a vacuous gate proves nothing and is
   said so.
5. **Push the update** when — and only when — the gate is `green` **and** the
   PR was behind: the merge commit goes onto the PR branch through the same
   leased, ancestry-proved push `converge` uses (append-only; a branch that
   moved since the resolve is a `push_error`, reported beside the green
   verdict, never a rewrite). `--no-push` disables it. Red, errored,
   no-checks and up-to-date results never push: an unverified or pointless
   update is not written to someone's PR.
6. **Clean up.** The worktree and its branch are removed (the merge commit
   object survives for the pushed ref). `--keep-worktree` keeps the merged
   tree for inspection and prints its path.

The record carries two fingerprints. The **config fingerprint** is a digest
of the resolved checks (name, command, state, stage), the image, the
per-check timeout and the blocking threshold — what actually gated. The
**settings fingerprint** is the resolved gate *settings* alone (image,
timeout, threshold, the profile by **definition** — its check declarations
and the catalog commands behind them, so a loom upgrade that tightens what
`standard` means moves it — plus a schema version for check semantics that
live in code, test command / gate, check tables, parity command): it needs
no worktree, so `--resolve-only` reports it without a
fetch, a merge or a check, and it is what the watcher sweep's re-run key
`(head_sha, base_sha, settings_fingerprint)` compares each pass — a tightened
check-set re-gates an already-observed PR, which is precisely why the
*current* config is re-resolved rather than a snapshot replayed (PRD S3
decision). A tree-dependent change (a lockfile appears) moves the head sha
anyway.

## Flags

| Flag | Meaning |
|------|---------|
| `CHANGE` | The PR: `#142`, `142`, or a GitHub PR URL. A bare range / branch is rejected — there is no PR to update. |
| `--profile`, `-p` | Review Profile whose check-set gates the result (default: the story's, else the host's `default_review_profile` resolved through its `unknown_profile` policy, else `standard`). |
| `--story TASK_ID` | Resolve the story's develop settings (project doc + task `develop_*`: profile, check-set, image, test command, parity) exactly as the daemon path does — the **current** config defending that project — as the base layer under any explicit flags. **Strict:** where the daemon would degrade to built-ins (no project slug / doc, a read failure, a malformed gate setting — `develop_image` / `develop_test_command` / `develop_test_gate` / `develop_check_commands` / `develop_check_states` / `develop_parity_command` / `develop_review_profile`, at either layer), merge-gate exits `config_unresolved` (4) and gates nothing. The watcher-dispatched run passes it. |
| `--check-command NAME=CMD` | Override a check's command (repeatable). Beside `--story`, merges **per key** over the story's `develop_check_commands` table — the resolver's own task-over-project shape — so overriding one check never drops the project's other overrides. |
| `--check-state NAME=STATE` | Override a check's blocking state (repeatable). Beside `--story`, merges **per key** over the story's `develop_check_states` table. |
| `--test-command` | Explicit `test` check command (beats detection). |
| `--parity-command` | Repo-parity command, run as a required raw-exit check. |
| `--image` | Sandbox image the checks run in. |
| `--test-timeout` | Per-check timeout in seconds. |
| `--no-push` | Gate only; never push the merge commit. |
| `--expect-repo OWNER/NAME` | Refuse to act unless the checkout's origin is this repository — compared **before** any fetch; a mismatch exits `repo_mismatch` (2) and, with `--json`, writes `{status: "repo_mismatch", expected_repo, actual_repo, message}`. A PR number resolves against the checkout, so the watcher pins every run to the repo its gate names (a stale `[projects.<slug>].repo` must never act on another repo's PR with the same number). |
| `--resolve-only` | Resolve the gate settings (flags over the story's current config) and print their fingerprint — no fetch, no merge, no checks, no GitHub call. Still strict under `--story` (exit 4). With `--json`, writes `{status: "resolved", settings_fingerprint, image, review_profile}`. The watcher sweep's per-pass probe. |
| `--keep-worktree` | Keep the throwaway worktree (the merged tree). |
| `--repo` | Repository to work in (default: current directory). |
| `--json PATH` | Write the structured record. |
| `--config` | Host config path (the watcher passes the one it booted with). |

## Output

- **Text**: `merge-gate <ref>: <status>`, the message, base/head shas and
  behind/up-to-date, one line per conflicting path, one per check, and the
  push outcome.
- **JSON** (`--json`): `status`, `head_ref`, `head_branch`, `base_ref`,
  `base_sha`, `head_sha`, `merge_sha` (the gated tree: the merge commit when
  behind, the head when up to date, empty on conflict), `behind`,
  `conflicting_paths[]`, `checks[]` (`name`, `command`, `state`, `stage`,
  `outcome`, `passed`, `exit_code`, `timed_out`, `output_tail`), `verdict`
  (the gate's own decision: `GREEN` / `RED`, null when no verdict was
  produced — never the process-exit aggregate), `config_fingerprint`,
  `settings_fingerprint`, `pushed`,
  `pushed_sha`, `push_error`, `message`.

## Exit codes

| Status | Exit | Meaning |
|--------|------|---------|
| `green` | 0 | The check-set passed on the merge result (update pushed when behind, unless `--no-push` or a push race — see `push_error`). |
| `no_checks` | 0 | The profile resolved to an empty check-set; nothing gated, nothing pushed. |
| `red` | 1 | A blocking check failed on the merge result. |
| `errored` | 1 | The check-set could not run (infrastructure); no verdict. |
| `fork_unsupported` | 2 | A fork PR; refused from GitHub's metadata before any fetch. |
| `pr_closed` | 2 | The PR is merged or closed; its branch is not a live target, nothing is trial-merged or pushed. |
| `repo_mismatch` | 2 | `--expect-repo` named a repository and the checkout's origin is a different one; nothing was fetched, gated or pushed. |
| `config_unresolved` | 4 | `--story` could not resolve the project's **current** config (no project slug, no context doc, a read failure, or a malformed gate-affecting setting); nothing was gated — S3 gates with the current config or not at all, never with built-in defaults. |
| `conflict` | 3 | The base no longer merges; `conflicting_paths` names why. |

## The watcher half

The github-watcher sweep (`[github_watcher] merge_gate_enabled`, default on)
runs this command autonomously on every still-open `pr` gate whose
`(head_sha, base_sha, settings_fingerprint)` changed — `merge-gate <pr>
--story <id> --repo <path> --json <path> --config <host>` — one in-flight run
per project, holding and held by external remediation per PR (either may
push to the branch). The record lives in `metadata.merge_gate` on the gate.
Green records (and a pushed merge commit is recorded as loom's own push on
the remediation budget); a green verdict whose push failed is `push_failed`
— `[Friction]` with the push error, one retry on the same key, then the
verdict stands until a settings change or a move; red / errored posts
`[MergeGateFailed]` on the story; a conflict widens `[PRConflicted]` with
the conflicting paths; an unresolvable config, an unmapped project, a fork,
a repo mismatch or a crash posts `[Friction]` on the story (a crash or
mismatch is retried once on the same key, then waits for a head or base
move). The settings probe runs as a background task, never inline in the
sweep, and every run is pinned with `--expect-repo` to the gate's repo. Per-project opt-out: context-doc
`develop_merge_gate = false`. See SPECIFICATION §2.2.

## Requirements

Same as `converge`: a checkout with `origin` pointing at the PR's repository,
`gh` authenticated (the PR resolve), Docker for the check containers. No
agent CLIs — nothing here spends tokens.
