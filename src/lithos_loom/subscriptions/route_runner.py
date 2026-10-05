"""RouteRunner — claim-bound bus subscriber that runs plugins.

A special subscriber type sitting on the in-process :class:`EventBus`.
It listens for ``lithos.task.created`` / ``lithos.task.updated`` /
``lithos.task.released`` events whose tags match the route's
``RouteMatch.tags``, claims the task via Lithos, runs the configured plugin
subprocess, and applies the resulting status:

* ``status="succeeded"`` → ``task_complete`` (releases all claims), or for a
  ``completes_task = false`` route a ``pr`` gate + ``task_release``
  (:mod:`.delivery_gate`)
* ``status="failed"`` (and every other non-delivering exit) → a loom
  ``human`` gate blocking the story + ``[NeedsHuman]`` finding +
  ``task_release`` (:mod:`.escalation`, b91177d2); ``[BlockerFailed]`` only
  on the marker-only fallback
* ``status="interrupted"`` → ``task_release`` (no finding — operator
  signal, not an error). When the result also carries a ``resume`` block
  (``resume_after`` timestamp — e.g. a story-develop run checkpointed on a
  provider usage limit), the runner schedules an in-process re-dispatch:
  at ``resume_after`` it re-checks the task is still open, drops it from
  the dedup set, and re-claims + re-runs. Bounded by
  ``MAX_RESUMES_PER_TASK``. The schedule is **durable** (U1, task
  250d231f): the time and the attempts used are recorded on the task
  (:mod:`.resume_record`, ``metadata.loom_resume:<route>``), consumed at
  the claim, and honoured on every event — so a restart during the wait
  re-arms the timer from the record instead of re-bootstrapping the open
  task straight back into the wall, and the attempt count survives.

The runner is instantiated directly by the route-runner child entry
point (one runner per route) — it does **not** go through the
``lithos_loom.subscriptions`` entry-point registry, because routes have
distinct semantics (claim-bound, plugin-driven) from the generic fire-
and-forget subscriptions that registry serves. Routes and subscriptions
share an internal type but are distinct TOML stanzas.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import shutil
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lithos_loom.bus import Event, EventBus, Subscription
from lithos_loom.config import RouteConfig
from lithos_loom.errors import LithosClientError, PluginContractError
from lithos_loom.gates import is_plain_run_id
from lithos_loom.plugin_runner import run_plugin
from lithos_loom.plugins.story_develop.checkpoint import (
    RESUMABLE_ESCALATION_REASONS,
    resumable_checkpoint,
)
from lithos_loom.subscriptions.brief_review_approval import apply_brief_approval
from lithos_loom.subscriptions.delivery_gate import gate_and_release
from lithos_loom.subscriptions.dispatch_guards import (
    AttemptStampStore,
    clear_superseded_failure,
    declines_bootstrap_replay,
    failed_attempt_for_route,
    on_ready_frontier,
    project_of,
    release_with_failure,
    resolve_command,
    task_payload,
)
from lithos_loom.subscriptions.escalation import (
    Escalation,
    clear_resolved_escalation,
    escalate_with_failure,
    escalation_from_result,
)
from lithos_loom.subscriptions.escalation_resolver import GATE_RESOLVED_ORIGIN
from lithos_loom.subscriptions.ready_recheck import ReadyRechecker
from lithos_loom.subscriptions.resume_record import (
    ResumeRecord,
    consume_resume_record,
    parse_resume_after,
    resume_record_for_route,
    write_resume_record,
)

__all__ = ["RESUME_DUE_SLACK_SECONDS", "RESUME_ORIGIN", "PluginRunFn", "RouteRunner"]

# The origin of the sleeper's own re-dispatch event (a synthetic
# `loom.route.resume`, never a bootstrap replay). It is NOT exempt from the
# pending-resume check: the sleeper disarms itself before the fresh re-read, so
# a record whose time still stands is due and dispatches, and one the operator
# moved later arms a fresh timer instead (PR #439 review).
RESUME_ORIGIN = "resume"

# A recorded resume time this close is "now": the sleeper's wake and the record
# are measured on two clocks (the loop's monotonic one, the wall clock), and a
# fire a few milliseconds ahead must not re-arm for the remainder.
RESUME_DUE_SLACK_SECONDS = 1.0

logger = logging.getLogger(__name__)


def _contained_in(child: Path, parent: Path) -> bool:
    """Whether *child* resolves to a direct child of *parent* (security/f-001).

    The belt to :func:`~lithos_loom.gates.is_plain_run_id`'s braces: the handle
    check already excludes a separator, but the join is checked too, so a future
    caller that loosens one is still stopped by the other. Symlinks resolve, so a
    run dir pointed outside the work dir does not pass either. An unresolvable
    path is not contained.
    """
    try:
        return child.resolve().parent == parent.resolve()
    except OSError:
        return False


def resumable_checkpoint_under(work_dir: Path) -> Path | None:
    """The first run dir under *work_dir* whose checkpoint has a committed round.

    "Is there anything here a re-dispatch would continue?" — asked of the whole
    per-task dir rather than of one run id, because the cleanup decision is
    about the directory (correctness/f-002) and a task may retain several runs.
    """
    if not work_dir.is_dir():
        return None
    try:
        children = sorted(work_dir.iterdir())
    except OSError:
        return None
    for run_dir in children:
        if run_dir.is_dir() and resumable_checkpoint(run_dir) is not None:
            return run_dir
    return None


def leaves_a_resumable_run(result: Mapping[str, Any]) -> bool:
    """Whether *result*'s exit is one a later dispatch may CONTINUE on its branch.

    The host verdicts (:data:`RESUMABLE_ESCALATION_REASONS`, read through the
    same ``escalation_from_result`` the escalation itself uses, so the two can
    never disagree) plus ``interrupted`` — whose designed recovery IS a
    re-dispatch (T10) and whose exhaustion escalates as ``resume_exhausted``,
    by which time the checkpoint must still exist.
    """
    status = result.get("status")
    if status == "interrupted":
        return True
    if status != "failed":
        return False
    reason = escalation_from_result(result, detail="").reason
    return reason in RESUMABLE_ESCALATION_REASONS


PluginRunFn = Callable[..., Awaitable[Mapping[str, Any]]]
"""Signature ``run_plugin`` exposes; injectable for tests."""


_HANDLED_EVENT_TYPES = (
    "lithos.task.created",
    "lithos.task.updated",
    "lithos.task.released",
)
# `updated` is treated as "re-evaluate match," not "always run" (issue #86):
# adding a route's trigger tag to an already-open task arrives as
# `lithos.task.updated` and should dispatch without a daemon restart. The
# `_handle` guards below make this safe against self-triggering — a plugin's
# own end-of-run `task_update` (e.g. story-develop writing `develop_*`
# metadata) fires `updated`, but the task is already in `_processed_tasks`
# (claimed this process) so it's skipped. Before lithos#283 Lithos emitted no
# `updated` event at all, which is why the original two-tuple was complete.

# Re-dispatch budget for `interrupted` results carrying a `resume` block.
# Each resume re-runs the full plugin (container spin-up + agent spend), so
# a run that keeps hitting its provider limit must not retry unbounded —
# after this many resumes the task is escalated (needs-human gate, reason
# resume_exhausted). Distinct from the failure retry budget (issue #11):
# resume is "try again after the limit lifts", not "retry a failure".
MAX_RESUMES_PER_TASK = 3

# `_plan_resume`'s "the recorded attempts meet the budget" answer.
_EXHAUSTED = object()


@dataclass
class RouteRunner:
    """One claim-bound subscriber per route.

    Attributes
    ----------
    route:
        The route configuration this runner serves. ``route.match.tags``
        becomes the bus filter; ``route.command`` is the plugin template;
        ``route.max_runtime_seconds`` caps each plugin invocation.
    bus:
        The in-process bus this runner subscribes against.
    lithos:
        A live :class:`lithos_loom.lithos_client.LithosClient` (or any
        object that quacks like one for tests).
    agent_id:
        The Lithos agent identity used for ``task_claim`` / ``task_renew``
        / ``task_release`` / ``task_complete`` / ``finding_post``.
    work_dir_base:
        Per-task staging directories are created at
        ``work_dir_base / <task_id>``.
    renew_interval_seconds:
        How often the renewer task calls ``task_renew``. Should be less
        than the claim TTL; defaults to 60s, matching the claim default.
    retain_failed_workdirs:
        When ``True`` (default), the work dir is left behind on plugin
        failure for operator inspection. ``False`` reaps it — except when the
        run is one a later dispatch may CONTINUE (a host death's checkpointed
        branch, an interrupted run's sessions): see
        :meth:`_cleanup_work_dir`.
    plugin_runner:
        Injectable subprocess-runner. Defaults to the real
        :func:`lithos_loom.plugin_runner.run_plugin`. Tests inject an
        ``AsyncMock`` to bypass real subprocess work.
    project_repos:
        Map of project slug → on-disk repo path, from the host's
        ``[projects.*]`` TOML. A route command may carry a ``{{repo}}``
        token; the runner resolves it per task from this map keyed by
        ``task.metadata.project``, so one generic route can serve every
        registered project instead of baking an absolute path into the
        command. Empty by default — routes that don't use ``{{repo}}``
        don't need it.
    notifier:
        The push-notification sinks (:class:`~lithos_loom.notifications.Notifier`)
        fired once when a run ends without delivering and a needs-human gate
        is raised (b91177d2). ``None`` → no push; the gate + finding still
        land (the pull surfaces).
    admission:
        Serial admission (PRD S6, ``subscriptions.admission.Admission``), asked
        after readiness, before the claim, on PR-producing routes only.
    """

    route: RouteConfig
    bus: EventBus
    lithos: Any
    agent_id: str
    work_dir_base: Path
    renew_interval_seconds: float = 60.0
    retain_failed_workdirs: bool = True
    plugin_runner: PluginRunFn = field(default=run_plugin)
    project_repos: Mapping[str, Path] = field(default_factory=dict)
    notifier: Any = None
    admission: Any = None

    def __post_init__(self) -> None:
        # #339: per-(route, task) updated_at stamps for the exact
        # failed-retry guard — beside the SSE cursor, same durability class.
        self._attempt_stamps = AttemptStampStore(
            self.work_dir_base / "route-runner" / "attempt_stamps"
        )
        # PR #352 review F2: an undetermined readiness is re-asked, not dropped.
        self._rechecker = ReadyRechecker(
            bus=self.bus, lithos=self.lithos, route=self.route.name
        )
        self._subscription: Subscription = self.bus.subscribe(
            event_types=_HANDLED_EVENT_TYPES,
            match={"tags": list(self.route.match.tags)},
            name=f"route-runner-{self.route.name}",
        )
        # Tasks this runner has successfully claimed. Future events for
        # the same id are skipped — without this, multiple stale events
        # queued for the same open task would each run the plugin,
        # relying on Lithos's claim_failed envelope for safety. Real
        # Lithos enforces it, but the runner should too. This is also what
        # makes subscribing to `lithos.task.updated` (issue #86) safe: a
        # plugin's own end-of-run `task_update` fires `updated` for a task
        # we've already claimed this process, and that event is dropped here
        # rather than re-running the plugin.
        #
        # Important: this set is also what suppresses re-attempts after a
        # plugin failure. When the plugin fails we release the claim and
        # post a [BlockerFailed] finding; Lithos then emits
        # lithos.task.released; that event hits this dedup check and is
        # silently skipped. The effect is "fail once per task per daemon
        # process" — deliberate, to avoid tight retry loops when a plugin
        # is deterministically broken — extended across restarts by the
        # persisted `loom_last_attempt:<route>` marker (dispatch_guards)
        # until an edit or marker deletion asks for another run. A proper
        # retry budget lives in follow-up issue #11. The lost-claim-race
        # path below (claim_failed) deliberately does NOT add to this set,
        # so a subsequent released event there does re-attempt the claim.
        self._processed_tasks: set[str] = set()
        # T10: pending usage-limit re-dispatches (task id → sleeper task).
        # The timer is the in-process half; the time and the attempts used
        # live on the task (U1, `resume_record`) — a restart re-arms the
        # timer from the record when the bootstrap replays the open task,
        # and the count is read from the dispatch payload, never kept here.
        self._resume_tasks: dict[str, asyncio.Task[None]] = {}
        # …and the instant each armed sleeper is for, so an event carrying the
        # same record is a no-op and one carrying an edited time re-arms.
        self._resume_due: dict[str, datetime] = {}
        # #407 slice 3 (`lithos-loom drain`): once draining, no new claim —
        # `_handle` refuses before any Lithos read and again right before
        # the claim (readiness + admission are awaits a drain can begin
        # under). `_idle` is clear exactly while a claimed run is in flight
        # — a COUNT of runs, not a flag: `_handle` has two owners (the bus
        # loop and the usage-limit resume sleeper), so two runs can be in
        # flight and the first to end must not read as idle.
        self._draining = False
        self._runs_in_flight = 0
        self._idle = asyncio.Event()
        self._idle.set()

    # ── drain ─────────────────────────────────────────────────────────

    def begin_drain(self) -> None:
        """Refuse every new dispatch from now on; the run in flight finishes."""
        if not self._draining:
            logger.info(
                "RouteRunner %s: draining — no new claims; %s",
                self.route.name,
                f"waiting for {self._runs_in_flight} run(s) in flight"
                if self._runs_in_flight
                else "idle",
            )
        self._draining = True

    async def drained(self) -> None:
        """Return once no claimed run is in flight (at once when idle)."""
        await self._idle.wait()

    def _run_started(self) -> None:
        self._runs_in_flight += 1
        self._idle.clear()

    def _run_ended(self) -> None:
        self._runs_in_flight -= 1
        if self._runs_in_flight == 0:
            self._idle.set()

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
                    "RouteRunner %s: unhandled error processing %s",
                    self.route.name,
                    event.type,
                )

    # ── handle ────────────────────────────────────────────────────────

    async def _handle(self, event: Event) -> None:
        payload = event.payload
        task_id = str(payload.get("id") or "")
        if not task_id:
            return
        if payload.get("status") != "open":
            return  # nothing to do for terminal-state observations
        if self._draining:
            logger.debug(
                "RouteRunner %s: draining — not dispatching %s",
                self.route.name,
                task_id,
            )
            return
        if event.origin == GATE_RESOLVED_ORIGIN:
            # b91177d2: the operator completed this story's needs-human gate —
            # THE retry gesture. The in-process "fail once per task" set
            # (below) would otherwise swallow it until a restart, and the T10
            # resume budget starts fresh by construction (PR #349 review F3:
            # a retry the operator authorized deserves a full bounded resume
            # window) — exhaustion writes no resume record, so there is no
            # count left on the task to reset.
            self._processed_tasks.discard(task_id)
        metadata = payload.get("metadata") or {}
        # U1: a re-dispatch this route already scheduled — recorded on the task,
        # so it is still here after a restart — is honoured on every origin but
        # the operator's gate tick, and BEFORE the in-process dedup: the record
        # is the authority over the armed timer, so a hand-edited time on an
        # already-processed task moves the timer rather than being swallowed
        # (PR #439 review), and the sleeper's own fire re-reads it. Payload-only,
        # so before the readiness round trip; before the bootstrap decline too,
        # though the two cannot coexist (the interrupted write clears the
        # marker, and a failure only ever follows a consumed record).
        if event.origin != GATE_RESOLVED_ORIGIN:
            pending = resume_record_for_route(metadata, self.route.name)
            if pending is not None and self._honour_pending_resume(task_id, pending):
                return
        if task_id in self._processed_tasks:
            logger.debug(
                "RouteRunner %s: skipping stale event for already-processed %s",
                self.route.name,
                task_id,
            )
            return

        # A bootstrap replay must not re-develop a story whose last attempt
        # FAILED and that nobody edited since — deliberate retry paths arrive
        # live or change the fingerprint (rationale + residual crash-window
        # leak: dispatch_guards docstring). Before `_is_ready`: the marker is
        # in the payload; readiness costs a Lithos round trip.
        if event.origin == "bootstrap" and declines_bootstrap_replay(
            metadata,
            self.route.name,
            payload,
            task_id=task_id,
            stamps=self._attempt_stamps,
        ):
            return

        # A delivered (completes_task=false) story is blocked by its `pr`
        # gate (Epic H) and so is absent from `task_ready` — the readiness
        # check below defers it, including across a restart's bootstrap
        # replay (US11 retired the `loom_delivered` short-circuit). FAILED
        # stories are the decline above; readiness guards blocked + gated.
        ready = await self._is_ready(task_id, metadata)
        # S6: PR-producing routes claim only under the project's delivered-PR limit.
        admission = None if self.route.completes_task else self.admission
        if not ready and admission is not None:
            # ADR 0012: admission releases held stories in order and nudges
            # the head; a head that cannot ask — not ready, or a readiness
            # Lithos will not settle or could not read — must step aside or
            # it holds everything behind it. It keeps its place for when it
            # asks again.
            await admission.forget(task_id, route=self.route.name)
        if ready is None:
            self._rechecker.schedule(task_id)
            return
        if not ready:
            self._rechecker.settled(task_id)  # a definitive answer: fresh budget
            logger.info(
                "RouteRunner %s: deferring %s — not on Lithos's ready frontier",
                self.route.name,
                task_id,
            )
            return
        if admission is not None:
            verdict = await admission.admit(
                route=self.route.name,
                task_id=task_id,
                project=project_of(metadata),
                tags=self.route.match.tags,
            )
            if not verdict.admitted:
                logger.info(
                    "RouteRunner %s: holding %s — %s (%d delivered PR(s) open, "
                    "%d escalated, %d in flight; %s)",
                    self.route.name,
                    task_id,
                    verdict.reason,
                    verdict.open_gates,
                    verdict.escalated,
                    verdict.in_flight,
                    verdict.limits,
                )
                self._rechecker.schedule(task_id, why="held by serial admission")
                return
        self._rechecker.settled(task_id)  # admitted: fresh budget
        try:
            if self._draining:
                # the drain began while readiness / admission were read: no
                # await between this check and `_run_started()`, so a
                # `drained()` that saw idle can never be followed by a claim
                logger.info(
                    "RouteRunner %s: draining — not claiming %s",
                    self.route.name,
                    task_id,
                )
                return
            self._run_started()
            try:
                await self._claim_and_run(task_id, payload)
            finally:
                self._run_ended()
        finally:
            if admission is not None:  # the reservation ends with the run
                await admission.release(task_id, route=self.route.name)

    async def _claim_and_run(self, task_id: str, payload: Mapping[str, Any]) -> None:
        try:
            await self.lithos.task_claim(
                task_id=task_id, aspect=self.route.name, agent=self.agent_id
            )
        except LithosClientError as exc:
            if exc.code == "claim_failed":
                # Another runner won the race. Don't add to processed —
                # if they release the claim, the lithos.task.released
                # event will land here again and we'll re-attempt. This
                # is the only path where released triggers a re-claim;
                # for the won-claim-then-plugin-fail path, see the
                # comment on _processed_tasks above (issue #11).
                logger.debug(
                    "RouteRunner %s: lost claim race for %s",
                    self.route.name,
                    task_id,
                )
                return
            raise

        # Claim succeeded; remember so duplicate queued events for the same
        # task ID are skipped rather than racing into a second plugin run.
        self._processed_tasks.add(task_id)
        logger.info("RouteRunner %s: claimed %s", self.route.name, task_id)
        # 604fb936: a story whose brief-review gate was just completed carries
        # an approved addendum — appended HERE, on every origin, before the
        # escalation is cleared and task.json is written (readiness flipped the
        # moment the gate completed, so no other point sees every dispatch).
        try:
            approval = await apply_brief_approval(
                self.lithos,
                task_id=task_id,
                route=self.route.name,
                agent=self.agent_id,
                payload=payload,
            )
        except Exception:
            # Unread is not refused: raising a draft-less gate beside the
            # approved one would make the next approval ambiguous. Free the
            # story; its re-check asks again.
            logger.exception(
                "RouteRunner %s: could not read %s's brief-review approval; "
                "releasing it to retry",
                self.route.name,
                task_id,
            )
            self._processed_tasks.discard(task_id)
            with contextlib.suppress(Exception):
                await self.lithos.task_release(
                    task_id=task_id, aspect=self.route.name, agent=self.agent_id
                )
            self._rechecker.schedule(task_id, why="brief-review approval unread")
            return
        if approval.refused is not None:
            await self._escalate(
                task_id, approval.refused, payload=payload, run_id=approval.run_id
            )
            return
        if approval.applied and approval.payload is not None:
            payload = approval.payload
        # b91177d2: a story carrying a needs-human gate id that has just passed
        # the readiness check has had its gate resolved — clear the provenance
        # + the failed-attempt marker HERE, on every origin, so the loop's
        # correctness never depends on the live resolver nudge (a gate ticked
        # while the daemon was down arrives via bootstrap, not the resolver).
        await clear_resolved_escalation(
            self.lithos,
            task_id=task_id,
            route=self.route.name,
            agent=self.agent_id,
            payload=payload,
            stamps=self._attempt_stamps,
        )
        # U1: the re-dispatch a resume record scheduled is now under way — the
        # record is consumed here, on every origin, so "present" keeps meaning
        # "scheduled and not started". The payload still carries it for the
        # attempt count the interrupted path reads, and for the resume pointer.
        await consume_resume_record(
            self.lithos,
            task_id=task_id,
            route=self.route.name,
            agent=self.agent_id,
            payload=payload,
        )
        await self._run_claimed_task(task_id, payload)

    def _resume_pointer(
        self, task_id: str, payload: Mapping[str, Any], work_dir: Path
    ) -> dict[str, str] | None:
        """The dead run this dispatch should CONTINUE, for the task envelope.

        5dbeb0c8 slice C. An infra death (a revoked token, a vanished coder
        container) is a verdict on the host, not on the work: its rounds are
        committed on a branch in its worktree and its checkpoint says where —
        so the re-dispatch the operator's gate tick asks for should resume
        there rather than pay for those rounds twice. The signal is the story's
        own failed-attempt marker as it stood at dispatch time (this payload;
        the claim path has since cleared it server-side): the route's last
        attempt failed for a host reason, and it names the run.

        The other signal (U1) is the route's resume record: a usage-limited run
        checkpointed on its branch too, and its designed recovery IS this
        re-dispatch — paying its rounds again after waiting out the limit would
        defeat the wait. The record names the run; the reason is
        ``usage_limited``.

        ``None`` — no marker or record, a stop that is a verdict on the WORK
        (``max_rounds`` / ``stalled`` / ``disputed`` / …, which is a separate
        question and deliberately not this), an unknown run, or a run with no
        committed round — means an ordinary dispatch, exactly as before. The
        plugin re-validates the pointer against its own work dir and falls back
        the same way, so a pointer is never load-bearing.

        The ``run_id`` in the marker came from a plugin's ``result.json`` — the
        subprocess contract of an operator-configured route, not in-process
        state — and this is the one place it is JOINED ONTO A PATH, so it is
        constrained to a plain handle and the join is checked for containment
        (security/f-001). Without both, ``../<other-task>/<run>`` reaches another
        story's run dir under the same work-dir base (story A's dispatch would
        continue story B's branch and deliver it as A's PR) and an ABSOLUTE
        ``run_id`` discards the join altogether, letting any local writer supply
        the branch, the intake handoffs and the budget remainder.
        """
        metadata = payload.get("metadata") or {}
        run_id: Any
        marker = failed_attempt_for_route(metadata, self.route.name)
        if marker is not None:
            reason = marker.get("reason")
            if (
                not isinstance(reason, str)
                or reason not in RESUMABLE_ESCALATION_REASONS
            ):
                return None
            run_id = marker.get("run_id")
        else:
            record = resume_record_for_route(metadata, self.route.name)
            if record is None or record.run_id is None:
                return None
            reason, run_id = record.reason, record.run_id
        if not isinstance(run_id, str) or not is_plain_run_id(run_id):
            if run_id:
                logger.warning(
                    "RouteRunner %s: %s's failed-attempt marker names run_id %r, "
                    "which is not a plain handle; not resuming",
                    self.route.name,
                    task_id,
                    run_id[:80],
                )
            return None
        run_dir = work_dir / run_id
        if not _contained_in(run_dir, work_dir):
            logger.warning(
                "RouteRunner %s: %s's named run dir %s is not under %s; not resuming",
                self.route.name,
                task_id,
                run_dir,
                work_dir,
            )
            return None
        if resumable_checkpoint(run_dir) is None:
            logger.info(
                "RouteRunner %s: %s's last run %s died (%s) but left no "
                "resumable checkpoint; developing from scratch",
                self.route.name,
                task_id,
                run_id,
                reason,
            )
            return None
        logger.info(
            "RouteRunner %s: resuming %s on run %s's branch (last attempt: %s)",
            self.route.name,
            task_id,
            run_id,
            reason,
        )
        return {"run_dir": str(run_dir), "reason": reason}

    async def _is_ready(self, task_id: str, metadata: Mapping[str, Any]) -> bool | None:
        """Membership test on Lithos's ready frontier — see
        ``dispatch_guards.on_ready_frontier`` (US4). ``None`` = undetermined."""
        return await on_ready_frontier(
            self.lithos,
            task_id=task_id,
            tags=self.route.match.tags,
            metadata=metadata,
            route=self.route.name,
        )

    async def _re_dispatch_unblocked(self, task_ids: Sequence[str]) -> None:
        """Re-evaluate route matching for tasks Lithos just unblocked (US6).

        ``task_complete`` returns the tasks whose last blocker cleared as a
        result of this completion. Rather than wait for a ``task.updated``
        round-trip, republish each onto the bus as a synthetic
        ``lithos.task.updated``: the bus's tag filter routes it to whichever
        route matches — usually a *different* route than this one — and the
        normal ``_handle`` path (ready check, then collision-safe claim)
        applies unchanged. Double-evaluation is harmless; the claim decides.

        Never raises: the task is already completed, so a failed nudge must not
        surface as a run error; a later event or the bootstrap re-surfaces it.
        """
        for unblocked_id in task_ids:
            try:
                task = await self.lithos.task_get(task_id=unblocked_id)
                if task is None or task.status != "open":
                    continue
                await self.bus.publish(
                    Event(
                        type="lithos.task.updated",
                        timestamp=datetime.now(UTC),
                        payload=task_payload(task),
                    )
                )
                logger.info(
                    "RouteRunner %s: re-dispatching newly-unblocked %s",
                    self.route.name,
                    unblocked_id,
                )
            except Exception:
                logger.exception(
                    "RouteRunner %s: could not re-dispatch newly-unblocked %s",
                    self.route.name,
                    unblocked_id,
                )

    # ── claimed-task lifecycle ────────────────────────────────────────

    async def _run_claimed_task(self, task_id: str, payload: Mapping[str, Any]) -> None:
        try:
            command = resolve_command(self.route.command, payload, self.project_repos)
        except PluginContractError as exc:
            # Token present but unresolvable: release with a finding before
            # any work-dir / plugin spend, same as a contract violation.
            await self._release_with_finding(
                task_id, f"route misconfigured: {exc}", payload=payload
            )
            return

        work_dir = self.work_dir_base / task_id
        work_dir.mkdir(parents=True, exist_ok=True)
        task_json_path = work_dir / "task.json"
        result_file = work_dir / "result.json"
        envelope: dict[str, Any] = {"task": dict(payload)}
        resume = self._resume_pointer(task_id, payload, work_dir)
        if resume is not None:
            envelope["resume"] = resume
        task_json_path.write_text(json.dumps(envelope))

        renew_task = asyncio.create_task(
            self._renew_loop(task_id), name=f"renew-{task_id}"
        )
        succeeded = False
        # Whether this exit leaves a run the NEXT dispatch may continue — the
        # host verdicts, plus `interrupted`, whose own recovery is a re-dispatch
        # and whose exhaustion escalates as `resume_exhausted`. Read here, where
        # the exit is known, because `_cleanup_work_dir` may not delete such a
        # run's checkpoint (correctness/f-002).
        resumable = False
        try:
            try:
                result = await self.plugin_runner(
                    command=command,
                    task_json_path=task_json_path,
                    work_dir=work_dir,
                    result_file=result_file,
                    max_runtime_seconds=self.route.max_runtime_seconds,
                )
            except PluginContractError as exc:
                # No usable result — the runner has only the exception and
                # the retained work dir to brief the human with.
                await self._escalate(
                    task_id,
                    Escalation(
                        reason="contract_violation",
                        summary=f"plugin contract violation: {exc}",
                        brief={"work_dir": str(work_dir)},
                    ),
                    payload=payload,
                )
            except TimeoutError as exc:
                await self._escalate(
                    task_id,
                    Escalation(
                        reason="timeout",
                        summary=f"plugin exceeded max runtime: {exc}",
                        brief={"work_dir": str(work_dir)},
                    ),
                    payload=payload,
                )
            except OSError as exc:
                # PR #349 review F1: a host-side launch / I/O failure (exec
                # not found, permissions, a read error) is a non-delivering
                # exit like any other. Letting it escape to the subscriber
                # loop stranded the story: claimed forever (it is already in
                # _processed_tasks), no gate, no finding.
                resumable = True  # `infra`: a host verdict (see above)
                await self._escalate(
                    task_id,
                    Escalation(
                        reason="infra",
                        summary=f"plugin could not run: {exc}",
                        brief={"work_dir": str(work_dir)},
                    ),
                    payload=payload,
                )
            except Exception as exc:  # CancelledError is BaseException: propagates
                # Same seam, unanticipated shape — "every other non-delivering
                # exit" must hold for exits nobody predicted, too.
                logger.exception(
                    "RouteRunner %s: unexpected error running the plugin for %s",
                    self.route.name,
                    task_id,
                )
                await self._escalate(
                    task_id,
                    Escalation(
                        reason="unknown",
                        summary=f"unexpected error running the plugin: {exc!r}",
                        brief={"work_dir": str(work_dir)},
                    ),
                    payload=payload,
                )
            else:
                resumable = leaves_a_resumable_run(result)
                succeeded = await self._apply_result(task_id, result, payload)
        finally:
            renew_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await renew_task
            # Cleanup runs on every exit path — success, plugin failure,
            # contract violation, timeout, even cancellation. The flag
            # decides whether failed dirs are retained for inspection; a
            # resumable run is kept regardless (correctness/f-002).
            self._cleanup_work_dir(work_dir, success=succeeded, resumable=resumable)

    async def _renew_loop(self, task_id: str) -> None:
        while True:
            await asyncio.sleep(self.renew_interval_seconds)
            try:
                await self.lithos.task_renew(
                    task_id=task_id, aspect=self.route.name, agent=self.agent_id
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "RouteRunner %s: renew failed for %s",
                    self.route.name,
                    task_id,
                )
                # Don't crash; next renewal attempt may succeed. If the
                # claim has fully expired, the next operation will fail
                # cleanly and the [BlockerFailed] finding will surface.

    async def _apply_result(
        self,
        task_id: str,
        result: Mapping[str, Any],
        payload: Mapping[str, Any],
    ) -> bool:
        """Apply the plugin's result. Returns ``True`` iff the task succeeded."""
        status = result.get("status")
        if status == "succeeded":
            if self.route.completes_task:
                unblocked = await self.lithos.task_complete(
                    task_id=task_id, agent=self.agent_id
                )
                logger.info("RouteRunner %s: completed %s", self.route.name, task_id)
                # US6: dispatch whatever this completion just released without
                # waiting for Lithos to tell us about it.
                await self._re_dispatch_unblocked(unblocked or ())
            else:
                # PR-producing route: success means a reviewed branch + PR
                # exist, awaiting human merge — NOT that the task is done.
                # Model the wait as a first-class `pr` gate (Epic H) and release
                # the claim; the gate blocks re-dispatch until the PR merges.
                await self._gate_and_release(task_id, payload, result)
            return True
        if status == "failed":
            # b91177d2: the run ended without delivering — raise the
            # needs-human gate from the plugin's own `escalation` block (or a
            # composed one), never re-run on our own.
            await self._escalate(
                task_id,
                escalation_from_result(result, detail="plugin reported failure"),
                payload=payload,
                run_id=result.get("run_id"),
            )
            return False
        if status == "interrupted":
            # T10: a `resume` block makes the interruption retryable — the
            # plugin says WHEN a re-run is expected to succeed. Planned before
            # the release so the durable record (U1) exists by the time the
            # task is externally visible as unclaimed — the marker's rule.
            resume = result.get("resume")
            record = (
                self._plan_resume(task_id, resume, payload)
                if isinstance(resume, Mapping)
                else None
            )
            superseded = (
                failed_attempt_for_route(payload.get("metadata") or {}, self.route.name)
                is not None
            )
            if isinstance(record, ResumeRecord):
                # One write: the record, and the superseded failed marker gone.
                if (
                    await write_resume_record(
                        self.lithos,
                        task_id=task_id,
                        route=self.route.name,
                        agent=self.agent_id,
                        record=record,
                        clear_failed_marker=superseded,
                    )
                    and superseded
                ):
                    self._attempt_stamps.clear(self.route.name, task_id)
            else:
                # An interrupted attempt supersedes an earlier failure — a
                # stale failed marker must not veto interrupted's designed
                # recovery, the restart bootstrap (see clear_superseded_failure).
                await clear_superseded_failure(
                    self.lithos,
                    task_id=task_id,
                    route=self.route.name,
                    agent=self.agent_id,
                    payload=payload,
                    stamps=self._attempt_stamps,
                )
            # Release the claim either way: a shutdown signal frees the task
            # for a future run; a usage-limit checkpoint must not hold the
            # claim across the (potentially hours-long) wait. No
            # [BlockerFailed] finding — neither case is an error.
            with contextlib.suppress(Exception):
                await self.lithos.task_release(
                    task_id=task_id,
                    aspect=self.route.name,
                    agent=self.agent_id,
                )
            logger.info(
                "RouteRunner %s: released %s (plugin interrupted)",
                self.route.name,
                task_id,
            )
            if isinstance(record, ResumeRecord):
                self._arm_resume(task_id, record)
            elif record is _EXHAUSTED and isinstance(resume, Mapping):
                await self._escalate_resume_exhausted(task_id, resume, payload)
            return False
        await self._escalate(
            task_id,
            Escalation(
                reason="contract_violation",
                summary=f"plugin returned unknown status {status!r}",
            ),
            payload=payload,
            run_id=result.get("run_id"),
        )
        return False

    # ── usage-limit re-dispatch (T10) ─────────────────────────────────

    def _plan_resume(
        self,
        task_id: str,
        resume: Mapping[str, Any],
        payload: Mapping[str, Any],
    ) -> ResumeRecord | object | None:
        """What this interruption's ``resume`` block asks for.

        A :class:`ResumeRecord` to persist and arm; :data:`_EXHAUSTED` when the
        attempts already recorded on the task meet ``MAX_RESUMES_PER_TASK``;
        ``None`` when the block is unusable (no re-dispatch, as before). The
        attempt count is read from the record the dispatch payload carried
        (U1) — the task is the count, so a restart cannot reset it — and the
        new record is that plus one.
        """
        resume_after = parse_resume_after(resume.get("resume_after"))
        if resume_after is None:
            logger.warning(
                "RouteRunner %s: %s has unparseable resume_after %r; not "
                "re-dispatching",
                self.route.name,
                task_id,
                resume.get("resume_after"),
            )
            return None
        prior = resume_record_for_route(payload.get("metadata") or {}, self.route.name)
        used = prior.attempts if prior is not None else 0
        if used >= MAX_RESUMES_PER_TASK:
            return _EXHAUSTED
        run_id = resume.get("run_id")
        return ResumeRecord(
            resume_after=resume_after,
            attempts=used + 1,
            run_id=run_id
            if isinstance(run_id, str) and is_plain_run_id(run_id)
            else None,
        )

    async def _escalate_resume_exhausted(
        self,
        task_id: str,
        resume: Mapping[str, Any],
        payload: Mapping[str, Any],
    ) -> None:
        logger.warning(
            "RouteRunner %s: %s exhausted its resume budget (%d); escalating",
            self.route.name,
            task_id,
            MAX_RESUMES_PER_TASK,
        )
        # b91177d2: a run that keeps hitting its provider limit is a human's
        # problem now, not a retry's. The claim was already released on the
        # interrupted path, so this raises the gate + the [NeedsHuman] finding
        # without releasing again. No resume record is written, so the gate
        # tick starts a fresh budget by construction.
        await self._escalate(
            task_id,
            Escalation(
                reason="resume_exhausted",
                summary=(
                    "usage-limited run resume budget exhausted "
                    f"({MAX_RESUMES_PER_TASK} re-dispatches)"
                ),
            ),
            payload=payload,
            run_id=resume.get("run_id"),
            release=False,
        )

    def _honour_pending_resume(self, task_id: str, record: ResumeRecord) -> bool:
        """Make the armed timer agree with the task's resume record.

        The record is the authority; the sleeper follows it. Four cases:

        * a sleeper is armed for this exact time → nothing to do (the record's
          own write, or a replayed event), handled;
        * a sleeper is armed for another time → the operator edited
          ``resume_after`` (PR #439 review): re-arm for the new remaining
          delay — zero when the new time has passed, so the re-dispatch runs
          at once through the sleeper path, which drops the dedup entry —
          handled;
        * no sleeper and the time is still ahead → arm one (a restart, or the
          first event after one), handled;
        * no sleeper and the time has passed → not handled: the caller
          dispatches now (the claim consumes the record; its attempts ride the
          payload). This is also how the sleeper's own fire proceeds — it
          disarms itself before re-reading the task.

        Nothing is written and no attempt counted on any branch. "Passed" has
        a second's slack, so a fire a few milliseconds ahead of the wall-clock
        instant (two clocks are compared) is not re-armed for the remainder.
        """
        delay = record.remaining_seconds()
        due = delay <= RESUME_DUE_SLACK_SECONDS
        armed = self._resume_tasks.get(task_id)
        armed_for = self._resume_due.get(task_id)
        if armed is not None and not armed.done():
            if armed_for == record.resume_after:
                return True
            logger.info(
                "RouteRunner %s: %s's recorded resume time moved %s -> %s; "
                "re-arming (re-dispatch in %.0fs, attempt %d/%d used)",
                self.route.name,
                task_id,
                armed_for.isoformat(timespec="seconds") if armed_for else "?",
                record.resume_after.isoformat(timespec="seconds"),
                0.0 if due else delay,
                record.attempts,
                MAX_RESUMES_PER_TASK,
            )
            self._arm_sleeper(task_id, 0.0 if due else delay, record.resume_after)
            return True
        if due:
            logger.info(
                "RouteRunner %s: %s's recorded resume time %s has passed "
                "(attempt %d/%d used); dispatching now",
                self.route.name,
                task_id,
                record.resume_after.isoformat(timespec="seconds"),
                record.attempts,
                MAX_RESUMES_PER_TASK,
            )
            return False
        logger.info(
            "RouteRunner %s: honouring %s's recorded resume — re-dispatch in "
            "%.0fs (at %s, attempt %d/%d used); not dispatching before it",
            self.route.name,
            task_id,
            delay,
            record.resume_after.isoformat(timespec="seconds"),
            record.attempts,
            MAX_RESUMES_PER_TASK,
        )
        self._arm_sleeper(task_id, delay, record.resume_after)
        return True

    def _arm_resume(self, task_id: str, record: ResumeRecord) -> None:
        delay = record.remaining_seconds()
        logger.info(
            "RouteRunner %s: scheduling re-dispatch of %s in %.0fs "
            "(resume %d/%d, at %s)",
            self.route.name,
            task_id,
            delay,
            record.attempts,
            MAX_RESUMES_PER_TASK,
            record.resume_after.isoformat(timespec="seconds"),
        )
        self._arm_sleeper(task_id, delay, record.resume_after)

    def _arm_sleeper(self, task_id: str, delay: float, due: datetime) -> None:
        existing = self._resume_tasks.get(task_id)
        if existing is not None and not existing.done():
            existing.cancel()
        sleeper = asyncio.create_task(
            self._resume_dispatch(task_id, delay),
            name=f"resume-{task_id}",
        )
        self._resume_tasks[task_id] = sleeper
        self._resume_due[task_id] = due

        def _cleanup(done: asyncio.Task[None]) -> None:
            # Only remove the entry this task still owns: a cancelled old
            # sleeper's callback can fire AFTER a replacement was stored,
            # and must not evict the replacement.
            if self._resume_tasks.get(task_id) is done:
                del self._resume_tasks[task_id]
                self._resume_due.pop(task_id, None)

        sleeper.add_done_callback(_cleanup)

    def _disarm_current_sleeper(self, task_id: str) -> None:
        """The sleeper that is firing is no longer *armed*: forget it before the
        re-read, so the record check sees "no timer" and lets a due record
        dispatch (and a moved-later one arm a fresh timer)."""
        if self._resume_tasks.get(task_id) is asyncio.current_task():
            del self._resume_tasks[task_id]
            self._resume_due.pop(task_id, None)

    async def _resume_dispatch(self, task_id: str, delay: float) -> None:
        """Sleep until ``resume_after``, then re-claim + re-run the task.

        The synthetic event is built from a FRESH ``task_get`` snapshot, not
        the payload captured when the run was interrupted: an operator may
        edit the task (body, acceptance criteria, reviewer override, deps)
        during the pause window, and the plugin re-reads all of that from the
        task.json the runner writes from this payload. Re-using the stale
        payload would silently develop against the old instructions.
        """
        try:
            await asyncio.sleep(delay)
            task = await self.lithos.task_get(task_id=task_id)
            if task is None or task.status != "open":
                logger.info(
                    "RouteRunner %s: %s no longer open at resume time; dropping",
                    self.route.name,
                    task_id,
                )
                return
            # Re-check the route's tag filter. The re-dispatch calls _handle
            # directly, bypassing the bus matcher that gates normal events, so
            # an operator who retagged the task during the pause window (e.g.
            # pulled the trigger tag to cancel it) would otherwise still get a
            # resumed run against a task that no longer matches this route.
            if not set(self.route.match.tags).issubset(set(task.tags)):
                logger.info(
                    "RouteRunner %s: %s no longer carries the route's trigger "
                    "tags at resume time; dropping",
                    self.route.name,
                    task_id,
                )
                return
            # Drop the dedup entry so _handle's claim path re-runs, and the
            # armed-timer entry so the record check re-reads the FRESH time
            # (an operator may have moved it; PR #439 review).
            self._disarm_current_sleeper(task_id)
            self._processed_tasks.discard(task_id)
            await self._handle(
                Event(
                    type="loom.route.resume",
                    timestamp=datetime.now(UTC),
                    payload=task_payload(task),
                    origin=RESUME_ORIGIN,
                )
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "RouteRunner %s: re-dispatch of %s failed",
                self.route.name,
                task_id,
            )

    async def _release_with_finding(
        self,
        task_id: str,
        detail: str,
        *,
        payload: Mapping[str, Any],
        run_id: Any = None,
    ) -> None:
        # The marker-only failure path (marker + stamp + [BlockerFailed] +
        # release) — kept for the one non-delivering exit that is NOT a story
        # decision: a misconfigured route (a host problem; a gate per task
        # would be spam and completing it fails again). Everything else goes
        # through _escalate.
        await release_with_failure(
            self.lithos,
            task_id=task_id,
            route=self.route.name,
            agent=self.agent_id,
            detail=detail,
            payload=payload,
            run_id=run_id,
            stamps=self._attempt_stamps,
        )

    async def _escalate(
        self,
        task_id: str,
        escalation: Escalation,
        *,
        payload: Mapping[str, Any],
        run_id: Any = None,
        release: bool = True,
    ) -> None:
        # The non-delivering exit (b91177d2): needs-human gate + marker naming
        # it + [NeedsHuman] finding + push notification + release, with the
        # marker-only path as the fallback when no gate can be raised. Lives
        # with its guards in dispatch_guards.escalate_with_failure.
        await escalate_with_failure(
            self.lithos,
            task_id=task_id,
            route=self.route.name,
            agent=self.agent_id,
            payload=payload,
            escalation=escalation,
            run_id=run_id,
            stamps=self._attempt_stamps,
            notifier=self.notifier,
            release=release,
        )

    async def _gate_and_release(
        self,
        task_id: str,
        payload: Mapping[str, Any],
        result: Mapping[str, Any],
    ) -> None:
        # The delivering exit (Epic H): pr gate + provenance + release, with
        # ONE [Friction] for any degraded step. Lives in delivery_gate.
        await gate_and_release(
            self.lithos,
            task_id=task_id,
            route=self.route.name,
            agent=self.agent_id,
            payload=payload,
            result=result,
            stamps=self._attempt_stamps,
        )

    def _cleanup_work_dir(
        self, work_dir: Path, *, success: bool, resumable: bool = False
    ) -> None:
        if success:
            with contextlib.suppress(OSError):
                shutil.rmtree(work_dir)
            return
        if self.retain_failed_workdirs:
            return
        # 5dbeb0c8 slice C / correctness/f-002: `retain_failed_workdirs = false`
        # is disk hygiene for runs nobody will look at again — but a HOST death
        # leaves a branch the next dispatch continues, and its checkpoint lives
        # right here. Reaping it turns the operator's gate tick back into a
        # from-scratch run and silently loses the rounds the whole slice exists
        # to keep. So a run that is resumable is kept whatever the flag says,
        # and named; everything else is reaped exactly as before.
        if resumable and resumable_checkpoint_under(work_dir) is not None:
            logger.info(
                "RouteRunner %s: keeping %s despite retain_failed_workdirs=false "
                "— it holds a resumable checkpoint the next dispatch continues",
                self.route.name,
                work_dir,
            )
            return
        with contextlib.suppress(OSError):
            shutil.rmtree(work_dir)
