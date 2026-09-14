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
import contextlib
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
)
from lithos_loom.subscriptions.admission_waker import AdmissionWaker
from lithos_loom.task_line import PRIORITY_EMOJI
from tests.support import FakeLithosClient, make_note

_PROJECT = "lens"
_AGENT = "loom"
_ROUTE = "story-develop"


def _probe(bus: EventBus) -> Subscription:
    """Subscribed the way a PR-producing route runner is — on the trigger
    tag — so every nudge asserted here is one a runner would receive."""
    return bus.subscribe(
        event_types=("lithos.task.updated",),
        match={"tags": ["trigger:story-develop"]},
        name="probe",
    )


def _admission(
    client: FakeLithosClient,
    *,
    limit: int = 1,
    total: int = 3,
    bus: EventBus | None = None,
) -> Admission:
    if bus is None:
        bus = EventBus()
        _probe(bus)  # a route subscribed: held stories stay matchable
    return Admission(
        lithos=client,
        agent_id=_AGENT,
        defaults=AdmissionLimits(limit=limit, total=total),
        bus=bus,
    )


async def _story(
    client: FakeLithosClient, title: str = "story", *, project: str | None = _PROJECT
) -> str:
    return await client.task_create(
        title=title,
        agent=_AGENT,
        tags=["trigger:story-develop"],
        metadata={"project": project} if project else {},
    )


async def _delivered(
    client: FakeLithosClient, *, number: int, project: str | None = _PROJECT
) -> tuple[str, str]:
    """A story with an open ``pr`` gate: ``(story_id, gate_id)``."""
    story = await _story(client, f"delivered {number}", project=project)
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


async def test_projectless_stories_share_one_bucket_bounded_by_projectless_gates() -> (
    None
):
    """Review #368 F1: a route with an absolute repo path needs no project,
    and its stories deliver projectless ``pr`` gates. They are not exempt —
    they share one bucket under the host defaults."""
    client = FakeLithosClient(agent_id=_AGENT)
    await _delivered(client, number=1, project=None)
    await _delivered(client, number=2)  # lens: a different bucket
    story = await _story(client, project=None)
    adm = _admission(client, limit=1)

    verdict = await adm.admit(route=_ROUTE, task_id=story, project=None)

    assert not verdict.admitted and verdict.reason == "limit"
    assert verdict.open_gates == 1
    assert adm.deferred(None) == frozenset({story})
    # and the project bucket does not see the projectless gate
    lens = await _story(client, "lens story")
    assert (
        await adm.admit(route=_ROUTE, task_id=lens, project=_PROJECT)
    ).open_gates == 1


async def test_projectless_admission_reserves_in_flight_too() -> None:
    client = FakeLithosClient(agent_id=_AGENT)
    first = await _story(client, "first", project=None)
    second = await _story(client, "second", project=None)
    adm = _admission(client, limit=1)
    assert (await adm.admit(route=_ROUTE, task_id=first, project=None)).admitted
    assert not (await adm.admit(route=_ROUTE, task_id=second, project=None)).admitted
    assert not client.called("note_read")  # no context doc to consult


async def test_under_the_limit_admits_and_reads_only_the_projects_pr_gates() -> None:
    client = FakeLithosClient(agent_id=_AGENT)
    await _delivered(client, number=1, project="other")
    story = await _story(client)
    adm = _admission(client)

    verdict = await adm.admit(route=_ROUTE, task_id=story, project=_PROJECT)

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

    verdict = await adm.admit(route=_ROUTE, task_id=story, project=_PROJECT)

    assert not verdict.admitted and verdict.reason == "limit"
    assert verdict.open_gates == 1 and verdict.escalated == 0
    assert adm.deferred(_PROJECT) == frozenset({story})
    assert client.findings == []  # the steady-state shape is not a signal


async def test_admission_releases_a_previously_deferred_story() -> None:
    client = FakeLithosClient(agent_id=_AGENT)
    _delivered_story, gate = await _delivered(client, number=1)
    story = await _story(client)
    adm = _admission(client, limit=1)
    assert not (await adm.admit(route=_ROUTE, task_id=story, project=_PROJECT)).admitted

    await client.task_complete(task_id=gate, agent=_AGENT)
    verdict = await adm.admit(route=_ROUTE, task_id=story, project=_PROJECT)

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

    verdict = await adm.admit(route=_ROUTE, task_id=story, project=_PROJECT)

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

    verdict = await adm.admit(route=_ROUTE, task_id=story, project=_PROJECT)

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

    verdict = await adm.admit(route=_ROUTE, task_id=story, project=_PROJECT)

    assert not verdict.admitted and verdict.escalated == 0


async def test_a_resolved_escalation_counts_again() -> None:
    client = FakeLithosClient(agent_id=_AGENT)
    delivered, _gate = await _delivered(client, number=1)
    human = await _escalate(client, delivered)
    await client.task_complete(task_id=human, agent=_AGENT)
    story = await _story(client)
    adm = _admission(client, limit=1)

    verdict = await adm.admit(route=_ROUTE, task_id=story, project=_PROJECT)

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

    v1 = await adm.admit(route=_ROUTE, task_id=first, project=_PROJECT)
    v2 = await adm.admit(route=_ROUTE, task_id=second, project=_PROJECT)

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

    verdict = await adm.admit(route=_ROUTE, task_id=story, project=_PROJECT)

    assert not verdict.admitted and verdict.reason == "total_cap"


async def test_zero_on_both_dials_means_unlimited() -> None:
    client = FakeLithosClient(agent_id=_AGENT)
    await _delivered(client, number=1)
    story = await _story(client)
    adm = _admission(client, limit=0, total=0)

    verdict = await adm.admit(route=_ROUTE, task_id=story, project=_PROJECT)

    assert verdict.admitted and verdict.reason == "unlimited"
    assert not client.called("task_list")


async def test_the_context_doc_overrides_the_host_defaults() -> None:
    client = FakeLithosClient(agent_id=_AGENT)
    _context_doc(client, {LIMIT_KEY: 2, TOTAL_KEY: 5})
    await _delivered(client, number=1)
    story = await _story(client)
    adm = _admission(client, limit=1, total=3)

    verdict = await adm.admit(route=_ROUTE, task_id=story, project=_PROJECT)

    assert verdict.admitted
    assert verdict.limits == AdmissionLimits(limit=2, total=5)


async def test_a_malformed_or_negative_dial_falls_back_to_the_default() -> None:
    client = FakeLithosClient(agent_id=_AGENT)
    _context_doc(client, {LIMIT_KEY: "lots", TOTAL_KEY: -1})
    story = await _story(client)
    adm = _admission(client, limit=1, total=3)

    verdict = await adm.admit(route=_ROUTE, task_id=story, project=_PROJECT)

    assert verdict.limits == AdmissionLimits(limit=1, total=3)


