"""The Lithos half of ``lithos-loom develop deliver`` (see :mod:`cli.deliver`).

Everything the hand delivery reads from and writes to Lithos: the story and the
gates that hold it, the gate swap itself (raise the ``pr`` gate, then complete
the stop's loom ``human`` gate — in that order, which is the whole safety
property), the durable ``[ManualDelivery]`` marker, and the short-lived-client
seams the sync Typer command drives through ``asyncio.run``. Split out so the
command module stays the five steps and their flags; the ordering rationale
lives with the code that enforces it.

Three invariants live here rather than in the command:

* **The gate must watch THIS PR.** An open ``pr`` gate is adopted only when
  its ``pr_url`` is the PR being delivered. A gate watching a *different* PR
  means the story is already behind someone else's delivery — the command
  refuses rather than pointing the maintenance machine at the wrong PR while
  retiring the story's escalation.
* **The story write is repaired, not assumed.** ``record_delivery_on_story``
  runs whenever the LIVE story does not already say what a delivered story
  says — so a first pass that created the gate but lost the metadata write is
  finished by the next run, instead of being skipped because the gate exists.
* **The decision is made on a fresh read.** The command reads the story long
  before it pushes and opens the PR; the gate decision re-reads under the same
  client, so a gate that appeared in between is adopted rather than duplicated.
  (The cross-process guard is the story claim — :func:`claim_story`.)

:class:`DeliverRefused` lives here rather than in the command because both
halves raise it — the git half refuses a diverged branch, this half refuses an
unreadable story — and the command maps it onto one exit code.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from lithos_loom.errors import LithosClientError, LithosLoomError
from lithos_loom.gates import (
    STORY_GATE_ID_KEY,
    STORY_HUMAN_GATE_ID_KEY,
    WAITS_ON_GATE,
    create_pr_gate_best_effort,
    is_loom_human_gate,
    is_pr_gate,
    parse_pr_gate,
)
from lithos_loom.lithos_client import LithosClient
from lithos_loom.subscriptions.delivery_gate import record_delivery_on_story
from lithos_loom.subscriptions.dispatch_guards import LAST_ATTEMPT_KEY_PREFIX

__all__ = [
    "DELIVERY_MARKER_KEY",
    "DELIVER_ASPECT",
    "DeliverRefused",
    "GateOutcome",
    "PrGateRef",
    "StoryState",
    "claim_story",
    "gate_delivery",
    "mark_delivery_finding",
    "post_finding",
    "read_story",
    "read_story_sync",
    "release_story",
    "run_gate_delivery",
]

DELIVERY_MARKER_KEY = "manual_delivery"
"""Gate-metadata key recording that this delivery's ``[ManualDelivery]``
finding was posted: ``{"run_id": …, "pr_url": …}``. Written on the **gate**
(the thing that survives and is re-read), AFTER the finding — the
finding-then-mark ordering the subscriptions use, so a crash in between costs
at most one duplicate finding rather than losing the provenance entirely."""

DELIVER_ASPECT = "deliver"
"""Claim aspect serialising concurrent deliveries of the same story. Two
``deliver`` processes that both read "no pr gate" before either pushed would
otherwise each create one; the claim is the same cross-process primitive the
route-runner uses to win a dispatch race. Its own aspect, so it never contends
with a route's claim."""


class DeliverRefused(LithosLoomError):
    """A precondition failed and nothing was written. Exits ``1``."""


@dataclass(frozen=True)
class PrGateRef:
    """An open ``pr`` gate holding the story, and what it watches."""

    gate_id: str
    pr_url: str
    """The PR this gate watches (``""`` when its metadata is unparseable — a
    malformed gate is never treated as watching ours)."""
    marker: Mapping[str, Any] = field(default_factory=dict)
    """:data:`DELIVERY_MARKER_KEY` as read off the gate."""

    def marks(self, *, pr_url: str, run_id: str) -> bool:
        """Whether this gate already records a ``[ManualDelivery]`` finding for
        this exact delivery."""
        return (
            self.marker.get("pr_url") == pr_url
            and str(self.marker.get("run_id") or "") == run_id
        )


