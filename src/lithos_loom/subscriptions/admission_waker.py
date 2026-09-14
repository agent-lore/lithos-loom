"""The admission waker — the gate-event half of serial admission (PRD S6).

One subscriber per route-runner child. When a ``pr`` gate closes or a loom
``human`` gate escalates one, a slot in that project's bucket may have
freed: it asks :meth:`~lithos_loom.subscriptions.admission.Admission.wake`
to republish the bucket's held stories — in release order, only as many
as there are free slots (ADR 0012) — so the runner re-asks admission now
rather than after the re-check backoff. A nudge only: the sleeper is the
fallback for a dropped one, and admission itself enforces the order at
every ask. Split from :mod:`.admission` for size; the decision lives there.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from lithos_loom.bus import Event, EventBus, Subscription
from lithos_loom.gates import GATE_TYPE_HUMAN, GATE_TYPE_PR, RAISED_BY_LOOM
from lithos_loom.subscriptions.admission import Admission
from lithos_loom.subscriptions.dispatch_guards import project_of

__all__ = ["AdmissionWaker"]

logger = logging.getLogger(__name__)


@dataclass
class AdmissionWaker:
    """One subscriber per route-runner child: when a ``pr`` gate closes or a
    loom ``human`` gate escalates one, ask :meth:`Admission.wake` to
    republish that project's held stories — in release order — so the
    runner re-asks admission now rather than after the re-check backoff. A
    nudge only — the sleeper is the fallback, and admission itself enforces
    the order at every ask. A story's own terminal event is the other thing
    it carries: :meth:`Admission.discard` releases the scheduler's memory of
    it (PR #398 review), so that memory is bounded by the open stories."""

    bus: EventBus
    admission: Admission

    def __post_init__(self) -> None:
        self._subscription: Subscription = self.bus.subscribe(
            event_types=(
                "lithos.task.created",
                "lithos.task.completed",
                "lithos.task.cancelled",
            ),
            name="admission-waker",
        )

    @property
    def subscription(self) -> Subscription:
        return self._subscription

    async def run(self) -> None:
        """Drain the subscription forever. Cancellable."""
        while True:
            event = await self._subscription.queue.get()
            try:
                await self._handle(event)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "AdmissionWaker: unhandled error processing %s", event.type
                )

    async def _handle(self, event: Event) -> None:
        metadata = event.payload.get("metadata") or {}
        if event.payload.get("task_type") != "gate":
            if event.type != "lithos.task.created":
                task_id = event.payload.get("id")
                if isinstance(task_id, str) and task_id:
                    await self.admission.discard(task_id)
            return
        if not _frees_a_slot(event.type, metadata):
            return
        project = project_of(metadata)  # None → the projectless bucket
        woken = await self.admission.wake(project)
        if woken:
            logger.info(
                "AdmissionWaker: %s %s in project %r; re-asked admission for %d "
                "held story(ies) in release order",
                event.type,
                event.payload.get("id"),
                project,
                woken,
            )


def _frees_a_slot(event_type: str, metadata: Any) -> bool:
    gate_type = metadata.get("gate_type")
    if gate_type == GATE_TYPE_PR:
        return event_type in ("lithos.task.completed", "lithos.task.cancelled")
    if gate_type == GATE_TYPE_HUMAN:
        return (
            event_type == "lithos.task.created"
            and metadata.get("raised_by") == RAISED_BY_LOOM
        )
    return False
