"""``is_human_actionable`` — the projection filter for Obsidian (Slice 1 US8).

Centralised so the ``obsidian-projection`` handler and any future
sub-systems (digest, doctor warnings) share a single definition of
what counts as "operator-facing work."

Decision order (cheapest tests first):

1. Operator opt-out for blocked work: if ``include_blocked = false``
   in ``[obsidian_sync]`` and the task carries a non-empty
   ``metadata.depends_on`` list → False.
2. Operator tag denylist: if any of ``task.tags`` is in
   ``cfg.exclude_tags`` → False.
3. Route-driven autonomy:
   - Find routes whose ``match.tags`` overlap the task's tags.
   - If any matching route has ``human_blocking = true`` → True.
   - If matching route(s) exist but all are autonomous → False.
   - If no route matches (orphan task, no automation) → True.

Pure function with no I/O; trivial to unit-test in isolation.
"""

from __future__ import annotations

from collections.abc import Sequence

from lithos_loom.config import ObsidianSyncConfig, RouteConfig
from lithos_loom.lithos_client import Task

__all__ = ["is_human_actionable"]


def is_human_actionable(
    task: Task,
    routes: Sequence[RouteConfig],
    cfg: ObsidianSyncConfig,
) -> bool:
    """Return ``True`` iff this task should appear in the operator's view.

    See module docstring for the decision order.
    """
    depends_on = task.metadata.get("depends_on") or []
    if not cfg.include_blocked and depends_on:
        return False

    task_tag_set = set(task.tags)
    if task_tag_set & set(cfg.exclude_tags):
        return False

    matching = [r for r in routes if set(r.match.tags) & task_tag_set]
    if not matching:
        # Orphan task: no automation will pick it up, so a human must.
        return True
    return any(r.human_blocking for r in matching)
