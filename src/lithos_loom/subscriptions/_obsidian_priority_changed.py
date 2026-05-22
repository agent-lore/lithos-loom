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

US22 idempotency: payload-only short-circuit
--------------------------------------------

If the event's ``prior`` equals ``new`` (after coercion), the
handler logs an INFO line and returns without calling Lithos. The
fs-watcher won't naturally emit ``prior == new`` in steady state
(layer-3 diff suppresses it), so this is belt-and-braces against
third-party sources and the degenerate case where the watcher's
``_observed_priorities`` is restored to a value matching the file
on disk.

**Restart-replay limitation.** A full daemon restart drops the
watcher's ``_observed_priorities`` and the projection's
``sync_state.task_priority_markers``. The fs-watcher's first poll
after restart then sees every priority-bearing line as a change
from ``None`` to the parsed enum, and emits one priority_changed
event per such task with ``prior=None, new=<enum>`` — those
events do NOT match the ``prior == new`` short-circuit and so
still produce one ``task_update`` call each. Lithos's additive
merge no-ops these (the patched key already matches), so it's
bounded write traffic, not a bug.

The right fix is upstream: ``lithos_task_status`` currently does
NOT return ``metadata`` (only ``id, title, status, claims``), so
this handler can't pre-check Lithos-side state. An upstream issue
asking to align ``task_status``'s envelope with ``task_list`` is
linked from this PR's description. When that lands, a follow-up
swaps this payload-only check for a strict
``current.metadata.get("priority") == new`` match using a single
``task_status`` RPC.
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

    # US22 payload-only idempotency short-circuit. The fs-watcher's
    # layer-3 diff already suppresses prior==new in steady state, so
    # this is belt-and-braces for third-party producers / degenerate
    # cases. See module docstring for the restart-replay limitation
    # (we can't pre-check Lithos-side metadata because task_status
    # doesn't expose it; upstream alignment issue linked in PR body).
    if prior_str == new_str:
        ctx.logger.info(
            "obsidian-priority-changed: payload prior==new (%s); "
            "skipping idempotent update for task %s",
            prior_str,
            task_id,
        )
        return

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
