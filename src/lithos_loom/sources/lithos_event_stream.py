"""LithosEventStream — push-based source consuming Lithos's /events SSE (issue #8).

Replaces the snapshot-polling :class:`LithosPoller` with a streaming
consumer of Lithos's dedicated event channel. The wire format is the
standard SSE protocol (``id:`` + ``event:`` + ``data:`` lines, blank line
terminator); the server's event vocabulary is documented at
``lithos/src/lithos/events.py``.

Lifecycle on ``run()``:

1. **Connect.** Open ``<events_url>?types=task.*`` with ``aconnect_sse``.
   The server immediately starts buffering events for this subscription.
2. **Bootstrap (first attempt only).** One ``task_list(status="open")``
   call inside the SSE context. Each returned task is published as
   ``lithos.task.created`` with the full poller-shaped payload. This is
   the same source-replay guarantee D11/D13 ask for: subscribers can be
   re-authoritative on restart. Running inside the SSE context closes
   the snapshot/connect race — any state change that happens during the
   snapshot is buffered server-side and drained in step 3 (duplicates
   are absorbed by ``RouteRunner._processed_tasks``).
3. **Stream.** Iterate events, translate ``task.X`` → ``lithos.task.X``,
   enrich each slim Lithos payload (which carries only
   ``{task_id, agent, aspect, …}``) into the full
   ``{id, title, status, tags, metadata, claims}`` shape RouteRunner
   expects by calling ``task_list(status="open")`` and matching by id.
   Cache the enriched task so terminal events (where the open list no
   longer contains the task) can fall back to the last-known snapshot.
4. **Reconnect.** On any error during connect, bootstrap, or iteration,
   sleep with exponential backoff and retry, passing ``Last-Event-ID``
   so the server can replay buffered events. Bootstrap is also retried
   under this loop until it succeeds; subsequent reconnects skip it. If
   the server's ring buffer evicted the gap, events are silently lost;
   the operator-facing PR documents this as a known limitation.

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
    """Minimum surface the event-stream source depends on.

    Only ``task_list`` is required — it returns the full Task shape
    (id, title, status, tags, metadata, claims) which downstream tag
    filters need. ``task_status`` is deliberately NOT used for
    enrichment because Lithos's implementation drops tags + metadata
    (see ``LithosClient.task_status`` docstring), which would make
    routed events unmatchable.
    """

    async def task_list(
        self,
        *,
        status: str | None = None,
        with_claims: bool = False,
    ) -> list[Task]: ...


def _default_httpx_timeout() -> httpx.Timeout:
    """Timeout for the SSE streaming AsyncClient.

    Read timeout disabled (``None``): Lithos sends keepalive comments
    every 15s, but the stream is otherwise idle between events. httpx's
    default 5s read timeout would fire constantly under steady-state
    quiet, triggering reconnect-with-backoff and losing events.

    Connect / write / pool retain modest defaults so connection-level
    failures still surface promptly.
    """
    return httpx.Timeout(connect=10.0, read=None, write=10.0, pool=5.0)


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
    _httpx_timeout: httpx.Timeout = field(default_factory=_default_httpx_timeout)

    def __post_init__(self) -> None:
        self._last_event_id: str | None = None
        # Cache of the most recent Task object seen per id. Populated
        # during bootstrap and refreshed via ``task_list`` whenever an
        # SSE event arrives for an unknown task id. The cache carries
        # the full Task shape (id, title, status, tags, metadata,
        # claims) so downstream tag filters work on every published
        # event, not just the bootstrap ones.
        self._known_tasks: dict[str, Task] = {}
        # Flips to True once bootstrap has published its snapshot at
        # least once. Subsequent reconnects skip bootstrap ONLY when we
        # also have a ``Last-Event-ID`` to replay from — otherwise the
        # dead subscription's buffered events would be lost. See
        # ``_stream_once`` for the combined gate.
        self._bootstrapped: bool = False
        # Events published during the current ``_stream_once`` attempt
        # (bootstrap + SSE). Exposed so ``run()`` can still see how
        # much progress an attempt made even when it raises mid-stream
        # — used for backoff-reset decisions.
        self._events_this_attempt: int = 0

    async def run(self) -> None:
        """Connect, bootstrap-once, then stream forever. Cancellable.

        Bootstrap runs *inside* the SSE context (see ``_stream_once``)
        so that the server is already subscribed when the open-tasks
        snapshot is taken. Any state change that occurs between snapshot
        and drain is buffered server-side and surfaces in ``aiter_sse``;
        the duplicate ``lithos.task.created`` for tasks present in both
        is absorbed by ``RouteRunner._processed_tasks``. Bootstrap also
        sits inside the reconnect/backoff loop, so a transient
        ``task_list`` failure at startup retries with backoff instead of
        escaping ``run()`` and killing the source task silently.
        """
        backoff = self.reconnect_backoff_seconds
        while True:
            try:
                events_seen = await self._stream_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Preserve any progress this attempt made before the
                # exception (bootstrap publishes + any drained SSE
                # events) so backoff resets when work happened. Without
                # this, a successful bootstrap followed by an immediate
                # stream drop would let backoff ratchet toward the cap.
                events_seen = self._events_this_attempt
                logger.exception(
                    "LithosEventStream: error; retrying after %.3fs",
                    backoff,
                )
            # Always sleep between reconnect attempts so a clean-but-empty
            # server response can't busy-loop us. Reset backoff only when
            # the attempt produced events.
            if events_seen > 0:
                backoff = self.reconnect_backoff_seconds
            await asyncio.sleep(backoff)
            if events_seen == 0:
                backoff = min(backoff * 2, self.max_reconnect_backoff_seconds)

    # ── bootstrap ────────────────────────────────────────────────────

    async def _bootstrap(self) -> int:
        """Snapshot open tasks and publish each as ``lithos.task.created``.

        Returns the number of events published. ``self._bootstrapped``
        flips to ``True`` only after every snapshot event has been
        published, so a mid-publish exception causes the next reconnect
        attempt to re-bootstrap (RouteRunner dedup absorbs any partial
        duplicates).
        """
        tasks = await self.client.task_list(status="open", with_claims=True)
        logger.info(
            "LithosEventStream: bootstrapping snapshot of %d open task(s)",
            len(tasks),
        )
        for task in tasks:
            self._known_tasks[task.id] = task
            await self._publish("lithos.task.created", task)
        self._bootstrapped = True
        return len(tasks)

    # ── streaming ────────────────────────────────────────────────────

    async def _stream_once(self) -> int:
        """Connect, bootstrap-if-needed inside the SSE context, then drain.

        Subscribe-before-snapshot: opening ``aconnect_sse`` causes the
        server to start buffering events for this subscription. We take
        the ``task_list`` snapshot *after* that, so any state change
        that lands between snapshot and drain still arrives via the
        buffered SSE feed once iteration begins. Returns total events
        published (bootstrap + SSE). ``self._events_this_attempt`` is
        updated incrementally so ``run()`` can read the partial count
        if this method raises.

        Bootstrap-on-reconnect contract: skip bootstrap only when the
        previous attempt left us with a ``Last-Event-ID`` to replay
        from. If we've bootstrapped at least once but never drained an
        SSE event with an id (e.g. the first subscription dropped
        before any event came through), re-bootstrap so we don't lose
        whatever events were buffered on the dead subscription. The
        duplicate ``lithos.task.created`` events that may result are
        absorbed by ``RouteRunner._processed_tasks``.
        """
        self._events_this_attempt = 0
        headers: dict[str, str] = {}
        if self._last_event_id is not None:
            headers["Last-Event-ID"] = self._last_event_id
        params = {"types": ",".join(_HANDLED_LITHOS_EVENT_TYPES)}

        # Re-bootstrap unless we have a resume cursor from a prior
        # successful event drain. Without that cursor a reconnect would
        # come up empty for any events buffered on the lost subscription.
        bootstrap_this_attempt = not self._bootstrapped or self._last_event_id is None

        logger.info(
            "LithosEventStream: connecting to %s (Last-Event-ID=%s, bootstrap=%s)",
            self.events_url,
            self._last_event_id or "<none>",
            bootstrap_this_attempt,
        )

        async with AsyncExitStack() as stack:
            # The real httpx_sse.aconnect_sse needs an AsyncClient owner;
            # tests inject a stub that ignores it. Pass the source's
            # configured timeout (read disabled by default — see
            # _default_httpx_timeout for rationale).
            http_client = await stack.enter_async_context(
                self._httpx_client_factory(timeout=self._httpx_timeout)
            )
            event_source = await stack.enter_async_context(
                self._aconnect_sse(
                    http_client,
                    "GET",
                    self.events_url,
                    headers=headers,
                    params=params,
                )
            )
            if bootstrap_this_attempt:
                self._events_this_attempt += await self._bootstrap()
            async for sse in event_source.aiter_sse():
                await self._handle_sse_event(sse)
                if sse.id:
                    self._last_event_id = sse.id
                self._events_this_attempt += 1
        return self._events_this_attempt

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
        1. Cached full-shape Task from bootstrap or a prior enrichment.
           For terminal events the ``status`` field is overridden with
           the canonical terminal state from the SSE event type.
        2. On cache miss, refresh from ``task_list(status="open")`` —
           this picks up tasks created after bootstrap. The cache is
           updated in-place (existing terminal-state entries are
           preserved so later terminal events still have something to
           fall back on).
        3. If still nothing, return ``None`` so the caller can skip.

        Errors from ``task_list`` propagate so the reconnect loop can
        retry the same SSE event (we have NOT yet advanced
        ``_last_event_id``, so the server replays). Swallowing the
        error here would acknowledge the event and lose it.
        """
        cached = self._known_tasks.get(task_id)
        if cached is not None:
            return _with_terminal_status(cached, event_type)

        # Unknown task id — refresh the open-task cache. Most SSE events
        # for currently-open tasks will resolve here.
        tasks = await self.client.task_list(status="open", with_claims=True)
        for t in tasks:
            self._known_tasks[t.id] = t

        cached = self._known_tasks.get(task_id)
        if cached is not None:
            logger.debug(
                "LithosEventStream: enriched unknown %s via task_list refresh",
                task_id,
            )
            return _with_terminal_status(cached, event_type)

        # Not in the refreshed open-task list either. Two cases:
        # - Truly unknown (deleted? race condition?): skip.
        # - Already terminal at the time of refresh and we never saw the
        #   open form: skip (no tags/metadata available, can't route).
        # Either way, drop the event with a debug note.
        return None

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


def _with_terminal_status(task: Task, lithos_event_type: str) -> Task:
    """Override ``task.status`` with the canonical terminal status for the SSE event.

    Returns ``task`` unchanged for non-terminal event types or when the
    status already matches. The SSE event is the source-of-truth — if a
    ``task.completed`` arrives, the published payload's status must
    reflect that even if the cached Task still shows ``open`` (which
    will happen during the brief window between Lithos updating the
    row and the source's cache being refreshed).
    """
    terminal = _terminal_status_for(lithos_event_type)
    if terminal is None or task.status == terminal:
        return task
    return Task(
        id=task.id,
        title=task.title,
        status=terminal,
        tags=task.tags,
        metadata=task.metadata,
        claims=task.claims,
    )


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
