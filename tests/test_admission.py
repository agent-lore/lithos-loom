"""Serial admission (PRD pr-reconciliation S6).

Before a PR-producing route claims a ready story, the runner counts the
project's OPEN ``pr`` gates — delivered-but-unmerged PRs — and refuses when
the limit is reached. Escalated gates (their story carries an open loom
``human`` gate) do not count against the admission limit, but they do
count against the looser total cap. A refused story is remembered and
re-evaluated when a gate in its project closes or escalates.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest

from lithos_loom.bus import Event, EventBus, Subscription
from lithos_loom.errors import LithosClientError
from lithos_loom.gates import create_human_gate, create_pr_gate
from lithos_loom.subscriptions.admission import (
    ADMISSION_HELD,
    ADMISSION_RECHECK_ORIGIN,
    LIMIT_KEY,
    TOTAL_KEY,
    Admission,
    AdmissionLimits,
    AdmissionWaker,
)
from tests.support import FakeLithosClient, make_note

_PROJECT = "lens"
_AGENT = "loom"


def _admission(
    client: FakeLithosClient, *, limit: int = 1, total: int = 3
) -> Admission:
    return Admission(
        lithos=client,
        agent_id=_AGENT,
        defaults=AdmissionLimits(limit=limit, total=total),
    )


async def _story(client: FakeLithosClient, title: str = "story") -> str:
    return await client.task_create(
        title=title,
        agent=_AGENT,
        tags=["trigger:story-develop"],
        metadata={"project": _PROJECT},
    )


async def _delivered(
    client: FakeLithosClient, *, number: int, project: str = _PROJECT
) -> tuple[str, str]:
    """A story with an open ``pr`` gate: ``(story_id, gate_id)``."""
    story = await _story(client, f"delivered {number}")
    gate = await create_pr_gate(
        client,
        story_id=story,
        story_title=f"delivered {number}",
        pr_url=f"https://github.com/agent-lore/lithos-lens/pull/{number}",
        project=project,
        agent=_AGENT,
    )
    return story, gate


async def _escalate(client: FakeLithosClient, story_id: str) -> str:
    return await create_human_gate(
        client,
        story_id=story_id,
        story_title="delivered",
        project=_PROJECT,
        agent=_AGENT,
        route="story-develop",
        reason="conflict_unresolved",
        summary="the resolver gave up",
    )


def _context_doc(client: FakeLithosClient, metadata: dict[str, Any]) -> None:
    client.add_note(
        make_note(
            "ctx",
            slug=_PROJECT,
            path=f"projects/{_PROJECT}/{_PROJECT}-project-context.md",
            tags=("project-context",),
            metadata=metadata,
        )
    )


# ── the verdict ──────────────────────────────────────────────────────────


async def test_no_project_is_admitted_without_reading_gates() -> None:
    client = FakeLithosClient(agent_id=_AGENT)
    adm = _admission(client)
    verdict = await adm.admit(task_id="s", project=None)
    assert verdict.admitted and verdict.reason == "no_project"
    assert not client.called("task_list")


async def test_under_the_limit_admits_and_reads_only_the_projects_pr_gates() -> None:
    client = FakeLithosClient(agent_id=_AGENT)
    await _delivered(client, number=1, project="other")
    story = await _story(client)
    adm = _admission(client)

    verdict = await adm.admit(task_id=story, project=_PROJECT)

    assert verdict.admitted and verdict.reason == "admitted"
    assert verdict.open_gates == 0
    (call,) = client.calls_to("task_list")
    assert call["status"] == "open" and call["task_type"] == "gate"
    assert call["metadata_match"] == {"gate_type": "pr", "project": _PROJECT}


async def test_at_the_limit_refuses_and_remembers_the_story() -> None:
    client = FakeLithosClient(agent_id=_AGENT)
    await _delivered(client, number=1)
    story = await _story(client)
    adm = _admission(client, limit=1)

    verdict = await adm.admit(task_id=story, project=_PROJECT)

    assert not verdict.admitted and verdict.reason == "limit"
    assert verdict.open_gates == 1 and verdict.escalated == 0
    assert adm.deferred(_PROJECT) == frozenset({story})
    assert client.findings == []  # the steady-state shape is not a signal


async def test_admission_releases_a_previously_deferred_story() -> None:
    client = FakeLithosClient(agent_id=_AGENT)
    _delivered_story, gate = await _delivered(client, number=1)
    story = await _story(client)
    adm = _admission(client, limit=1)
    assert not (await adm.admit(task_id=story, project=_PROJECT)).admitted

    await client.task_complete(task_id=gate, agent=_AGENT)
    verdict = await adm.admit(task_id=story, project=_PROJECT)

    assert verdict.admitted
    assert adm.deferred(_PROJECT) == frozenset()


async def test_an_escalated_gate_does_not_count_against_the_limit() -> None:
    """Operator decision 2026-08-24: a gate waiting on a human decision is
    not loom's work-in-progress; counting it would turn operator latency
    into a project stop."""
    client = FakeLithosClient(agent_id=_AGENT)
    delivered, _gate = await _delivered(client, number=1)
    await _escalate(client, delivered)
    story = await _story(client)
    adm = _admission(client, limit=1)

    verdict = await adm.admit(task_id=story, project=_PROJECT)

    assert verdict.admitted
    assert verdict.open_gates == 1 and verdict.escalated == 1


async def test_an_escalated_gate_is_found_via_its_edge_when_it_predates_story_id() -> (
    None
):
    """Gates created before S6 carry no ``story_id``; the waits_on_gate edge
    still names the story."""
    client = FakeLithosClient(agent_id=_AGENT)
    delivered = await _story(client, "old")
    gate = await client.task_create(
        title="Awaiting merge: old",
        agent=_AGENT,
        task_type="gate",
        metadata={
            "gate_type": "pr",
            "repo": "agent-lore/lithos-lens",
            "pr_number": 7,
            "required_state": "merged",
            "pr_url": "https://github.com/agent-lore/lithos-lens/pull/7",
            "project": _PROJECT,
        },
    )
    await client.task_edge_upsert(
        from_task_id=gate, to_task_id=delivered, type="waits_on_gate", agent=_AGENT
    )
    await _escalate(client, delivered)
    story = await _story(client)
    adm = _admission(client, limit=1)

    verdict = await adm.admit(task_id=story, project=_PROJECT)

    assert verdict.admitted and verdict.escalated == 1


async def test_the_operators_own_human_gate_does_not_escalate_a_pr_gate() -> None:
    client = FakeLithosClient(agent_id=_AGENT)
    delivered, _gate = await _delivered(client, number=1)
    human = await client.task_create(
        title="Decide something",
        agent="dave",
        task_type="gate",
        metadata={"gate_type": "human", "project": _PROJECT, "story_id": delivered},
    )
    await client.task_edge_upsert(
        from_task_id=human, to_task_id=delivered, type="waits_on_gate", agent="dave"
    )
    story = await _story(client)
    adm = _admission(client, limit=1)

    verdict = await adm.admit(task_id=story, project=_PROJECT)

    assert not verdict.admitted and verdict.escalated == 0


async def test_a_resolved_escalation_counts_again() -> None:
    client = FakeLithosClient(agent_id=_AGENT)
    delivered, _gate = await _delivered(client, number=1)
    human = await _escalate(client, delivered)
    await client.task_complete(task_id=human, agent=_AGENT)
    story = await _story(client)
    adm = _admission(client, limit=1)

    verdict = await adm.admit(task_id=story, project=_PROJECT)

    assert not verdict.admitted and verdict.escalated == 0


async def test_the_total_cap_counts_escalated_gates_and_posts_held_once() -> None:
    """Stuck PRs cannot accumulate unboundedly: the looser cap includes the
    escalated ones, stops dispatch, and says so — once per project per
    process, on the story it refused."""
    client = FakeLithosClient(agent_id=_AGENT)
    for n in (1, 2, 3):
        delivered, _gate = await _delivered(client, number=n)
        await _escalate(client, delivered)
    first = await _story(client, "first")
    second = await _story(client, "second")
    adm = _admission(client, limit=1, total=3)

    v1 = await adm.admit(task_id=first, project=_PROJECT)
    v2 = await adm.admit(task_id=second, project=_PROJECT)

    assert not v1.admitted and v1.reason == "total_cap"
    assert not v2.admitted and v2.reason == "total_cap"
    assert adm.deferred(_PROJECT) == frozenset({first, second})
    (finding,) = client.findings
    assert finding["task_id"] == first
    assert finding["summary"].startswith(ADMISSION_HELD)
    assert "3 delivered PR(s) open" in finding["summary"]
    assert f"{TOTAL_KEY}=3" in finding["summary"]
    assert "pull/1" in finding["summary"] and "pull/3" in finding["summary"]


async def test_the_total_cap_wins_over_an_unlimited_admission_limit() -> None:
    client = FakeLithosClient(agent_id=_AGENT)
    await _delivered(client, number=1)
    await _delivered(client, number=2)
    story = await _story(client)
    adm = _admission(client, limit=0, total=2)

    verdict = await adm.admit(task_id=story, project=_PROJECT)

    assert not verdict.admitted and verdict.reason == "total_cap"


async def test_zero_on_both_dials_means_unlimited() -> None:
    client = FakeLithosClient(agent_id=_AGENT)
    await _delivered(client, number=1)
    story = await _story(client)
    adm = _admission(client, limit=0, total=0)

    verdict = await adm.admit(task_id=story, project=_PROJECT)

    assert verdict.admitted and verdict.reason == "unlimited"
    assert not client.called("task_list")


async def test_the_context_doc_overrides_the_host_defaults() -> None:
    client = FakeLithosClient(agent_id=_AGENT)
    _context_doc(client, {LIMIT_KEY: 2, TOTAL_KEY: 5})
    await _delivered(client, number=1)
    story = await _story(client)
    adm = _admission(client, limit=1, total=3)

    verdict = await adm.admit(task_id=story, project=_PROJECT)

    assert verdict.admitted
    assert verdict.limits == AdmissionLimits(limit=2, total=5)


async def test_a_malformed_or_negative_dial_falls_back_to_the_default() -> None:
    client = FakeLithosClient(agent_id=_AGENT)
    _context_doc(client, {LIMIT_KEY: "lots", TOTAL_KEY: -1})
    story = await _story(client)
    adm = _admission(client, limit=1, total=3)

    verdict = await adm.admit(task_id=story, project=_PROJECT)

    assert verdict.limits == AdmissionLimits(limit=1, total=3)


async def test_a_total_below_the_limit_is_raised_to_the_limit() -> None:
    client = FakeLithosClient(agent_id=_AGENT)
    _context_doc(client, {LIMIT_KEY: 4, TOTAL_KEY: 2})
    story = await _story(client)
    adm = _admission(client)

    verdict = await adm.admit(task_id=story, project=_PROJECT)

    assert verdict.limits == AdmissionLimits(limit=4, total=4)


async def test_an_unreadable_context_doc_holds_the_story() -> None:
    """A project may TIGHTEN the host defaults (a total of 2 under a host 3),
    so an unreadable doc is not "use the defaults" — it is "cannot know the
    limit", and a serialisation invariant fails closed."""
    client = FakeLithosClient(agent_id=_AGENT)

    async def failing_note_read(**kwargs: Any) -> Any:
        raise LithosClientError("server_error", "lithos down")

    client.note_read = failing_note_read  # type: ignore[method-assign]
    story = await _story(client)
    adm = _admission(client, limit=1, total=3)

    verdict = await adm.admit(task_id=story, project=_PROJECT)

    assert not verdict.admitted and verdict.reason == "unreadable"
    assert adm.deferred(_PROJECT) == frozenset({story})
    assert not client.called("task_list")


