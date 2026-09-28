"""Reconcile a previous boot's reservation without inventing a second refund."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

from lithos_loom.gates import PrGateSpec, waiter_of
from lithos_loom.runner.orphans import ProcessIdentity
from lithos_loom.subscriptions import SubscriptionContext
from lithos_loom.subscriptions.remediation_budget import (
    RemediationBudget,
    RemediationSettings,
    read_budget,
)

from ._refunds import refund_lost_run


@dataclass(frozen=True)
class ReservationRecovery:
    """Read-only recovery facts; the dispatcher owns holds and wake-ups."""

    gate: Any
    budget: RemediationBudget
    held_identity: ProcessIdentity | None = None
    reparked: bool = False
    reported: bool = False
    owner_gone: bool = False


async def recover(
    ctx: SubscriptionContext,
    *,
    gate: Any,
    spec: PrGateSpec,
    budget: RemediationBudget,
    settings: RemediationSettings,
    story_id: str | None,
    boot_id: str,
    probe: Callable[[ProcessIdentity], bool | None],
    report: bool,
) -> ReservationRecovery:
    """A foreign stamp alone proves no death: check the full process identity.

    The returned view suppresses the foreign boot stamp for this sweep, while
    the durable marker is kept for the next sweep when recovery cannot land.
    A successful refund remains authoritative if its subsequent read fails.
    """
    view = replace(budget, in_flight_boot_id="")
    owner_gone = False
    try:
        identity = ProcessIdentity(
            pid=budget.in_flight_pid,
            start_ticks=budget.in_flight_pid_start,
            host_boot=budget.in_flight_host_boot,
        )
        alive = probe(identity)
        if alive is not False:
            if report:
                if alive:
                    ctx.logger.warning(
                        "external-remediation: the run boot %s dispatched on %s "
                        "(pid %d) outlived its daemon and is still running; holding "
                        "the PR — not refunded or re-dispatched beside it until it "
                        "ends",
                        budget.in_flight_boot_id,
                        spec.pr_url,
                        budget.in_flight_pid,
                    )
                else:
                    ctx.logger.warning(
                        "external-remediation: the run boot %s dispatched on %s "
                        "cannot currently be identified; holding the PR — not "
                        "refunded or re-dispatched without proof it died",
                        budget.in_flight_boot_id,
                        spec.pr_url,
                    )
            return ReservationRecovery(gate, view, identity, reported=report)
        owner_gone = True
        if story_id is None:
            meta_story = gate.metadata.get("story_id")
            story_id = (
                meta_story
                if isinstance(meta_story, str) and meta_story
                else await waiter_of(ctx.lithos, gate.id)
            )
        refund = await refund_lost_run(
            ctx,
            gate_id=gate.id,
            story_id=story_id,
            spec=spec,
            budget_limit=settings.budget,
            work_dir=settings.work_dir,
            cause="a daemon restart",
            next_step="it re-dispatches later in this sweep",
        )
        if refund is None:
            if report:
                ctx.logger.warning(
                    "external-remediation: the reservation boot %s left on %s "
                    "was not refundable (a recorded outcome beside it, a malformed "
                    "record, or the write did not land); reading the record as it "
                    "is — the stamp stays for the next sweep",
                    budget.in_flight_boot_id,
                    spec.pr_url,
                )
            return ReservationRecovery(gate, view, reported=report, owner_gone=True)
        try:
            latest = await ctx.lithos.task_get(task_id=gate.id)
        except Exception:  # noqa: BLE001 — the refund already landed
            latest = None
        if latest is None:
            return ReservationRecovery(gate, refund, reparked=True, owner_gone=True)
        current = read_budget(latest, spec.pr_url)
        if current.in_flight_boot_id and current.in_flight_boot_id != boot_id:
            current = replace(current, in_flight_boot_id="")
        return ReservationRecovery(latest, current, reparked=True, owner_gone=True)
    except Exception as exc:  # noqa: BLE001 — recovery must not break the sweep
        ctx.logger.warning(
            "[Friction] external-remediation: reconciling the reservation "
            "boot %s left on %s failed (%s: %s); the next sweep retries",
            budget.in_flight_boot_id,
            spec.pr_url,
            type(exc).__name__,
            exc,
        )
        return ReservationRecovery(gate, view, owner_gone=owner_gone)
