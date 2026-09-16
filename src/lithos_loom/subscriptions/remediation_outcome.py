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

from lithos_loom.gates import PrGateSpec
from lithos_loom.subscriptions import SubscriptionContext
from lithos_loom.subscriptions._findings import post_finding_then_mark
from lithos_loom.subscriptions.external_reviews import EXTERNAL_REVIEW
from lithos_loom.subscriptions.remediation_budget import (
    REMEDIATION_KEY,
    RemediationBudget,
    RemediationNotifier,
    read_budget,
)
from lithos_loom.subscriptions.remediation_escalation import (
    escalate_disputed,
    escalate_if_exhausted,
)

__all__ = [
    "REFUND_RETRY_DELAYS",
    "REPORTED_NOT_REMEDIATED",
    "REPO_MISMATCH_KEY",
    "escalate_or_report",
    "post_checkout_unresolved_refusal",
    "post_finding",
    "post_repo_mismatch_refusal",
    "record_result",
    "record_unsettled",
    "refusal_key",
    "settled_refusal",
]

# Backoff between attempts to land the refund's state write (seconds).
REFUND_RETRY_DELAYS: tuple[float, ...] = (0.5, 2.0, 5.0)

# Run statuses that are reported, not remediated (#380): every injected
# finding was refuted by triage (`triage_rejected` — the lens #84 route, PR
# #396 review) or judged not a defect with the loop approving the unchanged
# head (`already_clean`). Nothing was pushed and nothing had to be, so the
# reserved round comes back — once per budget (:func:`_refund_no_change`).
REPORTED_NOT_REMEDIATED: frozenset[str] = frozenset(
    {"already_clean", "triage_rejected"}
)


async def post_finding(ctx: SubscriptionContext, story_id: str, summary: str) -> None:
    """Best-effort finding post (the story may have completed mid-run).

    Genuinely best-effort (PR #379 review): a raw transport error — which
    propagates past the client's own recovery — is swallowed here too. Every
    caller posts the breadcrumb AFTER its durable write landed, and an
    exception escaping from here would reach ``ExternalRemediation._run``'s
    crash handler, which re-reads the ORIGINAL reserved budget and can raise
    a false ``remediation_exhausted`` gate over a round that was refunded.
    """
    try:
        await ctx.lithos.finding_post(task_id=story_id, summary=summary)
    except Exception as exc:  # noqa: BLE001 — see the docstring
        ctx.logger.warning(
            "[Friction] external-remediation: posting outcome for story %s failed "
            "(%s: %s); the breadcrumb is lost, the recorded state stands",
            story_id,
            type(exc).__name__,
            exc,
        )


async def write_marker_strict(
    ctx: SubscriptionContext, *, gate_id: str, marker: dict[str, Any]
) -> Exception | None:
    """A gate-marker write that keeps a budget round or a parked trigger:
    retried with backoff, and NEVER raising — a raw transport error
    propagates past the client's own recovery (``lithos_client._invoke``)
    and must land here, not in ``ExternalRemediation._run``'s crash handler,
    which re-reads the ORIGINAL reserved budget and can raise a false
    ``remediation_exhausted`` gate over a round that was refunded. Returns
    the last failure, or ``None`` when the write landed."""
    failure: Exception | None = None
    for delay in (0.0, *REFUND_RETRY_DELAYS):
        if delay:
            await asyncio.sleep(delay)
        try:
            await ctx.lithos.task_update(task_id=gate_id, metadata=marker)
            return None
        except Exception as exc:  # noqa: BLE001 — see above
            failure = exc
    return failure


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


