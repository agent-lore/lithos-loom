"""``obsidian-priority-changed`` subscription handler (Slice 2 US21).

Consumes ``obsidian.task.priority_changed`` events emitted by
:class:`~lithos_loom.sources.obsidian_fs_watcher.ObsidianFsWatcher`
when the user edits the priority emoji on a projected line, and
pushes the change to Lithos via
``lithos_task_update(task_id, metadata={"priority": <enum>})``.

The handler is **stateless** — mirrors :mod:`._noop` and
:mod:`._obsidian_status_transition`. The obsidian-sync child wires
this module's :func:`handle` directly into its ``my_handlers`` dict.

**Priority enum** (D18):
``"highest"`` / ``"high"`` / ``"medium"`` / ``"low"`` / ``"lowest"``
or ``None`` for "no priority". The fs watcher emits ``prior`` and
``new`` as enum strings (not emoji literals), so the handler doesn't
need the emoji-to-enum mapping; it just forwards ``new`` into the
metadata patch.

**Clearing a priority.** When the user deletes the emoji entirely
(``new=None``), the handler sends ``metadata={"priority": None}``.
Per Lithos's additive-per-key merge semantics (spec §5.4, post
lithos#290), a ``null`` value deletes the key from
``task.metadata``. Other metadata keys (``depends_on``,
``scheduled_for``, ``story_doc_id``, etc.) are preserved
unconditionally.

Idempotency is **not** enforced here. US22 will add a pre-check via
``lithos_task_status`` — until then, ``task_update`` on a task
whose ``metadata.priority`` already matches is a Lithos-side
no-op (the merge runs but produces identical state).
"""

from __future__ import annotations

from lithos_loom.bus import Event
from lithos_loom.subscriptions import SubscriptionContext

__all__ = ["handle"]


async def handle(event: Event, ctx: SubscriptionContext) -> None:
    """Dispatch a single ``obsidian.task.priority_changed`` event."""
    payload = event.payload
    try:
        task_id = str(payload["task_id"])
        prior = payload["prior"]
        new = payload["new"]
    except (KeyError, TypeError) as exc:
        ctx.logger.warning(
            "obsidian-priority-changed: malformed payload for %s: %r",
            event.type,
            exc,
        )
        return

    # Source emits prior/new as ``str | None``; coerce defensively in
    # case a third party publishes the event with a non-string value.
    prior_str: str | None = str(prior) if prior is not None else None
    new_str: str | None = str(new) if new is not None else None

    # Per-key merge patch. ``None`` deletes the priority key entirely
    # (Lithos JSON-null delete semantics); a string value sets it.
    # Other metadata keys are untouched.
    await ctx.lithos.task_update(
        task_id=task_id,
        agent=ctx.agent_id,
        metadata={"priority": new_str},
    )
    ctx.logger.info(
        "obsidian-priority-changed: updated task %s priority (%s → %s)",
        task_id,
        prior_str,
        new_str,
    )
