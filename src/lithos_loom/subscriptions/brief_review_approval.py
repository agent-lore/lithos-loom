"""Brief review at dispatch (604fb936): the approval, on the dispatch path.

A story held for its brief review waits behind a loom ``human`` gate with
reason ``brief_review`` whose description fences the draft addendum
(:data:`~lithos_loom.gates.BRIEF_REVIEW_FENCE_OPEN` /
:data:`~lithos_loom.gates.BRIEF_REVIEW_FENCE_CLOSE`). The operator edits the
draft if they want to and completes the gate. Readiness flips the moment it
completes, so the approval is applied HERE — where every dispatch passes,
after the claim and before ``task.json`` is written — never by a second
completion subscriber racing the resolver's nudge, and never only on the
nudge (a gate ticked while the daemon was down arrives via bootstrap).

Applying it is ONE optimistic-lock write to the story:

* the fenced text appended to the description **verbatim** (nothing when the
  operator emptied it);
* the pending record entry settled: ``approved``, ``approved_at``, the gate,
  per-item outcomes (unchanged / edited / cut / added — what phase 2 decides
  from), and the gate's completion ``outcome`` as ``operator_note``;
* the ``brief_review_hold`` flag dropped — the dispatch now holds the slot
  as an in-flight run.

The payload is then re-read, so ``task.json`` carries the addendum.

It fails CLOSED. A completed gate whose fences were edited away, or a story
that keeps changing under the write, yields a refusal the runner raises as a
fresh ``brief_review`` gate (the operator's text inside new fences, to trim)
instead of a dispatch from an unapproved or half-read brief. A gate still
open, cancelled, or belonging to another pass is not an approval at all:
nothing is written, and the plugin's phase reviews the brief again.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from ..errors import LithosClientError
from ..gates import (
    BRIEF_REVIEW_REASON,
    GATE_TYPE_HUMAN,
    RAISED_BY_LOOM,
    WAITS_ON_GATE,
    brief_review_block,
)
from ..plugins.story_develop.brief_review import (
    HOLD_KEY,
    OUTCOME_APPROVED,
    OUTCOME_PENDING,
    RECORD_KEY,
    item_outcomes,
    latest_entry,
    parse_addendum,
    records_of,
)
from .dispatch_guards import task_payload
from .escalation import Escalation

logger = logging.getLogger(__name__)

__all__ = ["ApprovalOutcome", "apply_brief_approval"]


@dataclass(frozen=True)
class ApprovalOutcome:
    """*applied*: the approved text is on the story and *payload* is its
    fresh read, to dispatch with. *refused*: the approval could not be
    applied — escalate it, do not run. Neither: no approval to apply."""

    applied: bool = False
    payload: Mapping[str, Any] | None = None
    refused: Escalation | None = None
    run_id: str | None = None


async def apply_brief_approval(
    lithos: Any,
    *,
    task_id: str,
    route: str,
    agent: str,
    payload: Mapping[str, Any],
    now: datetime | None = None,
) -> ApprovalOutcome:
    """Apply the story's approved brief-review addendum, if it has one (see
    the module doc). *payload* is the dispatch payload; only its metadata
    is read, to skip every round trip for a story with no pending review."""
    entry = latest_entry(payload.get("metadata") or {})
    if entry is None or entry.get("outcome") != OUTCOME_PENDING:
        return ApprovalOutcome()
    run_id = entry.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        return ApprovalOutcome()
    skip = {g for g in entry.get("superseded_gates") or () if isinstance(g, str)}
    gate = await _approved_gate(lithos, task_id, run_id, skip)
    if gate is None:
        return ApprovalOutcome(run_id=run_id)
    block = brief_review_block(gate.description)
    if block is None:
        await _supersede(lithos, task_id, run_id, gate.id, agent=agent, route=route)
        return ApprovalOutcome(
            run_id=run_id,
            refused=_refusal(
                f"the approval on gate {gate.id} could not be applied: the "
                "draft's `####` headings were edited away, so which text is the "
                "addendum is unknown",
                entry,
                gate.description or "",
            ),
        )
    approved = parse_addendum(block)
    when = (now or datetime.now(UTC)).isoformat()
    for attempt in (1, 2):
        story = await lithos.task_get(task_id=task_id)
        if story is None:
            return ApprovalOutcome(run_id=run_id)
        entries = records_of(story.metadata or {})
        current = entries[-1] if entries else None
        if (
            current is None
            or current.get("run_id") != run_id
            or current.get("outcome") != OUTCOME_PENDING
        ):
            # settled by another dispatch, or re-drafted: not ours to apply
            return ApprovalOutcome(run_id=run_id)
        settled = {
            **current,
            "outcome": OUTCOME_APPROVED,
            "approved_at": when,
            "gate_id": gate.id,
            "outcomes": item_outcomes(current, approved),
        }
        if gate.outcome:
            settled["operator_note"] = gate.outcome
        description = story.description or ""
        if block:
            description = f"{description.rstrip()}\n\n{block}\n"
        try:
            await lithos.task_update(
                task_id=task_id,
                agent=agent,
                description=description if block else None,
                metadata={RECORD_KEY: [*entries[:-1], settled], HOLD_KEY: None},
                expected_updated_at=story.updated_at,
            )
        except LithosClientError as exc:
            if exc.code == "version_conflict" and attempt == 1:
                continue
            logger.warning(
                "route %s: could not append %s's approved addendum: %s",
                route,
                task_id,
                exc,
            )
            await _supersede(lithos, task_id, run_id, gate.id, agent=agent, route=route)
            return ApprovalOutcome(
                run_id=run_id,
                refused=_refusal(
                    f"the approval on gate {gate.id} could not be appended "
                    f"({exc.code}: the story kept changing under the write); "
                    "completing this gate retries it",
                    entry,
                    gate.description or "",
                    keep=block,
                ),
            )
        fresh = await lithos.task_get(task_id=task_id)
        logger.info(
            "route %s: appended the approved brief review of %s (gate %s, run %s)",
            route,
            task_id,
            gate.id,
            run_id,
        )
        return ApprovalOutcome(
            applied=True,
            payload=task_payload(fresh if fresh is not None else story),
            run_id=run_id,
        )
    raise AssertionError("unreachable")  # pragma: no cover


async def _approved_gate(
    lithos: Any, story_id: str, run_id: str, skip: set[str]
) -> Any | None:
    """The COMPLETED brief-review gate *run_id* raised on the story, read
    from the story's incoming ``waits_on_gate`` edges (the authority — never
    the ``*_gate_id`` provenance keys). Gates in *skip* — ones an earlier
    refusal superseded, which stay completed on the story because a gate is
    never cancelled — are not candidates. ``None`` while a candidate is
    still open (the approval has not been given), and when none completed
    (cancelled, or never raised)."""
    edges = await lithos.task_edge_list(
        task_id=story_id, direction="incoming", types=[WAITS_ON_GATE]
    )
    completed: Any | None = None
    for edge in edges:
        if edge.from_task_id in skip:
            continue
        gate = await lithos.task_get(task_id=edge.from_task_id)
        if gate is None:
            continue
        meta = gate.metadata or {}
        if not (
            meta.get("gate_type") == GATE_TYPE_HUMAN
            and meta.get("raised_by") == RAISED_BY_LOOM
            and meta.get("escalation_reason") == BRIEF_REVIEW_REASON
            and meta.get("run_id") == run_id
        ):
            continue
        if gate.status == "open":
            return None
        if gate.status == "completed":
            completed = gate
    return completed


async def _supersede(
    lithos: Any, story_id: str, run_id: str, gate_id: str, *, agent: str, route: str
) -> None:
    """Record *gate_id* on the pending entry as superseded by the refusal's
    fresh gate, so the next dispatch does not find it again. Best-effort: a
    failure is logged (the fresh gate still holds the story)."""
    try:
        story = await lithos.task_get(task_id=story_id)
        entries = records_of((story.metadata if story else None) or {})
        if not entries or entries[-1].get("run_id") != run_id:
            return
        current = entries[-1]
        superseded = [*(current.get("superseded_gates") or ()), gate_id]
        await lithos.task_update(
            task_id=story_id,
            agent=agent,
            metadata={
                RECORD_KEY: [*entries[:-1], {**current, "superseded_gates": superseded}]
            },
        )
    except Exception:  # noqa: BLE001 — best-effort; logged
        logger.exception(
            "route %s: could not record gate %s as superseded on %s",
            route,
            gate_id,
            story_id,
        )


def _refusal(
    summary: str,
    entry: Mapping[str, Any],
    old_description: str,
    *,
    keep: str | None = None,
) -> Escalation:
    """A fresh brief-review gate for an approval that could not be applied:
    the operator's text comes back inside new fences, to trim and approve."""
    return Escalation(
        reason=BRIEF_REVIEW_REASON,
        summary=summary,
        brief={
            "base_sha": entry.get("base_sha") or "",
            "mode": entry.get("mode") or "full",
            "addendum": keep if keep is not None else old_description,
        },
    )