async def test_an_unreadable_gate_list_refuses_and_defers() -> None:
    """Fail closed: a serialisation invariant must never be waived by an
    outage. The re-check asks again."""
    client = FakeLithosClient(agent_id=_AGENT)

    async def failing_task_list(**kwargs: Any) -> Any:
        raise LithosClientError("server_error", "lithos down")

    client.task_list = failing_task_list  # type: ignore[method-assign]
    story = await _story(client)
    adm = _admission(client)

    verdict = await adm.admit(task_id=story, project=_PROJECT)

    assert not verdict.admitted and verdict.reason == "unreadable"
    assert adm.deferred(_PROJECT) == frozenset({story})


async def test_an_unreadable_human_gate_list_counts_every_gate() -> None:
    client = FakeLithosClient(agent_id=_AGENT)
    delivered, _gate = await _delivered(client, number=1)
    await _escalate(client, delivered)
    story = await _story(client)
    real_task_list = client.task_list

    async def flaky_task_list(**kwargs: Any) -> Any:
        match = kwargs.get("metadata_match") or {}
        if match.get("gate_type") == "human":
            raise LithosClientError("server_error", "lithos down")
        return await real_task_list(**kwargs)

    client.task_list = flaky_task_list  # type: ignore[method-assign]
    adm = _admission(client, limit=1)

    verdict = await adm.admit(task_id=story, project=_PROJECT)

    assert not verdict.admitted and verdict.reason == "limit"
    assert verdict.escalated == 0


