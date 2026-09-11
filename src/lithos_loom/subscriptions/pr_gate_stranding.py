"""A stranded ``pr`` gate becomes a loom ``human`` gate (04c2448b / #268).

A ``pr`` gate watches a delivered PR for its merge. When that PR is instead
**closed unmerged** or **deleted**, the gate has nothing left to watch — but
the story behind it is still open and still needs a decision: was the work
merged some other way (complete the story), should loom re-develop it into a
fresh PR (complete the gate), or is it abandoned (cancel the story)? Until
2026-09-11 the resolver left the ``pr`` gate open with a ``[DeliveredPRClosed]``
finding and nothing else — a correct signal with no surface: gate e8126732
sat **nineteen days** in August while its work had merged via another PR, and
79c0b605 stranded the same way in July. Neither board distinguishes a stranded
``pr`` gate from a healthy one; both distinguish a **human** gate.

So the stranding is converted into the escalation primitive (b91177d2):

1. a loom ``human`` gate is raised on the story — ``escalation_reason``
   ``pr_closed_unmerged`` / ``pr_gone``, the PR url and the superseded ``pr``
   gate in ``run_brief``, the three real choices as its actions — through
   the same :func:`~.escalation.raise_needs_human` core as every other
   in-daemon escalation (gate → record → push sinks → ``[NeedsHuman]``);
2. :data:`SUPERSEDED_BY_KEY` is written on the **``pr`` gate**, in the same
   write as its merge marker and reconciliation state — the idempotency key
   lives on the gate being converted, never on the story: a story-side key
   goes stale on the daemon-down path, and a story that strands a *second*
   ``pr`` gate after a re-develop must convert again;
3. the ``pr`` gate is **completed** — only now, so the story is never
   momentarily unblocked (one blocker, one action, and the ``pr``-gate list
   stops lying).

A story already behind an open loom ``human`` gate (a conflict or remediation
escalation) gets no second one: the ``pr`` gate is superseded by the gate that
exists. When no human gate can be raised the resolver falls back to the old
contract — ``pr`` gate left open, marker + state written — and the next sweep
retries the conversion through the terminal-marker guard, which is also how a
gate stranded before this shipped self-migrates.

Two more pieces of hygiene ride on the same sweep:

* **the reopen poll** — a stranding gate names its PR, so the sweep keeps
  polling it (:func:`human_gate_pr`): reopened *and merged* resolves the
  story-first completion the ``pr`` gate would have done, bounded by the
  human gate's own lifetime (the operator completing or cancelling ends it);
* **waiter-resolved** — every loom ``human`` gate whose waiter is already
  terminal is completed (:func:`complete_if_waiter_resolved`), never nudged:
  the story's own completion released its dependents, and a tidy-up must not
  write to a task a sibling blocker may still hold.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from lithos_loom.errors import LithosClientError
from lithos_loom.gates import (
    ESCALATION_SUMMARY_MAX_CHARS,
    STORY_HUMAN_GATE_ID_KEY,
    WAITS_ON_GATE,
    PrGateSpec,
    is_loom_human_gate,
    parse_human_gate,
    waiter_of,
)
from lithos_loom.github_client import parse_github_ref
from lithos_loom.subscriptions import SubscriptionContext
from lithos_loom.subscriptions._findings import (
    complete_swallowing,
    post_finding_then_mark,
    write_marker,
)
from lithos_loom.subscriptions.escalation import Escalation, raise_needs_human
from lithos_loom.subscriptions.reconciliation_state import (
    STATE_KEY,
    STATE_URL_KEY,
    closed_state_marker,
)
from lithos_loom.subscriptions.remediation_budget import RemediationNotifier

__all__ = [
    "DELIVERED_PR_CLOSED",
    "MERGE_STATE_KEY",
    "MERGE_STATE_URL_KEY",
    "STRANDING_ACTIONS",
    "STRANDING_REASONS",
    "STRANDING_ROUTE",
    "SUPERSEDED_BY_KEY",
    "StoryEscalation",
    "complete_if_waiter_resolved",
    "convert_stranded_gate",
    "human_gate_pr",
    "story_escalation_state",
]

# Stable, machine-parseable finding prefix (see AGENTS.md): a delivered PR
# reached a closed-without-merge end state (closed unmerged, or deleted) — the
# audit trail of the conversion below.
DELIVERED_PR_CLOSED = "[DeliveredPRClosed]"

# Gate-metadata keys carrying the merge-state marker (written on the GATE). The
# marker is SCOPED to the PR url it resolved (MERGE_STATE_URL_KEY): the resolver
# treats a gate as resolved only when its recorded state is terminal AND the
# recorded url still matches the gate's PR url, so a replacement PR re-evaluates.
MERGE_STATE_KEY = "develop_pr_merge_state"
MERGE_STATE_URL_KEY = "develop_pr_merge_url"

SUPERSEDED_BY_KEY = "superseded_by_gate_id"
"""On a ``pr`` gate: the loom ``human`` gate that took over its story. The
conversion's idempotency key — present means the human gate holds the story
and only the ``pr`` gate's completion may still be owed."""

