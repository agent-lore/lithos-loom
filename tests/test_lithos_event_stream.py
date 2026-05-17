"""Tests for ``lithos_loom.sources.lithos_event_stream`` (issue #8).

The event-stream source replaces the polling LithosPoller. It consumes
Lithos's ``GET /events`` SSE endpoint, enriches each slim event payload
via ``task_status``, and publishes the same ``lithos.task.*`` events
RouteRunner already consumes (so the source swap is invisible
downstream).

Tests inject a fake ``LithosClient`` and a fake ``aconnect_sse`` so the
source logic is exercised without an HTTP round trip — see
``test_lithos_client.py`` for the real client.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterable, Mapping
from typing import Any

import pytest

from lithos_loom.bus import EventBus, Subscription
from lithos_loom.lithos_client import Task
from lithos_loom.sources.lithos_event_stream import LithosEventStream

# ── Test helpers ────────────────────────────────────────────────────────


def _task(
    id_: str,
    *,
    status: str = "open",
    tags: tuple[str, ...] = (),
    metadata: Mapping[str, Any] | None = None,
    claims: tuple[Mapping[str, Any], ...] = (),
    title: str = "t",
) -> Task:
    return Task(
        id=id_,
        title=title,
        status=status,
        tags=tags,
        metadata=metadata or {},
        claims=claims,
    )


class _FakeSse:
    """Minimal stand-in for ``httpx_sse.ServerSentEvent``."""

    def __init__(self, *, event: str, data: dict[str, Any], id: str = "") -> None:
        self.event = event
        self.data = json.dumps(data)
        self.id = id


class _FakeEventSource:
    """Yields a pre-scripted iterable of SSE events, optionally raising."""

    def __init__(self, script: Iterable[_FakeSse | Exception]) -> None:
        self._script = list(script)

    async def aiter_sse(self) -> AsyncIterator[_FakeSse]:
        for item in self._script:
            if isinstance(item, Exception):
                raise item
            yield item


class _FakeAconnect:
    """Async-context-manager stand-in for ``httpx_sse.aconnect_sse``.

    Records every call with the kwargs it was invoked with (so tests can
    assert on ``Last-Event-ID`` header behaviour across reconnects) and
    dequeues the next pre-scripted EventSource from ``connections``.
    Entries can be either a list of events (success) or an Exception
    (raised when entering the context).
    """

    def __init__(
        self, connections: list[list[_FakeSse | Exception] | Exception]
    ) -> None:
        self._connections = list(connections)
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self, client: Any, method: str, url: str, **kwargs: Any
    ) -> _FakeAconnect._Ctx:
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": dict(kwargs.get("headers") or {}),
                "params": dict(kwargs.get("params") or {}),
            }
        )
        if not self._connections:
            return _FakeAconnect._Ctx(events=[])
        nxt = self._connections.pop(0)
        if isinstance(nxt, Exception):
            return _FakeAconnect._Ctx(error=nxt)
        return _FakeAconnect._Ctx(events=nxt)

    class _Ctx:
        def __init__(
            self,
            *,
            events: list[_FakeSse | Exception] | None = None,
            error: Exception | None = None,
        ) -> None:
            self._events = events
            self._error = error

        async def __aenter__(self) -> _FakeEventSource:
            if self._error is not None:
                raise self._error
            return _FakeEventSource(self._events or [])

        async def __aexit__(self, *exc: Any) -> None:
            return None


class _FakeClient:
    """Fake ``LithosClient`` with scripted ``task_list`` responses.

    The first ``task_list`` call returns ``bootstrap``. Subsequent
    calls (used by ``_enrich`` to refresh the cache on cache miss)
    return ``refresh_responses`` if provided, else ``bootstrap`` again.
    A ``RuntimeError`` (or any exception) in ``refresh_responses`` is
    raised instead of returned, so tests can assert reconnect behaviour
    when enrichment fails.
    """

    def __init__(
        self,
        *,
        bootstrap: list[Task] | None = None,
        refresh_responses: list[list[Task] | Exception] | None = None,
    ) -> None:
        self._bootstrap = list(bootstrap or [])
        self._refresh_responses = list(refresh_responses or [])
        self._first_call = True
        self.task_list_calls: list[dict[str, Any]] = []

    async def task_list(
        self,
        *,
        status: str | None = None,
        with_claims: bool = False,
    ) -> list[Task]:
        self.task_list_calls.append({"status": status, "with_claims": with_claims})
        if self._first_call:
            self._first_call = False
            return list(self._bootstrap)
        if self._refresh_responses:
            nxt = self._refresh_responses.pop(0)
            if isinstance(nxt, Exception):
                raise nxt
            return list(nxt)
        # No script set for refresh — return the bootstrap as a stable
        # fallback so well-known tasks remain enrichable.
        return list(self._bootstrap)


def _drain(sub: Subscription) -> list[tuple[str, dict[str, Any]]]:
    """Drain a subscription queue to (event_type, payload) tuples."""
    out: list[tuple[str, dict[str, Any]]] = []
    while not sub.queue.empty():
        ev = sub.queue.get_nowait()
        out.append((ev.type, dict(ev.payload)))
    return out


def _stream(
    *,
    client: _FakeClient,
    bus: EventBus,
    aconnect: _FakeAconnect,
    reconnect_backoff_seconds: float = 0.001,
    max_reconnect_backoff_seconds: float = 0.01,
) -> LithosEventStream:
    """Build a stream with the fake aconnect injected."""
    return LithosEventStream(
        client=client,
        bus=bus,
        events_url="http://lithos.test/events",
        reconnect_backoff_seconds=reconnect_backoff_seconds,
        max_reconnect_backoff_seconds=max_reconnect_backoff_seconds,
        _aconnect_sse=aconnect,
    )


# ── Bootstrap ───────────────────────────────────────────────────────────


async def test_bootstrap_emits_created_per_open_task() -> None:
    """Cold start: snapshot via task_list → one lithos.task.created per task."""
    bus = EventBus()
    listener = bus.subscribe(event_types=["lithos.task.created"])
    client = _FakeClient(
        bootstrap=[_task("a"), _task("b"), _task("c")],
    )
    aconnect = _FakeAconnect(connections=[[]])  # immediate clean EOF on stream
    source = _stream(client=client, bus=bus, aconnect=aconnect)

    task = asyncio.create_task(source.run())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    types = [t for t, _ in _drain(listener)]
    assert types[:3] == [
        "lithos.task.created",
        "lithos.task.created",
        "lithos.task.created",
    ]
    assert client.task_list_calls == [{"status": "open", "with_claims": True}]


async def test_bootstrap_payload_matches_poller_shape() -> None:
    """Bootstrap-emitted events carry the full Task payload shape.

    Same six keys the poller publishes (id, title, status, tags,
    metadata, claims) so the RouteRunner contract is preserved across
    the source swap.
    """
    bus = EventBus()
    listener = bus.subscribe(event_types=["lithos.task.created"])
    client = _FakeClient(
        bootstrap=[
            _task(
                "abc",
                tags=("trigger:test",),
                metadata={"depends_on": ["x"]},
                title="bootstrap task",
            )
        ],
    )
    aconnect = _FakeAconnect(connections=[[]])
    source = _stream(client=client, bus=bus, aconnect=aconnect)

    task = asyncio.create_task(source.run())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    drained = _drain(listener)
    assert len(drained) == 1
    _, payload = drained[0]
    assert payload == {
        "id": "abc",
        "title": "bootstrap task",
        "status": "open",
        "tags": ["trigger:test"],
        "metadata": {"depends_on": ["x"]},
        "claims": [],
    }


# ── Stream translation + enrichment ─────────────────────────────────────


async def test_stream_translates_sse_event_type_to_loom_namespace() -> None:
    """Lithos's task.released SSE event → Loom's lithos.task.released bus event."""
    bus = EventBus()
    listener = bus.subscribe(event_types=["lithos.task.released"])
    # Unknown task; enrichment refresh sees it via task_list.
    client = _FakeClient(
        bootstrap=[],
        refresh_responses=[[_task("r1", tags=("trigger:t",))]],
    )
    aconnect = _FakeAconnect(
        connections=[
            [_FakeSse(event="task.released", data={"task_id": "r1"}, id="evt-1")]
        ]
    )
    source = _stream(client=client, bus=bus, aconnect=aconnect)

    task = asyncio.create_task(source.run())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    types = [t for t, _ in _drain(listener)]
    assert types == ["lithos.task.released"]


async def test_stream_enriches_payload_via_task_list_for_unknown_task() -> None:
    """SSE event for a task not seen at bootstrap → refresh via task_list.

    Regression for Copilot review #4: the previous impl used
    task_status which drops tags + metadata, so streamed events would
    publish with empty tags and never match a RouteRunner. task_list
    returns the full Task shape.
    """
    bus = EventBus()
    listener = bus.subscribe(event_types=["lithos.task.created"])
    full = _task(
        "t1",
        status="open",
        tags=("trigger:x",),
        metadata={"depends_on": []},
        title="enriched",
    )
    # bootstrap is empty; the refresh after the SSE event yields the task.
    client = _FakeClient(bootstrap=[], refresh_responses=[[full]])
    aconnect = _FakeAconnect(
        connections=[
            [_FakeSse(event="task.created", data={"task_id": "t1"}, id="evt-1")]
        ]
    )
    source = _stream(client=client, bus=bus, aconnect=aconnect)

    task = asyncio.create_task(source.run())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    drained = _drain(listener)
    assert len(drained) == 1
    _, payload = drained[0]
    assert payload["id"] == "t1"
    assert payload["title"] == "enriched"
    assert payload["status"] == "open"
    assert payload["tags"] == ["trigger:x"]
    assert payload["metadata"] == {"depends_on": []}
    # First task_list call is bootstrap; second is the enrichment refresh.
    assert len(client.task_list_calls) == 2


async def test_stream_uses_cached_task_for_known_task_without_refresh() -> None:
    """Subsequent SSE events for tasks already in cache reuse the cached
    full-shape Task — no extra task_list refresh.

    This keeps the per-event cost down to one MCP call (the bootstrap)
    for the steady-state case where events arrive for tasks already
    known.
    """
    bus = EventBus()
    # Subscribe to both so we see the bootstrap-created event too.
    listener = bus.subscribe(
        event_types=["lithos.task.created", "lithos.task.released"]
    )
    full = _task("t1", status="open", tags=("trigger:x",), title="known")
    client = _FakeClient(bootstrap=[full])
    aconnect = _FakeAconnect(
        connections=[
            [_FakeSse(event="task.released", data={"task_id": "t1"}, id="evt-1")]
        ]
    )
    source = _stream(client=client, bus=bus, aconnect=aconnect)

    task = asyncio.create_task(source.run())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    drained = _drain(listener)
    # Bootstrap created event + released event.
    assert len(drained) == 2
    assert drained[0][0] == "lithos.task.created"
    assert drained[1][0] == "lithos.task.released"
    assert drained[1][1]["tags"] == ["trigger:x"]
    # Only bootstrap task_list — no extra refresh for the known task.
    assert len(client.task_list_calls) == 1


async def test_stream_propagates_task_list_errors_so_event_is_replayed() -> None:
    """Regression for Copilot review #3: a transient task_list failure
    during enrichment must NOT acknowledge the SSE event. The error
    should propagate so the reconnect loop replays the same event with
    the unchanged Last-Event-ID.
    """
    bus = EventBus()
    listener = bus.subscribe(event_types=["lithos.task.created"])
    full = _task("t1", tags=("trigger:x",))
    # First refresh raises (transient); second succeeds with the task.
    client = _FakeClient(
        bootstrap=[],
        refresh_responses=[RuntimeError("transient blip"), [full]],
    )
    aconnect = _FakeAconnect(
        connections=[
            # First connection: yields one event, then raises (mirroring
            # what happens when _enrich raises mid-iteration).
            [_FakeSse(event="task.created", data={"task_id": "t1"}, id="evt-1")],
            # Second connection: replay yields the same event again.
            [_FakeSse(event="task.created", data={"task_id": "t1"}, id="evt-1")],
        ]
    )
    source = _stream(client=client, bus=bus, aconnect=aconnect)

    task = asyncio.create_task(source.run())
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # The event was eventually published (second attempt succeeded).
    drained = _drain(listener)
    assert any(t == "lithos.task.created" for t, _ in drained)
    # Reconnect happened with no Last-Event-ID advance — the first
    # connect set Last-Event-ID="evt-1" only AFTER the publish, which
    # we never reached. So second connect must NOT carry it.
    assert "Last-Event-ID" not in aconnect.calls[1]["headers"]


async def test_stream_uses_cached_snapshot_for_terminal_event_on_known_task() -> None:
    """For terminal events on a task we knew during bootstrap, use cached snapshot.

    Lithos's task_list(status="open") won't return a task that's just
    completed, so the refresh path can't enrich. But because the task
    was open at bootstrap time, the cached entry carries the tags +
    metadata we need to route on. The SSE event's terminal status
    overrides the cached "open" so subscribers see the canonical state.
    """
    bus = EventBus()
    listener = bus.subscribe(event_types=["lithos.task.completed"])
    known = _task("done", tags=("trigger:t",), title="finished task")
    # Bootstrap returns the known task (initially open). Refresh sees an
    # empty list (the task has since left the open set).
    client = _FakeClient(bootstrap=[known], refresh_responses=[[]])
    aconnect = _FakeAconnect(
        connections=[
            [_FakeSse(event="task.completed", data={"task_id": "done"}, id="evt-1")]
        ]
    )
    source = _stream(client=client, bus=bus, aconnect=aconnect)

    task = asyncio.create_task(source.run())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    drained = _drain(listener)
    assert len(drained) == 1
    _, payload = drained[0]
    assert payload["id"] == "done"
    assert payload["title"] == "finished task"
    assert payload["tags"] == ["trigger:t"]
    # Status overridden to completed even though snapshot had "open" —
    # the SSE event carries the canonical terminal state.
    assert payload["status"] == "completed"


async def test_stream_skips_unknown_task_when_not_in_bootstrap_or_refresh() -> None:
    """SSE event for a task absent from cache AND refresh result is skipped."""
    bus = EventBus()
    listener = bus.subscribe(
        event_types=[
            "lithos.task.created",
            "lithos.task.updated",
            "lithos.task.claimed",
            "lithos.task.released",
            "lithos.task.completed",
            "lithos.task.cancelled",
        ]
    )
    # Empty bootstrap + empty refresh → enrichment yields nothing.
    client = _FakeClient(bootstrap=[], refresh_responses=[[]])
    aconnect = _FakeAconnect(
        connections=[
            [_FakeSse(event="task.created", data={"task_id": "ghost"}, id="evt-1")]
        ]
    )
    source = _stream(client=client, bus=bus, aconnect=aconnect)

    task = asyncio.create_task(source.run())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert _drain(listener) == []


async def test_stream_ignores_non_task_event_types() -> None:
    """Source filters by ``?types=task.*`` server-side; if a stray event leaks
    through (e.g., upstream config drift), we drop it locally rather than
    crash on the unknown shape."""
    bus = EventBus()
    listener = bus.subscribe(
        event_types=[
            "lithos.task.created",
            "lithos.task.updated",
            "lithos.task.claimed",
            "lithos.task.released",
            "lithos.task.completed",
            "lithos.task.cancelled",
        ]
    )
    client = _FakeClient(bootstrap=[])
    aconnect = _FakeAconnect(
        connections=[
            [_FakeSse(event="note.created", data={"note_id": "n1"}, id="evt-1")]
        ]
    )
    source = _stream(client=client, bus=bus, aconnect=aconnect)

    task = asyncio.create_task(source.run())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert _drain(listener) == []
    # And no enrichment was attempted for the non-task event (only the
    # bootstrap task_list, no follow-up refresh).
    assert len(client.task_list_calls) == 1


# ── Reconnect + replay ──────────────────────────────────────────────────


async def test_stream_reconnects_with_last_event_id_after_transient_error() -> None:
    """On disconnect, the next connect carries Last-Event-ID for ring-buffer replay."""
    bus = EventBus()
    bus.subscribe(event_types=["lithos.task.created"])  # passive consumer
    client = _FakeClient(
        bootstrap=[],
        # Two enrichment refreshes — one per SSE event.
        refresh_responses=[[_task("t1")], [_task("t2")]],
    )
    aconnect = _FakeAconnect(
        connections=[
            # First connection: one event then drops.
            [
                _FakeSse(event="task.created", data={"task_id": "t1"}, id="evt-1"),
                ConnectionError("simulated mid-stream drop"),
            ],
            # Second connection: another event, then immediate clean EOF.
            [_FakeSse(event="task.created", data={"task_id": "t2"}, id="evt-2")],
        ]
    )
    source = _stream(client=client, bus=bus, aconnect=aconnect)

    task = asyncio.create_task(source.run())
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(aconnect.calls) >= 2
    # First connect: no Last-Event-ID header.
    assert "Last-Event-ID" not in aconnect.calls[0]["headers"]
    # Second connect: Last-Event-ID set from the last successfully-processed event.
    assert aconnect.calls[1]["headers"].get("Last-Event-ID") == "evt-1"


async def test_stream_reconnect_backoff_grows_then_caps() -> None:
    """Repeated connection failures back off exponentially up to the cap.

    Captures the sleep durations the stream requests so we can assert the
    sequence without burning real wall-clock time.
    """
    sleep_calls: list[float] = []
    original_sleep = asyncio.sleep

    async def _record_sleep(delay: float) -> None:
        sleep_calls.append(delay)
        # Use a tiny real sleep so the loop keeps making progress.
        await original_sleep(0)

    bus = EventBus()
    client = _FakeClient(bootstrap=[])
    aconnect = _FakeAconnect(
        connections=[
            ConnectionError("boom 1"),
            ConnectionError("boom 2"),
            ConnectionError("boom 3"),
            ConnectionError("boom 4"),
            ConnectionError("boom 5"),
            [],  # finally a clean connection that yields nothing
        ]
    )
    source = LithosEventStream(
        client=client,
        bus=bus,
        events_url="http://lithos.test/events",
        reconnect_backoff_seconds=1.0,
        max_reconnect_backoff_seconds=4.0,
        _aconnect_sse=aconnect,
    )

    # Patch the module-level asyncio.sleep that the stream uses for backoff.
    import lithos_loom.sources.lithos_event_stream as mod

    mod_sleep_orig = mod.asyncio.sleep
    mod.asyncio.sleep = _record_sleep  # type: ignore[assignment]
    try:
        task = asyncio.create_task(source.run())
        await original_sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        mod.asyncio.sleep = mod_sleep_orig  # type: ignore[assignment]

    # Doubling sequence starting at 1.0, capped at 4.0: 1, 2, 4, 4, 4.
    assert sleep_calls[:5] == [1.0, 2.0, 4.0, 4.0, 4.0]


async def test_stream_cancellable_during_event_iteration() -> None:
    """``task.cancel()`` on a stream sitting in aiter_sse exits via CancelledError."""

    class _BlockingEventSource:
        async def aiter_sse(self) -> AsyncIterator[_FakeSse]:
            await asyncio.sleep(3600)  # park; cancellation should unwind
            if False:  # pragma: no cover — keeps mypy/yield-typing happy
                yield  # type: ignore[unreachable]

    class _BlockingAconnect:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(self, *args: Any, **kwargs: Any) -> _BlockingAconnect._Ctx:
            self.calls += 1
            return _BlockingAconnect._Ctx()

        class _Ctx:
            async def __aenter__(self) -> _BlockingEventSource:
                return _BlockingEventSource()

            async def __aexit__(self, *exc: Any) -> None:
                return None

    bus = EventBus()
    client = _FakeClient(bootstrap=[])
    aconnect = _BlockingAconnect()
    source = LithosEventStream(
        client=client,
        bus=bus,
        events_url="http://lithos.test/events",
        _aconnect_sse=aconnect,
    )

    task = asyncio.create_task(source.run())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert aconnect.calls == 1


# ── Wire-level argument contract ────────────────────────────────────────


async def test_stream_subscribes_only_to_task_event_types() -> None:
    """The source filters server-side via ``?types=task.*`` (saves bandwidth + CPU)."""
    bus = EventBus()
    client = _FakeClient(bootstrap=[])
    aconnect = _FakeAconnect(connections=[[]])
    source = _stream(client=client, bus=bus, aconnect=aconnect)

    task = asyncio.create_task(source.run())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    types_filter = aconnect.calls[0]["params"].get("types", "")
    parts = set(types_filter.split(","))
    assert parts == {
        "task.created",
        "task.claimed",
        "task.released",
        "task.completed",
        "task.cancelled",
    }
    assert aconnect.calls[0]["url"] == "http://lithos.test/events"
    assert aconnect.calls[0]["method"] == "GET"


# ── Operator-visibility logging ─────────────────────────────────────────


async def test_stream_logs_info_per_published_event(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Each bus publish emits one INFO log naming the event type + task id.

    Operator visibility regression: without this, the source is silent
    on the success path and the operator can't tell whether the SSE
    channel is actually delivering events.
    """
    import logging

    bus = EventBus()
    bus.subscribe(event_types=["lithos.task.created"])  # passive
    client = _FakeClient(
        bootstrap=[],
        refresh_responses=[[_task("abc-123")]],
    )
    aconnect = _FakeAconnect(
        connections=[
            [_FakeSse(event="task.created", data={"task_id": "abc-123"}, id="e1")]
        ]
    )
    source = _stream(client=client, bus=bus, aconnect=aconnect)

    source_logger = "lithos_loom.sources.lithos_event_stream"
    with caplog.at_level(logging.INFO, logger=source_logger):
        task = asyncio.create_task(source.run())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    publish_logs = [
        r
        for r in caplog.records
        if r.levelno == logging.INFO and "published" in r.getMessage()
    ]
    assert publish_logs, "expected at least one INFO 'published' log"
    msg = publish_logs[0].getMessage()
    assert "lithos.task.created" in msg
    assert "abc-123" in msg


