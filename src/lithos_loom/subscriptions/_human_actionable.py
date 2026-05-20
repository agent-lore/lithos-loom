"""``is_human_actionable`` — the projection filter for Obsidian (Slice 1 US8).

Centralised so the ``obsidian-projection`` handler and any future
sub-systems (digest, doctor warnings) share a single definition of
what counts as "operator-facing work."

Implements D6 from ``docs/prd/integration.md`` verbatim:

    Project a Lithos task iff is_human_actionable(task) — open AND
    not claimable by any route, OR claimed by a human_blocking = true
    route.

Decision order (cheapest tests first):

1. Operator opt-out for blocked work: if ``include_blocked = false``
   in ``[obsidian_sync]`` and the task carries a non-empty
   ``metadata.depends_on`` list → False.
2. Operator tag denylist: if any of ``task.tags`` is in
   ``cfg.exclude_tags`` → False.
3. D6 first disjunct — open orphan task: ``task.status == "open"`` AND
   no route's ``match.tags`` overlap the task's tags → True. Nothing
   automated will pick it up.
4. D6 second disjunct — claimed by a human_blocking route: any entry
   in ``task.claims`` whose ``aspect`` field equals the ``name`` of a
   route configured with ``human_blocking = true`` → True. The
   route-runner sets ``aspect = route.name`` on every claim it makes
   (see ``RouteRunner.run`` in ``subscriptions/route_runner.py``), so
   this is how we tell which route owns the claim.
5. Otherwise → False. This covers:
   - claimable by an autonomous route, not yet claimed (waiting for
     automation; nothing for the operator to do)
   - claimed by an autonomous route (automation is running)
   - claimable by a human_blocking route but not yet claimed (waiting
     for the route-runner to claim it; per D6 we wait for the claim
     before projecting so the operator only sees work that's actually
     blocked on them, not work that *might* end up blocked on them)

Pure function with no I/O; trivial to unit-test in isolation.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from lithos_loom.config import ObsidianSyncConfig, RouteConfig
from lithos_loom.lithos_client import Task

__all__ = ["is_human_actionable"]


def is_human_actionable(
    task: Task,
    routes: Sequence[RouteConfig],
    cfg: ObsidianSyncConfig,
) -> bool:
    """Return ``True`` iff this task should appear in the operator's view.

    See module docstring for the decision order and D6 quote.
    """
    # Terminal tasks are never actionable here. The handler removes
    # them on completed/cancelled events; if this helper is ever
    # called for a terminal task (e.g. a stale claim from a cancelled
    # workflow), don't surface it. US13 will overlay TTL lingering
    # with a separate signal.
    if task.status != "open":
        return False

    depends_on = task.metadata.get("depends_on") or []
    if not cfg.include_blocked and depends_on:
        return False

    task_tag_set = set(task.tags)
    if task_tag_set & set(cfg.exclude_tags):
        return False

    claimable_routes = [r for r in routes if set(r.match.tags) & task_tag_set]

    # D6 first disjunct: open AND not claimable by any route.
    if not claimable_routes:
        return True

    # D6 second disjunct: claimed by a human_blocking route. The
    # claim's `aspect` carries the route name (see RouteRunner).
    human_blocking_names = {r.name for r in routes if r.human_blocking}
    return bool(human_blocking_names) and any(
        _claim_aspect(c) in human_blocking_names for c in task.claims
    )


def _claim_aspect(claim: Mapping[str, Any] | Any) -> str | None:
    """Pull the ``aspect`` field out of a claim record, defensively.

    The :class:`Task` dataclass declares ``claims`` as a tuple of
    mappings, but the real wire payload is JSON so we treat it as a
    duck-typed mapping. Returns ``None`` for any shape we can't read,
    which falls through to "not a matching claim" in the caller.
    """
    if isinstance(claim, Mapping):
        val = claim.get("aspect")
        if isinstance(val, str):
            return val
    return None