STRANDING_ROUTE = "pr-gate"
"""``metadata.route`` on the human gate: which resolver raised it."""

STRANDING_REASONS: Mapping[str, str] = {
    "closed_unmerged": "pr_closed_unmerged",
    "gone": "pr_gone",
}
"""Merge-marker value → ``escalation_reason`` (both in the closed vocabulary)."""

STRANDING_ACTIONS = (
    "if the work already landed (merged elsewhere, or this PR reopened and "
    "merged) complete the STORY; to re-develop it into a fresh PR complete "
    "this gate; to abandon it cancel the story"
)
"""What the operator can do about a stranded delivery — the gate's brief and
the ``[NeedsHuman]`` finding's actions. Not the runner's two: "complete the
gate" here means re-develop, and "the work landed elsewhere" is a choice the
runner never has."""

_SUBSYSTEM = "pr-gate"


@dataclass(frozen=True)
class StoryEscalation:
    """What Lithos says about the story: its ``status`` (``""`` when it no
    longer exists), the open loom ``human`` gate that **blocks** it — read
    from its incoming ``waits_on_gate`` edges, never from the provenance key
    (``needs_human_gate_id`` is provenance only: it can be stale, absent after
    a partial write, or name a gate that holds a different story) — and its
    metadata (for the brief)."""

    status: str
    open_gate_id: str
    metadata: Mapping[str, Any]
    open_gate: Any = None
    """The gate record behind ``open_gate_id`` (for the reuse annotation)."""


async def story_escalation_state(
    story_id: str, ctx: SubscriptionContext
) -> StoryEscalation | None:
    """Read the story and the loom ``human`` gates that hold it.

    ``None`` when Lithos could not say — "cannot read" is kept distinct from
    "clear": a second human gate must never be raised on a story that may
    already carry one, so the caller defers to the next sweep. A terminal
    story (or one that no longer exists) reads as ``open_gate_id=""`` with
    its status; the caller decides that nothing is left to escalate.
    """
    try:
        story = await ctx.lithos.task_get(task_id=story_id)
        if story is None:
            return StoryEscalation(status="", open_gate_id="", metadata={})
        raw = getattr(story, "metadata", None)
        metadata: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}
        if story.status != "open":
            return StoryEscalation(
                status=story.status, open_gate_id="", metadata=metadata
            )
        edges = await ctx.lithos.task_edge_list(
            task_id=story_id, direction="incoming", types=[WAITS_ON_GATE]
        )
        for edge in edges:
            gate = await ctx.lithos.task_get(task_id=edge.from_task_id)
            if gate is not None and gate.status == "open" and is_loom_human_gate(gate):
                return StoryEscalation(
                    status="open",
                    open_gate_id=gate.id,
                    metadata=metadata,
                    open_gate=gate,
                )
    except (LithosClientError, OSError) as exc:
        ctx.logger.warning(
            "[Friction] %s: could not read story %s's escalation (%s)",
            _SUBSYSTEM,
            story_id,
            exc,
        )
        return None
    return StoryEscalation(status="open", open_gate_id="", metadata=metadata)