@dataclass(frozen=True)
class StoryState:
    """The live story, and the gates that hold it."""

    story_id: str
    title: str
    description: str
    status: str
    metadata: Mapping[str, Any]
    human_gate_ids: tuple[str, ...]
    pr_gates: tuple[PrGateRef, ...] = ()
    """Open ``pr`` gates already blocking the story — a previous ``deliver``,
    or the daemon's own delivery. Their ``pr_url`` is what decides whether one
    is ours to adopt."""

    @property
    def project(self) -> str | None:
        slug = self.metadata.get("project")
        return slug if isinstance(slug, str) and slug else None

    @property
    def acceptance_criteria(self) -> str | None:
        ac = self.metadata.get("acceptance_criteria")
        return ac if isinstance(ac, str) and ac.strip() else None

    @property
    def github_issue_url(self) -> str | None:
        url = self.metadata.get("github_issue_url")
        return url if isinstance(url, str) and url else None

    @property
    def attempt_routes(self) -> tuple[str, ...]:
        """Routes whose failed-attempt marker this delivery supersedes — read
        off the story's own keys, since a stopped run records no route name on
        disk."""
        return tuple(
            sorted(
                key[len(LAST_ATTEMPT_KEY_PREFIX) :]
                for key in self.metadata
                if key.startswith(LAST_ATTEMPT_KEY_PREFIX)
            )
        )

    @property
    def task_text(self) -> str:
        """Title + body, the shape story-develop hands the coder (and the PR)."""
        body = self.description.strip()
        return f"{self.title}\n\n{body}" if body else self.title

    def delivery_recorded(self, gate_id: str) -> bool:
        """Whether the story already carries everything a delivered story
        carries: this gate's id, and neither retirement left behind."""
        return (
            self.metadata.get(STORY_GATE_ID_KEY) == gate_id
            and self.metadata.get(STORY_HUMAN_GATE_ID_KEY) is None
            and not self.attempt_routes
        )


async def read_story(client: Any, story_id: str) -> StoryState:
    """Read the story plus the open gates blocking it.

    The gates come from the story's incoming ``waits_on_gate`` **edges**, never
    from ``needs_human_gate_id``: that key is provenance only — stale after a
    partial write, and it names one gate where a story may carry several.
    """
    story = await client.task_get(task_id=story_id)
    if story is None:
        raise DeliverRefused(f"Lithos task {story_id!r} not found")
    raw = getattr(story, "metadata", None)
    metadata: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}
    human_gates: list[str] = []
    pr_gates: list[PrGateRef] = []
    edges = await client.task_edge_list(
        task_id=story_id, direction="incoming", types=[WAITS_ON_GATE]
    )
    for edge in edges:
        gate = await client.task_get(task_id=edge.from_task_id)
        if gate is None or gate.status != "open":
            continue
        if is_loom_human_gate(gate):
            human_gates.append(gate.id)
        elif is_pr_gate(gate):
            spec = parse_pr_gate(gate)
            marker = (gate.metadata or {}).get(DELIVERY_MARKER_KEY)
            pr_gates.append(
                PrGateRef(
                    gate_id=gate.id,
                    pr_url=spec.pr_url if spec is not None else "",
                    marker=marker if isinstance(marker, Mapping) else {},
                )
            )
    return StoryState(
        story_id=story_id,
        title=str(getattr(story, "title", "") or story_id),
        description=str(getattr(story, "description", "") or ""),
        status=str(getattr(story, "status", "") or ""),
        metadata=metadata,
        human_gate_ids=tuple(human_gates),
        pr_gates=tuple(pr_gates),
    )


@dataclass
class GateOutcome:
    """What the Lithos half of the delivery managed to do."""

    pr_gate_id: str | None = None
    gate_created: bool = False
    """False when the story already had an open ``pr`` gate for THIS PR — a
    re-run adopts it rather than stacking a second blocker."""
    story_recorded: bool = False
    """This run wrote ``pr_gate_id`` + the retirements (a first delivery, or a
    repair of a partial one). False when the live story already said it."""
    finding_marked: bool = False
    """The adopted gate already records this delivery's ``[ManualDelivery]``
    finding, so it must not be posted twice."""
    human_gates_completed: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


