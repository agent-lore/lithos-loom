"""``develop-pr-merge`` reconcile — auto-close tasks on PR merge (#87).

When a PR-producing plugin (``story-develop``) delivers a PR and exits, the
route uses ``completes_task = false``, so the Lithos task stays **open** with
``metadata.develop_pr_url`` recording the PR. A GitHub-**issue**-linked task
closes on merge via the issue close-mirror (``_github_issue_push``); a task
created directly in Lithos (no ``github_issue_url``) has no such path.

This module polls the delivered PR's merge state and acts on the task. It is
called per-task by the github-watcher child's periodic reconcile sweep
(``children/github_watcher.py``), which already enumerates open tasks and holds
a ``GitHubClient``. It keys off ``develop_pr_url`` only — **plugin-agnostic**:
any plugin that records ``develop_pr_url`` gets merge→complete for free.

De-dup lives in a single ``metadata.develop_pr_merge_state`` marker (mirrors
``github_state_snapshot``): once it reaches a terminal value the sweep skips the
task, so neither completion nor a finding fires twice.

This reconcile reads the delivered PR's merge state from GitHub, independent of
the run-dir markers — a develop run's on-disk fate (``state.json`` /
``result.json`` / ``delivery.json``) is classified by
:mod:`plugins.story_develop.run_outcome`. Now that the classifier is a standalone
module, this reconcile *could* consume it to correlate a PR's merge state with the
run's recorded outcome; today it doesn't, and behaviour is unchanged.
"""

from __future__ import annotations

from typing import Any

from lithos_loom.errors import LithosClientError
from lithos_loom.gates import (
    STORY_GATE_ID_KEY,
    is_pr_gate,
    parse_pr_gate,
    waiter_of,
)
from lithos_loom.github_client import GitHubClient, GitHubError, parse_github_ref
from lithos_loom.subscriptions import SubscriptionContext
from lithos_loom.subscriptions._findings import post_finding_then_mark, write_marker

__all__ = [
    "DELIVERED_PR_CLOSED",
    "GATE_RESOLVED",
    "MERGE_STATE_KEY",
    "MERGE_STATE_TERMINAL",
    "MERGE_STATE_URL_KEY",
    "is_pr_gate",
    "reconcile_develop_pr",
    "reconcile_pr_gate",
]

# Stable, machine-parseable finding prefix (see AGENTS.md): a delivered PR
# reached a closed-without-merge end state (closed unmerged, or deleted), so the
# task is left open for a human rather than completed.
DELIVERED_PR_CLOSED = "[DeliveredPRClosed]"

# A `pr` gate was resolved on merge (Epic H): the gate + its story are completed
# and this finding records why the story unblocked — gate type, PR, resolver.
GATE_RESOLVED = "[GateResolved]"

# Task-metadata keys carrying the de-dup marker. The marker is SCOPED to the
# develop_pr_url it resolved (MERGE_STATE_URL_KEY): the sweep skips a task only
# when its resolved state is terminal AND the recorded url still matches the
# task's current develop_pr_url. So when a rejected PR is abandoned and the task
# is re-developed into a REPLACEMENT PR, develop_pr_url changes, the recorded
# url no longer matches, and the sweep re-evaluates the new PR — without that
# scoping a stale marker would suppress the new PR forever.
MERGE_STATE_KEY = "develop_pr_merge_state"
MERGE_STATE_URL_KEY = "develop_pr_merge_url"

# Marker values that mean "this develop_pr_url is resolved". A still-open PR
# leaves the marker UNSET so the sweep re-polls next cycle.
MERGE_STATE_TERMINAL: frozenset[str] = frozenset(
    {"merged", "closed_unmerged", "gone", "unparseable"}
)


def _parse_pr_url(url: object) -> tuple[str | None, int | None]:
    """``https://github.com/<owner>/<repo>/pull/<n>`` → ``("owner/repo", n)``.

    Thin adapter over :func:`~lithos_loom.github_client.parse_github_ref` that
    keeps this module's ``(None, None)``-on-failure tuple convention and filters
    to PR (``pull``) refs — an issue URL returns ``(None, None)`` here.
    """
    ref = parse_github_ref(url)
    if ref is None or ref.kind != "pull":
        return None, None
    return ref.repo, ref.number


