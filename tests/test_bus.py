"""Tests for ``lithos_loom.bus`` (Slice 0 US2).

The EventBus is an in-process pub/sub used inside each supervisor child.
Sources publish typed Events; subscribers receive the ones whose type +
structural filter + optional ``where`` predicate match. Delivery is fire-
and-forget with bounded per-subscriber queues; on overflow the bus drops
the event and bumps a per-subscription counter rather than blocking the
publisher.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import MappingProxyType

import pytest

from lithos_loom.bus import Event, EventBus


def _evt(
    type_: str = "lithos.task.created",
    payload: dict[str, object] | None = None,
) -> Event:
    return Event(
        type=type_,
        timestamp=datetime.now(UTC),
        payload=MappingProxyType(payload or {}),
    )


# ── fan-out + type filtering ────────────────────────────────────────────


async def test_bus_fan_out_delivers_to_all_matching_subscribers() -> None:
    bus = EventBus()
    sub_a = bus.subscribe(event_types=["lithos.task.created"])
    sub_b = bus.subscribe(event_types=["lithos.task.created"])

    event = _evt()
    await bus.publish(event)

    assert sub_a.queue.get_nowait() is event
    assert sub_b.queue.get_nowait() is event
    assert sub_a.queue.empty() and sub_b.queue.empty()


async def test_bus_skips_subscribers_with_unrelated_event_type() -> None:
    bus = EventBus()
    listener = bus.subscribe(event_types=["lithos.task.created"])

    await bus.publish(_evt("obsidian.task.toggled"))

    assert listener.queue.empty()
    assert listener.drop_count == 0  # type-mismatch is not a drop


async def test_bus_subscriber_can_listen_to_multiple_event_types() -> None:
    bus = EventBus()
    listener = bus.subscribe(
        event_types=["lithos.task.created", "lithos.task.completed"]
    )

    await bus.publish(_evt("lithos.task.created"))
    await bus.publish(_evt("lithos.task.completed"))
    await bus.publish(_evt("lithos.task.cancelled"))

    assert listener.queue.qsize() == 2
    types = [listener.queue.get_nowait().type for _ in range(2)]
    assert types == ["lithos.task.created", "lithos.task.completed"]


# ── structural match filter ─────────────────────────────────────────────


async def test_bus_match_table_filters_by_tag_membership() -> None:
    bus = EventBus()
    listener = bus.subscribe(
        event_types=["lithos.task.created"],
        match={"tags": ["trigger:story-implement"]},
    )

    await bus.publish(
        _evt(payload={"tags": ["trigger:story-implement", "priority:high"]})
    )
    await bus.publish(_evt(payload={"tags": ["trigger:prd-decompose"]}))

    assert listener.queue.qsize() == 1
    delivered = listener.queue.get_nowait()
    assert "trigger:story-implement" in delivered.payload["tags"]


async def test_bus_match_scalar_uses_equality() -> None:
    bus = EventBus()
    listener = bus.subscribe(
        event_types=["lithos.task.updated"],
        match={"status": "completed"},
    )

    await bus.publish(_evt("lithos.task.updated", {"status": "completed"}))
    await bus.publish(_evt("lithos.task.updated", {"status": "open"}))

    assert listener.queue.qsize() == 1
    assert listener.queue.get_nowait().payload["status"] == "completed"


async def test_bus_match_nested_table() -> None:
    bus = EventBus()
    listener = bus.subscribe(
        event_types=["lithos.task.updated"],
        match={"metadata": {"project": "lithos-loom"}},
    )

    await bus.publish(
        _evt("lithos.task.updated", {"metadata": {"project": "lithos-loom"}})
    )
    await bus.publish(
        _evt("lithos.task.updated", {"metadata": {"project": "lithos-lens"}})
    )

    assert listener.queue.qsize() == 1


async def test_bus_match_missing_key_means_no_match() -> None:
    bus = EventBus()
    listener = bus.subscribe(
        event_types=["lithos.task.created"],
        match={"tags": ["trigger:story-implement"]},
    )

    await bus.publish(_evt(payload={}))  # no "tags" key at all

    assert listener.queue.empty()


# ── where predicate ─────────────────────────────────────────────────────


async def test_bus_where_predicate_filters_events() -> None:
    bus = EventBus()
    listener = bus.subscribe(
        event_types=["lithos.task.updated"],
        where=lambda e: e.payload.get("priority") == "high",
    )

    await bus.publish(_evt("lithos.task.updated", {"priority": "high"}))
    await bus.publish(_evt("lithos.task.updated", {"priority": "low"}))
    await bus.publish(_evt("lithos.task.updated", {}))

    assert listener.queue.qsize() == 1


async def test_bus_match_and_where_combined_with_and_semantics() -> None:
    bus = EventBus()
    listener = bus.subscribe(
        event_types=["lithos.task.created"],
        match={"tags": ["trigger:story-implement"]},
        where=lambda e: e.payload.get("priority") == "high",
    )

    matching = _evt(payload={"tags": ["trigger:story-implement"], "priority": "high"})
    only_match = _evt(payload={"tags": ["trigger:story-implement"], "priority": "low"})
    only_where = _evt(payload={"tags": ["other"], "priority": "high"})

    await bus.publish(matching)
    await bus.publish(only_match)
    await bus.publish(only_where)

    assert listener.queue.qsize() == 1
    assert listener.queue.get_nowait() is matching


async def test_bus_where_predicate_exception_does_not_break_other_subscribers() -> None:
    """A buggy ``where`` must not poison delivery to siblings (D12 fire-and-forget)."""
    bus = EventBus()

    def kaboom(_: Event) -> bool:
        raise RuntimeError("predicate exploded")

    bad = bus.subscribe(event_types=["lithos.task.created"], where=kaboom)
    good = bus.subscribe(event_types=["lithos.task.created"])

    await bus.publish(_evt())

    assert good.queue.qsize() == 1
    assert bad.queue.empty()


# ── bounded queues + drop counters ──────────────────────────────────────


async def test_bus_drops_events_when_subscriber_queue_is_full() -> None:
    bus = EventBus()
    listener = bus.subscribe(event_types=["lithos.task.created"], queue_size=2)

    for _ in range(5):
        await bus.publish(_evt())

    assert listener.queue.qsize() == 2
    assert listener.drop_count == 3


async def test_bus_drop_count_does_not_advance_for_unmatched_events() -> None:
    bus = EventBus()
    listener = bus.subscribe(event_types=["lithos.task.created"], queue_size=1)

    await bus.publish(_evt("lithos.task.cancelled"))
    await bus.publish(_evt("lithos.task.cancelled"))

    assert listener.drop_count == 0


async def test_bus_publish_does_not_block_on_full_subscriber() -> None:
    """Slow subscriber must not stall publishers (D12 fire-and-forget)."""
    bus = EventBus()
    bus.subscribe(event_types=["lithos.task.created"], queue_size=1)

    async def hammer() -> None:
        for _ in range(100):
            await bus.publish(_evt())

    await asyncio.wait_for(hammer(), timeout=1.0)


# ── event immutability ─────────────────────────────────────────────────


def test_event_is_frozen_dataclass() -> None:
    event = _evt()
    with pytest.raises((AttributeError, Exception)):
        event.type = "mutated"  # type: ignore[misc]


def test_event_payload_can_be_read_only_mapping() -> None:
    """We use MappingProxyType in tests; the bus must not mutate payloads."""
    payload = MappingProxyType({"k": 1})
    event = Event(type="t", timestamp=datetime.now(UTC), payload=payload)
    assert event.payload == {"k": 1}
