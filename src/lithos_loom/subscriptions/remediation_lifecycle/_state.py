"""Budget transition vocabulary, private to the lifecycle implementation.

Clearing a reservation is part of recording a verdict, including a refund or
an escalation. Keeping that invariant here prevents a later full-record write
from resurrecting the process stamp or losing push attribution.
"""

from dataclasses import replace

from lithos_loom.runner.orphans import ProcessIdentity
from lithos_loom.subscriptions.remediation_budget import RemediationBudget


def settled(
    budget: RemediationBudget, status: str, succeeded: bool = False
) -> RemediationBudget:
    return replace(
        budget,
        last_status=status,
        last_settled=succeeded,
        in_flight_boot_id="",
        in_flight_pid=0,
        in_flight_pid_start=0,
        in_flight_host_boot="",
    )


def refunded(budget: RemediationBudget, status: str) -> RemediationBudget:
    return replace(settled(budget, status), rounds_used=max(0, budget.rounds_used - 1))


def reserved(
    budget: RemediationBudget, boot_id: str, identity: ProcessIdentity
) -> RemediationBudget:
    return replace(
        budget,
        rounds_used=budget.rounds_used + 1,
        last_status="",
        last_settled=False,
        in_flight_boot_id=boot_id,
        in_flight_pid=identity.pid,
        in_flight_pid_start=identity.start_ticks,
        in_flight_host_boot=identity.host_boot,
    )


def decision_raised(
    budget: RemediationBudget, gate_id: str, reason: str
) -> RemediationBudget:
    return replace(
        settled(budget, budget.last_status, budget.last_settled),
        needs_human_gate_id=gate_id,
        needs_human_reason=reason,
    )


def decision_released(budget: RemediationBudget) -> RemediationBudget:
    return replace(
        budget,
        rounds_used=0,
        needs_human_gate_id="",
        needs_human_reason="",
        no_change_refunded=False,
    )


def pushed(
    budget: RemediationBudget, sha: str, *, observed: bool = False
) -> RemediationBudget:
    return replace(
        budget,
        last_loom_pushed_sha=sha,
        last_seen_head_sha=sha if observed else budget.last_seen_head_sha,
    )
