# `develop prune` must not reap an in-flight run's dir

## Problem

`develop prune` removes the on-disk run-state dirs of **finished** story-develop
runs. It must decide "finished" from a signal that a run still in its **startup
window** cannot accidentally match.

Agent containers run with `--rm` (`containers.py`), so a torn-down run leaves no
container behind — but so does a run that has only just been seeded: the
route-runner creates the run dir and its `handoff/` subdir **before** the first
agent container starts. During that startup window the run has zero containers,
identical to a finished run.

If `prune` decides "finished" from container liveness **alone**, it deletes a
**concurrent, just-started run of the same task** out from under the live daemon —
data loss on an active run.

## Required behaviour

- `prune` must treat a run as finished only on a **durable terminal marker** the
  plugin writes at run end (`conversation.md`, written only after the agent
  containers stop) — not on container state alone.
- A run still in its startup window (handoff dir seeded, containers not yet
  started) must be left untouched, even though it reports zero containers.
- A still-running agent container remains a definitive "not finished" override.

There must be **no path** where `prune` removes the run dir of a run that has not
written its terminal marker.