async def test_a_total_below_the_limit_is_raised_to_the_limit() -> None:
    client = FakeLithosClient(agent_id=_AGENT)
    _context_doc(client, {LIMIT_KEY: 4, TOTAL_KEY: 2})
    story = await _story(client)
    adm = _admission(client)

    verdict = await adm.admit(route=_ROUTE, task_id=story, project=_PROJECT)

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

    verdict = await adm.admit(route=_ROUTE, task_id=story, project=_PROJECT)

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

    verdict = await adm.admit(route=_ROUTE, task_id=story, project=_PROJECT)

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

    verdict = await adm.admit(route=_ROUTE, task_id=story, project=_PROJECT)

    assert not verdict.admitted and verdict.reason == "limit"
    assert verdict.escalated == 0


# ── the waker ────────────────────────────────────────────────────────────


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
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def _deferred_behind(client: FakeLithosClient, adm: Admission) -> tuple[str, str]:
    """A story refused behind one delivered PR: ``(story_id, gate_id)``."""
    _delivered_story, gate = await _delivered(client, number=1)
    story = await _story(client, "waiting")
    assert not (await adm.admit(route=_ROUTE, task_id=story, project=_PROJECT)).admitted
    return story, gate


async def test_a_closed_pr_gate_wakes_the_projects_deferred_stories() -> None:
    client = FakeLithosClient(agent_id=_AGENT)
    bus = EventBus()
    adm = _admission(client, limit=1, bus=bus)
    story, gate = await _deferred_behind(client, adm)
    probe = _probe(bus)
    waker = AdmissionWaker(bus=bus, admission=adm)

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
    bus = EventBus()
    adm = _admission(client, limit=1, bus=bus)
    story, gate = await _deferred_behind(client, adm)
    delivered = client._tasks[gate].metadata["story_id"]  # noqa: SLF001
    probe = _probe(bus)
    waker = AdmissionWaker(bus=bus, admission=adm)

    human = await _escalate(client, delivered)
    await bus.publish(_gate_event(client, human, type_="lithos.task.created"))
    await _run_for(waker)

    nudge = probe.queue.get_nowait()
    assert nudge.payload["id"] == story


async def test_a_created_pr_gate_and_other_projects_do_not_wake() -> None:
    client = FakeLithosClient(agent_id=_AGENT)
    bus = EventBus()
    adm = _admission(client, limit=1, bus=bus)
    _story_id, gate = await _deferred_behind(client, adm)
    _other_story, other_gate = await _delivered(client, number=9, project="other")
    probe = _probe(bus)
    waker = AdmissionWaker(bus=bus, admission=adm)

    await bus.publish(_gate_event(client, gate, type_="lithos.task.created"))
    await client.task_complete(task_id=other_gate, agent=_AGENT)
    await bus.publish(_gate_event(client, other_gate, type_="lithos.task.completed"))
    await _run_for(waker)

    assert probe.queue.empty()


async def test_a_story_no_longer_open_is_forgotten_not_nudged() -> None:
    client = FakeLithosClient(agent_id=_AGENT)
    bus = EventBus()
    adm = _admission(client, limit=1, bus=bus)
    story, gate = await _deferred_behind(client, adm)
    await client.task_cancel(task_id=story, agent=_AGENT)
    probe = _probe(bus)
    waker = AdmissionWaker(bus=bus, admission=adm)

    await client.task_complete(task_id=gate, agent=_AGENT)
    await bus.publish(_gate_event(client, gate, type_="lithos.task.completed"))
    await _run_for(waker)

    assert probe.queue.empty()
    assert adm.deferred(_PROJECT) == frozenset()


async def test_the_waker_survives_a_failed_read_and_keeps_the_story() -> None:
    client = FakeLithosClient(agent_id=_AGENT)
    bus = EventBus()
    adm = _admission(client, limit=1, bus=bus)
    story, gate = await _deferred_behind(client, adm)

    async def failing_task_get(**kwargs: Any) -> Any:
        raise LithosClientError("server_error", "lithos down")

    client.task_get = failing_task_get  # type: ignore[method-assign]
    probe = _probe(bus)
    waker = AdmissionWaker(bus=bus, admission=adm)

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

    assert (await adm.admit(route=_ROUTE, task_id=first, project=_PROJECT)).admitted
    refused = await adm.admit(route=_ROUTE, task_id=second, project=_PROJECT)
    assert not refused.admitted and refused.reason == "limit"
    assert refused.open_gates == 0 and refused.in_flight == 1
    assert adm.deferred(_PROJECT) == frozenset({second})

    await adm.release(first, route=_ROUTE)
    assert (await adm.admit(route=_ROUTE, task_id=second, project=_PROJECT)).admitted


async def test_re_admitting_an_in_flight_story_does_not_count_itself() -> None:
    client = FakeLithosClient(agent_id=_AGENT)
    story = await _story(client)
    adm = _admission(client, limit=1)
    assert (await adm.admit(route=_ROUTE, task_id=story, project=_PROJECT)).admitted
    again = await adm.admit(route=_ROUTE, task_id=story, project=_PROJECT)
    assert again.admitted and again.in_flight == 0


async def test_in_flight_counts_against_the_total_cap_too() -> None:
    client = FakeLithosClient(agent_id=_AGENT)
    await _delivered(client, number=1)
    first = await _story(client, "first")
    second = await _story(client, "second")
    adm = _admission(client, limit=0, total=2)
    assert (await adm.admit(route=_ROUTE, task_id=first, project=_PROJECT)).admitted

    verdict = await adm.admit(route=_ROUTE, task_id=second, project=_PROJECT)

    assert not verdict.admitted and verdict.reason == "total_cap"


async def test_a_closed_gate_wakes_but_an_in_flight_story_still_holds() -> None:
    client = FakeLithosClient(agent_id=_AGENT)
    adm = _admission(client, limit=1)
    first = await _story(client, "first")
    assert (await adm.admit(route=_ROUTE, task_id=first, project=_PROJECT)).admitted
    second = await _story(client, "second")
    assert not (
        await adm.admit(route=_ROUTE, task_id=second, project=_PROJECT)
    ).admitted
    # the run ends without a PR (failed): the slot frees with the release
    await adm.release(first, route=_ROUTE)
    assert (await adm.admit(route=_ROUTE, task_id=second, project=_PROJECT)).admitted


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

    assert (
        await adm.admit(route=_ROUTE, task_id=story, project=_PROJECT)
    ).reason == "total_cap"
    await client.task_complete(task_id=gates[0], agent=_AGENT)
    assert (await adm.admit(route=_ROUTE, task_id=story, project=_PROJECT)).admitted
    await adm.release(story, route=_ROUTE)
    await _delivered(client, number=4)
    other = await _story(client, "other")
    assert (
        await adm.admit(route=_ROUTE, task_id=other, project=_PROJECT)
    ).reason == "total_cap"

    assert [f["task_id"] for f in client.findings] == [story, other]