async def reconcile_develop_pr(
    task: Any, github: GitHubClient, ctx: SubscriptionContext
) -> str | None:
    """Reconcile one open task's delivered-PR merge state.

    Returns a short outcome label for the sweep's counters
    (``merged`` / ``closed_unmerged`` / ``still_open`` / ``gone`` /
    ``unparseable`` / ``error``), or ``None`` when the task is not a
    develop-PR task (no ``develop_pr_url``, issue-linked, or already resolved
    for *this same* ``develop_pr_url``). Never raises — GitHub and Lithos
    failures are caught, logged as ``[Friction]``, and (for transient ones)
    retried next sweep by leaving the marker unset.
    """
    metadata = task.metadata
    pr_url = metadata.get("develop_pr_url")
    if not isinstance(pr_url, str) or not pr_url:
        return None
    if metadata.get(STORY_GATE_ID_KEY):
        # A `pr` gate now owns this task's merge lifecycle (Epic H); the gate
        # resolver completes it. Stand aside so the two paths never both act on
        # one PR. Retired with this whole sweep once gates are the sole path.
        return None
    if metadata.get("github_issue_url"):
        # Issue-linked: the issue close-mirror already handles merge→complete.
        return None
    if (
        metadata.get(MERGE_STATE_KEY) in MERGE_STATE_TERMINAL
        and metadata.get(MERGE_STATE_URL_KEY) == pr_url
    ):
        # Already resolved THIS pr_url. A replacement PR (changed develop_pr_url)
        # has a stale recorded url, so it falls through and gets re-evaluated.
        return None

    repo, number = _parse_pr_url(pr_url)
    if repo is None or number is None:
        await _friction_and_mark(
            task,
            ctx,
            "unparseable",
            pr_url,
            f"[Friction] develop-pr-merge: task {task.id} has an unparseable "
            f"develop_pr_url ({pr_url!r}); cannot watch it for merge",
        )
        return "unparseable"

    try:
        pr = await github.get_pull_request(repo, number)
    except GitHubError as exc:
        # Auth / rate-limit / 5xx / network — transient from the sweep's POV.
        # Leave the marker unset so the next cycle retries (the sweep is a
        # natural retry loop; no in-cycle backoff to trigger).
        ctx.logger.warning(
            "[Friction] develop-pr-merge: fetching %s#%d for task %s failed "
            "(%s: %s); will retry next sweep",
            repo,
            number,
            task.id,
            type(exc).__name__,
            exc,
        )
        return "error"

    if pr is None:  # 404 — PR or repo gone (permanent, cf. #69)
        await _friction_and_mark(
            task,
            ctx,
            "gone",
            pr_url,
            f"{DELIVERED_PR_CLOSED} develop-pr-merge: delivered PR {pr_url} no "
            f"longer exists (404) for task {task.id}; left open for a human",
        )
        return "gone"

    state = _pr_merge_state(pr)
    if state == "merged":
        await _complete_merged(task, pr_url, pr, ctx)
        return "merged"

    if state == "closed_unmerged":
        await _friction_and_mark(
            task,
            ctx,
            "closed_unmerged",
            pr_url,
            f"{DELIVERED_PR_CLOSED} develop-pr-merge: delivered PR {pr_url} was "
            f"closed without merging; task {task.id} left open for a human",
        )
        return "closed_unmerged"

    # state == "open" — still in flight; re-poll next sweep (no marker).
    return "still_open"


def _pr_merge_state(pr: Any) -> str:
    """Classify a fetched (non-``None``) PR: ``merged`` / ``closed_unmerged`` /
    ``still_open``.

    Shared by the story sweep and the gate resolver. The ``None`` (deleted) case
    is handled by callers — it needs a per-subject finding — and a raised
    ``GitHubError`` is transient; neither is a merge state.
    """
    if pr.merged:
        return "merged"
    if pr.state == "closed":
        return "closed_unmerged"
    return "still_open"


