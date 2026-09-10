"""The shared readiness classifier (PR #352 review F1 + F2).

``task_ready`` has no per-task filter and no pagination, so a membership test
on a page can be denied by a FULL page. Both the route-runner's dispatch guard
and the pr-gate resolver's recovery need the same answer, so there is one
classifier: narrow to the task's OWN project + tags (a page that cannot drop
the task itself), and when that ready page is still full use the other half
of the partition — open work is ready XOR blocked, so a COMPLETE blocked page
that lacks the task settles it as ready. Only both pages full is undetermined.
"""

from __future__ import annotations

from lithos_loom.subscriptions.dispatch_guards import (
    classify_readiness,
    on_ready_frontier,
)
from tests.support import FakeLithosClient

_TAGS = ["trigger:story-develop", "lens"]
_LIMIT = 3


async def _candidate(client: FakeLithosClient, *, blocked: bool) -> str:
    story = await client.task_create(title="S", tags=_TAGS, metadata={"project": "p"})
    dep = await client.task_create(title="C", tags=_TAGS, metadata={"project": "p"})
    await client.task_edge_upsert(from_task_id=story, to_task_id=dep, type="blocks")
    if not blocked:
        await client.task_complete(task_id=story)
    return dep


async def _fill(client: FakeLithosClient, n: int, *, blocked: bool = False) -> None:
    """*n* same-scope tasks ahead of the candidate on the page."""
    for i in range(n):
        t = await client.task_create(
            title=f"filler-{i}", tags=_TAGS, metadata={"project": "p"}
        )
        if blocked:
            b = await client.task_create(title=f"blocker-{i}")
            await client.task_edge_upsert(from_task_id=b, to_task_id=t, type="blocks")


async def test_a_full_ready_page_is_settled_by_a_complete_blocked_page() -> None:
    client = FakeLithosClient()
    await _fill(client, _LIMIT)
    dep = await _candidate(client, blocked=False)
    task = await client.task_get(task_id=dep)
    assert task is not None
    # the narrowed ready page is full and does not carry the candidate…
    ready = await client.task_ready(project="p", tags=_TAGS, limit=_LIMIT)
    assert len(ready) == _LIMIT and dep not in {t.id for t in ready}

    assert await classify_readiness(client, task, limit=_LIMIT) is True


async def test_a_blocked_candidate_is_read_off_the_blocked_page() -> None:
    client = FakeLithosClient()
    await _fill(client, _LIMIT)
    dep = await _candidate(client, blocked=True)
    task = await client.task_get(task_id=dep)
    assert task is not None
    assert await classify_readiness(client, task, limit=_LIMIT) is False


async def test_both_pages_full_is_undetermined() -> None:
    client = FakeLithosClient()
    await _fill(client, _LIMIT)
    await _fill(client, _LIMIT, blocked=True)
    dep = await _candidate(client, blocked=True)
    task = await client.task_get(task_id=dep)
    assert task is not None
    assert await classify_readiness(client, task, limit=_LIMIT) is None


async def test_a_gate_or_epic_is_never_ready_work() -> None:
    client = FakeLithosClient()
    gate = await client.task_create(
        title="G", task_type="gate", metadata={"gate_type": "human"}
    )
    task = await client.task_get(task_id=gate)
    assert task is not None
    assert await classify_readiness(client, task, limit=_LIMIT) is False
    assert client.calls_to("task_ready") == []


async def test_the_pages_are_narrowed_to_the_candidates_own_scope() -> None:
    client = FakeLithosClient()
    dep = await _candidate(client, blocked=False)
    task = await client.task_get(task_id=dep)
    assert task is not None
    assert await classify_readiness(client, task, limit=_LIMIT) is True
    (call,) = client.calls_to("task_ready")
    assert call["project"] == "p" and sorted(call["tags"]) == sorted(_TAGS)


async def test_frontier_guard_falls_back_to_the_own_scope_on_a_full_page() -> None:
    """The runner's fast path asks by the ROUTE's tags; when that page is
    full it fetches the task and classifies by the task's own (narrower)
    scope instead of dropping the event."""
    client = FakeLithosClient()
    for i in range(_LIMIT):  # route-tag-only work crowds the route's page
        await client.task_create(
            title=f"route-{i}",
            tags=["trigger:story-develop"],
            metadata={"project": "p"},
        )
    dep = await _candidate(client, blocked=False)
    verdict = await on_ready_frontier(
        client,
        task_id=dep,
        tags=("trigger:story-develop",),
        metadata={"project": "p"},
        route="story-develop",
        limit=_LIMIT,
    )
    assert verdict is True
    calls = client.calls_to("task_ready")
    assert [sorted(c["tags"]) for c in calls] == [
        ["trigger:story-develop"],
        sorted(_TAGS),
    ]


async def test_a_transient_read_error_is_undetermined_not_an_exception() -> None:
    """PR #352 review round 2: a failed `task_ready` / `task_get` used to
    escape to the runner's loop, which logs and moves on — the event, and
    the nudge it carried, gone. It is an undetermined answer instead, which
    the runner re-checks."""
    from lithos_loom.errors import LithosClientError

    client = FakeLithosClient()
    dep = await _candidate(client, blocked=False)

    async def boom(**kwargs):
        raise LithosClientError("server_error", "boom")

    client.task_ready = boom  # type: ignore[method-assign]
    verdict = await on_ready_frontier(
        client,
        task_id=dep,
        tags=("trigger:story-develop",),
        metadata={"project": "p"},
        route="story-develop",
        limit=_LIMIT,
    )
    assert verdict is None


async def test_an_exhausted_transport_failure_is_undetermined_too() -> None:
    """The client re-raises the RAW last exception once its transport retries
    are spent — not a LithosClientError. Same answer: undetermined."""
    client = FakeLithosClient()
    dep = await _candidate(client, blocked=False)

    async def dead(**kwargs):
        raise RuntimeError("SSE stream closed")

    client.task_ready = dead  # type: ignore[method-assign]
    verdict = await on_ready_frontier(
        client,
        task_id=dep,
        tags=("trigger:story-develop",),
        metadata={"project": "p"},
        route="story-develop",
        limit=_LIMIT,
    )
    assert verdict is None
