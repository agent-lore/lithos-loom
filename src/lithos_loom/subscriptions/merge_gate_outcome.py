"""What the merge-gate dispatcher writes and posts per run outcome (PRD S3).

The record on the gate plus, per outcome, the one-shot finding on the
story: ``[MergeGateFailed]`` (red / a required check that could not be
verified), ``[PRConflicted]`` widened with the conflicting paths (the trial
merge is the only source of that list), and ``[Friction]`` for a failed
push beside a green verdict, an unresolvable config, a checkout that is not
the gate's repo, or a crash. Finding-then-mark throughout, so a crash
between the two costs at most one duplicate. Split from
:mod:`.merge_gate_dispatch` (the decision + the subprocess) for the module
budget; the record shape lives in :mod:`.merge_gate_record`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from lithos_loom.errors import LithosClientError
from lithos_loom.gates import PrGateSpec
from lithos_loom.subscriptions import SubscriptionContext
from lithos_loom.subscriptions._findings import post_finding_then_mark, write_marker
from lithos_loom.subscriptions.merge_gate_record import (
    MAX_ATTEMPTS_PER_KEY,
    MERGE_GATE_FAILED,
    MERGE_GATE_KEY,
    MergeGateRecord,
)
from lithos_loom.subscriptions.pr_landability import PR_CONFLICTED
from lithos_loom.subscriptions.remediation_budget import (
    REMEDIATION_KEY,
    RemediationBudget,
    read_budget,
)

__all__ = [
    "post_config_unresolved",
    "post_conflict",
    "post_crashed",
    "post_failed",
    "post_push_failed",
    "post_repo_mismatch",
    "record_green",
    "value_of",
    "write_record",
]


def value_of(data: Mapping[str, Any], key: str) -> str:
    value = data.get(key)
    return value if isinstance(value, str) else ""


async def write_record(
    gate_id: str, record: MergeGateRecord, ctx: SubscriptionContext
) -> None:
    await write_marker(
        ctx,
        task_id=gate_id,
        marker={MERGE_GATE_KEY: record.as_marker()},
        subsystem="merge-gate",
    )


async def record_green(
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
        marker[REMEDIATION_KEY] = replace(
            budget, last_loom_pushed_sha=record.pushed_sha
        ).as_marker()
    await write_marker(ctx, task_id=gate_id, marker=marker, subsystem="merge-gate")


async def post_failed(
    gate_id: str,
    story_id: str,
    spec: PrGateSpec,
    record: MergeGateRecord,
    data: Mapping[str, Any],
    base_ref: str,
    ctx: SubscriptionContext,
) -> None:
    rows = data.get("checks")
    failing = [
        c for c in (rows if isinstance(rows, list) else []) if not c.get("passed")
    ]
    named = "; ".join(
        f"{c.get('name')} ({c.get('command')}) — "
        + (
            "errored, not verified"
            if c.get("outcome") == "errored"
            else "timed out"
            if c.get("timed_out")
            else f"exit {c.get('exit_code')}"
        )
        for c in failing
    )
    why = (
        f"went {record.verdict}"
        if record.verdict
        else "could not be verified (a required check errored)"
    )
    await post_finding_then_mark(
        ctx,
        task_id=story_id,
        summary=(
            f"{MERGE_GATE_FAILED} merge-gate: delivered PR {spec.pr_url} would "
            f"break {base_ref}: the project's current check-set {why} on the "
            f"trial merge {record.merge_sha[:12]} (head {record.head_sha[:12]} "
            f"+ base {record.base_sha[:12]}) — {named or 'no check named'}; "
            f"story {story_id} remains blocked on gate {gate_id}. Fix on the PR "
            f"branch (never rebase a delivered branch); the next sweep re-gates "
            f"at the new head."
        ),
        marker={MERGE_GATE_KEY: record.as_marker()},
        subsystem="merge-gate",
        retry_hint="will retry next sweep",
        marker_task_id=gate_id,
    )


async def post_conflict(
    gate_id: str,
    story_id: str,
    spec: PrGateSpec,
    record: MergeGateRecord,
    data: Mapping[str, Any],
    base_ref: str,
    ctx: SubscriptionContext,
) -> None:
    raw = data.get("conflicting_paths")
    paths = [p for p in (raw if isinstance(raw, list) else []) if isinstance(p, str)]
    await post_finding_then_mark(
        ctx,
        task_id=story_id,
        summary=(
            f"{PR_CONFLICTED} merge-gate: delivered PR {spec.pr_url} conflicts "
            f"with {base_ref} @ {record.base_sha[:12]} (head "
            f"{record.head_sha[:12]}) in {len(paths)} path(s): "
            f"{', '.join(paths) or '(unnamed)'}; story {story_id} remains "
            f"blocked on gate {gate_id}. Resolve by merging {base_ref} into the "
            f"PR branch (never rebase a delivered branch) and pushing; the next "
            f"sweep re-evaluates at the new head."
        ),
        marker={MERGE_GATE_KEY: record.as_marker()},
        subsystem="merge-gate",
        retry_hint="will retry next sweep",
        marker_task_id=gate_id,
    )


async def post_push_failed(
    gate_id: str,
    story_id: str,
    spec: PrGateSpec,
    record: MergeGateRecord,
    base_ref: str,
    ctx: SubscriptionContext,
) -> None:
    again = (
        "retried next sweep"
        if record.attempts < MAX_ATTEMPTS_PER_KEY
        else "the green verdict stands; a settings change, a head push or a "
        "base move re-gates"
    )
    await post_finding_then_mark(
        ctx,
        task_id=story_id,
        summary=(
            f"[Friction] merge-gate: delivered PR {spec.pr_url} is green on the "
            f"trial merge {record.merge_sha[:12]} with {base_ref} @ "
            f"{record.base_sha[:12]}, but pushing the merge commit onto the PR "
            f"branch failed: {record.push_error or 'no reason reported'} "
            f"(attempt {record.attempts}/{MAX_ATTEMPTS_PER_KEY}; {again}). The "
            f"PR is still behind {base_ref}; merge {base_ref} in by hand if it "
            f"must land now."
        ),
        marker={MERGE_GATE_KEY: record.as_marker()},
        subsystem="merge-gate",
        retry_hint="will retry next sweep",
        marker_task_id=gate_id,
    )


async def post_repo_mismatch(
    gate_id: str,
    story_id: str,
    spec: PrGateSpec,
    record: MergeGateRecord,
    data: Mapping[str, Any],
    ctx: SubscriptionContext,
) -> None:
    expected = value_of(data, "expected_repo") or spec.repo
    actual = value_of(data, "actual_repo") or "(unknown)"
    again = (
        "retried next sweep"
        if record.attempts < MAX_ATTEMPTS_PER_KEY
        else "waits for a head or base move after the mapping is fixed"
    )
    await post_finding_then_mark(
        ctx,
        task_id=story_id,
        summary=(
            f"[Friction] merge-gate: the checkout mapped for this project "
            f"has origin {actual}, not the gate's {expected} (PR "
            f"{spec.pr_url}); nothing was fetched, gated or pushed. Fix "
            f"[projects.<slug>].repo in the host config and restart loom "
            f"(attempt {record.attempts}/{MAX_ATTEMPTS_PER_KEY}; {again})."
        ),
        marker={MERGE_GATE_KEY: record.as_marker()},
        subsystem="merge-gate",
        retry_hint="will retry next sweep",
        marker_task_id=gate_id,
    )


async def post_config_unresolved(
    gate_id: str,
    story_id: str,
    spec: PrGateSpec,
    record: MergeGateRecord,
    tail: str,
    ctx: SubscriptionContext,
) -> None:
    await post_finding_then_mark(
        ctx,
        task_id=story_id,
        summary=(
            f"[Friction] merge-gate: the current config for story {story_id} "
            f"(PR {spec.pr_url}) could not be resolved — nothing was gated "
            f"(S3 gates with the project's current config or not at all): "
            f"{tail}"
        ),
        marker={MERGE_GATE_KEY: record.as_marker()},
        subsystem="merge-gate",
        retry_hint="will retry next sweep",
        marker_task_id=gate_id,
    )


async def post_crashed(
    gate_id: str,
    story_id: str,
    spec: PrGateSpec,
    record: MergeGateRecord,
    detail: str,
    ctx: SubscriptionContext,
) -> None:
    record = replace(record, status="crashed")
    ctx.logger.warning(
        "merge-gate: run for %s crashed (attempt %d/%d): %s",
        spec.pr_url,
        record.attempts,
        MAX_ATTEMPTS_PER_KEY,
        detail,
    )
    await post_finding_then_mark(
        ctx,
        task_id=story_id,
        summary=(
            f"[Friction] merge-gate: develop merge-gate for {spec.pr_url} "
            f"(story {story_id}) {detail} (attempt {record.attempts}/"
            f"{MAX_ATTEMPTS_PER_KEY}"
            + (
                "; retried next sweep)"
                if record.attempts < MAX_ATTEMPTS_PER_KEY
                else "; waits for a head or base move)"
            )
        ),
        marker={MERGE_GATE_KEY: record.as_marker()},
        subsystem="merge-gate",
        retry_hint="will retry next sweep",
        marker_task_id=gate_id,
    )
