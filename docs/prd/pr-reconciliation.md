---
title: Lithos Loom — PR maintenance (post-delivery reconciliation)
milestone: M-PR
status: draft
target_version: 0.9.0
references:
  - docs/SPECIFICATION.md (implemented surface — pr gates, github-watcher, story-develop delivery)
  - docs/prd/orchestration.md (epic H — the `pr` gate this PRD extends)
  - docs/prd/archive/story-develop.md (T9 — the inline Copilot round this PRD retires)
  - docs/adr/0009-converge-pr-loop.md (`develop converge` — the paid loop this PRD feeds)
  - docs/adr/0011-pr-maintenance-invariants.md (the four cross-cutting decisions, extracted)
labels: [needs-triage, lithos-loom, orchestrator, github]
---

# PR maintenance — post-delivery reconciliation

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

The proposal is to turn the `pr` gate from a merge poll into a **PR-maintenance
state machine**: loom keeps its own delivered PRs merge-ready, deals with
external review comments, resolves the conflicts it can, and escalates — as a
first-class state, not a log line — only what genuinely needs the operator.

The operating principle (2026-08-24): **mechanical cases are loom's job.** A
conflict is evidence that judgement is needed; it is not evidence that
*operator* judgement is needed. Loom should attempt agent judgement, review it
independently, and escalate disputes, ambiguity, exhausted budgets and
persistent red gates.

The gate answers three questions each sweep, and acts on all three:

1. **Is it still landable?** (behind / conflicted / would-break-on-merge)
2. **Did anyone review it?** (Copilot, code-quality bots, humans)
3. **Does it still pass a gate against its *current base*?**

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

- **S2 remediation** — external findings are injected into the fix loop, fixed,
  verified by loom's own panel and gate, and pushed. On by default.
- **S5 conflict convergence** — an agent attempts the merge resolution, loom's
  panel reviews the *composed tree*, the project's current check-set runs, and
  the result is pushed as an append-only merge commit.
- **S6 serial admission** — the conflicts that never happen.

**Only makes a problem visible earlier:**

- **S3's red-gate case.** `[MergeGateFailed]` turns a post-merge surprise into a
  pre-merge fact. A broken merge still needs fixing, but S5's coder gets first
  attempt at it.
- **S7 escalation.** By construction: it is what happens when automation stops.

**Measured against the batch below, honestly.** Neither #43 nor #46 would have
merged *untouched* — both conflict in real source (`frontier.py`; `tasks.py` /
`web.py` / `dashboard.html`), and one needed a semantic decision
(`filters_narrow_the_board`) that no merge driver could make. But "no merge
driver" is not "no agent": that resolution is exactly the kind of reasoning
S5's coder-plus-panel loop exists to attempt, and its correctness is
independently checkable — the operator's own fix was verified by the slice's
regression tests plus `check`, `diagrams` and `e2e`. So the honest claim is
**not** "S5 would have landed them", but "S5 would have attempted them, proven
its attempt against the same gate the operator used, and escalated only if that
failed".

Two things remain true regardless: stripping `docs/generated/` removes 2 of 5
and 4 of 9 conflicting paths and leaves both conflicted, so **S4 is necessary
and not sufficient**; and these four stories never being in flight together
(S6) is the cheapest fix of all. Serial-by-default is the plan — but it is not
the whole plan, because imported tasks, separate epics and imperfect
decompositions will keep producing concurrent PRs, and the machinery has to
handle them when they arrive.

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
  the other. (Post-hoc, #353: Conversation-tab comments are a **third** stream
  with its own mark — the only channel open to the PR's own author, i.e. the
  operator on every loom-delivered PR, which the first two streams missed.)
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

**Decision: the sweep detects, and converge remediates — but the findings must
be INJECTED, not merely used as a trigger.**

Dispatching `develop converge` on an external review does not work as the
earlier draft assumed. Converge runs its **own local-panel intake** first
(`converge.py:1`) and returns **`already_clean` without ever starting a coder**
when that panel finds nothing (`converge.py:230`). Loom's panel is precisely the
panel that missed the defect — it approved T1-S12 across five rounds. So
"Copilot finds a real bug → loom notices → converge does nothing" is the
default outcome, not an edge case.

