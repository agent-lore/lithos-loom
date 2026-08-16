"""Tests for ``tests.support.async_source.run_until`` (PR #322 review).

The helper exists to make the stream suites deterministic, so its own failure
paths need pinning: "wait for a condition" can hide a regression as easily as
expose one. A version that returned early, or that waited a never-true predicate
away without complaint, would turn every caller green for the wrong reason.
"""

from __future__ import annotations

import asyncio

import pytest

from tests.support import run_until


class _Looper:
    """A source that loops forever, bumping ``ticks``."""

    def __init__(self) -> None:
        self.ticks = 0
        self.cancelled = False

    async def run(self) -> None:
        try:
            while True:
                self.ticks += 1
                await asyncio.sleep(0.001)
        except asyncio.CancelledError:
            self.cancelled = True
            raise


class _Exits:
    """A source whose ``run()`` returns or raises instead of looping."""

    def __init__(self, exc: Exception | None = None) -> None:
        self._exc = exc

    async def run(self) -> None:
        await asyncio.sleep(0)
        if self._exc is not None:
            raise self._exc


async def test_returns_once_the_predicate_holds_and_cancels_the_source() -> None:
    source = _Looper()
    await run_until(source, lambda: source.ticks >= 3, timeout=5.0)
    assert source.ticks >= 3
    assert source.cancelled, "the source task must be cancelled on the way out"


async def test_raises_when_the_predicate_never_holds() -> None:
    # The regression-detection property: a source that never reaches the state
    # must FAIL the test, not be waited away.
    source = _Looper()
    with pytest.raises(AssertionError, match="timed out after .* waiting for ticks"):
        await run_until(source, lambda: False, timeout=0.05, what="ticks")
    assert source.cancelled, "cancelled even on the failure path"


async def test_reports_a_source_that_exits_cleanly_as_itself() -> None:
    # Silent death must not masquerade as a timeout — the message names it, and
    # arrives immediately rather than after the full timeout.
    expected = r"exited before ticks \(returned cleanly\)"
    with pytest.raises(AssertionError, match=expected):
        await run_until(_Exits(), lambda: False, timeout=5.0, what="ticks")


async def test_reports_a_source_that_raises_with_its_exception() -> None:
    with pytest.raises(AssertionError, match="exited before ticks: RuntimeError"):
        await run_until(
            _Exits(RuntimeError("boom")), lambda: False, timeout=5.0, what="ticks"
        )


async def test_a_predicate_already_true_does_no_work() -> None:
    # The task is created but cancelled before it is ever scheduled, so run()
    # never enters — hence no `cancelled` flag to observe. Pinned because the
    # zero-timeout path must not raise the "timed out" AssertionError.
    source = _Looper()
    await run_until(source, lambda: True, timeout=0.0)
    assert source.ticks == 0