async def test_the_total_cap_verdict_carries_the_real_escalated_count() -> None:
    client = FakeLithosClient(agent_id=_AGENT)
    for n in (1, 2):
        delivered, _gate = await _delivered(client, number=n)
        await _escalate(client, delivered)
    await _delivered(client, number=3)
    story = await _story(client)
    adm = _admission(client, limit=1, total=3)

    verdict = await adm.admit(route=_ROUTE, task_id=story, project=_PROJECT)

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


# ── review #368: atomic per-project transition, transport errors, edges ──


async def test_concurrent_admits_serialise_per_project() -> None:
    """Review #368 F2: two route runners share one Admission; with an
    escalated PR at the limit, both could snapshot an empty in-flight set
    across the human-gate read and both admit. The transition is locked."""
    client = FakeLithosClient(agent_id=_AGENT)
    delivered, _gate = await _delivered(client, number=1)
    await _escalate(client, delivered)
    a = await _story(client, "a")
    b = await _story(client, "b")
    real_task_list = client.task_list

    async def yielding_task_list(**kwargs: Any) -> Any:
        await asyncio.sleep(0)  # a real Lithos read suspends here
        return await real_task_list(**kwargs)

    client.task_list = yielding_task_list  # type: ignore[method-assign]
    adm = _admission(client, limit=1)

    va, vb = await asyncio.gather(
        adm.admit(route=_ROUTE, task_id=a, project=_PROJECT),
        adm.admit(route=_ROUTE, task_id=b, project=_PROJECT),
    )

    assert [va.admitted, vb.admitted].count(True) == 1
    assert len(adm.deferred(_PROJECT)) == 1


async def test_concurrent_admits_post_held_once() -> None:
    client = FakeLithosClient(agent_id=_AGENT)
    for n in (1, 2, 3):
        delivered, _gate = await _delivered(client, number=n)
        await _escalate(client, delivered)
    a = await _story(client, "a")
    b = await _story(client, "b")
    real_task_list = client.task_list

    async def yielding_task_list(**kwargs: Any) -> Any:
        await asyncio.sleep(0)
        return await real_task_list(**kwargs)

    client.task_list = yielding_task_list  # type: ignore[method-assign]
    adm = _admission(client, limit=1, total=3)

    await asyncio.gather(
        adm.admit(route=_ROUTE, task_id=a, project=_PROJECT),
        adm.admit(route=_ROUTE, task_id=b, project=_PROJECT),
    )

    assert len(client.findings) == 1


@pytest.mark.parametrize("failing", ["task_list", "note_read", "task_edge_list"])
async def test_a_raw_transport_error_on_any_read_refuses_and_defers(
    failing: str,
) -> None:
    """Review #368 F3: ``LithosClient._invoke`` re-raises the raw transport
    exception after its reconnect attempts; it must hold the story (so the
    sleeper re-asks), never escape and drop the event."""
    client = FakeLithosClient(agent_id=_AGENT)
    delivered = await _story(client, "old")
    gate = await client.task_create(
        title="Awaiting merge: old",
        agent=_AGENT,
        task_type="gate",
        metadata={"gate_type": "pr", "project": _PROJECT, "pr_number": 7},
    )
    await client.task_edge_upsert(
        from_task_id=gate, to_task_id=delivered, type="waits_on_gate", agent=_AGENT
    )
    await _escalate(client, delivered)
    story = await _story(client)

    async def boom(**kwargs: Any) -> Any:
        raise RuntimeError("stream closed")

    setattr(client, failing, boom)
    adm = _admission(client, limit=1)

    verdict = await adm.admit(route=_ROUTE, task_id=story, project=_PROJECT)

    assert not verdict.admitted and verdict.reason == "unreadable"
    assert adm.deferred(_PROJECT) == frozenset({story})


async def test_a_human_gate_without_its_edge_does_not_escalate() -> None:
    """Review #368 F4: the ``waits_on_gate`` edge is the authority; a gate
    task whose edge never landed (creation is not atomic) blocks nothing,
    so it must not free a slot."""
    client = FakeLithosClient(agent_id=_AGENT)
    delivered, _gate = await _delivered(client, number=1)
    await client.task_create(
        title="Needs human: delivered",
        agent=_AGENT,
        task_type="gate",
        metadata={
            "gate_type": "human",
            "raised_by": "loom",
            "project": _PROJECT,
            "story_id": delivered,
            "escalation_reason": "conflict_unresolved",
        },
    )
    story = await _story(client)
    adm = _admission(client, limit=1)

    verdict = await adm.admit(route=_ROUTE, task_id=story, project=_PROJECT)

    assert not verdict.admitted and verdict.escalated == 0


async def test_a_closed_projectless_gate_wakes_projectless_deferred_stories() -> None:
    client = FakeLithosClient(agent_id=_AGENT)
    bus = EventBus()
    adm = _admission(client, limit=1, bus=bus)
    _d, gate = await _delivered(client, number=1, project=None)
    story = await _story(client, "waiting", project=None)
    assert not (await adm.admit(route=_ROUTE, task_id=story, project=None)).admitted
    probe = _probe(bus)
    waker = AdmissionWaker(bus=bus, admission=adm)

    await client.task_complete(task_id=gate, agent=_AGENT)
    await bus.publish(_gate_event(client, gate, type_="lithos.task.completed"))
    await _run_for(waker)

    assert probe.queue.get_nowait().payload["id"] == story


# ── review #368 round 2: the reservation is (route, story) ───────────────


async def test_two_pr_producing_routes_on_one_story_each_take_a_slot() -> None:
    """A task may match several routes; two PR-producing ones are two runs
    that deliver two PRs. Keyed by story alone, the second admission saw
    its own reservation as free."""
    client = FakeLithosClient(agent_id=_AGENT)
    story = await _story(client)
    real_task_list = client.task_list

    async def yielding_task_list(**kwargs: Any) -> Any:
        await asyncio.sleep(0)
        return await real_task_list(**kwargs)

    client.task_list = yielding_task_list  # type: ignore[method-assign]
    adm = _admission(client, limit=1)

    va, vb = await asyncio.gather(
        adm.admit(route="story-develop", task_id=story, project=_PROJECT),
        adm.admit(route="story-develop-fast", task_id=story, project=_PROJECT),
    )

    assert [va.admitted, vb.admitted].count(True) == 1


