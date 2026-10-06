"""Brief review at dispatch (604fb936), phase 1: the plugin's half.

With ``develop_brief_review`` on, a story that carries ``metadata.prd`` — a
PRD slice, whose brief was written before the code it builds on — is checked
against the exact commit its coder would start from, before the coder starts.
The story's records (``metadata.brief_review``, :func:`brief_review.plan_review`)
decide what happens:

* **never reviewed** (or the last pass is pending or failed) → a full pass,
  and the story is **held**;
* **approved at this base** → proceed;
* **approved at another base** → a delta pass over the commits between:
  *no change* is recorded and the story proceeds; a *facts-only* recheck is
  appended to the description (optimistic lock) with a ``[BriefReview]``
  finding and the story proceeds with it; *any decision or scope cut* holds
  the story again.

Held means: the record entry is written ``pending`` (or ``failed``, for a
pass that produced no draft — no path dispatches an unreviewed brief), the
story's ``brief_review_hold`` flag reserves its project's admission slot, and
the run ends with a ``brief_review`` escalation whose brief carries the
rendered draft. The runner raises the gate from it (``gates.py`` fences the
draft into the gate's description); completing the gate re-dispatches, and
the dispatch path appends the approved text before this phase runs again
(``subscriptions/brief_review_approval.py``).

A story whose description is mirrored from a GitHub issue is skipped with a
friction: the issue drift sync rewrites the description from the issue body,
so an appended addendum would be erased on its next poll.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from ...errors import LithosClientError
from ...lithos_client import LithosClient
from ...runner import worktree
from .brief_review import (
    HOLD_KEY,
    MODE_DELTA,
    MODE_FULL,
    OUTCOME_AUTO_APPENDED,
    OUTCOME_FAILED,
    OUTCOME_NO_CHANGE,
    OUTCOME_PENDING,
    PLAN_DELTA,
    PLAN_PROCEED,
    RECORD_KEY,
    Addendum,
    BriefInputs,
    BriefReviewResult,
    draft_entry,
    plan_review,
    records_of,
    render_addendum,
    review_brief,
)
from .config import DevelopConfig
from .lithos_io import AGENT_ID, TaskContext, explicit_acceptance_criteria

logger = logging.getLogger(__name__)

__all__ = [
    "AGENT_ID",
    "HELD_STATUS",
    "PhaseOutcome",
    "run_brief_review_phase",
    "run_phase",
]

# The story's ``develop_status`` while it waits at its brief-review gate.
HELD_STATUS = "held_for_brief_review"
# The issue mirror's link on a story (``subscriptions/_github_issue_sync.py``).
_GITHUB_ISSUE_KEY = "github_issue_url"
# The escalation summary's ceiling (the gate truncates past it anyway).
_SUMMARY_MAX = 200

ReviewFn = Callable[..., BriefReviewResult]


@dataclass(frozen=True)
class PhaseOutcome:
    """What the dispatch does next. *proceed*: develop, cut at *start_sha*
    when set (the base the review checked), with *description* as the
    story's text when set (it may now end with an appended recheck). Not
    *proceed*: end the run held, reporting *escalation*."""

    proceed: bool
    start_sha: str | None = None
    description: str | None = None
    escalation: dict[str, Any] | None = None
    frictions: tuple[str, ...] = ()


async def run_phase(
    client: Any,
    config: DevelopConfig,
    ctx: TaskContext,
    *,
    start_sha: str,
    review: ReviewFn = review_brief,
    now: datetime | None = None,
    timeout: int = 1800,
) -> PhaseOutcome:
    """Run the brief-review phase for *ctx*'s story before its coder starts
    at *start_sha* (see the module doc). The caller has already decided the
    phase applies (the knob is on, the story carries ``metadata.prd``, the
    run is not a resume). Raises ``LookupError`` for a story Lithos cannot
    find, and lets a Lithos write failure propagate — the caller reports
    either as the run's failure, never as a dispatch."""
    issue = ctx.metadata.get(_GITHUB_ISSUE_KEY)
    if isinstance(issue, str) and issue:
        return PhaseOutcome(
            proceed=True,
            frictions=(
                "brief review skipped: this story's description is mirrored "
                f"from GitHub issue {issue}, and the issue sync would erase an "
                "appended addendum",
            ),
        )
    story = await client.task_get(task_id=ctx.task_id)
    if story is None:
        raise LookupError(f"Lithos task {ctx.task_id!r} not found")
    metadata: Mapping[str, Any] = story.metadata or {}
    plan, prior_base = plan_review(metadata, start_sha)
    if plan == PLAN_PROCEED:
        return PhaseOutcome(
            proceed=True, start_sha=start_sha, description=story.description or ""
        )
    mode = MODE_DELTA if plan == PLAN_DELTA else MODE_FULL
    result = review(
        config,
        BriefInputs(
            story_id=story.id,
            title=story.title,
            brief=story.description or "",
            prd=_text(metadata.get("prd")),
            prd_sections=_text(metadata.get("prd_sections")),
            written_at=story.created_at.isoformat() if story.created_at else None,
            acceptance_criteria=explicit_acceptance_criteria(metadata),
        ),
        base_sha=start_sha,
        mode=mode,
        prior_base=prior_base,
        timeout=timeout,
    )
    when = now or datetime.now(UTC)
    run_id = config.run_id

    addendum = result.addendum
    if addendum is None:
        await _record(
            client,
            story,
            draft_entry(
                run_id=run_id,
                mode=mode,
                base_sha=start_sha,
                addendum=None,
                drafted_at=when,
                outcome=OUTCOME_FAILED,
                note=result.note,
            ),
            hold=True,
            run_id=run_id,
        )
        brief: dict[str, Any] = {
            "base_sha": start_sha,
            "mode": mode,
            "note": result.note,
            "cost_usd": round(result.cost_usd, 2),
        }
        if prior_base:
            brief["prior_base"] = prior_base
        return PhaseOutcome(
            proceed=False,
            escalation={
                "reason": "brief_review",
                "summary": _clip(f"brief review could not run: {result.note}"),
                "brief": brief,
            },
        )

    rendered = render_addendum(
        addendum, base_sha=start_sha, on=when.date(), mode=mode, prior_base=prior_base
    )
    counts = _counts(addendum)
    if mode == MODE_DELTA and not addendum.items:
        # A recheck that found nothing: the brief stands at the new base — as
        # the write found it (an operator edit during the pass is theirs).
        description = await _record(
            client,
            story,
            _entry(run_id, mode, start_sha, addendum, when, OUTCOME_NO_CHANGE),
            hold=False,
            run_id=run_id,
        )
        return PhaseOutcome(proceed=True, start_sha=start_sha, description=description)
    if mode == MODE_DELTA and not addendum.decisions and not addendum.scope_cuts:
        # Facts describe the code; they change no decision the operator made.
        description = await _record(
            client,
            story,
            _entry(run_id, mode, start_sha, addendum, when, OUTCOME_AUTO_APPENDED),
            hold=False,
            run_id=run_id,
            append=rendered,
        )
        try:
            await client.finding_post(
                task_id=story.id,
                summary=(
                    f"[BriefReview] recheck at {start_sha[:12]} (from "
                    f"{(prior_base or '')[:12]}): {counts}, appended to the "
                    "description without a gate — facts only"
                ),
                agent=AGENT_ID,
            )
        except Exception:  # noqa: BLE001 — the append landed; the note is extra
            logger.exception(
                "brief review: [BriefReview] finding for %s failed", story.id
            )
        return PhaseOutcome(proceed=True, start_sha=start_sha, description=description)

    await _record(
        client,
        story,
        _entry(run_id, mode, start_sha, addendum, when, OUTCOME_PENDING),
        hold=True,
        run_id=run_id,
    )
    brief = {
        "base_sha": start_sha,
        "mode": mode,
        "counts": {
            "scope_cut": len(addendum.scope_cuts),
            "fact": len(addendum.facts),
            "decision": len(addendum.decisions),
        },
        "cost_usd": round(result.cost_usd, 2),
        "addendum": rendered,
    }
    if prior_base:
        brief["prior_base"] = prior_base
    what = "recheck" if mode == MODE_DELTA else "brief review"
    return PhaseOutcome(
        proceed=False,
        escalation={
            "reason": "brief_review",
            "summary": _clip(
                f"{what} at {start_sha[:12]}: {counts} — approve or edit the "
                "draft on this gate"
            ),
            "brief": brief,
        },
    )


