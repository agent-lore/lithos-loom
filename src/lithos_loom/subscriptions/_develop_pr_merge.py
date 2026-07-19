"""``pr``-gate resolver — resolve Epic H ``pr`` gates against their PR (#87, US11).

When a PR-producing plugin (``story-develop``) delivers a PR and exits under a
``completes_task = false`` route, the runner creates a **`pr` gate** (see
:mod:`lithos_loom.gates`) that blocks the delivered story until a human merges
the PR. This module resolves those gates: called per-open-gate by the
github-watcher child's periodic reconcile sweep (``children/github_watcher.py``,
which enumerates open tasks and holds a ``GitHubClient``), it reads the gate's
PR merge state from GitHub and, on merge, completes the story **then** the gate;
on closed-unmerged / deleted it leaves the gate open with a
``[DeliveredPRClosed]`` finding.

De-dup lives in a single ``metadata.develop_pr_merge_state`` marker written on
the GATE (mirrors ``github_state_snapshot``), scoped to the PR url it resolved
so a dead PR isn't re-polled every sweep while a replacement PR re-evaluates.

Until US11 this module also ran a legacy ``develop_pr_url`` *story* sweep for
pre-gate deliveries; that sweep and the ``loom_delivered`` marker are gone — the
gate is now the sole merge-tracking and re-dispatch path.
"""

from __future__ import annotations

from typing import Any

from lithos_loom.errors import LithosClientError
from lithos_loom.gates import (
    is_pr_gate,
    parse_pr_gate,
    waiter_of,
)
from lithos_loom.github_client import GitHubClient, GitHubError
from lithos_loom.subscriptions import SubscriptionContext
from lithos_loom.subscriptions._findings import post_finding_then_mark, write_marker

__all__ = [
    "DELIVERED_PR_CLOSED",
    "GATE_RESOLVED",
    "MERGE_STATE_KEY",
    "MERGE_STATE_TERMINAL",
    "MERGE_STATE_URL_KEY",
    "is_pr_gate",
    "reconcile_pr_gate",
]

# Stable, machine-parseable finding prefix (see AGENTS.md): a delivered PR
# reached a closed-without-merge end state (closed unmerged, or deleted), so the
# task is left open for a human rather than completed.
DELIVERED_PR_CLOSED = "[DeliveredPRClosed]"

# A `pr` gate was resolved on merge (Epic H): the gate + its story are completed
# and this finding records why the story unblocked — gate type, PR, resolver.
GATE_RESOLVED = "[GateResolved]"

# Gate-metadata keys carrying the de-dup marker (written on the GATE). The marker
# is SCOPED to the PR url it resolved (MERGE_STATE_URL_KEY): the resolver skips a
# gate only when its resolved state is terminal AND the recorded url still
# matches the gate's PR url. So when a rejected PR is abandoned and the story is
# re-developed into a REPLACEMENT PR (a fresh url), the recorded url no longer
# matches and the resolver re-evaluates the new PR — without that scoping a stale
# marker would suppress the new PR forever.
MERGE_STATE_KEY = "develop_pr_merge_state"
MERGE_STATE_URL_KEY = "develop_pr_merge_url"

# Marker values that mean "this PR url is resolved". A still-open PR leaves the
# marker UNSET so the resolver re-polls next cycle.
MERGE_STATE_TERMINAL: frozenset[str] = frozenset(
    {"merged", "closed_unmerged", "gone", "unparseable"}
)


def _pr_merge_state(pr: Any) -> str:
    """Classify a fetched (non-``None``) PR: ``merged`` / ``closed_unmerged`` /
    ``still_open``.

    Used by the gate resolver. The ``None`` (deleted) case is handled by callers
    — it needs a per-subject finding — and a raised ``GitHubError`` is transient;
    neither is a merge state.
    """
    if pr.merged:
        return "merged"
    if pr.state == "closed":
        return "closed_unmerged"
    return "still_open"


# ── PR-gate resolver (Epic H) ──────────────────────────────────────────
#
# The sole merge-tracking path since US11 (the legacy develop_pr_url story sweep
# and the loom_delivered marker are gone): the runner creates a `pr` gate per
# delivery, and this resolver owns the gate + its story — on merge it completes
# the story then the gate.


