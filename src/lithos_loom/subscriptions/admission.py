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
process's admitted-but-not-yet-delivered ``(route, story)`` runs — not
claims: that is the distinction the concurrency knob missed, and the
in-flight reservation is what stops a second PR-producing route on the same
project slipping a story (even the same story) past the count while the
first is still running. Escalated gates —
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

**Release order (561db86a, ADR 0012).** When a slot is free and more than
one story wants it, the story's own ``metadata.priority`` decides first —
the Lithos vocabulary (``highest`` … ``lowest``), the default of no
priority sitting between ``low`` and ``medium`` — and stories of one rank
leave in the order they FIRST ASKED this process (a re-ask that is refused
again keeps its place; so does a story whose run ended without a gate —
interrupted, failed — and asks again; a newcomer joins the back). The
priority is read when the slot frees, not remembered from the refusal:
marking a waiting story up is the operator's lever. The choice is made
HERE, under the bucket's lock, not by whichever producer publishes first:
with more askers than free slots, an asker outside the first ``free``
places is refused ``queued`` and those places are nudged — the same
synthetic event the waker sends — so the re-check sleeper can no longer
pre-empt the waker's sweep, and the order is deterministic for the same
inputs. Every transition that can free a place re-nudges: a gate event
(the waker), an admission that leaves slots free, a run that ends without
a gate (``release``), a head that leaves the queue (``forget``) — the
sleeper stays the fallback for a dropped nudge, never the primary.
Starvation is accepted and stated: a story of higher priority always
leaves first, so a bucket with an unbounded supply of high-priority work
never releases its default-priority stories — the operator set that
priority, and it is a per-bucket signal that can be read off the queue.
A held story the runner cannot place on the ready frontier (not ready,
undetermined, or the read failed) gives up its wait, and so does one its
route no longer matches (its trigger tag removed): a head that never asks
would hold everything behind it, and that — a stall, never a spin — is
the one way this design fails, so a story nudged as next ten times
without asking is named in the log. The story keeps its PLACE through
all of that (first asked is first asked while it is open), so a Lithos
blip that drops every wait does not reorder the queue. The reads run
under the bucket's lock — as the gate reads always have — so a slow
Lithos holds that bucket's askers; the queue is single digits by
construction (the caps are 1 and 3).

**Fail closed.** An unreadable gate list refuses (a serialisation invariant
is never waived by an outage); an unreadable human-gate list counts every
gate; an unreadable context doc refuses too — a project may *tighten* the
host defaults, so "cannot read the dial" is "cannot know the limit"; and any
other exception on a read (the client re-raises raw transport errors once its
reconnects are spent) refuses rather than escaping and dropping the event.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from lithos_loom.bus import Event, EventBus
from lithos_loom.errors import LithosClientError
from lithos_loom.gates import GATE_TYPE_PR
from lithos_loom.subscriptions.admission_count import (
    LIMIT_KEY,
    TOTAL_KEY,
    AdmissionLimits,
    escalated_count,
    limits_for,
    live_gates,
    open_gates,
)
from lithos_loom.subscriptions.dispatch_guards import project_of, task_payload
from lithos_loom.task_line import PRIORITY_EMOJI

__all__ = [
    "ADMISSION_HELD",
    "ADMISSION_RECHECK_ORIGIN",
    "LIMIT_KEY",
    "TOTAL_KEY",
    "Admission",
    "AdmissionLimits",
    "AdmissionVerdict",
]

logger = logging.getLogger(__name__)

ADMISSION_HELD = "[AdmissionHeld]"
"""Finding prefix: the project's total cap on open delivered PRs is reached,
so no further story is dispatched until one merges or is abandoned."""

ADMISSION_RECHECK_ORIGIN = "admission-recheck"
"""``Event.origin`` of the waker's synthetic re-dispatch nudge."""

_SUBSYSTEM = "serial-admission"


def _rank_order() -> tuple[str | None, ...]:
    """The release order's first key (ADR 0012): the Lithos priority
    vocabulary — derived from ``task_line.PRIORITY_EMOJI`` (the Obsidian
    Tasks scale, declared highest → lowest) so the two can never disagree —
    with the default (no priority set, ``None`` here) in its Obsidian place
    between ``low`` and ``medium``. Higher rank leaves first."""
    names: list[str | None] = list(reversed(PRIORITY_EMOJI))  # lowest → highest
    names.insert(names.index("low") + 1, None)
    return tuple(names)