async def test_a_release_frees_only_that_routes_reservation() -> None:
    client = FakeLithosClient(agent_id=_AGENT)
    story = await _story(client, "first")
    other = await _story(client, "other")
    adm = _admission(client, limit=1)
    assert (await adm.admit(route="a", task_id=story, project=_PROJECT)).admitted

    await adm.release(story, route="b")  # a route that never held it
    assert not (await adm.admit(route="a", task_id=other, project=_PROJECT)).admitted
    await adm.release(story, route="a")
    assert (await adm.admit(route="a", task_id=other, project=_PROJECT)).admitted


async def test_admitting_one_route_keeps_another_routes_deferral() -> None:
    """The deferred set is keyed like the reservation: route B being admitted
    must not forget route A's wait on the same story, or the waker's fast
    nudge for A degrades to the sleeper's backoff."""
    client = FakeLithosClient(agent_id=_AGENT)
    _d, gate = await _delivered(client, number=1)
    story = await _story(client)
    adm = _admission(client, limit=1)
    assert not (await adm.admit(route="a", task_id=story, project=_PROJECT)).admitted
    await client.task_complete(task_id=gate, agent=_AGENT)
    assert (await adm.admit(route="b", task_id=story, project=_PROJECT)).admitted

    assert adm.deferred(_PROJECT) == frozenset({story})
    await adm.forget(story)  # the story left the open set: every route's wait ends
    assert adm.deferred(_PROJECT) == frozenset()


# ── release order (561db86a / ADR 0012) ─────────────────────────────────
#
# Which held story takes a freed slot: `metadata.priority` first (the
# default — none set — between low and medium), then the order the stories
# were first held. The choice is made INSIDE Admission, so a sleeper's
# re-ask cannot pre-empt the waker's sweep.


_TAGS = ("trigger:story-develop",)  # what the story-develop route matches on


async def _hold_in_order(
    adm: Admission, *stories: str, project: str | None = _PROJECT
) -> None:
    """Refuse *stories* in the given order, each behind the open gate — as
    the story-develop route asks, its match tags reported."""
    for story in stories:
        verdict = await adm.admit(
            route=_ROUTE, task_id=story, project=project, tags=_TAGS
        )
        assert not verdict.admitted and verdict.reason == "limit"


async def _prioritised(client: FakeLithosClient, title: str, priority: str) -> str:
    return await client.task_create(
        title=title,
        agent=_AGENT,
        tags=["trigger:story-develop"],
        metadata={"project": _PROJECT, "priority": priority},
    )


async def _release_sequence(
    adm: Admission, probe: Subscription, *, project: str | None = _PROJECT
) -> list[str]:
    """Drain one slot's worth of the bucket: admit whatever was nudged,
    end its run without a gate (the slot frees, the next is woken), repeat.
    The order stories are admitted in IS the release order."""
    sequence: list[str] = []
    while nudged := _nudged(probe):
        assert len(nudged) == 1, nudged  # one slot: one story entitled
        story = nudged[0]
        assert (await adm.admit(route=_ROUTE, task_id=story, project=project)).admitted
        sequence.append(story)
        await adm.release(story, route=_ROUTE)
    return sequence


def _nudged(probe: Subscription) -> list[str]:
    ids: list[str] = []
    while not probe.queue.empty():
        event = probe.queue.get_nowait()
        assert event.origin == ADMISSION_RECHECK_ORIGIN
        ids.append(event.payload["id"])
    return ids


async def test_held_stories_are_released_in_the_order_they_were_held() -> None:
    """The old key was the UUID string; the fake's ids ascend with creation,
    so holding them in reverse creation order tells the two apart."""
    client = FakeLithosClient(agent_id=_AGENT)
    bus = EventBus()
    probe = _probe(bus)
    adm = _admission(client, limit=1, bus=bus)
    _d, gate = await _delivered(client, number=1)
    created = [await _story(client, t) for t in ("first made", "second", "third")]
    held_order = list(reversed(created))
    await _hold_in_order(adm, *held_order)
    await client.task_complete(task_id=gate, agent=_AGENT)

    assert await adm.wake(_PROJECT) == 1  # one slot: only the head is told

    assert await _release_sequence(adm, probe) == held_order


async def test_a_re_refused_story_keeps_its_place() -> None:
    """The sleeper re-asks every held story; a refusal must not send it to
    the back of the queue."""
    client = FakeLithosClient(agent_id=_AGENT)
    bus = EventBus()
    probe = _probe(bus)
    adm = _admission(client, limit=1, bus=bus)
    _d, gate = await _delivered(client, number=1)
    first, second = await _story(client, "first"), await _story(client, "second")
    await _hold_in_order(adm, first, second)
    await _hold_in_order(adm, first)  # its sleeper fired again
    await client.task_complete(task_id=gate, agent=_AGENT)

    await adm.wake(_PROJECT)

    assert await _release_sequence(adm, probe) == [first, second]


async def test_the_sleeper_cannot_pre_empt_the_head_of_the_queue() -> None:
    """A slot frees and the SECOND held story's re-check fires first. The
    choice lives in Admission: it refuses the late asker as ``queued`` and
    nudges the head itself, so the order does not depend on which producer
    — waker or sleeper — publishes first."""
    client = FakeLithosClient(agent_id=_AGENT)
    bus = EventBus()
    probe = _probe(bus)
    adm = _admission(client, limit=1, bus=bus)
    _d, gate = await _delivered(client, number=1)
    first, second = await _story(client, "first"), await _story(client, "second")
    await _hold_in_order(adm, first, second)
    await client.task_complete(task_id=gate, agent=_AGENT)

    late = await adm.admit(route=_ROUTE, task_id=second, project=_PROJECT)

    assert not late.admitted and late.reason == "queued"
    assert _nudged(probe) == [first]
    assert adm.deferred(_PROJECT) == frozenset({first, second})
    assert (await adm.admit(route=_ROUTE, task_id=first, project=_PROJECT)).admitted
    again = await adm.admit(route=_ROUTE, task_id=second, project=_PROJECT)
    assert not again.admitted and again.reason == "limit"
    assert _nudged(probe) == []  # no slot: nothing to nudge


async def test_a_priority_above_the_default_jumps_the_queue() -> None:
    client = FakeLithosClient(agent_id=_AGENT)
    bus = EventBus()
    probe = _probe(bus)
    adm = _admission(client, limit=1, bus=bus)
    _d, gate = await _delivered(client, number=1)
    plain = await _story(client, "plain")
    medium = await _prioritised(client, "medium", "medium")
    highest = await _prioritised(client, "highest", "highest")
    high = await _prioritised(client, "high", "high")
    await _hold_in_order(adm, plain, medium, highest, high)
    await client.task_complete(task_id=gate, agent=_AGENT)

    await adm.wake(_PROJECT)

    assert await _release_sequence(adm, probe) == [highest, high, medium, plain]