async def reconcile_pr_gate(
    gate: Any, github: GitHubClient, ctx: SubscriptionContext
) -> str | None:
    """Resolve one open ``pr`` gate against its PR's merge state.

    Returns a short outcome label for the sweep's counters
    (``merged`` / ``closed_unmerged`` / ``still_open`` / ``gone`` /
    ``unparseable`` / ``error``), or ``None`` when already resolved for this
    same PR url. Never raises.

    On **merge** the gate + its story are completed (story-first, so a crash can
    never leave the story open-and-ready — the mark-then-complete hazard the
    story sweep's docstring warns of) and a ``[GateResolved]`` finding is posted
    on the story. On **closed-unmerged / deleted** the gate is left OPEN (so the
    story stays correctly ``blocker_unsatisfiable`` — a cancelled gate would be
    terminal and unrecoverable through any Loom surface), a ``[DeliveredPRClosed]``
    finding is posted on the story, and a url-scoped marker on the GATE stops the
    dead PR being re-polled. A still-open PR re-polls next sweep; a transient
    GitHub failure retries.
    """
    spec = parse_pr_gate(gate)
    if spec is None:
        # The server validated gate_type at creation, so this is a
        # loom-side malformation (missing repo/pr_number/pr_url). It can never
        # resolve; leave it open, but mark it so we don't re-post every sweep.
        # The url-scoped terminal-marker guard below only fires once a spec
        # parses (it keys on spec.pr_url), so an unparseable gate needs its own
        # skip here — otherwise it re-marks + re-warns every sweep. A later
        # operator fix makes parse_pr_gate succeed, and the marker (no url key)
        # won't match the guard below, so the repaired gate resolves normally.
        if gate.metadata.get(MERGE_STATE_KEY) == "unparseable":
            return None
        await write_marker(
            ctx,
            task_id=gate.id,
            marker={MERGE_STATE_KEY: "unparseable"},
            subsystem="pr-gate",
        )
        ctx.logger.warning(
            "[Friction] pr-gate: gate %s has unparseable pr metadata (%r); "
            "cannot watch it for merge",
            gate.id,
            dict(gate.metadata),
        )
        return "unparseable"

    if (
        gate.metadata.get(MERGE_STATE_KEY) in MERGE_STATE_TERMINAL
        and gate.metadata.get(MERGE_STATE_URL_KEY) == spec.pr_url
    ):
        # Already resolved THIS pr_url (a closed-unmerged / gone gate left open).
        # A merged gate is completed → out of the open set → never re-swept, so
        # it needs no marker; this guard only fires for the left-open states.
        return None

    story_id = await waiter_of(ctx.lithos, gate.id)

    try:
        pr = await github.get_pull_request(spec.repo, spec.pr_number)
    except GitHubError as exc:
        ctx.logger.warning(
            "[Friction] pr-gate: fetching %s#%d for gate %s failed (%s: %s); "
            "will retry next sweep",
            spec.repo,
            spec.pr_number,
            gate.id,
            type(exc).__name__,
            exc,
        )
        return "error"

    if pr is None:  # 404 — PR or repo gone (permanent, cf. #69)
        await _gate_closed(gate, story_id, spec.pr_url, "gone", ctx)
        return "gone"

    state = _pr_merge_state(pr)
    if state == "merged":
        if await _resolve_gate_merged(gate, story_id, spec.pr_url, pr, ctx):
            return "merged"
        # A completion failed transiently; the gate is left open and retried
        # next sweep. Report it as `error` (not `merged`) so the sweep summary
        # doesn't count an un-landed resolution as resolved.
        return "error"
    if state == "closed_unmerged":
        await _gate_closed(gate, story_id, spec.pr_url, "closed_unmerged", ctx)
        return "closed_unmerged"

    # state == "open" — still in flight; re-poll next sweep (no marker).
    return "still_open"


