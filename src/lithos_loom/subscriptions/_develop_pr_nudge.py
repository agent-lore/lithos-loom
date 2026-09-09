"""Re-surface the dependents a story's completion released (#350).

The github-watcher child completes a delivered story when its PR merges
(:mod:`._develop_pr_merge`), but it runs no ``RouteRunner`` and has no IPC to
the child that does — and the ``task.completed`` it emits names the *story*,
not the dependents the completion just readied. So a tagged, now-ready
dependent would sit until a daemon restart replayed it.

This module is the bridge, and it uses **Lithos as the bus** (the
architecture's ``sources → bus → subscribers``, no inter-child IPC): a no-op
``task_update(metadata={})`` on each released dependent bumps ``updated_at``
and emits the ``lithos.task.updated`` the route-runner already route-matches,
ready-checks and claims on.

Two answers to "who was released", kept apart because they are not equally
trustworthy:

* the completion's own ``unblocked`` response — authoritative, used verbatim;
* after that response is lost (an interrupted sweep, or the issue close-mirror
  completing the story first), a **recovery** rebuilt from the durable graph:
  the story's ``blocks`` targets, each put to Lithos individually to see
  whether it is actually released. That rebuild is the delicate half — it
  over-approximates if taken raw, it can be denied by a saturated frontier
  page, and it must not write to a task that is not ready — so it carries its
  own state on the gate (:class:`RecoveryRecord`) and its own bound
  (:data:`RECOVERY_UNDETERMINED_MAX_SWEEPS`).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, NamedTuple

from lithos_loom.errors import LithosClientError
from lithos_loom.subscriptions import SubscriptionContext
from lithos_loom.subscriptions._findings import post_finding_then_mark
from lithos_loom.subscriptions.dispatch_guards import READY_QUERY_LIMIT

__all__ = [
    "NUDGE_RECOVERED_KEY",
    "NUDGE_UNDETERMINED_KEY",
    "RECOVERY_UNDETERMINED_MAX_SWEEPS",
    "NudgePlan",
    "RecoveryRecord",
    "nudge_unblocked",
    "recover_dependents",
]

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

# Gate-metadata key de-duping the one-shot [Friction] posted when a recovery
# sweep cannot classify a dependent. The deferral that follows is correct but
# silent — the gate just stays open and re-polls — so the story gets one
# task-level breadcrumb, scoped to the PR url, rather than one per sweep.
NUDGE_UNDETERMINED_KEY = "develop_pr_merge_nudge_undetermined"

# How many consecutive sweeps may end with a candidate loom cannot classify
# before it stops deferring the gate. Deferring is the safe answer but an
# unbounded one: the gate never completes, never posts [GateResolved] and
# re-polls GitHub every sweep on a resolution that may never land. At the
# bound the resolver gives up on the residue LOUDLY — a finding naming the
# exact ids — and resolves the merged gate, which is what the PR's state
# actually says.
RECOVERY_UNDETERMINED_MAX_SWEEPS = 3


async def nudge_unblocked(
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
    (:func:`_is_ready`) — and sends the recovery fan-out at most
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
    ``unblocked`` response, which :func:`recover_dependents` then narrows to
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


class RecoveryRecord(NamedTuple):
    """The recovery path's durable state, kept on the GATE under
    :data:`NUDGE_RECOVERED_KEY` and scoped to the PR url (a re-develop into a
    replacement PR starts a fresh one).

    ``nudged`` is what has already been re-surfaced — never nudged again, so a
    gate that stays open cannot re-mint ``updated_at`` evidence for the #339
    guard. ``undetermined`` counts the consecutive sweeps that ended with a
    candidate loom could not classify, which is what bounds the deferral.
    """

    pr_url: str
    nudged: tuple[str, ...]
    undetermined: int

    def as_metadata(self) -> dict[str, Any]:
        return {
            "pr_url": self.pr_url,
            "nudged": list(self.nudged),
            "undetermined": self.undetermined,
        }


def _recovery_record(gate: Any, pr_url: str) -> RecoveryRecord:
    """Read the gate's recovery record, or a fresh one.

    Anything that does not parse — a hand-edited value, or the bare url this
    key held before the record shape — reads as fresh. Re-nudging is harmless
    (the claim decides); mis-parsing into a *wrong* nudged-set would not be.
    """
    raw = gate.metadata.get(NUDGE_RECOVERED_KEY)
    if not isinstance(raw, Mapping) or raw.get("pr_url") != pr_url:
        return RecoveryRecord(pr_url, (), 0)
    nudged = raw.get("nudged")
    undetermined = raw.get("undetermined")
    return RecoveryRecord(
        pr_url,
        tuple(x for x in nudged if isinstance(x, str))
        if isinstance(nudged, list)
        else (),
        undetermined if isinstance(undetermined, int) else 0,
    )


class NudgePlan(NamedTuple):
    """What :func:`_complete_story` decided: who to nudge now, the recovery
    state to persist first (``None`` on the authoritative path, which needs
    none — the completion that produced it cannot succeed twice), and whether
    the gate must stay open for a later sweep to finish the job."""

    to_nudge: list[str]
    record: RecoveryRecord | None = None
    defer: bool = False


async def recover_dependents(
    story_id: str,
    gate: Any,
    pr_url: str,
    ctx: SubscriptionContext,
    *,
    limit: int = READY_QUERY_LIMIT,
) -> NudgePlan | None:
    """Rebuild the lost ``unblocked`` response from the durable graph.

    The edges alone OVER-approximate: :func:`_blocks_dependents` names every
    outgoing ``blocks`` target, whereas ``unblocked`` named only those whose
    *last* blocker was this story. With ``A → C``, ``A → D`` and ``B → D``,
    completing ``A`` releases ``C`` alone — nudging ``D`` too would write to a
    task that is not ready, bumping the ``updated_at`` the #339
    bootstrap-replay guard reads as "a human asked for this again"
    (:func:`nudge_unblocked` spells the hazard out). So each candidate is put
    to Lithos individually (:func:`_is_ready`): readiness is Lithos's answer
    (epic G), and it covers unmet gates and cycles, not just ``blocks``.

    **Progress is per candidate, not all-or-nothing.** A candidate loom cannot
    classify — no ``task_ready`` narrowing settles a task with neither project
    nor tags, and a page can always be saturated — must not strand its
    classifiable siblings, which are exactly the tagged dependents this whole
    path exists to dispatch. So the ready ones are nudged now and written into
    the gate's :class:`RecoveryRecord` (never nudged twice), and only the
    residue keeps the gate open.

    **And the deferral is bounded.** After
    :data:`RECOVERY_UNDETERMINED_MAX_SWEEPS` consecutive sweeps that still
    cannot classify everything, it stops: an unbounded defer means a gate that
    never completes, never posts ``[GateResolved]`` and re-polls GitHub for
    ever. Giving up is announced on the story, naming the ids — the residue is
    then a human's to touch, not something loom silently forgot.
    """
    record = _recovery_record(gate, pr_url)
    candidates = await _blocks_dependents(story_id, ctx)
    if candidates is None:
        return None  # the edge read failed — who to nudge is unknown
    pending = [task_id for task_id in candidates if task_id not in record.nudged]
    if not pending:
        return NudgePlan([])
    released: list[str] = []
    unknown: list[str] = []
    for task_id in pending:
        verdict = await _is_ready(task_id, ctx, limit=limit)
        if verdict is None:
            unknown.append(task_id)
        elif verdict:
            released.append(task_id)
    if not unknown:
        return NudgePlan(
            released, record=record._replace(nudged=(*record.nudged, *released))
        )
    sweeps = record.undetermined + 1
    if sweeps >= RECOVERY_UNDETERMINED_MAX_SWEEPS:
        await _give_up_on(unknown, story_id, gate, pr_url, ctx)
        return NudgePlan(
            released, record=record._replace(nudged=(*record.nudged, *released))
        )
    await _undetermined_dependents(unknown, story_id, gate, pr_url, ctx)
    return NudgePlan(
        released,
        record=record._replace(nudged=(*record.nudged, *released), undetermined=sweeps),
        defer=True,
    )


async def _is_ready(
    task_id: str, ctx: SubscriptionContext, *, limit: int
) -> bool | None:
    """Does Lithos currently offer *task_id* as ready work? ``None`` = it would
    not say — a failed read, or pages too full for absence to mean anything.

    ``task_ready`` has no per-task filter, so this is a membership test; what
    matters is what the page is narrowed to. It is narrowed to **this
    candidate's own** project and tag set, which is both the narrowing
    ``READY_QUERY_LIMIT`` is sized for and the only one that cannot drop the
    candidate itself — a ready task always appears in a page filtered by its
    own attributes. Enumerating the whole instance instead (as this did until
    #351 review r3) made the answer hostage to unrelated ready work: the
    github-issue watcher materialises every unseen open issue on a watched
    **public** repo as an edge-less, therefore immediately-ready task.

    That narrowing is only as selective as the candidate, though — a task with
    neither project nor tags narrows to nothing. So a full ready page falls
    back to the other half of the partition: ready and blocked together cover
    every open work task, so **finding** the candidate among the blocked
    settles it as not-released even when the ready page could not. Only a
    candidate missing from both, with at least one page full, is undetermined.
    """
    try:
        task = await ctx.lithos.task_get(task_id=task_id)
    except LithosClientError as exc:
        ctx.logger.warning(
            "[Friction] pr-gate: reading dependent %s to check its readiness "
            "failed (%s); it stays unclassified this sweep",
            task_id,
            exc,
        )
        return None
    if task is None or task.status != "open":
        # Deleted, or already completed/cancelled by someone else — there is
        # nothing left to dispatch, so this is a definite "do not nudge".
        return False
    project = task.metadata.get("project")
    scope: dict[str, Any] = {
        "project": project if isinstance(project, str) else None,
        "tags": list(task.tags),
        "limit": limit,
    }
    try:
        ready = await ctx.lithos.task_ready(**scope, with_claims=False)
    except LithosClientError as exc:
        ctx.logger.warning(
            "[Friction] pr-gate: reading the ready frontier for dependent %s "
            "failed (%s); it stays unclassified this sweep",
            task_id,
            exc,
        )
        return None
    if any(candidate.id == task_id for candidate in ready):
        return True
    if len(ready) < limit:
        return False  # a complete page — absence is an answer
    try:
        blocked = await ctx.lithos.task_blocked(**scope)
    except LithosClientError as exc:
        ctx.logger.warning(
            "[Friction] pr-gate: reading the blocked frontier for dependent %s "
            "failed (%s); it stays unclassified this sweep",
            task_id,
            exc,
        )
        return None
    if any(entry.task.id == task_id for entry in blocked):
        return False  # settled from the other side of the partition
    ctx.logger.warning(
        "[Friction] pr-gate: dependent %s is on neither the ready nor the "
        "blocked page for its own project/tags, and the ready page hit the "
        "%d-task query limit, so its readiness is undetermined. Raise "
        "READY_QUERY_LIMIT if a frontier this wide is expected.",
        task_id,
        limit,
    )
    return None


async def _undetermined_dependents(
    task_ids: Sequence[str],
    story_id: str,
    gate: Any,
    pr_url: str,
    ctx: SubscriptionContext,
) -> None:
    """Leave a task-level breadcrumb the first time a recovery sweep cannot
    classify a dependent.

    Deferring is the safe answer but a silent one — the gate stays open and
    re-polls GitHub, with nothing on the story to say why. One ``[Friction]``
    on the story (marker on the GATE, scoped to the PR url, so a persistent
    state does not re-post every sweep) puts it where an operator looks.
    Best-effort: a breadcrumb that fails to land never changes the decision.
    """
    if gate.metadata.get(NUDGE_UNDETERMINED_KEY) == pr_url:
        return
    await post_finding_then_mark(
        ctx,
        task_id=story_id,
        summary=(
            f"[Friction] pr-gate: PR {pr_url} merged and story {story_id} is "
            f"already complete, but Lithos would not say whether {_listed(task_ids)} "
            f"ready, so gate {gate.id} is left open and the nudge is retried "
            f"next sweep (up to {RECOVERY_UNDETERMINED_MAX_SWEEPS} sweeps). If "
            f"this persists, check whether that task's project/tag frontier "
            f"exceeds {READY_QUERY_LIMIT} ready tasks."
        ),
        marker={NUDGE_UNDETERMINED_KEY: pr_url},
        subsystem="pr-gate",
        retry_hint="will retry next sweep",
        marker_task_id=gate.id,
    )


async def _give_up_on(
    task_ids: Sequence[str],
    story_id: str,
    gate: Any,
    pr_url: str,
    ctx: SubscriptionContext,
) -> None:
    """Announce the residue the resolver is about to stop waiting on.

    The bound has to end in something an operator can act on, or it just moves
    the silence: this names the exact ids, so the recovery loom could not
    finish is a one-line manual touch rather than a mystery. Posted on the
    story (which is complete — the PR merged, that part is settled) rather
    than raised as a ``human`` gate: a gate blocks an OPEN task and its
    documented action is "complete it to re-dispatch", neither of which means
    anything for a finished story.
    """
    await _post_friction(
        story_id,
        ctx,
        summary=(
            f"[Friction] pr-gate: PR {pr_url} merged and story {story_id} is "
            f"complete, but after {RECOVERY_UNDETERMINED_MAX_SWEEPS} sweeps "
            f"Lithos still would not say whether {_listed(task_ids)} ready, so "
            f"loom gave up waiting and resolved gate {gate.id} on the merge. "
            f"If any of those tasks is ready and tagged for a route, touch it "
            f"(any edit emits the task.updated that dispatches it); a daemon "
            f"restart does the same for all of them."
        ),
    )
    ctx.logger.warning(
        "[Friction] pr-gate: gave up classifying %s after %d sweeps; resolving "
        "gate %s on the merge anyway",
        list(task_ids),
        RECOVERY_UNDETERMINED_MAX_SWEEPS,
        gate.id,
    )


def _listed(task_ids: Sequence[str]) -> str:
    """``"task X is"`` / ``"tasks X, Y are"`` — the findings read either way."""
    joined = ", ".join(task_ids)
    return f"task {joined} is" if len(task_ids) == 1 else f"tasks {joined} are"
