"""Autonomous external-review remediation (PRD S2 slice C + the S5b budget).

When the reconcile sweep's detection half (:mod:`.external_reviews`) posts an
``[ExternalReview]`` batch, this module decides whether loom also *acts*:
dispatch ``lithos-loom develop converge <pr> --from-github`` as a subprocess
(crash-isolated; converge's own CLI does the trust filter, S5a triage,
injection, push and thread replies) and record the outcome back on the story.

The guard rails, all sweep-owned (ADR 0011 decision 3 — single writer):

- **S5b budget** — ``metadata.external_remediation`` on the GATE:
  ``{pr_url, rounds_used, last_loom_pushed_sha, last_seen_head_sha}``,
  url-scoped like every other gate marker (a replacement PR re-evaluates).
  The counter **never resets on a loom-authored push** (head moved to
  ``last_loom_pushed_sha`` — the two-bot ping-pong this exists to bound) and
  **resets on a human push** (head moved to any sha other than loom's
  recorded push; an empty previous sighting is first-time initialization
  only, so a PR whose spending rounds never pushed still resets — PR #346
  review F2). Exhaustion stops *dispatch only* — detection keeps posting,
  with the exhaustion stated inside the finding body
  (:meth:`ExternalRemediation.exhaustion_note`).
- **Single-flight** — one in-flight remediation globally (the serial-runner
  philosophy); while one runs, a batch that would have dispatched parks a
  durable **pending trigger** on the gate (its high-water marks are consumed
  when it posts, so a later quiet sweep must resume it explicitly — PR #346
  review F1), and :meth:`~ExternalRemediation.observe_head` goes inert (a
  head move while loom's own converge may push at any moment cannot be
  attributed).
- **Own-sha skip** — material reviewing loom's own pushed sha is reported,
  never auto-remediated (it is almost always a re-review of the fix in
  flight). A conversation comment (#353) reviews no sha, so it is never
  own-sha: a human verdict after loom's push is exactly what must dispatch.
- **Trust** — only allowlisted bots / write-admin humans' material triggers a
  dispatch (converge re-applies the same line to what it feeds the coder).
- **Per-project dial** — context-doc ``develop_external_review_converge``
  (default **on**, ADR 0011 decision 6).

Failure economics: a run that produced a JSON result keeps its budget round
(``triage_rejected`` included); an exit-0 run with no JSON found nothing
live to ingest and gives the round back; a non-zero exit with no JSON posts
``[Friction]`` and keeps the round. The pre-run reservation is **strict**
(PR #346 review F3): a budget write that did not demonstrably land never
spawns. A decision gate on the budget (#387) holds every dispatch while it
is open — :meth:`RemediationLifecycle.decision_pending`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sys
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from lithos_loom.gates import PrGateSpec
from lithos_loom.github_client import GitHubClient
from lithos_loom.github_review_activity import ExternalReviewActivity
from lithos_loom.github_review_streams import AuthorTrust
from lithos_loom.runner.orphans import (
    ProcessIdentity,
    identity_alive,
    process_identity,
)
from lithos_loom.subscriptions import SubscriptionContext
from lithos_loom.subscriptions._findings import write_marker
from lithos_loom.subscriptions._project_settings import (
    OriginRead,
    origin_read,
    read_project_flag,
    resolve_project_repo,
)
from lithos_loom.subscriptions._subprocess import spawn_command
from lithos_loom.subscriptions.draining import DrainState, wait_idle
from lithos_loom.subscriptions.external_reviews import (
    IngestResult,
    PendingMarkerProvider,
)
from lithos_loom.subscriptions.remediation_budget import (
    PENDING_KEY,
    REMEDIATION_KEY,
    RemediationBudget,
    RemediationSettings,
    read_budget,
)
from lithos_loom.subscriptions.remediation_lifecycle import RemediationLifecycle
from lithos_loom.subscriptions.remediation_refusals import (
    post_checkout_unresolved_refusal,
    post_repo_mismatch_refusal,
    settled_refusal,
)

__all__ = [
    "CONVERGE_SETTING",
    "PENDING_KEY",
    "REMEDIATION_KEY",
    "ExternalRemediation",
    "OriginRead",
    "RemediationBudget",
    "RemediationSettings",
    "origin_read",
    "read_budget",
    "spawn_converge",
]

logger = logging.getLogger(__name__)


# Project-context metadata key: per-project dial for autonomous dispatch.
CONVERGE_SETTING = "develop_external_review_converge"


# Hard wall-clock cap on one converge subprocess, so a hung run can never hold
# the global single-flight slot forever. Generous: a thorough multi-round
# converge is an hours-scale run.
RUN_TIMEOUT_SECONDS = 4 * 3600

Spawn = Callable[[list[str]], Awaitable[tuple[int, str]]]
# Whether a merge-gate run is in flight on a PR url (PRD S3): the two
# dispatchers hold each other per PR, since either may push to its branch.
Hold = Callable[[str], bool]


async def spawn_converge(cmd: list[str]) -> tuple[int, str]:
    """Default spawn: run the converge CLI, return ``(returncode, output)``.

    A run past :data:`RUN_TIMEOUT_SECONDS` is ended and reported as rc -1 —
    the single-flight slot must never be held by a hung container. And a
    **cancellation** (watcher shutdown, PR #346 review F5) ends the child
    too before re-raising: an orphaned converge could keep fixing and
    pushing after loom stopped, and a restarted watcher would violate the
    global single-flight against it. (:func:`_subprocess.spawn_command`,
    shared with the merge-gate dispatcher.)
    """
    return await spawn_command(cmd, timeout=RUN_TIMEOUT_SECONDS, label="converge run")


class ExternalRemediation:
    """Owns the single-flight dispatch of ``develop converge --from-github``.

    One instance per watcher child; ``_task`` is the global in-flight slot.
    All ``external_remediation`` marker writes happen either in the sweep
    while the slot is idle, or inside the run task while it is busy — never
    both at once — so the no-CAS ``task_update`` merge stays race-free.
    """

    def __init__(
        self,
        settings: RemediationSettings,
        *,
        spawn: Spawn | None = None,
        hold: Hold | None = None,
        boot_id: str | None = None,
    ):
        self._settings = settings
        self._spawn: Spawn = spawn if spawn is not None else spawn_converge
        self._hold = hold
        # #407 slice 2b: stamped on every reservation; a stamp from another
        # boot with no outcome is a run this daemon never saw end
        self._boot_id = boot_id or uuid.uuid4().hex
        self._task: asyncio.Task[None] | None = None
        self._in_flight_pr_url = ""
        # #407: what the in-flight run is about — so shutdown can refund the
        # round it kills (gate id, story id, spec, ctx); None while idle
        self._in_flight: tuple[str, str, PrGateSpec, SubscriptionContext] | None = None
        # #377: PR urls whose last run this boot ended `infra_failed` — held
        # from re-dispatch until the daemon restarts (the operator's fix attempt)
        self._infra_held: set[str] = set()
        # #407 slice 2b: PR urls whose lost reservation this sweep refunded and
        # re-parked — the sweep's own gate copy predates that write, so
        # resume_pending re-reads the gate for these before looking for the
        # trigger (else the re-dispatch would wait a whole sweep)
        self._reparked: dict[str, str] = {}  # pr_url -> gate id
        # foreign stamps the guard refused (a recorded outcome beside them, a
        # malformed record, a still-live child): said once per boot each
        self._stale_logged: set[str] = set()
        # re-review of PR #416: a run that outlived its daemon (its dispatcher
        # identity still alive) is a REAL hold — on the single-flight slot and
        # on the per-PR holds the peers read — until that identity dies
        self._foreign_live: dict[str, ProcessIdentity] = {}
        # #407 slice 3 (`lithos-loom drain`): no new dispatch once draining
        self._drain = DrainState()

    @property
    def boot_id(self) -> str:
        return self._boot_id

    # ── drain ──────────────────────────────────────────────────────────

    def begin_drain(self) -> None:
        """Refuse every new dispatch from now on; the run in flight finishes
        and records its outcome as usual (parked triggers wait for the next
        boot, exactly as a busy slot leaves them)."""
        if self._drain.begin():
            logger.info(
                "external-remediation: draining — no new dispatch; %s",
                f"waiting for the run on {self._in_flight_pr_url}"
                if self._in_flight is not None
                else "idle",
            )

    async def drained(self) -> None:
        """Return once no run of ours is in flight or committing."""
        await wait_idle(lambda: self._drain.committing or self._in_flight is not None)

    @property
    def draining(self) -> bool:
        return self._drain.draining

    @property
    def busy(self) -> bool:
        return (self._task is not None and not self._task.done()) or bool(
            self._live_foreign_holds()
        )

    def _live_foreign_holds(self) -> dict[str, ProcessIdentity]:
        """The cached foreign holds, re-probed: a gate that stopped being
        swept (merged, closed) must not pin the global slot through a stale
        entry, so the slot asks the kernel whether what it holds still runs
        (re-review of PR #416)."""
        dead = [
            url
            for url, ident in self._foreign_live.items()
            if identity_alive(ident) is False
        ]
        for url in dead:
            del self._foreign_live[url]
        return self._foreign_live

    def busy_on(self, pr_url: str) -> bool:
        """Whether a run is claimed or in flight on *pr_url* — the merge-gate
        dispatcher's hold (PRD S3): a converge may push to that branch at
        any moment, so a merge commit must wait. Claimed from the moment a
        dispatch commits (before its reservation write, self-review of PR
        #362: a probe finishing during that await must already see it) until
        the run task ends."""
        # ...or, after a restart, while a run a previous daemon left on that
        # PR is still alive (re-review of PR #416)
        return self._in_flight_pr_url == pr_url or pr_url in self._live_foreign_holds()

    def _lifecycle(
        self,
        gate_id: str,
        spec: PrGateSpec,
        budget: RemediationBudget,
        ctx: SubscriptionContext,
        story_id: str | None = None,
    ) -> RemediationLifecycle:
        return RemediationLifecycle(
            ctx,
            gate_id=gate_id,
            spec=spec,
            snapshot=budget,
            settings=self._settings,
            story_id=story_id,
        )

    async def observe_head(
        self,
        gate: Any,
        spec: PrGateSpec,
        pr: Any,
        ctx: SubscriptionContext,
        *,
        story_id: str | None = None,
    ) -> RemediationBudget:
        """Track the PR head and apply the human-push reset. Never raises.

        Inert while a run is in flight — loom's own converge, OR a merge-gate
        run on this PR (the ``hold``, PRD S3): either may push at any moment,
        so a moved head cannot be attributed (each run's completion records
        its own push before its slot frees). If a run crashes after pushing
        but before recording, the next sweep misattributes that one push as
        human and resets — rare, and it errs toward more remediation
        headroom, never a stuck loop.
        """
        budget = read_budget(gate, spec.pr_url)
        # re-review of PR #416: the cached hold follows the DURABLE stamp. The
        # survivor's own outcome write clears the stamp (or a later boot's
        # reservation replaces it); either is proof the run this hold stood
        # for has settled — release it before anything reads busy.
        held = self._foreign_live.get(spec.pr_url)
        if held is not None and (
            budget.in_flight_pid != held.pid
            or budget.in_flight_pid_start != held.start_ticks
            or budget.in_flight_host_boot != held.host_boot
        ):
            del self._foreign_live[spec.pr_url]
        # #407 slice 2b: a reservation stamped by ANOTHER boot with no run in
        # flight here and no recorded outcome is a run the previous daemon
        # lost without a shutdown (SIGKILL, a host crash — slice 2a's refund
        # never ran). Refund it, re-park its trigger, say so; the parked
        # trigger then re-dispatches later in this very sweep.
        if (
            budget.in_flight_boot_id
            and budget.in_flight_boot_id != self._boot_id
            and self._in_flight_pr_url != spec.pr_url
        ):
            recovery = await self._lifecycle(
                gate.id,
                spec,
                budget,
                ctx,
                story_id,
            ).recover_previous_run(
                gate,
                boot_id=self._boot_id,
                probe=identity_alive,
                report=spec.pr_url not in self._stale_logged,
            )
            gate, budget = recovery.gate, recovery.budget
            if recovery.held_identity is not None:
                self._foreign_live[spec.pr_url] = recovery.held_identity
            elif recovery.owner_gone:
                self._foreign_live.pop(spec.pr_url, None)
            if recovery.reported:
                self._stale_logged.add(spec.pr_url)
            if recovery.reparked:
                self._reparked[spec.pr_url] = gate.id
        head = getattr(pr, "head_sha", "") or ""
        held = self._hold is not None and self._hold(spec.pr_url)
        if self.busy or held or not head or head == budget.last_seen_head_sha:
            return budget
        return await self._lifecycle(gate.id, spec, budget, ctx).observe_head(head)

    def exhaustion_note(self, budget: RemediationBudget) -> str | None:
        """The S5b exhaustion sentence for the ``[ExternalReview]`` body.

        ``None`` while rounds remain — and when the budget is 0 (the operator
        disabled autonomous dispatch on purpose; that is not an exhaustion).
        """
        limit = self._settings.budget
        if limit <= 0 or budget.rounds_used < limit:
            return None
        return (
            f"remediation budget exhausted ({budget.rounds_used}/{limit} "
            "round(s) used) — findings will be reported but not auto-fixed "
            "until a human pushes or merges"
        )

    async def consider(
        self,
        gate: Any,
        spec: PrGateSpec,
        story_id: str | None,
        budget: RemediationBudget,
        ingest: IngestResult,
        github: GitHubClient,
        ctx: SubscriptionContext,
    ) -> str:
        """Decide whether the just-posted batch dispatches a converge run.

        Returns a label for the sweep log; ``"dispatched"`` means the budget
        reservation landed and the run task started. Never raises.

        The pending trigger this decision leans on is parked by INGESTION,
        atomically with the batch's high-water marks and only after the
        provider found the batch dispatchable (PR #346 review F1 + re-reviews
        1/3: the marks consume the batch, so its dispatch debt must become
        durable in the same write). consider() then shapes the trigger's
        fate: a busy slot leaves it for :meth:`resume_pending`, a dispatch
        consumes it with the reservation, and dispatch-time refusals that
        retrying cannot fix (opt-out, unmapped project) clear it.
        """
        settings = self._settings
        if settings.budget <= 0:
            return "disabled"
        if self._drain.draining:
            return "draining"  # the trigger stays parked for the next boot
        # the gate before the count (PR #389 review): lifting it re-arms
        lifecycle = self._lifecycle(gate.id, spec, budget, ctx, story_id)
        pending = await lifecycle.decision_pending()
        budget = lifecycle.snapshot
        if pending:
            return "escalated"
        if budget.rounds_used >= settings.budget:
            return "exhausted"  # the note already rode out on the finding
        if story_id is None:
            return "no_story"  # nowhere to record the outcome

        dispatchable = await self._dispatchable(ingest, spec.repo, budget, github, ctx)
        if dispatchable != "yes":
            # This batch was never parked (the provider applies the same
            # check pre-park) — and it must NOT clear anything: an older
            # dispatchable batch's trigger may be waiting out a busy slot,
            # and its marks are already consumed (PR #346 re-review 3).
            return dispatchable

        if self.busy:
            ctx.logger.info(
                "external-remediation: a run is already in flight for another "
                "batch; %s stays parked on its pending trigger (detection "
                "continues; a later sweep resumes it)",
                spec.pr_url,
            )
            return "deferred_busy"

        return await self._dispatch(gate, spec, story_id, budget, ctx)

    def pending_marker_provider(
        self,
        spec: PrGateSpec,
        story_id: str | None,
        budget: RemediationBudget,
        github: GitHubClient,
        ctx: SubscriptionContext,
    ) -> PendingMarkerProvider:
        """The pending-trigger provider ingestion calls with the actionable
        batch just before its atomic marker write (PR #346 re-reviews 1+3).

        Dispatchability — trust AND the own-sha loop guard — is evaluated
        HERE, before any durable state exists. So the trigger is only ever
        parked for a batch that would genuinely dispatch, which closes two
        holes at the root: a later undispatchable batch has nothing to clear
        (an older batch's parked debt can never be erased by it), and no
        crash between a park and a clear can hand :meth:`resume_pending` a
        trigger that bypasses the own-sha guard — an own-sha-only batch is
        simply never parked.
        """

        async def provider(
            activities: list[ExternalReviewActivity],
        ) -> dict[str, Any] | None:
            if self._settings.budget <= 0 or story_id is None:
                return None
            batch = IngestResult(posted=True, actionable=list(activities))
            verdict = await self._dispatchable(batch, spec.repo, budget, github, ctx)
            if verdict != "yes":
                return None
            return {PENDING_KEY: {"pr_url": spec.pr_url}}

        return provider

    async def _clear_pending(self, gate_id: Any, ctx: SubscriptionContext) -> None:
        """Drop a parked trigger for a dispatch-time refusal that retrying
        cannot fix (explicit project opt-out, unmapped project).

        Never called for a merely-undispatchable *batch* — such a batch was
        never parked, and a newer batch must not erase an older batch's debt
        (PR #346 re-review 3). Best-effort: a failed clear costs at most one
        refunded no-op resume.
        """
        await write_marker(
            ctx,
            task_id=gate_id,
            marker={PENDING_KEY: None},
            subsystem="external-remediation",
        )

    async def resume_pending(
        self,
        gate: Any,
        spec: PrGateSpec,
        story_id: str | None,
        budget: RemediationBudget,
        github: GitHubClient,
        ctx: SubscriptionContext,
    ) -> str | None:
        """Fire a parked pending trigger on a sweep with no new batch.

        ``None`` when no trigger is parked for this PR url. No
        dispatchability revalidation: a trigger only exists for a batch the
        provider found trusted and non-own-sha at park time (re-review 3),
        and converge re-applies the trust line to what it re-fetches,
        refunding the round when nothing live remains. The trigger survives
        a busy slot, an exhausted budget and an open decision gate (it fires
        after the reset / the decision) and is consumed atomically with the
        budget reservation on dispatch.
        """
        if self._reparked.get(spec.pr_url) == gate.id:
            # this sweep's own re-park (#407 slice 2b) is newer than the gate
            # copy the sweep was handed
            self._reparked.pop(spec.pr_url, None)
            try:
                latest = await ctx.lithos.task_get(task_id=gate.id)
            except Exception:  # noqa: BLE001 — a failed re-read reads the copy
                latest = None
            if latest is not None:
                gate = latest
                budget = read_budget(gate, spec.pr_url)
        raw = gate.metadata.get(PENDING_KEY)
        if not isinstance(raw, dict) or raw.get("pr_url") != spec.pr_url:
            return None
        if self._settings.budget <= 0:
            return "disabled"
        if self.busy:
            return "deferred_busy"  # trigger stays parked
        lifecycle = self._lifecycle(gate.id, spec, budget, ctx, story_id)
        pending = await lifecycle.decision_pending()
        budget = lifecycle.snapshot
        if pending:
            return "escalated"  # trigger stays parked for after the decision
        if budget.rounds_used >= self._settings.budget:
            return "exhausted"  # trigger stays parked for after a reset
        if story_id is None:
            return "no_story"
        ctx.logger.info(
            "external-remediation: resuming the pending trigger for %s",
            spec.pr_url,
        )
        return await self._dispatch(gate, spec, story_id, budget, ctx)

    async def _dispatch(
        self,
        gate: Any,
        spec: PrGateSpec,
        story_id: str,
        budget: RemediationBudget,
        ctx: SubscriptionContext,
    ) -> str:
        """The shared dispatch tail: project resolve → reserve → spawn."""
        if self._drain.draining:
            return "draining"  # the trigger stays parked for the next boot
        self._drain.enter()  # a dispatch is committing: `drained()` waits
        try:
            return await self._dispatch_inner(gate, spec, story_id, budget, ctx)
        finally:
            self._drain.leave()

    async def _dispatch_inner(
        self,
        gate: Any,
        spec: PrGateSpec,
        story_id: str,
        budget: RemediationBudget,
        ctx: SubscriptionContext,
    ) -> str:
        if spec.pr_url in self._infra_held:
            # #377: decided before any read — nothing about this PR can
            # change until the daemon restarts (the parked trigger waits).
            ctx.logger.info(
                "external-remediation: %s is held after an infrastructure "
                "failure this boot; the parked trigger resumes after a restart",
                spec.pr_url,
            )
            return "held_infra"
        repo_path = await self._project_repo(gate, story_id, ctx)
        if repo_path is None:
            ctx.logger.warning(
                "[Friction] external-remediation: no project repo resolvable "
                "for gate %s (%s); cannot dispatch converge — map the project "
                "under [projects] or record metadata.project",
                gate.id,
                spec.pr_url,
            )
            await self._clear_pending(gate.id, ctx)
            return "no_project"
        slug, repo = repo_path
        enabled = await self._project_converge_enabled(slug, ctx)
        if enabled is None:
            # PR #346 re-review 2: the context doc may hold an explicit
            # opt-out we could not read — an unknown dial must never
            # authorize an autonomous code-pushing run. Fail closed and
            # LEAVE the pending trigger parked so the decision retries.
            ctx.logger.warning(
                "[Friction] external-remediation: cannot read project %r "
                "settings to check %s; failing closed — no dispatch, the "
                "pending trigger (if parked) retries next sweep",
                slug,
                CONVERGE_SETTING,
            )
            return "project_settings_unavailable"
        if not enabled:
            ctx.logger.info(
                "external-remediation: project %r disables %s; reporting only",
                slug,
                CONVERGE_SETTING,
            )
            await self._clear_pending(gate.id, ctx)
            return "project_disabled"
        if self._hold is not None and self._hold(spec.pr_url):
            # PRD S3: a merge-gate run on this PR may push its merge commit
            # at any moment; a converge dispatched beside it would lose its
            # leased push and waste the round. The pending trigger (if
            # parked) stays, so a later sweep resumes the dispatch.
            ctx.logger.info(
                "external-remediation: a merge-gate run is in flight on %s; "
                "deferring dispatch (the pending trigger, if parked, resumes later)",
                spec.pr_url,
            )
            return "deferred_merge_gate"
        # The cheap origin read (PR #362 re-review 2 F2): a checkout that is
        # not the gate's repo must not spend a round or consume the parked
        # trigger (it dispatches once the mapping is fixed); --expect-repo
        # on the CLI remains the authoritative check.
        read = await origin_read(repo)
        origin = read.repo
        seen = (origin or "").lower()
        settled = settled_refusal(gate, spec, repo, seen)
        if settled is not None:
            # a refusal (the sweep's or the CLI's) already stands for exactly
            # what the sweep observes; nothing runs until the mapping or the
            # read moves
            ctx.logger.debug(
                "external-remediation: %s still settled for %s", settled, spec.pr_url
            )
            return settled
        if origin is None:
            # PR #362 re-review 3 F1: "cannot resolve" is a refusal, never
            # permission to reserve a round and spawn a child that dies in gh
            await post_checkout_unresolved_refusal(
                ctx,
                gate=gate,
                story_id=story_id,
                spec=spec,
                repo=repo,
                reason=read.reason,
            )
            return "checkout_unresolved"
        if seen != spec.repo.lower():
            await post_repo_mismatch_refusal(
                ctx, gate=gate, story_id=story_id, spec=spec, repo=repo, origin=origin
            )
            return "repo_mismatch"
        # Claim the PR NOW, ahead of the reservation write: the merge-gate
        # dispatcher's hold reads busy_on, and a settings probe finishing
        # during the await below must not start a run beside this one.
        self._in_flight_pr_url = spec.pr_url

        # Reserve the round BEFORE the run (crash-safe: a lost refund wastes
        # one round; a lost increment would allow an unbounded retry loop) —
        # and STRICTLY (PR #346 review F3): write_marker swallows failures,
        # so the bound only exists if this write demonstrably landed. The
        # same update consumes any pending trigger (per-key merge: None
        # deletes), so trigger and reservation move together. The last
        # round's outcome goes with it (#408): the spent round has none yet,
        # and a run that never records one — a crash, a restart mid-run
        # (#407) — must read as unsettled, never as the round before.
        me = process_identity(os.getpid())
        if me is None:
            self._in_flight_pr_url = ""
            ctx.logger.warning(
                "[Friction] external-remediation: could not capture a stable "
                "dispatcher identity for %s; not reserving or dispatching — will "
                "retry next sweep",
                spec.pr_url,
            )
            return "identity_unavailable"
        lifecycle = self._lifecycle(gate.id, spec, budget, ctx, story_id)
        if not await lifecycle.reserve(boot_id=self._boot_id, identity=me):
            self._in_flight_pr_url = ""
            return "reservation_failed"
        budget = lifecycle.snapshot
        self._reparked.pop(spec.pr_url, None)  # the reservation consumed the trigger
        ctx.logger.info(
            "external-remediation: dispatching converge --from-github for %s "
            "(round %d/%d)",
            spec.pr_url,
            budget.rounds_used,
            self._settings.budget,
        )
        self._in_flight = (gate.id, story_id, spec, ctx)
        self._task = asyncio.create_task(
            self._run(gate.id, story_id, spec, repo, lifecycle, ctx),
            name=f"external-remediation-{spec.pr_number}",
        )
        return "dispatched"

    async def shutdown(self) -> None:
        """Cancel + await the in-flight run (PR #346 review F5).

        The cancellation-safe spawn terminates the converge subprocess, so
        loom's stop never orphans a child that could keep pushing — and a
        restarted watcher's single-flight slot starts truly empty.
        """
        task = self._task
        if task is None or task.done():
            return
        facts = self._in_flight
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        # #407 slice 2a: the daemon just killed that run. A cancelled run task
        # recorded nothing (its `except Exception` never sees the cancel), so
        # the reserved round and the consumed trigger would strand — as lens
        # #88 r2 did for a whole afternoon. Refund here, where the daemon
        # knows; guarded on the gate's outcome field for the cancel that
        # landed after the run recorded.
        if facts is not None and task.cancelled():
            gate_id, story_id, spec, ctx = facts
            await self._lifecycle(
                gate_id,
                spec,
                RemediationBudget(pr_url=spec.pr_url),
                ctx,
                story_id,
            ).lost_run()

    # ── decision helpers ───────────────────────────────────────────────

    async def _dispatchable(
        self,
        ingest: IngestResult,
        repo: str,
        budget: RemediationBudget,
        github: GitHubClient,
        ctx: SubscriptionContext,
    ) -> str:
        """``"yes"`` / ``"no_trusted"`` / ``"own_sha_only"`` for the batch.

        A conversation comment carries no sha, so it is never own-sha: a
        human verdict left after loom's push is exactly what must dispatch.
        """
        trust = self._author_trust(repo, github)
        any_trusted = False
        for a in ingest.actionable:
            if not await trust.is_trusted(a.author):
                continue
            any_trusted = True
            if (
                budget.last_loom_pushed_sha
                and a.head_sha == budget.last_loom_pushed_sha
            ):
                continue  # a re-review of loom's own fix in flight
            return "yes"
        return "own_sha_only" if any_trusted else "no_trusted"

    def _author_trust(self, repo: str, github: GitHubClient) -> AuthorTrust:
        """Allowlisted bots + write/admin humans; an unverifiable author never
        triggers an agent run (fail closed for dispatch — detection already
        reported the material)."""

        async def permission_of(author: str) -> str:
            return await github.get_collaborator_permission(repo, author)

        return AuthorTrust(permission_of, bots=self._settings.trusted_bots)

    async def _project_repo(
        self, gate: Any, story_id: str, ctx: SubscriptionContext
    ) -> tuple[str, Path] | None:
        """``(slug, repo_path)`` — :func:`_project_settings.resolve_project_repo`,
        shared with the merge-gate dispatcher."""
        return await resolve_project_repo(gate, story_id, self._settings.projects, ctx)

    async def _project_converge_enabled(
        self, slug: str, ctx: SubscriptionContext
    ) -> bool | None:
        """The per-project dial, default **on** (ADR 0011 decision 6) — the
        shared tri-state reader (:func:`_project_settings.read_project_flag`):
        ``None`` for an unreadable doc, which the caller fails closed on."""
        return await read_project_flag(
            slug, CONVERGE_SETTING, ctx, subsystem="external-remediation"
        )

    # ── the run itself ─────────────────────────────────────────────────

    def _command(
        self, spec: PrGateSpec, repo: Path, json_path: Path, story_id: str
    ) -> list[str]:
        cmd = [
            sys.executable,
            "-m",
            "lithos_loom",
            "develop",
            "converge",
            str(spec.pr_number),
            "--from-github",
            # The story: converge resolves the project's + task's develop_*
            # settings (rounds, profile, panel, check-set, image) the way the
            # daemon path does — lens#78 ran at the CLI default of 5 rounds
            # while the project said 8.
            "--story",
            story_id,
            "--repo",
            str(repo),
            # The checkout is pinned to the gate's repo (PR #362 review F2):
            # a PR number resolves against the checkout's origin, so a stale
            # [projects.<slug>].repo would otherwise act on owner/other#N.
            "--expect-repo",
            spec.repo,
            "--json",
            str(json_path),
        ]
        if self._settings.config_path is not None:
            cmd += ["--config", str(self._settings.config_path)]
        return cmd

    async def _run(
        self,
        gate_id: str,
        story_id: str,
        spec: PrGateSpec,
        repo: Path,
        lifecycle: RemediationLifecycle,
        ctx: SubscriptionContext,
    ) -> None:
        """Run one converge subprocess and record its outcome. Never raises.

        A crash anywhere in the run (spawning, the result file, recording)
        still spent the reserved round, so it is reported on the story and —
        if that was the last round — escalated like any other failed run
        (PR #361 review F2: a swallowed exception was the silent exhaustion
        this module exists to close, one layer up).
        """
        try:
            await self._run_inner(gate_id, story_id, spec, repo, lifecycle, ctx)
        except Exception as exc:  # noqa: BLE001 — the slot must always free cleanly
            ctx.logger.exception("external-remediation: run for %s raised", spec.pr_url)
            await lifecycle.crashed(exc)
        finally:
            self._in_flight_pr_url = ""
            self._in_flight = None

    async def _run_inner(
        self,
        gate_id: str,
        story_id: str,
        spec: PrGateSpec,
        repo: Path,
        lifecycle: RemediationLifecycle,
        ctx: SubscriptionContext,
    ) -> None:
        json_path = (
            self._settings.work_dir / "github-watcher" / f"remediation-{gate_id}.json"
        )
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.unlink(missing_ok=True)

        rc, output = await self._spawn(self._command(spec, repo, json_path, story_id))

        data: dict[str, Any] | None = None
        try:
            import json as _json

            data = _json.loads(json_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = None

        # Install the scheduling hold before settlement can await a write.
        # The lifecycle selects the refund and escalation policy.
        if data is not None and data.get("status") == "infra_failed":
            self._infra_held.add(spec.pr_url)
        await lifecycle.finished(data=data, returncode=rc, output=output, repo=repo)
