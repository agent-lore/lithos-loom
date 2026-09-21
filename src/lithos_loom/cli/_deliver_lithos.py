"""The Lithos half of ``lithos-loom develop deliver`` (see :mod:`cli.deliver`).

Everything the hand delivery reads from and writes to Lithos: the story and the
gates that hold it, the gate swap itself (raise the ``pr`` gate, then complete
the stop's loom ``human`` gate — in that order, which is the whole safety
property), and the short-lived-client seams the sync Typer command drives
through ``asyncio.run``. Split out so the command module stays the five steps
and their flags; the ordering rationale lives with the code that enforces it.

:class:`DeliverRefused` lives here rather than in the command because both
halves raise it — the git half refuses a diverged branch, this half refuses an
unreadable story — and the command maps it onto one exit code.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from lithos_loom.errors import LithosClientError, LithosLoomError
from lithos_loom.gates import (
    WAITS_ON_GATE,
    create_pr_gate_best_effort,
    is_loom_human_gate,
    is_pr_gate,
)
from lithos_loom.lithos_client import LithosClient
from lithos_loom.subscriptions.delivery_gate import record_delivery_on_story
from lithos_loom.subscriptions.dispatch_guards import LAST_ATTEMPT_KEY_PREFIX

__all__ = [
    "DeliverRefused",
    "GateOutcome",
    "StoryState",
    "gate_delivery",
    "post_finding",
    "read_story",
    "read_story_sync",
    "run_gate_delivery",
]


class DeliverRefused(LithosLoomError):
    """A precondition failed and nothing was written. Exits ``1``."""


@dataclass(frozen=True)
class StoryState:
    """The live story, and the loom ``human`` gates that hold it."""

    story_id: str
    title: str
    description: str
    status: str
    metadata: Mapping[str, Any]
    human_gate_ids: tuple[str, ...]
    pr_gate_ids: tuple[str, ...] = ()
    """Open ``pr`` gates already blocking the story — a previous
    ``deliver`` (or the daemon's own delivery). Its presence is what makes a
    second invocation a no-op instead of a second gate."""

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


async def read_story(client: Any, story_id: str) -> StoryState:
    """Read the story plus the open loom ``human`` gates blocking it.

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
    pr_gates: list[str] = []
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
            pr_gates.append(gate.id)
    return StoryState(
        story_id=story_id,
        title=str(getattr(story, "title", "") or story_id),
        description=str(getattr(story, "description", "") or ""),
        status=str(getattr(story, "status", "") or ""),
        metadata=metadata,
        human_gate_ids=tuple(human_gates),
        pr_gate_ids=tuple(pr_gates),
    )


@dataclass
class GateOutcome:
    """What the Lithos half of the delivery managed to do."""

    pr_gate_id: str | None = None
    gate_created: bool = False
    """False when the story already had an open ``pr`` gate — a re-run adopts
    it rather than stacking a second blocker on the same story."""
    human_gates_completed: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


async def gate_delivery(
    client: Any,
    *,
    story: StoryState,
    pr_url: str,
    agent: str,
) -> GateOutcome:
    """Steps 3 + 4: raise the ``pr`` gate, then retire the stop's human gates.

    Ordering is load-bearing: the ``pr`` gate must hold the story **before**
    any human gate is completed, or the story is momentarily on the ready
    frontier and a live runner could claim it into a second, duplicate run.
    So a failed ``pr`` gate leaves every human gate open — the story stays
    blocked by the gate it already had, which is the safe direction.
    """
    outcome = GateOutcome()
    if story.pr_gate_ids:
        # Already delivered: adopt the gate that holds the story rather than
        # stacking a second one (the branch's PR was adopted too). The human
        # gates below are still swept — a half-finished first run may have
        # left one open.
        outcome.pr_gate_id = story.pr_gate_ids[0]
        if len(story.pr_gate_ids) > 1:
            outcome.problems.append(
                "the story carries more than one open pr gate "
                f"({', '.join(story.pr_gate_ids)}); complete the stale one"
            )
    else:
        gate_id, problem = await create_pr_gate_best_effort(
            client,
            story_id=story.story_id,
            story_title=story.title,
            pr_url=pr_url,
            project=story.project,
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
        if not await record_delivery_on_story(
            client,
            task_id=story.story_id,
            agent=agent,
            gate_id=gate_id,
            routes=story.attempt_routes,
        ):
            outcome.problems.append(
                "could not record pr_gate_id on the story, but the pr gate "
                "already blocks re-dispatch"
            )
    for human_gate_id in story.human_gate_ids:
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
    url: str, agent: str, *, story: StoryState, pr_url: str
) -> GateOutcome:
    async with LithosClient(url, agent_id=agent) as client:
        return await gate_delivery(client, story=story, pr_url=pr_url, agent=agent)


async def _post_coro(url: str, agent: str, story_id: str, summary: str) -> None:
    async with LithosClient(url, agent_id=agent) as client:
        await client.finding_post(task_id=story_id, summary=summary, agent=agent)


def read_story_sync(url: str, agent: str, story_id: str) -> StoryState:
    """Step 0: the live story + the gates holding it."""
    return run_lithos(_read_story_coro(url, agent, story_id))


def run_gate_delivery(
    url: str, agent: str, *, story: StoryState, pr_url: str
) -> GateOutcome:
    """Steps 3 + 4, in one client session."""
    return run_lithos(_gate_coro(url, agent, story=story, pr_url=pr_url))


def post_finding(url: str, agent: str, story_id: str, summary: str) -> None:
    """Step 5."""
    run_lithos(_post_coro(url, agent, story_id, summary))
