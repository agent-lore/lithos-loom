"""Recording one external-remediation run's outcome (PRD S2 slice C + S5b).

The tail of a dispatched ``converge --from-github`` run, lifted out of the
dispatcher so it stays under the module budget: attribute loom's own push on
the budget marker, post the ``[ExternalReview]`` outcome finding on the
story, log the run's end, and — when the CLI says the run did not succeed
and the budget is spent — escalate through :mod:`.remediation_escalation`.
"""

from __future__ import annotations

import asyncio
import dataclasses
from pathlib import Path
from typing import Any

from lithos_loom.errors import LithosClientError
from lithos_loom.gates import PrGateSpec
from lithos_loom.subscriptions import SubscriptionContext
from lithos_loom.subscriptions._findings import post_finding_then_mark, write_marker
from lithos_loom.subscriptions.external_reviews import EXTERNAL_REVIEW
from lithos_loom.subscriptions.remediation_budget import (
    PENDING_KEY,
    REMEDIATION_KEY,
    RemediationBudget,
    RemediationNotifier,
)
from lithos_loom.subscriptions.remediation_escalation import escalate_if_exhausted

__all__ = [
    "REFUND_RETRY_DELAYS",
    "REPO_MISMATCH_KEY",
    "escalate_or_report",
    "post_checkout_unresolved_refusal",
    "post_finding",
    "post_repo_mismatch_refusal",
    "record_result",
    "refund_repo_mismatch",
    "refusal_key",
    "settled_refusal",
]

# Backoff between attempts to land the refund's state write (seconds).
REFUND_RETRY_DELAYS: tuple[float, ...] = (0.5, 2.0, 5.0)


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


# ── a mis-mapped checkout (PR #362 re-review 2 F2) ─────────────────────────

# Gate-metadata key de-duping the mis-mapped-checkout friction: {pr_url,
# repo_path, actual_repo}. The refusal itself spends nothing and re-checks
# every sweep; only the story finding is one-shot per mismatch.
REPO_MISMATCH_KEY = "external_remediation_repo_mismatch"


def refusal_key(spec: PrGateSpec, repo: Path, origin_seen: str) -> dict[str, str]:
    """What the sweep observes about the mapped checkout — the settle key a
    repo-mismatch refusal (the sweep's own, or the CLI's) is de-duped on."""
    return {"pr_url": spec.pr_url, "repo_path": str(repo), "origin_seen": origin_seen}


def settled_refusal(
    gate: Any, spec: PrGateSpec, repo: Path, origin_seen: str
) -> str | None:
    """The kind of refusal (``repo_mismatch`` / ``checkout_unresolved``)
    already recorded for exactly this key — no spawn, no re-post until the
    mapping or the read moves — or ``None``."""
    raw = gate.metadata.get(REPO_MISMATCH_KEY)
    if not isinstance(raw, dict):
        return None
    key = refusal_key(spec, repo, origin_seen)
    if not all(raw.get(k) == v for k, v in key.items()):
        return None
    kind = raw.get("kind")
    return kind if isinstance(kind, str) and kind else "repo_mismatch"


async def post_checkout_unresolved_refusal(
    ctx: SubscriptionContext,
    *,
    gate: Any,
    story_id: str,
    spec: PrGateSpec,
    repo: Path,
    reason: str,
) -> None:
    """The sweep could not resolve the mapped checkout's origin (PR #362
    re-review 3 F1): no spawn, no round spent, the parked trigger kept, one
    ``[Friction]`` naming why; settled on (path, "") until the path changes
    or the read starts to answer."""
    ctx.logger.warning(
        "[Friction] external-remediation: checkout %s cannot be resolved (%s); "
        "not dispatching for %s (the parked trigger, if any, waits)",
        repo,
        reason,
        spec.pr_url,
    )
    await post_finding_then_mark(
        ctx,
        task_id=story_id,
        summary=(
            f"[Friction] external-remediation: the checkout mapped for this "
            f"project ({repo}) cannot be resolved ({reason}: not a git checkout, "
            f"no origin remote, or an origin that is not a GitHub url); no "
            f"converge was dispatched and no budget round spent (PR "
            f"{spec.pr_url}). Provision the checkout with origin {spec.repo}, or "
            f"fix [projects.<slug>].repo and restart loom — the parked review "
            f"trigger resumes then."
        ),
        marker={
            REPO_MISMATCH_KEY: {
                **refusal_key(spec, repo, ""),
                "kind": "checkout_unresolved",
                "actual_repo": f"unresolved:{reason}",
            }
        },
        subsystem="external-remediation",
        retry_hint="will retry next sweep",
        marker_task_id=gate.id,
    )


