"""PRD S5b: an exhausted external-remediation budget is an escalation.

Until 2026-09-07 the exhausted round's outcome was a finding on the story and
nothing else — the August failure mode (a stop nobody is told about): lens#78's
round 2/2 ended ``not_converged`` at 07:24 and was found twelve hours later
by looking. This module raises the loom ``human`` gate the PRD asks for,
through the same :func:`~.escalation.raise_needs_human` core as the
route-runner's failed exit (gate → record → push sinks → ``[NeedsHuman]``).

The gate is a decision, not a re-dispatch — the story stays behind its
``pr`` gate either way — so its actions are remediation's own, and it is
raised **once per budget**: the gate id is recorded on the budget marker,
and a human push (which resets the budget) re-arms it.
"""

from __future__ import annotations

import dataclasses
from typing import Any

from lithos_loom.errors import LithosClientError
from lithos_loom.gates import (
    ESCALATION_SUMMARY_MAX_CHARS,
    STORY_HUMAN_GATE_ID_KEY,
    PrGateSpec,
)
from lithos_loom.subscriptions import SubscriptionContext
from lithos_loom.subscriptions._findings import write_marker
from lithos_loom.subscriptions.escalation import Escalation, raise_needs_human
from lithos_loom.subscriptions.remediation_budget import (
    REMEDIATION_KEY,
    RemediationBudget,
    RemediationNotifier,
)

__all__ = ["REMEDIATION_ACTIONS", "escalate_if_exhausted"]

REMEDIATION_ACTIONS = (
    "the story stays behind its pr gate; push the fix branch by hand if the "
    "residual is acceptable, re-run `develop converge <pr> --from-github` with "
    "a higher --max-rounds, or address the finding directly — a human push to "
    "the PR re-arms loom's budget; complete this gate once decided"
)
"""What the operator can do about an exhausted remediation — none of it is a
re-dispatch, so the runner's two actions would mislead here."""


async def escalate_if_exhausted(
    ctx: SubscriptionContext,
    *,
    gate_id: str,
    story_id: str,
    spec: PrGateSpec,
    budget: RemediationBudget,
    budget_limit: int,
    notifier: RemediationNotifier | None,
    last_status: str,
    detail: str,
    cost: float | None = None,
) -> str | None:
    """Raise the needs-human gate when *budget* is spent and the PR is still
    not converged. Returns ``None`` when nothing was needed or the gate
    landed, else the problem that stopped the gate (for the caller's
    ``[Friction]``). Never raises.
    """
    if budget.rounds_used < budget_limit or budget.needs_human_gate_id:
        return None
    brief: dict[str, Any] = {
        "pr_url": spec.pr_url,
        "rounds_used": budget.rounds_used,
        "budget": budget_limit,
        "last_status": last_status,
    }
    if cost is not None:
        brief["cost_usd"] = cost
    escalation = Escalation(
        reason="remediation_exhausted",
        summary=(
            f"external-review remediation budget spent on {spec.pr_url} "
            f"({budget.rounds_used}/{budget_limit}) — last run "
            f"{last_status}: {detail}"
        )[:ESCALATION_SUMMARY_MAX_CHARS],
        brief=brief,
    )

    async def _record(human_gate_id: str) -> bool:
        updated = dataclasses.replace(budget, needs_human_gate_id=human_gate_id)
        ok = await write_marker(
            ctx,
            task_id=gate_id,
            marker={REMEDIATION_KEY: updated.as_marker()},
            subsystem="external-remediation",
        )
        try:
            await ctx.lithos.task_update(
                task_id=story_id,
                agent=ctx.agent_id,
                metadata={STORY_HUMAN_GATE_ID_KEY: human_gate_id},
            )
        except LithosClientError as exc:
            ctx.logger.warning(
                "[Friction] external-remediation: recording gate %s on story "
                "%s failed (%s)",
                human_gate_id,
                story_id,
                exc,
            )
            return False
        return ok

    human_gate_id, problem = await raise_needs_human(
        ctx.lithos,
        task_id=story_id,
        route="external-remediation",
        agent=ctx.agent_id,
        escalation=escalation,
        notifier=notifier,
        actions=REMEDIATION_ACTIONS,
        record=_record,
        record_problem=(
            "could not record the gate on the budget marker / story — "
            "a later exhausted run may raise a second gate"
        ),
    )
    return None if human_gate_id is not None else (problem or "unknown")
