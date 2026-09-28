"""The remediation budget lifecycle: events in, durable policy outcomes out.

The watcher serializes operations on each PR (including push attribution from
its other dispatchers). This module is not a second writer or a budget cache.
A lifecycle carries one sweep/run's read-only snapshot; failure paths re-read
where recovery requires it. Internal helpers own persistence, refunds and the
ordered needs-human epilogue; callers never assemble a replacement marker.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from lithos_loom.errors import LithosClientError
from lithos_loom.gates import PrGateSpec
from lithos_loom.runner.orphans import ProcessIdentity
from lithos_loom.subscriptions import SubscriptionContext
from lithos_loom.subscriptions._findings import write_marker
from lithos_loom.subscriptions._project_settings import origin_read
from lithos_loom.subscriptions._subprocess import log_text, message_tail
from lithos_loom.subscriptions.remediation_budget import (
    PENDING_KEY,
    REMEDIATION_KEY,
    RemediationBudget,
    RemediationSettings,
)

from ._escalation import decision_pending
from ._outcome import escalate_or_report, post_finding, record_result, record_unsettled
from ._push import (
    PendingPush,
    record_conflict_push,
    record_merge_push,
    recover_conflict_push,
)
from ._recovery import ReservationRecovery, recover
from ._refunds import refund_infra_failed, refund_lost_run, refund_repo_mismatch
from ._state import refunded, reserved

__all__ = [
    "RemediationLifecycle",
    "PendingPush",
    "record_conflict_push",
    "record_merge_push",
    "recover_conflict_push",
]


class RemediationLifecycle:
    """Own one PR budget's transitions and their ordered side effects.

    ``snapshot`` is immutable information for dispatch/read-model queries. The
    initial snapshot comes from the sweep that holds this PR, not a caller's
    proposed edit. Keep the lifecycle used to reserve a run through settlement;
    a new sweep constructs another from its current gate read.
    """

    def __init__(
        self,
        ctx: SubscriptionContext,
        *,
        gate_id: str,
        spec: PrGateSpec,
        snapshot: RemediationBudget,
        settings: RemediationSettings,
        story_id: str | None = None,
    ) -> None:
        self._ctx = ctx
        self._gate_id = gate_id
        self._spec = spec
        self._snapshot = snapshot
        self._settings = settings
        self._story_id = story_id

    @property
    def snapshot(self) -> RemediationBudget:
        return self._snapshot

    @property
    def _story(self) -> str:
        if self._story_id is None:
            raise ValueError("a remediation run must have a story")
        return self._story_id

    async def observe_head(self, head: str) -> RemediationBudget:
        """Observe a head only after the dispatcher established the PR is idle."""
        budget = self._snapshot
        if not head or head == budget.last_seen_head_sha:
            return budget
        if budget.last_seen_head_sha and head != budget.last_loom_pushed_sha:
            self._ctx.logger.info(
                "external-remediation: head of %s moved to %s (not loom's %s) — "
                "human push, resetting budget (was %d round(s) used)",
                self._spec.pr_url,
                head[:12],
                budget.last_loom_pushed_sha[:12] or "(never pushed)",
                budget.rounds_used,
            )
            budget = RemediationBudget(
                pr_url=self._spec.pr_url, last_seen_head_sha=head
            )
        else:
            budget = replace(budget, last_seen_head_sha=head)
        await write_marker(
            self._ctx,
            task_id=self._gate_id,
            marker={REMEDIATION_KEY: budget.as_marker()},
            subsystem="external-remediation",
        )
        self._snapshot = budget
        return budget

    async def decision_pending(self) -> bool:
        self._snapshot, pending = await decision_pending(
            self._ctx,
            gate_id=self._gate_id,
            spec=self._spec,
            budget=self._snapshot,
        )
        return pending

    async def reserve(self, *, boot_id: str, identity: ProcessIdentity) -> bool:
        """Reserve before spawn, atomically consuming the parked trigger.

        The caller holds the PR throughout this await. A failed reservation
        never authorizes a spawn; cancellation still propagates to shutdown.
        """
        budget = reserved(self._snapshot, boot_id, identity)
        try:
            await self._ctx.lithos.task_update(
                task_id=self._gate_id,
                metadata={REMEDIATION_KEY: budget.as_marker(), PENDING_KEY: None},
            )
        except LithosClientError as exc:
            self._ctx.logger.warning(
                "[Friction] external-remediation: budget reservation for gate "
                "%s failed (%s); not dispatching — will retry next sweep",
                self._gate_id,
                exc,
            )
            return False
        self._snapshot = budget
        return True

    async def recover_previous_run(
        self,
        gate: Any,
        *,
        boot_id: str,
        probe: Callable[[ProcessIdentity], bool | None],
        report: bool,
    ) -> ReservationRecovery:
        recovery = await recover(
            self._ctx,
            gate=gate,
            spec=self._spec,
            budget=self._snapshot,
            settings=self._settings,
            story_id=self._story_id,
            boot_id=boot_id,
            probe=probe,
            report=report,
        )
        self._snapshot = recovery.budget
        return recovery

    async def lost_run(
        self,
        *,
        cause: str = "a loom shutdown",
        next_step: str = "it re-dispatches after the next boot",
    ) -> RemediationBudget | None:
        """Recover a dead run; re-read so a recorded verdict is never refunded."""
        recovered = await refund_lost_run(
            self._ctx,
            gate_id=self._gate_id,
            story_id=self._story_id,
            spec=self._spec,
            budget_limit=self._settings.budget,
            work_dir=self._settings.work_dir,
            cause=cause,
            next_step=next_step,
        )
        if recovered is not None:
            self._snapshot = recovered
        return recovered

    async def finished(
        self,
        *,
        data: dict[str, Any] | None,
        returncode: int,
        output: str,
        repo: Path,
    ) -> None:
        """Settle a subprocess exit under the dispatcher's run/host-failure hold.

        Reporting failures cannot undo a durable refund. Repo refusals, host
        failures and successful no-op runs select their own refund policy here,
        not in the dispatcher. A malformed result may raise; ``crashed`` is
        the run's recovery operation and preserves any already-recorded push.
        """
        if data is not None and data.get("status") == "repo_mismatch":
            await refund_repo_mismatch(
                self._ctx,
                gate_id=self._gate_id,
                story_id=self._story,
                spec=self._spec,
                repo=repo,
                origin_seen=((await origin_read(repo)).repo or "").lower(),
                budget=self._snapshot,
                budget_limit=self._settings.budget,
                notifier=self._settings.notifier,
                data=data,
            )
            return
        if data is not None and data.get("status") == "infra_failed":
            await refund_infra_failed(
                self._ctx,
                gate_id=self._gate_id,
                story_id=self._story,
                spec=self._spec,
                budget=self._snapshot,
                budget_limit=self._settings.budget,
                notifier=self._settings.notifier,
                data=data,
            )
            return
        if data is not None:
            await record_result(
                self._ctx,
                gate_id=self._gate_id,
                story_id=self._story,
                spec=self._spec,
                budget=self._snapshot,
                budget_limit=self._settings.budget,
                notifier=self._settings.notifier,
                data=data,
            )
            return
        if returncode == 0:
            self._ctx.logger.info(
                "external-remediation: converge for %s found nothing live to "
                "ingest; returning the budget round",
                self._spec.pr_url,
            )
            budget = refunded(self._snapshot, "no_result")
            await write_marker(
                self._ctx,
                task_id=self._gate_id,
                marker={REMEDIATION_KEY: budget.as_marker()},
                subsystem="external-remediation",
            )
            return
        budget = await record_unsettled(
            self._ctx,
            gate_id=self._gate_id,
            spec=self._spec,
            budget=self._snapshot,
        )
        tail = log_text(output[-600:]) if output else "(no output)"
        self._ctx.logger.warning(
            "external-remediation: converge for %s finished: failed (exit %d) "
            "without a result, round %d/%d spent; output tail: %s",
            self._spec.pr_url,
            returncode,
            budget.rounds_used,
            self._settings.budget,
            tail,
        )
        await post_finding(
            self._ctx,
            self._story,
            f"[Friction] external-remediation: converge --from-github for "
            f"{self._spec.pr_url} failed (exit {returncode}) without a result; "
            f"the round is "
            f"spent ({budget.rounds_used}/{self._settings.budget}). Last "
            f'output line: "{message_tail(output)}"',
        )
        await self._escalate(budget, f"converge exited {returncode} without a result")
        return

    async def crashed(self, exc: Exception) -> None:
        """Recover an exception, including one after the result write landed."""
        detail = f"{type(exc).__name__}: {exc}"
        budget = await record_unsettled(
            self._ctx,
            gate_id=self._gate_id,
            spec=self._spec,
            budget=self._snapshot,
        )
        await post_finding(
            self._ctx,
            self._story,
            f"[Friction] external-remediation: converge --from-github for "
            f"{self._spec.pr_url} crashed before recording a result ({detail}); "
            f"the round is spent ({budget.rounds_used}/{self._settings.budget})",
        )
        await self._escalate(budget, detail)

    async def _escalate(self, budget: RemediationBudget, detail: str) -> None:
        await escalate_or_report(
            self._ctx,
            gate_id=self._gate_id,
            story_id=self._story,
            spec=self._spec,
            budget=budget,
            budget_limit=self._settings.budget,
            notifier=self._settings.notifier,
            last_status="failed",
            detail=detail,
        )