# ── httpx timeout for SSE streaming ─────────────────────────────────────


def test_default_httpx_timeout_disables_read_timeout() -> None:
    """Regression for Copilot review #5: httpx's default 5s read timeout
    is shorter than Lithos's 15s keepalive interval, so an idle stream
    would disconnect every 5s and back off, missing events. The source
    must use a timeout with read disabled (or longer than the keepalive).
    """
    import httpx

    from lithos_loom.sources.lithos_event_stream import _default_httpx_timeout

    timeout = _default_httpx_timeout()
    assert isinstance(timeout, httpx.Timeout)
    # Read timeout disabled (None) → httpx never fires a read-timeout error
    # on an idle SSE stream.
    assert timeout.read is None
    # Connect/write/pool still have sensible bounds.
    assert timeout.connect is not None and timeout.connect > 0


async def test_stream_passes_timeout_to_httpx_client_factory() -> None:
    """The configured timeout is passed to the httpx.AsyncClient factory."""
    import httpx

    bus = EventBus()
    client = _FakeClient(bootstrap=[])
    aconnect = _FakeAconnect(connections=[[]])
    factory_calls: list[dict[str, Any]] = []

    class _SpyClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            factory_calls.append(kwargs)

        async def __aenter__(self) -> _SpyClient:
            return self

        async def __aexit__(self, *exc: Any) -> None:
            return None

    custom_timeout = httpx.Timeout(connect=5.0, read=None, write=5.0, pool=5.0)
    source = LithosEventStream(
        client=client,
        bus=bus,
        events_url="http://lithos.test/events",
        reconnect_backoff_seconds=0.001,
        max_reconnect_backoff_seconds=0.01,
        _aconnect_sse=aconnect,
        _httpx_client_factory=_SpyClient,
        _httpx_timeout=custom_timeout,
    )

    task = asyncio.create_task(source.run())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert factory_calls, "expected at least one client factory invocation"
    assert factory_calls[0].get("timeout") is custom_timeout