async def record_unsettled(
    ctx: SubscriptionContext,
    *,
    gate_id: str,
    spec: PrGateSpec,
    budget: RemediationBudget,
) -> RemediationBudget:
    """#408: a run that died without a verdict (a crash, or an exit with no
    result) left the round spent AND the PR unsettled — say so on the budget
    so a budget this spends reads as a stop, never as "nothing to do" from
    the round before. Composed on a RE-READ of the gate (the merge-gate
    precedent): the crash may have come after :func:`record_result` landed
    the run's push, and writing the dispatched copy back would revert that
    attribution — the next sweep would read loom's own push as a human's.
    Strict and never raising, like every budget write on a failure path; a
    write that does not land leaves the prior record."""
    try:
        latest = await ctx.lithos.task_get(task_id=gate_id)
    except Exception as exc:  # noqa: BLE001 — a raw transport error must not reach the crash handler twice
        ctx.logger.warning(
            "external-remediation: could not re-read gate %s before recording "
            "the failed outcome (%s); composing on the dispatched copy",
            gate_id,
            exc,
        )
        latest = None
    if latest is not None:
        budget = read_budget(latest, spec.pr_url)
    budget = dataclasses.replace(
        budget,
        last_status="failed",
        last_settled=False,
        in_flight_boot_id="",
        in_flight_pid=0,
        in_flight_pid_start=0,
        in_flight_host_boot="",
    )
    failure = await write_marker_strict(
        ctx, gate_id=gate_id, marker={REMEDIATION_KEY: budget.as_marker()}
    )
    if failure is not None:
        ctx.logger.warning(
            "[Friction] external-remediation: recording the failed outcome on "
            "gate %s did not land (%s); the prior budget record stands",
            gate_id,
            failure,
        )
    return budget


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
    # #387: a fix the loop made and then undid is a DECISION (the review vs
    # the acceptance criteria) — raised below whatever the budget says.
    reverted = next(
        (
            o
            for o in data.get("external_outcomes") or []
            if isinstance(o, dict) and o.get("disposition") == "reverted"
        ),
        None,
    )
    # The CLI's own verdict decides (PR #361 review F1): `triage_rejected`
    # is a success — nothing left for the operator; the reverted shape is
    # `converged` but NOT succeeded. An older record without the flag is
    # judged by status alone.
    succeeded = data.get("succeeded")
    if not isinstance(succeeded, bool):
        succeeded = status == "converged" and reverted is None
    # #408: the outcome is recorded on the budget whatever it was — the
    # reconciliation state reads `last_settled` to tell a spent budget whose
    # last round settled the PR from one that left it unconverged. Every
    # later write on this budget (the refund and the escalations below)
    # starts from THIS copy, or it would clobber what was just recorded
    # (opus round 1 on #361: a later write clobbered the push attribution
    # and the next sweep read loom's own push as a human's).
    budget = dataclasses.replace(
        budget,
        last_status="reverted" if reverted is not None else str(status),
        last_settled=succeeded and reverted is None,
        in_flight_boot_id="",  # decided (#407 slice 2b)
        in_flight_pid=0,
        in_flight_pid_start=0,
        in_flight_host_boot="",
    )
    if data.get("pushed") and pushed_sha:
        # Loom's own push: recorded so the next sweep's head observation
        # attributes it (no human-push reset) and own-sha material skips.
        budget = dataclasses.replace(
            budget,
            last_loom_pushed_sha=pushed_sha,
            last_seen_head_sha=pushed_sha,
        )
    # Strict, never raising: an escaping transport error would reach the
    # crash handler, which re-reads the ORIGINAL reserved budget (the #380
    # false-gate route); a write that still does not land leaves the prior
    # record — the refund below and the escalations retry their own writes.
    failure = await write_marker_strict(
        ctx, gate_id=gate_id, marker={REMEDIATION_KEY: budget.as_marker()}
    )
    if failure is not None:
        ctx.logger.warning(
            "[Friction] external-remediation: recording the %s outcome for %s "
            "on gate %s did not land (%s); the prior budget record stands",
            status,
            spec.pr_url,
            gate_id,
            failure,
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
        # #399: how the epilogue read a final ack that disagreed with an
        # earlier round's — shown here, never on the reviewer's thread
        drift = f" [note: {o['note']}]" if o.get("note") else ""
        lines.append(
            f"- {o.get('finding_id', '?')} by {o.get('author', '?')}: "
            f"{o.get('disposition', '?')}{detail}{where}{drift}"
        )
    cost = data.get("total_cost_usd")
    if isinstance(cost, int | float):
        lines.append(f"- spend ${cost:.2f}")
    if status in REPORTED_NOT_REMEDIATED:
        budget, note = await _refund_no_change(ctx, gate_id, budget, budget_limit)
        lines.append(note)
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
    # the #387 decision is raised before the exhaustion rule, which would
    # otherwise wait for the last round
    if reverted is not None:
        problem = await escalate_disputed(
            ctx,
            gate_id=gate_id,
            story_id=story_id,
            spec=spec,
            budget=budget,
            notifier=notifier,
            outcome=reverted,
            pushed_sha=pushed_sha,
        )
        if problem is not None:
            await post_finding(
                ctx,
                story_id,
                f"[Friction] external-remediation: external finding "
                f"{reverted.get('finding_id', '?')} on {spec.pr_url} was fixed "
                f"then reverted, but no needs-human gate could be raised "
                f"({problem}); the decision is outstanding",
            )
        return
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


async def _refund_no_change(
    ctx: SubscriptionContext, gate_id: str, budget: RemediationBudget, limit: int
) -> tuple[RemediationBudget, str]:
    """#380: a reported-not-remediated run (:data:`REPORTED_NOT_REMEDIATED`
    — every injected finding refuted by triage, or not a defect with the
    loop approving the unchanged head; nothing committed) gives its reserved
    round back: ONCE per budget (it is a paid run — triage, and for
    `already_clean` a coder turn + a panel round — so an unbounded refund
    would let five "thanks" comments be five paid runs at 0/2; the own-sha
    skip and the no-JSON refund, the two precedents, spend nothing). A human
    push is a fresh budget, and so is a completed decision gate (PR #396
    review). The write is strict and never raises (opus round 1: an escaping
    write would reach the crash handler's false-exhaustion path); a write
    that still does not land leaves the round spent and says so."""
    if budget.no_change_refunded:
        return budget, (
            "- nothing to change, but this budget's one reported-not-remediated "
            f"refund was already used: the round stays spent "
            f"({budget.rounds_used}/{limit})"
        )
    refund = dataclasses.replace(
        budget, rounds_used=max(0, budget.rounds_used - 1), no_change_refunded=True
    )
    failure = await write_marker_strict(
        ctx, gate_id=gate_id, marker={REMEDIATION_KEY: refund.as_marker()}
    )
    if failure is None:
        return refund, (
            f"- nothing to change: the round is refunded ({refund.rounds_used}/{limit})"
        )
    ctx.logger.warning(
        "[Friction] external-remediation: no-change refund for gate %s did not "
        "land (%s); round %d/%d stays spent",
        gate_id,
        failure,
        budget.rounds_used,
        limit,
    )
    return budget, (
        f"- nothing to change, but recording the refund did not land ({failure}): "
        f"the round stays spent ({budget.rounds_used}/{limit})"
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
