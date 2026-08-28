"""Persistent dispatch guards for the route-runner.

Two checks decide whether an event may dispatch, both reading state that
survives a daemon restart (unlike the in-process ``_processed_tasks`` dedup):
Lithos's ready frontier (`on_ready_frontier`), and the failed-attempt marker
below.

Failed-attempt markers (6c4423a0):

A story whose plugin run FAILED stays open and unblocked on Lithos's ready
frontier, so without a persistent trace every daemon restart's bootstrap
replay would re-dispatch it at full cost (T1-S7 was developed three times
for $182.52 this way, the third run overrunning completed out-of-band
remediation). The runner therefore records each failure as
``metadata.loom_last_attempt:<route>`` on the task and declines a
**bootstrap-origin** event whose marker records a failed last attempt for
the same route — a restart is a process lifecycle event, not anyone asking
for another run.

One metadata key PER ROUTE (Lithos ``task_update`` merges per top-level
key), so two routes failing on the same task each keep their own guard —
a single shared key would let route B's write erase route A's protection.

Failure must stay retryable. Two retry gestures are guaranteed across a
restart, and the operator contract is exactly these:

1. **Delete the marker key** (``metadata.loom_last_attempt:<route>`` →
   null) — the canonical, always-works signal: no marker, no decline.
2. **Edit the task to a NEW state.** The marker stores a
   ``task_fingerprint`` of the title/description/tags the failed run saw,
   and the decline applies only while that fingerprint still matches — so
   an operator who sharpened the task while the daemon was down (or whose
   same-process edit was absorbed by the in-process dedup, issue #11) gets
   the retry on the next restart, exactly the pre-guard operational flow
   minus the unconditional replay.

Two edits the fingerprint deliberately CANNOT see — for these, delete the
marker (gesture 1):

- **Metadata-only edits** (e.g. pointing ``develop_image`` at a fixed
  image). Metadata is excluded because the failed run itself writes
  metadata before the marker lands (``develop_*``, and the marker), so
  including it would make every decline fail open, and a plugin-agnostic
  runner cannot tell operator inputs from plugin outputs by key. An exact
  guard needs Lithos to expose ``updated_at`` on tasks (follow-up #339).
- **Reverted edits** (removing and re-adding the same trigger tag
  restores the original fingerprint). No state fingerprint can detect a
  revert; the explicit gesture exists for precisely this.

A live ``lithos.task.updated`` reaching a runner that has not suppressed
the task in-process, and the T10 resume re-dispatch, bypass the decline by
origin. A later gated delivery clears the marker
(``route_runner._gate_and_release``), and an ``interrupted`` run clears it
too — interrupted's designed recovery IS the restart bootstrap, which a
stale failure marker must not veto.

Reserved namespace: plugins see ``loom_last_attempt:*`` in their
``task.json`` metadata and must not repurpose it (SPECIFICATION §2.2).
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Protocol

__all__ = [
    "LAST_ATTEMPT_KEY_PREFIX",
    "READY_QUERY_LIMIT",
    "clear_superseded_failure",
    "failed_attempt_for_route",
    "last_attempt_key",
    "on_ready_frontier",
    "record_failed_attempt",
    "task_fingerprint",
]

logger = logging.getLogger(__name__)

# Per-route task-metadata key prefix; the full key is
# ``loom_last_attempt:<route>`` and its value is
# {"status", "ended_at", "task_fingerprint", "run_id"?}.
LAST_ATTEMPT_KEY_PREFIX = "loom_last_attempt:"


def last_attempt_key(route: str) -> str:
    """The task-metadata key holding ``route``'s last failed attempt."""
    return f"{LAST_ATTEMPT_KEY_PREFIX}{route}"


def task_fingerprint(payload: Mapping[str, Any]) -> str:
    """Fingerprint of the operator-shaped task fields (title, description,
    tags — order-insensitive).

    Stored in the failure marker and compared at bootstrap: a differing
    fingerprint means the task was edited since the failure, which is the
    operator's deliberate-retry gesture, so the decline does not apply.
    Metadata is deliberately excluded — plugins write metadata at will
    (``develop_*``, and the marker itself), and none of that is an operator
    asking for another run. The cost of that exclusion, and of state
    comparison generally (metadata-only edits and reverted edits are
    invisible), is documented in the module docstring: the marker-deletion
    gesture is the contract for those.
    """
    material = json.dumps(
        {
            "title": payload.get("title") or "",
            "description": payload.get("description") or "",
            "tags": sorted(str(t) for t in (payload.get("tags") or ())),
        },
        sort_keys=True,
    )
    return hashlib.sha256(material.encode()).hexdigest()[:16]


# Page size for the `task_ready` membership query (US4). `task_ready` has no
# per-task filter — the runner asks for the frontier and looks for its task on
# it — so the page must be big enough to hold a realistic frontier. The query
# is already narrowed to one route's tags and one project, but that can still
# be large: a decomposed PRD's parallel stories all carry the same trigger tag
# and all become ready at once. The default limit of 50 would quietly truncate
# such a frontier, so ask for far more and treat a full page as undetermined
# rather than as "not ready" (see `on_ready_frontier`).
READY_QUERY_LIMIT = 500


