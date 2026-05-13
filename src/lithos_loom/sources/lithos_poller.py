"""LithosPoller — periodic ``lithos_task_list`` source (Slice 0 US3).

Polls Lithos at a configured interval, diffs the returned list against an
in-memory snapshot keyed by task id, and publishes ``lithos.task.*`` events
for each transition onto the in-process :class:`EventBus`.

Emitted event types:

* ``lithos.task.created`` — id newly seen this poll
  (suppressed on first poll for tasks already in terminal state, to avoid
  re-firing history every time the daemon restarts)
* ``lithos.task.updated`` — same id, content changed
  (tags, title, metadata)
* ``lithos.task.completed`` — status went ``open → completed``
* ``lithos.task.cancelled`` — status went ``open → cancelled``
* ``lithos.task.claimed`` — claims went empty → non-empty
* ``lithos.task.released`` — claims went non-empty → empty

D11/D13 make this a re-authoritative source: on daemon restart the first
poll replays whatever the current open-task list looks like, and
subscribers are responsible for idempotency.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any, Protocol

from lithos_loom.bus import Event, EventBus
from lithos_loom.lithos_client import Task

__all__ = ["LithosPoller", "TaskListClient"]

logger = logging.getLogger(__name__)


class TaskListClient(Protocol):
    """Minimum surface the poller depends on. Lets tests inject a fake."""

    async def task_list(
        self,
        *,
        status: str | None = None,
        with_claims: bool = False,
    ) -> list[Task]: ...


@dataclass
class LithosPoller:
    client: TaskListClient
    bus: EventBus
    interval: float = 30.0

    def __post_init__(self) -> None:
        self._snapshot: dict[str, Task] = {}
        self._first_poll: bool = True

    async def run(self) -> None:
        """Poll forever at ``interval`` seconds. Cancellable."""
        while True:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("LithosPoller: poll_once raised; retrying after sleep")
            await asyncio.sleep(self.interval)

    async def poll_once(self) -> None:
        """One poll iteration. Useful in tests + for manual triggering."""
        tasks = await self.client.task_list(with_claims=True)
        new_snapshot = {t.id: t for t in tasks}

        for task in tasks:
            await self._emit_for_task(task)

        self._snapshot = new_snapshot
        self._first_poll = False

    async def _emit_for_task(self, task: Task) -> None:
        prev = self._snapshot.get(task.id)
        if prev is None:
            # New id this poll.
            if self._first_poll and task.status != "open":
                # First-ever poll picked up an already-terminal task; don't
                # re-fire its created event every daemon restart.
                return
            await self._publish("lithos.task.created", task)
            return

        # Status transitions take precedence over content / claim diffs.
        if prev.status == "open" and task.status == "completed":
            await self._publish("lithos.task.completed", task)
            return
        if prev.status == "open" and task.status == "cancelled":
            await self._publish("lithos.task.cancelled", task)
            return

        # Claim transitions.
        if not prev.claims and task.claims:
            await self._publish("lithos.task.claimed", task)
            return
        if prev.claims and not task.claims:
            await self._publish("lithos.task.released", task)
            return

        # Generic content change (tags, title, metadata, or claim shape).
        if task != prev:
            await self._publish("lithos.task.updated", task)

    async def _publish(self, event_type: str, task: Task) -> None:
        event = Event(
            type=event_type,
            timestamp=datetime.now(UTC),
            payload=_event_payload(task),
        )
        await self.bus.publish(event)


def _event_payload(task: Task) -> Mapping[str, Any]:
    """Project a :class:`Task` into the read-only event payload shape."""
    return MappingProxyType(
        {
            "id": task.id,
            "title": task.title,
            "status": task.status,
            "tags": list(task.tags),
            "metadata": dict(task.metadata),
            "claims": [dict(c) for c in task.claims],
        }
    )
