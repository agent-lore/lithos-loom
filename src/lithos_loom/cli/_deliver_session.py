"""The short-lived Lithos sessions ``develop deliver`` drives.

The Typer command is synchronous and the Lithos client is async, so every
phase that talks to Lithos is one ``asyncio.run`` over one client session:
read the story, do the gate swap, post the finding, take / renew / release the
delivery claim. Kept apart from :mod:`cli._deliver_lithos` (which owns the
story model and the gate rules) so the rules can be read and tested without
the plumbing, and so the plumbing has exactly one place where a transport
failure becomes :class:`~lithos_loom.cli._deliver_lithos.DeliverRefused`.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Sequence
from typing import Any

from lithos_loom.cli._deliver_lithos import (
    DELIVER_ASPECT,
    DELIVER_CLAIM_TTL_MINUTES,
    DeliverRefused,
    GateOutcome,
    StoryState,
    gate_delivery,
    mark_delivery_finding,
    read_story,
)
from lithos_loom.errors import LithosClientError
from lithos_loom.lithos_client import LithosClient

__all__ = [
    "claim_story",
    "post_finding",
    "read_story_sync",
    "release_story",
    "renew_story",
    "run_gate_delivery",
    "run_lithos",
]


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
    url: str,
    agent: str,
    *,
    story: StoryState,
    pr_url: str,
    run_id: str,
    dispatch_routes: Sequence[str],
) -> GateOutcome:
    async with LithosClient(url, agent_id=agent) as client:
        return await gate_delivery(
            client,
            story=story,
            pr_url=pr_url,
            run_id=run_id,
            agent=agent,
            dispatch_routes=dispatch_routes,
        )


async def _post_coro(
    url: str,
    agent: str,
    story_id: str,
    summary: str,
    *,
    pr_url: str,
    run_id: str,
    gated: bool,
    swapped: bool,
    mark: bool,
) -> None:
    async with LithosClient(url, agent_id=agent) as client:
        await client.finding_post(task_id=story_id, summary=summary, agent=agent)
        if mark:
            await mark_delivery_finding(
                client,
                story_id=story_id,
                pr_url=pr_url,
                run_id=run_id,
                gated=gated,
                swapped=swapped,
                agent=agent,
            )


async def _claim_coro(url: str, agent: str, story_id: str, aspect: str) -> bool:
    async with LithosClient(url, agent_id=agent) as client:
        try:
            await client.task_claim(
                task_id=story_id,
                aspect=aspect,
                agent=agent,
                ttl_minutes=DELIVER_CLAIM_TTL_MINUTES,
            )
        except LithosClientError as exc:
            if exc.code == "claim_failed":
                return False
            raise
    return True


async def _renew_coro(url: str, agent: str, story_id: str) -> None:
    async with LithosClient(url, agent_id=agent) as client:
        await client.task_renew(
            task_id=story_id,
            aspect=DELIVER_ASPECT,
            agent=agent,
            ttl_minutes=DELIVER_CLAIM_TTL_MINUTES,
        )


async def _release_coro(url: str, agent: str, story_id: str, aspect: str) -> None:
    async with LithosClient(url, agent_id=agent) as client:
        await client.task_release(task_id=story_id, aspect=aspect, agent=agent)


def read_story_sync(url: str, agent: str, story_id: str) -> StoryState:
    """Step 0: the live story + the gates holding it."""
    return run_lithos(_read_story_coro(url, agent, story_id))


def run_gate_delivery(
    url: str,
    agent: str,
    *,
    story: StoryState,
    pr_url: str,
    run_id: str,
    dispatch_routes: Sequence[str],
) -> GateOutcome:
    """Steps 3 + 4, in one client session."""
    return run_lithos(
        _gate_coro(
            url,
            agent,
            story=story,
            pr_url=pr_url,
            run_id=run_id,
            dispatch_routes=dispatch_routes,
        )
    )


def post_finding(
    url: str,
    agent: str,
    story_id: str,
    summary: str,
    *,
    pr_url: str = "",
    run_id: str = "",
    gated: bool = False,
    swapped: bool = False,
    mark: bool = True,
) -> None:
    """Step 5: post ``[ManualDelivery]``, then mark the story (in that order).

    *mark* is False for a delivery that did NOT finish: the marker is what
    silences later runs, so a partial pass must not write one — the run that
    completes the delivery posts the corrected record and marks it then.
    """
    run_lithos(
        _post_coro(
            url,
            agent,
            story_id,
            summary,
            pr_url=pr_url,
            run_id=run_id,
            gated=gated,
            swapped=swapped,
            mark=mark,
        )
    )


def claim_story(
    url: str, agent: str, story_id: str, *, aspect: str = DELIVER_ASPECT
) -> bool:
    """Claim *aspect* of the story; ``False`` when another agent holds it.

    Two aspects are claimed by this command: its own ``deliver`` lease (two
    deliveries of one story must not interleave), and — under the dispatch
    hold's own identity — each configured route, so no dispatch can start
    while the delivery runs (:class:`~lithos_loom.cli.deliver._DispatchHold`).
    """
    return run_lithos(_claim_coro(url, agent, story_id, aspect))


def renew_story(url: str, agent: str, story_id: str) -> bool:
    """Re-up the ``deliver`` lease before the gate work — the phase that must
    be exclusive — so it never runs on a lease the git / gh phases spent.
    ``False`` when the renewal did not land, and the caller then **skips the
    whole gate phase**: a lease that would not renew may already belong to
    another delivery, and a gate raised under it is the duplicate the claim
    exists to prevent. The PR stands and the story keeps the gate it had — a
    partial a later invocation finishes."""
    try:
        run_lithos(_renew_coro(url, agent, story_id))
    except DeliverRefused:
        return False
    return True


def release_story(
    url: str, agent: str, story_id: str, *, aspect: str = DELIVER_ASPECT
) -> None:
    """Release *aspect*. Best-effort: a lingering claim only expires with its
    TTL — and a released route claim is itself the signal that re-triggers
    the runner's readiness check (``task.released``), which then defers the
    story behind the ``pr`` gate this delivery just raised."""
    with contextlib.suppress(DeliverRefused):
        run_lithos(_release_coro(url, agent, story_id, aspect))
