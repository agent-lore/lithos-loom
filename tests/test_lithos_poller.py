"""Tests for ``lithos_loom.sources.lithos_poller`` (Slice 0 US3).

The poller fetches Lithos tasks at a configured interval, diffs the
returned list against an in-memory snapshot, and publishes
``lithos.task.*`` events for each transition. Tests inject a fake client
rather than the real ``LithosClient`` so the poller's diff logic is
exercised without an HTTP round trip — see ``test_lithos_client.py`` for
the client surface.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

import pytest

from lithos_loom.bus import EventBus, Subscription
from lithos_loom.lithos_client import Task
from lithos_loom.sources.lithos_poller import LithosPoller

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


class FakePoller:
    """Records the script of polls. Each ``task_list`` call dequeues the
    next entry; entries can be either a ``list[Task]`` (success) or an
    ``Exception`` (raised). When the script is exhausted the next call
    returns ``[]``.
    """

    def __init__(self, polls: list[list[Task] | Exception]) -> None:
        self._polls = list(polls)
        self.calls: list[dict[str, Any]] = []

    async def task_list(
        self,
        *,
        status: str | None = None,
        with_claims: bool = False,
    ) -> list[Task]:
        self.calls.append({"status": status, "with_claims": with_claims})
        if not self._polls:
            return []
        nxt = self._polls.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


def _drain(sub: Subscription) -> list[str]:
    """Drain a subscription's queue to a list of event types (in order)."""
    out: list[str] = []
    while not sub.queue.empty():
        out.append(sub.queue.get_nowait().type)
    return out


# ── Per-tick diff/emit semantics ────────────────────────────────────────


async def test_poll_once_first_tick_emits_created_for_open_tasks() -> None:
    bus = EventBus()
    listener = bus.subscribe(
        event_types=[
            "lithos.task.created",
            "lithos.task.updated",
            "lithos.task.completed",
            "lithos.task.cancelled",
            "lithos.task.claimed",
            "lithos.task.released",
        ]
    )
    client = FakePoller([[_task("a"), _task("b"), _task("c", status="completed")]])
    poller = LithosPoller(client=client, bus=bus, interval=0.0)

    await poller.poll_once()

    # Open tasks emit created. Completed-on-first-sight is snapshotted but
    # not surfaced as a created event — it's an old terminal state.
    assert _drain(listener) == ["lithos.task.created", "lithos.task.created"]


async def test_poll_once_emits_updated_when_tags_change() -> None:
    bus = EventBus()
    listener = bus.subscribe(event_types=["lithos.task.updated"])
    client = FakePoller(
        [
            [_task("a", tags=("v1",))],
            [_task("a", tags=("v1", "v2"))],
        ]
    )
    poller = LithosPoller(client=client, bus=bus, interval=0.0)

    await poller.poll_once()
    await poller.poll_once()

    assert _drain(listener) == ["lithos.task.updated"]


async def test_poll_once_emits_completed_on_status_transition() -> None:
    bus = EventBus()
    listener = bus.subscribe(event_types=["lithos.task.completed"])
    client = FakePoller(
        [
            [_task("a", status="open")],
            [_task("a", status="completed")],
        ]
    )
    poller = LithosPoller(client=client, bus=bus, interval=0.0)
    await poller.poll_once()
    await poller.poll_once()
    assert _drain(listener) == ["lithos.task.completed"]


async def test_poll_once_emits_cancelled_on_status_transition() -> None:
    bus = EventBus()
    listener = bus.subscribe(event_types=["lithos.task.cancelled"])
    client = FakePoller(
        [
            [_task("a", status="open")],
            [_task("a", status="cancelled")],
        ]
    )
    poller = LithosPoller(client=client, bus=bus, interval=0.0)
    await poller.poll_once()
    await poller.poll_once()
    assert _drain(listener) == ["lithos.task.cancelled"]


async def test_poll_once_emits_claimed_when_claim_appears() -> None:
    bus = EventBus()
    listener = bus.subscribe(event_types=["lithos.task.claimed"])
    claim = {"agent": "a1", "aspect": "impl", "expires_at": "2026-01-01T00:00:00Z"}
    client = FakePoller(
        [
            [_task("a", claims=())],
            [_task("a", claims=(claim,))],
        ]
    )
    poller = LithosPoller(client=client, bus=bus, interval=0.0)
    await poller.poll_once()
    await poller.poll_once()
    assert _drain(listener) == ["lithos.task.claimed"]


async def test_poll_once_emits_released_when_claim_disappears() -> None:
    bus = EventBus()
    listener = bus.subscribe(event_types=["lithos.task.released"])
    claim = {"agent": "a1", "aspect": "impl", "expires_at": "2026-01-01T00:00:00Z"}
    client = FakePoller(
        [
            [_task("a", claims=(claim,))],
            [_task("a", claims=())],
        ]
    )
    poller = LithosPoller(client=client, bus=bus, interval=0.0)
    await poller.poll_once()
    await poller.poll_once()
    assert _drain(listener) == ["lithos.task.released"]


async def test_poll_once_skips_unchanged_tasks() -> None:
    bus = EventBus()
    listener = bus.subscribe(
        event_types=[
            "lithos.task.created",
            "lithos.task.updated",
            "lithos.task.completed",
            "lithos.task.cancelled",
            "lithos.task.claimed",
            "lithos.task.released",
        ]
    )
    same = _task("a", tags=("x",))
    client = FakePoller([[same], [same]])
    poller = LithosPoller(client=client, bus=bus, interval=0.0)

    await poller.poll_once()  # created
    pre_drain = _drain(listener)
    await poller.poll_once()  # nothing should happen
    post_drain = _drain(listener)

    assert pre_drain == ["lithos.task.created"]
    assert post_drain == []


async def test_poll_once_passes_with_claims_true_to_client() -> None:
    bus = EventBus()
    client = FakePoller([[]])
    poller = LithosPoller(client=client, bus=bus, interval=0.0)
    await poller.poll_once()
    assert client.calls[0]["with_claims"] is True


# ── run() lifecycle ─────────────────────────────────────────────────────


async def test_run_loops_until_cancelled() -> None:
    bus = EventBus()
    listener = bus.subscribe(event_types=["lithos.task.created"])
    client = FakePoller([[_task("a")], [_task("b")], [_task("c")]])
    poller = LithosPoller(client=client, bus=bus, interval=0.001)

    task = asyncio.create_task(poller.run())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # At least two polls should have happened in 50ms.
    types = _drain(listener)
    assert len(types) >= 2
    assert all(t == "lithos.task.created" for t in types)


async def test_run_continues_through_transient_client_error() -> None:
    bus = EventBus()
    listener = bus.subscribe(event_types=["lithos.task.created"])
    client = FakePoller(
        [
            RuntimeError("transient network blip"),
            [_task("a")],
        ]
    )
    poller = LithosPoller(client=client, bus=bus, interval=0.001)

    task = asyncio.create_task(poller.run())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # The poller recovered from the first poll's error and emitted created
    # for "a" on a later iteration.
    assert "lithos.task.created" in _drain(listener)
