"""The non-delivering exit: raise a needs-human gate (b91177d2).

When a route-runner run ends without delivering — the plugin reports
``failed`` (any story-develop stop), violates the result contract, overruns
its runtime, returns an unknown status, or exhausts its usage-limit resume
budget — the story is a human's problem, not a retry's. This module owns that
exit end to end:

1. raise a loom ``human`` gate on the story (:mod:`lithos_loom.gates`), so it
   is *structurally* off the ready frontier — the primary guard;
2. ONE story write carrying ``needs_human_gate_id`` (provenance) plus the
   failed-attempt marker naming the gate (which then abstains — see
   :mod:`.dispatch_guards`);
3. a ``[NeedsHuman]`` finding on the story: reason, summary, the run facts
   every August rescue needed by hand, the gate id, and the two actions;
4. the push sinks (:mod:`lithos_loom.notifications`), best-effort;
5. release the claim.

The operator completes the gate to re-dispatch (or cancels the *story* to
abandon); :func:`clear_resolved_escalation` runs on the dispatch path and
clears the provenance + marker on every origin, so the loop's correctness
never depends on the live :mod:`.escalation_resolver` nudge. When no gate can
be raised, the exit degrades to the marker-only contract
(:func:`.dispatch_guards.release_with_failure`) with the gate problem folded
in as ``[Friction]`` text — the pre-gate behaviour, as the fallback.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from lithos_loom.gates import (
    ESCALATION_REASONS,
    STORY_HUMAN_GATE_ID_KEY,
    create_human_gate_best_effort,
)
from lithos_loom.notifications import NeedsHumanNotice, notice_github_ref
from lithos_loom.subscriptions.dispatch_guards import (
    AttemptStampStore,
    last_attempt_key,
    record_failed_attempt,
    release_with_failure,
)

__all__ = [
    "Escalation",
    "clear_resolved_escalation",
    "escalate_with_failure",
    "escalation_from_result",
]

logger = logging.getLogger(__name__)


class _EscalationClient(Protocol):
    """What raising a needs-human gate needs: the guard client's calls plus a
    fresh story read and the three gate-writer calls
    (:class:`~lithos_loom.gates.GateWriter`). Standalone (not a subclass of
    ``dispatch_guards._GuardClient``) so no private name crosses modules."""

    async def task_update(
        self,
        *,
        task_id: str,
        agent: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Any: ...

    async def finding_post(
        self, *, task_id: str, summary: str, agent: str | None = None
    ) -> Any: ...

    async def task_release(
        self, *, task_id: str, aspect: str, agent: str | None = None
    ) -> Any: ...

    async def task_ready(
        self,
        *,
        project: str | None = None,
        tags: list[str] | None = None,
        metadata_match: dict[str, Any] | None = None,
        limit: int = 50,
        with_claims: bool = True,
    ) -> Any: ...

    async def task_get(self, *, task_id: str) -> Any: ...

    async def task_create(
        self,
        *,
        title: str,
        agent: str | None = None,
        description: str | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        task_type: str | None = None,
        parent_task_id: str | None = None,
        depends_on: list[str] | None = None,
    ) -> str: ...

    async def task_edge_upsert(
        self,
        *,
        from_task_id: str,
        to_task_id: str,
        type: str,
        agent: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None: ...

    async def task_cancel(self, *, task_id: str, agent: str | None = None) -> Any: ...


# ── needs-human escalation (b91177d2) ─────────────────────────────────


@dataclass(frozen=True)
class Escalation:
    """Why a run ended without delivering, in the gate's shape: a closed-
    vocabulary *reason*, a one-line *summary*, and the *brief* (branch, rounds,
    cost, gate verdict, findings, paths) the gate carries as ``run_brief``."""

    reason: str
    summary: str
    brief: Mapping[str, Any] = field(default_factory=dict)


_ERROR_CATEGORY_REASONS: Mapping[str, str] = {
    "delivery": "delivery",
    "environment": "infra",
}


def escalation_from_result(result: Mapping[str, Any], *, detail: str) -> Escalation:
    """Read a ``failed`` result.json into an :class:`Escalation`.

    The plugin's own ``escalation`` block wins when present and well-formed
    (schema-validated upstream, so a bad reason here means an out-of-band
    write — ignored rather than trusted). Otherwise the runner composes one
    from ``error.category`` / ``error.message`` plus the contract fields it
    always has (``rounds``, ``worktree``), with *detail* as the summary of last
    resort.
    """
    block = result.get("escalation")
    if isinstance(block, Mapping):
        reason = block.get("reason")
        if isinstance(reason, str) and reason in ESCALATION_REASONS:
            summary = block.get("summary")
            brief = block.get("brief")
            return Escalation(
                reason=reason,
                summary=summary if isinstance(summary, str) and summary else detail,
                brief=dict(brief) if isinstance(brief, Mapping) else {},
            )
    err = result.get("error")
    category = err.get("category") if isinstance(err, Mapping) else None
    message = err.get("message") if isinstance(err, Mapping) else None
    brief = {
        key: result[key]
        for key in ("rounds", "worktree")
        if result.get(key) is not None
    }
    return Escalation(
        reason=_ERROR_CATEGORY_REASONS.get(str(category), "failed"),
        summary=message if isinstance(message, str) and message else detail,
        brief=brief,
    )


class _Notifier(Protocol):
    async def needs_human(self, notice: NeedsHumanNotice) -> list[str]: ...


def _needs_human_summary(
    *,
    route: str,
    escalation: Escalation,
    run_id: str | None,
    gate_id: str,
) -> str:
    """The ``[NeedsHuman]`` finding: reason, summary, the run facts every
    August rescue needed, the gate, and the two actions."""
    b = escalation.brief
    facts: list[str] = []
    if run_id:
        facts.append(f"run {run_id}")
    if b.get("rounds") is not None:
        facts.append(f"{b['rounds']} round(s)")
    cost = b.get("cost_usd")
    if isinstance(cost, int | float) and not isinstance(cost, bool):
        facts.append(f"${cost:.2f}")
    if b.get("branch"):
        facts.append(f"branch {b['branch']}")
    if b.get("worktree"):
        facts.append(f"worktree {b['worktree']}")
    facts_part = f"; {', '.join(facts)}" if facts else ""
    return (
        f"[NeedsHuman] route {route}: {escalation.reason} — {escalation.summary}"
        f"{facts_part}; gate {gate_id} — complete it to re-dispatch (edit the "
        "story first if the brief must change), cancel the story to abandon"
    )


async def escalate_with_failure(
    lithos: _EscalationClient,
    *,
    task_id: str,
    route: str,
    agent: str,
    payload: Mapping[str, Any],
    escalation: Escalation,
    run_id: Any = None,
    stamps: AttemptStampStore | None = None,
    notifier: _Notifier | None = None,
    release: bool = True,
) -> str | None:
    """The whole non-delivering exit: raise the needs-human gate, record it on
    the story, tell the operator, release the claim. Returns the gate id, or
    ``None`` when no gate could be raised (the marker-only fallback ran).

    Order: gate (best-effort) → ONE story write carrying ``needs_human_gate_id``
    + the failed-attempt marker naming the gate → ``[NeedsHuman]`` finding →
    push notification → release. If the gate lands but the story write fails,
    NO marker exists — a missing marker fails open into the readiness check,
    which the gate guards; a marker WITHOUT the gate id beside a live gate
    would decline at bootstrap forever once the gate is ticked. If the gate
    cannot be raised at all, this degrades to :func:`release_with_failure`'s
    contract (marker + ``[BlockerFailed]``) with the gate problem folded in as
    ``[Friction]`` text. Every step is best-effort and independently logged.
    """
    run = run_id if isinstance(run_id, str) and run_id else None
    # A fresh snapshot: the plugin's end-of-run `task_update` (develop_* keys,
    # a delivered PR url) post-dates the dispatch payload, and the gate's
    # brief + the mention sink want the current story, not the one dispatched.
    story_title = str(payload.get("title") or task_id)
    story_meta: Mapping[str, Any] = payload.get("metadata") or {}
    try:
        fresh = await lithos.task_get(task_id=task_id)
    except Exception:
        logger.exception(
            "route %s: task_get for %s failed; using the payload", route, task_id
        )
        fresh = None
    if fresh is not None:
        story_title = str(getattr(fresh, "title", None) or story_title)
        fresh_meta = getattr(fresh, "metadata", None)
        if isinstance(fresh_meta, Mapping):
            story_meta = fresh_meta
    project = story_meta.get("project")

    gate_id, gate_problem = await create_human_gate_best_effort(
        lithos,
        story_id=task_id,
        story_title=story_title,
        project=project if isinstance(project, str) else None,
        agent=agent,
        route=route,
        reason=escalation.reason,
        summary=escalation.summary,
        run_id=run,
        brief=escalation.brief,
    )
    if gate_id is None:
        detail = f"{escalation.reason} — {escalation.summary}"
        await release_with_failure(
            lithos,
            task_id=task_id,
            route=route,
            agent=agent,
            detail=f"{detail}; [Friction] {gate_problem}",
            payload=payload,
            run_id=run,
            stamps=stamps,
            release=release,
        )
        return None

    problems: list[str] = []
    recorded = await record_failed_attempt(
        lithos,
        task_id=task_id,
        route=route,
        agent=agent,
        payload=payload,
        run_id=run,
        stamps=stamps,
        gate_id=gate_id,
    )
    if not recorded:
        problems.append(
            "could not record the gate on the story (no marker written; the "
            "gate alone guards re-dispatch, and the resolver nudges on an "
            "absent provenance key, so completing the gate still retries)"
        )
    summary = _needs_human_summary(
        route=route, escalation=escalation, run_id=run, gate_id=gate_id
    )
    logger.info("RouteRunner %s: escalated %s to gate %s", route, task_id, gate_id)

    if notifier is not None:
        notice = NeedsHumanNotice(
            gate_id=gate_id,
            story_id=task_id,
            story_title=story_title,
            project=project if isinstance(project, str) else None,
            route=route,
            reason=escalation.reason,
            summary=escalation.summary,
            run_id=run,
            github_ref=notice_github_ref(dict(story_meta)),
        )
        try:
            problems.extend(await notifier.needs_human(notice))
        except Exception:  # the notifier's own contract is never-raises
            logger.exception("RouteRunner %s: notifier failed for %s", route, task_id)
            problems.append("notifier crashed")
    if problems:
        summary += " [Friction] " + "; ".join(problems)
    try:
        await lithos.finding_post(task_id=task_id, summary=summary, agent=agent)
    except Exception:
        logger.exception("RouteRunner %s: finding_post failed for %s", route, task_id)
    if release:
        try:
            await lithos.task_release(task_id=task_id, aspect=route, agent=agent)
        except Exception:
            logger.exception(
                "RouteRunner %s: task_release failed for %s", route, task_id
            )
    return gate_id


async def clear_resolved_escalation(
    lithos: _EscalationClient,
    *,
    task_id: str,
    route: str,
    agent: str,
    payload: Mapping[str, Any],
    stamps: AttemptStampStore | None = None,
) -> None:
    """Best-effort per-key delete of a story's ``needs_human_gate_id`` and
    ``route``'s failed-attempt marker, iff the dispatch-time *payload* carried
    the gate key (no round trip otherwise).

    Runs on the DISPATCH path, after a successful claim, on every origin: a
    story that is being dispatched has passed the readiness check, so any
    gate it carried is resolved — whether the live resolver nudged it or the
    restart bootstrap re-surfaced it (the route-runner child replays open
    tasks only, so a gate ticked while the daemon was down never reaches the
    resolver). Correctness of the loop therefore never depends on the live
    nudge. Failures are logged and swallowed.
    """
    metadata = payload.get("metadata") or {}
    if not metadata.get(STORY_HUMAN_GATE_ID_KEY):
        return
    key = last_attempt_key(route)
    try:
        await lithos.task_update(
            task_id=task_id,
            agent=agent,
            metadata={STORY_HUMAN_GATE_ID_KEY: None, key: None},
        )
    except Exception:
        logger.exception(
            "route %s: clearing the resolved escalation on %s failed", route, task_id
        )
        return
    if stamps is not None:
        stamps.clear(route, task_id)