# ── PR-gate resolver (Epic H) ──────────────────────────────────────────
#
# Coexists with the develop_pr_url story sweep above during the loom_delivered
# soak: the story sweep owns a *gate-less* delivered task (it skips a task
# carrying ``pr_gate_id``), and this resolver owns the gate + its story. The two
# never both act on one PR. When gates become the sole path (US11), the story
# sweep and its markers are deleted and this remains.


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
        await _resolve_gate_merged(gate, story_id, spec.pr_url, pr, ctx)
        return "merged"
    if state == "closed_unmerged":
        await _gate_closed(gate, story_id, spec.pr_url, "closed_unmerged", ctx)
        return "closed_unmerged"

    # state == "open" — still in flight; re-poll next sweep (no marker).
    return "still_open"


async def _resolve_gate_merged(
    gate: Any, story_id: str | None, pr_url: str, pr: Any, ctx: SubscriptionContext
) -> None:
    """PR merged: complete the story, then the gate, then post ``[GateResolved]``.

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
        return  # transient — leave gate open, retry next sweep
    if not await _complete_swallowing(gate.id, ctx, subject=f"gate {gate.id}"):
        return
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


# ── Lithos side-effects (idempotent; swallow task_not_found) ───────────


async def _complete_merged(
    task: Any, pr_url: str, pr: Any, ctx: SubscriptionContext
) -> None:
    """Complete the task on PR merge, then write the ``merged`` marker.

    Order is complete-first: a completion failure leaves the marker unset so the
    next sweep retries the whole reconcile, rather than a marked-but-uncompleted
    task being skipped forever. The marker write then lands on the now-terminal
    task — Lithos accepts ``task_update`` on a terminal task (lithos#303, fixed
    2026-06-19); ``task_complete`` itself stays idempotent (``task_not_found``
    for an already-terminal task is swallowed). The one residual gap — a crash
    *strictly between* the complete and the mark — loses the marker permanently
    (the completed task leaves the open set, so it is never re-swept), but that
    is benign: the merged marker is observability-only, since merged-path de-dup
    is the open-set exclusion, not the marker.
    """
    try:
        await ctx.lithos.task_complete(task_id=task.id)
    except LithosClientError as exc:
        if exc.code == "task_not_found":
            ctx.logger.info(
                "develop-pr-merge: task %s already terminal; marking merged",
                task.id,
            )
        else:
            ctx.logger.warning(
                "[Friction] develop-pr-merge: completing task %s on PR merge "
                "failed (%s); will retry next sweep",
                task.id,
                exc,
            )
            return  # leave the marker unset → retry next sweep
    ctx.logger.info(
        "develop-pr-merge: completed task %s on PR merge %s (%s)",
        task.id,
        pr_url,
        pr.merge_commit_sha or "no sha",
    )
    await _mark(task, ctx, "merged", pr_url)


async def _friction_and_mark(
    task: Any, ctx: SubscriptionContext, marker: str, pr_url: str, summary: str
) -> None:
    """Post a one-shot finding, then write this module's (state, url) marker.

    Builds the develop-pr-merge marker dict and delegates the finding-then-mark
    idiom to :func:`~lithos_loom.subscriptions._findings.post_finding_then_mark`
    (shared with ``_github_issue_push``).
    """
    await post_finding_then_mark(
        ctx,
        task_id=task.id,
        summary=summary,
        marker={MERGE_STATE_KEY: marker, MERGE_STATE_URL_KEY: pr_url},
        subsystem="develop-pr-merge",
        retry_hint="will retry next sweep",
    )


async def _mark(task: Any, ctx: SubscriptionContext, state: str, pr_url: str) -> None:
    """Write the de-dup marker (state + the url it resolved), no finding.

    The finding-less path (merged → mark ``merged``); delegates to the shared
    :func:`~lithos_loom.subscriptions._findings.write_marker`.
    """
    await write_marker(
        ctx,
        task_id=task.id,
        marker={MERGE_STATE_KEY: state, MERGE_STATE_URL_KEY: pr_url},
        subsystem="develop-pr-merge",
    )
