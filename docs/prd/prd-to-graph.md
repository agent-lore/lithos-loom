---
title: Lithos Loom — PRD to graph
milestone: M-P2G
status: draft
references:
  - docs/SPECIFICATION.md (`project import` and the bulk-import path this PRD reuses; the contract this document must not contradict)
  - docs/prd/archive/orchestration.md (A2 — the prd-review / prd-decompose stories this PRD replaces; archived 2026-09-28)
  - docs/prd/archive/bulk-task-import.md (the indented-list importer P2 builds on)
  - docs/prd/archive/pr-reconciliation.md (S4 — "`blocks` edges would have prevented all of them"; P3 is that rule)
  - docs/prd/review-convergence.md (sibling — R5 reads the verification approach P1 adds to each slice)
  - docs/adr/0011-pr-maintenance-invariants.md (per-story PRs to `main`; no integration branch)
  - docs/adr/0012-admission-release-order.md (serial admission — what an ordering edge still has to add)
labels: [lithos-loom, orchestrator, planning]
---

# PRD to graph

> **Status (2026-09-29).** Drafted in the consolidation pass of 2026-09-28/29
> (the interim "accumulator" document, deleted once this PRD and its two
> siblings, [`review-convergence.md`](review-convergence.md) and
> [`unattended-duration.md`](unattended-duration.md), carried everything it
> held). First use: **lens T3's decomposition** (loom's October customer,
> decided 2026-09-29). Four stories; the existing tasks are the orchestration
> PRD's A2 tasks re-scoped (a7cee412 US20 → P1, 6981561c US22 → P2–P4).

## Summary

A PRD becomes loom work today by hand: the operator writes the slices into
Lithos, then tags them for dispatch in a second pass. Lens T2's seven tasks
were created that way on 2026-09-04 and tagged `trigger:story-develop` on
2026-09-13. The orchestration PRD planned two plugins for this (a PRD
reviewer and a decomposer that also cut an integration branch); the
reviewer's job turned out to be the step where the operator's judgement
matters most, and the integration branch is dead.

The proposal: PRD authoring and review stay interactive, as skills in the
operator's harness against the Lithos MCP, and loom's obligation shrinks to
two things — **documenting the slice-list contract** that workflow must
emit (P1), and **a writer that turns a reviewed slice list into the
dispatchable graph in one step** (P2), applying the one planning rule the
PR-maintenance series proved necessary (P3), and retiring the stub that
pointed at the old plan (P4).

## Evidence

- **Hand entry then a tagging pass.** Lens T2: created 09-04, re-tagged
  09-13 before dispatch. The September review names this "a contract gap
  that showed up as a tagging pass".
- **Slices with no edges land on each other.** Lens T1-S5/S9/S10/S12 were
  four slices of one dashboard with no edges; the ready queue offered all
  four; every one needed a hand to land. pr-reconciliation S4: "`blocks`
  edges would have prevented all of them." Serial admission (S6) now stops
  them *running* together; what the edge still encodes is *order*, and order
  is what stops the second story being cut from a `main` the first has not
  reached.
- **The decomposer never existed.** `plugins/prd_decompose/__main__.py`
  raises `NotImplementedError`; its `prompt.md` is a TODO. The A2 stories
  shipped nothing in three months while thirty other loom tasks closed.
- **Two review rounds with Dave.** The T2 PRD took two review passes with
  the operator in the loop; the September plan names the operator's
  attention as the binding constraint, and that is the step where it is best
  spent.

## Problem statement

1. Loom does not say what shape of slice list it accepts, so every PRD's
   decomposition is re-derived by hand and the graph is entered twice.
2. Nothing writes the dispatchable shape — epic, tasks, edges, project tag
   and metadata, trigger tag — in one step.
3. Nothing enforces the one ordering rule that experience proved: slices
   touching the same surface get a `blocks` edge in PRD order.
4. Dead scaffolding points at a superseded plan.

## Stories