async def _resolve_gate_merged(
    gate: Any, story_id: str | None, pr_url: str, pr: Any, ctx: SubscriptionContext
) -> bool:
    """PR merged: complete the story, then the gate, then post ``[GateResolved]``.

    Returns ``True`` when the gate is resolved (both completes landed or were
    already terminal), ``False`` on a transient completion failure — the caller
    surfaces that as a retry outcome rather than counting it as ``merged``.

    **Story-first.** Completing the gate first momentarily readies a story that
    still carries its ``trigger:*`` tag; if the story completion then failed, the
    gate would be gone from the open set and the story stranded open-and-ready →
    re-developed into a duplicate PR. Story-first is fail-safe: any failure
    leaves the gate open and the story blocked, and the next sweep retries.
    Both completes swallow ``task_not_found`` so a race with the issue
    close-mirror (or a retry after a partial run) converges. The gate leaving
    the open set is the de-dup — no marker needed on the merged path.
    """
    if story_id is not None and not await _complete_swallowing(
        story_id, ctx, subject=f"story {story_id}"
    ):
        return False  # transient — leave gate open, retry next sweep
    if not await _complete_swallowing(gate.id, ctx, subject=f"gate {gate.id}"):
        return False
    if story_id is not None:
        summary = (
            f"{GATE_RESOLVED} pr-gate: PR {pr_url} merged "
            f"({pr.merge_commit_sha or 'no sha'}); gate {gate.id} resolved and "
            f"story {story_id} completed"
        )
        try:
            await ctx.lithos.finding_post(task_id=story_id, summary=summary)
        except LithosClientError as exc:
            # Observability only; the story + gate are already completed.
            ctx.logger.warning(
                "[Friction] pr-gate: posting %s for story %s failed (%s)",
                GATE_RESOLVED,
                story_id,
                exc,
            )
    ctx.logger.info(
        "pr-gate: resolved gate %s on PR merge %s (%s)",
        gate.id,
        pr_url,
        pr.merge_commit_sha or "no sha",
    )
    return True


async def _complete_swallowing(
    task_id: str, ctx: SubscriptionContext, *, subject: str
) -> bool:
    """``task_complete`` swallowing ``task_not_found`` (already terminal).

    Returns ``True`` when the task is now terminal (completed here or already
    was), ``False`` on a transient error the caller should retry next sweep.
    """
    try:
        await ctx.lithos.task_complete(task_id=task_id)
    except LithosClientError as exc:
        if exc.code == "task_not_found":
            return True  # already terminal — fine
        ctx.logger.warning(
            "[Friction] pr-gate: completing %s failed (%s); will retry next sweep",
            subject,
            exc,
        )
        return False
    return True


async def _gate_closed(
    gate: Any,
    story_id: str | None,
    pr_url: str,
    marker: str,
    ctx: SubscriptionContext,
) -> None:
    """PR closed-unmerged or deleted: leave the gate OPEN, tell the operator.

    The gate is *not* cancelled — a cancelled gate is terminal and its story
    would be permanently ``blocker_unsatisfiable`` with no Loom surface to
    recover it (no ``task_reopen`` / edge-delete wrapper). Left open, the story
    stays correctly blocked with a ``⛔`` in the vault, and the operator's
    recovery is to complete the gate (proceed) or re-point it at a replacement
    PR. A ``[DeliveredPRClosed]`` finding goes on the story; a url-scoped marker
    on the GATE stops the dead PR being re-polled and re-reported every sweep.
    """
    reason = "no longer exists (404)" if marker == "gone" else "was closed unmerged"
    gate_marker = {MERGE_STATE_KEY: marker, MERGE_STATE_URL_KEY: pr_url}
    if story_id is None:
        # Orphan gate (no waiter): nothing to post the finding on. Just mark it.
        await write_marker(
            ctx, task_id=gate.id, marker=gate_marker, subsystem="pr-gate"
        )
        ctx.logger.warning(
            "[Friction] pr-gate: gate %s has no waiter; PR %s %s",
            gate.id,
            pr_url,
            reason,
        )
        return
    await post_finding_then_mark(
        ctx,
        task_id=story_id,
        summary=(
            f"{DELIVERED_PR_CLOSED} pr-gate: delivered PR {pr_url} {reason}; "
            f"story {story_id} left blocked on gate {gate.id} for a human"
        ),
        marker=gate_marker,
        subsystem="pr-gate",
        retry_hint="will retry next sweep",
        marker_task_id=gate.id,
    )
