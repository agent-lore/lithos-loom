"""What the conflict resolver writes per outcome (PRD S5): strict writes,
the friction and success breadcrumbs, the needs-human escalation.

Every write that BOUNDS the dispatcher — the pre-spawn reservation, the crash
record, the post-push outcome + budget — goes through :func:`strict_write`
(retried with backoff; the outcome is the caller's to honour). The record
always lands BEFORE its breadcrumb: a breadcrumb that cannot post must never
erase a bound.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from lithos_loom.errors import LithosClientError
from lithos_loom.gates import (
    ESCALATION_SUMMARY_MAX_CHARS,
    STORY_HUMAN_GATE_ID_KEY,
    PrGateSpec,
)
from lithos_loom.subscriptions import SubscriptionContext
from lithos_loom.subscriptions._findings import write_marker
from lithos_loom.subscriptions.conflict_resolve_record import (
    CONFLICT_ACTIONS,
    CONFLICT_RESOLVE_KEY,
    PUSHED_BREADCRUMB_KEY,
    STRICT_WRITE_DELAYS,
    ConflictResolveRecord,
)
from lithos_loom.subscriptions.escalation import Escalation, raise_needs_human
from lithos_loom.subscriptions.remediation_budget import RemediationNotifier

__all__ = [
    "clear_breadcrumb",
    "escalate",
    "paths_of",
    "post_finding",
    "post_friction",
    "story_escalation",
    "strict_write",
    "write_once",
    "write_record",
]


async def write_once(
    gate_id: str, marker: Mapping[str, Any], ctx: SubscriptionContext
) -> bool:
    """One attempt at a gate write (the debt flush: one per sweep)."""
    return await write_marker(
        ctx, task_id=gate_id, marker=marker, subsystem="conflict-resolve"
    )


async def strict_write(
    gate_id: str, marker: Mapping[str, Any], ctx: SubscriptionContext
) -> bool:
    """A write the dispatcher's bounds depend on: retried with backoff;
    the outcome is the caller's to honour."""
    for delay in (0.0, *STRICT_WRITE_DELAYS):
        if delay:
            await asyncio.sleep(delay)
        if await write_once(gate_id, marker, ctx):
            return True
    return False


async def write_record(
    gate_id: str, record: ConflictResolveRecord, ctx: SubscriptionContext
) -> bool:
    return await strict_write(gate_id, {CONFLICT_RESOLVE_KEY: record.as_marker()}, ctx)


async def clear_breadcrumb(story_id: str, ctx: SubscriptionContext) -> None:
    await write_marker(
        ctx,
        task_id=story_id,
        marker={PUSHED_BREADCRUMB_KEY: None},
        subsystem="conflict-resolve",
    )


async def post_finding(story_id: str, summary: str, ctx: SubscriptionContext) -> None:
    try:
        await ctx.lithos.finding_post(task_id=story_id, summary=summary)
    except LithosClientError as exc:
        ctx.logger.warning(
            "[Friction] conflict-resolve: posting a finding for story %s failed (%s)",
            story_id,
            exc,
        )


async def post_friction(
    gate_id: str,
    story_id: str,
    record: ConflictResolveRecord,
    detail: str,
    ctx: SubscriptionContext,
) -> None:
    """The record FIRST (strict — it is the once-per-pair bound), then the
    breadcrumb; a breadcrumb that cannot post never erases the record."""
    await write_record(gate_id, record, ctx)
    await post_finding(
        story_id,
        (
            f"[Friction] conflict-resolve: resolving {record.pr_url}'s conflict "
            f"with its base @ {record.base_sha[:12]} (head {record.head_sha[:12]}) "
            f"{detail} (attempt {record.attempts}; a daemon restart retries a "
            f"crash once, a head or base move re-keys it)"
        ),
        ctx,
    )


def paths_of(data: Mapping[str, Any]) -> list[str]:
    conflict = data.get("conflict")
    raw = conflict.get("paths") if isinstance(conflict, dict) else None
    return [p for p in (raw if isinstance(raw, list) else []) if isinstance(p, str)]


async def story_escalation(story_id: str, ctx: SubscriptionContext) -> str:
    """Does an OPEN loom human gate already wait on the story? ``escalated``
    when one does, ``clear`` when none does, ``unknown`` when Lithos could
    not say (PR #369 review round 2: "cannot read" is kept distinct from a
    confirmed gate — the state must not claim a decision that was never
    raised, and a paid run must not start on an unknown either).

    The record on the gate is the once-per-key guard; this is the belt for a
    record that failed to land after the gate was raised (the story still
    names it), so a paid run is never repeated and a second gate never
    raised — and the belt is what the reconciliation state reads, so it
    speaks before any record-based early return."""
    try:
        story = await ctx.lithos.task_get(task_id=story_id)
        if story is None:
            return "clear"
        gate_id = story.metadata.get(STORY_HUMAN_GATE_ID_KEY)
        if not isinstance(gate_id, str) or not gate_id:
            return "clear"
        human = await ctx.lithos.task_get(task_id=gate_id)
    except (LithosClientError, OSError):
        return "unknown"
    return "escalated" if human is not None and human.status == "open" else "clear"


async def escalate(
    gate_id: str,
    story_id: str,
    spec: PrGateSpec,
    record: ConflictResolveRecord,
    data: Mapping[str, Any],
    notifier: RemediationNotifier | None,
    ctx: SubscriptionContext,
) -> None:
    """The residue is a human's: raise the loom ``human`` gate on the story,
    once per sha pair (the record carries the gate id)."""
    paths = paths_of(data)
    brief: dict[str, Any] = {
        "pr_url": spec.pr_url,
        "head_sha": record.head_sha,
        "base_sha": record.base_sha,
        "paths": paths,
        "status": record.status,
        "rounds": data.get("rounds"),
        "fixer_commits": data.get("fixer_commits"),
        "cost_usd": data.get("total_cost_usd"),
        "message": record.message,
    }
    escalation = Escalation(
        reason="conflict_unresolved",
        summary=(
            f"loom could not resolve {spec.pr_url}'s conflict with its base "
            f"@ {record.base_sha[:12]} in {', '.join(paths) or 'unnamed paths'} "
            f"— run {record.status}: {record.message}"
        )[:ESCALATION_SUMMARY_MAX_CHARS],
        brief=brief,
    )

    async def _record(human_gate_id: str) -> bool:
        ok = await write_record(
            gate_id, replace(record, needs_human_gate_id=human_gate_id), ctx
        )
        try:
            await ctx.lithos.task_update(
                task_id=story_id,
                agent=ctx.agent_id,
                metadata={STORY_HUMAN_GATE_ID_KEY: human_gate_id},
            )
        except LithosClientError as exc:
            ctx.logger.warning(
                "[Friction] conflict-resolve: recording gate %s on story %s "
                "failed (%s)",
                human_gate_id,
                story_id,
                exc,
            )
            return False
        return ok

    human_gate_id, problem = await raise_needs_human(
        ctx.lithos,
        task_id=story_id,
        route="conflict-resolve",
        agent=ctx.agent_id,
        escalation=escalation,
        notifier=notifier,
        actions=CONFLICT_ACTIONS,
        record=_record,
        record_problem=(
            "could not record the gate on the resolve record / story — a "
            "restart may raise a second gate for the same conflict"
        ),
    )
    if human_gate_id is None:
        # the record still lands, so the same sha pair is never re-run
        await post_friction(
            gate_id,
            story_id,
            record,
            f"ended {record.status} and the needs-human gate could not be raised "
            f"({problem or 'unknown'}); the conflict is a human's to resolve",
            ctx,
        )
