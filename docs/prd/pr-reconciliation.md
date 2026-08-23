---
title: Lithos Loom — Post-delivery PR reconciliation
milestone: M-PR
status: draft
target_version: 0.9.0
references:
  - docs/SPECIFICATION.md (implemented surface — pr gates, github-watcher, story-develop delivery)
  - docs/prd/orchestration.md (epic H — the `pr` gate this PRD extends)
  - docs/prd/archive/story-develop.md (T9 — the inline Copilot round this PRD retires)
  - docs/adr/0009-converge-pr-loop.md (`develop converge` — the paid loop this PRD feeds)
labels: [needs-triage, lithos-loom, orchestrator, github]
---

# Post-delivery PR reconciliation

> **Status (2026-08-23).** Written from a live failure: the lithos-lens T1
> rollout delivered four PRs in one day, and **every one of them needed manual
> intervention to land**. Four distinct mechanisms were involved, none of them
> the one that looked obvious — including one (S0) where the agents were never
> given the task description at all. This PRD covers the post-delivery sweep
> that closes three of them, plus the input fix that closes the fourth.

## Summary

A delivered PR enters a `pr` gate and loom stops looking at it, except to ask
"has it merged?". Everything else that happens to a PR between delivery and
merge — the base moving underneath it, conflicts appearing, an external
reviewer filing real defects — is invisible to loom and lands on the operator.

The proposal is to widen the existing `pr`-gate sweep from a merge poll into a
**reconciliation sweep**: same cadence, same PR fetch, three more questions.

1. **Is it still landable?** (behind / conflicted / would-break-on-merge)
2. **Did anyone review it?** (Copilot, code-quality bots, humans)
3. **Does it still pass its own gate against *current* main?**

Investigating the last of these turned up a separate, larger defect that this
PRD also carries: **in daemon mode the agents never receive the task
description** — the coder's brief and the reviewers' acceptance criteria are
both the one-line task title. That is why the generated PR bodies say nothing
about what the PR does, and it is fixed here (S0) because it is the same
question — *is delivery doing its job?* — from the input side.

And, per the operator's steer, **stop triggering Copilot inline**. Loom
currently requests Copilot during delivery and blocks on it under a shared
budget. Checking asynchronously is strictly better: no deadline to starve, and
it picks up reviews loom never asked for.

## What this eliminates, and what it only makes visible

The originating question was *"how do we land changes like this without manual
intervention?"* — so this section has to be blunt about which parts of this PRD
answer it and which do not.

**Eliminates work outright:**

- **S0** (shipped, #333). No reporting involved; the agents get a real brief.
- **S4 prevention.** A conflict that never happens needs no resolution. This is
  the *only* part of this document that would have removed the manual work in
  the batch below, and it was drafted last, as an afterthought. Corrected: it is
  the primary answer, and the cheapest.
- **S1's auto-update** for the `behind`-and-clean case (below) — genuinely
  zero-judgement.

**Only makes a problem visible earlier:**

- **S3.** `[MergeGateFailed]` turns a post-merge surprise into a pre-merge fact.
  Real value — the operator hit the module-budget breach twice *after* merging —
  but the fix still needs a human.
- **S1's `dirty` case.** Knowing a PR conflicts is not resolving it.
- **S2's detection half.** Reporting an external finding is strictly better than
  losing it, which is today's behaviour. (S2's *remediation* half is back in the
  eliminates-work column: converge is on by default per Decision 2, because a
  fast-forward push is additive.)

**Measured against the batch below, honestly:** neither #43 nor #46 would have
auto-landed under any automation in this PRD. Both conflict in real source
(`frontier.py`; `tasks.py` / `web.py` / `dashboard.html`) and one needed a
semantic decision no merge driver could make (`filters_narrow_the_board`).
Stripping `docs/generated/` from the diff removes 2 of 5 and 4 of 9 conflicting
paths respectively — and **still leaves both conflicted**.

What *would* have removed the work is that these four stories never being in
flight together. That is S4's `blocks` edges, it costs nothing, and it is the
honest headline: **the reconciliation sweep is damage control; the fix for this
batch was scheduling.**

