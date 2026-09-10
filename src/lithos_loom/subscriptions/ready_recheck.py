"""Re-check a task whose readiness Lithos would not settle (PR #352 review F2).

``task_ready`` has no per-task filter, so when both frontier pages for a
task's own scope are full — or the read itself fails — the runner cannot
decide, and a ``task.updated`` carrying a released dependent used to be
dropped at that point, with nothing left to retry it until a restart. This
re-asks by republishing the task onto the bus as a synthetic
``lithos.task.updated`` (origin :data:`READY_RECHECK_ORIGIN`) after a delay
that backs off exponentially to a cap, and it **never gives up** (PR #352
review round 3: a bounded retry re-created the original failure once the
bound was spent): one coalesced sleeper per task stays live until a
definitive answer — ready, not ready, gone, terminal — resets it. In-process
state, deliberately: a restart's bootstrap replay re-asks every open task
anyway.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from lithos_loom.bus import Event, EventBus
from lithos_loom.subscriptions.dispatch_guards import task_payload

__all__ = [
    "READY_RECHECK_MAX_SECONDS",
    "READY_RECHECK_ORIGIN",
    "READY_RECHECK_SECONDS",
    "ReadyRechecker",
    "delay_for",
]

logger = logging.getLogger(__name__)

# The first re-check's delay; each further inconclusive answer doubles it,
# capped at READY_RECHECK_MAX_SECONDS (a saturated frontier or a Lithos
# outage lasting an hour costs a handful of reads, and recovery is noticed
# within the cap).
READY_RECHECK_SECONDS = 60.0
READY_RECHECK_MAX_SECONDS = 900.0
READY_RECHECK_ORIGIN = "ready-recheck"
# every this many inconclusive re-checks, say so again in the log
_NAG_EVERY = 10


def delay_for(attempts: int) -> float:
    """Seconds to wait before the next re-check after *attempts* inconclusive
    ones: exponential from the base, capped."""
    # the exponent is capped: a task stuck for days must not overflow a float
    return min(
        READY_RECHECK_SECONDS * (2 ** min(attempts, 30)), READY_RECHECK_MAX_SECONDS
    )


class ReadyRechecker:
    """Per-route re-check scheduler (see the module docstring)."""

    def __init__(self, *, bus: EventBus, lithos: Any, route: str) -> None:
        self._bus = bus
        self._lithos = lithos
        self._route = route
        self._attempts: dict[str, int] = {}  # inconclusive re-checks that RAN
        self._pending: dict[str, asyncio.Task[None]] = {}  # one sleeper per task

    def pending(self, task_id: str) -> bool:
        return task_id in self._pending

    def pending_count(self) -> int:
        return len(self._pending)

    def attempts(self, task_id: str) -> int:
        return self._attempts.get(task_id, 0)

    def settled(self, task_id: str) -> None:
        """Lithos answered definitively (ready, not ready, gone, terminal):
        the task's backoff starts fresh and any sleeper is dropped."""
        self._attempts.pop(task_id, None)
        sleeper = self._pending.pop(task_id, None)
        if sleeper is not None:
            sleeper.cancel()

    def schedule(self, task_id: str) -> None:
        """Re-ask about *task_id* after the current backoff. Coalesced: a task
        with a sleeper already pending gets no second one (duplicate events
        must not spend the budget — only a re-check that RUNS does)."""
        if task_id in self._pending:
            return
        attempts = self.attempts(task_id)
        if attempts and attempts % _NAG_EVERY == 0:
            logger.warning(
                "RouteRunner %s: %s's readiness is still undetermined after %d "
                "re-checks; asking again in %.0fs and until Lithos answers "
                "(any edit to the task, or a restart, re-asks too)",
                self._route,
                task_id,
                attempts,
                delay_for(attempts),
            )
        task = asyncio.create_task(
            self._recheck(task_id, delay_for(attempts)),
            name=f"ready-recheck-{task_id}",
        )
        self._pending[task_id] = task

    async def _recheck(self, task_id: str, delay: float) -> None:
        await asyncio.sleep(delay)
        self._attempts[task_id] = self.attempts(task_id) + 1
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
