"""``obsidian-priority-changed`` subscription handler (Slice 2 US21).

Consumes ``obsidian.task.priority_changed`` events emitted by
:class:`~lithos_loom.sources.obsidian_fs_watcher.ObsidianFsWatcher`
when the user edits the priority emoji on a projected line.

The terminal action — pushing the change back to Lithos via
``lithos_task_update(task_id, metadata={"priority": <enum>})`` — is
blocked on upstream support: the current ``lithos_task_update`` MCP
tool only accepts ``title`` / ``description`` / ``tags`` per
``lithos/docs/SPECIFICATION.md`` §5.4. Until upstream lands a
``metadata`` arg (issue filed alongside this PR), this handler uses
the same workaround shape that US19's ``_reopen_request`` uses for
the still-missing ``task_reopen`` — post a finding with a stable
prefix so lithos-lens and the operator have the signal.

Summary format::

    [PriorityChangeRequested] task priority changed in Obsidian: <prior> → <new>

``<prior>`` / ``<new>`` are the canonical D18 enum strings
(``"highest"``, ``"high"``, ``"medium"``, ``"low"``, ``"lowest"``)
or the literal string ``"none"`` when the emoji was absent.

When upstream metadata-update support ships, swap the
``finding_post`` call below for a real
``task_update(task_id=..., metadata={"priority": new_enum or None})``.
The fs watcher, sync_state extension, and projection plumbing all
stay; only this handler's inner call changes.

Idempotency is NOT enforced here. US22 will add a pre-check via
``lithos_task_status``; until then, posting a
``[PriorityChangeRequested]`` finding for a no-op edit (rare race)
is harmless but redundant.
"""

from __future__ import annotations

from lithos_loom.bus import Event
from lithos_loom.subscriptions import SubscriptionContext

__all__ = ["handle"]


_PRIORITY_CHANGE_PREFIX = "[PriorityChangeRequested]"
"""Stable finding-summary prefix. Operators and lithos-lens grep
for this exact string to surface pending priority changes; do not
reword without also updating any downstream consumer."""


def _format_priority_summary(prior: str | None, new: str | None) -> str:
    """Build the finding summary so the prefix is the stable part and
    the prior/new enum values are included for context. ``None``
    renders as the literal string ``none`` so the summary is grep-
    friendly even when one side has no priority."""
    return (
        f"{_PRIORITY_CHANGE_PREFIX} task priority changed in Obsidian: "
        f"{prior or 'none'} → {new or 'none'}"
    )


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
    # case a third party publishes the event with a non-string value
    # (e.g. an int priority level).
    prior_str: str | None = str(prior) if prior is not None else None
    new_str: str | None = str(new) if new is not None else None

    summary = _format_priority_summary(prior_str, new_str)
    await ctx.lithos.finding_post(
        task_id=task_id,
        summary=summary,
        agent=ctx.agent_id,
    )
    ctx.logger.info(
        "obsidian-priority-changed: posted [PriorityChangeRequested] for "
        "task %s (%s → %s)",
        task_id,
        prior_str,
        new_str,
    )
