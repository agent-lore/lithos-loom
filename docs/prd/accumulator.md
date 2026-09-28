---
title: Lithos Loom — Accumulator (scope carried forward from the legacy PRDs)
milestone: M-next (working title — renamed and refactored once every legacy PRD is processed)
status: draft
target_version: tbd
references:
  - docs/SPECIFICATION.md (implemented surface — the contract this document must not contradict)
  - docs/prd/archive/orchestration.md (processed 2026-09-28 and archived — disposition below)
  - docs/prd/pr-reconciliation.md (not yet processed)
  - docs/prd/capture-macro-tag-parsing.md (not yet processed)
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
> Until then it accumulates; it does not yet claim a milestone.

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
| `pr-reconciliation.md` | — | pending. 10 of 11 slices are on main; expected outcome is a closing status line plus the four follow-ons already tracked as tasks |
| `capture-macro-tag-parsing.md` | — | pending. Shipped Obsidian slice; expected outcome is a move to `archive/` |
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

### Parked from other sections (not for the first slice of this PRD)

5. **P5 — The merge-policy dial (from A3).** As the operator, I want each
   project to declare how a delivered PR is merged — `human` (today's `pr`
   gate), `shadow` (loom records what it *would* have merged and why, the gate
   stays human), `canary` (loom merges in a named project when the merge-gate
   is green and the panel and external review agree), with an `every-n` human
   checkpoint available under the last two — so that autonomy is turned up per
   project on evidence rather than switched on globally. Held here; designed
   and sliced in the perpetual-daemon PRD, where the confidence measurement
   (shadow recording against the eleven labelled PRs) is the first slice.
6. **P6 — Ops residue (from XC).** As the operator, I want a `systemd --user`
   unit that runs the daemon through `lithos-loom drain` on stop, and a
   usage-share reserve so autonomous work cannot consume the subscription
   allowance I am using interactively (the task-level `develop_max_rounds` /
   `develop_max_cost_usd` knobs of #350 are the per-story half of that), so
   that restarts stop being an intervention class (four of the six T2-era
   hands were host restarts under a run) and the resource that actually binds
   is enforced rather than watched. Held here pending the perpetual-daemon PRD.

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

## Not yet processed

- `pr-reconciliation.md` — expected: closing status (10 of 11 slices on
  main), carry nothing new; the follow-ons are tasks 2bf0bb2b, d48caecd
  (#374), 7bd2696b (#391), 307ac035 (#395).
- `capture-macro-tag-parsing.md` — expected: archive.
