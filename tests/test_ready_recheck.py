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
    assert sub.queue.qsize() == 1  # one republish
    assert checker.pending("t1") is False
    assert checker.attempts("t1") == 1  # spent by the re-check that ran
    # PR #352 review round 3: never a terminal refusal — the retry stays
    # live, with backoff, until a definitive answer
    for _ in range(30):
        checker.schedule("t1")
        await _drain()
    assert checker.attempts("t1") == 31 and sub.queue.qsize() == 31


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
    assert calls["n"] == 2 and sub.queue.qsize() == 1


async def test_settled_resets_the_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rr, "READY_RECHECK_SECONDS", 0.01)
    monkeypatch.setattr(rr, "READY_RECHECK_MAX_SECONDS", 0.01)
    client = FakeLithosClient()
    client.add_task(make_task("t1", status="open"))
    checker = rr.ReadyRechecker(bus=EventBus(), lithos=client, route="r")
    checker.schedule("t1")
    await _drain()
    checker.schedule("t1")
    await _drain()
    assert checker.attempts("t1") == 2
    checker.settled("t1")  # a definitive answer — any of True / False / gone
    assert checker.attempts("t1") == 0 and checker.pending("t1") is False