# ── the waker ────────────────────────────────────────────────────────────


def _probe(bus: EventBus) -> Subscription:
    return bus.subscribe(event_types=("lithos.task.updated",), name="probe")


def _gate_event(client: FakeLithosClient, gate_id: str, *, type_: str) -> Event:
    gate = client._tasks[gate_id]  # noqa: SLF001 — the fake's own store
    return Event(
        type=type_,
        timestamp=datetime.now(UTC),
        payload={
            "id": gate.id,
            "title": gate.title,
            "status": gate.status,
            "tags": list(gate.tags),
            "metadata": dict(gate.metadata),
            "task_type": gate.task_type,
        },
        origin="live",
    )


async def _run_for(waker: AdmissionWaker, *, seconds: float = 0.1) -> None:
    task = asyncio.create_task(waker.run())
    await asyncio.sleep(seconds)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def _deferred_behind(client: FakeLithosClient, adm: Admission) -> tuple[str, str]:
    """A story refused behind one delivered PR: ``(story_id, gate_id)``."""
    _delivered_story, gate = await _delivered(client, number=1)
    story = await _story(client, "waiting")
    assert not (await adm.admit(task_id=story, project=_PROJECT)).admitted
    return story, gate


async def test_a_closed_pr_gate_wakes_the_projects_deferred_stories() -> None:
    client = FakeLithosClient(agent_id=_AGENT)
    adm = _admission(client, limit=1)
    story, gate = await _deferred_behind(client, adm)
    bus = EventBus()
    probe = _probe(bus)
    waker = AdmissionWaker(bus=bus, lithos=client, admission=adm)

    await client.task_complete(task_id=gate, agent=_AGENT)
    await bus.publish(_gate_event(client, gate, type_="lithos.task.completed"))
    await _run_for(waker)

    nudge = probe.queue.get_nowait()
    assert nudge.origin == ADMISSION_RECHECK_ORIGIN
    assert nudge.payload["id"] == story
    assert nudge.payload["metadata"]["project"] == _PROJECT
    assert probe.queue.empty()


