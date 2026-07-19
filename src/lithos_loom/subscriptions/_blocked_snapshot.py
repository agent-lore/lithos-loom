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

__all__ = ["BlockedSnapshot"]

# Page size for the per-flush ``task_blocked`` sweep — the projection's own knob,
# deliberately decoupled from the route-runner's ready-frontier limit (US11): the
# blocked set spans every project and is the likeliest of Loom's queries to
# truncate, so it may need raising independently. A full page means the set was
# truncated (see :meth:`BlockedSnapshot.get`).
BLOCKED_QUERY_LIMIT = 500


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

    **Truncation.** ``lithos_task_blocked`` has no per-task filter and no
    cursor, so a full page means the set was cut short and absence from it is
    no longer evidence of anything. This sweep is unnarrowed (the projection
    spans every project), so it is the likeliest of Loom's three to truncate —
    hence the explicit warning below.

    The degradation is chosen deliberately, and *differs* from the runner's.
    There, one direction is plainly unsafe: dispatching a task whose blocker is
    still open violates the dependency, so it defers. Here both directions are
    display errors, so the tiebreak is which one the operator can recover from:

    * unknown → **unblocked**: a blocked task shows, without ⛔, and
      ``include_blocked = false`` fails to hide it. The operator sees work they
      wanted filtered out — visible, obvious, ignorable.
    * unknown → **blocked**: an *actionable* task silently vanishes from the
      vault. Nobody can recover from work they cannot see.

    So the rule is: **hide only on positive evidence of blocked-ness, never on
    inferred unblocked-ness** — and say loudly when the evidence is partial.
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
            blocked = await ctx.lithos.task_blocked(limit=BLOCKED_QUERY_LIMIT)
        except Exception:
            ctx.logger.warning(
                "obsidian-projection: lithos_task_blocked sweep failed; treating "
                "every task as unblocked for this event",
                exc_info=True,
            )
            return {}
        if len(blocked) >= BLOCKED_QUERY_LIMIT:
            # See the class docstring: absence from a truncated page proves
            # nothing, so tasks past the cut render without ⛔ and slip past
            # `include_blocked = false`. That is the recoverable direction, but
            # the operator should know their filter is running on partial data.
            ctx.logger.warning(
                "obsidian-projection: lithos_task_blocked returned a full "
                "%d-task page, so the blocked set is truncated — tasks beyond it "
                "render without ⛔ markers and are not hidden by "
                "include_blocked=false. Raise BLOCKED_QUERY_LIMIT if a blocked set "
                "this large is expected.",
                BLOCKED_QUERY_LIMIT,
            )
        self._snapshot = {bt.task.id: bt.blockers for bt in blocked}
        return self._snapshot

    def invalidate(self) -> None:
        """End the cycle so the next :meth:`get` re-reads Lithos."""
        self._snapshot = None