async def _raise_stranding_gate(
    *,
    gate: Any,
    story_id: str,
    spec: PrGateSpec,
    merge_state: str,
    gate_marker: Mapping[str, Any],
    story_metadata: Mapping[str, Any],
    notifier: RemediationNotifier | None,
    ctx: SubscriptionContext,
) -> tuple[str | None, str | None]:
    """Raise the loom ``human`` gate that supersedes *gate*.

    Returns ``(human_gate_id, problem)`` like the core it wraps. The record
    hook writes *gate_marker* + :data:`SUPERSEDED_BY_KEY` on the ``pr`` gate
    in one ``task_update`` and ``needs_human_gate_id`` on the story; either
    failing is folded into the ``[NeedsHuman]`` finding as friction — the
    next sweep finds the raised gate on the story's edges, so no second gate.
    """
    what = _describe(merge_state)
    brief: dict[str, Any] = {
        "pr_url": spec.pr_url,
        "pr_state": merge_state,
        "superseded_pr_gate_id": gate.id,
    }
    branch = story_metadata.get("develop_branch")
    if isinstance(branch, str) and branch:
        brief["branch"] = branch
    escalation = Escalation(
        reason=STRANDING_REASONS[merge_state],
        summary=(
            f"delivered PR {spec.pr_url} {what} while the story is still open — "
            f"decide: merged elsewhere, re-develop, or abandon"
        )[:ESCALATION_SUMMARY_MAX_CHARS],
        brief=brief,
    )

    async def _record(human_gate_id: str) -> bool:
        landed = await write_marker(
            ctx,
            task_id=gate.id,
            marker={**gate_marker, SUPERSEDED_BY_KEY: human_gate_id},
            subsystem=_SUBSYSTEM,
        )
        try:
            await ctx.lithos.task_update(
                task_id=story_id,
                agent=ctx.agent_id,
                metadata={STORY_HUMAN_GATE_ID_KEY: human_gate_id},
            )
        except LithosClientError as exc:
            ctx.logger.warning(
                "[Friction] %s: recording gate %s on story %s failed (%s)",
                _SUBSYSTEM,
                human_gate_id,
                story_id,
                exc,
            )
            return False
        return landed

    return await raise_needs_human(
        ctx.lithos,
        task_id=story_id,
        route=STRANDING_ROUTE,
        agent=ctx.agent_id,
        escalation=escalation,
        notifier=notifier,
        actions=STRANDING_ACTIONS,
        record=_record,
        record_problem=(
            "could not record the gate on the pr gate / story — the next sweep "
            "finds it on the story's edges and completes the pr gate"
        ),
    )


def _describe(merge_state: str) -> str:
    return "no longer exists (404)" if merge_state == "gone" else "was closed unmerged"


async def convert_stranded_gate(
    gate: Any,
    story_id: str,
    spec: PrGateSpec,
    merge_state: str,
    notifier: RemediationNotifier | None,
    ctx: SubscriptionContext,
) -> str:
    """Supersede a ``pr`` gate whose PR is *merge_state* (``closed_unmerged``
    / ``gone``) with a loom ``human`` gate on *story_id*, then complete it.

    The gate is never *cancelled* — a cancelled gate is terminal and its story
    would be permanently ``blocker_unsatisfiable``. It is **completed**, and
    only once the human gate holds the story, so the story is never
    momentarily unblocked. In order:

    1. an idempotency check on the ``pr`` gate (:data:`SUPERSEDED_BY_KEY`) —
       present means only the completion is still owed;
    2. the story's own state — a **terminal** story (the #268 shape: the work
       landed via another PR and the issue mirror completed the story before
       the superseded PR was closed) has nothing to decide: marker + complete,
       no gate, no finding, no notification; an open loom human gate that
       already blocks it (read from the edges) is **reused**; an unreadable
       answer defers the whole conversion;
    3. the human gate, recorded on the ``pr`` gate **in the same write** as
       its merge marker + reconciliation state;
    4. ``[DeliveredPRClosed]`` on the story (once — the marker is the de-dup);
    5. the ``pr`` gate's completion.

    No human gate → the pre-change contract: the gate stays open with its
    url-scoped marker + ``needs_human`` state (the finding says why, once),
    and the terminal-marker guard retries the conversion next sweep.
    Returns the sweep's outcome label (*merge_state*), or ``error`` when the
    conversion is deferred to the next sweep.
    """
    reason = _describe(merge_state)
    gate_marker = {
        MERGE_STATE_KEY: merge_state,
        MERGE_STATE_URL_KEY: spec.pr_url,
        **closed_state_marker(spec.pr_url, merge_state),  # PRD S7: one write
    }
    already_marked = (
        gate.metadata.get(MERGE_STATE_KEY) == merge_state
        and gate.metadata.get(MERGE_STATE_URL_KEY) == spec.pr_url
    )
    recorded = gate.metadata.get(SUPERSEDED_BY_KEY)
    human_gate_id = recorded if isinstance(recorded, str) and recorded else None
    if human_gate_id is None:
        escalation = await story_escalation_state(story_id, ctx)
        if escalation is None:
            ctx.logger.warning(
                "[Friction] %s: converting gate %s deferred — story %s's "
                "escalation is unreadable; will retry next sweep",
                _SUBSYSTEM,
                gate.id,
                story_id,
            )
            return "error"
        if escalation.status != "open":
            return await _tidy_finished_story(
                gate, story_id, spec, merge_state, gate_marker, already_marked, ctx
            )
        problem: str | None = None
        if escalation.open_gate_id:
            # A conflict / remediation escalation already holds the story:
            # one blocker is enough — supersede the pr gate by that one. The
            # marker is best-effort here: the human gate already holds the
            # story, so completing the pr gate below is safe either way, and
            # leaving it open while the operator may tick the human gate
            # would ready a story the runner then declines without a re-check.
            human_gate_id = escalation.open_gate_id
            await write_marker(
                ctx,
                task_id=gate.id,
                marker={**gate_marker, SUPERSEDED_BY_KEY: human_gate_id},
                subsystem=_SUBSYSTEM,
            )
            await _annotate_reused_gate(
                escalation.open_gate, gate, spec, merge_state, ctx
            )
        else:
            human_gate_id, problem = await _raise_stranding_gate(
                gate=gate,
                story_id=story_id,
                spec=spec,
                merge_state=merge_state,
                gate_marker=gate_marker,
                story_metadata=escalation.metadata,
                notifier=notifier,
                ctx=ctx,
            )
        if human_gate_id is None:
            await _fall_back(
                gate, story_id, spec, reason, gate_marker, already_marked, problem, ctx
            )
            return merge_state
        if not already_marked:
            await _post_closed(gate, story_id, spec, reason, human_gate_id, ctx)
    # The human gate holds the story; the pr gate has nothing left to watch.
    if not await complete_swallowing(
        ctx, task_id=gate.id, subject=f"gate {gate.id}", subsystem=_SUBSYSTEM
    ):
        return "error"
    ctx.logger.info(
        "%s: gate %s superseded by human gate %s and completed — PR %s %s",
        _SUBSYSTEM,
        gate.id,
        human_gate_id,
        spec.pr_url,
        reason,
    )
    return merge_state


