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

US22 idempotency
----------------

US22 requires re-firing for an unchanged priority to be a no-op so
source-replay on restart is safe. This is delivered jointly by the
fs-watcher and this handler:

1. **Source-side (load-bearing).** The watcher's poll loop gates
   every emission on ``prior_status is None: continue`` (see
   ``obsidian_fs_watcher.py``: layer-3 loop). On a cold-start
   restart, both ``_observed_priorities`` and
   ``sync_state.task_priority_markers`` are empty for every task,
   so the status-side ``prior_status is None`` check fires first
   and the ``continue`` short-circuits the entire loop iteration —
   including the priority diff below. No priority_changed events
   are emitted on cold start. (Regression test:
   ``test_cold_start_restart_with_unchanged_file_emits_nothing``.)

2. **Handler-side (belt-and-braces).** If a third-party producer
   ever publishes a priority_changed event with ``prior == new``
   (or the degenerate ``None → None`` case), the payload-only
   short-circuit below catches it. The fs-watcher itself won't
   emit such an event in steady state, but the architecture allows
   for additional sources, so the handler enforces the invariant
   too.

A future upstream extension exposing ``metadata`` on
``lithos_task_status`` (currently only ``id, title, status,
claims``) would let the handler compare strictly against
Lithos-side state — useful as a tightening but not required for
US22 compliance, which the source-side gate already satisfies.
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
    # projection-known gate ("prior_status is None: continue") is the
    # load-bearing US22 mechanism — it ensures cold-start restart
    # emits zero priority_changed events. This handler-side check is
    # belt-and-braces for third-party producers and the degenerate
    # ``None → None`` case. See module docstring.
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