1. **P1 — PRD review is a skill, and loom documents the contract it feeds**
   (task a7cee412). As the operator, I want PRD authoring and review to be
   an interactive workflow in my harness (`to-prd`, a PRD-review pass,
   `to-issues`) whose *output* — a slice list — is what loom consumes, so
   that my judgement stays in the loop at the point where it matters and
   loom carries no plugin whose only job is to wait for me. Ships: a workflow
   document in the loom repo (alongside `docs/macros/`, which already
   documents operator-side Obsidian workflows) stating the slice-list shape
   the decompose step accepts, and the review brief the pass runs with. The
   output schema in `plugins/prd_decompose/prompt.md` (title, ≥80-word brief,
   acceptance criteria, deps, files hint) is the starting point and moves
   into that document, extended with two fields per slice: a **verification
   approach** (which check, test, or probe proves each criterion — the input
   R5's AC-to-evidence checklist reads) and **provenance** (the PRD file and
   section the slice came from, written to the task as `metadata.prd` /
   `metadata.prd_section`, the same pattern as the `carried_to` /
   `carried_as` metadata the consolidation pass used), so that a story can
   be traced back to its requirement and forward to its proof without reading
   the run.
2. **P2 — Decompose writes the dispatchable graph** (task 6981561c). As the
   operator, I want a loom command that takes a reviewed slice list and
   writes, for one project, an `epic` plus one `task` per slice with
   `parent_task_id`, `blocks` edges from the declared deps, `project:<slug>`
   + `metadata.project`, the provenance metadata from P1, and the
   `trigger:story-develop` tag (or none, for projects loom must not
   auto-dispatch — loom's own stories deliberately omit it), with `--dry-run`
   rendering the tree and each task's readiness before anything is written,
   so that a decomposed PRD is a runnable pipeline in one step instead of hand
   entry followed by a separate tagging pass. Reuses `project import`'s bulk
   writer (`_project_import_bulk.create_tasks`), which already writes this
   shape from an indented list — but with a **graph preflight in front of
   it**, because the existing path is only safe for the shape it was built
   for: `task_graph.build_plan` derives dependencies solely from the
   `[sequential]` sibling chain, and `create_tasks` creates in document
   order and resolves each `depends_on` from tasks already created, which is
   correct only because that chain makes document order topological. A
   declared dependency on a later slice has no path through it, and a cycle
   caught at edge-write time leaves a half-written graph. So P2 (a) merges
   the declared dependencies with P3's inferred surface edges into one edge
   set, (b) rejects dangling references and cycles **before any write**, (c)
   sorts the slices topologically and creates them in that order with every
   blocker present in the creation call's `depends_on`, so no task is ever
   ready without its blockers, and (d) writes the trigger tag last, after
   every edge exists, as a second guard. `--dry-run` renders the result of
   (a)–(c). (PR #436 review, 2026-09-29.)
3. **P3 — Slices declare the surfaces they touch; overlap is serialised.**
   As the operator, I want each slice to name the surfaces (paths, modules,
   screens) it will change, and the writer to add a `blocks` edge, in PRD
   order, between any two slices whose surfaces overlap unless they already
   have one, refusing to write an edge-less overlapping pair without an
   explicit `--allow-parallel-surface`, so that the failure that produced the
   PR-maintenance series cannot be re-planned. The inferred edges are an
   **input to P2's preflight**, not a pass after creation: added afterwards
   they would leave trigger-tagged tasks ready before their blockers exist.
4. **P4 — Retire the `prd-decompose` stub.** As a maintainer, I want the
   `plugins/prd_decompose` package, its `prompt.md` and the reference in
   `plugins/__init__.py` removed in the same change that lands P2, so that
   there is no dead scaffolding pointing at a superseded PRD — the same
   subtraction US2 did for `story-implement` and `story-review-human`.

## Decisions

1. **PRD authoring and review are interactive, not loom plugins**
   (2026-09-28). The T2 PRD took two review rounds with Dave in the loop;
   that is the step where his judgement matters most, so it stays interactive.
   A2's prd-generate (US19) is not carried — PRDs are hand-written with the
   Pocock skills, as the MVP PRD said and T2 was. The prd-review-human gate
   (US21) and auto-retag on approval (US23) are not carried: approval *is*
   running the decompose step; generic `human` gates exist anyway.
2. **`lithos-coding-mcp` is not required** (2026-09-28). The repo is a May
   skeleton with no further commits. The operator surface it was to provide
   is the loom CLI; the agent-facing surface on the host is the Lithos MCP
   itself. Inside the story-develop container the coder gets ADRs and context
   from the tree and discovered work leaves through the reviewer handoff.
   Whether a sandboxed agent should ever reach Lithos directly is open
   question 1, not a story.
3. **The integration-branch delivery model is dead** (2026-09-28). Since
   August every story ships as its own PR to `main` under a human `pr` gate
   ([ADR 0011](../adr/0011-pr-maintenance-invariants.md)); A2's decompose
   story loses its `loom/<prd-slug>` branch and A8's merge-stories step is
   not carried.
4. **Replanning stays interactive** (2026-09-29, from the LRA comparison).
   T2's seven-slice graph never needed revising; replanning happens over
   longer horizons and is done the way this PRD was produced. No
   evidence-driven replanner is carried.
5. **Knowledge between stories is a single candidate, not a story here**
   (2026-09-29). K1 (task 9e8d4b80): story completion writes an outcome built
   from the final handoff, with the project-context doc as the retrieval
   side, and its acceptance carries its own measurement (after two T3
   slices, did the second cite the first's record?). Loom writes no
   completion record today — the route runner completes a task with no
   outcome — but no story has yet been shown to re-investigate settled
   ground, so K1 waits for that evidence. Lesson and procedure tiers, a
   separate memory store and automatic skill promotion are not carried.

## Sequencing

P1 first, because T3's PRD is written through it; P2, P3 and P4 in one
change — P3's edges feed P2's preflight, so they cannot land separately
without reintroducing the ready-before-blocked window. The whole milestone
precedes T3's first dispatch. Success: T3's graph is produced by
P2 from P1's slice list, with P3's edges, and no tagging pass follows.

## Non-goals

- Generating PRDs (A2 prd-generate).
- An integration branch or a merge-stories step.
- Replanning a running epic.
- A plugin SDK, bash runner or events journal (A1): only two plugins exist
  and this PRD removes the third.

## Open questions

1. **Should a sandboxed agent ever reach Lithos directly?** Today it cannot
   (nothing mounts an MCP into the container). The coder gets ADRs from the
   tree and discovered work leaves via the handoff; nothing in the T2 rollout
   was lost for want of it. Task 0b5ca206 (#92, capability profiles) is the
   place to decide; it stays a candidate, with #138 (reviewer cross-file
   context) blocked on it by edge.
2. **New subcommand or an adapter on `project import`?** The bulk-import
   path already writes the shape P2 needs from an indented Markdown list
   with `[sequential]` markers; the slice list from P1 is richer (deps,
   surfaces, verification, provenance). Decide at slicing time; the contract
   is the same either way.
3. **Where does the review brief live?** Loom already ships operator-side
   workflow docs under `docs/macros/`; the alternative is the operator's
   harness only. Proposal: the contract and brief in the loom repo (so a
   consuming project can find them), the skill invocation in the harness.

## Provenance

| Story | From | Carried on |
|---|---|---|
| P1 | orchestration.md A2 US20 (prd-review-agent), as a skill | 2026-09-28; schema fields added 2026-09-29 |
| P2, P4 | orchestration.md A2 US22 (prd-decompose), minus the integration branch | 2026-09-28 |
| P3 | pr-reconciliation.md S4's `blocks`-edge rule | 2026-09-28 |
| K1 (candidate) | the LRA comparison, addition 2 | 2026-09-29 |
