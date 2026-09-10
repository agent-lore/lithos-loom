"""Serial admission — bound a project's delivered-but-unmerged PRs (PRD S6).

``blocks`` edges serialise stories the planner knew would collide; nothing
serialised the rest, and ``max_concurrency`` never bounded *delivered* PRs
— once delivery released its claim the next story started while the first
``pr`` gate was still open. That is how the 2026-08-22 batch of conflicting
lens PRs happened.

**Admission invariant.** Before a PR-producing route (``completes_task =
false``) claims a ready story, :class:`Admission` counts the project's OPEN
``pr`` gates — one filtered ``task_list`` — and refuses at the limit
(``max_open_delivered_prs``, default 1). It counts *gates* plus this
process's admitted-but-not-yet-delivered runs — not claims: that is the
distinction the concurrency knob missed, and the in-flight reservation is
what stops a second PR-producing route on the same project slipping a story
past the count while the first is still running. Escalated gates —
whose story carries an open loom ``human`` gate — do not count against the
limit (operator decision, 2026-08-24: a decision the operator owes is not
loom's work-in-progress, and counting it would turn operator latency into a
project stop). A looser cap (``max_open_delivered_prs_total``, default 3)
bounds the total including escalated ones; reaching it stops dispatch and
is itself worth surfacing (``[AdmissionHeld]``, once per project per
process): several PRs stuck awaiting decisions says something about the
decomposition. Both dials are host defaults under ``[orchestrator]``,
overridable per project in the context doc under the same keys; ``0`` is
unlimited. Counting is per project — a ``pr`` gate records no base branch,
so the PRD's "same base branch" narrows to "same project" here.

**Waiting.** A refused story is remembered in memory, per project, and
re-evaluated two ways: :class:`AdmissionWaker` republishes it the moment a
``pr`` gate in its project closes or a loom ``human`` gate escalates one
(a synthetic ``lithos.task.updated`` with origin
:data:`ADMISSION_RECHECK_ORIGIN`, the US6 nudge shape), and the runner's
readiness re-check sleeper is the fallback with its backoff — the bus is
fire-and-forget, so a dropped nudge must not strand a story. In-process
state is the right durability: a restart's bootstrap replays every open
story and each is admitted or deferred afresh.

**Fail closed.** An unreadable gate list refuses (a serialisation invariant
is never waived by an outage); an unreadable human-gate list counts every
gate; an unreadable context doc refuses too — a project may *tighten* the
host defaults, so "cannot read the dial" is "cannot know the limit".
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from lithos_loom.bus import Event, EventBus, Subscription
from lithos_loom.errors import LithosClientError
from lithos_loom.gates import GATE_TYPE_HUMAN, GATE_TYPE_PR, RAISED_BY_LOOM, waiter_of
from lithos_loom.subscriptions._project_settings import (
    project_count,
    read_project_metadata,
)
from lithos_loom.subscriptions.dispatch_guards import task_payload

__all__ = [
    "ADMISSION_HELD",
    "ADMISSION_RECHECK_ORIGIN",
    "LIMIT_KEY",
    "TOTAL_KEY",
    "Admission",
    "AdmissionLimits",
    "AdmissionVerdict",
    "AdmissionWaker",
]

logger = logging.getLogger(__name__)

ADMISSION_HELD = "[AdmissionHeld]"
"""Finding prefix: the project's total cap on open delivered PRs is reached,
so no further story is dispatched until one merges or is abandoned."""

ADMISSION_RECHECK_ORIGIN = "admission-recheck"
"""``Event.origin`` of the waker's synthetic re-dispatch nudge."""

# The two dials: host defaults under [orchestrator], per-project overrides
# in the context doc's metadata under the same keys.
LIMIT_KEY = "max_open_delivered_prs"
TOTAL_KEY = "max_open_delivered_prs_total"

_SUBSYSTEM = "serial-admission"


@dataclass(frozen=True)
class AdmissionLimits:
    """``limit`` bounds non-escalated open ``pr`` gates; ``total`` bounds all
    of them. ``0`` = unlimited."""

    limit: int = 1
    total: int = 3


@dataclass(frozen=True)
class AdmissionVerdict:
    """What :meth:`Admission.admit` decided and why.

    ``reason`` is one of ``admitted`` / ``unlimited`` / ``no_project`` (the
    three admissions), ``limit`` / ``total_cap`` / ``unreadable`` (the
    refusals).
    """

    admitted: bool
    reason: str
    open_gates: int = 0
    escalated: int = 0
    limits: AdmissionLimits = AdmissionLimits()
    pr_urls: tuple[str, ...] = ()
    in_flight: int = 0
    """Other stories this process admitted into the project whose run has
    not ended yet — counted like a gate, since each will deliver one."""


