"""The runner's readiness re-check (PR #352 review round 2).

A re-check is the only in-process retry an undetermined (or unreadable)
readiness has, so it must be one sleeper per task, spend its budget when it
actually re-asks, retry its own read failures, and start fresh after any
definitive answer.
"""

from __future__ import annotations

import asyncio

import pytest

from lithos_loom.bus import EventBus
from lithos_loom.errors import LithosClientError
from lithos_loom.subscriptions import ready_recheck as rr
from tests.support import FakeLithosClient, make_task


async def _drain(seconds: float = 0.05) -> None:
    await asyncio.sleep(seconds)


async def test_one_sleeper_per_task_and_budget_spent_per_recheck(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(rr, "READY_RECHECK_SECONDS", 0.01)
    monkeypatch.setattr(rr, "READY_RECHECK_MAX", 2)
    client = FakeLithosClient()
    client.add_task(make_task("t1", status="open"))
    bus = EventBus()
    sub = bus.subscribe(event_types=["lithos.task.updated"], name="probe")
    checker = rr.ReadyRechecker(bus=bus, lithos=client, route="r")

    # ten duplicate events for one undetermined task: ONE sleeper
    for _ in range(10):
        assert checker.schedule("t1") is True
    assert checker.pending("t1") is True and checker.pending_count() == 1
    await _drain()
    assert sub.queue.qsize() == 1  # one republish
    assert checker.pending("t1") is False
    # the budget was spent by the re-check that ran, not by the ten events
    assert checker.schedule("t1") is True
    await _drain()
    assert sub.queue.qsize() == 2
    assert checker.schedule("t1") is False  # the bound (2) is spent now


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

    assert checker.schedule("t1") is True
    await _drain(0.1)
    assert calls["n"] == 2 and sub.queue.qsize() == 1


async def test_settled_resets_the_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rr, "READY_RECHECK_SECONDS", 0.01)
    monkeypatch.setattr(rr, "READY_RECHECK_MAX", 1)
    client = FakeLithosClient()
    client.add_task(make_task("t1", status="open"))
    checker = rr.ReadyRechecker(bus=EventBus(), lithos=client, route="r")
    assert checker.schedule("t1") is True
    await _drain()
    assert checker.schedule("t1") is False
    checker.settled("t1")  # a definitive answer — any of True / False / gone
    assert checker.schedule("t1") is True
