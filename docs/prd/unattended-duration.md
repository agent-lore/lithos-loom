---
title: Lithos Loom — Unattended duration
milestone: M-UD
status: draft
references:
  - docs/SPECIFICATION.md §5.5 and the route-runner section (usage-limit re-dispatch, failure reaction table, serial admission; the contract this document must not contradict)
  - docs/prd/archive/pr-reconciliation.md (open question 4 — does the sweep defer on a usage limit; S5b's remediation budget and boot hold)
  - docs/prd/archive/orchestration.md (XC US39 — the ops surfaces this PRD keeps the residue of)
  - docs/prd/review-convergence.md (sibling — M1's agreement rate is what the merge-policy dial waits for; this PRD is what the daemon needs before the dial is safe)
  - docs/adr/0009-converge-pr-loop.md (the paid loop a resume re-enters)
  - docs/adr/0012-admission-release-order.md (serial admission — the cap that actually binds)
  - operator note `20260926-lra-vs-loom.md` (Obsidian, not in this repo — additions 3 and 4, processed 2026-09-29)
labels: [lithos-loom, orchestrator, route-runner, ops, planning]
---

# Unattended duration

> **Status (2026-09-29).** Drafted in the consolidation pass of 2026-09-28/29
> (the interim "accumulator" document, deleted once this PRD and its two
> siblings, [`review-convergence.md`](review-convergence.md) and
> [`prd-to-graph.md`](prd-to-graph.md), carried everything it held). Scoped
> from a cluster of seven open issues plus the ops residue of the
> orchestration PRD; scheduled by the October plan, not by this document.
> Every slice has a Lithos task tagged `prd:unattended-duration`.

## Summary

Loom recovers well from the failures it was built around: checkpoints
preserve committed work and reviewed handoffs, successful results are
idempotent, bootstrap reconciles open tasks, PR recovery adopts delivered
branches. What it does not yet survive is *time*. The one schedule it keeps
between runs — when to resume after a provider usage limit — lives in the
route runner's memory, and its own docstring says so: "the schedule is
in-memory only (a daemon restart re-bootstraps open tasks anyway)". A
restart during that wait re-dispatches straight back into the wall. A
provider capacity refusal retries the same model and, in remediation, holds
the PR until the next daemon boot. The auth file mounted into a container
cannot be refreshed inside a long run. Four of the six T2-era interventions
were host restarts under a run.

The proposal is the narrow durable-execution principle without a new
service: persist the next eligible attempt, the attempts consumed and the
last confirmed phase **on the Lithos task**, reconcile against Lithos and
GitHub at boot before continuing, and let known-transient failures retry
on schedule without a restart — while authentication and product decisions
keep their escalation paths. Around it, the operations residue that makes a
restart a non-event: a `systemd --user` unit that drains on stop, the
per-story dials that are silently ignored today, a bus drop that is
visible, and a reserve on the resource that actually binds, which is
subscription share rather than dollars.

This PRD is what the daemon needs **before** the merge-policy dial in the
sibling PRD is safe to leave running above `human`.

## Evidence

| Observation | Where |
|---|---|
| Usage-limit resume schedule is in-memory; a restart re-bootstraps into the wall | `subscriptions/route_runner.py` docstring lines 18–23; `_resume_tasks` / `_resume_counts` are dicts on the runner (:266–267); no boot re-scan |
| A capacity refusal retries the same model and boot-holds the PR | #420 (task fe400fb5): #419 shipped classification only (5ca858df); `panel.py:527` falls back only on `kind == "pause"`; `external_remediation.py:833` adds every `infra_failed` to `_infra_held` |
| The auth bind mount cannot refresh in-container | #406 (task ea12efe9): `containers.py:137–138` single-file mounts; only the #403 resync stopgap landed |
| Codex signal fidelity: a recovered "Reconnecting… n/5" fails a completed turn; the usage-limit classification is text-based over a synthetic fixture | #411 (5061554c): `engines.py:440–450`; #103 (795961b1) Part B |
| Task-level `develop_max_rounds` / `develop_max_cost_usd` silently ignored; no friction posted | #350 (704e08d1): `settings_resolver.py:271–293` reads the project doc only |
| A dropped bus event is logged at DEBUG only; `drop_count` read nowhere | #365 (ef8cdee5): `bus.py:196–202` |
| The dollar ceiling cannot meter codex; the binding resource is subscription share | #102 (874949f2): `develop.py:336–370` warns once; codex `cost_usd=0.0` |
| Four of six T2-era hands were host restarts under a run | `plans/2026-09-review.md` §2; the restart family (#407 `drain`, #412, #415–#419) removed the largest cause, not the class |

## Problem statement

1. **A scheduled wait does not survive a restart**, so the cheapest recovery
   (wait it out) is the one loom forgets.
2. **Self-clearing failures are treated as infrastructure death**: a capacity
   refusal boot-holds a PR that would have succeeded on the next model or the
   next minute.
3. **Signals from one engine are misread**: recovered errors fail completed
   turns; the usage-limit boundary was never captured from a real event.
4. **Per-story bounds do not bind** and their being ignored is silent.
5. **Operations are by hand**: the daemon is started and drained manually,
   and an overloaded queue loses events without a trace.
6. **The resource that binds is watched, not enforced**: subscription share
   consumed by autonomous work is discovered when the interactive session
   hits the wall.

## Stories

1. **U1 — A scheduled resume survives a daemon restart** (task 250d231f,
   P6a). As the operator, I want a usage-limit resume (`resume_after`,
   attempts used) persisted on the task rather than held in the route
   runner's memory, and boot to honour it — wait out the remaining window,
   keep the attempt count — instead of re-bootstrapping the open task straight
   back into the usage wall, so that a host restart under a run stops being
   an intervention class and what loom will do next after a restart can be
   read from the task. Business state stays in Lithos; the execution record
   is the task's metadata (`resume` block: resume_after, attempts, last
   phase, operation id), not a second journal that could disagree with the
   graph. Acceptance: restart the daemon during a usage-limit wait → the task
   is re-dispatched at the original `resume_after`, attempt count preserved,
   no dispatch before it.
2. **U2 — A capacity refusal tries the fallback chain and does not hold**
   (task fe400fb5, #420; absorbs #155). As the operator, I want a provider
   capacity refusal to try the project's `develop_fallback_chain` instead of
   retrying the same model, and remediation not to boot-hold a PR on a
   condition that clears by itself, so that a refusal costs one engine
   switch, not a daemon restart. An all-Claude panel (#155) is one instance
   of the chain, not a separate variant.
3. **U3 — The auth mount can refresh inside a long run** (task ea12efe9,
   #406). As the operator, I want the sandbox auth mount shaped so that a
   token rotation during a run reaches the container (keep-alive or per-run
   copy, decided after verifying the CLI's write mode and rotation), so that
   a run longer than a token's life does not fail on auth and escalate as if
   the credential were gone.
4. **U4 — Engine signals are read faithfully** (tasks 5061554c #411,
   795961b1 #103 Part B). As the operator, I want a codex turn that completed
   after recovered "Reconnecting" errors to count as completed, and the
   usage-limit boundary classified from a structured error type captured
   from a real event rather than a regex over a synthetic fixture, so that a
   coder turn is not retried at full cost for a transient that already
   healed and the resume window is set from what the provider said.
5. **U5 — Per-story dials bind, and an ignored dial says so** (task
   704e08d1, #350). As the operator, I want `develop_max_rounds` and
   `develop_max_cost_usd` resolved project-then-task like every other scalar,
   and a `[Friction]` posted when a task carries a dial the run cannot
   honour, so that the per-story half of the reserve (U8) exists and silence
   is never the outcome.
6. **U6 — A dropped event is visible** (task ef8cdee5, #365). As the
   operator, I want a WARNING on the first drop per subscription and then
   rate-limited, naming the subscription, queue size and dropped event, with
   the counters in `lithos-loom doctor` or the supervisor's status line, so
   that an overloaded route queue is diagnosed from the log rather than from
   a nudge that never arrived.
7. **U7 — The daemon is a service** (task 3ec94510, the residue of XC US39).
   As the operator, I want a `systemd --user` unit that runs the daemon and
   stops it through `lithos-loom drain`, so that a host restart is a drain
   and a boot, not an intervention. Lens is the dashboard; `develop list` /
   `develop attach` are the replay; OTel has no consumer — none of the rest
   of US39 is carried.
8. **U8 — A reserve on the resource that binds** (design: task 874949f2,
   #102; acceptance: task 2bf0bb2b). As the operator, I want a usage-share
   reserve so autonomous work cannot consume the subscription allowance I am
   using interactively, with the reconcile sweep deferring cleanly on a
   usage limit instead of retrying into the wall (pr-reconciliation open
   question 4), so that the resource that actually binds is enforced rather
   than watched. Dollar metering is not the instrument: codex is subscription
   too and the ceiling cannot see it (#102's finding); the reserve is
   time- and round-shaped unless the CLIs expose a share signal before the
   limit event.

## Decisions

1. **Loom's own project stays at `max_open_delivered_prs = 1`**
   (2026-09-29). The six-hour hold on 2026-09-25 was caused by #431's review,
   not by admission; a higher cap would have let a second story open a PR on
   a base the operator was still fixing by hand.
2. **`orchestrator.max_concurrency` is removed, not enforced** (2026-09-29,
   0a7b8dfd). Concurrency is the number of loom workers and has not been an
   issue; serial admission is the cap that binds.
3. **No separate perpetual-daemon PRD** (2026-09-29). This PRD and the
   merge-policy dial recorded in the sibling are its two halves; the dial is
   delivered only after the checkpoint that reads M1 over ≥7 closed T3
   gates (3398a388; its design may start earlier), and this PRD is sequenced
   ahead of it
   because a daemon that forgets its schedule on restart is not one to leave
   merging.
4. **Durable records, no new service** (2026-09-29, from the LRA
   comparison). Business state stays in Lithos; execution records are narrow
   and live on the task. A Temporal migration is not carried; reconsider only
   if distributed execution or recovery complexity justifies another service.
   Preflight budget reservation is deferred: subscription usage does not map
   to marginal dollars, and hard round and time limits stay whatever the
   estimates do.

## Sequencing

Scheduled by the October plan. Within the milestone: U1 and U2 first (the
two restart-shaped failures with evidence); U7 with them if restarts return
as an intervention class before then; U4 and U3 as their triggers recur;
U5 and U6 are small and can ride with any of the above; U8 last, once U5
gives it a per-story half. Success: a host restart during a T3 run costs no
operator action and no re-entry into a usage wall; no PR is boot-held on a
self-clearing condition.

## Non-goals

- The merge-policy dial and any auto-merge (sibling PRD, after M1).
- Dollar-accurate cost accounting across engines.
- A second execution journal outside Lithos.
- Multi-host or webhook delivery (orchestration A7): one host; polling stays.

## Open questions

1. Do the agent CLIs expose any usage-share signal before the limit event
   fires? If not, U8's reserve can only be time- or round-based.
2. Does U1's `resume` block generalise to the external-remediation boot hold
   (#377's `_infra_held`), or does that stay a per-boot set with U2 carving
   out the self-clearing conditions?

## Provenance

| Story | From | Carried on |
|---|---|---|
| U1 | the LRA comparison, addition 3 (durable retry schedules) | 2026-09-29 |
| U2–U6 | open issues #420, #406, #411, #103, #350, #365, verified against 36a4d140 | 2026-09-29 |
| U7 | orchestration.md XC US39, residue | 2026-09-28 |
| U8 | orchestration.md XC (usage-share reserve), #102, pr-reconciliation open question 4 | 2026-09-28; reframed 2026-09-29 |