async def test_a_newly_escalated_gate_wakes_the_projects_deferred_stories() -> None:
    client = FakeLithosClient(agent_id=_AGENT)
    adm = _admission(client, limit=1)
    story, gate = await _deferred_behind(client, adm)
    delivered = client._tasks[gate].metadata["story_id"]  # noqa: SLF001
    bus = EventBus()
    probe = _probe(bus)
    waker = AdmissionWaker(bus=bus, lithos=client, admission=adm)

    human = await _escalate(client, delivered)
    await bus.publish(_gate_event(client, human, type_="lithos.task.created"))
    await _run_for(waker)

    nudge = probe.queue.get_nowait()
    assert nudge.payload["id"] == story


async def test_a_created_pr_gate_and_other_projects_do_not_wake() -> None:
    client = FakeLithosClient(agent_id=_AGENT)
    adm = _admission(client, limit=1)
    _story_id, gate = await _deferred_behind(client, adm)
    _other_story, other_gate = await _delivered(client, number=9, project="other")
    bus = EventBus()
    probe = _probe(bus)
    waker = AdmissionWaker(bus=bus, lithos=client, admission=adm)

    await bus.publish(_gate_event(client, gate, type_="lithos.task.created"))
    await client.task_complete(task_id=other_gate, agent=_AGENT)
    await bus.publish(_gate_event(client, other_gate, type_="lithos.task.completed"))
    await _run_for(waker)

    assert probe.queue.empty()


async def test_a_story_no_longer_open_is_forgotten_not_nudged() -> None:
    client = FakeLithosClient(agent_id=_AGENT)
    adm = _admission(client, limit=1)
    story, gate = await _deferred_behind(client, adm)
    await client.task_cancel(task_id=story, agent=_AGENT)
    bus = EventBus()
    probe = _probe(bus)
    waker = AdmissionWaker(bus=bus, lithos=client, admission=adm)

    await client.task_complete(task_id=gate, agent=_AGENT)
    await bus.publish(_gate_event(client, gate, type_="lithos.task.completed"))
    await _run_for(waker)

    assert probe.queue.empty()
    assert adm.deferred(_PROJECT) == frozenset()


async def test_the_waker_survives_a_failed_read_and_keeps_the_story() -> None:
    client = FakeLithosClient(agent_id=_AGENT)
    adm = _admission(client, limit=1)
    story, gate = await _deferred_behind(client, adm)

    async def failing_task_get(**kwargs: Any) -> Any:
        raise LithosClientError("server_error", "lithos down")

    client.task_get = failing_task_get  # type: ignore[method-assign]
    bus = EventBus()
    probe = _probe(bus)
    waker = AdmissionWaker(bus=bus, lithos=client, admission=adm)

    await bus.publish(_gate_event(client, gate, type_="lithos.task.completed"))
    await _run_for(waker)

    assert probe.queue.empty()
    assert adm.deferred(_PROJECT) == frozenset({story})


