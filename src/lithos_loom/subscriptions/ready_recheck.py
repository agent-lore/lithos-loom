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
        self._attempts: dict[str, int] = {}  # spent by re-checks that RAN
        self._pending: dict[str, asyncio.Task[None]] = {}  # one sleeper per task

    def pending(self, task_id: str) -> bool:
        return task_id in self._pending

    def pending_count(self) -> int:
        return len(self._pending)

    def settled(self, task_id: str) -> None:
        """Lithos answered definitively (ready, not ready, gone, terminal):
        the task's re-check budget starts fresh and any sleeper is dropped."""
        self._attempts.pop(task_id, None)
        sleeper = self._pending.pop(task_id, None)
        if sleeper is not None:
            sleeper.cancel()

    def schedule(self, task_id: str) -> bool:
        """Re-ask about *task_id* after the delay. Coalesced: a task with a
        sleeper already pending gets no second one (duplicate events must not
        spend the budget — only a re-check that RUNS does). ``False`` when the
        bound is spent and the task is left to the bootstrap replay."""
        if task_id in self._pending:
            return True
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
        task = asyncio.create_task(
            self._recheck(task_id), name=f"ready-recheck-{task_id}"
        )
        self._pending[task_id] = task
        return True

    async def _recheck(self, task_id: str) -> None:
        await asyncio.sleep(READY_RECHECK_SECONDS)
        self._attempts[task_id] = self._attempts.get(task_id, 0) + 1
        try:
            task = await self._lithos.task_get(task_id=task_id)
            if task is None or task.status != "open":
                self._attempts.pop(task_id, None)  # gone / terminal: definitive
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
                "RouteRunner %s: could not re-check readiness of %s; re-asking",
                self._route,
                task_id,
            )
            self._pending.pop(task_id, None)
            self.schedule(task_id)  # a failed read is itself undetermined
            return
        finally:
            if self._pending.get(task_id) is asyncio.current_task():
                self._pending.pop(task_id, None)