def _entry(
    run_id: str,
    mode: str,
    base_sha: str,
    addendum: Addendum,
    when: datetime,
    outcome: str,
) -> dict[str, Any]:
    return draft_entry(
        run_id=run_id,
        mode=mode,
        base_sha=base_sha,
        addendum=addendum,
        drafted_at=when,
        outcome=outcome,
    )


async def _record(
    client: Any,
    story: Any,
    entry: dict[str, Any],
    *,
    hold: bool,
    run_id: str,
    append: str | None = None,
) -> str:
    """Write *entry* onto the story's records — with the hold flag set or
    cleared, and *append* added to the description — in ONE write guarded by
    the story's ``updated_at``. A concurrent edit is re-read and retried
    once (the records and the description are recomputed from the fresh
    read, so the other writer's change survives). Returns the description
    now on the story."""
    for attempt in (1, 2):
        description = story.description or ""
        if append is not None:
            description = f"{description.rstrip()}\n\n{append.strip()}\n"
        metadata: dict[str, Any] = {
            RECORD_KEY: [*records_of(story.metadata or {}), entry],
            HOLD_KEY: True if hold else None,
        }
        if hold:
            metadata["develop_status"] = HELD_STATUS
            metadata["develop_run_id"] = run_id
        try:
            await client.task_update(
                task_id=story.id,
                agent=AGENT_ID,
                metadata=metadata,
                description=description if append is not None else None,
                expected_updated_at=story.updated_at,
            )
            return description
        except LithosClientError as exc:
            if exc.code != "version_conflict" or attempt == 2:
                raise
            fresh = await client.task_get(task_id=story.id)
            if fresh is None:
                raise LookupError(f"Lithos task {story.id!r} vanished") from exc
            story = fresh
    raise AssertionError("unreachable")  # pragma: no cover