_RANK_ORDER = _rank_order()
_DEFAULT_RANK = _RANK_ORDER.index(None)
# every this many nudges a story never answered, say so in the log
_UNANSWERED_NAG_EVERY = 10


@dataclass(frozen=True)
class AdmissionVerdict:
    """What :meth:`Admission.admit` decided and why.

    ``reason`` is ``admitted`` / ``unlimited`` (the admissions) or ``limit``
    / ``total_cap`` / ``unreadable`` / ``queued`` (the refusals — the last
    one a free slot that another held story is ahead for, ADR 0012).
    """

    admitted: bool
    reason: str
    open_gates: int = 0
    escalated: int = 0
    limits: AdmissionLimits = AdmissionLimits()
    pr_urls: tuple[str, ...] = ()
    in_flight: int = 0
    """Other ``(route, story)`` runs this process admitted into the bucket
    that have not ended yet — counted like a gate, since each delivers one."""


@dataclass(frozen=True)
class _Headroom:
    """One count of a bucket against both dials (see :meth:`Admission._headroom`)."""

    limits: AdmissionLimits
    gates: list[Any]
    running: int
    escalated: int
    at_cap: bool

    @property
    def urls(self) -> tuple[str, ...]:
        return tuple(_url_of(g) for g in self.gates)

    @property
    def free(self) -> int:
        """How many stories may be admitted now: the tighter of the two
        dials, escalated gates not counted against the limit. Escalated
        gates are only read when a dial was in question, so the limit's
        headroom can be under-counted by them — the story that holds back
        is admitted on its next ask, once the count reaches the limit and
        the escalation is read. Never a reason to admit above a dial: every
        admission re-checks both."""
        total = len(self.gates) + self.running
        room = []
        if self.limits.limit:
            room.append(self.limits.limit - (total - self.escalated))
        if self.limits.total:
            room.append(self.limits.total - total)
        return max(0, min(room)) if room else sys.maxsize


