"""The drain primitive the github-watcher's dispatchers share (#407 slice 3).

``lithos-loom drain`` asks a child to stop admitting new dispatch, finish
what is in flight and exit. A dispatcher implements that as a one-way
``begin_drain()`` flag its decision paths read before any reservation, plus
a ``drained()`` that returns once nothing it owns is running. "Running"
covers a dispatch that has passed the flag check but not yet started its
run task — the reservation write is an await the drain can begin under —
so each dispatcher counts those too (:class:`DispatchGuard`); otherwise the
child could exit with a stamped round and no run, which the next boot would
have to refund.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

__all__ = ["DRAIN_POLL_SECONDS", "DrainState", "wait_idle"]

DRAIN_POLL_SECONDS = 0.2


class DrainState:
    """The draining flag plus the count of dispatches committing right now."""

    def __init__(self) -> None:
        self.draining = False
        self._committing = 0

    def begin(self) -> bool:
        """Flip to draining; True the first time (so the caller logs once)."""
        first = not self.draining
        self.draining = True
        return first

    @property
    def committing(self) -> bool:
        return self._committing > 0

    def enter(self) -> None:
        self._committing += 1

    def leave(self) -> None:
        self._committing -= 1


async def wait_idle(
    is_busy: Callable[[], bool], *, poll: float = DRAIN_POLL_SECONDS
) -> None:
    """Return once ``is_busy()`` is false (at once when it already is)."""
    while is_busy():
        await asyncio.sleep(poll)
