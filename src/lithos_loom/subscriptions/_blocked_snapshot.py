"""Per-flush-cycle cache of Lithos's blocked set (Epic G / US8).

The Obsidian projection needs "is this task blocked, and by what?" for every
event it renders — the ``include_blocked`` gate and the ``⛔`` markers both
read it. Blocked-ness is a server-side query (``lithos_task_blocked``) with no
per-task filter, so this batches it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from lithos_loom.lithos_client import Blocker
from lithos_loom.subscriptions.route_runner import READY_QUERY_LIMIT

__all__ = ["BlockedSnapshot"]


class BlockedSnapshot:
    """Lithos's blocked set for the current projection flush cycle.

    Fetching per event would cost one Lithos round-trip per task on the restart
    bootstrap, which re-emits EVERY open task as ``created``. And every event in
    a burst renders into the same debounced file write anyway, so **one sweep
    per write** is the honest granularity: :meth:`get` fetches on the first call
    of a cycle and reuses that answer until :meth:`invalidate` ends the cycle at
    flush time.

    The trade is bounded staleness: a blocked-ness change landing mid-burst is
    picked up on the next cycle rather than instantly. That is well inside the
    accuracy this replaced — the old ``metadata.depends_on`` mirror showed a ⛔
    for a dependency that had completed months ago, because the list records
    what a task *declared*, not what still holds it.
    """

    def __init__(self) -> None:
        self._snapshot: dict[str, tuple[Blocker, ...]] | None = None

    async def get(self, ctx: Any) -> Mapping[str, tuple[Blocker, ...]]:
        """This cycle's ``{task_id: blockers}``, fetching when the cache is cold.

        A sweep failure degrades to "nothing blocked" rather than propagating:
        the projection is a *display*, and a missing ⛔ beats a handler that
        raises and drops the task's line out of the vault entirely. The failure
        is not cached, so the next event retries.
        """
        if self._snapshot is not None:
            return self._snapshot
        try:
            blocked = await ctx.lithos.task_blocked(limit=READY_QUERY_LIMIT)
        except Exception:
            ctx.logger.warning(
                "obsidian-projection: lithos_task_blocked sweep failed; treating "
                "every task as unblocked for this event",
                exc_info=True,
            )
            return {}
        self._snapshot = {bt.task.id: bt.blockers for bt in blocked}
        return self._snapshot

    def invalidate(self) -> None:
        """End the cycle so the next :meth:`get` re-reads Lithos."""
        self._snapshot = None
