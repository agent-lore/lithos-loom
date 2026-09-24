# `lithos-loom develop converge-push`

Report an **exhausted** `develop converge` run's unpushed rounds — and, on the
operator's say-so, push them onto the PR that run was converging.

```
lithos-loom develop converge-push <run-id|pr-number>
    [--yes] [--complete-gate] [--story TASK_ID] [--json PATH] [--config config.toml]
```

## Why it exists

`develop converge` pushes **only when its loop approves**, and that is right: an
unapproved push would put unreviewed rounds on a delivered PR. But a run that
stops exhausted — `not_converged` on `max_rounds` / `disputed` / `stalled` /
`cost_exceeded` — leaves those rounds **committed on a local branch** in
`<work_dir>/converge/<run>/worktree`, and until this command nothing in the CLI
said what they produced or let the operator decide.

The concrete case (2026-09-24, converge run `26f8ecc5`, story `284f3a40`):
`max_rounds` 5 with the gate green and the operator's Medium fixed in round 1.
Recovering the five commits took three hand steps — find the run dir, prove the
PR's remote head is an ancestor of the worktree tip, `git push origin
HEAD:<pr-branch>` — none of which the CLI surfaced.

This is the third choice after an exhausted remediation, beside completing the
gate (re-dispatch, which spends a fresh budget on work already done) and
pushing by hand.

## Report first — `--yes` is the only thing that writes

Without `--yes` the command **writes nothing**: no push, no PR write, no Lithos
write. It prints, from the run dir and one read-only `ls-remote` / `fetch`:

- the PR (url, number, head branch), the head sha the run started from
  (`intake_head_sha`) and the PR's **current** remote head;
- the run's status and stop reason, its rounds and `total_cost_usd`;
- the last round's gate verdict (the test gate, plus any blocking check by
  name) and the findings the last review round left **open** (severity +
  title) — read from the run's recorded ledger data, never re-parsed prose;
- the fixer commits PR head → worktree tip (first-parent, the same set
  `converge --json` reports as `fixer_commits`) with a diffstat;
- the **push verdict**.

`--json PATH` writes the same facts as a stable object (`verdict`,
`fixer_commits`, `open_findings`, `gate`, `pushed`, `pushed_sha`, `notes`, …).

### The push verdict

| Verdict | Meaning | Exit |
|---|---|---|
| `fast-forward` | The PR's remote head is an ancestor of the worktree tip — `--yes` will push. | 0 |
| `already pushed` | The remote head **is** the tip. `--yes` writes nothing. | 0 |
| `refused` | The remote head is not an ancestor of the tip (someone else pushed), or the head branch is gone. `--yes` writes nothing. | 1 |

The remote ref is fetched before the ancestry question is asked, because
`--is-ancestor` can only answer about commits this clone *has*; a head that
still cannot be resolved is **refused**, never assumed safe.

## `--yes`

1. **Push, append-only**, through the same seam `converge` and `pr_delivery`
   use (`push_to_pr_ref`): exact-ref `ls-remote`, `--is-ancestor`, an atomic
   `--force-with-lease` against the head just read — never a force, never a
   non-descendant. The lease makes the push atomic: a head that moves between
   the report and the push is rejected, and nothing lands.
2. **Answer the reviewers.** The per-finding thread replies the run would have
   posted had it converged, under the existing acknowledgement rules (#387 /
   #399): `Fixed in <sha>` only for an id the coder acknowledged `FIXED` in its
   final handoff; rejections, disputes and `no change needed` as ever; every
   reply ends with the automated marker so the watcher's trust filter ignores
   it. The dispositions are replayed from what the run recorded at intake
   (`external.json`) plus the handoffs on disk — with `loop_approved=True`,
   because the operator's `--yes` **is** the approval the loop never gave.
3. **Record the decision.** `[ConvergePushed]` on the story, naming the pushed
   sha, the rounds, the gate verdict and **the findings it was pushed with**.
   A run with an unapproved last round is still pushed — the operator has read
   the open findings and decided — so the record says exactly that.
4. **Record the push in the run dir** (`state.json`'s `converge_push` block),
   so a second invocation is `already pushed` and `develop list` drops its
   `unpushed` marker.

Everything after the push is best-effort and degrades into a `note:` line (and
into `notes` in `--json`): the commits are on the PR, and no later failure may
be reported as a failure to push.

### Budget semantics

**The push is the OPERATOR's, not loom's.** It is deliberately *not* recorded
as loom's own push on the S5b external-remediation budget, so the github-watcher
reads the new head as a **human push** — the budget re-arms exactly as it does
after a hand `git push`, which is what this is.

`--complete-gate` additionally completes the run's **own**
`remediation_exhausted` loom `human` gate, matched on both keys: the gate's
`escalation_reason` *and* its `run_id`. Another run's gate, or one raised for
another reason, is a decision nobody made here and is left open. The flag is
opt-in because leaving the gate to close on merge is equally valid. **No gate
is ever cancelled** — a cancelled gate is terminal and would strand the story.

## Resolving the run

`<run>` is a converge run id, or a **PR number** (`425` / `#425`), which
resolves to that PR's newest converge run. Same operator-typed key shape as
`attach` / `dump` / `deliver`.

A key that names a **story-develop** run is refused (exit 2) naming
`develop deliver`: that run has no PR. The mirror image holds — `develop
deliver` refuses a converge run dir (exit 2) naming this command, since
delivering one would push the run's *local* branch as a new remote branch and
open a **second** PR.

## Refusals (exit 1, nothing written)

- **The run has no recorded outcome.** `state.json` lands only at run end, so
  the run may be mid-round right now; watch it with `develop attach <run>`.
- **The run recorded no PR** — a run from before `converge` wrote its intake
  record. There is no head branch to push onto; find the PR and push by hand.
- **The worktree is gone.** The commits are not on this host any more.
- **`refused: PR head moved`** — see the verdict table.

## What `converge` records, and when

`develop converge` writes a `converge` block into the run's `state.json` **at
intake, before the first paid turn**, so a run killed at any point after it
(SIGTERM, exit 143) is still resolvable:

| Field | |
|---|---|
| `pr_url` / `pr_number` | The PR being converged. |
| `pr_head_branch` | The PR's head branch — *not* the run's own local branch. |
| `intake_head_sha` | The PR head the run started from. |
| `base_sha` | The base the run diffed against. |
| `repo` | `owner/name` of the origin the PR lives on. |
| `story_id` | The `--story` the run was dispatched with, when any. |

The loop's own exit **merges** into the same file rather than overwriting it,
so the block survives a finished run. External mode additionally writes
`external.json` (the injected id→row map, triage's verdicts, the surviving ids)
— what the thread replies are replayed from.

`develop list` shows the PR number in the `title` column for a converge run
(blank before, since a converge run has no Lithos task), plus an `unpushed`
marker while the worktree tip is ahead of the recorded intake head.

## Out of scope

- Rebasing or merging the base into the pushed branch — the **merge-gate**
  (`develop merge-gate`, PRD S3) owns that.
- Re-running the panel on the pushed tip — a later `develop converge <pr>` does.
- A run still in flight — refused; `develop list` shows it.

## Exit codes

| Code | |
|---|---|
| 0 | Reported, pushed, or already pushed. |
| 1 | A refusal — nothing was written. |
| 2 | Bad input: no such run, or a run this command does not own. |

Host-only (`git` + `gh` credentials + Lithos); not part of the hermetic
`make check`.
