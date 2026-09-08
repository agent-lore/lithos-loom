"""Recording one external-remediation run's outcome (PRD S2 slice C + S5b).

The tail of a dispatched ``converge --from-github`` run, lifted out of the
dispatcher so it stays under the module budget: attribute loom's own push on
the budget marker, post the ``[ExternalReview]`` outcome finding on the
story, log the run's end, and — when the CLI says the run did not succeed
and the budget is spent — escalate through :mod:`.remediation_escalation`.
"""

from __future__ import annotations

import dataclasses
from typing import Any

from lithos_loom.errors import LithosClientError
from lithos_loom.gates import PrGateSpec
from lithos_loom.subscriptions import SubscriptionContext
from lithos_loom.subscriptions._findings import write_marker
from lithos_loom.subscriptions.external_reviews import EXTERNAL_REVIEW
from lithos_loom.subscriptions.remediation_budget import (
    REMEDIATION_KEY,
    RemediationBudget,
    RemediationNotifier,
)
from lithos_loom.subscriptions.remediation_escalation import escalate_if_exhausted

__all__ = ["escalate_or_report", "post_finding", "record_result"]


async def post_finding(ctx: SubscriptionContext, story_id: str, summary: str) -> None:
    """Best-effort finding post (the story may have completed mid-run)."""
    try:
        await ctx.lithos.finding_post(task_id=story_id, summary=summary)
    except LithosClientError as exc:
        ctx.logger.warning(
            "[Friction] external-remediation: posting outcome for story %s failed (%s)",
            story_id,
            exc,
        )


async def escalate_or_report(
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
) -> None:
    """PRD S5b: exhaustion → human gate; a gate that could not be raised is
    said so on the story instead of vanishing."""
    problem = await escalate_if_exhausted(
        ctx,
        gate_id=gate_id,
        story_id=story_id,
        spec=spec,
        budget=budget,
        budget_limit=budget_limit,
        notifier=notifier,
        last_status=last_status,
        detail=detail,
        cost=cost,
    )
    if problem is not None:
        await post_finding(
            ctx,
            story_id,
            f"[Friction] external-remediation: budget spent on {spec.pr_url} "
            f"but no needs-human gate could be raised ({problem}); the PR "
            "is not converged and loom will not dispatch again until a "
            "human pushes to the branch",
        )


async def record_result(
    ctx: SubscriptionContext,
    *,
    gate_id: str,
    story_id: str,
    spec: PrGateSpec,
    budget: RemediationBudget,
    budget_limit: int,
    notifier: RemediationNotifier | None,
    data: dict[str, Any],
) -> None:
    """Record a run that produced a JSON result: marker, finding, log, and
    the exhaustion escalation when the CLI reports it did not succeed."""
    status = data.get("status", "unknown")
    pushed_sha = data.get("pushed_sha") or ""
    if data.get("pushed") and pushed_sha:
        # Loom's own push: recorded so the next sweep's head observation
        # attributes it (no human-push reset) and own-sha material skips.
        updated = dataclasses.replace(
            budget,
            last_loom_pushed_sha=pushed_sha,
            last_seen_head_sha=pushed_sha,
        )
        await write_marker(
            ctx,
            task_id=gate_id,
            marker={REMEDIATION_KEY: updated.as_marker()},
            subsystem="external-remediation",
        )

    lines = [
        f"{EXTERNAL_REVIEW} remediation outcome for delivered PR "
        f"{spec.pr_url}: {status} "
        f"(round {budget.rounds_used}/{budget_limit})"
    ]
    if data.get("message"):
        lines.append(f"- {data['message']}")
    if pushed_sha:
        lines.append(f"- pushed {pushed_sha[:12]} to the PR branch")
    for o in data.get("external_outcomes") or []:
        if not isinstance(o, dict):
            continue
        where = f" ({o['thread_url']})" if o.get("thread_url") else ""
        detail = f" — {o['detail']}" if o.get("detail") else ""
        lines.append(
            f"- {o.get('finding_id', '?')} by {o.get('author', '?')}: "
            f"{o.get('disposition', '?')}{detail}{where}"
        )
    cost = data.get("total_cost_usd")
    if isinstance(cost, int | float):
        lines.append(f"- spend ${cost:.2f}")
    ctx.logger.info(
        "external-remediation: converge for %s finished: %s, round %d/%d%s%s",
        spec.pr_url,
        status,
        budget.rounds_used,
        budget_limit,
        f", pushed {pushed_sha[:12]}" if pushed_sha else ", nothing pushed",
        f", ${cost:.2f}" if isinstance(cost, int | float) else "",
    )
    await post_finding(ctx, story_id, "\n".join(lines))
    # The CLI's own verdict decides (PR #361 review F1): `triage_rejected`
    # is a success — nothing left for the operator. An older record without
    # the flag is judged by status alone.
    succeeded = data.get("succeeded")
    if not isinstance(succeeded, bool):
        succeeded = status == "converged"
    if not succeeded:
        await escalate_or_report(
            ctx,
            gate_id=gate_id,
            story_id=story_id,
            spec=spec,
            budget=budget,
            budget_limit=budget_limit,
            notifier=notifier,
            last_status=str(status),
            detail=str(data.get("message") or status),
            cost=cost if isinstance(cost, int | float) else None,
        )