def _counts(addendum: Addendum) -> str:
    parts = [
        _n(len(addendum.scope_cuts), "scope cut"),
        _n(len(addendum.facts), "fact"),
        _n(len(addendum.decisions), "decision"),
    ]
    shown = [p for p in parts if not p.startswith("0 ")]
    return ", ".join(shown) if shown else "no change"


def _n(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


def _clip(text: str) -> str:
    return text if len(text) <= _SUMMARY_MAX else text[: _SUMMARY_MAX - 1] + "…"


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


class _PerCallClient:
    """The phase's Lithos calls, each on its own short session. The pass in
    between blocks for minutes; holding one MCP session open across it
    would leave its stream unserviced (the event loop is blocked), so each
    read and write opens, calls and closes — the plugin's usual shape
    (``lithos_io`` / ``daemon_io``)."""

    def __init__(self, url: str) -> None:
        self._url = url

    async def task_get(self, *, task_id: str) -> Any:
        async with LithosClient(self._url, agent_id=AGENT_ID) as client:
            return await client.task_get(task_id=task_id)

    async def task_update(self, **kwargs: Any) -> Any:
        async with LithosClient(self._url, agent_id=AGENT_ID) as client:
            return await client.task_update(**kwargs)

    async def finding_post(self, **kwargs: Any) -> Any:
        async with LithosClient(self._url, agent_id=AGENT_ID) as client:
            return await client.finding_post(**kwargs)


def run_brief_review_phase(
    url: str,
    config: DevelopConfig,
    ctx: TaskContext,
    *,
    timeout: int = 1800,
) -> PhaseOutcome:
    """The daemon's entry: resolve the base the coder would be cut at (the
    same fetch :func:`worktree.create` makes) ONCE, then run the phase
    against it. The caller cuts the coder at the outcome's ``start_sha``, so
    a base that moves after this fetch cannot slip past the review."""
    start_sha = worktree.current_base_ref(config.repo, config.base_branch)
    client = _PerCallClient(url)
    return asyncio.run(
        run_phase(client, config, ctx, start_sha=start_sha, timeout=timeout)
    )
