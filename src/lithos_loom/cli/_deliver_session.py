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
from dataclasses import dataclass, field
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
    "Claim",
    "DispatchHold",
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


async def _claim_coro(
    url: str, agent: str, story_id: str, aspect: str, ttl_minutes: int
) -> bool:
    async with LithosClient(url, agent_id=agent) as client:
        try:
            await client.task_claim(
                task_id=story_id,
                aspect=aspect,
                agent=agent,
                ttl_minutes=ttl_minutes,
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
    url: str,
    agent: str,
    story_id: str,
    *,
    aspect: str = DELIVER_ASPECT,
    ttl_minutes: int = DELIVER_CLAIM_TTL_MINUTES,
) -> bool:
    """Claim *aspect* of the story; ``False`` when another agent holds it.

    Two aspects are claimed by this command: its own ``deliver`` lease (two
    deliveries of one story must not interleave), and — under the dispatch
    hold's own identity — each configured route, so no dispatch can start
    while the delivery runs (:class:`DispatchHold`). *ttl_minutes* is the
    lease's length: the short default, or the chain's long one
    (:data:`~lithos_loom.cli._deliver_lithos.DELIVER_CHAIN_CLAIM_TTL_MINUTES`).
    """
    return run_lithos(_claim_coro(url, agent, story_id, aspect, ttl_minutes))


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


# ── the two exclusions a delivery holds ────────────────────────────────


@dataclass
class DispatchHold:
    """The story's route claims, held for the length of one delivery.

    The exclusion against a **dispatch**, as opposed to :class:`Claim`'s
    exclusion against another delivery. Every configured dispatch route is
    claimed before the push and released after the gate work, so a daemon
    that boots (or bootstraps this story) mid-delivery cannot take the story
    into a fresh run behind our back: the route-runner claims the same aspect
    before it dispatches, finds it held by another agent, logs the lost race
    and defers. The identity is :func:`dispatch_hold_agent`'s, not the host's
    — a claim only excludes another *agent*.

    Taken route by route and released the same way, so a refusal leaves
    nothing behind (the caller releases in its ``finally``).
    """

    url: str
    agent: str
    story_id: str
    routes: Sequence[str]
    ttl_minutes: int = DELIVER_CLAIM_TTL_MINUTES
    held: list[str] = field(default_factory=list)

    def take(self) -> str | None:
        """Claim every route. Returns the first route already held by someone
        else (nothing was written), or ``None`` when the story is ours."""
        for route in self.routes:
            if not claim_story(
                self.url,
                self.agent,
                self.story_id,
                aspect=route,
                ttl_minutes=self.ttl_minutes,
            ):
                return route
            self.held.append(route)
        return None

    def release(self) -> None:
        for route in self.held:
            release_story(self.url, self.agent, self.story_id, aspect=route)
        self.held.clear()


@dataclass
class Claim:
    """The story's ``deliver`` lease for the length of one delivery.

    Exclusivity is only ever *asserted* while the lease is provably ours, so
    the handle remembers whether a renewal failed: a lease that would not renew
    may already belong to another delivery, and then this process must neither
    mutate gate state (:func:`_deliver_claimed` skips it) nor **release** —
    releasing would hand the other holder's own claim away, since deliveries on
    one host share the configured agent id.
    """

    url: str
    agent: str
    story_id: str
    ttl_minutes: int = DELIVER_CLAIM_TTL_MINUTES
    held: bool = False
    lost: bool = False

    def take(self) -> bool:
        self.held = claim_story(
            self.url, self.agent, self.story_id, ttl_minutes=self.ttl_minutes
        )
        return self.held

    def renew(self) -> bool:
        if not self.held:
            return False
        if renew_story(self.url, self.agent, self.story_id):
            return True
        # Not ours to release either: another delivery may hold it now.
        self.lost = True
        return False

    def release(self) -> None:
        if self.held and not self.lost:
            release_story(self.url, self.agent, self.story_id)
