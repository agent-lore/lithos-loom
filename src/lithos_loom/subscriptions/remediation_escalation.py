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
and a human push (which resets the budget) re-arms it — as does the
operator completing the gate (:func:`decision_pending`, PR #389 review).
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

__all__ = [
    "DISPUTE_ACTIONS",
    "REMEDIATION_ACTIONS",
    "decision_pending",
    "escalate_disputed",
    "escalate_if_exhausted",
]

REMEDIATION_ACTIONS = (
    "the story stays behind its pr gate; push the fix branch by hand if the "
    "residual is acceptable, re-run `develop converge <pr> --from-github` with "
    "a higher --max-rounds, or address the finding directly — a human push to "
    "the PR re-arms loom's budget, and so does completing this gate once "
    "decided (loom then remediates the next review on a fresh budget)"
)
"""What the operator can do about an exhausted remediation — none of it is a
re-dispatch, so the runner's two actions would mislead here."""

DISPUTE_ACTIONS = (
    "the external review and the story's acceptance criteria disagree, and "
    "the loop undid its own fix rather than choose — decide: amend the "
    "story's acceptance criteria and re-run `develop converge <pr> "
    "--from-github` (or re-apply the fix by hand), or answer the reviewer on "
    "the thread and leave the code as it is; completing this gate once decided "
    "re-arms loom's budget (as a human push does) and remediation resumes on "
    "the next review"
)
"""What the operator can do about a reverted external fix (#387)."""


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
    return await _escalate(
        ctx,
        gate_id=gate_id,
        story_id=story_id,
        budget=budget,
        notifier=notifier,
        escalation=escalation,
        actions=REMEDIATION_ACTIONS,
    )


async def escalate_disputed(
    ctx: SubscriptionContext,
    *,
    gate_id: str,
    story_id: str,
    spec: PrGateSpec,
    budget: RemediationBudget,
    notifier: RemediationNotifier | None,
    outcome: dict[str, Any],
    pushed_sha: str,
) -> str | None:
    """#387: the loop made an external fix and then undid it — the reviewer
    and the story's acceptance criteria disagree (lens #84: "Fixed in" was
    posted over a net no-op). A decision, not a re-run: raise the gate NOW,
    whatever the budget says, once per budget; the marker then holds
    dispatch until a human push. Same return contract as
    :func:`escalate_if_exhausted`.
    """
    if budget.needs_human_gate_id:
        return None
    fid = str(outcome.get("finding_id") or "?")
    reason = str(outcome.get("detail") or "(no reason given)")
    brief: dict[str, Any] = {
        "pr_url": spec.pr_url,
        "finding_id": fid,
        "author": outcome.get("author") or "",
        "thread_url": outcome.get("thread_url") or "",
        "coder_reason": reason,
        "pushed_sha": pushed_sha,
        "rounds_used": budget.rounds_used,
    }
    escalation = Escalation(
        reason="disputed",
        summary=(
            f"external finding {fid} on {spec.pr_url} was fixed, then reverted: "
            f"the review and the story's acceptance criteria disagree — {reason}"
        )[:ESCALATION_SUMMARY_MAX_CHARS],
        brief=brief,
    )
    return await _escalate(
        ctx,
        gate_id=gate_id,
        story_id=story_id,
        budget=budget,
        notifier=notifier,
        escalation=escalation,
        actions=DISPUTE_ACTIONS,
    )


async def _escalate(
    ctx: SubscriptionContext,
    *,
    gate_id: str,
    story_id: str,
    budget: RemediationBudget,
    notifier: RemediationNotifier | None,
    escalation: Escalation,
    actions: str,
) -> str | None:
    async def _record(human_gate_id: str) -> bool:
        updated = dataclasses.replace(
            budget,
            needs_human_gate_id=human_gate_id,
            needs_human_reason=escalation.reason,
        )
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
        actions=actions,
        record=_record,
        record_problem=(
            "could not record the gate on the budget marker / story — "
            "a later exhausted run may raise a second gate"
        ),
    )
    return None if human_gate_id is not None else (problem or "unknown")


async def decision_pending(
    ctx: SubscriptionContext,
    *,
    gate_id: str,
    spec: PrGateSpec,
    budget: RemediationBudget,
) -> tuple[RemediationBudget, bool]:
    """A loom remediation gate on this budget — a reverted fix (#387, raised
    with rounds to spare) or an exhausted budget — holds every dispatch,
    ``consider`` and a parked trigger alike, while it is OPEN. The gate IS
    the budget's stop, and the operator's decision need not involve a push
    ("answer the reviewer, leave the code"), so the gate going terminal is
    the release AND the operator's consent to continue: the marker on the
    ``pr`` gate *gate_id* forgets it and the budget re-arms (rounds reset,
    loom's push attribution kept — the own-sha skip must still hold), as a
    human push would (PR #389 review: the motivating run was the last
    budgeted round). An unreadable gate holds (fail closed); a gate that no
    longer exists can never be completed, so it releases.
    """
    decision_id = budget.needs_human_gate_id
    if not decision_id:
        return budget, False
    try:
        decision = await ctx.lithos.task_get(task_id=decision_id)
    except LithosClientError as exc:
        ctx.logger.warning(
            "external-remediation: decision gate %s for %s unreadable (%s); holding",
            decision_id,
            spec.pr_url,
            exc,
        )
        return budget, True
    if decision is not None and getattr(decision, "status", "open") == "open":
        return budget, True
    released = dataclasses.replace(
        budget, rounds_used=0, needs_human_gate_id="", needs_human_reason=""
    )
    ok = await write_marker(
        ctx,
        task_id=gate_id,
        marker={REMEDIATION_KEY: released.as_marker()},
        subsystem="external-remediation",
    )
    if not ok:
        return budget, True  # the release must be durable before a spend
    ctx.logger.info(
        "external-remediation: decision gate %s for %s is resolved; dispatch resumes",
        decision_id,
        spec.pr_url,
    )
    return released, False
