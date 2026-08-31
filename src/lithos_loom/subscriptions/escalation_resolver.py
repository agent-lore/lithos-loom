"""Re-dispatch nudge for a resolved needs-human gate (b91177d2).

When the operator completes a loom-raised ``human`` gate, its story returns
to Lithos's ready frontier — but no event names the *story*: Lithos emits
``task.completed`` for the gate, and the story's own last event was the
failure that raised it. This subscriber closes that gap: it watches
``lithos.task.completed`` for loom's gates (``task_type=gate``,
``gate_type=human``, ``raised_by=loom`` — the operator's own human gates are
theirs), finds the waiter, and republishes a synthetic ``lithos.task.updated``
for it with ``origin="gate-resolved"``. The bus's tag filter routes that to
whichever route matches, and ``RouteRunner._handle`` treats the origin as an
explicit un-dedup of its in-process "fail once per task" set, so the story is
re-claimed and re-run *in the same daemon process* — the operator's tick is
the retry gesture.

This is a NUDGE, not the loop's correctness. The route-runner child's
bootstrap replays open tasks only, so a gate ticked while the daemon was down
never reaches this subscriber; the story is simply re-surfaced by bootstrap,
passes the readiness check (the gate is resolved) and is dispatched — and the
clean-up of ``needs_human_gate_id`` + the failed-attempt marker lives on that
dispatch path (``dispatch_guards.clear_resolved_escalation``), not here. So a
dropped or duplicate nudge is always recoverable: this handler nudges each
gate AT MOST ONCE per process (a completion is terminal, so a second delivery
is always an SSE replay — deduped by gate id), and only while the story is
still open and does not name a NEWER gate as its blocker; slice D's hygiene
completing a gate whose waiter already resolved is likewise a no-op. A
replayed completion arriving in a fresh process nudges again by design: the
state a duplicate nudge could corrupt (the fail-once dedup, the resume
schedule and budget) is per-process too, so post-restart it degrades to the
restart bootstrap's own correct semantics.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from lithos_loom.bus import Event, EventBus, Subscription
from lithos_loom.gates import (
    GATE_TYPE_HUMAN,
    RAISED_BY_LOOM,
    STORY_HUMAN_GATE_ID_KEY,
    waiter_of,
)
from lithos_loom.subscriptions.dispatch_guards import task_payload

__all__ = ["GATE_RESOLVED_ORIGIN", "EscalationResolver"]

logger = logging.getLogger(__name__)

GATE_RESOLVED_ORIGIN = "gate-resolved"
"""``Event.origin`` of the synthetic re-dispatch nudge; the route-runner
un-dedups its in-process processed set on exactly this origin."""


@dataclass
class EscalationResolver:
    """One subscriber per route-runner child; see the module docstring."""

    bus: EventBus
    lithos: Any
    agent_id: str

    def __post_init__(self) -> None:
        self._subscription: Subscription = self.bus.subscribe(
            event_types=("lithos.task.completed",),
            match={
                "task_type": "gate",
                "metadata": {"gate_type": GATE_TYPE_HUMAN, "raised_by": RAISED_BY_LOOM},
            },
            name="escalation-resolver",
        )
        # Gates this process has already nudged for (PR #349 review, round 2).
        # The event source is replay-capable, and a gate's completion is
        # terminal — so a second delivery is always a replay, never new
        # intent. Without this, a late/replayed completion arriving after the
        # first retry ran (and cleared the story's provenance) would look
        # exactly like the missing-provenance fallback below and nudge again:
        # un-dedup the runner's fail-once set, reset the resume budget, and
        # start a duplicate run past a pending resume schedule. In-memory is
        # the right durability: those three pieces of state are themselves
        # per-process, and after a restart a replayed nudge degrades to the
        # restart bootstrap's own (correct) semantics. Grows by one small id
        # per resolved loom gate per process lifetime.
        self._handled_gates: set[str] = set()

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
                    "EscalationResolver: unhandled error processing %s", event.type
                )

    async def _handle(self, event: Event) -> None:
        gate_id = str(event.payload.get("id") or "")
        if not gate_id:
            return
        if gate_id in self._handled_gates:
            logger.info(
                "EscalationResolver: gate %s already nudged this process; "
                "replayed completion ignored",
                gate_id,
            )
            return
        story_id = await waiter_of(self.lithos, gate_id)
        if story_id is None:
            logger.info(
                "EscalationResolver: gate %s has no waiter; nothing to nudge", gate_id
            )
            return
        story = await self.lithos.task_get(task_id=story_id)
        if story is None or story.status != "open":
            logger.info(
                "EscalationResolver: gate %s's story %s is not open; no nudge",
                gate_id,
                story_id,
            )
            return
        recorded = (story.metadata or {}).get(STORY_HUMAN_GATE_ID_KEY)
        if recorded and recorded != gate_id:
            # A stale completion: the story has since failed into a NEWER gate,
            # which guards it via readiness; nothing to do here.
            logger.info(
                "EscalationResolver: story %s names gate %s as its blocker, not "
                "%s; no nudge",
                story_id,
                recorded,
                gate_id,
            )
            return
        # An ABSENT key still nudges (PR #349 review F2): the degraded
        # escalation path — gate + edge landed, the story provenance write
        # failed — leaves exactly this shape, and skipping it made the
        # operator's tick a no-op until a daemon restart. The waits_on_gate
        # edge already proves this gate was the story's blocker. Absence is
        # ALSO the normal state after the first retry dispatched and cleared
        # the key — the per-process _handled_gates dedup above is what keeps
        # a replayed completion of this gate from nudging twice; beyond it,
        # readiness defers a story behind any newer gate and the
        # collision-safe claim refuses one that is mid-run.
        self._handled_gates.add(gate_id)
        await self.bus.publish(
            Event(
                type="lithos.task.updated",
                timestamp=datetime.now(UTC),
                payload=task_payload(story),
                origin=GATE_RESOLVED_ORIGIN,
            )
        )
        logger.info(
            "EscalationResolver: gate %s resolved; nudging story %s for re-dispatch",
            gate_id,
            story_id,
        )
