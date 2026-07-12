"""Tests for the shared retry primitive.

``lithos_loom.subscriptions.retry.run_with_retry`` is the retry/backoff/
give-up control flow shared by :class:`SubscriptionRunner` and the
github-watcher's ``consume_push`` consumer (#237). These tests pin its
contract directly — the loop had no direct unit test before the
extraction.
"""

from __future__ import annotations

import asyncio

import pytest

from lithos_loom.config import RetryPolicy
from lithos_loom.subscriptions.retry import run_with_retry


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make backoff sleeps instant — the curve is asserted via the
    ``on_attempt_failed`` callback, not by timing real waits."""

    async def _fake(_delay: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", _fake)


async def _noop_give_up(_exc: BaseException | None) -> None:
    return None


async def test_returns_on_first_success_without_calling_back() -> None:
    calls = 0

    async def op() -> None:
        nonlocal calls
        calls += 1

    attempts: list[tuple[int, Exception, float | None]] = []
    gave_up = False

    async def give_up(_exc: BaseException | None) -> None:
        nonlocal gave_up
        gave_up = True

    await run_with_retry(
        op,
        RetryPolicy(attempts=5, initial_delay_seconds=0.0),
        on_attempt_failed=lambda i, e, d: attempts.append((i, e, d)),
        on_give_up=give_up,
    )

    assert calls == 1
    assert attempts == []
    assert gave_up is False


async def test_retries_until_success() -> None:
    calls = 0

    async def op() -> None:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise RuntimeError(f"transient {calls}")

    attempts: list[int] = []
    gave_up = False

    async def give_up(_exc: BaseException | None) -> None:
        nonlocal gave_up
        gave_up = True

    await run_with_retry(
        op,
        RetryPolicy(attempts=5, initial_delay_seconds=0.0),
        on_attempt_failed=lambda i, e, d: attempts.append(i),
        on_give_up=give_up,
    )

    assert calls == 3
    # Two failures before the third-attempt success.
    assert attempts == [0, 1]
    assert gave_up is False


async def test_gives_up_after_exhausting_attempts() -> None:
    calls = 0
    last = RuntimeError("final")

    async def op() -> None:
        nonlocal calls
        calls += 1
        if calls == 4:
            raise last
        raise RuntimeError(f"boom {calls}")

    given: list[BaseException | None] = []

    async def give_up(exc: BaseException | None) -> None:
        given.append(exc)

    await run_with_retry(
        op,
        RetryPolicy(attempts=4, initial_delay_seconds=0.0),
        on_give_up=give_up,
    )

    assert calls == 4  # every attempt used
    # give_up called exactly once with the *last* exception.
    assert given == [last]


async def test_handler_exception_is_never_reraised() -> None:
    async def op() -> None:
        raise RuntimeError("always")

    # No exception escapes — drop-and-continue is the terminal behaviour.
    await run_with_retry(
        op,
        RetryPolicy(attempts=2, initial_delay_seconds=0.0),
        on_give_up=_noop_give_up,
    )


async def test_cancelled_error_propagates_and_is_not_retried() -> None:
    calls = 0
    gave_up = False

    async def op() -> None:
        nonlocal calls
        calls += 1
        raise asyncio.CancelledError

    async def give_up(_exc: BaseException | None) -> None:
        nonlocal gave_up
        gave_up = True

    with pytest.raises(asyncio.CancelledError):
        await run_with_retry(
            op,
            RetryPolicy(attempts=5, initial_delay_seconds=0.0),
            on_give_up=give_up,
        )

    assert calls == 1  # cancellation aborts immediately
    assert gave_up is False  # give-up never runs on cancellation


async def test_on_attempt_failed_next_delay_is_none_on_final_attempt() -> None:
    async def op() -> None:
        raise RuntimeError("boom")

    delays: list[float | None] = []

    await run_with_retry(
        op,
        RetryPolicy(attempts=3, initial_delay_seconds=1.0, backoff="exponential"),
        on_attempt_failed=lambda i, e, d: delays.append(d),
        on_give_up=_noop_give_up,
    )

    # attempts 0,1 have a next delay; the final attempt (2) has None.
    assert delays == [1.0, 2.0, None]


async def test_backoff_curve_matches_github_push_policy() -> None:
    """Pins the consume_push docstring's 2,4,8,16,32,60,60 sequence: the
    watcher's hard-coded push backoff is exactly this policy's curve."""

    async def op() -> None:
        raise RuntimeError("boom")

    delays: list[float | None] = []

    push_policy = RetryPolicy(
        attempts=8,
        initial_delay_seconds=2.0,
        max_delay_seconds=60.0,
        backoff="exponential",
    )
    await run_with_retry(
        op,
        push_policy,
        on_attempt_failed=lambda i, e, d: delays.append(d),
        on_give_up=_noop_give_up,
    )

    assert delays == [2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0, None]


async def test_linear_backoff_curve() -> None:
    async def op() -> None:
        raise RuntimeError("boom")

    delays: list[float | None] = []

    await run_with_retry(
        op,
        RetryPolicy(
            attempts=4,
            initial_delay_seconds=5.0,
            max_delay_seconds=12.0,
            backoff="linear",
        ),
        on_attempt_failed=lambda i, e, d: delays.append(d),
        on_give_up=_noop_give_up,
    )

    # linear = initial * (attempt+1), capped at max_delay: 5, 10, 15→12, then None.
    assert delays == [5.0, 10.0, 12.0, None]
