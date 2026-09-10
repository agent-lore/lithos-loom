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
so the PRD's "same base branch" narrows to "same project" here — and
projectless stories and gates (a route with an absolute repo path) share one
bucket under the host defaults. Each bucket's read → decide → reserve
transition is serialised by a lock, so the route runners sharing one gate
cannot both admit on one snapshot.

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
host defaults, so "cannot read the dial" is "cannot know the limit"; and any
other exception on a read (the client re-raises raw transport errors once its
reconnects are spent) refuses rather than escaping and dropping the event.
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
from lithos_loom.subscriptions.dispatch_guards import project_of, task_payload

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

    ``reason`` is ``admitted`` / ``unlimited`` (the admissions) or ``limit``
    / ``total_cap`` / ``unreadable`` (the refusals).
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
    """The per-process admission gate every PR-producing route consults.

    All state is keyed by *bucket*: the story's project slug, or ``""`` for
    projectless work (a route with an absolute repo path needs no project;
    its stories deliver projectless ``pr`` gates, and they share one bucket
    under the host defaults — review #368 F1). Each bucket's read → decide
    → reserve transition runs under its own lock, so two route runners
    sharing this object cannot both admit on one snapshot (F2).
    """

    def __init__(self, *, lithos: Any, agent_id: str, defaults: AdmissionLimits):
        self._lithos = lithos
        self._agent_id = agent_id
        self._defaults = defaults
        self._deferred: dict[str, set[str]] = {}  # bucket → refused story ids
        self._in_flight: dict[str, set[str]] = {}  # bucket → admitted, running
        self._held_notified: set[str] = set()  # buckets told [AdmissionHeld]
        self._locks: dict[str, asyncio.Lock] = {}

    def deferred(self, project: str | None) -> frozenset[str]:
        """Stories refused for *project*'s bucket and not yet admitted or
        forgotten."""
        return frozenset(self._deferred.get(_bucket(project), ()))

    def forget(self, task_id: str) -> None:
        """Drop *task_id* from every bucket's deferred set (it was admitted,
        or it is no longer open)."""
        for waiting in self._deferred.values():
            waiting.discard(task_id)

    def release(self, task_id: str) -> None:
        """The admitted story's run ended (delivered, failed, interrupted, or
        the claim was lost): its reservation is no longer needed — a
        delivered one is now counted by its ``pr`` gate."""
        for running in self._in_flight.values():
            running.discard(task_id)

    def _defer(self, bucket: str, task_id: str) -> None:
        self._deferred.setdefault(bucket, set()).add(task_id)

    def _admit(self, bucket: str, task_id: str) -> None:
        self.forget(task_id)
        self._in_flight.setdefault(bucket, set()).add(task_id)
        self._held_notified.discard(bucket)  # the cap cleared: re-arm the finding

    async def admit(self, *, task_id: str, project: str | None) -> AdmissionVerdict:
        """Decide whether *task_id* may be claimed now (see the module doc).

        Never raises: ``LithosClient._invoke`` re-raises the raw transport
        exception once its reconnects are spent, and an escaped exception
        would drop the event with nothing armed to re-ask (review #368 F3)
        — so ANY failure on the reads is "unreadable": held and deferred, the
        sleeper retries.
        """
        bucket = _bucket(project)
        lock = self._locks.setdefault(bucket, asyncio.Lock())
        async with lock:
            try:
                return await self._decide(task_id, project, bucket)
            except Exception:
                logger.exception(
                    "%s: cannot decide admission for %s (bucket %r); holding it "
                    "until Lithos answers",
                    _SUBSYSTEM,
                    task_id,
                    bucket,
                )
                self._defer(bucket, task_id)
                return AdmissionVerdict(False, "unreadable")

    async def _decide(
        self, task_id: str, project: str | None, bucket: str
    ) -> AdmissionVerdict:
        limits = await self._limits_for(project)
        if limits is None:
            self._defer(bucket, task_id)
            return AdmissionVerdict(False, "unreadable")
        if limits.limit == 0 and limits.total == 0:
            self._admit(bucket, task_id)
            return AdmissionVerdict(True, "unlimited", limits=limits)
        gates = await self._open_gates(GATE_TYPE_PR, project)
        running = len(self._in_flight.get(bucket, set()) - {task_id})
        total = len(gates) + running
        urls = tuple(_url_of(g) for g in gates)
        at_cap = bool(limits.total) and total >= limits.total
        at_limit = bool(limits.limit) and total >= limits.limit
        escalated = 0
        if at_cap or at_limit:
            escalated = await self._escalated_count(project, gates)
        if at_cap:
            self._defer(bucket, task_id)
            await self._notify_held(task_id, bucket, gates, limits, escalated, running)
            return AdmissionVerdict(
                False, "total_cap", len(gates), escalated, limits, urls, running
            )
        if at_limit and total - escalated >= limits.limit:
            self._defer(bucket, task_id)
            self._held_notified.discard(bucket)  # under the cap: re-arm
            return AdmissionVerdict(
                False, "limit", len(gates), escalated, limits, urls, running
            )
        self._admit(bucket, task_id)
        return AdmissionVerdict(
            True, "admitted", len(gates), escalated, limits, urls, running
        )

    async def _open_gates(self, gate_type: str, project: str | None) -> list[Any]:
        """The bucket's open gates of *gate_type* — one filtered read for a
        project; for the projectless bucket, every open gate of the type
        that names no project (``metadata_match`` cannot say "absent")."""
        match: dict[str, Any] = {"gate_type": gate_type}
        if project:
            match["project"] = project
        gates = await self._lithos.task_list(
            status="open", task_type="gate", metadata_match=match
        )
        if project:
            return list(gates)
        return [g for g in gates if project_of(g.metadata) is None]

    async def _limits_for(self, project: str | None) -> AdmissionLimits | None:
        """The project's dials, or ``None`` when its context doc cannot be
        read — the caller holds the story rather than guess. Projectless work
        has no doc: the host defaults."""
        if not project:
            return self._defaults
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

    async def _escalated_count(self, project: str | None, gates: Sequence[Any]) -> int:
        """How many of *gates* wait on a story that an OPEN loom ``human``
        gate structurally blocks. The human gate's ``waits_on_gate`` edge is
        the authority (review #368 F4): gate creation is not atomic, and a
        gate task whose edge never landed blocks nothing — its ``story_id``
        alone must not free a slot. Unreadable → 0 (every gate counts)."""
        try:
            humans = await self._open_gates(GATE_TYPE_HUMAN, project)
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
            story = await waiter_of(self._lithos, human.id)
            if story is not None:
                escalated_stories.add(story)
        if not escalated_stories:
            return 0
        count = 0
        for gate in gates:
            story = (gate.metadata or {}).get("story_id")
            if not (isinstance(story, str) and story):
                # a gate from before story_id was recorded: the edge names it
                story = await waiter_of(self._lithos, gate.id)
            if story in escalated_stories:
                count += 1
        return count

    async def _notify_held(
        self,
        task_id: str,
        bucket: str,
        gates: Sequence[Any],
        limits: AdmissionLimits,
        escalated: int,
        running: int,
    ) -> None:
        """Once per bucket per cap episode: re-armed when the bucket next
        admits (or is merely at the limit), so a later pile-up fires again."""
        if bucket in self._held_notified:
            return
        summary = (
            f"{ADMISSION_HELD} project {bucket or '(none)'}: {len(gates)} delivered "
            f"PR(s) open ({escalated} escalated), {running} run(s) in flight — "
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
        self._held_notified.add(bucket)


def _bucket(project: str | None) -> str:
    return project or ""


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
        project = project_of(metadata)  # None → the projectless bucket
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