async def gate_delivery(
    client: Any,
    *,
    story: StoryState,
    pr_url: str,
    run_id: str,
    agent: str,
) -> GateOutcome:
    """Steps 3 + 4: raise (or adopt) the ``pr`` gate, then retire the stop's
    human gates.

    Ordering is load-bearing: the ``pr`` gate must hold the story **before**
    any human gate is completed, or the story is momentarily on the ready
    frontier and a live runner could claim it into a second, duplicate run.
    So every path that ends without a gate for *pr_url* leaves every human
    gate open — the story stays blocked by the gate it already had, which is
    the safe direction.
    """
    outcome = GateOutcome()
    # Re-read under THIS client: the caller's snapshot predates the push and
    # the PR open, so a gate raised in between (a concurrent deliver, the
    # daemon) must be seen — adopting beats duplicating.
    live = await read_story(client, story.story_id)
    ours = [gate for gate in live.pr_gates if gate.pr_url == pr_url]
    foreign = [gate for gate in live.pr_gates if gate.pr_url != pr_url]

    if ours:
        outcome.pr_gate_id = ours[0].gate_id
        outcome.finding_marked = ours[0].marks(pr_url=pr_url, run_id=run_id)
        if len(ours) > 1:
            outcome.problems.append(
                "the story carries more than one open pr gate for this PR "
                f"({', '.join(g.gate_id for g in ours)}); complete the stale one"
            )
    elif foreign:
        # The story is already behind a delivery of a DIFFERENT PR. Adopting
        # that gate would point merge tracking, review ingestion and the
        # story's completion at a PR that does not contain this branch —
        # while retiring the escalation that says so. Refuse instead.
        watched = ", ".join(
            f"{g.gate_id} → {g.pr_url or '(unparseable)'}" for g in ours + foreign
        )
        outcome.problems.append(
            f"an open pr gate already holds this story, but it watches a "
            f"different PR ({watched}) — {pr_url} was NOT gated and no "
            "needs-human gate was completed. Resolve the existing delivery "
            "first (merge or close its PR, or complete its gate)"
        )
        return outcome
    else:
        gate_id, problem = await create_pr_gate_best_effort(
            client,
            story_id=live.story_id,
            story_title=live.title,
            pr_url=pr_url,
            project=live.project,
            agent=agent,
        )
        if problem is not None:
            outcome.problems.append(problem)
        if gate_id is None:
            # Nothing structurally holds the story, so its needs-human gate
            # stays: completing it now would put the story back on the ready
            # frontier and buy a duplicate PR.
            return outcome
        outcome.pr_gate_id = gate_id
        outcome.gate_created = True

    # The one story write — made whenever the LIVE story does not already say
    # what a delivered story says. An adopted gate whose first pass lost this
    # write is repaired here; a story that already carries it is untouched.
    if not live.delivery_recorded(outcome.pr_gate_id):
        if await record_delivery_on_story(
            client,
            task_id=live.story_id,
            agent=agent,
            gate_id=outcome.pr_gate_id,
            routes=live.attempt_routes,
        ):
            outcome.story_recorded = True
        else:
            outcome.problems.append(
                "could not record pr_gate_id on the story, but the pr gate "
                "already blocks re-dispatch"
            )

    for human_gate_id in live.human_gate_ids:
        try:
            await client.task_complete(task_id=human_gate_id, agent=agent)
        except (LithosClientError, OSError) as exc:
            outcome.problems.append(
                f"could not complete the needs-human gate {human_gate_id} "
                f"({exc}); the story now carries two blockers — complete it by "
                "hand (the pr gate holds the story, so this will not re-dispatch)"
            )
        else:
            outcome.human_gates_completed.append(human_gate_id)
    return outcome