async def post_repo_mismatch_refusal(
    ctx: SubscriptionContext,
    *,
    gate: Any,
    story_id: str,
    spec: PrGateSpec,
    repo: Path,
    origin: str,
) -> None:
    """The sweep's own origin read refused the checkout: one ``[Friction]``
    on the story per settle key, de-duped by a marker on the gate. Nothing
    else is written — no round spent, the parked trigger kept."""
    current = {
        **refusal_key(spec, repo, origin.lower()),
        "kind": "repo_mismatch",
        "actual_repo": origin,
    }
    ctx.logger.warning(
        "[Friction] external-remediation: checkout %s has origin %s, not the "
        "gate's %s; not dispatching for %s (the parked trigger, if any, waits)",
        repo,
        origin,
        spec.repo,
        spec.pr_url,
    )
    await post_finding_then_mark(
        ctx,
        task_id=story_id,
        summary=(
            f"[Friction] external-remediation: the checkout mapped for this "
            f"project ({repo}) has origin {origin}, not the gate's {spec.repo} "
            f"(PR {spec.pr_url}); no converge was dispatched and no budget "
            f"round spent. Fix [projects.<slug>].repo in the host config and "
            f"restart loom — the parked review trigger resumes then."
        ),
        marker={REPO_MISMATCH_KEY: current},
        subsystem="external-remediation",
        retry_hint="will retry next sweep",
        marker_task_id=gate.id,
    )


async def refund_repo_mismatch(
    ctx: SubscriptionContext,
    *,
    gate_id: str,
    story_id: str,
    spec: PrGateSpec,
    repo: Path,
    origin_seen: str,
    budget: RemediationBudget,
    budget_limit: int,
    notifier: RemediationNotifier | None,
    data: dict[str, Any],
) -> None:
    """The CLI's authoritative ``--expect-repo`` check refused where the
    sweep's origin read passed: refund the reserved round, re-park the
    review trigger (the reservation consumed it), record the settle key so
    the sweep does not spawn again until the mapping or the remote url
    moves, and say so.

    The state write is STRICT (PR #362 re-review 3 F2): it is what keeps the
    round and the review debt, so it is retried with backoff, and when it
    still does not land the breadcrumb says exactly that — the round stays
    spent, the trigger stays consumed — and an exhausted budget escalates
    to a human like any other unconverged last round. Never a success
    claim over state that did not land.
    """
    actual = data.get("actual_repo") or "(unknown)"
    refund = dataclasses.replace(budget, rounds_used=max(0, budget.rounds_used - 1))
    marker = {
        REMEDIATION_KEY: refund.as_marker(),
        PENDING_KEY: {"pr_url": spec.pr_url},
        REPO_MISMATCH_KEY: {
            **refusal_key(spec, repo, origin_seen),
            "kind": "repo_mismatch",
            "actual_repo": actual,
        },
    }
    failure: LithosClientError | None = None
    for delay in (0.0, *REFUND_RETRY_DELAYS):
        if delay:
            await asyncio.sleep(delay)
        try:
            await ctx.lithos.task_update(task_id=gate_id, metadata=marker)
            failure = None
            break
        except LithosClientError as exc:
            failure = exc
    if failure is None:
        await post_finding(
            ctx,
            story_id,
            f"[Friction] external-remediation: converge refused to act on "
            f"{spec.pr_url}: the checkout's origin is {actual}, not the gate's "
            f"{spec.repo}; no agent ran, the round is refunded and the review "
            f"trigger re-parked. Fix [projects.<slug>].repo (or the checkout's "
            f"remote url) and restart loom — nothing runs until one of them "
            f"changes.",
        )
        return
    ctx.logger.warning(
        "[Friction] external-remediation: refund for %s did not land after %d "
        "attempts (%s); round %d/%d remains spent and the review trigger is "
        "not re-parked",
        spec.pr_url,
        1 + len(REFUND_RETRY_DELAYS),
        failure,
        budget.rounds_used,
        budget_limit,
    )
    await post_finding(
        ctx,
        story_id,
        f"[Friction] external-remediation: converge refused to act on "
        f"{spec.pr_url} (the checkout's origin is {actual}, not the gate's "
        f"{spec.repo}), but recording the refund did not land ({failure}): "
        f"round {budget.rounds_used}/{budget_limit} remains spent and the review "
        f"trigger is not re-parked — the material that triggered this run will "
        f"not be re-dispatched until a human pushes to the branch or re-runs "
        f"`develop converge --from-github`. Fix [projects.<slug>].repo and "
        f"restart loom first.",
    )
    await escalate_or_report(
        ctx,
        gate_id=gate_id,
        story_id=story_id,
        spec=spec,
        budget=budget,
        budget_limit=budget_limit,
        notifier=notifier,
        last_status="repo_mismatch",
        detail=(
            f"converge refused the mis-mapped checkout ({actual}) and the refund "
            f"could not be recorded ({failure})"
        ),
    )
