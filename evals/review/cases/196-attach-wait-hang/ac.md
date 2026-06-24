# `develop attach --wait` must not hang forever

## Problem

`develop attach --wait <task-id>` is meant to be run right after dispatching a task
— before the route-runner has created the run dir — and block quietly until the run
reaches a terminal state. But a run can **complete without any observable run dir**:

- an **idempotency replay** writes `result.json` and exits *before* `run_dir.mkdir`,
  so no run dir is ever created;
- a **fast success** is reaped by the route-runner (`shutil.rmtree(work_dir)`)
  between two 2-second polls, so the dir is never seen.

If the wait loop only watches for the run dir to appear, it loops **forever** in
both cases.

## Required behaviour

- `attach --wait` must terminate when the run has already completed even if no run
  dir is observable — the durable **completion store** (the idempotency record the
  route-runner never removes, keyed by the idempotency key = task id by default) is
  the signal. On a completed record, recover the outcome and report it.
- It must still block normally while the run is genuinely pending (the dir simply
  hasn't been seeded yet).

There must be **no path** where `attach --wait <task-id>` loops forever for a run
that has already finished.