S2 therefore adds an **external-findings input** to the convergence entry point,
which seeds the coder directly and bypasses the intake gate:

```
ExternalFinding:
  source          copilot | github-code-quality | human | <configured bot>
  author          login
  trust           trusted | untrusted        (see below)
  review_id       int | None                 (summary-only reviews have no comment)
  comment_id      int | None
  thread_url      str                        (for the reply, and for the operator)
  head_sha        str                        the EXACT sha the reviewer read
  path / line     str | None
  body            str
  severity        mapped, default minor      (external reviewers state none)
```

`head_sha` is load-bearing: a finding written against a sha the branch has since
moved past may already be fixed, and must be re-anchored or dropped rather than
re-fixed blindly. The existing `comments_to_handoff_text` +`FindingLedger` path
(`pr_delivery.py:157`) already renders findings into a synthetic review handoff
and is the natural implementation seam — it is what the inline round used.

**Then loom verifies.** The injected findings drive the coder; loom's own panel
and the project's check-set review the *result*. The external reviewer proposes;
loom's gate disposes. Thread replies ("Fixed in `<sha>` — …") are restored, since
the thread url and comment id are carried through.

**Trust policy (operator decision, 2026-08-24): allowlisted bots + repo write
access.** External comment text reaching a coder that then pushes is a
prompt-injection surface — anyone who can comment can attempt to inject
instructions. So:

- Configured bot logins (`copilot-pull-request-reviewer[bot]`,
  `github-code-quality[bot]`, …) → **trusted**, seed the coder.
- Human commenters with **write or admin** permission on the repo → trusted
  (one permissions API call per unseen author, cached per sweep).
- Everyone else → **reported to the operator, never fed to an agent.** The
  finding still appears; only the automatic remediation is withheld.

`external_review_converge` defaults **on** (Decision 6). It spends tokens in
response to a third party's output, so it stays a per-project key and inherits
converge's existing `--max-cost` ceiling; but default-off would make this PRD a
net regression against the round it retires.

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

### S5 — automatic conflict convergence

The reviewer's point, and the operator's: *routine conflict resolution should
not require the operator.* A conflict means judgement is required; it does not
mean **operator** judgement is required. Loom attempts agent judgement, reviews
it independently, and escalates only what survives.

```
pr gate, base moved, merge is dirty
  → fetch the EXACT current base sha and head sha
  → attempt an additive merge in a throwaway worktree
  → conflicted: coder resolves, given
        - the story's brief and acceptance criteria (S0 made these real)
        - the PR's own intent (title, body, its round history)
        - what changed on the base since the merge-base, and why
          (the landed PRs' titles/bodies — this is how a resolver learns that
           `matches_filters` moved and that a project filter now exists)
        - the conflicted hunks
  → loom's PANEL reviews the COMPOSED TREE, not the resolution diff alone
  → the project's CURRENT check-set runs on the merge result (S3)
  → green + panel-approved → push the merge commit (append-only, ancestry-proved)
  → otherwise → S7 human gate with a decision brief
```

Three things make this defensible rather than reckless:

1. **The output is checkable.** The operator's own #43 resolution was validated
   by the slice's regression tests plus `check`, `diagrams` and `e2e` — the same
   gate loom would run. A resolution that passes the project's full check-set
   and an independent panel is not a guess.
2. **It is append-only.** A merge commit, ancestry-proved, pushed per
   Decision 2. Nothing is rewritten; a bad attempt is visible in history and
   revertible.
3. **It escalates rather than persisting.** **One** convergence attempt per
   `(base_sha, head_sha)`. A failure escalates to S7; it does not retry the same
   inputs, and a later base move is a genuinely new attempt.

**Deliberately harder than S2's case**, and the PRD says so: reviewing a
composed tree is a different task from reviewing a diff, and loom's panel is the
one that missed the original defects. Two mitigations: the check-set is
deterministic and does not depend on panel quality, and semantic-conflict cases
(`filters_narrow_the_board`) are exactly the AC-grounded reasoning that S0 just
restored the inputs for. If measurement shows the panel cannot judge composed
trees, the fallback is check-set-only auto-push for **non-semantic** conflicts
and escalation for the rest — a narrowing, not a redesign.

