# `develop attach` must follow to terminal state, not agent liveness

## Problem

`lithos-loom develop attach` is the operator's "watch a live run" surface, but it
can report a run as **finished while it is still working** — and when it stops it
gives **no outcome**, so an operator can't tell whether the run approved, failed,
or is mid-delivery.

**Motivating bug (observed on a live run):** `state.json` showed
`status: approved` at round 3 while the plugin kept running **host-side** to push
the branch and open the PR — with **no agent container active** during that
PR-delivery phase. An operator attached at that moment sees "no active agent →
done" and exits **before the PR even exists**.

Root cause: **end-detection is gated on instantaneous agent liveness (or on a
bare `approved` verdict), not on the run's real terminal state.**

## Required behaviour

1. **Gate the follow loop on terminal state, not liveness.** Stop following only
   when a real terminal signal is present — `result.json` written (a `succeeded`
   status), the run dir reaped on success, or a recorded terminal outcome. An
   **approved verdict is NOT yet terminal in daemon mode**: PR delivery (branch
   push, Copilot round, `result.json` write) runs *after* the dialogue approves,
   so attach must keep following through that window rather than exiting on the
   approved state.

2. **Show a distinct "delivering PR…" phase after approval**, and only treat the
   run as done once delivery has actually completed.

3. **Print a real terminal summary on exit** (verdict, rounds, PR / failure
   reason) so there is no "did it finish, and how?" ambiguity.

The change must make the false-done window **impossible**: there must be no path
where `attach` (especially `attach --wait`) exits on the `approved` state before
the PR delivery has completed.