async def test_a_priority_below_the_default_yields_to_it() -> None:
    client = FakeLithosClient(agent_id=_AGENT)
    bus = EventBus()
    probe = _probe(bus)
    adm = _admission(client, limit=1, bus=bus)
    _d, gate = await _delivered(client, number=1)
    low = await _prioritised(client, "low", "low")
    lowest = await _prioritised(client, "lowest", "lowest")
    plain = await _story(client, "plain")
    await _hold_in_order(adm, low, lowest, plain)
    await client.task_complete(task_id=gate, agent=_AGENT)

    await adm.wake(_PROJECT)

    assert await _release_sequence(adm, probe) == [plain, low, lowest]


@pytest.mark.parametrize("priority", ["bogus", 3, None])
async def test_an_unknown_priority_is_the_default(priority: Any) -> None:
    client = FakeLithosClient(agent_id=_AGENT)
    bus = EventBus()
    probe = _probe(bus)
    adm = _admission(client, limit=1, bus=bus)
    _d, gate = await _delivered(client, number=1)
    plain = await _story(client, "plain")
    odd = await client.task_create(
        title="odd",
        agent=_AGENT,
        tags=["trigger:story-develop"],
        metadata={"project": _PROJECT, "priority": priority},
    )
    await _hold_in_order(adm, odd, plain)
    await client.task_complete(task_id=gate, agent=_AGENT)

    await adm.wake(_PROJECT)

    assert await _release_sequence(adm, probe) == [odd, plain]


async def test_a_priority_raised_while_held_is_read_at_release() -> None:
    """The operator's lever: mark a waiting story up (Obsidian / lens /
    task_update) and it leaves first — the priority is read when the slot
    frees, not remembered from the refusal."""
    client = FakeLithosClient(agent_id=_AGENT)
    bus = EventBus()
    probe = _probe(bus)
    adm = _admission(client, limit=1, bus=bus)
    _d, gate = await _delivered(client, number=1)
    first, second = await _story(client, "first"), await _story(client, "second")
    await _hold_in_order(adm, first, second)
    await client.task_update(
        task_id=second, agent=_AGENT, metadata={"priority": "high"}
    )
    await client.task_complete(task_id=gate, agent=_AGENT)

    await adm.wake(_PROJECT)

    assert await _release_sequence(adm, probe) == [second, first]


async def test_a_newcomer_queues_behind_the_stories_already_held() -> None:
    client = FakeLithosClient(agent_id=_AGENT)
    bus = EventBus()
    probe = _probe(bus)
    adm = _admission(client, limit=1, bus=bus)
    _d, gate = await _delivered(client, number=1)
    held = await _story(client, "held")
    await _hold_in_order(adm, held)
    await client.task_complete(task_id=gate, agent=_AGENT)
    newcomer = await _story(client, "newcomer")

    verdict = await adm.admit(route=_ROUTE, task_id=newcomer, project=_PROJECT)

    assert not verdict.admitted and verdict.reason == "queued"
    assert _nudged(probe) == [held]
    assert adm.deferred(_PROJECT) == frozenset({held, newcomer})
    assert (await adm.admit(route=_ROUTE, task_id=held, project=_PROJECT)).admitted


async def test_a_newcomer_with_a_higher_priority_takes_the_slot_first() -> None:
    client = FakeLithosClient(agent_id=_AGENT)
    bus = EventBus()
    probe = _probe(bus)
    adm = _admission(client, limit=1, bus=bus)
    _d, gate = await _delivered(client, number=1)
    held = await _story(client, "held")
    await _hold_in_order(adm, held)
    await client.task_complete(task_id=gate, agent=_AGENT)
    urgent = await _prioritised(client, "urgent", "high")

    assert (await adm.admit(route=_ROUTE, task_id=urgent, project=_PROJECT)).admitted

    assert _nudged(probe) == []
    assert adm.deferred(_PROJECT) == frozenset({held})


async def test_a_lone_asker_is_admitted_without_reading_the_queue() -> None:
    client = FakeLithosClient(agent_id=_AGENT)
    adm = _admission(client, limit=1)
    story = await _story(client)

    assert (await adm.admit(route=_ROUTE, task_id=story, project=_PROJECT)).admitted

    assert not client.called("task_get")


async def test_a_held_story_no_longer_open_is_dropped_from_the_order() -> None:
    client = FakeLithosClient(agent_id=_AGENT)
    bus = EventBus()
    probe = _probe(bus)
    adm = _admission(client, limit=1, bus=bus)
    _d, gate = await _delivered(client, number=1)
    first, second = await _story(client, "first"), await _story(client, "second")
    await _hold_in_order(adm, first, second)
    await client.task_cancel(task_id=first, agent=_AGENT)
    await client.task_complete(task_id=gate, agent=_AGENT)

    assert (await adm.admit(route=_ROUTE, task_id=second, project=_PROJECT)).admitted

    assert _nudged(probe) == []
    assert adm.deferred(_PROJECT) == frozenset()


async def test_an_unreadable_head_keeps_its_place_and_holds_the_rest() -> None:
    """Fail closed: a head that cannot be read is not skipped (its priority
    is unknown, its place is not); nothing can be nudged for it, so its
    own re-check sleeper is the retry."""
    client = FakeLithosClient(agent_id=_AGENT)
    bus = EventBus()
    probe = _probe(bus)
    adm = _admission(client, limit=1, bus=bus)
    _d, gate = await _delivered(client, number=1)
    first, second = await _story(client, "first"), await _story(client, "second")
    await _hold_in_order(adm, first, second)
    await client.task_complete(task_id=gate, agent=_AGENT)
    real_task_get = client.task_get

    async def flaky_task_get(**kwargs: Any) -> Any:
        if kwargs["task_id"] == first:
            raise LithosClientError("server_error", "lithos down")
        return await real_task_get(**kwargs)

    client.task_get = flaky_task_get  # type: ignore[method-assign]

    verdict = await adm.admit(route=_ROUTE, task_id=second, project=_PROJECT)

    assert not verdict.admitted and verdict.reason == "queued"
    assert _nudged(probe) == []
    assert adm.deferred(_PROJECT) == frozenset({first, second})


async def test_the_projectless_bucket_is_ordered_the_same_way() -> None:
    client = FakeLithosClient(agent_id=_AGENT)
    bus = EventBus()
    probe = _probe(bus)
    adm = _admission(client, limit=1, bus=bus)
    _d, gate = await _delivered(client, number=1, project=None)
    first = await _story(client, "first", project=None)
    urgent = await client.task_create(
        title="urgent",
        agent=_AGENT,
        tags=["trigger:story-develop"],
        metadata={"priority": "high"},
    )
    await _hold_in_order(adm, first, urgent, project=None)
    await client.task_complete(task_id=gate, agent=_AGENT)

    await adm.wake(None)

    assert await _release_sequence(adm, probe, project=None) == [urgent, first]