class Admission:
    """The per-process admission gate every PR-producing route consults.

    All state is keyed by *bucket*: the story's project slug, or ``""`` for
    projectless work (a route with an absolute repo path needs no project;
    its stories deliver projectless ``pr`` gates, and they share one bucket
    under the host defaults — review #368 F1). Each bucket's read → decide
    → reserve transition runs under its own lock, so two route runners
    sharing this object cannot both admit on one snapshot (F2).
    """

    def __init__(
        self, *, lithos: Any, agent_id: str, defaults: AdmissionLimits, bus: EventBus
    ):
        self._lithos = lithos
        self._agent_id = agent_id
        self._defaults = defaults
        self._bus = bus  # held stories are nudged on it (the waker's shape)
        # bucket → (route, story) refusals still waiting
        self._deferred: dict[str, set[tuple[str, str]]] = {}
        # story → the order it FIRST asked this process (ADR 0012): the FIFO
        # key, kept for as long as the story is open — a refusal, an
        # admission, a run that ended without a gate, a wait dropped because
        # the story could not be placed or matched, all keep it, so a story
        # that comes back comes back to its place (review round 3: a Lithos
        # blip that empties the waits must not reorder the queue).
        self._first_asked: dict[str, int] = {}
        self._ask_seq = itertools.count()
        # story → its priority rank as last read: a read that fails ranks the
        # story where it was last seen, never at the default, so a flapping
        # Lithos cannot swap the head back and forth (review round 2)
        self._rank_seen: dict[str, int] = {}
        # (route, story) → nudges as "next" since that route last asked: a
        # head that is told and never asks is the one way this design
        # stalls, so say so
        self._unanswered: dict[tuple[str, str], int] = {}
        # route → the tags it matches on, as each ask reports them: a held
        # wait whose story no longer carries them would never be received
        # by that route's runner (review round 3)
        self._route_tags: dict[str, tuple[str, ...]] = {}
        # bucket → (route, story) reservations: admitted, run not yet ended
        self._in_flight: dict[str, set[tuple[str, str]]] = {}
        self._held_notified: set[str] = set()  # buckets told [AdmissionHeld]
        self._locks: dict[str, asyncio.Lock] = {}

    def deferred(self, project: str | None) -> frozenset[str]:
        """Inspection: the stories some route was refused for *project*'s
        bucket and still waits on."""
        return frozenset(t for _r, t in self._deferred.get(_bucket(project), ()))

    async def forget(self, task_id: str, *, route: str | None = None) -> None:
        """*route*'s wait on the story ends (every route's when *route* is
        ``None``): the runner could not place it on the ready frontier —
        not ready, undetermined, or the read failed — so as head it would
        never ask and would hold everything behind it (ADR 0012). The
        story keeps its place: when it asks again it re-joins where it was,
        so a Lithos blip that drops every wait does not reorder the queue
        (review round 3). Each bucket it was held in is woken: a departed
        head would otherwise strand the stories behind it until their
        sleepers fire (review round 2). Never raises — it sits on the
        runner's dispatch path."""
        # membership first, the lock only where it holds a wait: this runs
        # on every story's terminal event, most of which never asked
        held_in = [
            b for b, w in self._deferred.items() if any(s == task_id for _r, s in w)
        ]
        for bucket in held_in:
            async with self._lock(bucket):
                waiting = self._deferred.get(bucket, set())
                gone = {w for w in waiting if w[1] == task_id}
                if route is not None:
                    gone &= {(route, task_id)}
                if not gone:
                    continue
                for wait in gone:
                    self._drop_wait(wait)
                await self._wake_safely(bucket)

    async def discard(self, task_id: str) -> None:
        """The story is terminal (its ``completed`` / ``cancelled`` event, via
        the waker): every wait on it ends, the buckets it was held in are
        woken, and its first-asked place and seen rank are released — the
        scheduler's memory is bounded by the OPEN stories (PR #398 review).
        A story that never asked costs nothing here: no lock is taken."""
        if self._first_asked.pop(task_id, None) is None:
            return
        self._rank_seen.pop(task_id, None)
        await self.forget(task_id)

    async def release(self, task_id: str, *, route: str) -> None:
        """*route*'s run of the admitted story ended (delivered, failed,
        interrupted, or the claim was lost): its reservation is no longer
        needed — a delivered one is now counted by its ``pr`` gate. Keyed by
        ``(route, story)`` (review #368 round 2): a task may match several
        PR-producing routes, each a run that delivers its own PR, so each
        takes a slot and one route's release never erases another's. The
        bucket is woken: a run that ends without a gate frees a slot with no
        gate event to say so (review round 2). Never raises — it sits in the
        runner's ``finally``."""
        for bucket in [b for b, r in self._in_flight.items() if (route, task_id) in r]:
            async with self._lock(bucket):
                self._in_flight[bucket].discard((route, task_id))
                await self._wake_safely(bucket)

    def _lock(self, bucket: str) -> asyncio.Lock:
        return self._locks.setdefault(bucket, asyncio.Lock())

    def _drop_story(self, task_id: str) -> None:
        """The story is no longer open (or gone): every route's wait on it,
        its place, its seen rank."""
        for waiting in self._deferred.values():
            for wait in [w for w in waiting if w[1] == task_id]:
                self._drop_wait(wait)
        self._first_asked.pop(task_id, None)
        self._rank_seen.pop(task_id, None)

    def _drop_wait(self, wait: tuple[str, str]) -> None:
        """One route's wait on a story; the story keeps its place."""
        for waiting in self._deferred.values():
            waiting.discard(wait)
        self._unanswered.pop(wait, None)

    def _defer(self, bucket: str, route: str, task_id: str, *, left: set[str]) -> None:
        # A story is in one project at a time: a wait left in another bucket
        # would be a phantom head there, never asking, never forgotten
        # (review round 2). This is the one mutation of another bucket's
        # state outside that bucket's lock; a pick there that already
        # ordered the stale wait nudges once for nothing and self-heals.
        # The bucket it left is woken once this ask is over (*left*): its
        # departed head may have been the one entitled to a free slot.
        left.update(self._leave_others(bucket, (route, task_id)))
        self._deferred.setdefault(bucket, set()).add((route, task_id))

    def _admit(self, bucket: str, route: str, task_id: str, *, left: set[str]) -> None:
        # only THIS route's wait ends (round 2: another route's stays keyed)
        left.update(self._leave_others(bucket, (route, task_id)))
        self._deferred.get(bucket, set()).discard((route, task_id))
        self._in_flight.setdefault(bucket, set()).add((route, task_id))
        self._held_notified.discard(bucket)  # the cap cleared: re-arm the finding

    def _leave_others(self, bucket: str, wait: tuple[str, str]) -> set[str]:
        """Remove *wait* from every bucket but *bucket*; the buckets it left."""
        left: set[str] = set()
        for other, waiting in self._deferred.items():
            if other != bucket and wait in waiting:
                waiting.discard(wait)
                left.add(other)
        return left

    async def admit(
        self,
        *,
        route: str,
        task_id: str,
        project: str | None,
        tags: Sequence[str] | None = None,
    ) -> AdmissionVerdict:
        """Decide whether *route* may claim *task_id* now (see the module doc).
        *tags* are the route's match tags — what its runner's subscription
        requires of a task, so a held wait whose story loses them is known
        to be one nobody will ever nudge; ``None`` leaves the last report.

        Never raises: ``LithosClient._invoke`` re-raises the raw transport
        exception once its reconnects are spent, and an escaped exception
        would drop the event with nothing armed to re-ask (review #368 F3)
        — so ANY failure on the reads is "unreadable": held and deferred, the
        sleeper retries.
        """
        bucket = _bucket(project)
        if tags is not None:
            self._route_tags[route] = tuple(tags)
        left: set[str] = set()  # buckets this ask moved the story out of
        async with self._lock(bucket):
            self._first_asked.setdefault(task_id, next(self._ask_seq))
            self._unanswered.pop((route, task_id), None)  # it asked
            try:
                verdict = await self._decide(route, task_id, project, bucket, left)
            except Exception:
                logger.exception(
                    "%s: cannot decide admission for %s (bucket %r); holding it "
                    "until Lithos answers",
                    _SUBSYSTEM,
                    task_id,
                    bucket,
                )
                self._defer(bucket, route, task_id, left=left)
                verdict = AdmissionVerdict(False, "unreadable")
        for other in sorted(left):
            # A re-homed story was a head there and may have been the one a
            # free slot was waiting for (PR #398 review): wake it, outside
            # this bucket's lock — never two bucket locks at once.
            await self.wake(other or None)
        return verdict

    async def _decide(
        self,
        route: str,
        task_id: str,
        project: str | None,
        bucket: str,
        left: set[str],
    ) -> AdmissionVerdict:
        limits = await limits_for(self._lithos, project, self._defaults)
        if limits is None:
            self._defer(bucket, route, task_id, left=left)
            return AdmissionVerdict(False, "unreadable")
        if limits.limit == 0 and limits.total == 0:
            self._admit(bucket, route, task_id, left=left)
            return AdmissionVerdict(True, "unlimited", limits=limits)
        asker = (route, task_id)
        others = [w for w in self._deferred.get(bucket, ()) if w != asker]
        room = await self._headroom(
            limits, project, bucket, own=asker, ordering=bool(others)
        )
        facts = (len(room.gates), room.escalated, limits, room.urls, room.running)
        if room.at_cap:
            self._defer(bucket, route, task_id, left=left)
            await self._notify_held(
                task_id, bucket, room.gates, limits, room.escalated, room.running
            )
            return AdmissionVerdict(False, "total_cap", *facts)
        if room.free == 0:
            self._defer(bucket, route, task_id, left=left)
            self._held_notified.discard(bucket)  # under the cap: re-arm
            return AdmissionVerdict(False, "limit", *facts)
        # Slots are free. They go to the front of the bucket's release order
        # — which may not include the asker (ADR 0012): the choice is made
        # here so the order cannot depend on whose nudge arrived first. With
        # no other wait there is nothing to order and no read.
        if not others:
            self._admit(bucket, route, task_id, left=left)
            return AdmissionVerdict(True, "admitted", *facts)
        order = await self._release_order(bucket, asker=asker)
        entitled = order[: room.free]
        if asker not in [w for w, _t in entitled]:
            self._defer(bucket, route, task_id, left=left)
            self._held_notified.discard(bucket)
            logger.info(
                "%s: %s is queued behind %s in bucket %r (%d free slot(s)); "
                "nudging the head",
                _SUBSYSTEM,
                task_id,
                ", ".join(dict.fromkeys(s for (_r, s), _t in entitled)),
                bucket,
                room.free,
            )
            await self._nudge_all(entitled, asker=None, unanswered=True)
            return AdmissionVerdict(False, "queued", *facts)
        self._admit(bucket, route, task_id, left=left)
        # the places left go to the next in order
        await self._nudge_all(entitled, asker=asker, unanswered=False)
        return AdmissionVerdict(True, "admitted", *facts)

    async def _headroom(
        self,
        limits: AdmissionLimits,
        project: str | None,
        bucket: str,
        *,
        own: tuple[str, str] | None,
        ordering: bool,
    ) -> _Headroom:
        """The bucket's count against both dials — *own* is the asker's own
        reservation, not counted against itself. Escalated gates are read
        when a dial is in question, and always when *ordering*: the free
        count then decides how many held stories are nudged, and an
        under-count would leave a slot to the sleepers (review round 3)."""
        gates = await open_gates(self._lithos, GATE_TYPE_PR, project)
        running = len(self._in_flight.get(bucket, set()) - ({own} if own else set()))
        total = len(gates) + running
        at_cap = bool(limits.total) and total >= limits.total
        at_limit = bool(limits.limit) and total >= limits.limit
        if at_cap or at_limit:
            # #372: a gate whose story is already terminal (the work landed
            # via another PR, the issue mirror completed it, the PR stayed
            # open) is the operator's PR, not loom's work-in-progress — it
            # counts against neither cap. Read only when a cap is in
            # question; the sweep completes such gates on its next pass.
            gates = await live_gates(self._lithos, gates)
            total = len(gates) + running
            at_cap = bool(limits.total) and total >= limits.total
            at_limit = bool(limits.limit) and total >= limits.limit
        escalated = 0
        if at_cap or at_limit or ordering:
            escalated = await escalated_count(self._lithos, project, gates)
        return _Headroom(limits, gates, running, escalated, at_cap)

    async def wake(self, project: str | None) -> int:
        """A slot in *project*'s bucket may have freed: republish the held
        stories entitled to the free slots, in release order (the waker's
        sweep). Returns how many were nudged; a story that could not be
        read keeps its place but gets no nudge (its own sleeper re-asks).
        Each nudge re-enters :meth:`admit`, which enforces the same order at
        the ask."""
        bucket = _bucket(project)
        async with self._lock(bucket):
            return await self._wake_locked(bucket)

    async def _wake_safely(self, bucket: str) -> None:
        try:
            await self._wake_locked(bucket)
        except Exception:
            logger.exception(
                "%s: could not wake bucket %r; the held stories' re-check "
                "sleepers re-ask",
                _SUBSYSTEM,
                bucket,
            )

    async def _wake_locked(self, bucket: str) -> int:
        if not self._deferred.get(bucket):
            return 0
        free = await self._free_now(bucket)
        if free == 0:
            return 0  # nothing to give: a nudge would only be a refusal
        order = await self._release_order(bucket, asker=None)
        entitled = order if free is None else order[:free]
        nudged = await self._nudge_all(entitled, asker=None, unanswered=True)
        if not nudged and self._deferred.get(bucket):
            logger.warning(
                "%s: bucket %r has %s and held stories, but none could be "
                "nudged (unreadable, or no route matches them any more)",
                _SUBSYSTEM,
                bucket,
                f"{free} free slot(s)" if free is not None else "unknown headroom",
            )
        return nudged

    async def _free_now(self, bucket: str) -> int | None:
        """How many stories the bucket could admit right now, or ``None``
        when that cannot be known (unlimited, or a read failed — then every
        held story is nudged and each ask decides for itself)."""
        project = bucket or None
        try:
            limits = await limits_for(self._lithos, project, self._defaults)
            if limits is None or (limits.limit == 0 and limits.total == 0):
                return None
            room = await self._headroom(
                limits, project, bucket, own=None, ordering=True
            )
        except Exception as exc:
            logger.warning(
                "%s: cannot count bucket %r's headroom (%s); nudging every held "
                "story instead",
                _SUBSYSTEM,
                bucket,
                exc,
            )
            return None
        return 0 if room.at_cap else room.free

    async def _release_order(
        self, bucket: str, *, asker: tuple[str, str] | None
    ) -> list[tuple[tuple[str, str], Any]]:
        """The bucket's ``(route, story)`` waits — plus *asker* — in the
        order they leave (ADR 0012): priority rank descending, then the
        order the story first asked, then the route name. One read per
        story: the priority is the operator's live word. A held story is
        dropped when it is no longer open (or gone); a wait is dropped when
        its story no longer carries that route's match tags (re-tagged to
        park it, or onto another route: that runner would never receive
        the nudge, and as head the wait would hold the bucket — the story
        keeps its place for when it asks again). One that cannot be read —
        for ANY reason, the raw transport errors the client re-raises
        included: one story's read never loses the sweep — keeps its place
        at the rank it was last seen (the default if never) and is returned
        without a task, so it cannot be nudged. The asker is never dropped:
        whether it is still open is the claim's to tell. One story's waits
        share its rank and place, and the asker's own sorts first among
        them: the guarantee is deterministic STORY selection — which of a
        story's routes runs first is whichever asks first, because a nudge
        for the story reaches every route that matches it, so refusing the
        asker in favour of a sibling route would nudge the asker straight
        back into the same refusal (PR #398 review)."""
        waits = set(self._deferred.get(bucket, ()))
        if asker is not None:
            waits.add(asker)
        asking = asker[1] if asker is not None else None
        read: dict[str, tuple[int, Any]] = {}  # story → (rank, task | None)
        for story in sorted({s for _r, s in waits}):
            try:
                task = await self._lithos.task_get(task_id=story)
                if story != asking and (task is None or task.status != "open"):
                    self._drop_story(story)
                    continue
                home = _bucket(project_of(task.metadata)) if task else bucket
                for wait in [w for w in waits if w[1] == story and w != asker]:
                    if home != bucket:
                        # PR #398 review: a story moved to another project
                        # is not this bucket's head; it asks there, and the
                        # wait it would leave behind must not hold this one.
                        logger.info(
                            "%s: held story %s now lives in bucket %r, not %r; "
                            "dropping its wait (it keeps its place; its own "
                            "re-check re-asks under its project)",
                            _SUBSYSTEM,
                            story,
                            home,
                            bucket,
                        )
                    elif self._matched(task, route=wait[0]):
                        continue
                    else:
                        logger.info(
                            "%s: route %s no longer matches held story %s; "
                            "dropping its wait (the story keeps its place)",
                            _SUBSYSTEM,
                            wait[0],
                            story,
                        )
                    self._drop_wait(wait)
                    waits.discard(wait)
                if not any(s == story for _r, s in waits):
                    continue
                rank = _rank(task.metadata if task is not None else None)
                self._rank_seen[story] = rank
            except Exception as exc:
                rank, task = self._rank_seen.get(story, _DEFAULT_RANK), None
                logger.warning(
                    "%s: cannot read held story %s (%s); it keeps its place at "
                    "its last-seen priority and its re-check sleeper re-asks",
                    _SUBSYSTEM,
                    story,
                    exc,
                )
            read[story] = (rank, task)
        ranked = [
            (-read[s][0], self._first_asked.get(s, sys.maxsize), s, (r, s) != asker, r)
            for r, s in waits
            if s in read
        ]
        return [((r, s), read[s][1]) for _rank, _since, s, _mine, r in sorted(ranked)]

    def _matched(self, task: Any, *, route: str) -> bool:
        """Whether *route*'s runner would receive a nudge for *task*: its
        subscription requires every one of the route's match tags."""
        return set(self._route_tags.get(route, ())) <= set(task.tags or ())

    async def _nudge_all(
        self,
        entitled: Sequence[tuple[tuple[str, str], Any]],
        *,
        asker: tuple[str, str] | None,
        unanswered: bool,
    ) -> int:
        """Republish each story with an entitled wait other than the asker's
        own, once — the waker's shape (origin
        :data:`ADMISSION_RECHECK_ORIGIN`). Returns how many went out."""
        nudged = 0
        seen: set[str] = set()
        for wait, task in entitled:
            story = wait[1]
            if wait == asker:
                continue
            if task is None:
                logger.info(
                    "%s: %s is next but could not be read; its re-check "
                    "sleeper re-asks",
                    _SUBSYSTEM,
                    story,
                )
                continue
            if unanswered:
                count = self._unanswered[wait] = self._unanswered.get(wait, 0) + 1
                if count % _UNANSWERED_NAG_EVERY == 0:
                    logger.warning(
                        "%s: %s has been nudged %d times as next in line for "
                        "route %s without asking admission — is it on the "
                        "ready frontier, and is that runner alive?",
                        _SUBSYSTEM,
                        story,
                        count,
                        wait[0],
                    )
            if story in seen:
                continue  # counted for this wait; published once per story
            seen.add(story)
            await self._bus.publish(_nudge_event(task))
            nudged += 1
        return nudged

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


def _nudge_event(task: Any) -> Event:
    return Event(
        type="lithos.task.updated",
        timestamp=datetime.now(UTC),
        payload=task_payload(task),
        origin=ADMISSION_RECHECK_ORIGIN,
    )


def _rank(metadata: Any) -> int:
    """Where ``metadata.priority`` puts a story in the release order; an
    absent, non-string or unknown value is the default rank."""
    priority = (metadata or {}).get("priority")
    if isinstance(priority, str) and priority in _RANK_ORDER:
        return _RANK_ORDER.index(priority)
    return _DEFAULT_RANK


def _url_of(gate: Any) -> str:
    url = (gate.metadata or {}).get("pr_url")
    return url if isinstance(url, str) and url else f"gate {gate.id}"
