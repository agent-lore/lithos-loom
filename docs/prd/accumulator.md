---
title: Lithos Loom — Accumulator (scope carried forward from the legacy PRDs)
milestone: M-next (working title — renamed and refactored once every legacy PRD is processed)
status: draft
target_version: tbd
references:
  - docs/SPECIFICATION.md (implemented surface — the contract this document must not contradict)
  - docs/prd/archive/orchestration.md (processed 2026-09-28 and archived — disposition below)
  - docs/prd/archive/pr-reconciliation.md (processed 2026-09-28 and archived — disposition below)
  - docs/prd/archive/capture-macro-tag-parsing.md (processed 2026-09-28 — archived unbuilt)
  - docs/adr/0011-pr-maintenance-invariants.md (per-story PRs to main — the model that retired the integration branch)
  - docs/adr/0012-admission-release-order.md (serial admission — what an ordering edge still has to add)
labels: [needs-triage, lithos-loom, orchestrator, planning]
---

# Accumulator — scope carried forward from the legacy PRDs

> **Status (2026-09-28).** A working document, not a finished PRD. Each legacy
> PRD under `docs/prd/` is read once, section by section, and every section is
> either **carried** (rewritten here as a story in today's terms), **shipped**
> (already on main, with the evidence), or **not carried** (with the reason, so
> the decision can be vetoed rather than rediscovered). When the ledger below
> shows every legacy PRD processed, this file is renamed, its stories are
> sliced, and the processed PRDs move to `docs/prd/archive/` with a pointer.
> Until then it accumulates; it does not yet claim a milestone. *(The ledger
> closed on 2026-09-28; the tidy-up is the next step.)*

## Why this exists

The graph lagged the repo. On 2026-09-25 the open lithos-loom graph held 97
items; 33 of them were the orchestration PRD's A-layer stories, of which zero
had shipped in three months while thirty *other* loom tasks closed in
September, every one of them pulled forward by friction from running lens T2
through loom. Two of the A-layer's premises had quietly become false (the
integration-branch delivery model, and consumers that never arrived), and
several stories had shipped in a different shape without anyone closing them.
A plan whose stories cannot be checked against `git log` is not a plan. This
document is the check.

## Ledger

