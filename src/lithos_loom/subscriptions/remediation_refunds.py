"""What gives an S5b remediation round back (PRD S5b, #377, #407).

The three refunds a dispatched ``develop converge`` can end in without a
verdict on the change — the host failed under it (``infra_failed``), the
CLI refused a mis-mapped checkout (``repo_mismatch``), or the daemon that
dispatched it went away (a shutdown, #407 slice 2a; an ungraceful death
reconciled at the next boot, slice 2b). Each returns the reserved round,
re-parks the review trigger the reservation consumed, stamps the round's
outcome so no later refund can read it as unrecorded, and tells the story
where any work the run left behind sits. Split from
:mod:`.remediation_outcome` (the record-and-escalate half) on the module
line budget; the two share :func:`.remediation_outcome.write_marker_strict`
and :data:`.remediation_outcome.REFUND_RETRY_DELAYS`.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

from lithos_loom.gates import PrGateSpec
from lithos_loom.subscriptions import SubscriptionContext
from lithos_loom.subscriptions.remediation_budget import (
    PENDING_KEY,
    REMEDIATION_KEY,
    RemediationBudget,
    RemediationNotifier,
    read_budget,
)
from lithos_loom.subscriptions.remediation_outcome import (
    REFUND_RETRY_DELAYS,
    REPO_MISMATCH_KEY,
    escalate_or_report,
    post_finding,
    refusal_key,
    write_marker_strict,
)

__all__ = ["refund_infra_failed", "refund_lost_run", "refund_repo_mismatch"]


def _kept_work(data: dict[str, Any]) -> str:
    """#412: the host may have died AFTER the coder committed a fix (lens #89
    r1). Converge commits on the RUN's own worktree branch — never on the PR's
    branch, which an `infra_failed` run never pushes — so the breadcrumb names
    that worktree, and nothing when no coder ran (the intake-infra shape)."""
    worktree = str(data.get("worktree") or "")
    branch = str(data.get("branch") or "")
    if not worktree and not branch:
        return ""
    where = worktree or branch
    tag = f" (branch {branch})" if worktree and branch else ""
    return (
        f" The run's worktree {where}{tag} holds any fix the coder committed "
        "before the failure — recover it from there rather than paying for it again."
    )


async def refund_lost_run(
    ctx: SubscriptionContext,
    *,
    gate_id: str,
    story_id: str | None,
    spec: PrGateSpec,
    budget_limit: int,
    work_dir: Path | None = None,
    cause: str = "a loom shutdown",
    next_step: str = "it re-dispatches after the next boot",
) -> RemediationBudget | None:
    """#407 slice 2a: the daemon is stopping and just killed this PR's run.
    The daemon knows the moment it strands a round, so the refund belongs
    here, not in boot-time archaeology: the reservation is given back, the
    review trigger re-parked (the reservation consumed it), and the story
    told once — it re-dispatches after the next boot.

    Guarded by #410's outcome field on a RE-READ of the gate: a cancel that
    landed after the run's outcome write leaves ``last_status`` set, and a
    refund on top of a recorded outcome would be a second refund. Strict
    write, never raising; the refunded budget when a refund landed, else
    ``None``. *story_id* ``None`` (an orphan gate) refunds without the
    finding — the refund needs no story, only the breadcrumb does.
    """
    try:
        latest = await ctx.lithos.task_get(task_id=gate_id)
    except Exception as exc:  # noqa: BLE001 — shutdown must finish either way
        ctx.logger.warning(
            "[Friction] external-remediation: could not re-read gate %s to refund "
            "the run killed by shutdown (%s); the round stays spent — the next "
            "boot's sweep reconciles it",
            gate_id,
            exc,
        )
        return None
    if latest is None:
        return None
    budget = read_budget(latest, spec.pr_url)
    if budget.rounds_used <= 0 or budget.last_status:
        return None  # never reserved here, or the run recorded its outcome
    refund = dataclasses.replace(
        budget,
        rounds_used=budget.rounds_used - 1,
        last_status="",
        last_settled=False,
        in_flight_boot_id="",
        in_flight_pid=0,
        in_flight_pid_start=0,
        in_flight_host_boot="",
    )
    failure = await write_marker_strict(
        ctx,
        gate_id=gate_id,
        marker={
            REMEDIATION_KEY: refund.as_marker(),
            PENDING_KEY: {"pr_url": spec.pr_url},
        },
    )
    if failure is not None:
        ctx.logger.warning(
            "[Friction] external-remediation: refund of the run killed by shutdown "
            "did not land on gate %s (%s); round %d/%d stays spent",
            gate_id,
            failure,
            budget.rounds_used,
            budget_limit,
        )
        return None
    ctx.logger.warning(
        "external-remediation: round %d/%d for %s was lost to %s; "
        "refunded, trigger re-parked",
        budget.rounds_used,
        budget_limit,
        spec.pr_url,
        cause,
    )
    where = (
        f" Any commits the killed run made sit in its worktree under "
        f"{work_dir / 'converge'}; a fix it had already pushed reads as a human "
        "push once the head is observed and re-arms the budget."
        if work_dir is not None
        else ""
    )
    summary = (
        f"[Friction] external-remediation: remediation round {budget.rounds_used}/"
        f"{budget_limit} for {spec.pr_url} was lost to {cause} before it "
        f"recorded an outcome; the round is refunded ({refund.rounds_used}/"
        f"{budget_limit}) and the review trigger re-parked — {next_step}.{where}"
    )
    if story_id is None:
        ctx.logger.warning("%s (orphan gate %s: no story to tell)", summary, gate_id)
    else:
        await post_finding(ctx, story_id, summary)
    return refund


async def refund_infra_failed(
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
    """The run ended ``infra_failed`` (#377): the host, not the change, is
    broken — refund the reserved round, re-park the review trigger, and say
    what to fix. No exhaustion escalation (the change was never judged) and
    no settle key: the dispatcher holds the PR in memory for the rest of this
    boot, and a daemon restart — the operator's fix attempt — retries once.

    The state write is STRICT, as for a repo-mismatch refund: it is what
    keeps the round and the review debt.
    """
    action = str(data.get("host_action") or "fix the host")
    detail = str(data.get("message") or "infrastructure failure")[:300]
    kept = _kept_work(data)
    # The refund IS this round's outcome (#407 slice 2a review): stamped, so a
    # shutdown cancel that lands during the friction post below cannot read
    # the refunded round as "never recorded" and refund it again.
    refund = dataclasses.replace(
        budget,
        rounds_used=max(0, budget.rounds_used - 1),
        last_status="infra_failed",
        last_settled=False,
        in_flight_boot_id="",
        in_flight_pid=0,
        in_flight_pid_start=0,
        in_flight_host_boot="",
    )
    marker = {
        REMEDIATION_KEY: refund.as_marker(),
        PENDING_KEY: {"pr_url": spec.pr_url},
    }
    failure = await write_marker_strict(ctx, gate_id=gate_id, marker=marker)
    if failure is None:
        ctx.logger.warning(
            "[Friction] external-remediation: converge for %s stopped on an "
            "infrastructure failure (%s); round refunded, trigger re-parked, "
            "held until the next daemon boot",
            spec.pr_url,
            detail,
        )
        await post_finding(
            ctx,
            story_id,
            f"[Friction] external-remediation: converge --from-github for "
            f"{spec.pr_url} stopped on an infrastructure failure, not a verdict "
            f"on the change ({detail}). The round is refunded "
            f"({refund.rounds_used}/{budget_limit}) and the review trigger "
            f"re-parked; loom will not retry this PR until the daemon restarts. "
            f"{action} — then restart loom.{kept}",
        )
        return
    ctx.logger.warning(
        "[Friction] external-remediation: infra refund for %s did not land after "
        "%d attempts (%s); round %d/%d remains spent",
        spec.pr_url,
        1 + len(REFUND_RETRY_DELAYS),
        failure,
        budget.rounds_used,
        budget_limit,
    )
    await post_finding(
        ctx,
        story_id,
        f"[Friction] external-remediation: converge --from-github for "
        f"{spec.pr_url} stopped on an infrastructure failure ({detail}), but "
        f"recording the refund did not land ({failure}): round "
        f"{budget.rounds_used}/{budget_limit} remains spent and the review "
        f"trigger is not re-parked. {action} — then restart loom and re-run "
        f"`develop converge --from-github` for the material.{kept}",
    )
    # the round IS spent on this path: a last round decides like any other
    await escalate_or_report(
        ctx,
        gate_id=gate_id,
        story_id=story_id,
        spec=spec,
        budget=budget,
        budget_limit=budget_limit,
        notifier=notifier,
        last_status="infra_failed",
        detail=f"{detail}; the refund did not land ({failure})",
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
    refund = dataclasses.replace(
        budget,
        rounds_used=max(0, budget.rounds_used - 1),
        last_status="repo_mismatch",  # the round's outcome (#407 slice 2a review)
        last_settled=False,
        in_flight_boot_id="",
        in_flight_pid=0,
        in_flight_pid_start=0,
        in_flight_host_boot="",
    )
    marker = {
        REMEDIATION_KEY: refund.as_marker(),
        PENDING_KEY: {"pr_url": spec.pr_url},
        REPO_MISMATCH_KEY: {
            **refusal_key(spec, repo, origin_seen),
            "kind": "repo_mismatch",
            "actual_repo": actual,
        },
    }
    failure = await write_marker_strict(ctx, gate_id=gate_id, marker=marker)
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
