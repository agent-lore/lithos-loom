"""``pr``-gate resolver — resolve Epic H ``pr`` gates against their PR (#87, US11).

When a PR-producing plugin (``story-develop``) delivers a PR and exits under a
``completes_task = false`` route, the runner creates a **`pr` gate** (see
:mod:`lithos_loom.gates`) that blocks the delivered story until a human merges
the PR. This module resolves those gates: called per-open-gate by the
github-watcher child's periodic reconcile sweep (``children/github_watcher.py``,
which enumerates open tasks and holds a ``GitHubClient``), it reads the gate's
PR merge state from GitHub and, on merge, completes the story **then** the gate;
on closed-unmerged / deleted it leaves the gate open with a
``[DeliveredPRClosed]`` finding; while still open it also reports
landability (:mod:`.pr_landability`, PRD S1) and ingests external reviews
(:mod:`.external_reviews`, PRD S2).

De-dup lives in a single ``metadata.develop_pr_merge_state`` marker written on
the GATE (mirrors ``github_state_snapshot``), scoped to the PR url it resolved
so a dead PR isn't re-polled every sweep while a replacement PR re-evaluates.

Until US11 this module also ran a legacy ``develop_pr_url`` *story* sweep for
pre-gate deliveries; that sweep and the ``loom_delivered`` marker are gone — the
gate is now the sole merge-tracking and re-dispatch path.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from typing import Any, NamedTuple

from lithos_loom.errors import LithosClientError
from lithos_loom.gates import (
    PrGateSpec,
    is_pr_gate,
    parse_pr_gate,
    waiter_of,
)
from lithos_loom.github_client import GitHubClient, GitHubError
from lithos_loom.subscriptions import SubscriptionContext
from lithos_loom.subscriptions._findings import post_finding_then_mark, write_marker
from lithos_loom.subscriptions.dispatch_guards import READY_QUERY_LIMIT
from lithos_loom.subscriptions.external_remediation import ExternalRemediation
from lithos_loom.subscriptions.external_reviews import ingest_external_reviews
from lithos_loom.subscriptions.merge_gate_dispatch import MergeGateDispatch
from lithos_loom.subscriptions.pr_landability import check_landability

__all__ = [
    "DELIVERED_PR_CLOSED",
    "GATE_RESOLVED",
    "MERGE_STATE_KEY",
    "MERGE_STATE_TERMINAL",
    "MERGE_STATE_URL_KEY",
    "NUDGE_RECOVERED_KEY",
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

# Gate-metadata key recording that the RECOVERY fan-out (the graph fallback in
# `_complete_story`, taken when the completion response is gone for good) has
# already been nudged for this PR url. The merged branch writes no
# MERGE_STATE_KEY marker — the gate leaving the open set is its de-dup — so a
# gate whose own completion fails for a DURABLE reason stays in the swept open
# set and re-enters the merged branch every sweep. This marker bounds that to
# one fan-out: without it loom would re-write every released dependent once per
# sweep, forever, and each write is indistinguishable from the human edit the
# #339 bootstrap-replay guard reads as authorization to re-run a failed story.
NUDGE_RECOVERED_KEY = "develop_pr_merge_nudged"

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


async def _live_base_tip(
    github: Any, spec: PrGateSpec, pr: Any, ctx: SubscriptionContext
) -> str:
    """The base branch's current tip, or ``""`` when it cannot be read."""
    branch = getattr(pr, "base_ref", "") or ""
    if not branch:
        return ""
    try:
        tip = await github.get_branch_tip(spec.repo, branch)
    except GitHubError as exc:
        ctx.logger.warning(
            "[Friction] pr-gate: reading base tip %s@%s for %s failed (%s: %s); "
            "landability + re-gate skipped this sweep",
            spec.repo,
            branch,
            spec.pr_url,
            type(exc).__name__,
            exc,
        )
        return ""
    if not tip:
        ctx.logger.warning(
            "[Friction] pr-gate: base branch %s@%s of %s not found; "
            "landability + re-gate skipped this sweep",
            spec.repo,
            branch,
            spec.pr_url,
        )
    return tip or ""


