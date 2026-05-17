"""LithosEventStream — push-based source consuming Lithos's /events SSE (issue #8).

Replaces the snapshot-polling :class:`LithosPoller` with a streaming
consumer of Lithos's dedicated event channel. The wire format is the
standard SSE protocol (``id:`` + ``event:`` + ``data:`` lines, blank line
terminator); the server's event vocabulary is documented at
``lithos/src/lithos/events.py``.

Lifecycle on ``run()``:

1. **Bootstrap.** One ``task_list(status="open")`` call. Each returned
   task is published as ``lithos.task.created`` with the full poller-
   shaped payload. This is the same source-replay guarantee D11/D13 ask
   for: subscribers can be re-authoritative on restart.
2. **Stream.** Connect to ``<events_url>?types=task.*``. Iterate events,
   translate ``task.X`` → ``lithos.task.X``, enrich each slim Lithos
   payload (which carries only ``{task_id, agent, aspect, …}``) into the
   full ``{id, title, status, tags, metadata, claims}`` shape RouteRunner
   expects by calling ``task_status(task_id)``. Cache the enriched task
   so terminal events (where ``task_status`` may report not-found) can
   fall back to the last-known snapshot.
3. **Reconnect.** On any error during connection or iteration, sleep with
   exponential backoff and reconnect, passing ``Last-Event-ID`` so the
   server can replay buffered events. If the server's ring buffer evicted
   the gap, events are silently lost; the operator-facing PR documents
   this as a known limitation.

The source uses ``httpx_sse.aconnect_sse`` under the hood; the
constructor accepts an ``_aconnect_sse`` injection point so tests can
stub it without spinning up an HTTP server.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any, Protocol

import httpx
from httpx_sse import aconnect_sse

from lithos_loom.bus import Event, EventBus
from lithos_loom.lithos_client import Task

__all__ = ["LithosEventStream", "EventStreamClient"]

logger = logging.getLogger(__name__)


_HANDLED_LITHOS_EVENT_TYPES = (
    "task.created",
    "task.claimed",
    "task.released",
    "task.completed",
    "task.cancelled",
)
"""Lithos-side event types we subscribe to. Sent server-side as ``?types=``."""


class EventStreamClient(Protocol):
    """Minimum surface the event-stream source depends on."""

    async def task_list(
        self,
        *,
        status: str | None = None,
        with_claims: bool = False,
    ) -> list[Task]: ...

    async def task_status(self, *, task_id: str) -> Task | None: ...


@dataclass
class LithosEventStream:
    client: EventStreamClient
    bus: EventBus
    events_url: str
    reconnect_backoff_seconds: float = 1.0
    max_reconnect_backoff_seconds: float = 30.0
    # Injection points for tests. Default to the real httpx surfaces.
    _aconnect_sse: Any = field(default=aconnect_sse)
    _httpx_client_factory: Any = field(default=httpx.AsyncClient)

    def __post_init__(self) -> None:
        self._last_event_id: str | None = None
        # Cache of the most recent Task object seen per id. Populated
        # during bootstrap and after every successful task_status
        # enrichment. Used as a fallback payload when a terminal-state
        # SSE event arrives but task_status reports not-found.
        self._known_tasks: dict[str, Task] = {}

    async def run(self) -> None:
        """Bootstrap then stream forever. Cancellable."""
        await self._bootstrap()
        backoff = self.reconnect_backoff_seconds
        while True:
            events_seen = 0
            try:
                events_seen = await self._stream_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "LithosEventStream: stream error; reconnecting after %.3fs",
                    backoff,
                )
            # Always sleep between reconnect attempts so a clean-but-empty
            # server response can't busy-loop us. Reset backoff only when
            # the connection actually produced events.
            if events_seen > 0:
                backoff = self.reconnect_backoff_seconds
            await asyncio.sleep(backoff)
            if events_seen == 0:
                backoff = min(backoff * 2, self.max_reconnect_backoff_seconds)

    # ── bootstrap ────────────────────────────────────────────────────

    async def _bootstrap(self) -> None:
        tasks = await self.client.task_list(status="open", with_claims=True)
        logger.info(
            "LithosEventStream: bootstrapping snapshot of %d open task(s)",
            len(tasks),
        )
        for task in tasks:
            self._known_tasks[task.id] = task
            await self._publish("lithos.task.created", task)

    # ── streaming ────────────────────────────────────────────────────

    async def _stream_once(self) -> int:
        """Connect, drain events until EOF or error. Returns count seen."""
        headers: dict[str, str] = {}
        if self._last_event_id is not None:
            headers["Last-Event-ID"] = self._last_event_id
        params = {"types": ",".join(_HANDLED_LITHOS_EVENT_TYPES)}

        logger.info(
            "LithosEventStream: connecting to %s (Last-Event-ID=%s)",
            self.events_url,
            self._last_event_id or "<none>",
        )

        events_seen = 0
        async with AsyncExitStack() as stack:
            # The real httpx_sse.aconnect_sse needs an AsyncClient owner;
            # tests inject a stub that ignores it.
            http_client = await stack.enter_async_context(self._httpx_client_factory())
            event_source = await stack.enter_async_context(
                self._aconnect_sse(
                    http_client,
                    "GET",
                    self.events_url,
                    headers=headers,
                    params=params,
                )
            )
            async for sse in event_source.aiter_sse():
                await self._handle_sse_event(sse)
                if sse.id:
                    self._last_event_id = sse.id
                events_seen += 1
        return events_seen

    # ── per-event handling ───────────────────────────────────────────

    async def _handle_sse_event(self, sse: Any) -> None:
        sse_id = getattr(sse, "id", "") or "<none>"
        event_type = getattr(sse, "event", "") or ""
        if event_type not in _HANDLED_LITHOS_EVENT_TYPES:
            # Server-side ?types= filter is the canonical defence; this
            # is belt-and-braces against config drift / future event
            # types that leak into the same stream.
            logger.debug(
                "LithosEventStream: ignoring non-task event id=%s type=%r",
                sse_id,
                event_type,
            )
            return

        try:
            data = json.loads(sse.data) if sse.data else {}
        except json.JSONDecodeError:
            logger.warning(
                "LithosEventStream: malformed JSON in SSE id=%s type=%s; skipping",
                sse_id,
                event_type,
            )
            return

        task_id = data.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            logger.warning(
                "LithosEventStream: SSE id=%s type=%s missing task_id; skipping",
                sse_id,
                event_type,
            )
            return

        logger.debug(
            "LithosEventStream: received SSE id=%s type=%s task=%s",
            sse_id,
            event_type,
            task_id,
        )

        task = await self._enrich(task_id, event_type)
        if task is None:
            logger.warning(
                "LithosEventStream: cannot resolve task %s for %s "
                "(SSE id=%s); skipping",
                task_id,
                event_type,
                sse_id,
            )
            return

        loom_type = f"lithos.{event_type}"
        await self._publish(loom_type, task)

    async def _enrich(self, task_id: str, event_type: str) -> Task | None:
        """Return the best Task for the event, or None if we have nothing useful.

        Preference order:
        1. Fresh ``task_status(task_id)`` result, if available.
        2. Cached snapshot from bootstrap or a prior enrichment, with the
           ``status`` field overridden by the canonical terminal state
           inferred from the SSE event type (so the payload's status
           reflects what the SSE event reports, not what we last saw).
        """
        try:
            current = await self.client.task_status(task_id=task_id)
        except Exception:
            logger.exception(
                "LithosEventStream: task_status(%s) failed; using snapshot fallback",
                task_id,
            )
            current = None

        if current is not None:
            self._known_tasks[task_id] = current
            return current

        cached = self._known_tasks.get(task_id)
        if cached is None:
            return None

        terminal_status = _terminal_status_for(event_type)
        if terminal_status is None:
            # Non-terminal event for a task we lost track of — best we
            # can do is publish the cached payload as-is.
            logger.info(
                "LithosEventStream: enriching %s with stale snapshot "
                "(task_status not_found)",
                task_id,
            )
            return cached
        logger.info(
            "LithosEventStream: enriching terminal %s with snapshot "
            "(task_status not_found); status=%s",
            task_id,
            terminal_status,
        )
        return Task(
            id=cached.id,
            title=cached.title,
            status=terminal_status,
            tags=cached.tags,
            metadata=cached.metadata,
            claims=cached.claims,
        )

    # ── bus publish ──────────────────────────────────────────────────

    async def _publish(self, event_type: str, task: Task) -> None:
        event = Event(
            type=event_type,
            timestamp=datetime.now(UTC),
            payload=_event_payload(task),
        )
        await self.bus.publish(event)
        logger.info("LithosEventStream: published %s for %s", event_type, task.id)


def _terminal_status_for(lithos_event_type: str) -> str | None:
    """Map a terminal-state Lithos event type to its canonical status string."""
    if lithos_event_type == "task.completed":
        return "completed"
    if lithos_event_type == "task.cancelled":
        return "cancelled"
    return None


def _event_payload(task: Task) -> Mapping[str, Any]:
    """Project a :class:`Task` into the read-only event payload shape.

    Mirrors :func:`lithos_loom.sources.lithos_poller._event_payload` so
    RouteRunner (and any future bus subscriber) is unaffected by the
    source swap.
    """
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