async def test_two_stories_of_one_rank_leave_in_first_asked_order() -> None:
    client = FakeLithosClient(agent_id=_AGENT)
    bus = EventBus()
    probe = _probe(bus)
    adm = _admission(client, limit=1, bus=bus)
    _d, gate = await _delivered(client, number=1)
    created = [await _prioritised(client, t, "high") for t in ("made first", "second")]
    await _hold_in_order(adm, *reversed(created))
    await client.task_complete(task_id=gate, agent=_AGENT)

    await adm.wake(_PROJECT)

    assert await _release_sequence(adm, probe) == list(reversed(created))


@pytest.mark.parametrize("priority", list(PRIORITY_EMOJI))
async def test_every_priority_in_the_vocabulary_is_placed(priority: str) -> None:
    client = FakeLithosClient(agent_id=_AGENT)
    bus = EventBus()
    probe = _probe(bus)
    adm = _admission(client, limit=1, bus=bus)
    _d, gate = await _delivered(client, number=1)
    marked = await _prioritised(client, "marked", priority)
    plain = await _story(client, "plain")
    await _hold_in_order(adm, marked, plain)
    await client.task_complete(task_id=gate, agent=_AGENT)

    await adm.wake(_PROJECT)

    jumps = priority in ("medium", "high", "highest")
    expected = [marked, plain] if jumps else [plain, marked]
    assert await _release_sequence(adm, probe) == expected


# ── review round 2: every transition that frees a place re-nudges ────────


async def test_a_head_that_leaves_the_queue_wakes_the_stories_behind_it() -> None:
    """A blocks edge lands on the head while it waits: its runner finds it
    not ready and forgets it. The slot is free and the next story must not
    wait for its sleeper (up to 15 min) to find out."""
    client = FakeLithosClient(agent_id=_AGENT)
    bus = EventBus()
    probe = _probe(bus)
    adm = _admission(client, limit=1, bus=bus)
    _d, gate = await _delivered(client, number=1)
    first, second = await _story(client, "first"), await _story(client, "second")
    await _hold_in_order(adm, first, second)
    await client.task_complete(task_id=gate, agent=_AGENT)
    late = await adm.admit(route=_ROUTE, task_id=second, project=_PROJECT)
    assert late.reason == "queued" and _nudged(probe) == [first]

    await adm.forget(first)

    assert _nudged(probe) == [second]
    assert adm.deferred(_PROJECT) == frozenset({second})
    assert (await adm.admit(route=_ROUTE, task_id=second, project=_PROJECT)).admitted


async def test_a_run_that_ends_without_a_gate_wakes_the_bucket() -> None:
    """A failed / interrupted run frees its slot with no gate event for the
    waker to see; the release itself must nudge the next story."""
    client = FakeLithosClient(agent_id=_AGENT)
    bus = EventBus()
    probe = _probe(bus)
    adm = _admission(client, limit=1, bus=bus)
    first, second = await _story(client, "first"), await _story(client, "second")
    assert (await adm.admit(route=_ROUTE, task_id=first, project=_PROJECT)).admitted
    await _hold_in_order(adm, second)

    await adm.release(first, route=_ROUTE)

    assert _nudged(probe) == [second]


async def test_a_raw_transport_error_on_one_story_does_not_lose_the_sweep() -> None:
    """The client re-raises the raw transport exception once its reconnects
    are spent (review #368 F3); one unreadable story keeps its place and
    the other entitled story still goes out."""
    client = FakeLithosClient(agent_id=_AGENT)
    bus = EventBus()
    probe = _probe(bus)
    adm = _admission(client, limit=2, bus=bus)
    gates = [(await _delivered(client, number=n))[1] for n in (1, 2)]
    first, second = await _story(client, "first"), await _story(client, "second")
    await _hold_in_order(adm, first, second)
    for gate in gates:
        await client.task_complete(task_id=gate, agent=_AGENT)
    real_task_get = client.task_get

    async def broken_task_get(**kwargs: Any) -> Any:
        if kwargs["task_id"] == first:
            raise RuntimeError("connection reset")
        return await real_task_get(**kwargs)

    client.task_get = broken_task_get  # type: ignore[method-assign]

    assert await adm.wake(_PROJECT) == 1

    assert _nudged(probe) == [second]
    assert adm.deferred(_PROJECT) == frozenset({first, second})


async def test_a_flapping_read_keeps_the_last_seen_rank() -> None:
    """Ranking an unreadable story at the default would move a high-priority
    head behind the default-priority story on every failed read — and each
    swap nudges the other, with the slot idle. The last-seen rank holds."""
    client = FakeLithosClient(agent_id=_AGENT)
    bus = EventBus()
    probe = _probe(bus)
    adm = _admission(client, limit=1, bus=bus)
    _d, gate = await _delivered(client, number=1)
    plain = await _story(client, "plain")
    urgent = await _prioritised(client, "urgent", "high")
    await _hold_in_order(adm, plain, urgent)
    await client.task_complete(task_id=gate, agent=_AGENT)
    await adm.wake(_PROJECT)  # both read once: urgent is the head
    assert _nudged(probe) == [urgent]
    real_task_get = client.task_get

    async def flaky_task_get(**kwargs: Any) -> Any:
        if kwargs["task_id"] == urgent:
            raise LithosClientError("server_error", "lithos down")
        return await real_task_get(**kwargs)

    client.task_get = flaky_task_get  # type: ignore[method-assign]

    verdict = await adm.admit(route=_ROUTE, task_id=plain, project=_PROJECT)

    assert not verdict.admitted and verdict.reason == "queued"
    assert _nudged(probe) == []  # the head cannot be nudged; its sleeper re-asks


async def test_every_free_slot_is_filled_when_the_limit_allows() -> None:
    """The head rule binds only while askers outnumber free slots: with two
    slots free, the second in order is admitted too, not queued."""
    client = FakeLithosClient(agent_id=_AGENT)
    bus = EventBus()
    probe = _probe(bus)
    adm = _admission(client, limit=2, bus=bus)
    gates = [(await _delivered(client, number=n))[1] for n in (1, 2)]
    first, second, third = [
        await _story(client, t) for t in ("first", "second", "third")
    ]
    await _hold_in_order(adm, first, second, third)
    for gate in gates:
        await client.task_complete(task_id=gate, agent=_AGENT)

    late = await adm.admit(route=_ROUTE, task_id=third, project=_PROJECT)
    assert late.reason == "queued" and _nudged(probe) == [first, second]
    assert (await adm.admit(route=_ROUTE, task_id=second, project=_PROJECT)).admitted
    assert _nudged(probe) == [first]  # the slot left goes to the next in order
    assert (await adm.admit(route=_ROUTE, task_id=first, project=_PROJECT)).admitted
    assert _nudged(probe) == []
    assert (
        await adm.admit(route=_ROUTE, task_id=third, project=_PROJECT)
    ).reason == "limit"


