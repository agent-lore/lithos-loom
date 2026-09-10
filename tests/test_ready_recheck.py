"""The runner's readiness re-check (PR #352 review round 2).

A re-check is the only in-process retry an undetermined (or unreadable)
readiness has, so it must be one sleeper per task, spend its budget when it
actually re-asks, retry its own read failures, and start fresh after any
definitive answer.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from lithos_loom.bus import Event, EventBus
from lithos_loom.errors import LithosClientError
from lithos_loom.subscriptions import ready_recheck as rr
from tests.support import FakeLithosClient, make_task


async def _drain(seconds: float = 0.05) -> None:
    await asyncio.sleep(seconds)


async def test_one_sleeper_per_task_and_attempts_count_runs_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(rr, "READY_RECHECK_SECONDS", 0.01)
    monkeypatch.setattr(rr, "READY_RECHECK_MAX_SECONDS", 0.01)
    client = FakeLithosClient()
    client.add_task(make_task("t1", status="open"))
    bus = EventBus()
    sub = bus.subscribe(event_types=["lithos.task.updated"], name="probe")
    checker = rr.ReadyRechecker(bus=bus, lithos=client, route="r")

    # ten duplicate events for one undetermined task: ONE sleeper
    for _ in range(10):
        checker.schedule("t1")
    assert checker.pending("t1") is True and checker.pending_count() == 1
    await _drain()
    assert sub.queue.qsize() >= 1  # republished…
    assert checker.attempts("t1") >= 1  # …spent by re-checks that RAN
    # PR #352 review rounds 3 + 4: never a terminal refusal, and the sleeper
    # is re-armed after each republish — the retry stays live until settled
    assert checker.pending("t1") is True
    for _ in range(30):
        checker.schedule("t1")
    assert checker.pending_count() == 1
    checker.settled("t1")
    assert checker.pending("t1") is False and checker.attempts("t1") == 0


def test_backoff_grows_and_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rr, "READY_RECHECK_SECONDS", 60.0)
    monkeypatch.setattr(rr, "READY_RECHECK_MAX_SECONDS", 900.0)
    assert [rr.delay_for(n) for n in (0, 1, 2, 3, 4, 5, 50)] == [
        60.0,
        120.0,
        240.0,
        480.0,
        900.0,
        900.0,
        900.0,
    ]
    # a task stuck for days: the exponent must not overflow a float
    assert rr.delay_for(5000) == 900.0


async def test_a_failed_recheck_read_retries_itself(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(rr, "READY_RECHECK_SECONDS", 0.01)
    client = FakeLithosClient()
    client.add_task(make_task("t1", status="open"))
    real_get = client.task_get
    calls = {"n": 0}

    async def flaky(*, task_id: str):
        calls["n"] += 1
        if calls["n"] == 1:
            raise LithosClientError("server_error", "boom")
        return await real_get(task_id=task_id)

    client.task_get = flaky  # type: ignore[method-assign]
    bus = EventBus()
    sub = bus.subscribe(event_types=["lithos.task.updated"], name="probe")
    checker = rr.ReadyRechecker(bus=bus, lithos=client, route="r")

    checker.schedule("t1")
    await _drain(0.1)
    checker.settled("t1")
    assert calls["n"] >= 2 and sub.queue.qsize() >= 1  # the failed read retried


async def test_settled_resets_the_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rr, "READY_RECHECK_SECONDS", 0.01)
    monkeypatch.setattr(rr, "READY_RECHECK_MAX_SECONDS", 0.01)
    client = FakeLithosClient()
    client.add_task(make_task("t1", status="open"))
    checker = rr.ReadyRechecker(bus=EventBus(), lithos=client, route="r")
    checker.schedule("t1")
    await _drain()
    assert checker.attempts("t1") >= 1 and checker.pending("t1") is True
    checker.settled("t1")  # a definitive answer — any of True / False / gone
    assert checker.attempts("t1") == 0 and checker.pending("t1") is False


async def test_a_dropped_republish_keeps_the_retry_armed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PR #352 review round 4: the bus is fire-and-forget — a full route queue
    drops the republish and says nothing. The rechecker must not equate
    "published" with "delivered": it stays armed until the runner's
    definitive answer (`settled()`), so a dropped event just fires again."""
    monkeypatch.setattr(rr, "READY_RECHECK_SECONDS", 0.01)
    monkeypatch.setattr(rr, "READY_RECHECK_MAX_SECONDS", 0.01)
    client = FakeLithosClient()
    client.add_task(make_task("t1", status="open"))
    bus = EventBus()
    sub = bus.subscribe(event_types=["lithos.task.updated"], name="route", queue_size=1)
    filler = Event(
        type="lithos.task.updated",
        timestamp=datetime.now(UTC),
        payload={"id": "filler", "status": "open"},
    )
    await bus.publish(filler)  # the route's queue is full before the retry fires
    checker = rr.ReadyRechecker(bus=bus, lithos=client, route="r")

    checker.schedule("t1")
    await _drain()
    assert sub.drop_count >= 1  # the republish was dropped…
    assert checker.pending("t1") is True  # …and the retry is still armed

    sub.queue.get_nowait()  # the runner drains the backlog
    await _drain(0.1)
    ids = []
    while not sub.queue.empty():
        ids.append(sub.queue.get_nowait().payload["id"])
    assert "t1" in ids  # re-emitted without a restart
    assert checker.pending("t1") is True  # and STILL armed: only settled() ends it
    checker.settled("t1")
    assert checker.pending("t1") is False


async def test_a_gone_task_ends_the_retry_cleanly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A re-check that finds the task gone or terminal is a definitive
    answer: no republish, nothing re-armed, and no stale pending entry left
    behind (the self-review's find after the re-arm change)."""
    monkeypatch.setattr(rr, "READY_RECHECK_SECONDS", 0.01)
    client = FakeLithosClient()  # t1 does not exist
    bus = EventBus()
    sub = bus.subscribe(event_types=["lithos.task.updated"], name="probe")
    checker = rr.ReadyRechecker(bus=bus, lithos=client, route="r")
    checker.schedule("t1")
    await _drain()
    assert sub.queue.qsize() == 0
    assert checker.pending("t1") is False and checker.pending_count() == 0
    assert checker.attempts("t1") == 0