## Evidence — the 2026-08-22 lithos-lens rollout

Four stories delivered on the day, four PRs, four manual interventions; a fifth
(#47, T1-S3) landed from the same queue while this PRD was being written and is
included because it shows the pattern continuing. All numbers below are from the
run metadata, the PRs, and a reproduced rebase.

| PR | story | rounds | cost | panel findings | gate | outcome |
|---|---|---|---|---|---|---|
| #44 | T1-S10 | — | — | — | — | merged after manual update |
| #45 | T1-S9 | — | — | — | — | manually re-based onto #44 |
| #43 | T1-S12 | 5 | $66.45 | **0 critical / 0 major / 0 minor** | GREEN | conflicted + 3 real defects fixed by hand |
| #46 | T1-S5 | 5 | $54.07 | **0 / 0 / 0** | GREEN | still conflicted |
| #47 | T1-S3 | 4 | $55.49 | **0 / 0 / 0** | GREEN | open |

### Failure 0 — in daemon mode the agents never see the task description

Found while investigating why the generated PR bodies say nothing about what
the PR does. The empty description is the *symptom*; the cause runs much deeper.

`route_runner.py:402` writes the plugin's brief as
`json.dumps({"task": dict(payload)})` — the **SSE event payload**. That payload
has no `description` field. A real `task.json` retained from one of these runs:

```
KEYS: ['claims', 'id', 'metadata', 'resolved_at', 'status', 'tags', 'task_type', 'title']
title      : 'T1-S7: Task detail rebase (text-first)'
description: None
```

All four retained runs are identical — `description=None`,
`metadata.acceptance_criteria=None`. So `TaskContext.task_text`
(`lithos_io.py:62`) takes its `else self.title` branch, and everything
downstream is fed a one-line title:

- the **coder's brief** (`__main__.py:505`, `description=ctx.task_text`);
- the **reviewers' acceptance criteria** —
  `effective_acceptance_criteria` (`config.py:637`) is
  `self.acceptance_criteria or self.description`, and both are the title;
- the PR body's `## What`, with no `## Acceptance criteria` section at all.

Meanwhile the description is sitting in Lithos, unread. T1-S12's is:

> Render all four states: no tasks at all; all-clear (open sections empty);
> Lithos unreachable (existing banner); frontier tools missing on an older
> Lithos → graceful 'graph features need Lithos ≥ 0.4' notice with a flat-list
> fallback. Acceptance: all four branches render. PRD slice 12.

**Standalone mode is unaffected.** `__main__.py:744` builds its context from
`fetch_task_context` — a real `lithos_task_get` — so the same story developed
by hand gets the full brief. Only the daemon path reads the slim payload. Every
dogfood run driven through the CLI has therefore been testing a different input
than production.

**What this plausibly explains — stated as a hypothesis, not a result.** The
three delivered lens PRs each ran 4–5 rounds for $54–$66 and produced **zero
findings at any severity**, and the two defects Copilot did find were both
*contract* violations — `docs/REQUIREMENTS.md:1267` §14, and a healthy-stripe
claim contradicted by the classifier. Contract violations are what an
AC-grounded review is built to catch and what a title-only review structurally
cannot. That is consistent with the review-strength thread already on record
(#173 — "a persona panel isn't an AC auditor"; #208 — the per-criterion
AC→evidence checklist; the US-18 dogfood conclusion that AC quality beats round
budget). It is **not** established here: no A/B has been run with the
description restored, and the review-hardening epic's own lesson is that a
single arm is not evidence. Restoring the brief is worth doing on its own
merits; measuring what it buys is separate work.

### Failure 1 — the inline Copilot round starves itself

T1-S12's `[DevelopResult]`:

```
copilot round: 0 comment(s); no code change; 0 repl(ies) posted — INCOMPLETE
note: Copilot review did not settle: expected 2 comment(s), 0 arrived within 45s
      — the rest may be unaddressed; review the PR or re-trigger Copilot
```

Loom polled, **received Copilot's review, parsed that it claimed 2 comments**,
then had 45 seconds to wait for them to materialise, and gave up. Both were
real:

- `frontier.py:575` — a frontier read failure leaves rows in "Not classified",
  whose own banner explains it as frontier-limit overflow. **An outage renders
  as truncation.** It violates `docs/REQUIREMENTS.md:1267` — the §14 resilience
  contract the slice was implementing.
- `tasks.py:336` — "All systems healthy" renders while open epics and gates are
  invisible, because `classify_open_tasks` skips non-`task` rows.

**Mechanism** (`pr_delivery.py:838-862`): `copilot_timeout` is one budget shared
by the review-summary wait and the comment-materialisation wait. Copilot posted
its summary 553s after the PR opened; 47s of the 600s budget remained; the
settle window got 45s. The comment is explicit that this is deliberate — but the
consequence is an **anti-correlation**: the slower Copilot is, the less time its
findings get to arrive. A late review is exactly the case where the round is
guaranteed to fail.

Cost of the miss: $66.45 and five review rounds produced **zero findings at any
severity**, and the two defects that did exist were found by an external
reviewer nine minutes later and fixed by a human nine hours after that.

### Failure 2 — the reviewed artifact is not the landed artifact

All four PRs share merge-base `61993963` — lens main as of 2026-08-14. Rebasing
them onto current main produces genuine content conflicts:

```
#43 → docs/generated/metrics.{json,md}, frontier.py,
      tests/test_frontier.py, tests/test_tasks_mvp.py
#46 → docs/generated/{metrics.json,metrics.md,domain_model.md,components/Tasks.md},
      tasks.py, web.py, templates/tasks/dashboard.html,
      tests/test_frontier.py, tests/test_tasks_mvp.py
```

19 of #43's 23 files are also touched by the merged pair.

Resolving them is not mechanical. From the operator's own resolution of #43:
`filters_narrow_the_board` had to start counting `projects` as narrowing,
because T1-S9 added a project filter *after* that helper was written — without
it, a `?project=` board feeds the healthy stripe and **turns one project's rows
into a system-wide claim**. Both PRs are individually correct; the composition
is not. No panel ever saw that code, because it did not exist until the conflict
was resolved.

**Note on `docs/generated/`.** Every story regenerates `metrics.json`,
`metrics.md`, `domain_model.md` and `components/*.md`, so **any two concurrent
stories conflict there even when their source changes are disjoint**. That is
the architecture-guardrail kit working against the orchestrator, and it is
independently fixable.

### Failure 3 — a green PR is a statement about an old main

lens `.github/workflows/ci.yml` triggers on `pull_request` with a default
`actions/checkout@v7`, so CI tests `refs/pull/N/merge` — the PR merged into the
base **as it was when the run was triggered**. GitHub re-runs on push, *not*
when the base moves. #43's last green before the operator touched it was
computed against main from eight days earlier.

The operator found this the expensive way, twice:

> a module-budget breach can be invisible to every PR's own CI and appear only
> on the merge. It happened on #45 (`tasks.py` 821) and again here.

The budget check is not special — it is simply the first thing that happened to
be **additive** across slices. Anything that composes rather than conflicts is
equally invisible: two PRs each adding a dependency, each adding a route, each
growing a file toward its ceiling.

### What was *not* the cause

**[#288](https://github.com/agent-lore/lithos-loom/issues/288) — "story-develop
bases worktrees on stale local main" — did not fire here.** It is a real latent
bug and should still be fixed, but at all three dispatch times (09:47, 11:56,
13:08) `61993963` *was* the tip of main; nothing merged until 19:02. A
`git fetch` at run start would have handed all three the identical base.

The actual precondition is [#285](https://github.com/agent-lore/lithos-loom/issues/285)'s
world: stories dispatched from the ready frontier before their siblings land.
That is the steady state, not an anomaly — so "PRs in flight against a moving
base" is a permanent condition to be managed, not a bug to be eliminated.

## Problem statement

The `pr` gate models **"awaiting human merge"**. It does not model
**"awaiting human merge, and by the way this no longer applies, its gate is
stale, and a reviewer left two criticals on it."**

Three consequences, all currently absorbed by the operator:

- A delivered PR rots silently. Discovery is by opening it.
- External review findings are lost when they arrive later than a fixed budget.
- A PR's own green tells you nothing about whether it will break main.

## Design

One new subscriber concern, hung off the sweep that already exists.
`_develop_pr_merge.py:153` fetches the PR object **every sweep** and classifies
`merged` / `closed_unmerged` / `gone` / `still_open`. The `still_open` branch is
currently empty. That is where this lives.

### S0 — give the daemon path the real task

Independent of the sweep, and the cheapest fix in this document.

**Implemented in PR #333 (open at the time of writing — this section moves to
"shipped" when it merges).** The design below was drafted before the cause was
located, and the answer turned out simpler than either option it weighed.

Neither a plugin-side re-fetch nor route-runner enrichment is needed, because
**nothing was missing upstream**. `lithos_task_list` returns `description`,
loom's `Task` carries it (`lithos_client.py:160`) and the parser reads it
(`:1801`) — `_enrich` had the body in hand the whole time. `_event_payload`
(`lithos_event_stream.py:588`) is a hand-written eight-key projection that
simply never published it. The fix is **one key**, no extra Lithos round-trip,
and both modes converge on one brief.

`_task_from_payload` reconstructs `description` too, so it stays the true
inverse of `_event_payload` rather than a lossy subset — a partial inverse is
how this class of bug hides.

The guard is a **pair test**: every existing daemon fixture hand-wrote a payload
containing a `description` the real projection did not emit, so both halves
passed while the relationship between them was broken. The new test builds the
payload with `_event_payload` and feeds it through `read_task_payload`.

Then **PR body**: with a real description the existing `build_pr_body` renders
`## What` and `## Acceptance criteria` correctly with no changes at all — which
is the whole fix for the reported symptom.

Two further body changes were drafted here and then **assessed and dropped**:

- *Take the title from `ctx.title` rather than
  `config.description.strip().splitlines()[0][:90]`* — with the brief restored
  that expression **is** the title in every task-driven run, and it is the only
  thing that works for standalone `--description` free text, which has no task.
  A new `DevelopConfig.title` would compute the identical string at both call
  sites.
- *Stop `## What` repeating the title* — once the body is real, `## What` reads
  title-then-body, which is a normal and useful PR body. The duplication was
  only conspicuous while the body was empty.

Recorded rather than silently dropped: both looked worth doing when the cause
was unknown, and neither survives contact with the actual fix.

**Also worth carrying into the body** — all of it already in hand at delivery
time, and none of it available to the sweep later: the acceptance criteria, the
per-round review verdicts, and the check-set result. The current body has the
process metadata (rounds, cost, task id) but not the substance. A reviewer
opening a loom PR should be able to see what it was asked to do and what the
panel checked, without opening Lithos.

### S1 — PR landability

`PullRequest` (`github_client.py:168`) gains **three** fields from the same
`GET /pulls/{n}` response already being fetched — `mergeable: bool | None`,
`mergeable_state: str`, and **`base_sha: str`**. The third is not decoration:
without it the sweep cannot tell "the base moved" from "nothing changed", and
today the dataclass carries `head_sha` / `base_ref` / `head_ref` but no base
sha at all. On `still_open`, classify:

- `clean` — nothing to do.
- `behind` — the base moved, no conflict. **Auto-update by default**: merge the
  base in, run S3's gate, push if green (Decision 2). Non-destructive, no force,
  zero tokens, no judgement. `PUT /pulls/{n}/update-branch` does exactly this
  server-side and `allow_update_branch` is already on for lens; doing it in the
  worktree instead is preferred only because the gate must run on the merge
  result before the push, not after.
- `dirty` — real conflict. Post `[PRConflicted]` on the story.

**`[PRConflicted]` cannot name the conflicting paths.** GitHub reports
`mergeable_state: dirty` and nothing more — the file list in this PRD's evidence
section came from a local rebase in a throwaway clone, not from the API. So S1
alone reports *that* a PR conflicts; the **paths come from S3's trial merge**,
which has to run one anyway. Two consequences: `[PRConflicted]` is a thin
finding when S3 is not enabled, and S1/S3 should be implemented in that order
but read as one report.

**Two caveats that must be in the implementation, not discovered later.**

1. GitHub computes `mergeable` lazily and returns `null` on a cold fetch —
   reproduced while researching this PRD. A `null` is "ask again", never
   "clean".
2. **The marker key is `(pr_url, base_sha, head_sha)`**, not `(pr_url,
   base_sha)`. Pushing a conflict resolution moves the **head** without moving
   the base, so a base-scoped marker would suppress the re-check that proves
   the fix landed. Same shape as `metadata.develop_pr_merge_state`, one field
   wider.

### S2 — external review ingestion, and no more triggering

**Stop requesting.** Delete the `request_copilot` call from the delivery path
and the blocking `wait_for_copilot` / `fetch_copilot_comments_settled` round
with it. Delivery ends when the PR is open.

**Start checking.** The reconciliation sweep reads the PR's reviews and
review-comments each pass and surfaces anything new as a finding on the story:

- Copilot (`copilot-pull-request-reviewer[bot]`) — the existing
  `fetch_copilot_comments` already filters replies correctly and can move
  wholesale.
- `github-code-quality` and any other reviewing bot — an allowlist in config,
  not a hardcoded login.
- Human reviewers — currently invisible to loom entirely.

Post as `[ExternalReview]` (a fresh prefix; do not overload `[DevelopResult]`,
which is a terminal run record).

**De-dup needs more than comment ids, and the model needs three new fields.**
`PullRequestReview` (`github_client.py:206`) is `{author, body}` today — no
review id, no state, no timestamp. Comment-id de-dup therefore cannot represent
a **summary-only** review: an `APPROVED` or `CHANGES_REQUESTED` with no inline
comments has nothing to key on and would be reported forever or never. So:

- `PullRequestReview` gains `review_id: int`, `state: str`
  (`APPROVED` / `CHANGES_REQUESTED` / `COMMENTED` / `DISMISSED`), and
  `submitted_at: datetime | None`.
- The marker stores the highest-seen review id **and** the set of seen comment
  ids — reviews and inline comments are separate streams and neither subsumes
  the other.
- **Policy per state, declared not implied:** `CHANGES_REQUESTED` posts a
  finding; `COMMENTED` posts one only if it carries inline comments or a
  non-empty body; `APPROVED` and `DISMISSED` are recorded in the marker and
  post nothing (an approval is not an operator action item).

**Cost — this is not free, and the earlier draft of this PRD said it was.**
Reviews and review-comments are **two additional paginated endpoints** beyond
the PR fetch (`github_client.py:582`). Both need bounded pagination with a
stored cursor, or a long-lived PR with a large review history re-walks its whole
history every sweep.

**The honest comparison — the sweep is better on reach and worse on remedy:**

| | inline round (today) | sweep alone |
|---|---|---|
| deadline | 600s shared budget, self-starving | none — polls until merge |
| late review | lost, flagged INCOMPLETE | picked up next sweep |
| reviews loom didn't request | never seen | seen |
| human reviews | never seen | seen |
| blocks delivery | yes | no |
| cost when nothing to do | a delivery-time stall | two extra paginated calls |
| **fixes what it finds** | yes, when the round does not starve | **yes — via converge, on by default** |

**The regression this PRD must not hide.** The inline round is not a wait; it is
a full remediation cycle (`pr_delivery.py:894` onward): Copilot's comments become
a synthetic review handoff, go through the `FindingLedger`, drive **one coder fix
turn on the resumed session**, `commit_round` the result, run
`run_delivery_test_gate` on that commit, **push only if the gate is green**, and
reply to each comment thread with the fixing sha. Deleting it without a
replacement means **every external finding becomes manual work** — the exact
cost this PRD exists to reduce.

**Decision: the sweep detects, `converge` remediates.** `develop converge`
(ADR 0009) already is this cycle — panel + gate + coder fixes on an existing PR
until green, then fast-forward push — but on demand and unbounded in time, which
is the right shape for an external reviewer. So S2 ships as:

1. **Always:** post `[ExternalReview]` with the findings. Detection is never
   optional and never blocks.
2. **Remediate by default** (`external_review_converge`, default **on**): on a
   new blocking external review, dispatch `develop converge` against the PR.
   Converge's push epilogue is a **fast-forward** onto the PR head ref and it
   refuses to force (`MergeRaceDetected`), so this is additive work on the
   branch — squarely inside Decision 2's automate-it half, and it *restores* the
   remediation the inline round used to do rather than trading it away.

   It does spend tokens in response to a third party's output, so it remains a
   config key an operator can turn off per project, and it inherits the run's
   existing cost ceiling. But default-off would have made this PRD a net
   regression on remediation, which is not the trade to make.

**Open question for the operator.** Copilot reviewed all four lens PRs, but loom
requested each one, so this data cannot tell us whether lens has GitHub's
automatic Copilot code review enabled at the repo level. If it does not, ceasing
to request means no Copilot review at all. **Before S2 ships, confirm the repo
setting and record it as an operator prerequisite** — with a config escape hatch
(`request_copilot = true`) for repos without the automatic rule.

### S3 — re-gate against current main

The piece that closes Failure 3. On a base move, in a throwaway worktree:
`git merge origin/<base>`, then run a **check-set** on the merge result.

- Clean + green → record it; the PR's own CI green is now meaningful.
- Clean + red → `[MergeGateFailed]` naming the failing check. This is the module
  budget case, caught before the operator merges instead of after.
- Conflict → hand the conflicting paths to S1's `[PRConflicted]`; no gate run.
  This trial merge is the **only** source of that path list — the API does not
  provide it.

Zero tokens — it is a deterministic check-set on a different tree.

**"The story's check-set" is not reconstructible, and the PRD must say which
check-set it means.** A `pr` gate stores only `{gate_type, repo, pr_number,
required_state, pr_url, project?}` (`gates.py:91`). Re-running the gate the
story actually passed would need the resolved repo path, image, profile,
commands, expected states, parity command, timeouts and per-task overrides —
none of which the gate carries. Two options with genuinely different semantics:

1. **Persist a resolved gate manifest at delivery** and replay it. Faithful to
   "the gate this PR passed", but it goes stale the moment the project's config
   moves, and it needs somewhere durable to live.
2. **Re-resolve the project's *current* config at sweep time.**

**Decision: (2), re-resolve current config.** The question S3 answers is *"will
main break if I merge this?"*, and main is defended by today's gate, not by the
one that ran a week ago. A story whose PR now fails a check the project has
since tightened **should** be reported. This also keeps the gate free of a
config snapshot that would silently rot.

Consequences to implement, not discover:

- **Gate creation must always record `project`**, not "if project" — the sweep
  resolves the repo path and check-set config through the project-context doc,
  and a gate without it is unresolvable. A gate that predates this, or whose
  project has since been removed, is skipped with a one-shot `[Friction]`,
  never silently.
- **Result key is `(head_sha, base_sha, config fingerprint)`.** Without the
  fingerprint a re-gate is not re-run when the project's check-set changes,
  which is precisely the case option (2) exists to catch.
- **Forks are out of scope for S3.** `PullRequest` already carries
  `head_repo` / `base_repo` and converge refuses to push to a fork branch; the
  sweep likewise skips a fork PR with a recorded reason rather than fetching a
  third-party head into the operator's checkout.
- **Concurrency.** The sweep runs inside the github-watcher child; a check-set
  run is minutes, not milliseconds, so it must not block the merge poll. Fire
  it on base-change detection only, bounded to one in-flight re-gate per
  project.

### S4 — prevention (the part that actually removes the work)

Neither of these is loom code. They are listed last only because they are not
loom changes — by leverage they are **first**: nothing else in this PRD would
have prevented a single manual intervention in the batch above, and `blocks`
edges would have prevented all of them.

- **`blocks` edges between stories on one surface.** T1-S5/S9/S10/S12 are four
  slices of one dashboard. They had no edges, so Lithos's ready queue offered
  them all. `project import`'s `[sequential]` marker already writes exactly the
  edges that would have serialised them. This is a planning fix, not a tooling
  one — and it is the cheapest intervention available.
- **Take `docs/generated/` out of the per-story diff** in repos using the
  guardrail kit. It would not have saved #43 (which conflicts in real source
  too), but it is the difference between "clean rebase" and "conflict" for
  every genuinely disjoint pair of stories.

  **Not a union merge driver.** These files are *generated*, and CI fails when
  the committed copy disagrees with what the generator produces — a union of two
  branches' `metrics.json` is a file no generator would ever emit, so it
  converts a merge conflict into a guaranteed drift-gate failure. Anything that
  merges generated output textually is wrong for the same reason. The workable
  shapes are **regenerate after the merge** (a merge driver that runs the
  generator rather than combining text, or a post-merge hook), or stop
  committing the outputs and have CI generate and compare. Both are repo-policy
  changes for the consuming project, which is why this sits in prevention and
  not in loom.

## Decisions

1. **The sweep, not the delivery path, owns everything post-PR.** Delivery ends
   at "PR is open". Anything with unbounded external latency belongs to a poll
   loop, not a budget.
2. **Additive is automatic; destructive needs the operator.** This is an
   operator policy (2026-08-23), and it is the line this PRD draws — not
   "report before acting", which the earlier draft used as a blanket rule and
   which was over-cautious.

   **Additive → automate, by default, no prompt.** Merge commits, fix commits,
   and fast-forward pushes onto a delivered branch. None of these destroy
   anything: the reviewer's line anchors survive, the per-round dialogue commits
   survive, and history is only ever appended to. Concretely that authorises
   S1's `behind` auto-update, S2's converge remediation, and any regenerate-
   and-push of derived files.

   **Destructive → never without explicit approval.** A force push — including
   `--force-with-lease` — rewrites what a human may be mid-review on, and no
   automated path in loom may take it. This is not a new constraint so much as
   a newly-written-down one: `src/` contains **zero** force pushes today,
   `push_branch` is a plain `git push -u origin <branch>`, and converge already
   models the correct behaviour in `MergeRaceDetected` — on a non-fast-forward
   it *stops and reports* rather than clobbering the concurrent commit. Any
   future need to rewrite a delivered branch is an operator action, surfaced as
   a finding, never taken by the sweep.

   **Implementation requirement:** add a guardrail test asserting no force-push
   invocation exists in `src/` (alongside the existing `tests/guardrail/`
   contracts), so the invariant is enforced rather than remembered. Test it
   negatively — a guard that cannot fail is not a guard.

   What still reports rather than acts, and why: **conflicts in real source, or
   a red gate.** Those need judgement, and the evidence is
   `filters_narrow_the_board` — two individually-correct PRs whose composition
   was wrong in a way no merge driver could see. Rebase is not needed anywhere
   in this design; a merge achieves the same landability additively, and it is
   what the operator chose unprompted on #43 (*"kept as a merge so the per-round
   story-develop commits survive"*).
3. **Conflict resolution stays human or `converge`.** The evidence says these
   conflicts need judgement (`filters_narrow_the_board`), not a merge driver.
   When automation is wanted it belongs to `develop converge`
   ([ADR 0009](../adr/0009-converge-pr-loop.md)) as a rebase mode, operator-
   triggered — not to this sweep.
4. **The gate is never auto-resolved by this work.** Only a merge completes a
   `pr` gate, exactly as epic H specifies.
5. **A fresh finding prefix per concern** — `[PRConflicted]`,
   `[ExternalReview]`, `[MergeGateFailed]` — per the house rule against
   overloading existing prefixes, since operators grep by prefix.
6. **Retiring the inline Copilot round is a real loss of automatic remediation,
   and it is accepted deliberately.** Detection always runs; fixing becomes
   `develop converge`, opt-in per project and off by default. Trading a fix
   cycle that reliably fires for one that reliably *detects* is the right way
   round — but it is a trade, not a free upgrade, and S2 says so in full.
7. **S3 re-resolves the project's current check-set rather than replaying the
   one the story passed.** The question is "will main break", and main is
   defended by today's config. The cost is that a gate must always carry its
   `project` to be resolvable at all.

## Non-goals

- Auto-merge, merge queues, or branch protection. Lens has no protection today;
  a merge queue serialises landing but does not resolve a conflict, and it is a
  repo-policy decision, not an orchestrator feature.
- Rewriting delivered branches without an operator asking.
- Reviewing the merge resolution with a panel. Worth doing, plausibly the next
  PRD, but it is paid work and needs its own scope.
- Fixing #288. Independent, still real, tracked separately.

## Testing

- S0 (as shipped — there is no fallback to test, the payload carries the body):
  a **pair** test builds the payload with `_event_payload` and feeds it through
  `read_task_payload`, asserting the brief does not collapse to the title. Every
  pre-existing daemon fixture hand-writes a `description` the real projection
  never emitted (`tests/test_story_develop_daemon.py:72`) — both halves passed
  while the relationship was broken, so the guard has to be built from the real
  projection, not a convenient dict.
- S0: the published payload's exact key set is pinned by an equality assertion,
  so a future field added to `Task` that agents need cannot be dropped silently
  the same way.
- Sweep classification is a pure function of a fetched `PullRequest` plus the
  stored marker — table-driven, no network, including the `mergeable == null`
  case (must re-ask, must never read as clean).
- Marker scoping: a finding fires once per `(pr_url, base_sha, head_sha)`, and
  **re-fires when either sha moves** — the negative test that matters is
  *pushing a conflict resolution* (head moves, base does not), which a
  base-only marker would wrongly suppress.
- External-review de-dup: the same comment id across two sweeps posts once; a
  new comment on an already-reported PR posts; and a **summary-only** review
  with zero inline comments (`CHANGES_REQUESTED`) posts exactly once — the case
  comment-id de-dup alone cannot represent.
- Review-state policy is table-driven: `APPROVED` / `DISMISSED` post nothing,
  `CHANGES_REQUESTED` always posts, `COMMENTED` posts only with content.
- S3 runs a real check-set against a real merge result in a temp repo — per the
  [ADR 0005](../adr/0005-review-correctness-eval-harness.md) posture, hermetic
  and no live Lithos. Also assert the **skip** paths are loud, not silent: a
  gate with no `project`, a project since removed, and a fork PR each record a
  reason.
- S3 result keying: changing the project's check-set config must invalidate a
  stored green for an unchanged `(head_sha, base_sha)` — otherwise option (2)'s
  whole point (today's gate, not last week's) is unobservable.
- Retiring the inline round deletes `pr_delivery`'s Copilot tests; the delivery
  budget (`delivery_budget_seconds`) loses its `copilot_timeout` term and its
  docstring contract test must be updated in the same diff.

## Slices

| # | slice | ships | cost |
|---|---|---|---|
| 0 | **S0 real task brief + PR body** — implemented, PR #333 **open** | the coder, the panel AC and the PR body all get the description | none |
| 1 | S1 landability + `[PRConflicted]` | two fields, one branch, one marker | none |
| 2 | S2 ingestion + retire the inline round | delivery gets faster and simpler | none |
| 3 | S3 re-gate on base move | the merge-blindness fix | none |
| 4 | S4 prevention | graph edges + generated-file policy | none |

**Order by leverage, not by number: S0, then S4, then the sweep.** S4 is last in
the table and first in value — it is the only entry that removes manual work
rather than reporting on it, and for the batch that motivated this PRD it is the
only one that would have changed the outcome.

**S0 first, and it should not wait for the rest of this PRD.** It is a live
correctness bug on every daemon run, it is a handful of lines, and every further
measurement of review strength is confounded until it lands. Slice 2 is the one
with an operator prerequisite (the repo-level Copilot setting); slices 1 and 3
are independent of it.

## Open questions

1. Does lithos-lens have automatic Copilot code review enabled at the repo
   level? Gates slice 2's default.
2. Should `[PRConflicted]` mark the story **needs-attention** in the Obsidian
   projection, or is a finding enough? The gate already blocks the story; the
   question is whether a stale PR should be visually distinct from a healthy one
   awaiting merge.
3. Sweep cadence for re-gating. S1 and S2 are one API call; S3 runs a check-set,
   so it should fire on base-change detection only, not every sweep.