async def _annotate_reused_gate(
    human: Any, gate: Any, spec: PrGateSpec, merge_state: str, ctx: SubscriptionContext
) -> None:
    """Tell the gate the operator reads that its PR died: a conflict /
    remediation gate's brief and summary still say "re-run converge on the
    PR"; the stranding facts are merged into ``run_brief`` and appended to
    ``escalation_summary`` (the reason stays its own). Best-effort — the
    ``[DeliveredPRClosed]`` finding carries the same facts."""
    if human is None:
        return
    md = getattr(human, "metadata", None) or {}
    raw = md.get("run_brief")
    brief = dict(raw) if isinstance(raw, Mapping) else {}
    brief.update(
        {
            "pr_url": spec.pr_url,
            "pr_state": merge_state,
            "superseded_pr_gate_id": gate.id,
        }
    )
    summary = md.get("escalation_summary")
    note = f"delivered PR {spec.pr_url} {_describe(merge_state)} meanwhile"
    summary = (f"{summary} — {note}" if isinstance(summary, str) and summary else note)[
        :ESCALATION_SUMMARY_MAX_CHARS
    ]
    await write_marker(
        ctx,
        task_id=human.id,
        marker={"run_brief": brief, "escalation_summary": summary},
        subsystem=_SUBSYSTEM,
    )


async def _tidy_finished_story(
    gate: Any,
    story_id: str,
    spec: PrGateSpec,
    merge_state: str,
    gate_marker: Mapping[str, Any],
    already_marked: bool,
    ctx: SubscriptionContext,
) -> str:
    """The story is already terminal: nothing to decide, nobody to tell — the
    ``pr`` gate is just noise on every board. Mark it and complete it."""
    if not already_marked and not await write_marker(
        ctx, task_id=gate.id, marker=gate_marker, subsystem=_SUBSYSTEM
    ):
        return "error"
    if not await complete_swallowing(
        ctx, task_id=gate.id, subject=f"gate {gate.id}", subsystem=_SUBSYSTEM
    ):
        return "error"
    ctx.logger.info(
        "%s: gate %s completed — PR %s %s and story %s is already resolved",
        _SUBSYSTEM,
        gate.id,
        spec.pr_url,
        _describe(merge_state),
        story_id,
    )
    return merge_state


