"""Loom push attribution and each dispatcher's existing persistence contract.

Completion and budget attribution are one write. A failed merge-gate write is
best-effort; a failed conflict-resolution write is an opaque pending push the
scheduler holds and retries. It never receives a budget marker to reconstruct.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any

from lithos_loom.errors import LithosClientError
from lithos_loom.gates import PrGateSpec
from lithos_loom.subscriptions import SubscriptionContext
from lithos_loom.subscriptions._findings import write_marker
from lithos_loom.subscriptions.conflict_resolve_outcome import (
    clear_breadcrumb,
    paths_of,
    post_finding,
    strict_write,
    write_once,
)
from lithos_loom.subscriptions.conflict_resolve_record import (
    CONFLICT_RESOLVE_KEY,
    CONFLICT_RESOLVED,
    PUSHED_BREADCRUMB_KEY,
    STRICT_WRITE_DELAYS,
    ConflictResolveRecord,
    read_record,
)
from lithos_loom.subscriptions.merge_gate_record import MERGE_GATE_KEY, MergeGateRecord
from lithos_loom.subscriptions.remediation_budget import (
    REMEDIATION_KEY,
    RemediationBudget,
    read_budget,
)

from ._state import pushed as attribute_push


@dataclass(frozen=True)
class PendingPush:
    """A pushed resolution held until its combined completion write lands.

    The scheduler owns the hold and when to retry; this object owns the write
    and success reporting. Restart recovery uses the story breadcrumb.
    """

    gate_id: str
    story_id: str
    _marker: Mapping[str, Any]
    summary: str

    async def retry(
        self,
        ctx: SubscriptionContext,
        *,
        on_recorded: Callable[[], object],
    ) -> bool:
        if not await write_once(self.gate_id, self._marker, ctx):
            return False
        # Release the scheduling hold once the write lands, even when later
        # reporting raises. The scheduler supplies the action, not its order.
        on_recorded()
        await clear_breadcrumb(self.story_id, ctx)
        await post_finding(self.story_id, self.summary, ctx)
        return True


async def record_merge_push(
    gate_id: str,
    record: MergeGateRecord,
    budget: RemediationBudget,
    ctx: SubscriptionContext,
) -> None:
    """Record a green gate; a pushed merge commit is loom's own push on
    the S5b budget (else observe_head reads it as a human push and
    resets the remediation counter — the invariant S5b exists for).

    The push has already HAPPENED by now, so this must not fail into a
    bare crash record. Prefer the gate's current budget (a fresh read);
    fall back to the dispatch-time snapshot when Lithos will not answer
    — no other writer moved it meanwhile: remediation is held on this
    PR and observe_head is inert while the run is in flight. Record and
    budget land in ONE write. The residual: that one write itself
    failing (write_marker swallows) loses the sha, and the next sweep
    resets the budget — rare, and it errs toward more headroom.
    """
    marker: dict[str, Any] = {MERGE_GATE_KEY: record.as_marker()}
    if record.pushed_sha:
        try:
            fresh = await ctx.lithos.task_get(task_id=gate_id)
        except LithosClientError as exc:
            ctx.logger.warning(
                "[Friction] merge-gate: re-reading gate %s to record loom's "
                "push %s failed (%s); recording from the dispatch-time budget",
                gate_id,
                record.pushed_sha[:12],
                exc,
            )
            fresh = None
        if fresh is not None:
            budget = read_budget(fresh, record.pr_url)
        marker[REMEDIATION_KEY] = attribute_push(budget, record.pushed_sha).as_marker()
    await write_marker(ctx, task_id=gate_id, marker=marker, subsystem="merge-gate")


async def recover_conflict_push(
    gate: Any,
    spec: PrGateSpec,
    story_id: str | None,
    ctx: SubscriptionContext,
) -> PendingPush | None:
    """Re-arm a held debt a previous boot left behind: the story's
    breadcrumb names a push the gate's budget does not yet know as
    loom's own. Called by the sweep BEFORE remediation observes the head,
    so the PR is held from the first sweep after a restart. Never raises."""
    if story_id is None:
        return
    try:
        story = await ctx.lithos.task_get(task_id=story_id)
    except LithosClientError:
        return  # retried next sweep; the hold is what matters and it is cheap
    crumb = None if story is None else story.metadata.get(PUSHED_BREADCRUMB_KEY)
    if not isinstance(crumb, dict) or crumb.get("pr_url") != spec.pr_url:
        return
    pushed = crumb.get("pushed_sha")
    if not isinstance(pushed, str) or not pushed:
        return
    budget = read_budget(gate, spec.pr_url)
    if budget.last_loom_pushed_sha == pushed:
        await clear_breadcrumb(story_id, ctx)  # it landed after all
        return
    record = read_record(gate, spec.pr_url)
    if record is None or record.pushed_sha != pushed:
        record = ConflictResolveRecord(
            spec.pr_url,
            head_sha=str(crumb.get("head_sha") or ""),
            base_sha=str(crumb.get("base_sha") or ""),
            status="converged",
            attempts=1,
            pushed_sha=pushed,
            message="recovered from the story breadcrumb after a restart",
        )
    marker = {
        CONFLICT_RESOLVE_KEY: record.as_marker(),
        REMEDIATION_KEY: attribute_push(budget, pushed).as_marker(),
    }
    summary = (
        f"{CONFLICT_RESOLVED} conflict-resolve: loom's push {pushed[:12]} onto "
        f"{spec.pr_url} (a conflict resolution recorded after a restart) is "
        f"now on the record; the next sweep re-gates at the new head."
    )
    ctx.logger.warning(
        "conflict-resolve: recovered a held debt for %s from the story "
        "breadcrumb (push %s not yet on the budget); holding the PR",
        spec.pr_url,
        pushed[:12],
    )

    # ── the run ────────────────────────────────────────────────────────

    return PendingPush(gate.id, story_id, marker, summary)


async def record_conflict_push(
    gate_id: str,
    story_id: str,
    spec: PrGateSpec,
    record: ConflictResolveRecord,
    data: Mapping[str, Any],
    budget: RemediationBudget,
    ctx: SubscriptionContext,
    *,
    on_pending: Callable[[PendingPush], None],
) -> None:
    """Converged + pushed: the merge commit is loom's own push on the S5b
    budget — record and budget in ONE write, made FIRST and STRICTLY
    (PR #366 review F1): the push has happened, and once the head moved
    the trigger is gone, so nothing would re-derive a lost sha. The
    budget comes from a fresh read of the gate, else the dispatch-time
    snapshot (no other writer moved it: the PR was held). A write that
    still does not land becomes a held debt — the PR stays `busy_on`
    (remediation's head observation inert), the story gets an honest
    [Friction], and the next sweeps retry the write; the success finding
    posts only once it landed."""
    pushed = str(data.get("pushed_sha") or "")
    record = replace(record, pushed_sha=pushed)
    # the breadcrumb first, on the STORY: survives a gate write outage and
    # a restart (recover_debt reads it) — cleared once the record landed
    await write_once(
        story_id,
        {
            PUSHED_BREADCRUMB_KEY: {
                "pr_url": spec.pr_url,
                "pushed_sha": pushed,
                "head_sha": record.head_sha,
                "base_sha": record.base_sha,
            }
        },
        ctx,
    )
    try:
        fresh = await ctx.lithos.task_get(task_id=gate_id)
    except LithosClientError as exc:
        ctx.logger.warning(
            "[Friction] conflict-resolve: re-reading gate %s to record loom's "
            "push %s failed (%s); recording from the dispatch-time budget",
            gate_id,
            pushed[:12],
            exc,
        )
        fresh = None
    if fresh is not None:
        budget = read_budget(fresh, spec.pr_url)
    marker: dict[str, Any] = {
        CONFLICT_RESOLVE_KEY: record.as_marker(),
        REMEDIATION_KEY: attribute_push(budget, pushed).as_marker(),
    }
    paths = paths_of(data)
    summary = (
        f"{CONFLICT_RESOLVED} conflict-resolve: delivered PR {spec.pr_url}'s "
        f"conflict with its base @ {record.base_sha[:12]} in "
        f"{len(paths)} path(s) ({', '.join(paths) or 'unnamed'}) was resolved "
        f"by loom and pushed as {pushed[:12]} onto the PR branch after "
        f"{data.get('rounds')} round(s) (${data.get('total_cost_usd')}); the "
        f"composed tree passed the project's check-set and the panel. The "
        f"next sweep re-gates at the new head."
    )
    if await strict_write(gate_id, marker, ctx):
        await clear_breadcrumb(story_id, ctx)
        await post_finding(story_id, summary, ctx)
        return
    # Establish the scheduling hold before reporting can await or fail.
    on_pending(PendingPush(gate_id, story_id, marker, summary))
    await post_finding(
        story_id,
        (
            f"[Friction] conflict-resolve: loom resolved {spec.pr_url}'s conflict "
            f"and pushed {pushed[:12]}, but recording that push on gate "
            f"{gate_id} did not land after {len(STRICT_WRITE_DELAYS) + 1} "
            f"attempts; the PR stays held and the record is retried every "
            f"sweep until it lands (nothing else acts on this PR meanwhile)"
        ),
        ctx,
    )
