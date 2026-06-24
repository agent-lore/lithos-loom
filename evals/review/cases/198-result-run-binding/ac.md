# `develop attach` must bind result.json terminal detection to the run

## Problem

`develop attach` decides an approved run's terminal state by reading `result.json`,
which lives in the **shared** per-task dir (`<work_dir>/<task_id>/result.json`) — so
a **prior** run of the same task can leave one behind. The old reasoning ("a
succeeded run's whole work dir is reaped, so a surviving `succeeded` result.json
must be the current run's") relies on the reap, which is **best-effort**:
`_cleanup_work_dir` suppresses `rmtree` `OSError`. A failed reap leaves a stale
`succeeded` result.json, and the **next** run of the same task reads it and is
reported as already-delivered before its own delivery has finished — the #171
false-done window, on a retry.

## Required behaviour

- `attach` must bind the shared `result.json` to **this** run before trusting it:
  the file is this run's only when its `run_id` equals the run's id
  (`run_dir.name`). A prior run's leftover (a different `run_id`) is ignored.
- This removes the dependency on the best-effort reap: a stale `succeeded` (or
  `failed`) result.json from an earlier run can never be mistaken for the current
  run's delivery.