async def _fall_back(
    gate: Any,
    story_id: str,
    spec: PrGateSpec,
    reason: str,
    gate_marker: Mapping[str, Any],
    already_marked: bool,
    problem: str | None,
    ctx: SubscriptionContext,
) -> None:
    """No human gate could be raised: the pre-change contract — the ``pr``
    gate stays open with its marker + state, the finding says why (once; the
    marker de-dups), and the terminal-marker guard retries next sweep."""
    if not already_marked:
        await post_finding_then_mark(
            ctx,
            task_id=story_id,
            summary=(
                f"{DELIVERED_PR_CLOSED} pr-gate: delivered PR {spec.pr_url} "
                f"{reason}; story {story_id} left blocked on gate {gate.id} "
                f"for a human — [Friction] no needs-human gate could be "
                f"raised ({problem}); the next sweep retries"
            ),
            marker=gate_marker,
            subsystem=_SUBSYSTEM,
            retry_hint="will retry next sweep",
            marker_task_id=gate.id,
        )
        return
    ctx.logger.warning(
        "[Friction] %s: no needs-human gate could be raised for story %s (%s); "
        "gate %s stays open, retrying next sweep",
        _SUBSYSTEM,
        story_id,
        problem,
        gate.id,
    )
    if (
        gate.metadata.get(STATE_URL_KEY) != spec.pr_url
        or gate.metadata.get(STATE_KEY) != "needs_human"
    ):
        # PRD S7 backfill for a gate stranded before the state existed
        await write_marker(
            ctx, task_id=gate.id, marker=gate_marker, subsystem=_SUBSYSTEM
        )


async def _post_closed(
    gate: Any,
    story_id: str,
    spec: PrGateSpec,
    reason: str,
    human_gate_id: str,
    ctx: SubscriptionContext,
) -> None:
    """The audit trail; the ``[NeedsHuman]`` finding carries the decision."""
    try:
        await ctx.lithos.finding_post(
            task_id=story_id,
            summary=(
                f"{DELIVERED_PR_CLOSED} pr-gate: delivered PR {spec.pr_url} "
                f"{reason}; gate {gate.id} superseded by human gate "
                f"{human_gate_id} and completed — story {story_id} stays "
                f"blocked on that gate: {STRANDING_ACTIONS}"
            ),
        )
    except LithosClientError as exc:
        ctx.logger.warning(
            "[Friction] %s: posting %s for story %s failed (%s)",
            _SUBSYSTEM,
            DELIVERED_PR_CLOSED,
            story_id,
            exc,
        )


def human_gate_pr(gate: Any) -> PrGateSpec | None:
    """The PR a stranding gate was raised for, or ``None`` for any other gate
    (an operator's own human gate, a conflict / remediation escalation — those
    carry a ``pr_url`` too but are not merge-polled — or an unparseable url)."""
    if not is_loom_human_gate(gate):
        return None
    spec = parse_human_gate(gate)
    if spec is None or spec.reason not in STRANDING_REASONS.values():
        return None
    url = spec.brief.get("pr_url")
    if not isinstance(url, str) or not url:
        return None
    ref = parse_github_ref(url)
    if ref is None or ref.kind != "pull":
        return None
    return PrGateSpec(repo=ref.repo, pr_number=ref.number, pr_url=url)


async def complete_if_waiter_resolved(gate: Any, ctx: SubscriptionContext) -> str:
    """Complete a loom ``human`` gate whose waiter is already terminal.

    Returns ``completed`` (done here, or already was), ``open`` (the waiter is
    still open — the gate is live), ``orphan`` (no waiter edge; the ``gates``
    CLI flags it, a human decides), ``waiter-gone`` (the edge dangles — "gone"
    is not "done") or ``error`` (retry next sweep). Never nudges: the
    completion's ``unblocked`` answer is ignored on purpose.
    """
    try:
        waiter_id = await waiter_of(ctx.lithos, gate.id)
        if waiter_id is None:
            return "orphan"
        waiter = await ctx.lithos.task_get(task_id=waiter_id)
    except (LithosClientError, OSError) as exc:
        ctx.logger.warning(
            "[Friction] %s: reading gate %s's waiter failed (%s); will retry "
            "next sweep",
            _SUBSYSTEM,
            gate.id,
            exc,
        )
        return "error"
    if waiter is None:
        return "waiter-gone"
    if waiter.status == "open":
        return "open"
    if not await complete_swallowing(
        ctx,
        task_id=gate.id,
        subject=f"gate {gate.id} (waiter {waiter_id} is {waiter.status})",
        subsystem=_SUBSYSTEM,
    ):
        return "error"
    ctx.logger.info(
        "%s: completed loom human gate %s — its waiter %s is already %s",
        _SUBSYSTEM,
        gate.id,
        waiter_id,
        waiter.status,
    )
    return "completed"