### S5a — triage external claims before acting on them

A trusted bot can be confidently wrong, and S2 hands its findings straight to a
coder that then pushes. The reviewer least likely to catch a bogus fix is loom's
own panel, which already approved this code. So **verify the claim before
actioning it** (operator decision, 2026-08-24).

**A separate triage step, not a "be sceptical" clause in the fixer prompt.**
The fixer has been handed a job and is pulled toward doing it; asking one agent
to both decide-if-real and fix biases toward acting. Triage is also cheap — one
call against a named file, line and mechanism, no container fix loop — and it
**produces an artifact**: a rejection is posted as a reply on the reviewer's own
thread ("not reproducible because …"), which closes the loop honestly instead of
ignoring it silently.

**Default to acting when uncertain.** The costs are asymmetric: actioning a
false positive is recoverable (the check-set and panel still run, and the result
is an append-only commit that can be reverted), while ignoring a true positive
is the failure that produced this whole PRD. So triage rejects only with cited
evidence — the code that refutes the claim — and anything short of that proceeds
to the fixer.

**Why the lens34 negative result does not apply.** RH-1 measured a
"discriminator" clause suppressing *true* findings (arm C: 2/5 vs a 3/5
control), and the rule it produced was that enumerate-and-discriminate only
works on a **closed** set. That result is about open-ended *detection*. Triage
is the opposite shape: the claim is already made and localised to a file, a
line and a mechanism, so checking it is a closed question. The risk of
over-suppression is nonetheless the one to watch, which is what the
default-to-act rule and the evidence requirement are for — and it is
**measurable**: seed a known-false external finding in the eval harness and
assert triage rejects it, alongside a true one it must pass through.

### S5b — bound the external-remediation loop

Loom pushes → GitHub fires `synchronize` → the external reviewer re-reviews the
new head → new comments → loom fixes → pushes. Nothing terminates this.

