"""``obsidian-due-date-changed`` subscription handler (Slice 3
round-trip).

Consumes ``obsidian.task.due_date_changed`` events emitted by
:class:`~lithos_loom.sources.obsidian_fs_watcher.ObsidianFsWatcher`
when the user edits the ``📅 YYYY-MM-DD`` marker on a projected
line, and pushes the change to Lithos via
``lithos_task_update(task_id, metadata={"scheduled_for": <date>})``.

This is the date-side analogue of
:mod:`._obsidian_priority_changed`. Without it, edits to the date
on a projected line never make it back to Lithos and are silently
overwritten on the next projection rewrite (since the projection
re-renders from Lithos truth).

The handler is **stateless** — mirrors :mod:`._noop`,
:mod:`._obsidian_status_transition`, and
:mod:`._obsidian_priority_changed`. The obsidian-sync child wires
this module's :func:`handle` directly into its ``my_handlers``
dict.

**Date format.** The fs-watcher emits ``prior`` / ``new`` as
``YYYY-MM-DD`` strings (the canonical form the Tasks plugin renders
and parses) or ``None`` for "no 📅 marker on the line". The handler
forwards ``new`` verbatim into the metadata patch; the projection
on the next rewrite reads ``metadata.scheduled_for`` back via
:func:`lithos_loom.render.due_date_str` to render the marker, so
the round-trip is closed under valid inputs.

**Clearing a date.** When the user deletes the marker entirely
(``new=None``), the handler sends
``metadata={"scheduled_for": None}``. Per Lithos's additive-per-key
merge semantics (post lithos#290), a ``null`` value deletes the
key from ``task.metadata``. Other metadata keys (``depends_on``,
``priority``, ``project``, ``story_doc_id``, etc.) are preserved
unconditionally.

Idempotency follows the same three-layer pattern as
:mod:`._obsidian_priority_changed` (see that module's docstring):

1. **Source-side gate** in the fs-watcher (``prior_status is None:
   continue`` blocks the entire layer-3 loop iteration for
   projection-unknown tasks; covers cold-start replay).
2. **Handler-side payload short-circuit** (`prior == new` → no I/O).
3. **Handler-side Lithos strict pre-check** via ``task_get`` →
   skip when ``current.metadata.get("scheduled_for") == new``.
"""

from __future__ import annotations

from lithos_loom.bus import Event
from lithos_loom.subscriptions import SubscriptionContext

__all__ = ["handle"]


async def handle(event: Event, ctx: SubscriptionContext) -> None:
    """Dispatch a single ``obsidian.task.due_date_changed`` event."""
    payload = event.payload
    try:
        task_id = str(payload["task_id"])
        prior = payload["prior"]
        new = payload["new"]
    except (KeyError, TypeError) as exc:
        ctx.logger.warning(
            "obsidian-due-date-changed: malformed payload for %s: %r",
            event.type,
            exc,
        )
        return

    # Source emits prior/new as ``str | None``; coerce defensively in
    # case a third party publishes the event with a non-string value.
    prior_str: str | None = str(prior) if prior is not None else None
    new_str: str | None = str(new) if new is not None else None

    # Layer 2: payload-only short-circuit. Free (no I/O); catches
    # degenerate ``prior == new`` publishes before the Lithos-side
    # pre-check below.
    if prior_str == new_str:
        ctx.logger.info(
            "obsidian-due-date-changed: payload prior==new (%s); "
            "skipping idempotent update for task %s",
            prior_str,
            task_id,
        )
        return

    # Layer 3: Lithos-side strict pre-check. Reads the canonical
    # task and skips when ``metadata.scheduled_for`` already matches
    # ``new`` — catches the case where the watcher emits a genuine
    # prior!=new but Lithos already has the new value (another agent
    # updated it, or sync_state drifted from Lithos truth). Uses
    # ``task_get`` (no claims needed).
    current = await ctx.lithos.task_get(task_id=task_id)
    if current is None:
        ctx.logger.info(
            "obsidian-due-date-changed: task %s not found in Lithos "
            "(possibly deleted); skipping",
            task_id,
        )
        return
    current_due = current.metadata.get("scheduled_for")
    if current_due == new_str:
        ctx.logger.info(
            "obsidian-due-date-changed: task %s already at "
            "scheduled_for=%s; skipping idempotent update",
            task_id,
            new_str,
        )
        return

    # Per-key merge patch. ``None`` deletes ``scheduled_for`` entirely
    # (Lithos JSON-null delete semantics); a YYYY-MM-DD string sets
    # it. Other metadata keys are untouched.
    await ctx.lithos.task_update(
        task_id=task_id,
        agent=ctx.agent_id,
        metadata={"scheduled_for": new_str},
    )
    ctx.logger.info(
        "obsidian-due-date-changed: updated task %s scheduled_for (%s → %s)",
        task_id,
        prior_str,
        new_str,
    )
