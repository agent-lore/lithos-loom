# ADR 0012 — Serial-admission release order: priority, then first-held order; the choice lives in `Admission`

- **Status:** Accepted
- **Date:** 2026-09-14
- **Deciders:** Dave Snowdon
- **Task:** Lithos `561db86a` (the decision); implementation in the same PR

## Context

Serial admission (PRD [pr-reconciliation](../prd/pr-reconciliation.md) S6,
`subscriptions/admission.py`) bounds a project's delivered-but-unmerged PRs.
It did not decide **which** held story takes the slot when one frees. Two
mechanisms settled it, neither a decision anyone made:

1. **UUID sort.** The waker released `sorted(waiting)` over a `set` of
   `(route, story)` pairs — nothing about the inputs survived but the id
   string.
2. **A race with the re-check sleeper.** A refused story also armed the
   readiness re-check sleeper (60 s doubling to 900 s, never gives up). The
   waker and the sleeper publish the same synthetic `lithos.task.updated`;
   each runner drains serially and `Admission.admit` reads → decides →
   reserves under a per-bucket lock, so the first event to arrive took the
   slot. The order was not merely arbitrary; it was non-deterministic.

Observed 2026-09-12 on `lithos-lens`: two held stories, one slot. `02d9019a`
(a self-healing empty-state bug) sorted before `aa1dcd84` (T2-A6, the head
of the milestone's critical path, held six hours earlier), so the critical
path queued behind the bug for a full develop-review-merge cycle.

The task listed six candidates: (a) FIFO by defer time, (b) blocking impact
/ critical path, (c) `metadata.priority`, (d) task age, (e) an explicit
operator rank, (f) keep it arbitrary but deterministic.

## Decision

**FIFO by the time a story was first held, except that a story whose
`metadata.priority` is above the default leaves first** — (a) with (c) as
the override. In full:

1. **Key: `metadata.priority`, descending.** The Lithos vocabulary is the
   Obsidian Tasks scale already rendered and pushed by the bridge
   (`task_line.PRIORITY_EMOJI`): `highest` › `high` › `medium` › *none* ›
   `low` › `lowest`. The default — no priority set — sits in its Obsidian
   place between `low` and `medium`, so `medium` and above jump the queue
   and `low` / `lowest` yield to unmarked work. An absent, non-string or
   unknown value is the default. No new metadata key: the operator rank
   candidate (e) is this field, which already has three write surfaces
   (Obsidian `🔼 ⏫ 🔺`, lens, `lithos_task_update`).
2. **Tie-break: the order the stories first asked admission in this
   process.** A held story whose sleeper re-asks and is refused again keeps
   its place; so does a story whose run ended without a gate (a usage-limit
   pause, a failed run the operator retries) and asks again — it asked
   first, it is not a newcomer behind everything that queued while it ran;
   a newcomer that arrives while others wait joins the back. The place is
   the story's for as long as it is open: a wait dropped because the
   story could not be placed or matched (below) comes back to the same
   place when the story asks again — so a Lithos blip that drops every
   wait does not reorder the queue (review round 3).
3. **Read at release, not remembered from the refusal.** Each pick reads
   every held story once (`task_get`, the read the waker already made) and
   ranks on the live value. Raising a waiting story's priority is the
   operator's lever; it takes effect at the next slot.
4. **Starvation is accepted and stated.** A higher-priority story always
   leaves first, so a bucket fed an unbounded supply of high-priority work
   never releases its unmarked stories. The operator set that priority, the
   bucket's queue is small and legible, and within one rank the first-held
   order is starvation-free. No age escalation is layered on: it would make
   the release order depend on a clock, which is exactly the illegibility
   this ADR removes.
5. **The race is closed by moving the choice into `Admission`**, not by
   ordering the callers. When slots are free and more stories want them
   than there are slots, `admit` computes the bucket's release order under
   the bucket lock; an asker outside the first *free* places is refused
   with reason `queued` and those places are nudged with the waker's own
   synthetic event (`origin="admission-recheck"`); an asker inside them is
   admitted and the places left are nudged. With `max_open_delivered_prs`
   above one (or unlimited under the total cap) every free slot is filled,
   not one. The order is over `(route, story)` waits, so a story two
   PR-producing routes hold is two runs and two slots, and the story
   behind it is entitled only to a third. The waker's sweep is now
   `Admission.wake`, which counts the bucket's headroom first and
   republishes only the held stories entitled to it, in release order;
   every nudge re-enters `admit`, which enforces the same order at the
   ask. Same inputs, same
   choice, whichever producer publishes first. The re-check sleeper keeps
   its never-gives-up property untouched: its re-ask is simply one more
   ask, and a `queued` refusal arms it like any other.
6. **Every transition that can free a place re-nudges.** A gate event (the
   waker), an admission that leaves slots free, a run that ends without a
   gate (`release` — interrupted, failed, claim lost: there is no gate
   event for the waker to see), and a head that leaves the queue
   (`forget`) each republish the bucket's entitled stories in order —
   after counting the headroom, so a delivered run's release (its slot is
   now its gate's) or a head leaving a full bucket nudges nobody. Without
   this the stories behind a departed head would wait for their own
   sleepers, up to 15 minutes, with the slot idle.
7. **The queue tracks dispatchability — a head that never asks is the one
   way this design fails.** Before this ADR a stuck deferral was inert;
   the head rule turns one into a project-wide stop (a stall, never a
   spin: every nudge path terminates). So every way a held story can stop
   asking steps its wait aside — keeping its place — and it re-joins
   where it was when it asks again: the runner's "not on the ready
   frontier" (a `blocks` edge added while it waited, a gate raised on it)
   **and** an undetermined or unreadable readiness (unknown is not "not
   ready", but a head that cannot answer holds the bucket just the same)
   both call `Admission.forget` for that route's wait, which wakes the
   rest; a wait whose story **no longer carries its route's match tags**
   (re-tagged to park it — a routine gesture — or onto another route: that
   runner would never receive the nudge, and another route matching is no
   help) is dropped at the next pick, the check per route from the tags
   each ask reports; a story held in one bucket and admitted under another
   leaves no wait behind (a phantom head that never asks there and, once
   processed, can never be forgotten). What remains — a matched, ready
   head whose runner never asks — is named in the log every tenth nudge it
   does not answer.
8. **Fail closed on the reads, per story.** A held story that cannot be
   read — for any reason, the raw transport errors the client re-raises
   once its reconnects are spent included — keeps its place at the rank it
   was **last seen** (the default if never) and is not nudged; its own
   sleeper re-asks. Ranking it at the default instead would move a
   high-priority head behind the unmarked story on every failed read, and
   each swap nudges the other with the slot idle. One story's failed read
   never loses the rest of the sweep. A story Lithos no longer returns, or
   that is no longer open, is forgotten; the asker itself is never dropped
   — whether it is still open is the claim's to tell.

Not chosen: (b) critical path — a graph read per held story per release,
and lens already computes and displays the chain; the operator can read it
there and set the priority. Revisit only if `priority` stays hollow. (d)
task age — a backlog-clearing order, not a delivery order. (f) is the floor
this decision also meets.

## Consequences

- **Operator-facing:** to move a held story ahead, set its priority. The
  `lithos-loom` log names the head a story is queued behind, and warns
  when a story nudged as next ten times has not asked; the verdict reason
  `queued` joins `limit` / `total_cap` / `unreadable`. No new finding
  prefix and no new config.
- **Cost:** one `task_get` per held story per pick. A slot-free event with
  N held stories costs O(N²) reads (the wake's order plus each nudged
  story's own pick) and re-enters the head's runner O(N) times; N is
  single digits by construction — the caps that hold them are 1 and 3.
  The reads run under the bucket lock, as the gate reads always have, so a
  slow Lithos holds that bucket's askers (and, since each runner drains
  serially, that runner's queue) for the length of the reads.
- **State:** still in-process. A restart's bootstrap replays every open
  story in Lithos's order and each is admitted or held afresh; the
  first-asked order restarts with the process. Priority survives the
  restart because it is the task's own field. The first-asked record is
  kept for every story that ever asked, for as long as the story is open
  — released when a pick finds it terminal or gone.
- `AdmissionWaker` no longer reads Lithos and moved to its own module
  (`subscriptions/admission_waker.py`); the Lithos reads — the dials, open
  gates, terminal-story gates, escalated gates — moved to
  `subscriptions/admission_count.py`. Both for size: the decision stays in
  `admission.py`. `Admission` takes the bus.
