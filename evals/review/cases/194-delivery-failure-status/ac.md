# A failed PR delivery on an approved run must not be recorded as `succeeded`

## Problem

In daemon mode, after the dialogue approves, `story-develop` delivers the PR
host-side (push the branch, open the PR, an optional Copilot round). If delivery
**fails before a PR exists** — `push_branch()` or `gh pr create` raises — the run
produced no PR. `build_result_payload` must not record such a run as a clean
success.

## Required behaviour

- An approved run whose PR delivery **failed** maps to `status: "failed"` with an
  `error.category: "delivery"` carrying the reason — **not** `succeeded`. No PR
  exists, so the task stays open / retriable and is not recorded under the
  idempotency key.
- Deriving `status` from `result.approved` **alone** (ignoring whether delivery
  actually succeeded) recreates the #171 false-done window: the operator sees the
  run as done with no PR and no failure reason, and `develop attach --wait` exits 0
  reporting an "approved" success for a PR that never opened.

The recorded status must reflect the **delivery outcome**, not just the dialogue
verdict.