async def test_an_unlimited_limit_under_a_total_cap_admits_every_free_slot() -> None:
    client = FakeLithosClient(agent_id=_AGENT)
    bus = EventBus()
    probe = _probe(bus)
    adm = _admission(client, limit=0, total=2, bus=bus)
    gates = [(await _delivered(client, number=n))[1] for n in (1, 2)]
    first, second = await _story(client, "first"), await _story(client, "second")
    for story in (first, second):
        verdict = await adm.admit(route=_ROUTE, task_id=story, project=_PROJECT)
        assert verdict.reason == "total_cap"
    for gate in gates:
        await client.task_complete(task_id=gate, agent=_AGENT)

    assert (await adm.admit(route=_ROUTE, task_id=second, project=_PROJECT)).admitted

    assert _nudged(probe) == [first]


async def test_a_story_whose_run_ended_keeps_its_place_when_it_asks_again() -> None:
    """A usage-limit resume re-asks admission (T10). It asked first, so it
    is first — not a newcomer behind everything that queued while it ran."""
    client = FakeLithosClient(agent_id=_AGENT)
    bus = EventBus()
    probe = _probe(bus)
    adm = _admission(client, limit=1, bus=bus)
    resumed, waiting = await _story(client, "resumed"), await _story(client, "waiting")
    assert (await adm.admit(route=_ROUTE, task_id=resumed, project=_PROJECT)).admitted
    await _hold_in_order(adm, waiting)
    await adm.release(resumed, route=_ROUTE)  # interrupted: the slot frees
    assert _nudged(probe) == [waiting]

    assert (await adm.admit(route=_ROUTE, task_id=resumed, project=_PROJECT)).admitted

    assert adm.deferred(_PROJECT) == frozenset({waiting})
    assert (
        await adm.admit(route=_ROUTE, task_id=waiting, project=_PROJECT)
    ).reason == ("limit")


async def test_a_story_admitted_under_a_new_project_leaves_no_phantom_head() -> None:
    """A story re-homed to another project while held would otherwise stay
    the old bucket's head forever — never asking there, never forgotten."""
    client = FakeLithosClient(agent_id=_AGENT)
    bus = EventBus()
    probe = _probe(bus)
    adm = _admission(client, limit=1, bus=bus)
    _d, gate = await _delivered(client, number=1)
    moved, stayed = await _story(client, "moved"), await _story(client, "stayed")
    await _hold_in_order(adm, moved, stayed)
    await client.task_update(task_id=moved, agent=_AGENT, metadata={"project": "other"})
    assert (await adm.admit(route=_ROUTE, task_id=moved, project="other")).admitted
    await client.task_complete(task_id=gate, agent=_AGENT)

    assert (await adm.admit(route=_ROUTE, task_id=stayed, project=_PROJECT)).admitted

    assert _nudged(probe) == []
    assert adm.deferred(_PROJECT) == frozenset()


# ── review round 3: a head nobody can dispatch, and wakes that cost nothing ──


@pytest.mark.parametrize("new_tags", [["parked"], ["trigger:docs"]])
async def test_a_held_wait_its_route_no_longer_matches_is_dropped(
    new_tags: list[str],
) -> None:
    """Re-tagging a held story — to park it, or onto ANOTHER route — means
    this route's runner never receives its nudge, so the wait never asks
    and never steps aside: as head it would stop the bucket. The check is
    per route (review round 3): another route matching is no help."""
    client = FakeLithosClient(agent_id=_AGENT)
    bus = EventBus()
    probe = _probe(bus)
    bus.subscribe(  # the other route's runner: it would hear the nudge
        event_types=("lithos.task.updated",),
        match={"tags": ["trigger:docs"]},
        name="route-runner-docs",
    )
    adm = _admission(client, limit=1, bus=bus)
    _d, gate = await _delivered(client, number=1)
    parked, other = await _story(client, "parked"), await _story(client, "other")
    await _hold_in_order(adm, parked, other)
    await client.task_update(task_id=parked, agent=_AGENT, tags=new_tags)
    await client.task_complete(task_id=gate, agent=_AGENT)

    verdict = await adm.admit(route=_ROUTE, task_id=other, project=_PROJECT, tags=_TAGS)
    assert verdict.admitted

    assert _nudged(probe) == []
    assert adm.deferred(_PROJECT) == frozenset()


async def test_a_story_that_steps_aside_keeps_its_place() -> None:
    """A Lithos blip makes every runner's readiness read fail; each drops
    its wait. Recovery must not reorder the queue — that was the incident
    (ADR 0012 context) — so the story that asked first is still first."""
    client = FakeLithosClient(agent_id=_AGENT)
    bus = EventBus()
    probe = _probe(bus)
    adm = _admission(client, limit=1, bus=bus)
    _d, gate = await _delivered(client, number=1)
    first, second = await _story(client, "first"), await _story(client, "second")
    await _hold_in_order(adm, first, second)
    await adm.forget(first, route=_ROUTE)
    await adm.forget(second, route=_ROUTE)
    assert adm.deferred(_PROJECT) == frozenset()
    await _hold_in_order(adm, second, first)  # recovery: second's sleeper first
    await client.task_complete(task_id=gate, agent=_AGENT)

    await adm.wake(_PROJECT)

    assert _nudged(probe) == [first]


async def test_forgetting_one_routes_wait_keeps_the_other_routes() -> None:
    """Readiness is route-scoped (the frontier page is the route's): route
    B's inconclusive read must not cost route A its wait."""
    client = FakeLithosClient(agent_id=_AGENT)
    adm = _admission(client, limit=1)
    await _delivered(client, number=1)
    story = await _story(client)
    for route in ("a", "b"):
        held = await adm.admit(route=route, task_id=story, project=_PROJECT)
        assert held.reason == "limit"

    await adm.forget(story, route="b")

    assert adm.deferred(_PROJECT) == frozenset({story})


async def test_an_unreadable_context_doc_nudges_every_held_story() -> None:
    """The headroom cannot be counted, so the wake cannot know how many are
    entitled: every held story is told and each ask decides for itself."""
    client = FakeLithosClient(agent_id=_AGENT)
    bus = EventBus()
    probe = _probe(bus)
    adm = _admission(client, limit=1, bus=bus)
    _d, gate = await _delivered(client, number=1)
    first, second = await _story(client, "first"), await _story(client, "second")
    await _hold_in_order(adm, first, second)
    await client.task_complete(task_id=gate, agent=_AGENT)

    async def failing_note_read(**kwargs: Any) -> Any:
        raise LithosClientError("server_error", "lithos down")

    client.note_read = failing_note_read  # type: ignore[method-assign]
    _context_doc(client, {})  # the doc exists, and cannot be read

    assert await adm.wake(_PROJECT) == 2

    assert _nudged(probe) == [first, second]
    assert (await adm.admit(route=_ROUTE, task_id=first, project=_PROJECT)).reason == (
        "unreadable"
    )