| Legacy PRD | Processed | Result |
|---|---|---|
| `orchestration.md` → `archive/` | 2026-09-28 | G + H shipped; A2's decompose half carried as P1–P4; A3's review policy and XC's ops residue parked as P5–P6; A1, A4, A5, A6, A7, A8, A9 and the rest of A2/XC not carried (see disposition) |
| `pr-reconciliation.md` → `archive/` | 2026-09-28 | all eleven slices shipped (the table had marked six); follow-ons d48caecd (#374) and 7bd2696b (#391) carried as R3/R4, 2bf0bb2b as P6's acceptance test; nothing else carried |
| `capture-macro-tag-parsing.md` → `archive/` | 2026-09-28 | archived **unbuilt**: US47–US54 never reached the macro; only D40's tag regex shipped, as `TAG_REGEX` in the bulk-import line parser. Dropped because the operator no longer captures through the Obsidian macro; nothing carried |
| `archive/*` | n/a | already archived |

## Decisions taken while processing (2026-09-28, with Dave)

1. **PRD authoring and review are interactive, not loom plugins.** Writing a
   PRD, reviewing it and turning it into slices is operator-side work run as
   skills in the operator's harness (`to-prd`, `to-issues`, a review pass)
   against the Lithos MCP. The September plan names the operator's attention
   as the binding constraint and the T2 PRD took two review rounds with Dave in
   the loop; that is the step where his judgement matters most, so it stays
   interactive. Loom's obligation shrinks to (a) documenting the graph
   contract the workflow must emit and (b) the writer that emits it.
2. **`lithos-coding-mcp` is not required.** The repo is a May skeleton with no
   further commits. The operator surface it was to provide is the loom CLI
   (`develop`, `converge`, `review`, `deliver`, `merge-gate`, `gates`); the
   agent-facing surface on the host is the Lithos MCP itself. Inside the
   story-develop container the coder gets ADRs and context from the tree
   (`docs/adr/`, `CONTEXT.md`) and discovered work leaves through the reviewer
   handoff, which loom already turns into a spawned follow-up task. Whether a
   sandboxed agent should ever reach Lithos directly is open question Q1, not a
   story.
3. **The integration-branch delivery model is dead.** Since August every story
   ships as its own PR to `main` under a human `pr` gate, and the whole
   PR-maintenance series and serial admission are built on that
   ([ADR 0011](../adr/0011-pr-maintenance-invariants.md)). Anything in a legacy
   PRD that assumes `loom/<prd-slug>` or a terminal merge-stories step is
   disposed on that basis.
4. **pr-reconciliation.md is complete, and the review pipeline's next problem
   is convergence, not catch rate.** Every slice of the PR-maintenance series
   is on main, including S8's three instruments. The week of 2026-09-21 then
   produced five story-develop / converge runs that did not converge, at a
   cost of $495, with codex as the correctness reviewer in all five and the
   coder never once taking the `needs-decision` exit. August's
   review-hardening epic (61a2bd00) was about what the panel *misses*; the
   escape corpus (#401, #402, #409) now measures that. The new evidence is
   about the loop failing to *stop* and reviewers judging against an unstated
   operational model. That is the second section of this document, and the
   review-hardening epic is re-cut against it there.

## Disposition of `orchestration.md`

| Section | Fate | Evidence |
|---|---|---|
| G — graph adoption (US1–US9) | shipped | header of the PRD itself; `lithos_task_ready` gates dispatch; `project import` writes epics + `blocks` edges (US9, #260) |
| H — human-merge gate (US10–US13) | shipped | PRs #261–#263; the `pr` gate is the sole awaiting-merge state |
| A1 — plugin SDK, bash-runner, events.jsonl, idempotency (US14–US18) | not carried | only two plugins exist (story-develop, echo) and decision 1 removes the two that would have been next; US18 shipped inside story-develop only (task b25d9e33 closed with that outcome) |
| A2 — prd-generate (US19) | not carried | PRDs are hand-written with the Pocock skills (the MVP PRD says so; T2 was) |
| A2 — prd-review-agent (US20) | carried as **P1** | as a skill, per decision 1 |
| A2 — prd-review-human gate (US21), auto-retag on approval (US23) | not carried | under decision 1, approval *is* running the decompose step; no gate or retag is needed between them. Generic `human` gates exist anyway (b91177d2) |
| A2 — prd-decompose (US22) | carried as **P2, P3, P4** | minus the integration branch (decision 3); the stub package retires with P4 |
| A3 — review policy (US24, US25) | parked as **P5** | it is the merge-policy dial (human-per-story → shadow → canary) that gates the perpetual-daemon goal; designed in that PRD, held here so it is not lost |
| A3 — story-fix (US26) | not carried | superseded by `develop converge` + S2/S5a/S5b + escalation (task 9edce802 cancelled 2026-09-26) |
| A9 — lithos-coding-mcp (US27, US28) | not carried | decision 2 |
| A4 — decide-next brain (US29, US30) | not carried | the failure classifier and reaction table (5dbeb0c8 slice B, #378) decide deterministically; Dave's rule is "escalate if stuck, never silently continue", which is the opposite of a model choosing `cancel_remaining` |
| A5 — crash recovery (US31) | not carried | restart family on main (orphan reaping, `drain`, `develop deliver`); the per-round checkpoint is task 307ac035 (#395) |
| A5 — loom-improve (US32) | not carried | the escape-review process (#347) is the human version and is producing eval cases; automate only when its shape is stable |
| A8 — merge-stories (US33) | not carried | decision 3 (epic 7148f23e cancelled 2026-09-26) |
| A6 — A2A endpoint (US34) | not carried | no consumer; Agent Zero / Hanuman are not dispatching to loom and "what is ready" is answered by the Lithos MCP and lens |
| A7 — multi-host (US35), webhook (US36) | not carried | one host; webhook enqueue is the PR-reconciliation PRD's open question 5 and polling remains v1. Re-file if a second host or a latency complaint appears |
| A7 — SSE readiness re-evaluation (US37) | shipped | the route-runner's readiness re-check plus #352 (task 8c8eba46 closed with that outcome) |
| XC — `[Plan]` / `[Drift]` findings (US38) | not carried | the scope-dispute escalation (#424) covers the under-delivery half that hurt; over-delivery has not cost an intervention. Candidate if the escape corpus shows otherwise |
| XC — cost / dashboard / replay / OTel / systemd (US39) | residue parked as **P6** | lens is the dashboard; `develop list` / `develop attach` are the replay; OTel has no consumer. Still owed: the systemd unit and a usage-share reserve |
| Implementation decisions — config additions (`mode = webhook`, `next_route`, `decide_via_brain`, `[loom_improve]`, `claude_config`, `host_affinity`) | not carried | each falls with its feature above; `review_policy` returns with P5 |

## Disposition of `pr-reconciliation.md`

| Section | Fate | Evidence |
|---|---|---|
| S0 real brief, S1 landability, S3 merge-gate, S5/S5a/S5b/S5c convergence, S6 serial admission, S7 reconciliation state | shipped | its own slices table and AGENTS.md; epic 000a4f9f completed 2026-09-26 |
| S2 external-review ingestion + inline round retired | shipped | slices A–D; the row was never marked, the sections were |
| S4 prevention | shipped (loom half) / practice (edge half) | generated-paths policy in merge-gate + resolver intake; the `blocks`-edge rule is carried as P3 |
| S8 measurement | shipped | `eval resolve`, `eval triage`, `lens43-composed-projects`, each with a first reading; the "A/B precondition" is process, not a deliverable |
| follow-on d48caecd (#374) | carried as **R3** | the panel blocks correct conflict resolutions on pre-existing story code |
| follow-on 7bd2696b (#391) | carried as **R4** | merge-gate red has no autonomous fix path |
| open question 4 / task 2bf0bb2b | carried into **P6** | "usage limits, not cost, are the constraint": the sweep must defer on a subscription limit |
| open question 1 (repo-level Copilot review on lens) | moot | S2 retired the inline round; nothing depends on the answer |
| open question 5 (webhook enqueue) | not carried | polling is v1; the A7 webhook was cancelled with the orchestration PRD |

## Carried stories

Numbered fresh (P-prefix) so they cannot be confused with the legacy US
numbers. Same house style as every loom PRD: independently grabbable, each
with the evidence that makes it worth doing.

### The PRD-to-graph workflow (from A2)

1. **P1 — PRD review is a skill, and loom documents the contract it feeds.**
   As the operator, I want PRD authoring and review to be an interactive
   workflow in my harness (`to-prd`, a PRD-review pass, `to-issues`) whose
   *output* — a slice list — is what loom consumes, so that my judgement stays
   in the loop at the point where it matters and loom carries no plugin whose
   only job is to wait for me. Ships: a workflow document in the loom repo
   (alongside `docs/macros/`, which already documents operator-side Obsidian
   workflows) stating the slice-list shape the decompose step accepts, and the
   review brief the pass runs with. The output schema in
   `plugins/prd_decompose/prompt.md` (title, ≥80-word brief, acceptance
   criteria, deps, files hint) is the starting point and moves into that
   document.
2. **P2 — Decompose writes the dispatchable graph.** As the operator, I want a
   loom command that takes a reviewed slice list and writes, for one project,
   an `epic` plus one `task` per slice with `parent_task_id`, `blocks` edges
   from the declared deps, `project:<slug>` + `metadata.project`, and the
   `trigger:story-develop` tag (or none, for projects loom must not
   auto-dispatch — loom's own stories deliberately omit it), with `--dry-run`
   rendering the tree and each task's readiness before anything is written, so
   that a decomposed PRD is a runnable pipeline in one step instead of hand
   entry followed by a separate tagging pass (which is how lens T2 was created
   on 2026-09-04 and re-tagged on 2026-09-13). Reuses `project import`'s bulk
   path (`task_graph.build_plan` + `_project_import_bulk.create_tasks`), which
   already writes exactly this shape from an indented list; whether this is a
   new subcommand or an input adapter on `project import` is open question Q2.
3. **P3 — Slices declare the surfaces they touch; overlap is serialised.** As
   the operator, I want each slice to name the surfaces (paths, modules,
   screens) it will change, and the writer to add a `blocks` edge, in PRD
   order, between any two slices whose surfaces overlap unless they already
   have one, refusing to write an edge-less overlapping pair without an
   explicit `--allow-parallel-surface`, so that the failure that produced the
   PR-maintenance series cannot be re-planned: T1-S5/S9/S10/S12 were four slices
   of one dashboard with no edges, the ready queue offered all four, and every
   one needed a hand to land (pr-reconciliation S4: "`blocks` edges would have
   prevented all of them"). Serial admission (S6) now stops them *running*
   together; what the edge still encodes is *order*, and order is what stops
   the second story being cut from a main that the first has not reached.
4. **P4 — Retire the `prd-decompose` stub.** As a maintainer, I want the
   `plugins/prd_decompose` package, its `prompt.md`, the reference in
   `plugins/__init__.py` removed in the same change that lands P2, so
   that there is no dead scaffolding pointing at a superseded PRD — the same
   subtraction US2 did for `story-implement` and `story-review-human`.

### Review convergence (from the week of 2026-09-21, and the review-hardening epic)

The evidence, from the two stories Dave filed on 2026-09-25 (34bb82c4,
a3f17c21) and their 2026-09-26 addenda:

| Run | Story | Correctness tool | Outcome | Cost |
|---|---|---|---|---|
| d9287814 | f78e6223 `develop deliver` | codex | disputed, 4 rounds | $89 |
| c2e44d61 | #427 remediation | codex | disputed, 4 rounds | $67 |
| 86613f8e | fd71001f `converge-push` | codex | max_rounds, 5 | $112 |
| 8e9ac9c8 | #431 remediation | codex | max_rounds, 5 | $125 |
| a1817376 | 307ac035 resume, #433 remediation | codex | max_rounds, 5 | $102 |

Two shapes recur. The reviewer treats any constructible interleaving as a
defect because nothing states the operational model (single operator,
hand-run commands, Lithos reachable within a claim TTL), and the coder fixes
rather than asking, so each fix grows protocol and the next round's blocking
ids are new. And the loop has no notion of "not converging": it runs to
`max_rounds` and hands the operator a verdict instead of the question. The
a1817376 run adds the twist that even *in-model* lifecycle findings did not
converge, so the fix is not "lifecycle findings are minor" but a model precise
enough to tell in-model from out-of-model, with `needs-decision` as the exit for
either when the criteria do not settle it.

5. **R1 — Reviewers judge against a stated operational model.** As the
   operator, I want a project-level `develop_review_scope` block (Markdown, in
   the project-context doc, host default optional) rendered under the
   acceptance criteria in every reviewer prompt and the triage prompt, and the
   coder prompts to route a finding that needs an actor or condition neither
   the criteria nor the block names to `needs-decision` ("is <actor> in scope
   for this command?") rather than a fix or a bare dispute, with a reviewer
   `contest` conceding unless it cites the line that names that actor, so
   that a product question costs one round and one sentence from me instead of
   $100 of protocol. Acceptance and fixtures as filed on task 34bb82c4,
   including the in-model triple-failure case from a1817376.
6. **R2 — A churning review stops with the question.** As the operator, I want
   the shared round loop (story-develop, `converge`, `--from-github`,
   `--resolve-conflicts`) to compare each round's blocking finding ids with
   the previous round's from round 3 on, treat a round that opened at least
   one blocking id and carried none over as churned, and end the run after two
   consecutive churned rounds (`develop_review_churn_rounds`, default 2, 0 =
   off) with `escalation.reason = review_not_converging` and a brief listing
   per round which ids opened and closed, so that the disagreement reaches me
   while it is still legible and a round or two of spend is saved per stalled
   run. Sequences and acceptance as filed on task a3f17c21.
7. **R3 — Composed-tree review blocks only on the conflicted hunks.** As the
   operator, I want the panel in `--resolve-conflicts` mode to block on
   defects inside the merge's conflicted hunks and report anything in
   pre-existing story code as non-blocking, so that a correct resolution is not
   held for a defect the story already shipped and the operator already
   accepted (task d48caecd, #374). The same disease as R1 in resolve mode: the
   scope of judgement is unstated, so the reviewer takes the widest one.
8. **R4 — Merge-gate red has a fix path and keeps its reason.** As the
   operator, I want a `[MergeGateFailed]` to carry the failing check's reason
   (not only its output tail) and to feed the same remediation loop an
   external review does, bounded by the same budget, so that a base move that
   breaks a delivered PR is fixed by loom when it can be and escalated with a
   legible cause when it cannot (task 7bd2696b, #391, from lens #85).

**Re-cut of the review-hardening epic (61a2bd00) and the other open review
items** — 27 of the 64 open loom tasks on 2026-09-28 touch review. Their fate
under this section:

| Cluster | Tasks | Fate |
|---|---|---|
| Termination and scope | 34bb82c4, a3f17c21, d48caecd, 7bd2696b | **R1–R4**, the first slices |
| Reviewer trust in claims | c7b1adee, 4db7f60b, 32347e77 + 4b6a1565 (sandbox disclosure) | candidate, second slice: a reviewer that accepts an unverified environment claim is the mirror of one that blocks on an unstated actor; both are scope statements the panel lacks |
| Panel coverage gaps | f78669ae spec-conformance persona, 77064874 AC-completeness, 8c1f33e0 visual UI evaluation, b15f937c cross-file context (#92) | candidate, ordered by what the escape corpus shows the panel actually misses; none is dispatched on a hunch |
| Measurement | 23db4be6 RH-4, cb29b6af RH-9, e46200f9, 4777ca14, 189657db, a666a81d, d9e67eeb, 88afc13b | the escape corpus is the instrument now (RH-9's question is partly answered: engine-confounded, no arm dominates); the rest are harness hygiene, kept as issues, not roadmap |
| Engine robustness | 5061554c (#411), fe400fb5 (#420), 942ca9bb (#155) | reliability, not review quality; kept as issues |
| Elsewhere | f8b5ff7c shared reviewer package (cardinal), 21d4a59e page capture, 54fa7c3e provenance labels | candidate, unchanged |

The epic 61a2bd00 itself is retained as the container for its children and
tagged `prd:accumulator`; its charter ("close the 2026-08 baseline blind
spots") is superseded by this section and the escape-review process.

### Parked from other sections (not for the first slice of this PRD)

9. **P5 — The merge-policy dial (from A3).** As the operator, I want each
   project to declare how a delivered PR is merged — `human` (today's `pr`
   gate), `shadow` (loom records what it *would* have merged and why, the gate
   stays human), `canary` (loom merges in a named project when the merge-gate
   is green and the panel and external review agree), with an `every-n` human
   checkpoint available under the last two — so that autonomy is turned up per
   project on evidence rather than switched on globally. Held here; designed
   and sliced in the perpetual-daemon PRD, where the confidence measurement
   (shadow recording against the eleven labelled PRs) is the first slice.
10. **P6 — Ops residue (from XC).** As the operator, I want a `systemd --user`
   unit that runs the daemon through `lithos-loom drain` on stop, and a
   usage-share reserve so autonomous work cannot consume the subscription
   allowance I am using interactively (the task-level `develop_max_rounds` /
   `develop_max_cost_usd` knobs of #350 are the per-story half of that), so
   that restarts stop being an intervention class (four of the six T2-era
   hands were host restarts under a run) and the resource that actually binds
   is enforced rather than watched. Held here pending the perpetual-daemon PRD.
   Acceptance carried from pr-reconciliation open question 4 (task 2bf0bb2b):
   the reconcile sweep defers cleanly on a subscription usage limit instead of
   retrying into the wall.

## Open questions

1. **Should a sandboxed agent ever reach Lithos directly?** Today it cannot
   (nothing mounts an MCP into the container). The coder gets ADRs from the
   tree and discovered work leaves via the handoff; nothing in the T2 rollout
   was lost for want of it. Task 0b5ca206 (#92, capability profiles) is the
   place to decide; it stays a candidate.
2. **New subcommand or an adapter on `project import`?** The bulk-import path
   already writes the shape P2 needs from an indented Markdown list with
   `[sequential]` markers; the slice list from P1 is richer (deps, surfaces).
   Decide at slicing time; the contract is the same either way.
3. **Where does the review brief live?** Loom already ships operator-side
   workflow docs under `docs/macros/`; the alternative is the operator's
   harness only. Proposal: the contract and brief in the loom repo (so a
   consuming project can find them), the skill invocation in the harness.

## Ledger complete

Every legacy PRD under `docs/prd/` has been processed as of 2026-09-28. What
remains for this document is its own tidy-up: a real name and milestone, the
stories sliced in delivery order, and the status header rewritten from
"accumulator" to a plan.