class _GuardClient(Protocol):
    async def task_update(
        self,
        *,
        task_id: str,
        agent: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Any: ...

    async def task_ready(
        self,
        *,
        project: str | None = None,
        tags: list[str] | None = None,
        metadata_match: dict[str, Any] | None = None,
        limit: int = 50,
        with_claims: bool = True,
    ) -> Any: ...


async def on_ready_frontier(
    lithos: _GuardClient,
    *,
    task_id: str,
    tags: tuple[str, ...],
    metadata: Mapping[str, Any],
    route: str,
) -> bool:
    """Is ``task_id`` on Lithos's ready frontier for this route? (US4)

    Readiness — every ``blocks`` predecessor completed, no unmet gate, no
    cycle — is computed once, server-side, and shared with every other
    agent. The runner no longer mirrors it from ``metadata.depends_on``.

    ``task_ready`` has no per-task filter, so this is a membership test
    over a frontier narrowed to the route's tags and (when the task
    declares one) its project. A *full* page means the frontier was
    truncated, which makes absence from it meaningless — so that case is
    reported as not-ready-yet rather than trusted, and logged. Deferring
    is the safe direction: the inverse mistake would dispatch a task whose
    blocker is still open, which is exactly what this gate exists to stop.
    """
    project = metadata.get("project")
    ready = await lithos.task_ready(
        tags=list(tags),
        project=project if isinstance(project, str) else None,
        limit=READY_QUERY_LIMIT,
        # Claims never exclude a task from the frontier (collision-safety
        # comes from the runner's atomic claim), so don't pay to fetch them.
        with_claims=False,
    )
    if any(task.id == task_id for task in ready):
        return True
    if len(ready) >= READY_QUERY_LIMIT:
        logger.warning(
            "RouteRunner %s: ready frontier for tags %s hit the %d-task query "
            "limit, so %s's readiness is undetermined — deferring. Raise "
            "READY_QUERY_LIMIT if a frontier this wide is expected.",
            route,
            list(tags),
            READY_QUERY_LIMIT,
            task_id,
        )
    return False


def failed_attempt_for_route(
    metadata: Mapping[str, Any], route: str
) -> Mapping[str, Any] | None:
    """The task's last-attempt marker, iff it records a FAILURE for ``route``."""
    last = metadata.get(last_attempt_key(route))
    if isinstance(last, Mapping) and last.get("status") == "failed":
        return last
    return None


def declines_bootstrap_replay(
    metadata: Mapping[str, Any],
    route: str,
    payload: Mapping[str, Any],
    *,
    task_id: str,
) -> bool:
    """True iff a bootstrap replay of this payload must be declined (logged).

    Declines when ``route``'s last attempt failed AND the task is unchanged
    since that failure (fingerprint match — a marker without a fingerprint,
    e.g. from a partial write, is treated as unchanged: fail closed). An
    edited task dispatches: the edit is the deliberate-retry gesture.
    """
    last = failed_attempt_for_route(metadata, route)
    if last is None:
        return False
    recorded = last.get("task_fingerprint")
    if recorded is not None and recorded != task_fingerprint(payload):
        return False
    logger.info(
        "RouteRunner %s: declining bootstrap replay of %s — last attempt "
        "failed (%s) and the task's title/description/tags are unchanged "
        "since. Delete metadata.%s to retry (always works — metadata-only "
        "or reverted edits are not detected), or edit the task to a new "
        "state.",
        route,
        task_id,
        last.get("ended_at"),
        last_attempt_key(route),
    )
    return True


async def record_failed_attempt(
    lithos: _GuardClient,
    *,
    task_id: str,
    route: str,
    agent: str,
    payload: Mapping[str, Any],
    run_id: str | None = None,
) -> None:
    """Best-effort persist the failed attempt on the task.

    Called BEFORE the claim release so the record exists by the time the task
    is externally visible as unclaimed. A Lithos hiccup here must never mask
    the ``[BlockerFailed]`` finding or the release, so failures are logged
    and swallowed. ``task_update`` metadata is an additive per-key merge, so
    this cannot clobber ``develop_status``, ``pr_gate_id``, or any
    plugin-written key.
    """
    marker: dict[str, Any] = {
        "status": "failed",
        "ended_at": datetime.now(UTC).isoformat(),
        "task_fingerprint": task_fingerprint(payload),
    }
    if run_id:
        marker["run_id"] = run_id
    key = last_attempt_key(route)
    try:
        await lithos.task_update(
            task_id=task_id,
            agent=agent,
            metadata={key: marker},
        )
    except Exception:
        logger.exception("route %s: recording %s on %s failed", route, key, task_id)


async def clear_superseded_failure(
    lithos: _GuardClient,
    *,
    task_id: str,
    route: str,
    agent: str,
    payload: Mapping[str, Any],
) -> None:
    """Best-effort per-key delete of ``route``'s failed-attempt marker, iff
    the dispatch-time ``payload`` carried one (no round trip otherwise).

    Used when an ``interrupted`` run supersedes an earlier failure: the
    restart bootstrap is interrupted's designed recovery path, and a stale
    failure marker must not veto it. Failures are logged and swallowed —
    same contract as :func:`record_failed_attempt`.
    """
    if failed_attempt_for_route(payload.get("metadata") or {}, route) is None:
        return
    key = last_attempt_key(route)
    try:
        await lithos.task_update(
            task_id=task_id,
            agent=agent,
            metadata={key: None},
        )
    except Exception:
        logger.exception("route %s: clearing %s on %s failed", route, key, task_id)