async def reconcile_pr_gate(
    gate: Any,
    github: GitHubClient,
    ctx: SubscriptionContext,
    *,
    ingest_reviews: bool = False,
    remediation: ExternalRemediation | None = None,
    merge_gate: MergeGateDispatch | None = None,
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

    ``ingest_reviews`` additionally runs the external-review ingestion
    (:mod:`.external_reviews`, PRD S2) on the still-open branch — the one place
    that has the fetched PR, the parsed spec and the waiting story all in
    hand. It never changes the merge outcome. ``remediation``, when set (and
    ingestion is on), wraps that ingestion with the slice-C autonomy: the
    head observation + S5b budget *before* it (so exhaustion is stated in
    the finding body), the dispatch decision *after* it (on the batch it
    posted). See :mod:`.external_remediation`. ``merge_gate``, when set, is
    considered LAST on the still-open branch (PRD S3): it re-gates the PR
    against its base's current tip on a key change, told whether a
    remediation run is in flight on this PR so the two never push beside
    each other. See :mod:`.merge_gate_dispatch`.
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
        if ingest_reviews:
            # PR #348 review F1 + re-review 1: a review that landed before
            # the first sweep, on a PR that merged before that sweep, would
            # otherwise vanish unrecorded — the gate leaves the open set on
            # completion and is never swept again. Ingest ONCE here
            # (detection-only: the [ExternalReview] record on the story;
            # remediation on a merged PR is structurally impossible —
            # converge refuses it — and the human merge is the authority on
            # the PR's final state). A FAILED observation (GitHub listing
            # error, or the record/mark not landing) defers resolution: the
            # gate stays open and the whole branch retries next sweep,
            # because after resolution there is no next sweep to retry in.
            final = await ingest_external_reviews(
                gate, spec, story_id, github, ctx, post_merge=True
            )
            if final.failed:
                ctx.logger.warning(
                    "[Friction] pr-gate: final review observation for %s "
                    "failed; leaving the merged gate open to retry — "
                    "resolution deferred to the next sweep",
                    spec.pr_url,
                )
                return "error"
        if await _resolve_gate_merged(gate, story_id, spec.pr_url, pr, ctx):
            return "merged"
        # A completion failed transiently; the gate is left open and retried
        # next sweep. Report it as `error` (not `merged`) so the sweep summary
        # doesn't count an un-landed resolution as resolved.
        return "error"
    if state == "closed_unmerged":
        await _gate_closed(gate, story_id, spec.pr_url, "closed_unmerged", ctx)
        return "closed_unmerged"

    # state == "open" — still in flight; re-poll next sweep (no merge marker).
    # The base-move key for everything below is the base branch's LIVE tip
    # (the PR payload's base.sha is a stale snapshot — see PullRequest);
    # unreadable → "" and neither consumer keys on it this sweep.
    pr = replace(pr, base_sha=await _live_base_tip(github, spec, pr, ctx))
    # PRD S1: say so on the story when the PR cannot merge as it stands. Runs
    # on every merge poll (no separate dial): it reads fields the fetch above
    # already returned and writes only on a change.
    await check_landability(gate, spec, story_id, pr, ctx)
    if ingest_reviews:
        budget = None
        note = None
        if remediation is not None:
            budget = await remediation.observe_head(gate, spec, pr, ctx)
            note = remediation.exhaustion_note(budget)
        ingest = await ingest_external_reviews(
            gate,
            spec,
            story_id,
            github,
            ctx,
            extra_note=note,
            # Parked atomically with the seen marks, and only for a batch
            # the provider finds dispatchable (PR #346 re-reviews 1+3): the
            # marks consume the batch, so its dispatch debt must become
            # durable in the same write or the whole batch retries — and an
            # undispatchable batch must neither park nor clear.
            pending_marker_for=(
                remediation.pending_marker_provider(spec, story_id, budget, github, ctx)
                if remediation is not None and budget is not None
                else None
            ),
        )
        if remediation is not None and budget is not None:
            if ingest.posted:
                label: str | None = await remediation.consider(
                    gate, spec, story_id, budget, ingest, github, ctx
                )
            else:
                # A quiet sweep may still owe a dispatch: a batch deferred
                # behind the busy slot parked a pending trigger (its marks
                # were consumed when it posted — PR #346 review F1).
                label = await remediation.resume_pending(
                    gate, spec, story_id, budget, github, ctx
                )
            if label is not None:
                ctx.logger.info("external-remediation: %s for %s", label, spec.pr_url)
    if merge_gate is not None:
        held = remediation is not None and remediation.busy_on(spec.pr_url)
        verdict = await merge_gate.consider(gate, spec, story_id, pr, ctx, hold=held)
        if verdict != "unchanged":
            ctx.logger.info("merge-gate: %s for %s", verdict, spec.pr_url)
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

    The story completion's ``unblocked`` ids are nudged (see
    :func:`_nudge_unblocked`) so a now-ready dependent dispatches without
    waiting for a daemon restart. That happens **before the gate is
    completed**, deliberately: completing the gate is the durable "this merge
    is resolved" transition — it takes the gate out of the open set the sweep
    enumerates, so anything after it (a crash, a kill, a cancellation) is
    never retried. With the nudge ahead of it, an interrupted batch leaves the
    gate open and the next sweep re-enters this branch, where
    :func:`_complete_story` recovers the ids the lost response would have
    named — the story's ``blocks`` targets, intersected with Lithos's ready
    frontier so a dependent still held by a sibling blocker is not written to,
    and marked on the gate so a gate stuck open cannot re-send that fan-out
    every sweep. Anything that leaves those ids *unknown* — a failed
    completion, or a graph read that did not answer — returns ``False`` here
    rather than resolving the gate, so the retry stays possible.
    """
    if story_id is not None:
        completion = await _complete_story(story_id, gate, pr_url, ctx)
        if completion is None:
            return False  # transient — leave gate open, retry next sweep
        await _nudge_unblocked(completion.to_nudge, story_id, ctx)
        if completion.recovered and completion.to_nudge:
            # Durably record that this gate's recovery fan-out has been sent, so
            # a gate stuck open on a failing completion cannot re-send it every
            # sweep (see NUDGE_RECOVERED_KEY). Best-effort like the nudge
            # itself: if the marker does not land the next sweep re-nudges,
            # which is the pre-existing behaviour and no worse than it.
            await write_marker(
                ctx,
                task_id=gate.id,
                marker={NUDGE_RECOVERED_KEY: pr_url},
                subsystem="pr-gate",
            )
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


async def _nudge_unblocked(
    task_ids: Sequence[str], story_id: str | None, ctx: SubscriptionContext
) -> None:
    """Re-surface the dependents the story's completion just readied (#350).

    ``task_complete`` names the tasks whose last ``blocks`` predecessor just
    cleared. The route-runner has US6 machinery for exactly this
    (``RouteRunner._re_dispatch_unblocked``) but it lives in a *different*
    child with no IPC, and the ``task.completed`` for the story names the
    story, not its dependents — so a tagged, now-ready dependent would sit
    until a restart's bootstrap replay.

    The nudge therefore goes through **Lithos as the bus** (the architecture's
    ``sources → bus → subscribers``, no inter-child IPC): a no-op
    ``task_update(metadata={})`` bumps ``updated_at`` and emits
    ``lithos.task.updated``, which the runner picks up on its normal SSE path
    — route match, ready check, collision-safe claim. Double-evaluation is
    harmless; the claim decides.

    The write is not free of side effects, which is why the caller keeps this
    set to ids Lithos has vouched for — the ones it named as unblocked, or on
    the recovery path the ones it still reports ready
    (:func:`_released_dependents`) — and sends the recovery fan-out at most
    once per gate (:data:`NUDGE_RECOVERED_KEY`): bumping ``updated_at``
    consumes the "nobody has edited it since it failed" evidence the #339
    bootstrap-replay guard reads (``dispatch_guards.declines_bootstrap_replay``),
    so nudging a task that is not even ready — or re-nudging one every sweep —
    could cost an unrequested re-run of a failed story on the next restart.

    Best-effort. The story is completed by the time we get here, so a failed
    nudge must not undo the merge resolution: it posts ``[Friction]`` on the
    story, lets the caller finish resolving the gate, and leaves the bootstrap
    replay as the backstop. Never raises a ``LithosClientError`` — but
    cancellation propagates, and the caller's ordering (nudge before the gate's
    terminal transition) is what makes an interrupted batch retryable.
    """
    for task_id in task_ids:
        try:
            await ctx.lithos.task_update(task_id=task_id, metadata={})
        except LithosClientError as exc:
            ctx.logger.warning(
                "[Friction] pr-gate: nudging newly-unblocked %s failed (%s); "
                "it waits for the next daemon restart",
                task_id,
                exc,
            )
            if story_id is not None:
                await _post_friction(
                    story_id,
                    ctx,
                    summary=(
                        f"[Friction] pr-gate: story {story_id} completed on PR "
                        f"merge but nudging newly-unblocked task {task_id} "
                        f"failed ({exc}); it will not dispatch until the "
                        f"daemon restarts or the task is touched"
                    ),
                )
        else:
            ctx.logger.info(
                "pr-gate: nudged newly-unblocked %s after story %s completed",
                task_id,
                story_id,
            )


async def _blocks_dependents(
    story_id: str, ctx: SubscriptionContext
) -> list[str] | None:
    """The tasks the story ``blocks`` — the CANDIDATES for a completion's lost
    ``unblocked`` response, which :func:`_released_dependents` then narrows to
    the ones actually released (these edges name every dependent, released or
    not).

    The graph edges are the record: unlike the response they survive a crash,
    so a retried sweep can still nudge. Returns ``None`` when the read itself
    failed — "could not read the edges" must NOT collapse into "there are no
    edges", or a transient failure here would silently nudge nobody and the
    caller would then complete the gate, putting the story's dependents beyond
    the reach of any later sweep. ``None`` defers the whole resolution instead.
    """
    try:
        edges = await ctx.lithos.task_edge_list(
            task_id=story_id, direction="outgoing", types=["blocks"]
        )
    except LithosClientError as exc:
        ctx.logger.warning(
            "[Friction] pr-gate: listing story %s's dependents failed (%s); "
            "leaving the gate open to retry the nudge next sweep",
            story_id,
            exc,
        )
        return None
    return [edge.to_task_id for edge in edges]


async def _post_friction(
    task_id: str, ctx: SubscriptionContext, *, summary: str
) -> None:
    """Post a ``[Friction]`` finding, swallowing a failure to post it."""
    try:
        await ctx.lithos.finding_post(task_id=task_id, summary=summary)
    except LithosClientError as exc:
        ctx.logger.warning(
            "[Friction] pr-gate: posting friction for task %s failed (%s)",
            task_id,
            exc,
        )


class _StoryCompletion(NamedTuple):
    """What :func:`_complete_story` learned: who to nudge, and whether those ids
    came from the durable graph (the recovery path) instead of Lithos's own
    ``unblocked`` response. Only a recovery fan-out needs bounding by
    :data:`NUDGE_RECOVERED_KEY` — the authoritative path runs at most once,
    because the completion that produced it cannot succeed twice."""

    to_nudge: list[str]
    recovered: bool


async def _complete_story(
    story_id: str, gate: Any, pr_url: str, ctx: SubscriptionContext
) -> _StoryCompletion | None:
    """Complete the story and answer **who to nudge**, or ``None`` to retry.

    Three outcomes, deliberately kept apart (collapsing any two of them loses
    dependents):

    * **completed here** → the ids Lithos names as newly unblocked, which is
      the authoritative answer and often empty for a good reason: an ordinary
      fan-in dependent still waiting on a *sibling* blocker is not ready, and
      must not be nudged. No fallback runs on this branch.
    * **already terminal** (``task_not_found`` — the issue close-mirror got
      there first, or an earlier sweep completed the story and died mid-nudge)
      → the response that would have named them is gone for good, so recover
      the candidates from the durable graph — but only the ones Lithos still
      reports ready (:func:`_released_dependents`), and only once per gate
      (:data:`NUDGE_RECOVERED_KEY`).
    * **transient failure** (either the completion or that graph read) →
      ``None``: who to nudge is unknown, so the caller leaves the gate open
      and the next sweep tries the whole branch again.
    """
    try:
        return _StoryCompletion(
            await ctx.lithos.task_complete(task_id=story_id), recovered=False
        )
    except LithosClientError as exc:
        if exc.code == "task_not_found":
            if gate.metadata.get(NUDGE_RECOVERED_KEY) == pr_url:
                # This gate's fan-out already went out; re-sending it would be
                # a pure write with no new information. Nothing to nudge, and
                # nothing to re-mark — the caller only has the gate left.
                return _StoryCompletion([], recovered=True)
            released = await _released_dependents(story_id, ctx)
            if released is None:
                return None
            return _StoryCompletion(released, recovered=True)
        ctx.logger.warning(
            "[Friction] pr-gate: completing story %s failed (%s); "
            "will retry next sweep",
            story_id,
            exc,
        )
        return None


async def _released_dependents(
    story_id: str, ctx: SubscriptionContext, *, limit: int = READY_QUERY_LIMIT
) -> list[str] | None:
    """The story's dependents that this completion actually released, rebuilt
    without the lost ``unblocked`` response. ``None`` = could not tell, retry.

    The graph alone OVER-approximates: :func:`_blocks_dependents` names every
    outgoing ``blocks`` target, whereas ``unblocked`` named only those whose
    *last* blocker was this story. With ``A → C``, ``A → D`` and ``B → D``,
    completing ``A`` releases ``C`` alone — nudging ``D`` too would write to a
    task that is not ready, bumping the ``updated_at`` the #339
    bootstrap-replay guard reads as "a human asked for this again"
    (:func:`_nudge_unblocked` spells the hazard out).

    So the candidates are intersected with Lithos's ready frontier rather than
    filtered by a readiness rule of loom's own: readiness is Lithos's answer
    (epic G), and it covers unmet gates and cycles, not just ``blocks``.

    Every candidate must come back **classified**. A full page means the
    frontier was truncated, which makes absence from it meaningless — and
    unlike the dispatch guard's version of this test (``on_ready_frontier``,
    which merely defers one dispatch to the next event) there is no next look
    here: the caller resolves the gate on this answer, and a resolved gate
    leaves the swept open set for good. So an unclassifiable candidate returns
    ``None`` and defers the whole resolution, exactly as an unreadable edge
    list does. It is all-or-nothing on purpose — nudging the confirmed subset
    would also burn the once-only :data:`NUDGE_RECOVERED_KEY` on an incomplete
    fan-out, making the loss permanent.
    """
    candidates = await _blocks_dependents(story_id, ctx)
    if not candidates:
        return candidates  # None (read failed) or [] (no dependents) as-is
    try:
        # Unnarrowed: a `blocks` edge may cross projects, and the frontier page
        # is wide (READY_QUERY_LIMIT) — a tag/project filter would silently drop
        # a legitimate dependent for no saving on a path this rare.
        ready = await ctx.lithos.task_ready(limit=limit, with_claims=False)
    except LithosClientError as exc:
        ctx.logger.warning(
            "[Friction] pr-gate: reading the ready frontier to recover story "
            "%s's released dependents failed (%s); leaving the gate open to "
            "retry the nudge next sweep",
            story_id,
            exc,
        )
        return None
    ready_ids = {task.id for task in ready}
    released = [task_id for task_id in candidates if task_id in ready_ids]
    if len(released) < len(candidates) and len(ready) >= limit:
        # A FULL page means the frontier was truncated, so absence from it is
        # "not seen", not "not ready" — and this is the caller's last chance to
        # act: resolving the gate takes it out of the swept open set for good,
        # so a candidate dropped here never gets its `task.updated` and waits
        # for a restart, the exact failure #350 removes. Undetermined defers the
        # whole resolution, all-or-nothing (partially nudging would also burn
        # the once-only NUDGE_RECOVERED_KEY on an incomplete fan-out).
        ctx.logger.warning(
            "[Friction] pr-gate: the ready frontier hit its %d-task query limit "
            "while recovering story %s's released dependents, so %d of %d could "
            "not be classified; leaving the gate open to retry next sweep. Raise "
            "READY_QUERY_LIMIT if a frontier this wide is expected.",
            limit,
            story_id,
            len(candidates) - len(released),
            len(candidates),
        )
        return None
    return released


async def _complete_swallowing(
    task_id: str, ctx: SubscriptionContext, *, subject: str
) -> bool:
    """``task_complete`` swallowing ``task_not_found`` (already terminal).

    Returns ``True`` when the task is now terminal (completed here or already
    was), ``False`` on a transient error the caller should retry next sweep.
    The story has its own variant (:func:`_complete_story`) because it also
    has to answer *who this released*.
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