The inline round had an explicit bound (*"No Copilot re-request — one round is
the bound"*, `pr_delivery.py:16`); retiring it removes the bound. And S5's
"one attempt per `(base_sha, head_sha)`" **does not substitute** — every push
mints a new head sha, so that counter resets on precisely the event that should
stop it.

**Budget: 2–3 external-triggered remediation rounds per PR, and it must survive
head changes.** Concretely:

- Counted per PR, on the gate, incrementing per *remediation round* (not per
  finding, not per commit).
- **Does not reset on a loom-authored push.** That is the bug being fixed.
- **Resets on a human push to the branch** — the operator has taken ownership
  and changed the situation — and on a merge, which ends the PR.
- Separate from S5's conflict-attempt budget: a base move and a review comment
  are different events and must not consume each other's allowance.
- On exhaustion → S7 human gate. **Detection is never disabled**: new external
  findings keep being reported as `[ExternalReview]`, they simply stop
  triggering fixes. Going over budget must not blind loom.
- Additionally: skip remediation of findings written against a `head_sha` loom
  itself authored within the current round — they are almost always a
  re-review of loom's own fix in flight.

### S5c — make the range helpers merge-aware (prerequisite for S5)

Merging the base into a story branch breaks an assumption baked into every
range helper in `runner/git.py`. All three are **two-dot against a `base_sha`
captured once at worktree creation** — and `base_sha`'s own docstring says it is
recorded "immediately after worktree creation (before any agent commit)":

```python
rev-list --reverse f"{base_sha}..HEAD"   # commits_since
log      --reverse f"{base}..{head}"     # log_between  → converge's {commit_log}
diff     --stat    f"{base_sha}..HEAD"   # diff_stat    → the REVIEWER prompt (#136)
```

Once the branch absorbs a merge, that sha is no longer the fork point, and each
helper degrades differently:

- **`diff_stat` is the damaging one.** `git diff A..B` is a plain two-endpoint
  tree diff, so after the merge the reviewer's orientation diff contains
  **everything that landed on the base** as well as the story's own work. The
  panel would be asked to review other people's merged PRs.
- **`commits_since` over-counts** — the merged-in commits are reachable from
  HEAD but not from the stale `base_sha`, so they are reported as round
  commits. The `[DevelopResult]` "N commit(s) on branch" inflates, and any
  round-detection keyed on commit count sees phantom work.
- **`log_between`** feeds converge's cold-start `{commit_log}`; it gains the
  base's history as if the PR author had narrated it.

**Fix, per helper — the two-dot/three-dot distinction is exactly the tool:**

- **Diffs → three-dot against the base *ref***: `git diff <base_ref>...HEAD`
  diffs from the current merge-base, yielding the branch's own changes only.
- **Commit enumeration → two-dot against the *current base ref***, not the
  recorded start sha: `rev-list <base_ref>..HEAD` correctly excludes merged-in
  commits because they *are* reachable from the base. Add `--first-parent`
  where only the branch's own round commits are wanted.
- **Stop treating the recorded `base_sha` as the fork point.** Where a sha is
  genuinely needed, recompute it: `git merge-base HEAD <base_ref>`.

**One deliberate exception.** S5's panel reviews the *composed tree*, and a
conflict resolution is not visible in `base...HEAD` at all — a merge commit's
resolution shows only against its parents. That review needs the merge-commit
diff shape (`git show -m` / `--cc`, or a diff against each parent), which is a
different prompt input and must be built as one rather than assumed.

**Guard:** a test asserting that a branch which has absorbed a base merge
produces the **same review diff** as before the merge, modulo the conflict
resolution itself. That is a pair test — the two-dot helpers pass their own unit
tests today precisely because no fixture has a merge commit in it.

### S6 — serial admission (make "serial by default" true, not advisory)

S4's `blocks` edges are the precise mechanism but they rely on a planner adding
them. Imported tasks, separate epics and imperfect decompositions all still
produce concurrent PRs, and `max_concurrent_tasks` (`orchestration.md:541`)
bounds **running tasks, not delivered-but-unmerged PRs** — once delivery
releases its claim, the next story starts while the first `pr` gate is still
open. That is precisely how this batch happened.

**Admission invariant:** before dispatching a story, count the project's **open
`pr` gates on the same base branch**. Default limit **1**; over the limit, the
story is not claimed and waits.

- Configurable per project (`max_open_delivered_prs`), because a project whose
  stories genuinely do not collide should not be throttled.
- Counts **gates**, not claims — that is the distinction the existing knob
  misses.
- **Escalated gates do not count** (operator decision, 2026-08-24). A gate in
  `needs_human` is no longer loom's work-in-progress; it is waiting on a
  decision, and counting it would convert operator latency into a full project
  stop — a stuck decision could halt a project for days. So an escalated gate
  is excluded from the admission count and dispatch continues.
- **But stuck PRs cannot accumulate unboundedly**, so a second, looser cap
  bounds *total* open delivered PRs per project (escalated ones included).
  Reaching it stops dispatch and is itself worth surfacing: several PRs stuck
  awaiting decisions is a signal about the decomposition, not just a queue
  depth. Default meaningfully above the admission limit — the admission limit
  is the steady-state shape, this is the backstop.
- `blocks` edges remain the more precise tool where the decomposition knows
  which stories are safe to run together; admission is the backstop for when it
  does not.

**Cost, stated plainly:** with a limit of 1, dispatch idles while a PR gate
waits on a human merge. In this batch that would have been roughly nine hours.
That is the trade for near-zero conflicts, it is why the limit is configurable,
and it is a strong argument for S1/S2/S5 keeping PRs merge-ready so the gate is
open for as little time as possible.

### S7 — escalation as a first-class state

`[PRConflicted]` and `[MergeGateFailed]` are audit history. They do not model
*"loom needs a decision from you"*, and an append-only finding stream cannot
answer *"what is the state of this PR right now?"*.

**Reconciliation state lives in Lithos** (operator decision, 2026-08-24), on the
`pr` gate plus claimed maintenance subtasks — so Lens renders it and findings
stay history rather than doubling as a database. States:

`awaiting_review` · `reconciling` · `behind` · `resolving_conflict` ·
`gate_failed` · `needs_human` · `ready_to_merge`

When automation stops, loom creates a **human gate** (`gate_type=human`) whose
brief carries what a decision actually needs:

- the PR, story, base/head shas and the affected files;
- **why automation stopped** — dispute, ambiguity, exhausted budget, persistent
  red gate;
- the conflicting requirements or ADRs, where the resolver identified them;
- **what was attempted** and the check-set / panel results of each attempt;
- the choices the resolver considered, so the operator picks rather than
  re-derives;
- retry / approve / abandon as the available actions.

This is the difference between "loom told me something went wrong" and "loom
handed me a decision". It also gives the gate a real state machine rather than
the current binary open/merged, which is what makes a Lens console possible.

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

### S8 — measure the autonomous paths before trusting them

This PRD proposes the largest autonomous mechanism loom has, and none of it is
currently measurable. The review-correctness harness
([ADR 0005](../adr/0005-review-correctness-eval-harness.md)) scores panel
catch-rate on **seeded defects in a diff**; it has no notion of a composed tree,
an injected external finding, or a merge resolution.

That matters here more than it would elsewhere, because the whole
review-hardening epic exists to stop shipping unmeasured review changes — and
RH-5's conclusion was that a single unmeasured arm is not evidence. Shipping S5
on the strength of "it seemed to work on one PR" is the same mistake at ten
times the scale.

Three case shapes, all buildable from material already on hand:

- **Conflict resolution.** #43 is a ready-made fixture: base `61993963`, the
  delivered head, the landed pair (#44 + #45), and **the operator's own
  resolution as known-good**. Score whether an agent resolution passes the same
  check-set, and whether the panel accepts a *deliberately wrong* resolution —
  a plausible-looking merge that drops `projects` from
  `filters_narrow_the_board` is the obvious seeded defect, since that is the
  real one.
- **Triage.** A known-false external finding must be rejected with cited
  evidence; a known-true one must pass; an ambiguous one must proceed. The
  over-suppression failure mode is the one to watch, per RH-1's lens34 result.
- **Composed-tree review.** Whether the panel can find a defect that exists
  only in the *combination* of two individually-correct branches — the
  `filters_narrow_the_board` class. If it cannot, S5 narrows to check-set-only
  auto-push for non-semantic conflicts, which is a narrowing rather than a
  redesign.

**Precondition on any A/B here, from RH-5:** state the minimum detectable effect
before paying for an arm. These are near-0/near-saturated questions, which is
the regime K=5 can actually resolve — unlike a mid-band case, which needs
K≈20–30 per arm.

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

   **Destructive → never without explicit approval.** The invariant is
   **"never perform a non-fast-forward push or history rewrite"** — a property
   of what happens to the ref, *not* of which git flag spells it.

   **Correction (2026-08-24).** An earlier revision of this PRD asserted that
   `src/` contains zero force pushes and proposed a guardrail banning
   `--force-with-lease`. Both were wrong. `push_to_pr_ref`
   (`pr_delivery.py:360`) uses `--force-with-lease`, and it is **correct code**:
   it first proves ancestry with `git merge-base --is-ancestor` and raises
   `MergeRaceDetected` when the reviewed head does not descend from the expected
   remote head, then uses the lease as an atomic compare-and-swap pinning
   origin's ref to `expected_remote_sha`, closing the `ls-remote`→push TOCTOU
   window. A deleted, advanced or rewound ref is rejected as "stale info"
   instead of being silently overwritten. The proposed guardrail would have
   **deleted a safety mechanism in the name of safety**.

   So the rule is: an automated path may push only when the new tip **descends
   from** the ref it replaces, and must prove that before pushing. A lease is
   the right tool for that proof. Rewriting a delivered branch's history — a
   non-descendant push, however spelled — is an operator action, surfaced for a
   decision, never taken automatically.

   **Implementation requirement:** a `tests/guardrail/` contract asserting every
   push site in `src/` is ancestry-guarded — i.e. any `--force`/lease push is
   preceded by an `--is-ancestor` check on the same refs — rather than grepping
   for a flag. Test it negatively: a push site added without the guard must
   fail the contract.

   What still reports rather than acts, and why: **conflicts in real source, or
   a red gate.** Those need judgement, and the evidence is
   `filters_narrow_the_board` — two individually-correct PRs whose composition
   was wrong in a way no merge driver could see. Rebase is not needed anywhere
   in this design; a merge achieves the same landability additively, and it is
   what the operator chose unprompted on #43 (*"kept as a merge so the per-round
   story-develop commits survive"*).
3. **Conflict resolution is attempted automatically; only the residue is
   human.** *(Revised 2026-08-24 — this previously read "stays human or
   converge", which the operator rejected: mechanical cases are loom's job.)*
   The evidence that these conflicts need **judgement** stands
   (`filters_narrow_the_board` is not merge-driver territory) — but judgement is
   not the operator's monopoly. S5 runs a coder resolution, has loom's panel
   review the composed tree, runs the project's current check-set, and pushes
   only on green. One attempt per `(base_sha, head_sha)`; failure escalates to
   an S7 human gate rather than retrying.
4. **The gate is never auto-resolved by this work.** Only a merge completes a
   `pr` gate, exactly as epic H specifies.
5. **A fresh finding prefix per concern** — `[PRConflicted]`,
   `[ExternalReview]`, `[MergeGateFailed]` — per the house rule against
   overloading existing prefixes, since operators grep by prefix.
6. **Retiring the inline Copilot round must not lose automatic remediation.**
   Detection always runs; fixing moves to the convergence path and is **on by
   default** (`external_review_converge`), because a fast-forward push is
   additive per Decision 2. Default-off would make this PRD a net regression
   against the round it retires. *(Corrected 2026-08-24 — this decision
   previously said "opt-in and off by default", contradicting S2.)*
7. **External findings are injected into the fix loop, never used as a bare
   trigger.** Converge's own intake returns `already_clean` when loom's panel
   sees nothing — which is the panel that missed the defect. A trigger-only
   design would reliably do nothing.
8. **Untrusted comment text never reaches an agent.** Allowlisted bots plus
   repo write/admin seed the coder; everyone else is reported to the operator
   only. Comment bodies are third-party input on a prompt path.
9. **Reconciliation state is persisted in Lithos, on the `pr` gate plus
   maintenance subtasks** — not in loom logs, and not modelled by findings.
   Findings are history; the gate is current state; Lens is the console.
10. **Serial admission counts open `pr` gates, not active claims**, defaulting
   to one per project + base branch. `blocks` edges stay the precise mechanism;
   admission is the backstop for decompositions that lack them.
11. **Loom is the single writer of a gate's reconciliation state.** Lithos has
   no optimistic concurrency on task updates — `lithos_task_update` takes no
   `expected_version`, unlike `lithos_write` for notes — so two writers can lose
   an update on the same key. One reconciler owns a gate; webhooks and other
   triggers *enqueue*, they do not write. Chosen over adding CAS upstream
   because it is the smaller change and has no cross-repo dependency.
12. **External claims are triaged before they are actioned**, by a separate
   cheap step rather than a sceptical clause in the fixer prompt — and triage
   defaults to *acting* when uncertain, because actioning a false positive is
   recoverable and ignoring a true one is the failure this PRD exists to fix.
13. **External-triggered remediation is bounded per PR (2–3 rounds), and the
   budget survives head changes.** It resets on a human push, never on loom's
   own. Exhaustion escalates and stops *fixing*; it never stops *reporting*.
14. **S3 re-resolves the project's current check-set rather than replaying the
   one the story passed.** The question is "will main break", and main is
   defended by today's config. The cost is that a gate must always carry its
   `project` to be resolvable at all.

## Alignment with the other active PRDs

This PRD now owns a chunk of behaviour the orchestration plan also describes.
Reconciling explicitly, so the two do not drift:

- **`story-fix` (`orchestration.md:355`) overlaps with external-review
  convergence.** Resolution: **converge is the single pre-merge remediation
  engine** — external findings, conflict resolution and re-gating all run
  through it (operator decision, 2026-08-24). `story-fix` is either scoped to
  *post-merge* failures, or reimplemented as a caller of the same loop. Two fix
  loops is precisely what ADR 0004 §1 single-sources against.
- **Webhooks (`orchestration.md:441`) currently cover only `pr`-gate merge
  resolution.** They should wake **this same state machine** on `review`,
  `review_comment`, `synchronize` and base-update events. Polling stays as the
  recovery path — a missed webhook must degrade to "slower", never to "never".
- **`merge-stories` (`orchestration.md:420`) introduces integration branches.**
  Everything here must therefore say **"the PR's current base branch"**, never
  "main". Any remaining "main" in this document is a bug; S3's re-gate, S6's
  admission count and S1's behind-detection are all per-base-branch.
- **The console is Lens, not loom.** `orchestration.md:469` proposes a loom CLI
  dashboard and excludes a web UI. Keep the CLI as an operational/debug surface;
  Lens is the authoritative console, which is exactly why S7's state is
  persisted in Lithos rather than in loom.
- **A9 (`orchestration.md:374`) is the right knowledge-feedback foundation** for
  recording why a resolution was chosen; S5's decision briefs are a natural
  producer of that.
- **The capture-macro PRD is independent** — no material conflict. It is an
  optional Obsidian capture adapter beside the primary Lens experience.

## Non-goals

- Auto-merge, merge queues, or branch protection. Lens has no protection today;
  a merge queue serialises landing but does not resolve a conflict, and it is a
  repo-policy decision, not an orchestrator feature.
- Rewriting delivered branches without an operator asking.
- ~~Reviewing the merge resolution with a panel.~~ **Now in scope as S5** —
  the operator's requirement is that routine conflicts do not reach them, and a
  resolution nobody reviews is not one loom should push.
- Fixing #288. Independent, still real, tracked separately.
- **Webhooks.** Polling is the v1 trigger; webhooks wake the same machine later
  (see Alignment). Deferred, not rejected — and polling must remain the
  recovery path, so a missed webhook degrades to "slower", never to "never".
- **Rebase as a landing strategy.** A merge achieves the same landability
  additively; rebasing a delivered branch is a history rewrite and needs the
  operator (Decision 2).

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
- Retiring the inline round deletes `pr_delivery`'s Copilot tests, and it also
  moves an **on-disk contract**: `delivery_budget_seconds` loses its
  `copilot_timeout` term, and `run_outcome.py:64` documents the composed budget
  (`copilot 600 + coder 3600 + gate 900 + overhead 1800 = 6900s`) that `attach`
  uses to decide a delivery is incomplete rather than slow. `run_outcome` is
  pinned as a leaf on-disk-contract module (`test_run_outcome_leaf.py`), so the
  budget change, its docstring contract test, and `attach`'s timeout behaviour
  must all move in one diff — not discovered when a live run reports a false
  delivery timeout.
- **S5c (merge-aware ranges)** — the pair test: a branch that has absorbed a
  base merge produces the **same review diff** as before the merge, modulo the
  resolution itself. Every existing range test passes today only because no
  fixture contains a merge commit; add one first, watch `diff_stat` fail, then
  fix. Also assert `commits_since` does not count merged-in commits.
- **S5a (triage)** — a known-false external finding is rejected **with cited
  evidence**; a known-true one passes through; an ambiguous one **proceeds**
  (the default-to-act rule is the property most likely to be silently
  regressed by a prompt re-tune, so pin it). Natural home is the eval harness,
  alongside the seeded-defect cases.
- **S5b (budget)** — the loop terminates: N loom-authored pushes do **not**
  reset the counter, a human push **does**, and exhaustion stops remediation
  while `[ExternalReview]` findings keep being posted. The failure mode is a
  budget that resets on the wrong event, so test the reset condition
  negatively.
- **S6 (admission)** — counts open `pr` gates, not claims; an escalated
  (`needs_human`) gate does not block the project indefinitely; the limit is
  per project **and base branch**, so two base branches do not starve each
  other.
- **S7 (state)** — single-writer discipline is testable: a second concurrent
  reconciler must not transition a gate it does not own. Assert the state
  machine's illegal transitions are rejected rather than silently applied.

## Slices

| # | slice | ships | cost |
|---|---|---|---|
| 0 | **S0 real task brief + PR body** — implemented, PR #333 **open** | the coder, the panel AC and the PR body all get the description | none |
| 1 | S1 landability + `[PRConflicted]` — **shipped** (detection; the `behind` auto-update rides with S3) | two fields, one branch, one marker | none |
| 2 | S2 ingestion + retire the inline round | delivery gets faster and simpler | none |
| 3 | S3 re-gate on base move | the merge-blindness fix | none |
| 4 | S4 prevention | graph edges + generated-file policy | none |
| 5 | **S5c merge-aware ranges** | prerequisite: reviewers stop seeing other people's work | none |
| 6 | **S5a external-claim triage** | a wrong bot comment does not become a wrong commit | one cheap call per finding |
| 7 | **S5b remediation budget** | the two-bot loop terminates | none |
| 8 | **S5 conflict convergence** | routine conflicts resolved without the operator | 1 attempt per sha pair |
| 9 | **S6 serial admission** | concurrent delivered PRs bounded by config, not by hope | none |
| 10 | **S7 escalation state** | a decision brief in Lithos, renderable by Lens | none |
| 11 | **S8 measurement** | the autonomous paths get an instrument before they get trusted | eval runs |

*(Slice numbers are delivery order; `S<n>` labels name the design sections and
are deliberately not renumbered, so review comments referring to "S5" keep
meaning the same thing.)*

**Delivery order and why:**

1. **S0** — merged/queued already (#333). A live correctness bug, and every
   measurement downstream is confounded until it lands.
2. **S6 serial admission** — free, and it reduces how often everything else has
   to fire. The cheapest intervention in the document.
3. **S4 prevention** — also free, also reduces frequency; consuming-project
   policy, so it can proceed in parallel.
4. **S1 + S3** — detection and the merge-blindness fix. Independent of the
   Copilot prerequisite, so they can land while that is confirmed.
5. **S5c** — a hard prerequisite for anything that merges into a story branch.
   Landing S5 without it means reviewers see other people's PRs.
6. **S2 + S5a + S5b** — remediation, its triage guard and its bound. These
   three ship together or not at all: S2 without S5a actions wrong claims, and
   without S5b it does not terminate.
7. **S5** — conflict convergence, the largest and least certain slice.
8. **S7** — the Lithos half can accompany S5; the Lens console follows.
9. **S8** — measurement should ideally precede trusting S5, not follow it.

The one external dependency is slice 2's Copilot prerequisite (the repo-level
automatic-review setting); nothing else waits on it.

## Open questions

1. **Does lithos-lens have automatic Copilot code review enabled at the repo
   level?** Gates slice 2's default. Loom requested Copilot on all four PRs, so
   the observed reviews cannot distinguish the two causes. One settings check.
2. ~~Does an escalated (`needs_human`) gate count toward S6's admission
   limit?~~ **Decided 2026-08-24: no.** An escalated gate is waiting on the
   operator, not on loom, and counting it would turn operator latency into a
   project-wide stop. A separate looser cap on *total* open delivered PRs stops
   stuck ones accumulating. Written into S6.
3. ~~Should the cross-cutting decisions become an ADR?~~ **Decided
   2026-08-24: yes, one ADR covering all four** —
   [ADR 0011](../adr/0011-pr-maintenance-invariants.md): converge as the single
   pre-merge remediation engine, reconciliation state in Lithos, single-writer
   concurrency, and the additive-only push invariant. Decisions 1, 2, 7, 9 and
   11 below are its PRD-side restatements; the ADR is authoritative.
4. **Usage limits, not cost, are the resource constraint.** Coding agents run
   on a subscription, so dollar figures in this document are indicative only.
   The real risk is autonomous work consuming the same allowance the operator
   is using interactively. story-develop has usage-limit reactions; the open
   question is whether the *sweep* defers cleanly or retries into a wall.
5. **Webhook enqueue mechanism.** The alignment section says webhooks should
   wake this state machine, and Decision 11 says non-owners enqueue rather than
   write. That queue does not exist yet; polling is the v1 trigger.