async def test_a_delivered_run_ending_does_not_nudge_a_full_bucket() -> None:
    """Delivery is the commonest release, and its slot is now its gate's:
    nudging the held stories would only buy each a `limit` refusal."""
    client = FakeLithosClient(agent_id=_AGENT)
    bus = EventBus()
    probe = _probe(bus)
    adm = _admission(client, limit=1, bus=bus)
    delivered = await _story(client, "delivered")
    assert (await adm.admit(route=_ROUTE, task_id=delivered, project=_PROJECT)).admitted
    waiting = await _story(client, "waiting")
    await _hold_in_order(adm, waiting)
    await create_pr_gate(
        client,
        story_id=delivered,
        story_title="delivered",
        pr_url="https://github.com/agent-lore/lithos-lens/pull/1",
        project=_PROJECT,
        agent=_AGENT,
    )

    await adm.release(delivered, route=_ROUTE)

    assert _nudged(probe) == []
    assert adm.deferred(_PROJECT) == frozenset({waiting})


async def test_a_head_leaving_a_full_bucket_does_not_nudge() -> None:
    client = FakeLithosClient(agent_id=_AGENT)
    bus = EventBus()
    probe = _probe(bus)
    adm = _admission(client, limit=1, bus=bus)
    await _delivered(client, number=1)  # the gate stays open
    first, second = await _story(client, "first"), await _story(client, "second")
    await _hold_in_order(adm, first, second)

    await adm.forget(first)

    assert _nudged(probe) == []
    assert adm.deferred(_PROJECT) == frozenset({second})


async def test_one_story_on_two_routes_takes_two_slots_and_no_more() -> None:
    """The order is over (route, story) waits: a story two PR-producing
    routes hold is two runs and two slots, and the story behind it is
    entitled only to a third."""
    client = FakeLithosClient(agent_id=_AGENT)
    bus = EventBus()
    probe = _probe(bus)
    adm = _admission(client, limit=2, bus=bus)
    gates = [(await _delivered(client, number=n))[1] for n in (1, 2)]
    shared, other = await _story(client, "shared"), await _story(client, "other")
    for route in ("a", "b"):
        assert (
            await adm.admit(route=route, task_id=shared, project=_PROJECT)
        ).reason == ("limit")
    await _hold_in_order(adm, other)
    for gate in gates:
        await client.task_complete(task_id=gate, agent=_AGENT)

    assert (await adm.admit(route="a", task_id=shared, project=_PROJECT)).admitted
    assert _nudged(probe) == [shared]  # for route b's wait, not for `other`
    assert (await adm.admit(route="b", task_id=shared, project=_PROJECT)).admitted
    assert _nudged(probe) == []
    held = await adm.admit(route=_ROUTE, task_id=other, project=_PROJECT)
    assert held.reason == "limit"


async def test_release_and_forget_never_raise() -> None:
    """Both sit on the runner's dispatch path (`release` in a `finally`):
    a wake that blows up must not replace the run's own outcome."""
    client = FakeLithosClient(agent_id=_AGENT)
    adm = _admission(client, limit=1)
    running, waiting = await _story(client, "running"), await _story(client, "waiting")
    assert (await adm.admit(route=_ROUTE, task_id=running, project=_PROJECT)).admitted
    await _hold_in_order(adm, waiting)

    async def broken(**kwargs: Any) -> Any:
        raise RuntimeError("connection reset")

    client.task_list = broken  # type: ignore[method-assign]
    client.task_get = broken  # type: ignore[method-assign]

    await adm.release(running, route=_ROUTE)
    await adm.forget(waiting)

    assert adm.deferred(_PROJECT) == frozenset()


# ── #372: a terminal story whose delivered PR is still open ──────────────────


@pytest.mark.parametrize("terminal", ["completed", "cancelled"])
async def test_a_gate_whose_story_is_terminal_counts_against_neither_cap(
    terminal: str,
) -> None:
    """#372: the story went terminal (its work landed via another PR and the
    issue mirror completed it) while the delivered PR stayed OPEN, so its
    `pr` gate is still live. That PR is the operator's now, not loom's
    work-in-progress: it holds no admission slot — under the limit and under
    the total cap alike — or one such gate would stall the project's whole
    story-develop route until a human noticed."""
    client = FakeLithosClient(agent_id=_AGENT)
    done, _gate = await _delivered(client, number=1)
    if terminal == "completed":
        await client.task_complete(task_id=done)
    else:
        await client.task_cancel(task_id=done)
    story = await _story(client)
    adm = _admission(client, limit=1, total=1)

    verdict = await adm.admit(route=_ROUTE, task_id=story, project=_PROJECT)

    assert verdict.admitted
    assert verdict.open_gates == 0  # the dead-story gate is not counted


async def test_a_gate_whose_waiter_cannot_be_read_still_counts() -> None:
    # fail closed, as for the escalation read: a gate whose story Lithos
    # cannot return ("gone" is not "done") keeps its slot
    client = FakeLithosClient(agent_id=_AGENT)
    done, _gate = await _delivered(client, number=1)
    real_get = client.task_get

    async def get(**kw):
        if kw.get("task_id") == done:
            return None
        return await real_get(**kw)

    client.task_get = get  # type: ignore[method-assign]
    story = await _story(client)
    adm = _admission(client, limit=1)

    verdict = await adm.admit(route=_ROUTE, task_id=story, project=_PROJECT)

    assert not verdict.admitted and verdict.reason == "limit"
    assert verdict.open_gates == 1


async def test_a_waiter_read_that_fails_keeps_its_gate_counted() -> None:
    # the unreadable arm (opus round 1, L3): a Lithos error on the story read
    # is not "unreadable admission" — the gate simply keeps its slot
    client = FakeLithosClient(agent_id=_AGENT)
    done, _gate = await _delivered(client, number=1)
    real_get = client.task_get

    async def get(**kw):
        if kw.get("task_id") == done:
            raise LithosClientError("server_error", "lithos down")
        return await real_get(**kw)

    client.task_get = get  # type: ignore[method-assign]
    story = await _story(client)
    adm = _admission(client, limit=1)

    verdict = await adm.admit(route=_ROUTE, task_id=story, project=_PROJECT)

    assert not verdict.admitted and verdict.reason == "limit"
    assert verdict.open_gates == 1
