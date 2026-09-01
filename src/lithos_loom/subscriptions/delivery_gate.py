"""The delivering exit of a ``completes_task = false`` route (Epic H).

A PR-producing route's success means "a reviewed branch + PR exist, awaiting
human merge" — NOT that the story is done. This module owns that exit: raise
the ``pr`` gate that blocks the delivered story (:mod:`lithos_loom.gates`),
record its id on the story as provenance (and retire any failed-attempt
marker / needs-human provenance an earlier run left), release the claim, and
post ONE ``[Friction]`` when any of that degraded. The sibling non-delivering
exit lives in :mod:`.escalation`.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Mapping
from typing import Any

from lithos_loom.gates import (
    STORY_GATE_ID_KEY,
    STORY_HUMAN_GATE_ID_KEY,
    create_pr_gate_best_effort,
)
from lithos_loom.subscriptions.dispatch_guards import (
    AttemptStampStore,
    last_attempt_key,
)

__all__ = ["gate_and_release"]

logger = logging.getLogger(__name__)


async def gate_and_release(
    lithos: Any,
    *,
    task_id: str,
    route: str,
    agent: str,
    payload: Mapping[str, Any],
    result: Mapping[str, Any],
    stamps: AttemptStampStore | None = None,
) -> None:
    """Gate a delivered task on human merge, then release (``completes_task
    =false``).

    Epic H: create a ``pr`` gate from the run's ``pr_url`` so "awaiting
    merge" is a first-class blocker, record its id on the story as
    provenance, and release the claim (don't hold it across a potentially
    long human wait). Each step's failure is degraded, not fatal — the
    branch + PR exist regardless — so we collect the consequences and post
    ONE ``[Friction]`` making the degraded state visible in Lithos rather
    than only logging.

    The gate is the sole re-dispatch guard now (US11 retired
    ``loom_delivered``): a gated story is absent from ``task_ready``, so the
    runner won't re-develop it. Gate creation is therefore load-bearing and
    best-effort: if ``pr_url`` is missing (a PR-less success — for
    story-develop, #194 makes that impossible, but the runner is
    plugin-agnostic) or the write fails, NO gate exists and the loud
    ``[Friction]`` (carried in ``gate_problem``) warns that a restart could
    re-develop the story into a duplicate PR until a human merges the PR or
    creates the gate.
    """
    problems: list[str] = []

    project = (payload.get("metadata") or {}).get("project")
    gate_id, gate_problem = await create_pr_gate_best_effort(
        lithos,
        story_id=task_id,
        story_title=str(payload.get("title") or task_id),
        pr_url=result.get("pr_url"),
        project=project if isinstance(project, str) else None,
        agent=agent,
    )
    if gate_problem is not None:
        problems.append(gate_problem)

    # Record the gate on the story as provenance (the inverse of the
    # waits_on_gate edge, so an operator sees which gate withholds the story
    # without walking edges). Only written when a gate exists; loom_delivered
    # is retired (US11) — the gate plus the runner's task_ready check are the
    # whole re-dispatch guard.
    story_metadata: dict[str, Any] = {}
    if gate_id is not None:
        story_metadata[STORY_GATE_ID_KEY] = gate_id
        # A delivery supersedes any earlier failed attempt: per-key delete of
        # the marker (and any needs-human provenance the failure left) on the
        # same write. Skipped when no gate exists — the loud [Friction] owns
        # that state, and a leftover marker only declines bootstrap replay,
        # the safe direction.
        story_metadata[last_attempt_key(route)] = None
        story_metadata[STORY_HUMAN_GATE_ID_KEY] = None

    marked = True
    if story_metadata:
        try:
            await lithos.task_update(
                task_id=task_id, agent=agent, metadata=story_metadata
            )
            # The marker delete rode along on this write — retire its local
            # stamp with it (#339).
            if stamps is not None:
                stamps.clear(route, task_id)
        except Exception:
            marked = False
            logger.exception(
                "RouteRunner %s: recording pr_gate_id on %s failed", route, task_id
            )
    released = True
    try:
        await lithos.task_release(task_id=task_id, aspect=route, agent=agent)
    except Exception:
        released = False
        logger.exception("RouteRunner %s: task_release failed for %s", route, task_id)
    if not marked:
        # We only attempt the write when a gate exists, so reaching here
        # means the gate is present but its provenance id didn't land. The
        # gate already blocks re-dispatch (a gated story is absent from the
        # ready frontier), so the missing marker is benign.
        problems.append(
            "could not record pr_gate_id on the story, but the pr gate "
            "already blocks re-dispatch"
        )
    if not released:
        problems.append(
            "could not release the claim — it will linger until its TTL "
            "expires, briefly blocking other runners"
        )
    if not problems:
        logger.info(
            "RouteRunner %s: delivered %s — gated on merge (gate %s)",
            route,
            task_id,
            gate_id,
        )
        return
    summary = f"[Friction] route {route}: delivered task (PR raised) but " + "; ".join(
        problems
    )
    with contextlib.suppress(Exception):
        await lithos.finding_post(task_id=task_id, summary=summary, agent=agent)
