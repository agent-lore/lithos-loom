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
``metadata.loom_last_attempt`` on the task and declines a
**bootstrap-origin** event whose marker records a failed last attempt for
the same route — a restart is a process lifecycle event, not anyone asking
for another run.

Failure must stay retryable: the primary recovery loop is an operator
sharpening the task, and that edit arrives as a live ``lithos.task.updated``
which bypasses the decline by origin — as do a re-added trigger tag, the T10
resume re-dispatch, and deleting the marker key. A later gated delivery
clears the marker (see ``route_runner._gate_and_release``).

Route-scoped on purpose: a task carrying several trigger tags can be handled
by different routes, and a failure under route A says nothing about route B.

Reserved namespace: plugins see ``loom_last_attempt`` in their ``task.json``
metadata and must not repurpose it (SPECIFICATION §2.2).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Protocol

__all__ = [
    "LAST_ATTEMPT_KEY",
    "READY_QUERY_LIMIT",
    "failed_attempt_for_route",
    "on_ready_frontier",
    "record_failed_attempt",
]

logger = logging.getLogger(__name__)

# Task-metadata key: {"route", "status", "ended_at", "run_id"?}.
LAST_ATTEMPT_KEY = "loom_last_attempt"

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
    last = metadata.get(LAST_ATTEMPT_KEY)
    if (
        isinstance(last, Mapping)
        and last.get("status") == "failed"
        and last.get("route") == route
    ):
        return last
    return None


async def record_failed_attempt(
    lithos: _GuardClient,
    *,
    task_id: str,
    route: str,
    agent: str,
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
        "route": route,
        "status": "failed",
        "ended_at": datetime.now(UTC).isoformat(),
    }
    if run_id:
        marker["run_id"] = run_id
    try:
        await lithos.task_update(
            task_id=task_id,
            agent=agent,
            metadata={LAST_ATTEMPT_KEY: marker},
        )
    except Exception:
        logger.exception(
            "route %s: recording %s on %s failed", route, LAST_ATTEMPT_KEY, task_id
        )