# ── the reservation (self-review HIGH) ───────────────────────────────────


async def test_an_admitted_story_holds_its_slot_until_its_run_ends() -> None:
    """The count is gates + in-flight admissions, or two PR-producing routes
    on one project could both admit before either delivered a gate."""
    client = FakeLithosClient(agent_id=_AGENT)
    first = await _story(client, "first")
    second = await _story(client, "second")
    adm = _admission(client, limit=1)

    assert (await adm.admit(task_id=first, project=_PROJECT)).admitted
    refused = await adm.admit(task_id=second, project=_PROJECT)
    assert not refused.admitted and refused.reason == "limit"
    assert refused.open_gates == 0 and refused.in_flight == 1
    assert adm.deferred(_PROJECT) == frozenset({second})

    adm.release(first)
    assert (await adm.admit(task_id=second, project=_PROJECT)).admitted


async def test_re_admitting_an_in_flight_story_does_not_count_itself() -> None:
    client = FakeLithosClient(agent_id=_AGENT)
    story = await _story(client)
    adm = _admission(client, limit=1)
    assert (await adm.admit(task_id=story, project=_PROJECT)).admitted
    again = await adm.admit(task_id=story, project=_PROJECT)
    assert again.admitted and again.in_flight == 0


async def test_in_flight_counts_against_the_total_cap_too() -> None:
    client = FakeLithosClient(agent_id=_AGENT)
    await _delivered(client, number=1)
    first = await _story(client, "first")
    second = await _story(client, "second")
    adm = _admission(client, limit=0, total=2)
    assert (await adm.admit(task_id=first, project=_PROJECT)).admitted

    verdict = await adm.admit(task_id=second, project=_PROJECT)

    assert not verdict.admitted and verdict.reason == "total_cap"


async def test_a_closed_gate_wakes_but_an_in_flight_story_still_holds() -> None:
    client = FakeLithosClient(agent_id=_AGENT)
    adm = _admission(client, limit=1)
    first = await _story(client, "first")
    assert (await adm.admit(task_id=first, project=_PROJECT)).admitted
    second = await _story(client, "second")
    assert not (await adm.admit(task_id=second, project=_PROJECT)).admitted
    # the run ends without a PR (failed): the slot frees with the release
    adm.release(first)
    assert (await adm.admit(task_id=second, project=_PROJECT)).admitted


# ── the held finding re-arms (self-review MEDIUM) ────────────────────────


async def test_the_held_finding_fires_again_after_the_cap_clears() -> None:
    client = FakeLithosClient(agent_id=_AGENT)
    gates = []
    for n in (1, 2, 3):
        delivered, gate = await _delivered(client, number=n)
        await _escalate(client, delivered)
        gates.append(gate)
    story = await _story(client)
    adm = _admission(client, limit=1, total=3)

    assert (await adm.admit(task_id=story, project=_PROJECT)).reason == "total_cap"
    await client.task_complete(task_id=gates[0], agent=_AGENT)
    assert (await adm.admit(task_id=story, project=_PROJECT)).admitted
    adm.release(story)
    _d, _g = await _delivered(client, number=4)
    other = await _story(client, "other")
    assert (await adm.admit(task_id=other, project=_PROJECT)).reason == "total_cap"

    assert [f["task_id"] for f in client.findings] == [story, other]


async def test_the_total_cap_verdict_carries_the_real_escalated_count() -> None:
    client = FakeLithosClient(agent_id=_AGENT)
    for n in (1, 2):
        delivered, _gate = await _delivered(client, number=n)
        await _escalate(client, delivered)
    await _delivered(client, number=3)
    story = await _story(client)
    adm = _admission(client, limit=1, total=3)

    verdict = await adm.admit(task_id=story, project=_PROJECT)

    assert verdict.reason == "total_cap"
    assert verdict.open_gates == 3 and verdict.escalated == 2
    (finding,) = client.findings
    assert "(2 escalated)" in finding["summary"]
    # one human-gate read, not one for the verdict and another for the finding
    humans = [
        c
        for c in client.calls_to("task_list")
        if (c["metadata_match"] or {}).get("gate_type") == "human"
    ]
    assert len(humans) == 1
