"""Tests for the gate-resolved re-dispatch nudge (b91177d2).

The resolver watches ``lithos.task.completed`` for loom-raised ``human`` gates
and republishes a synthetic ``lithos.task.updated`` (``origin="gate-resolved"``)
for the waiting story — only when the story is still open and still names
that gate as its blocker. Everything else is a no-op, by design: the nudge is
a convenience, the dispatch path owns correctness.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest

from lithos_loom.bus import Event, EventBus, Subscription
from lithos_loom.gates import create_human_gate
from lithos_loom.subscriptions.escalation_resolver import (
    GATE_RESOLVED_ORIGIN,
    EscalationResolver,
)
from tests.support import FakeLithosClient, make_task


def _probe(bus: EventBus) -> Subscription:
    """Watch what the resolver republishes."""
    return bus.subscribe(event_types=("lithos.task.updated",), name="probe")


def _completed_gate_event(gate: Any, *, origin: str = "live") -> Event:
    return Event(
        type="lithos.task.completed",
        timestamp=datetime.now(UTC),
        payload={
            "id": gate.id,
            "title": gate.title,
            "status": "completed",
            "tags": list(gate.tags),
            "metadata": dict(gate.metadata),
            "task_type": gate.task_type,
        },
        origin=origin,
    )


async def _run_for(resolver: EscalationResolver, *, seconds: float = 0.1) -> None:
    task = asyncio.create_task(resolver.run())
    await asyncio.sleep(seconds)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def _escalated_story(client: FakeLithosClient) -> tuple[str, str]:
    """A story with a loom gate on it, provenance recorded — the state the
    failure path leaves behind."""
    story = await client.task_create(
        title="US42", tags=["trigger:story-develop"], metadata={"project": "loom"}
    )
    gate = await create_human_gate(
        client,
        story_id=story,
        story_title="US42",
        project="loom",
        agent="loom",
        route="story-develop",
        reason="max_rounds",
        summary="round 5",
    )
    await client.task_update(
        task_id=story, agent="loom", metadata={"needs_human_gate_id": gate}
    )
    return story, gate


async def test_completed_loom_gate_nudges_its_story() -> None:
    client = FakeLithosClient(agent_id="loom")
    story, gate_id = await _escalated_story(client)
    await client.task_complete(task_id=gate_id, agent="dave")
    gate = await client.task_get(task_id=gate_id)
    assert gate is not None

    bus = EventBus()
    probe = _probe(bus)
    resolver = EscalationResolver(bus=bus, lithos=client, agent_id="loom")
    await bus.publish(_completed_gate_event(gate))
    await _run_for(resolver)

    nudge = probe.queue.get_nowait()
    assert nudge.type == "lithos.task.updated"
    assert nudge.origin == GATE_RESOLVED_ORIGIN
    assert nudge.payload["id"] == story
    assert nudge.payload["tags"] == ["trigger:story-develop"]
    assert nudge.payload["status"] == "open"
    assert probe.queue.empty()


async def test_operators_own_human_gate_is_not_the_resolvers_business() -> None:
    """No ``raised_by=loom`` → the bus filter never delivers it."""
    client = FakeLithosClient(agent_id="loom")
    story = await client.task_create(title="US42", tags=["trigger:story-develop"])
    gate_id = await client.task_create(
        title="Robot day", task_type="gate", metadata={"gate_type": "human"}
    )
    client.add_edge(from_task_id=gate_id, to_task_id=story, type="waits_on_gate")
    await client.task_complete(task_id=gate_id, agent="dave")
    gate = await client.task_get(task_id=gate_id)
    assert gate is not None

    bus = EventBus()
    probe = _probe(bus)
    resolver = EscalationResolver(bus=bus, lithos=client, agent_id="loom")
    await bus.publish(_completed_gate_event(gate))
    await _run_for(resolver)

    assert resolver.subscription.queue.empty()
    assert probe.queue.empty()


async def test_pr_gate_completion_is_ignored() -> None:
    bus = EventBus()
    probe = _probe(bus)
    resolver = EscalationResolver(bus=bus, lithos=FakeLithosClient(), agent_id="loom")
    pr_gate = make_task(
        "g", task_type="gate", status="completed", metadata={"gate_type": "pr"}
    )
    await bus.publish(_completed_gate_event(pr_gate))
    await _run_for(resolver)
    assert probe.queue.empty()


async def test_orphan_gate_nudges_nothing() -> None:
    client = FakeLithosClient(agent_id="loom")
    gate_id = await client.task_create(
        title="orphan",
        task_type="gate",
        metadata={"gate_type": "human", "raised_by": "loom"},
    )
    await client.task_complete(task_id=gate_id, agent="dave")
    gate = await client.task_get(task_id=gate_id)
    assert gate is not None

    bus = EventBus()
    probe = _probe(bus)
    resolver = EscalationResolver(bus=bus, lithos=client, agent_id="loom")
    await bus.publish(_completed_gate_event(gate))
    await _run_for(resolver)
    assert probe.queue.empty()


async def test_resolved_story_is_not_nudged() -> None:
    """Slice D's hygiene completes a gate whose waiter already resolved; the
    story is terminal, so there is nothing to re-dispatch."""
    client = FakeLithosClient(agent_id="loom")
    story, gate_id = await _escalated_story(client)
    await client.task_cancel(task_id=story, agent="dave")
    await client.task_complete(task_id=gate_id, agent="loom")
    gate = await client.task_get(task_id=gate_id)
    assert gate is not None

    bus = EventBus()
    probe = _probe(bus)
    resolver = EscalationResolver(bus=bus, lithos=client, agent_id="loom")
    await bus.publish(_completed_gate_event(gate))
    await _run_for(resolver)
    assert probe.queue.empty()


async def test_completion_of_a_superseded_gate_is_a_no_op() -> None:
    """The story has since failed into a NEWER gate: the completed gate no
    longer names its blocker, so a duplicate / late completion must not
    un-dedup a run — the newer gate guards it via readiness anyway."""
    client = FakeLithosClient(agent_id="loom")
    story, gate_id = await _escalated_story(client)
    await client.task_update(
        task_id=story, agent="loom", metadata={"needs_human_gate_id": "gate-newer"}
    )
    await client.task_complete(task_id=gate_id, agent="dave")
    gate = await client.task_get(task_id=gate_id)
    assert gate is not None

    bus = EventBus()
    probe = _probe(bus)
    resolver = EscalationResolver(bus=bus, lithos=client, agent_id="loom")
    await bus.publish(_completed_gate_event(gate))
    await _run_for(resolver)
    assert probe.queue.empty()


async def test_missing_provenance_still_nudges() -> None:
    """PR #349 review F2: the degraded escalation path — gate + edge landed,
    the story provenance write failed — leaves the story with NO
    needs_human_gate_id. Completing the gate must still retry in-process; the
    waits_on_gate edge proves the linkage."""
    client = FakeLithosClient(agent_id="loom")
    story, gate_id = await _escalated_story(client)
    await client.task_update(
        task_id=story, agent="loom", metadata={"needs_human_gate_id": None}
    )
    await client.task_complete(task_id=gate_id, agent="dave")
    gate = await client.task_get(task_id=gate_id)
    assert gate is not None

    bus = EventBus()
    probe = _probe(bus)
    resolver = EscalationResolver(bus=bus, lithos=client, agent_id="loom")
    await bus.publish(_completed_gate_event(gate))
    await _run_for(resolver)

    nudge = probe.queue.get_nowait()
    assert nudge.origin == GATE_RESOLVED_ORIGIN
    assert nudge.payload["id"] == story


async def test_a_replayed_completion_nudges_at_most_once_per_process() -> None:
    """PR #349 review, round 2: a completion is terminal, so a second delivery
    of the same gate's completion is always an SSE replay. It must not nudge
    again — after the first retry dispatched and cleared the story's
    provenance, a marker-free re-nudge would un-dedup the runner, reset the
    resume budget, and start a duplicate run past a pending resume schedule."""
    client = FakeLithosClient(agent_id="loom")
    story, gate_id = await _escalated_story(client)
    await client.task_complete(task_id=gate_id, agent="dave")
    gate = await client.task_get(task_id=gate_id)
    assert gate is not None

    bus = EventBus()
    probe = _probe(bus)
    resolver = EscalationResolver(bus=bus, lithos=client, agent_id="loom")
    await bus.publish(_completed_gate_event(gate))
    await _run_for(resolver)
    assert probe.queue.get_nowait().payload["id"] == story

    # The first nudge's dispatch cleared the provenance; the replay arrives.
    await client.task_update(
        task_id=story, agent="loom", metadata={"needs_human_gate_id": None}
    )
    await bus.publish(_completed_gate_event(gate))
    await _run_for(resolver)
    assert probe.queue.empty()


async def test_a_fresh_process_replay_nudges_again_by_design() -> None:
    """The dedup is deliberately per-process: after a restart the state a
    duplicate nudge could corrupt is gone too, so a replayed completion
    degrades to the restart bootstrap's own (correct) semantics."""
    client = FakeLithosClient(agent_id="loom")
    story, gate_id = await _escalated_story(client)
    await client.task_complete(task_id=gate_id, agent="dave")
    gate = await client.task_get(task_id=gate_id)
    assert gate is not None

    for _ in range(2):  # two daemon lifetimes, one delivery each
        bus = EventBus()
        probe = _probe(bus)
        resolver = EscalationResolver(bus=bus, lithos=client, agent_id="loom")
        await bus.publish(_completed_gate_event(gate))
        await _run_for(resolver)
        assert probe.queue.get_nowait().payload["id"] == story


async def test_a_lithos_error_does_not_kill_the_loop() -> None:
    client = FakeLithosClient(agent_id="loom")
    story, gate_id = await _escalated_story(client)
    await client.task_complete(task_id=gate_id, agent="dave")
    gate = await client.task_get(task_id=gate_id)
    assert gate is not None
    client.raise_on["task_edge_list"] = RuntimeError("lithos hiccup")

    bus = EventBus()
    probe = _probe(bus)
    resolver = EscalationResolver(bus=bus, lithos=client, agent_id="loom")
    await bus.publish(_completed_gate_event(gate))
    await _run_for(resolver)  # the run loop survives the exception

    assert probe.queue.empty()