async def mark_delivery_finding(
    client: Any, *, gate_id: str, pr_url: str, run_id: str, agent: str
) -> None:
    """Record on the ``pr`` gate that this delivery's finding was posted.

    Finding-then-mark: the marker is what makes ``[ManualDelivery]`` one-shot,
    and writing it only after the post means a crash in between re-posts next
    run rather than losing the provenance.
    """
    await client.task_update(
        task_id=gate_id,
        agent=agent,
        metadata={DELIVERY_MARKER_KEY: {"run_id": run_id, "pr_url": pr_url}},
    )


def run_lithos(coro: Any) -> Any:
    """Run one Lithos phase, mapping transport failures onto the refusal."""
    try:
        return asyncio.run(coro)
    except (LithosClientError, OSError, ExceptionGroup) as exc:
        # LithosClient.__aenter__ surfaces a connect failure as a plain OSError
        # or, inside a task group, an ExceptionGroup wrapping it (the `gates`
        # command's rationale). ExceptionGroup, not BaseExceptionGroup, so
        # KeyboardInterrupt / SystemExit still propagate.
        raise DeliverRefused(f"Lithos call failed: {exc}") from exc


async def _read_story_coro(url: str, agent: str, story_id: str) -> StoryState:
    async with LithosClient(url, agent_id=agent) as client:
        return await read_story(client, story_id)


async def _gate_coro(
    url: str, agent: str, *, story: StoryState, pr_url: str, run_id: str
) -> GateOutcome:
    async with LithosClient(url, agent_id=agent) as client:
        return await gate_delivery(
            client, story=story, pr_url=pr_url, run_id=run_id, agent=agent
        )


async def _post_coro(
    url: str,
    agent: str,
    story_id: str,
    summary: str,
    *,
    gate_id: str | None,
    pr_url: str,
    run_id: str,
) -> None:
    async with LithosClient(url, agent_id=agent) as client:
        await client.finding_post(task_id=story_id, summary=summary, agent=agent)
        if gate_id is not None:
            await mark_delivery_finding(
                client, gate_id=gate_id, pr_url=pr_url, run_id=run_id, agent=agent
            )


async def _claim_coro(url: str, agent: str, story_id: str) -> bool:
    async with LithosClient(url, agent_id=agent) as client:
        try:
            await client.task_claim(
                task_id=story_id, aspect=DELIVER_ASPECT, agent=agent, ttl_minutes=15
            )
        except LithosClientError as exc:
            if exc.code == "claim_failed":
                return False
            raise
    return True


async def _release_coro(url: str, agent: str, story_id: str) -> None:
    async with LithosClient(url, agent_id=agent) as client:
        await client.task_release(task_id=story_id, aspect=DELIVER_ASPECT, agent=agent)


def read_story_sync(url: str, agent: str, story_id: str) -> StoryState:
    """Step 0: the live story + the gates holding it."""
    return run_lithos(_read_story_coro(url, agent, story_id))


def run_gate_delivery(
    url: str, agent: str, *, story: StoryState, pr_url: str, run_id: str
) -> GateOutcome:
    """Steps 3 + 4, in one client session."""
    return run_lithos(_gate_coro(url, agent, story=story, pr_url=pr_url, run_id=run_id))


def post_finding(
    url: str,
    agent: str,
    story_id: str,
    summary: str,
    *,
    gate_id: str | None = None,
    pr_url: str = "",
    run_id: str = "",
) -> None:
    """Step 5: post ``[ManualDelivery]``, then mark the gate (in that order)."""
    run_lithos(
        _post_coro(
            url, agent, story_id, summary, gate_id=gate_id, pr_url=pr_url, run_id=run_id
        )
    )


def claim_story(url: str, agent: str, story_id: str) -> bool:
    """Take the ``deliver`` claim on the story; ``False`` when another process
    holds it (two deliveries of one story must not interleave)."""
    return run_lithos(_claim_coro(url, agent, story_id))


def release_story(url: str, agent: str, story_id: str) -> None:
    """Release the ``deliver`` claim. Best-effort: a lingering claim only
    expires with its short TTL."""
    with contextlib.suppress(DeliverRefused):
        run_lithos(_release_coro(url, agent, story_id))