class Admission:
    """The per-process admission gate every PR-producing route consults."""

    def __init__(self, *, lithos: Any, agent_id: str, defaults: AdmissionLimits):
        self._lithos = lithos
        self._agent_id = agent_id
        self._defaults = defaults
        self._deferred: dict[str, set[str]] = {}  # project → refused story ids
        self._in_flight: dict[str, set[str]] = {}  # project → admitted, running
        self._held_notified: set[str] = set()  # projects told [AdmissionHeld]

    def deferred(self, project: str) -> frozenset[str]:
        """Stories refused for *project* and not yet admitted or forgotten."""
        return frozenset(self._deferred.get(project, ()))

    def forget(self, task_id: str) -> None:
        """Drop *task_id* from every project's deferred set (it was admitted,
        or it is no longer open)."""
        for waiting in self._deferred.values():
            waiting.discard(task_id)

    def release(self, task_id: str) -> None:
        """The admitted story's run ended (delivered, failed, interrupted, or
        the claim was lost): its reservation is no longer needed — a
        delivered one is now counted by its ``pr`` gate."""
        for running in self._in_flight.values():
            running.discard(task_id)

    def _defer(self, project: str, task_id: str) -> None:
        self._deferred.setdefault(project, set()).add(task_id)

    def _admit(self, project: str, task_id: str) -> None:
        self.forget(task_id)
        self._in_flight.setdefault(project, set()).add(task_id)
        self._held_notified.discard(project)  # the cap cleared: re-arm the finding

    async def admit(self, *, task_id: str, project: str | None) -> AdmissionVerdict:
        """Decide whether *task_id* may be claimed now (see the module doc)."""
        if not project:
            return AdmissionVerdict(True, "no_project")
        limits = await self._limits_for(project)
        if limits is None:
            self._defer(project, task_id)
            return AdmissionVerdict(False, "unreadable")
        if limits.limit == 0 and limits.total == 0:
            self._admit(project, task_id)
            return AdmissionVerdict(True, "unlimited", limits=limits)
        try:
            gates = await self._lithos.task_list(
                status="open",
                task_type="gate",
                metadata_match={"gate_type": GATE_TYPE_PR, "project": project},
            )
        except (LithosClientError, OSError) as exc:
            logger.warning(
                "%s: cannot list project %r's open pr gates (%s); holding %s "
                "until Lithos answers",
                _SUBSYSTEM,
                project,
                exc,
                task_id,
            )
            self._defer(project, task_id)
            return AdmissionVerdict(False, "unreadable", limits=limits)
        running = len(self._in_flight.get(project, set()) - {task_id})
        total = len(gates) + running
        urls = tuple(_url_of(g) for g in gates)
        at_cap = bool(limits.total) and total >= limits.total
        at_limit = bool(limits.limit) and total >= limits.limit
        escalated = 0
        if at_cap or at_limit:
            escalated = await self._escalated_count(project, gates)
        if at_cap:
            self._defer(project, task_id)
            await self._notify_held(task_id, project, gates, limits, escalated, running)
            return AdmissionVerdict(
                False, "total_cap", len(gates), escalated, limits, urls, running
            )
        if at_limit and total - escalated >= limits.limit:
            self._defer(project, task_id)
            self._held_notified.discard(project)  # under the cap: re-arm
            return AdmissionVerdict(
                False, "limit", len(gates), escalated, limits, urls, running
            )
        self._admit(project, task_id)
        return AdmissionVerdict(
            True, "admitted", len(gates), escalated, limits, urls, running
        )

    async def _limits_for(self, project: str) -> AdmissionLimits | None:
        """The project's dials, or ``None`` when its context doc cannot be
        read — the caller holds the story rather than guess."""
        meta = await read_project_metadata(self._lithos, project)
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
            meta, LIMIT_KEY, self._defaults.limit, subsystem=_SUBSYSTEM, slug=project
        )
        total = project_count(
            meta, TOTAL_KEY, self._defaults.total, subsystem=_SUBSYSTEM, slug=project
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

    async def _escalated_count(self, project: str, gates: Sequence[Any]) -> int:
        """How many of *gates* wait on a story that carries an OPEN loom
        ``human`` gate. Unreadable → 0 (every gate counts: fail closed)."""
        try:
            humans = await self._lithos.task_list(
                status="open",
                task_type="gate",
                metadata_match={
                    "gate_type": GATE_TYPE_HUMAN,
                    "raised_by": RAISED_BY_LOOM,
                    "project": project,
                },
            )
        except (LithosClientError, OSError) as exc:
            logger.warning(
                "%s: cannot list project %r's open human gates (%s); counting "
                "every delivered PR",
                _SUBSYSTEM,
                project,
                exc,
            )
            return 0
        escalated_stories = {
            sid
            for sid in ((h.metadata or {}).get("story_id") for h in humans)
            if isinstance(sid, str) and sid
        }
        if not escalated_stories:
            return 0
        count = 0
        for gate in gates:
            story = (gate.metadata or {}).get("story_id")
            if not (isinstance(story, str) and story):
                # a gate from before story_id was recorded: the edge names it
                try:
                    story = await waiter_of(self._lithos, gate.id)
                except (LithosClientError, OSError):
                    story = None
            if story in escalated_stories:
                count += 1
        return count

    async def _notify_held(
        self,
        task_id: str,
        project: str,
        gates: Sequence[Any],
        limits: AdmissionLimits,
        escalated: int,
        running: int,
    ) -> None:
        """Once per project per cap episode: re-armed when the project next
        admits (or is merely at the limit), so a later pile-up fires again."""
        if project in self._held_notified:
            return
        summary = (
            f"{ADMISSION_HELD} project {project}: {len(gates)} delivered PR(s) "
            f"open ({escalated} escalated), {running} run(s) in flight — "
            f"{TOTAL_KEY}={limits.total} reached; no further story is dispatched "
            "for this project until one merges or is abandoned: "
            + ", ".join(_url_of(g) for g in gates)
        )
        logger.warning("%s (held %s)", summary, task_id)
        try:
            await self._lithos.finding_post(
                task_id=task_id, summary=summary, agent=self._agent_id
            )
        except (LithosClientError, OSError) as exc:
            logger.warning(
                "%s: could not post %s on %s: %s",
                _SUBSYSTEM,
                ADMISSION_HELD,
                task_id,
                exc,
            )
            return
        self._held_notified.add(project)


def _url_of(gate: Any) -> str:
    url = (gate.metadata or {}).get("pr_url")
    return url if isinstance(url, str) and url else f"gate {gate.id}"


# ── the waker ────────────────────────────────────────────────────────────


@dataclass
class AdmissionWaker:
    """One subscriber per route-runner child: when a ``pr`` gate closes or a
    loom ``human`` gate escalates one, republish that project's deferred
    stories so the runner re-asks admission now rather than after the
    re-check backoff. A nudge only — the sleeper is the fallback."""

    bus: EventBus
    lithos: Any
    admission: Admission

    def __post_init__(self) -> None:
        self._subscription: Subscription = self.bus.subscribe(
            event_types=(
                "lithos.task.created",
                "lithos.task.completed",
                "lithos.task.cancelled",
            ),
            match={"task_type": "gate"},
            name="admission-waker",
        )

    @property
    def subscription(self) -> Subscription:
        return self._subscription

    async def run(self) -> None:
        """Drain the subscription forever. Cancellable."""
        while True:
            event = await self._subscription.queue.get()
            try:
                await self._handle(event)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "AdmissionWaker: unhandled error processing %s", event.type
                )

    async def _handle(self, event: Event) -> None:
        metadata = event.payload.get("metadata") or {}
        if not _frees_a_slot(event.type, metadata):
            return
        project = metadata.get("project")
        if not isinstance(project, str) or not project:
            return
        waiting = self.admission.deferred(project)
        if not waiting:
            return
        logger.info(
            "AdmissionWaker: %s %s in project %r; re-asking admission for %d "
            "held story(ies)",
            event.type,
            event.payload.get("id"),
            project,
            len(waiting),
        )
        for task_id in sorted(waiting):
            try:
                task = await self.lithos.task_get(task_id=task_id)
            except (LithosClientError, OSError) as exc:
                logger.warning(
                    "AdmissionWaker: could not read held story %s (%s); the "
                    "re-check sleeper retries",
                    task_id,
                    exc,
                )
                continue
            if task is None or task.status != "open":
                self.admission.forget(task_id)
                continue
            await self.bus.publish(
                Event(
                    type="lithos.task.updated",
                    timestamp=datetime.now(UTC),
                    payload=task_payload(task),
                    origin=ADMISSION_RECHECK_ORIGIN,
                )
            )


def _frees_a_slot(event_type: str, metadata: Any) -> bool:
    gate_type = metadata.get("gate_type")
    if gate_type == GATE_TYPE_PR:
        return event_type in ("lithos.task.completed", "lithos.task.cancelled")
    if gate_type == GATE_TYPE_HUMAN:
        return (
            event_type == "lithos.task.created"
            and metadata.get("raised_by") == RAISED_BY_LOOM
        )
    return False
