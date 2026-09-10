"""Re-check a task whose readiness Lithos would not settle (PR #352 review F2).

``task_ready`` has no per-task filter, so when both frontier pages for a
task's own scope are full the runner cannot decide, and a ``task.updated``
carrying a released dependent used to be dropped at that point — deferred
with nothing left to retry it until a restart. This re-asks after a delay by
republishing the task onto the bus as a synthetic ``lithos.task.updated``
(origin :data:`READY_RECHECK_ORIGIN`), bounded per task; past the bound the
restart's bootstrap replay is the durable backstop, and any edit to the task
re-surfaces it too. In-process state, deliberately: the restart replays every
open task anyway.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from lithos_loom.bus import Event, EventBus
from lithos_loom.subscriptions.dispatch_guards import task_payload

__all__ = [
    "READY_RECHECK_MAX",
    "READY_RECHECK_ORIGIN",
    "READY_RECHECK_SECONDS",
    "ReadyRechecker",
]

logger = logging.getLogger(__name__)

READY_RECHECK_SECONDS = 60.0
READY_RECHECK_MAX = 10
READY_RECHECK_ORIGIN = "ready-recheck"


class ReadyRechecker:
    """Per-route bounded re-check scheduler (see the module docstring)."""

    def __init__(self, *, bus: EventBus, lithos: Any, route: str) -> None:
        self._bus = bus
        self._lithos = lithos
        self._route = route
        self._attempts: dict[str, int] = {}
        self._tasks: set[asyncio.Task[None]] = set()

    def settled(self, task_id: str) -> None:
        """Lithos answered — the task's re-check budget starts fresh."""
        self._attempts.pop(task_id, None)

    def schedule(self, task_id: str) -> bool:
        """Re-ask about *task_id* after the delay; ``False`` when the bound is
        spent and the task is left to the bootstrap replay."""
        attempts = self._attempts.get(task_id, 0)
        if attempts >= READY_RECHECK_MAX:
            logger.warning(
                "RouteRunner %s: %s's readiness is still undetermined after %d "
                "re-checks; leaving it to the next restart's bootstrap replay "
                "(or any edit to the task)",
                self._route,
                task_id,
                attempts,
            )
            return False
        self._attempts[task_id] = attempts + 1
        task = asyncio.create_task(
            self._recheck(task_id), name=f"ready-recheck-{task_id}"
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return True

    async def _recheck(self, task_id: str) -> None:
        await asyncio.sleep(READY_RECHECK_SECONDS)
        try:
            task = await self._lithos.task_get(task_id=task_id)
            if task is None or task.status != "open":
                return
            await self._bus.publish(
                Event(
                    type="lithos.task.updated",
                    timestamp=datetime.now(UTC),
                    payload=task_payload(task),
                    origin=READY_RECHECK_ORIGIN,
                )
            )
        except Exception:
            logger.exception(
                "RouteRunner %s: could not re-check readiness of %s",
                self._route,
                task_id,
            )
