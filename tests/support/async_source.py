"""Deterministic driver for the long-running SSE source tests.

Both stream suites (``test_lithos_event_stream``, ``test_lithos_note_stream``)
start a ``source.run()`` task, let it work, then cancel. Where a test asserts on
a state the source reaches **once and then holds**, sampling after a fixed
``asyncio.sleep`` is fine. Where it counts **reconnect cycles**, it is a race:
cycles are paced by the source's retry sleep, so how many fit in a fixed window
is a property of the machine, not of the code — an idle host fits dozens, a
contended CI runner fits one. That is what flaked in CI (PR #322), reproduced at
3/15 under CPU contention against 0/5 idle.

:func:`run_until` waits for the condition instead, which makes the count
deterministic and turns the timeout into a genuine failure bound. It owns its
own failure reporting, so a caller never has to infer "the predicate never came
true" from a downstream ``IndexError`` or a bare count mismatch.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from typing import Any, Protocol


class _Runnable(Protocol):
    def run(self) -> Any: ...


async def run_until(
    source: _Runnable,
    predicate: Callable[[], bool],
    *,
    timeout: float = 5.0,
    what: str = "the expected condition",
) -> None:
    """Run ``source.run()`` until *predicate* holds, then cancel it.

    *predicate* should describe **completed work** — an event on the bus, a
    recorded call — rather than merely entry into the next attempt, or the wait
    can end one step before the behaviour under test actually happens.

    Three outcomes, each reported as itself:

    - predicate holds → cancel and return;
    - ``run()`` exits first → ``AssertionError`` naming it, since a source that
      returns or raises instead of looping is the silent-death bug these suites
      exist to catch, not a timeout;
    - deadline passes → ``AssertionError`` naming *what* was awaited.

    Cancellation is bounded and runs on every path, so a source that stops
    honouring cancellation fails the test rather than hanging the suite.
    """
    task = asyncio.create_task(source.run())
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    try:
        while not predicate():
            if task.done():
                # Retrieve the exception so it is never "never retrieved".
                exc = task.exception()
                raise AssertionError(
                    f"source.run() exited before {what}"
                    + (f": {exc!r}" if exc is not None else " (returned cleanly)")
                )
            if loop.time() >= deadline:
                raise AssertionError(f"timed out after {timeout}s waiting for {what}")
            await asyncio.sleep(0.001)
    finally:
        task.cancel()
        # Cleanup must not change the outcome. Awaiting a task that already
        # failed re-raises ITS exception, which would replace the AssertionError
        # above — the caller would see `RuntimeError: boom` instead of "run()
        # exited before X: RuntimeError('boom')", losing which of the three
        # outcomes occurred. CancelledError is a BaseException, so naming it
        # beside Exception keeps KeyboardInterrupt/SystemExit propagating.
        with contextlib.suppress(Exception, asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=timeout)
