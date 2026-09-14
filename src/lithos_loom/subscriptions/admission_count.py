"""Serial admission's reads — what a bucket holds against its dials (PRD S6).

The count half of :mod:`.admission`, split out for size: the project's
dials from its context doc, its open gates of a type, the gates whose
story is already terminal (#372), and the gates an open loom ``human``
gate escalates. Pure reads against Lithos with the fail-closed rules the
module doc of :mod:`.admission` records; the queue and the decision live
there.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from lithos_loom.errors import LithosClientError
from lithos_loom.gates import GATE_TYPE_HUMAN, RAISED_BY_LOOM, waiter_of
from lithos_loom.subscriptions._project_settings import (
    project_count,
    read_project_metadata,
)
from lithos_loom.subscriptions.dispatch_guards import project_of

__all__ = [
    "LIMIT_KEY",
    "TOTAL_KEY",
    "AdmissionLimits",
    "escalated_count",
    "limits_for",
    "live_gates",
    "open_gates",
]

logger = logging.getLogger(__name__)

_SUBSYSTEM = "serial-admission"

# The two dials: host defaults under [orchestrator], per-project overrides
# in the context doc's metadata under the same keys.
LIMIT_KEY = "max_open_delivered_prs"
TOTAL_KEY = "max_open_delivered_prs_total"


@dataclass(frozen=True)
class AdmissionLimits:
    """``limit`` bounds non-escalated open ``pr`` gates; ``total`` bounds all
    of them. ``0`` = unlimited."""

    limit: int = 1
    total: int = 3


def _bucket(project: str | None) -> str:
    return project or ""


async def open_gates(lithos: Any, gate_type: str, project: str | None) -> list[Any]:
    """The bucket's open gates of *gate_type* — one filtered read for a
    project; for the projectless bucket, every open gate of the type
    that names no project (``metadata_match`` cannot say "absent")."""
    match: dict[str, Any] = {"gate_type": gate_type}
    if project:
        match["project"] = project
    gates = await lithos.task_list(
        status="open", task_type="gate", metadata_match=match
    )
    if project:
        return list(gates)
    return [g for g in gates if project_of(g.metadata) is None]


async def limits_for(
    lithos: Any, project: str | None, defaults: AdmissionLimits
) -> AdmissionLimits | None:
    """The project's dials, or ``None`` when its context doc cannot be
    read — the caller holds the story rather than guess. Projectless work
    has no doc: the host defaults."""
    if not project:
        return defaults
    meta = await read_project_metadata(lithos, project)
    if meta is None:
        logger.warning(
            "%s: project %r's context doc is unreadable; its %s / %s are "
            "unknown, so its stories are held until Lithos answers",
            _SUBSYSTEM,
            project,
            LIMIT_KEY,
            TOTAL_KEY,
        )
        return None
    limit = project_count(
        meta, LIMIT_KEY, defaults.limit, subsystem=_SUBSYSTEM, slug=project
    )
    total = project_count(
        meta, TOTAL_KEY, defaults.total, subsystem=_SUBSYSTEM, slug=project
    )
    if total and limit and total < limit:
        logger.warning(
            "[Friction] %s: project %r sets %s=%d below %s=%d; using %d for both",
            _SUBSYSTEM,
            project,
            TOTAL_KEY,
            total,
            LIMIT_KEY,
            limit,
            limit,
        )
        total = limit
    return AdmissionLimits(limit=limit, total=total)


async def live_gates(lithos: Any, gates: Sequence[Any]) -> list[Any]:
    """*gates* minus those whose waiter is terminal (#372). A waiter that
    cannot be read, or that Lithos no longer returns ("gone" is not
    "done"), keeps its gate counted — fail closed, as the escalation read
    does. The story is named by the gate's ``story_id`` metadata first
    (the cheap link, as ``_escalated_count`` reads it) and the
    ``waits_on_gate`` edge only for an older gate: ``create_pr_gate``
    writes both or neither, so they disagree only after a hand edit."""
    live: list[Any] = []
    for gate in gates:
        story = (gate.metadata or {}).get("story_id")
        try:
            if not (isinstance(story, str) and story):
                story = await waiter_of(lithos, gate.id)
            waiter = await lithos.task_get(task_id=story) if story is not None else None
        except (LithosClientError, OSError) as exc:
            logger.warning(
                "%s: cannot read the story behind gate %s (%s); counting it",
                _SUBSYSTEM,
                gate.id,
                exc,
            )
            live.append(gate)
            continue
        if waiter is not None and waiter.status != "open":
            logger.info(
                "%s: pr gate %s waits on %s, already %s — not counted (#372)",
                _SUBSYSTEM,
                gate.id,
                story,
                waiter.status,
            )
            continue
        live.append(gate)
    return live


async def escalated_count(
    lithos: Any, project: str | None, gates: Sequence[Any]
) -> int:
    """How many of *gates* wait on a story that an OPEN loom ``human``
    gate structurally blocks. The human gate's ``waits_on_gate`` edge is
    the authority (review #368 F4): gate creation is not atomic, and a
    gate task whose edge never landed blocks nothing — its ``story_id``
    alone must not free a slot. Unreadable → 0 (every gate counts)."""
    try:
        humans = await open_gates(lithos, GATE_TYPE_HUMAN, project)
    except (LithosClientError, OSError) as exc:
        logger.warning(
            "%s: cannot list bucket %r's open human gates (%s); counting "
            "every delivered PR",
            _SUBSYSTEM,
            _bucket(project),
            exc,
        )
        return 0
    escalated_stories: set[str] = set()
    for human in humans:
        if (human.metadata or {}).get("raised_by") != RAISED_BY_LOOM:
            continue
        story = await waiter_of(lithos, human.id)
        if story is not None:
            escalated_stories.add(story)
    if not escalated_stories:
        return 0
    count = 0
    for gate in gates:
        story = (gate.metadata or {}).get("story_id")
        if not (isinstance(story, str) and story):
            # a gate from before story_id was recorded: the edge names it
            story = await waiter_of(lithos, gate.id)
        if story in escalated_stories:
            count += 1
    return count
