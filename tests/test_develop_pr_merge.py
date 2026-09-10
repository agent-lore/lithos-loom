"""Tests for ``lithos_loom.subscriptions._develop_pr_merge`` (#87).

The reconcile polls a delivered PR's merge state and acts on the open Lithos
task: merged → complete; closed-unmerged / deleted → one-shot
``[DeliveredPRClosed]`` finding + leave open; still-open → no-op. A
``develop_pr_merge_state`` + ``develop_pr_merge_url`` marker scoped to the
resolved PR de-dups across sweeps while letting a replacement PR recover.
GitHub + Lithos are stubbed; the ``task`` is a minimal id + metadata object.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from lithos_loom.errors import LithosClientError
from lithos_loom.gates import create_pr_gate
from lithos_loom.github_client import (
    GitHubError,
    PullRequest,
    PullRequestReview,
)
from lithos_loom.lithos_client import Task
from lithos_loom.subscriptions import SubscriptionContext
from lithos_loom.subscriptions._develop_pr_merge import (
    DELIVERED_PR_CLOSED,
    GATE_RESOLVED,
    MERGE_STATE_KEY,
    MERGE_STATE_URL_KEY,
    reconcile_pr_gate,
)
from lithos_loom.subscriptions._develop_pr_nudge import (
    NUDGE_RECOVERED_KEY,
)
from lithos_loom.subscriptions.dispatch_guards import READY_QUERY_LIMIT
from tests.support import FakeLithosClient

_PR_URL = "https://github.com/agent-lore/lithos-loom/pull/7"


async def _get(client: FakeLithosClient, task_id: str) -> Task:
    """Fetch a task, asserting it still exists (keeps callers non-optional)."""
    task = await client.task_get(task_id=task_id)
    assert task is not None
    return task


def _ctx(lithos: Any) -> SubscriptionContext:
    return SubscriptionContext(
        lithos=lithos,
        logger=logging.getLogger("test-develop-pr-merge"),
        agent_id="lithos-loom-agent",
    )


def _pr(*, state: str, merged: bool, sha: str | None = "abc123") -> PullRequest:
    return PullRequest(
        repo="agent-lore/lithos-loom",
        number=7,
        state=state,
        merged=merged,
        merged_at=datetime(2026, 6, 13, tzinfo=UTC) if merged else None,
        merge_commit_sha=sha if merged else None,
    )


# ── reconcile_pr_gate (Epic H, US12/US13) ──────────────────────────────


async def _gate_with_story(
    client: FakeLithosClient, *, pr_url: str = _PR_URL
) -> tuple[str, Any]:
    """Create a story + its pr gate; return (story_id, gate task record)."""
    story = await client.task_create(title="US7", metadata={"project": "p"})
    gate_id = await create_pr_gate(
        client,
        story_id=story,
        story_title="US7",
        pr_url=pr_url,
        project="p",
        agent="a",
    )
    gate = await client.task_get(task_id=gate_id)
    return story, gate


def _github(pr: PullRequest | None, *, base_tip: str | None = None) -> AsyncMock:
    github = AsyncMock()
    github.get_pull_request.return_value = pr
    # the live base tip the sweep resolves; defaults to the PR's own base sha
    # so a test that doesn't care sees no base move
    github.get_branch_tip.return_value = (
        base_tip if base_tip is not None else (pr.base_sha if pr else None)
    )
    return github


async def test_gate_merged_completes_story_and_gate_and_posts_finding() -> None:
    client = FakeLithosClient(agent_id="a")
    story, gate = await _gate_with_story(client)
    github = _github(_pr(state="closed", merged=True))

    outcome = await reconcile_pr_gate(gate, github, _ctx(client))

    assert outcome == "merged"
    assert (await _get(client, story)).status == "completed"
    assert (await _get(client, gate.id)).status == "completed"
    findings = [f["summary"] for f in client._findings]
    assert any(s.startswith(GATE_RESOLVED) and story in s for s in findings)


async def test_gate_merged_story_first_ordering() -> None:
    """Story is completed BEFORE the gate: gate-first would momentarily ready a
    still-tagged story and, on a completion failure, strand it → duplicate PR."""
    client = FakeLithosClient(agent_id="a")
    story, gate = await _gate_with_story(client)
    completed_order: list[str] = []
    original = client.task_complete

    async def _spy(**kwargs: Any) -> Any:
        completed_order.append(kwargs["task_id"])
        return await original(**kwargs)

    client.task_complete = _spy  # type: ignore[method-assign]
    github = _github(_pr(state="closed", merged=True))
    await reconcile_pr_gate(gate, github, _ctx(client))

    assert completed_order == [story, gate.id]


async def test_gate_merged_swallows_already_completed_story() -> None:
    """A race with the issue close-mirror (story already completed) converges:
    the story completion swallows task_not_found and the gate still resolves."""
    client = FakeLithosClient(agent_id="a")
    story, gate = await _gate_with_story(client)
    await client.task_complete(task_id=story)  # mirror got there first

    outcome = await reconcile_pr_gate(
        gate, _github(_pr(state="closed", merged=True)), _ctx(client)
    )

    assert outcome == "merged"
    assert (await _get(client, gate.id)).status == "completed"


async def test_gate_merged_story_completion_failure_is_not_counted_resolved() -> None:
    """A transient story-completion failure on the merged path: the gate is left
    OPEN for the next sweep, and the outcome is `error` (not `merged`) so the
    sweep summary never reports an un-landed resolution as resolved."""
    client = FakeLithosClient(agent_id="a")
    story, gate = await _gate_with_story(client)
    original = client.task_complete

    async def _fail_story(**kwargs: Any) -> Any:
        if kwargs["task_id"] == story:
            raise LithosClientError("server_error", "boom")
        return await original(**kwargs)

    client.task_complete = _fail_story  # type: ignore[method-assign]

    outcome = await reconcile_pr_gate(
        gate, _github(_pr(state="closed", merged=True)), _ctx(client)
    )

    assert outcome == "error"
    # Gate + story both still open; nothing marked → re-polled next sweep.
    assert (await _get(client, gate.id)).status == "open"
    assert (await _get(client, story)).status == "open"
    assert MERGE_STATE_KEY not in (await _get(client, gate.id)).metadata
    assert not any(f["summary"].startswith(GATE_RESOLVED) for f in client._findings)


def _filler(task_id: str) -> Task:
    """A ready task that is none of our business — padding for a frontier page."""
    return Task(
        id=task_id, title=task_id, status="open", tags=(), metadata={}, claims=()
    )


async def _blocked_dependent(client: FakeLithosClient, blocker: str) -> str:
    """A second tagged story the first one ``blocks`` — the T2-slice shape."""
    dependent = await client.task_create(
        title="US8", tags=["trigger:story-develop"], metadata={"project": "p"}
    )
    await client.task_edge_upsert(
        from_task_id=blocker, to_task_id=dependent, type="blocks"
    )
    return dependent


async def test_gate_merged_nudges_the_tasks_the_story_unblocked() -> None:
    """#350: the story completion names its newly-unblocked dependents, and each
    gets a no-op ``task_update`` so Lithos emits the ``task.updated`` the
    route-runner child (no IPC to this one) dispatches off. Without the nudge a
    tagged, now-ready dependent waits for a daemon restart."""
    client = FakeLithosClient(agent_id="a")
    story, gate = await _gate_with_story(client)
    dependent = await _blocked_dependent(client, story)

    outcome = await reconcile_pr_gate(
        gate, _github(_pr(state="closed", merged=True)), _ctx(client)
    )

    assert outcome == "merged"
    nudges = [c for c in client.calls_to("task_update") if c["task_id"] == dependent]
    assert [c["metadata"] for c in nudges] == [{}]  # a no-op write, stamp only
    # …and the nudge is worth sending: Lithos now offers the dependent as ready.
    assert dependent in [t.id for t in await client.task_ready()]


async def test_gate_merged_nudges_nothing_when_no_dependent_was_released() -> None:
    """The common case — a story with no dependents — writes nothing extra."""
    client = FakeLithosClient(agent_id="a")
    story, gate = await _gate_with_story(client)
    before = len(client.calls_to("task_update"))

    await reconcile_pr_gate(
        gate, _github(_pr(state="closed", merged=True)), _ctx(client)
    )

    assert len(client.calls_to("task_update")) == before


async def test_gate_merged_interrupted_mid_nudge_is_retried_next_sweep() -> None:
    """The nudge lands BEFORE the gate's terminal transition, so a sweep killed
    mid-batch is retryable: the gate is still open, and the second sweep — whose
    story completion can no longer name anybody — recovers the dependents from
    the story's `blocks` edges."""
    client = FakeLithosClient(agent_id="a")
    story, gate = await _gate_with_story(client)
    dependent = await _blocked_dependent(client, story)

    async def _killed(**kwargs: Any) -> Any:
        raise asyncio.CancelledError  # the watcher process is taken down

    client.task_update = _killed  # type: ignore[method-assign]
    with pytest.raises(asyncio.CancelledError):
        await reconcile_pr_gate(
            gate, _github(_pr(state="closed", merged=True)), _ctx(client)
        )

    # Story completed, gate NOT completed → the sweep still has the gate to
    # retry from, and the dependent has not been nudged.
    assert (await _get(client, story)).status == "completed"
    assert (await _get(client, gate.id)).status == "open"
    assert not [c for c in client.calls_to("task_update") if c["task_id"] == dependent]

    del client.task_update  # back to the real implementation
    outcome = await reconcile_pr_gate(
        await _get(client, gate.id),
        _github(_pr(state="closed", merged=True)),
        _ctx(client),
    )

    assert outcome == "merged"
    nudges = [c for c in client.calls_to("task_update") if c["task_id"] == dependent]
    assert [c["metadata"] for c in nudges] == [{}]
    assert (await _get(client, gate.id)).status == "completed"


async def test_gate_merged_unreadable_dependents_defer_the_gate() -> None:
    """The recovery read must not fail silently: "could not read the edges" is
    not "there are no edges". A transient `task_edge_list` failure on the retry
    sweep leaves the gate OPEN (outcome `error`), so a third sweep can still
    nudge — completing the gate here would put the dependent beyond reach."""
    client = FakeLithosClient(agent_id="a")
    story, gate = await _gate_with_story(client)
    dependent = await _blocked_dependent(client, story)
    await client.task_complete(task_id=story)  # an earlier sweep died mid-nudge
    original = client.task_edge_list

    async def _fail_blocks_read(**kwargs: Any) -> Any:
        if kwargs.get("types") == ["blocks"]:
            raise LithosClientError("server_error", "boom")
        return await original(**kwargs)

    client.task_edge_list = _fail_blocks_read  # type: ignore[method-assign]

    outcome = await reconcile_pr_gate(
        gate, _github(_pr(state="closed", merged=True)), _ctx(client)
    )

    assert outcome == "error"
    assert (await _get(client, gate.id)).status == "open"
    assert not [c for c in client.calls_to("task_update") if c["task_id"] == dependent]

    # Lithos recovers → the next sweep resolves the gate and nudges after all.
    del client.task_edge_list  # back to the real implementation
    outcome = await reconcile_pr_gate(
        await _get(client, gate.id),
        _github(_pr(state="closed", merged=True)),
        _ctx(client),
    )

    assert outcome == "merged"
    assert (await _get(client, gate.id)).status == "completed"
    nudges = [c for c in client.calls_to("task_update") if c["task_id"] == dependent]
    assert [c["metadata"] for c in nudges] == [{}]


async def test_gate_merged_recovery_nudges_only_the_dependents_it_released() -> None:
    """The graph fallback OVER-approximates: a story's outgoing ``blocks`` edges
    name every dependent, not the subset the lost ``unblocked`` response named.
    Shape: A blocks C and D, and B also blocks D — completing A releases only C.
    A sweep killed mid-nudge must not, on retry, write to D: the bump would
    consume the #339 bootstrap-replay guard's evidence on a task that is not
    even ready. So the recovered ids are intersected with Lithos's ready
    frontier before anything is written."""
    client = FakeLithosClient(agent_id="a")
    story, gate = await _gate_with_story(client)
    released = await _blocked_dependent(client, story)  # C — only A blocks it
    still_blocked = await _blocked_dependent(client, story)  # D — A and B do
    sibling = await client.task_create(title="US9", metadata={"project": "p"})
    await client.task_edge_upsert(
        from_task_id=sibling, to_task_id=still_blocked, type="blocks"
    )

    async def _killed(**kwargs: Any) -> Any:
        raise asyncio.CancelledError  # the watcher process is taken down

    client.task_update = _killed  # type: ignore[method-assign]
    with pytest.raises(asyncio.CancelledError):
        await reconcile_pr_gate(
            gate, _github(_pr(state="closed", merged=True)), _ctx(client)
        )
    del client.task_update  # back to the real implementation

    outcome = await reconcile_pr_gate(
        await _get(client, gate.id),
        _github(_pr(state="closed", merged=True)),
        _ctx(client),
    )

    assert outcome == "merged"
    nudges = [c for c in client.calls_to("task_update") if c["task_id"] == released]
    assert [c["metadata"] for c in nudges] == [{}]
    assert not [
        c for c in client.calls_to("task_update") if c["task_id"] == still_blocked
    ]
    # D is still blocked by B, so it must still be off the ready frontier.
    assert still_blocked not in [t.id for t in await client.task_ready()]


async def test_gate_merged_recovery_survives_a_saturated_unrelated_frontier() -> None:
    """Readiness is asked about the CANDIDATE, never about the instance. The
    github-issue watcher materialises every unseen open issue on a watched
    public repo as an edge-less — therefore immediately ready — task, so the
    global ready count is a number a third party can inflate at will.
    Conditioning the recovery on it let anyone with a GitHub account stall
    every `pr` gate that takes this branch (no gate completed, no
    [GateResolved], no dependent nudged — the very failure #350 removes). The
    frontier query is narrowed to the candidate's own project + tags, so
    unrelated ready work cannot deny the answer."""
    client = FakeLithosClient(agent_id="a")
    for i in range(READY_QUERY_LIMIT):  # unrelated, edge-less, all ready
        await client.task_create(title=f"gh-issue-{i}", metadata={"project": "other"})
    story, gate = await _gate_with_story(client)
    dependent = await _blocked_dependent(client, story)
    await client.task_complete(task_id=story)  # an earlier sweep died mid-nudge
    # The unnarrowed frontier really is saturated past the query limit…
    assert len(await client.task_ready(limit=READY_QUERY_LIMIT)) >= READY_QUERY_LIMIT

    outcome = await reconcile_pr_gate(
        gate, _github(_pr(state="closed", merged=True)), _ctx(client)
    )

    # …and the gate resolves anyway, because the noise shares neither the
    # dependent's project nor its trigger tag.
    assert outcome == "merged"
    assert (await _get(client, gate.id)).status == "completed"
    nudges = [c for c in client.calls_to("task_update") if c["task_id"] == dependent]
    assert [c["metadata"] for c in nudges] == [{}]


def _saturate_unnarrowed_frontier(client: FakeLithosClient) -> None:
    """Make the INSTANCE-WIDE ready page full while narrowed pages answer
    normally — the shape a third party can create by filing issues on a watched
    public repo (each becomes an edge-less, immediately-ready task)."""
    real_ready = client.task_ready

    async def _saturated(**kwargs: Any) -> list[Task]:
        if kwargs.get("tags") or kwargs.get("project"):
            return await real_ready(**kwargs)  # a narrowed page is unaffected
        return [_filler(f"gh-issue-{i}") for i in range(kwargs["limit"])]

    client.task_ready = _saturated  # type: ignore[method-assign]


async def _unscoped_dependent(client: FakeLithosClient, blocker: str) -> str:
    """A dependent with no project and no tags — a hand-made follow-up task.
    Nothing narrows a readiness query about it, so its page is the instance."""
    dependent = await client.task_create(title="US-loose")
    await client.task_edge_upsert(
        from_task_id=blocker, to_task_id=dependent, type="blocks"
    )
    return dependent


def _saturate_both_unnarrowed_pages(client: FakeLithosClient) -> dict[str, bool]:
    """Both halves of the instance-wide partition full — the only shape in
    which a candidate with no project and no tags cannot be classified."""
    real_ready, real_blocked = client.task_ready, client.task_blocked
    state = {"on": True}

    async def _ready(**kwargs: Any) -> list[Task]:
        if not state["on"] or kwargs.get("tags") or kwargs.get("project"):
            return await real_ready(**kwargs)
        return [_filler(f"gh-issue-{i}") for i in range(kwargs["limit"])]

    async def _blocked(**kwargs: Any) -> Any:
        if not state["on"] or kwargs.get("tags") or kwargs.get("project"):
            return await real_blocked(**kwargs)
        return [
            SimpleNamespace(task=_filler(f"held-{i}"), blockers=())
            for i in range(kwargs["limit"])
        ]

    client.task_ready = _ready  # type: ignore[method-assign]
    client.task_blocked = _blocked  # type: ignore[method-assign]
    return state


async def test_gate_merged_recovery_progresses_past_an_unclassifiable_one() -> None:
    """An unclassifiable candidate must not hold a classifiable sibling
    hostage. A blocks tagged C (whose own project+tag page is tiny) and an
    untagged, projectless D (whose pages ARE the instance's, both saturated).
    All-or-nothing would strand C — the very dependent this task exists to
    dispatch — behind a candidate a third party can keep unreadable. So C is
    nudged now and recorded as nudged; only D's fate keeps the gate open."""
    client = FakeLithosClient(agent_id="a")
    story, gate = await _gate_with_story(client)
    scoped = await _blocked_dependent(client, story)
    loose = await _unscoped_dependent(client, story)
    await client.task_complete(task_id=story)  # an earlier sweep died mid-nudge
    saturated = _saturate_both_unnarrowed_pages(client)

    outcome = await reconcile_pr_gate(
        gate, _github(_pr(state="closed", merged=True)), _ctx(client)
    )

    assert outcome == "error"  # D unresolved → gate stays open to retry
    assert (await _get(client, gate.id)).status == "open"
    assert [
        c["metadata"] for c in client.calls_to("task_update") if c["task_id"] == scoped
    ] == [{}]
    assert not [c for c in client.calls_to("task_update") if c["task_id"] == loose]
    record = (await _get(client, gate.id)).metadata[NUDGE_RECOVERED_KEY]
    assert record["pr_url"] == _PR_URL
    assert record["nudged"] == [scoped]

    # The frontier clears → D is classified and nudged, the gate resolves, and
    # C is NOT nudged a second time (the record is what stops it).
    saturated["on"] = False  # back to the real implementation
    outcome = await reconcile_pr_gate(
        await _get(client, gate.id),
        _github(_pr(state="closed", merged=True)),
        _ctx(client),
    )

    assert outcome == "merged"
    assert (await _get(client, gate.id)).status == "completed"
    assert [
        c["metadata"] for c in client.calls_to("task_update") if c["task_id"] == loose
    ] == [{}]
    assert (
        len([c for c in client.calls_to("task_update") if c["task_id"] == scoped]) == 1
    )


async def test_gate_merged_recovery_reads_a_blocked_candidate_off_the_other_page() -> (
    None
):
    """Ready and blocked partition the open work tasks, so a candidate absent
    from a saturated ready page can still be settled by finding it on the
    blocked one. Without that second look a still-blocked fan-in dependent
    would read as *undetermined* and stall the gate for nothing."""
    client = FakeLithosClient(agent_id="a")
    story, gate = await _gate_with_story(client)
    loose = await _unscoped_dependent(client, story)
    sibling = await client.task_create(title="US9")  # also blocks D → not ready
    await client.task_edge_upsert(from_task_id=sibling, to_task_id=loose, type="blocks")
    await client.task_complete(task_id=story)  # an earlier sweep died mid-nudge
    _saturate_unnarrowed_frontier(client)

    outcome = await reconcile_pr_gate(
        gate, _github(_pr(state="closed", merged=True)), _ctx(client)
    )

    # Definitively not released → nothing to nudge, and the gate resolves
    # rather than deferring on a candidate we could in fact classify.
    assert outcome == "merged"
    assert not [c for c in client.calls_to("task_update") if c["task_id"] == loose]
    assert [c for c in client.calls_to("task_blocked")]


async def test_gate_merged_recovery_settles_a_loose_dependent_via_blocked_page() -> (
    None
):
    """A dependent with neither project nor tags narrows to the instance, and
    the instance's ready page is one a third party can fill. Open work is
    ready XOR blocked, so a COMPLETE blocked page that lacks the dependent
    settles it as released — nudged, gate resolved, first sweep."""
    client = FakeLithosClient(agent_id="a")
    story, gate = await _gate_with_story(client)
    loose = await _unscoped_dependent(client, story)
    await client.task_complete(task_id=story)  # an earlier sweep died mid-nudge
    _saturate_unnarrowed_frontier(client)

    outcome = await reconcile_pr_gate(
        gate, _github(_pr(state="closed", merged=True)), _ctx(client)
    )

    assert outcome == "merged"
    assert (await _get(client, gate.id)).status == "completed"
    nudges = [c for c in client.calls_to("task_update") if c["task_id"] == loose]
    assert [c["metadata"] for c in nudges] == [{}]


async def test_gate_merged_recovery_keeps_the_gate_open_until_lithos_answers() -> None:
    """PR #352 review F1: giving up made the merged gate terminal while a
    candidate that MAY have been released was still unaccounted for — and
    nothing after gate completion can ever emit its event. So there is no
    bound: the gate (the only retry surface that survives) stays open, one
    breadcrumb names the residue, and the moment Lithos can answer the
    dependent is nudged and the gate resolves."""
    client = FakeLithosClient(agent_id="a")
    story, gate = await _gate_with_story(client)
    loose = await _unscoped_dependent(client, story)
    await client.task_complete(task_id=story)  # an earlier sweep died mid-nudge
    saturated = _saturate_both_unnarrowed_pages(client)

    for sweep in range(1, 6):
        outcome = await reconcile_pr_gate(
            await _get(client, gate.id),
            _github(_pr(state="closed", merged=True)),
            _ctx(client),
        )
        assert outcome == "error", f"sweep {sweep} must keep deferring"
        assert (await _get(client, gate.id)).status == "open"
    assert not [c for c in client.calls_to("task_update") if c["task_id"] == loose]
    breadcrumbs = [
        f for f in client.findings if f["task_id"] == story and loose in f["summary"]
    ]
    assert len(breadcrumbs) == 1 and breadcrumbs[0]["summary"].startswith("[Friction]")
    assert "gave up" not in breadcrumbs[0]["summary"]
    # ...and an open-forever gate is not a write-forever gate: after the first
    # undetermined sweep records the state, later sweeps write nothing
    gate_writes = [c for c in client.calls_to("task_update") if c["task_id"] == gate.id]
    assert len(gate_writes) == 2  # the recovery record + the breadcrumb marker

    saturated["on"] = False  # Lithos can answer again
    outcome = await reconcile_pr_gate(
        await _get(client, gate.id),
        _github(_pr(state="closed", merged=True)),
        _ctx(client),
    )
    assert outcome == "merged"
    assert (await _get(client, gate.id)).status == "completed"
    nudges = [c for c in client.calls_to("task_update") if c["task_id"] == loose]
    assert [c["metadata"] for c in nudges] == [{}]


async def test_gate_merged_recovery_fan_out_is_written_once_per_gate() -> None:
    """The merged branch deliberately writes no ``develop_pr_merge_state``
    marker ("the gate leaving the open set is the de-dup"), so a gate whose own
    completion fails for a DURABLE reason stays in the swept open set and
    re-enters this branch every sweep. Unbounded, that re-nudges the whole
    released fan-out once an hour — a loom-authored write that is
    indistinguishable from the human edit the #339 guard reads as "retry this".
    The recovery fan-out is therefore marked on the gate and runs once."""
    client = FakeLithosClient(agent_id="a")
    story, gate = await _gate_with_story(client)
    dependent = await _blocked_dependent(client, story)
    await client.task_complete(task_id=story)  # an earlier sweep died mid-nudge
    original = client.task_complete

    async def _gate_completion_fails(**kwargs: Any) -> Any:
        if kwargs["task_id"] == gate.id:
            raise LithosClientError("server_error", "boom")
        return await original(**kwargs)

    client.task_complete = _gate_completion_fails  # type: ignore[method-assign]

    for _ in range(3):
        outcome = await reconcile_pr_gate(
            await _get(client, gate.id),
            _github(_pr(state="closed", merged=True)),
            _ctx(client),
        )
        assert outcome == "error"
        assert (await _get(client, gate.id)).status == "open"

    nudges = [c for c in client.calls_to("task_update") if c["task_id"] == dependent]
    assert [c["metadata"] for c in nudges] == [{}]  # once, not once per sweep
    record = (await _get(client, gate.id)).metadata[NUDGE_RECOVERED_KEY]
    assert record["pr_url"] == _PR_URL and record["nudged"] == [dependent]


async def test_gate_merged_does_not_nudge_a_still_blocked_fan_in_dependent() -> None:
    """The ordinary fan-in case (A and B both block C; A's PR merges first) is a
    LEGITIMATELY empty `unblocked`, not a lost response — C is not ready. No
    graph fallback runs and C is left alone: a nudge would bump its `updated_at`
    and consume the #339 bootstrap-replay guard's evidence for nothing."""
    client = FakeLithosClient(agent_id="a")
    story, gate = await _gate_with_story(client)
    dependent = await _blocked_dependent(client, story)
    sibling = await client.task_create(title="US9", metadata={"project": "p"})
    await client.task_edge_upsert(
        from_task_id=sibling, to_task_id=dependent, type="blocks"
    )

    outcome = await reconcile_pr_gate(
        gate, _github(_pr(state="closed", merged=True)), _ctx(client)
    )

    assert outcome == "merged"
    assert not [c for c in client.calls_to("task_update") if c["task_id"] == dependent]
    assert not [
        c for c in client.calls_to("task_edge_list") if c["types"] == ["blocks"]
    ]


async def test_gate_merged_nudge_failure_posts_friction_and_still_resolves() -> None:
    """A failed nudge is best-effort: it surfaces as [Friction] on the story and
    leaves the merged outcome (story + gate completed) untouched — the restart
    bootstrap remains the backstop."""
    client = FakeLithosClient(agent_id="a")
    story, gate = await _gate_with_story(client)
    dependent = await _blocked_dependent(client, story)
    original = client.task_update

    async def _fail_nudge(**kwargs: Any) -> Any:
        if kwargs["task_id"] == dependent:
            raise LithosClientError("server_error", "boom")
        return await original(**kwargs)

    client.task_update = _fail_nudge  # type: ignore[method-assign]

    outcome = await reconcile_pr_gate(
        gate, _github(_pr(state="closed", merged=True)), _ctx(client)
    )

    assert outcome == "merged"
    assert (await _get(client, story)).status == "completed"
    assert (await _get(client, gate.id)).status == "completed"
    friction = [
        f
        for f in client.findings
        if f["summary"].startswith("[Friction]") and dependent in f["summary"]
    ]
    assert [f["task_id"] for f in friction] == [story]
    assert any(f["summary"].startswith(GATE_RESOLVED) for f in client.findings)


async def test_gate_closed_unmerged_leaves_gate_open_and_warns() -> None:
    """Closed-unmerged: the gate is LEFT OPEN (never cancelled — a cancelled
    gate is terminal and its story would be unrecoverable), a [DeliveredPRClosed]
    finding lands on the story, and the gate is marked so the dead PR isn't
    re-polled."""
    client = FakeLithosClient(agent_id="a")
    story, gate = await _gate_with_story(client)
    github = _github(_pr(state="closed", merged=False))

    outcome = await reconcile_pr_gate(gate, github, _ctx(client))

    assert outcome == "closed_unmerged"
    # Gate + story both still open; story still blocked.
    assert (await _get(client, gate.id)).status == "open"
    assert (await _get(client, story)).status == "open"
    assert [bt.task.id for bt in await client.task_blocked(project="p")] == [story]
    # Finding on the STORY; marker on the GATE.
    findings = [f["summary"] for f in client._findings]
    assert any(s.startswith(DELIVERED_PR_CLOSED) for s in findings)
    marked_gate = await _get(client, gate.id)
    assert marked_gate.metadata[MERGE_STATE_KEY] == "closed_unmerged"
    assert marked_gate.metadata[MERGE_STATE_URL_KEY] == _PR_URL


async def test_gate_deleted_pr_leaves_gate_open() -> None:
    client = FakeLithosClient(agent_id="a")
    story, gate = await _gate_with_story(client)

    outcome = await reconcile_pr_gate(gate, _github(None), _ctx(client))

    assert outcome == "gone"
    assert (await _get(client, gate.id)).status == "open"
    assert (await _get(client, gate.id)).metadata[MERGE_STATE_KEY] == "gone"


async def test_gate_still_open_pr_is_a_noop() -> None:
    client = FakeLithosClient(agent_id="a")
    story, gate = await _gate_with_story(client)

    outcome = await reconcile_pr_gate(
        gate, _github(_pr(state="open", merged=False)), _ctx(client)
    )

    assert outcome == "still_open"
    assert (await _get(client, gate.id)).status == "open"
    assert MERGE_STATE_KEY not in (await _get(client, gate.id)).metadata


async def test_gate_transient_github_error_retries_next_sweep() -> None:
    client = FakeLithosClient(agent_id="a")
    story, gate = await _gate_with_story(client)
    github = AsyncMock()
    github.get_pull_request.side_effect = GitHubError("rate limited")

    outcome = await reconcile_pr_gate(gate, github, _ctx(client))

    assert outcome == "error"
    # No marker → re-polled next sweep.
    assert MERGE_STATE_KEY not in (await _get(client, gate.id)).metadata


async def test_gate_already_resolved_for_this_url_is_skipped() -> None:
    """A closed-unmerged gate carries a terminal marker scoped to its url; the
    next sweep skips it without a GitHub call."""
    client = FakeLithosClient(agent_id="a")
    story, gate = await _gate_with_story(client)
    await client.task_update(
        task_id=gate.id,
        metadata={MERGE_STATE_KEY: "closed_unmerged", MERGE_STATE_URL_KEY: _PR_URL},
    )
    refreshed = await client.task_get(task_id=gate.id)
    github = AsyncMock()

    outcome = await reconcile_pr_gate(refreshed, github, _ctx(client))

    assert outcome is None
    github.get_pull_request.assert_not_awaited()


async def test_gate_with_unparseable_metadata_is_marked_then_stays_quiet() -> None:
    """A loom-side malformed gate (missing repo/number) can never resolve; mark
    it on the FIRST sweep, then stay silent — a re-marked/re-warned unparseable
    gate every sweep would be persistent watcher noise."""
    client = FakeLithosClient(agent_id="a")
    gate_id = await client.task_create(
        title="bad gate",
        task_type="gate",
        metadata={"gate_type": "pr"},  # no repo / pr_number / pr_url
    )
    github = AsyncMock()

    # First sweep: mark + report.
    first = await reconcile_pr_gate(await _get(client, gate_id), github, _ctx(client))
    assert first == "unparseable"
    assert (await _get(client, gate_id)).metadata[MERGE_STATE_KEY] == "unparseable"

    # Second sweep (re-fetched with the marker): no-op, no GitHub call.
    second = await reconcile_pr_gate(await _get(client, gate_id), github, _ctx(client))
    assert second is None
    github.get_pull_request.assert_not_awaited()


async def test_orphan_gate_merged_completes_gate_without_a_finding() -> None:
    """A gate with no waiter (edge never landed): merging its PR still completes
    the gate; there is no story to post [GateResolved] on."""
    client = FakeLithosClient(agent_id="a")
    gate_id = await client.task_create(
        title="orphan",
        task_type="gate",
        metadata={
            "gate_type": "pr",
            "repo": "agent-lore/lithos-loom",
            "pr_number": 7,
            "pr_url": _PR_URL,
        },
    )
    gate = await client.task_get(task_id=gate_id)

    outcome = await reconcile_pr_gate(
        gate, _github(_pr(state="closed", merged=True)), _ctx(client)
    )

    assert outcome == "merged"
    assert (await _get(client, gate_id)).status == "completed"
    assert client._findings == []


# ── external-review ingestion wiring (PRD S2 — detail in test_external_reviews) ──


def _open_pr() -> PullRequest:
    return PullRequest(
        repo="agent-lore/lithos-loom",
        number=7,
        state="open",
        merged=False,
        merged_at=None,
        merge_commit_sha=None,
        head_sha="e" * 40,
    )


def _review_github(pr: PullRequest) -> AsyncMock:
    github = _github(pr)
    github.list_pull_request_reviews.return_value = [
        PullRequestReview(
            author="reviewer-human",
            body="two problems here",
            review_id=500,
            state="CHANGES_REQUESTED",
        )
    ]
    github.list_pull_request_review_comments.return_value = []
    return github


async def test_reconcile_still_open_ingests_reviews_when_enabled() -> None:
    client = FakeLithosClient(agent_id="a")
    story, gate = await _gate_with_story(client)
    github = _review_github(_open_pr())

    outcome = await reconcile_pr_gate(gate, github, _ctx(client), ingest_reviews=True)

    assert outcome == "still_open"
    findings = [f["summary"] for f in client._findings]
    assert len(findings) == 1 and findings[0].startswith("[ExternalReview]")


async def test_reconcile_still_open_skips_ingestion_by_default() -> None:
    client = FakeLithosClient(agent_id="a")
    _story, gate = await _gate_with_story(client)
    github = _review_github(_open_pr())

    outcome = await reconcile_pr_gate(gate, github, _ctx(client))

    assert outcome == "still_open"
    github.list_pull_request_reviews.assert_not_called()


# ── remediation wiring (slice C — detail in test_external_remediation) ──


def _remediation(tmp_path, *, budget: int = 2, spawn=None):
    from lithos_loom.subscriptions.external_remediation import (
        ExternalRemediation,
        RemediationSettings,
    )

    async def _no_spawn(cmd):  # pragma: no cover — dispatch not expected
        raise AssertionError("spawn must not be called")

    return ExternalRemediation(
        RemediationSettings(
            trusted_bots=("copilot-pull-request-reviewer[bot]",),
            budget=budget,
            projects={"p": tmp_path / "repo"},
            work_dir=tmp_path / "work",
        ),
        spawn=spawn if spawn is not None else _no_spawn,
    )


async def test_reconcile_still_open_dispatches_remediation(
    tmp_path, monkeypatch
) -> None:
    """The full still-open chain: head observed, batch ingested, converge
    dispatched, budget incremented on the gate."""
    import json as _json
    from pathlib import Path as _Path

    from lithos_loom.subscriptions import external_remediation as rem_mod
    from lithos_loom.subscriptions.external_remediation import (
        REMEDIATION_KEY,
        OriginRead,
    )

    # the mapped checkout resolves to the gate's repo (an unresolvable
    # origin fails closed — PR #362 re-review 3)
    async def resolvable(path: _Path) -> OriginRead:
        return OriginRead("agent-lore/lithos-loom", "ok")

    monkeypatch.setattr(rem_mod, "origin_read", resolvable)

    client = FakeLithosClient(agent_id="a")
    story, gate = await _gate_with_story(client)
    github = _review_github(_open_pr())
    github.get_collaborator_permission.return_value = "write"  # trusted human

    calls: list[list[str]] = []

    async def spawn(cmd):
        calls.append(cmd)
        path = _Path(cmd[cmd.index("--json") + 1])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_json.dumps({"status": "triage_rejected", "pushed": False}))
        return 0, ""

    rem = _remediation(tmp_path, spawn=spawn)
    outcome = await reconcile_pr_gate(
        gate, github, _ctx(client), ingest_reviews=True, remediation=rem
    )

    assert outcome == "still_open"
    assert rem._task is not None
    await rem._task
    assert len(calls) == 1
    marker = (await _get(client, gate.id)).metadata[REMEDIATION_KEY]
    assert marker["rounds_used"] == 1
    assert marker["last_seen_head_sha"] == "e" * 40  # observed pre-ingest


async def test_reconcile_exhausted_budget_states_it_in_the_finding(
    tmp_path,
) -> None:
    """Exhaustion stops dispatch, never detection — and the operator reads it
    off the [ExternalReview] finding itself."""
    from lithos_loom.subscriptions.external_remediation import REMEDIATION_KEY

    client = FakeLithosClient(agent_id="a")
    story, gate = await _gate_with_story(client)
    await client.task_update(
        task_id=gate.id,
        metadata={
            REMEDIATION_KEY: {
                "pr_url": _PR_URL,
                "rounds_used": 2,
                "last_loom_pushed_sha": "",
                "last_seen_head_sha": "e" * 40,
            }
        },
    )
    gate = await _get(client, gate.id)
    github = _review_github(_open_pr())
    rem = _remediation(tmp_path, budget=2)

    outcome = await reconcile_pr_gate(
        gate, github, _ctx(client), ingest_reviews=True, remediation=rem
    )

    assert outcome == "still_open"
    findings = [f["summary"] for f in client._findings]
    (finding,) = findings
    assert finding.startswith("[ExternalReview]")
    assert "remediation budget exhausted" in finding
    assert rem._task is None  # nothing dispatched


async def test_reconcile_busy_slot_parks_the_trigger_with_the_marks(
    tmp_path,
) -> None:
    """PR #346 re-review 1, end-to-end: a batch arriving while a run is in
    flight lands its pending trigger in the same sweep that consumed its
    high-water marks — the deferred dispatch is durable."""
    import asyncio
    import contextlib

    from lithos_loom.subscriptions.external_remediation import PENDING_KEY

    client = FakeLithosClient(agent_id="a")
    story, gate = await _gate_with_story(client)
    github = _review_github(_open_pr())
    github.get_collaborator_permission.return_value = "write"
    rem = _remediation(tmp_path)
    rem._task = asyncio.create_task(asyncio.sleep(30))  # a run in flight
    try:
        outcome = await reconcile_pr_gate(
            gate, github, _ctx(client), ingest_reviews=True, remediation=rem
        )
    finally:
        rem._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await rem._task

    assert outcome == "still_open"
    refreshed = await _get(client, gate.id)
    assert refreshed.metadata.get(PENDING_KEY) == {"pr_url": _PR_URL}


async def test_gate_merged_ingests_final_review_activity_first() -> None:
    """PR #348 review F1: a review that lands before the first sweep, on a PR
    that merges before that sweep, must still be OBSERVED — the merged branch
    ingests once (a detection-only [ExternalReview] record; remediation on a
    merged PR is structurally impossible) before the gate leaves the open set
    forever."""
    client = FakeLithosClient(agent_id="a")
    story, gate = await _gate_with_story(client)
    github = _review_github(_pr(state="closed", merged=True))

    outcome = await reconcile_pr_gate(gate, github, _ctx(client), ingest_reviews=True)

    assert outcome == "merged"
    findings = [f["summary"] for f in client._findings]
    assert any(f.startswith("[ExternalReview]") for f in findings)
    assert any(f.startswith(GATE_RESOLVED) for f in findings)
    assert (await _get(client, story)).status == "completed"
    assert (await _get(client, gate.id)).status == "completed"


async def test_gate_merged_without_ingestion_flag_stays_pure_merge_poll() -> None:
    client = FakeLithosClient(agent_id="a")
    _story, gate = await _gate_with_story(client)
    github = _review_github(_pr(state="closed", merged=True))

    outcome = await reconcile_pr_gate(gate, github, _ctx(client))

    assert outcome == "merged"
    findings = [f["summary"] for f in client._findings]
    assert not any(f.startswith("[ExternalReview]") for f in findings)


async def test_gate_merged_final_ingestion_failure_retries_not_resolves() -> None:
    """PR #348 re-review 1: the merged-path observation is only durable if a
    transient ingestion failure DEFERS resolution — completing the gate on a
    failed final ingest loses the record forever (no next sweep exists)."""
    client = FakeLithosClient(agent_id="a")
    story, gate = await _gate_with_story(client)
    github = _review_github(_pr(state="closed", merged=True))
    github.list_pull_request_reviews.side_effect = GitHubError("transient")

    outcome = await reconcile_pr_gate(gate, github, _ctx(client), ingest_reviews=True)

    assert outcome == "error"  # retry next sweep — the gate MUST stay open
    assert (await _get(client, story)).status == "open"
    assert (await _get(client, gate.id)).status == "open"
    assert not any(f["summary"].startswith(GATE_RESOLVED) for f in client._findings)

    # Next sweep, GitHub healthy again: the record posts AND the gate resolves.
    github.list_pull_request_reviews.side_effect = None
    outcome = await reconcile_pr_gate(gate, github, _ctx(client), ingest_reviews=True)
    assert outcome == "merged"
    findings = [f["summary"] for f in client._findings]
    assert any(f.startswith("[ExternalReview]") for f in findings)
    assert any(f.startswith(GATE_RESOLVED) for f in findings)


async def test_gate_merged_finding_post_failure_retries_not_resolves() -> None:
    """Same guarantee for the Lithos half: the [ExternalReview] record (or its
    de-dup mark) not landing defers resolution rather than losing the batch."""
    client = FakeLithosClient(agent_id="a")
    story, gate = await _gate_with_story(client)
    github = _review_github(_pr(state="closed", merged=True))
    original = client.finding_post
    fail = {"on": True}

    async def flaky_post(**kwargs: Any) -> Any:
        if fail["on"]:
            raise LithosClientError("server_error", "boom")
        return await original(**kwargs)

    client.finding_post = flaky_post  # type: ignore[method-assign]

    outcome = await reconcile_pr_gate(gate, github, _ctx(client), ingest_reviews=True)
    assert outcome == "error"
    assert (await _get(client, gate.id)).status == "open"

    fail["on"] = False
    outcome = await reconcile_pr_gate(
        await _get(client, gate.id), github, _ctx(client), ingest_reviews=True
    )
    assert outcome == "merged"
    findings = [f["summary"] for f in client._findings]
    assert any(f.startswith("[ExternalReview]") for f in findings)


async def test_gate_merged_quiet_ingestion_still_resolves() -> None:
    """No review activity at all: the merged gate resolves first sweep — a
    quiet pass must never be mistaken for a failed one."""
    client = FakeLithosClient(agent_id="a")
    story, gate = await _gate_with_story(client)
    github = _review_github(_pr(state="closed", merged=True))
    github.list_pull_request_reviews.return_value = []

    outcome = await reconcile_pr_gate(gate, github, _ctx(client), ingest_reviews=True)

    assert outcome == "merged"
    assert (await _get(client, story)).status == "completed"


async def test_gate_merged_final_record_speaks_post_merge() -> None:
    """PR #348 re-review 3: the final record must not instruct an impossible
    pre-merge action — it says the activity was observed after merge."""
    client = FakeLithosClient(agent_id="a")
    story, gate = await _gate_with_story(client)
    github = _review_github(_pr(state="closed", merged=True))

    await reconcile_pr_gate(gate, github, _ctx(client), ingest_reviews=True)

    record = next(
        f["summary"]
        for f in client._findings
        if f["summary"].startswith("[ExternalReview]")
    )
    assert "already merged" in record
    assert "remains blocked" not in record
    assert "before merging" not in record


async def test_gate_merged_silent_review_marker_failure_defers() -> None:
    """PR #348 re-review round 3: an APPROVED review's only durable record is
    the seen marker — a failed marker write on the merged path must defer
    resolution, exactly like a failed finding post."""
    client = FakeLithosClient(agent_id="a")
    story, gate = await _gate_with_story(client)
    github = _review_github(_pr(state="closed", merged=True))
    github.list_pull_request_reviews.return_value = [
        PullRequestReview(
            author="reviewer-human", body="", review_id=500, state="APPROVED"
        )
    ]
    original = client.task_update
    fail = {"on": True}

    async def flaky_update(**kwargs: Any) -> Any:
        if fail["on"] and "external_review_seen" in (kwargs.get("metadata") or {}):
            raise LithosClientError("server_error", "boom")
        return await original(**kwargs)

    client.task_update = flaky_update  # type: ignore[method-assign]

    outcome = await reconcile_pr_gate(gate, github, _ctx(client), ingest_reviews=True)
    assert outcome == "error"
    assert (await _get(client, story)).status == "open"
    assert (await _get(client, gate.id)).status == "open"

    fail["on"] = False
    outcome = await reconcile_pr_gate(
        await _get(client, gate.id), github, _ctx(client), ingest_reviews=True
    )
    assert outcome == "merged"
    marked = await _get(client, gate.id)
    assert "external_review_seen" in marked.metadata


async def test_still_open_branch_considers_the_merge_gate_after_remediation() -> None:
    """PRD S3 watcher half: the still-open branch hands the fetched PR to the
    merge-gate dispatcher AFTER landability + remediation, telling it whether
    a remediation run is in flight on this PR (the mutual hold)."""
    from dataclasses import replace

    client = FakeLithosClient(agent_id="a")
    story, gate = await _gate_with_story(client)
    pr = replace(
        _pr(state="open", merged=False),
        head_sha="h" * 40,
        base_sha="b" * 40,
        base_ref="main",
        mergeable=True,
        mergeable_state="behind",
    )
    seen: list[dict[str, Any]] = []

    class _MergeGate:
        def busy_on(self, pr_url: str) -> bool:
            return False

        async def consider(self, gate, spec, story_id, pr, ctx, *, hold):
            seen.append({"story": story_id, "head": pr.head_sha, "hold": hold})
            return "dispatched"

    class _Remediation:
        def busy_on(self, pr_url: str) -> bool:
            return pr_url == _PR_URL

    outcome = await reconcile_pr_gate(
        gate,
        _github(pr),
        _ctx(client),
        merge_gate=_MergeGate(),  # type: ignore[arg-type]
    )
    assert outcome == "still_open"
    assert seen == [{"story": story, "head": "h" * 40, "hold": False}]

    seen.clear()
    outcome = await reconcile_pr_gate(
        gate,
        _github(pr),
        _ctx(client),
        merge_gate=_MergeGate(),  # type: ignore[arg-type]
        remediation=_Remediation(),  # type: ignore[arg-type]
    )
    assert outcome == "still_open"
    assert seen == [{"story": story, "head": "h" * 40, "hold": True}]


async def test_still_open_branch_checks_landability(caplog: Any) -> None:
    """PRD S1: the merge poll's still-open branch classifies the PR and posts
    [PRConflicted] on the story when GitHub reports it cannot merge."""
    from dataclasses import replace

    from lithos_loom.subscriptions.pr_landability import LANDABILITY_KEY, PR_CONFLICTED

    client = FakeLithosClient(agent_id="a")
    story, gate = await _gate_with_story(client)
    dirty = replace(
        _pr(state="open", merged=False),
        head_sha="h" * 40,
        base_sha="b" * 40,
        base_ref="main",
        mergeable=False,
        mergeable_state="dirty",
    )
    outcome = await reconcile_pr_gate(gate, _github(dirty), _ctx(client))

    assert outcome == "still_open"
    findings = [f["summary"] for f in client._findings]
    assert any(s.startswith(PR_CONFLICTED) for s in findings)
    assert (await _get(client, gate.id)).metadata[LANDABILITY_KEY]["state"] == "dirty"


async def test_still_open_branch_keys_the_base_move_on_the_live_tip() -> None:
    """GitHub's PR payload snapshots ``base.sha`` at the PR's last update —
    loom#352 carried a base four days and three merges stale, so a key built
    from it never saw main move. The sweep reads the base branch's live tip
    and hands THAT to landability and the merge-gate dispatcher; an
    unreadable tip is "unknown" (neither consumer keys on it)."""
    from dataclasses import replace

    from lithos_loom.subscriptions.pr_landability import LANDABILITY_KEY

    client = FakeLithosClient(agent_id="a")
    story, gate = await _gate_with_story(client)
    pr = replace(
        _pr(state="open", merged=False),
        head_sha="h" * 40,
        base_sha="",  # the parser no longer fills it
        base_ref="main",
        mergeable=False,
        mergeable_state="dirty",
    )
    seen: list[str] = []

    class _MergeGate:
        def busy_on(self, pr_url: str) -> bool:
            return False

        async def consider(self, gate, spec, story_id, pr, ctx, *, hold):
            seen.append(pr.base_sha)
            return "dispatched"

    github = _github(pr, base_tip="t" * 40)
    outcome = await reconcile_pr_gate(
        gate,
        github,
        _ctx(client),
        merge_gate=_MergeGate(),  # type: ignore[arg-type]
    )
    assert outcome == "still_open"
    github.get_branch_tip.assert_awaited_once_with("agent-lore/lithos-loom", "main")
    assert seen == ["t" * 40]
    marker = (await _get(client, gate.id)).metadata[LANDABILITY_KEY]
    assert marker["base_sha"] == "t" * 40

    # the tip cannot be read: nothing is keyed on a guessed base
    seen.clear()
    gone = _github(pr, base_tip=None)
    gone.get_branch_tip.return_value = None
    gate = await _get(client, gate.id)
    before = dict(gate.metadata[LANDABILITY_KEY])
    outcome = await reconcile_pr_gate(
        gate,
        gone,
        _ctx(client),
        merge_gate=_MergeGate(),  # type: ignore[arg-type]
    )
    assert outcome == "still_open"
    assert seen == [""]
    assert (await _get(client, gate.id)).metadata[LANDABILITY_KEY] == before


async def test_gate_merged_recovery_defers_on_a_raw_transport_failure() -> None:
    """An exhausted transport retry re-raises the raw exception; recovery must
    treat it like any unreadable candidate — gate open, retried next sweep —
    not crash the sweep."""
    client = FakeLithosClient(agent_id="a")
    story, gate = await _gate_with_story(client)
    dependent = await _blocked_dependent(client, story)
    await client.task_complete(task_id=story)  # an earlier sweep died mid-nudge
    real_get = client.task_get

    async def dead(*, task_id: str):
        if task_id == dependent:
            raise RuntimeError("SSE stream closed")
        return await real_get(task_id=task_id)

    client.task_get = dead  # type: ignore[method-assign]
    outcome = await reconcile_pr_gate(
        gate, _github(_pr(state="closed", merged=True)), _ctx(client)
    )
    assert outcome == "error"
    assert (await _get(client, gate.id)).status == "open"


async def test_still_open_branch_considers_conflict_resolution_last() -> None:
    """PRD S5 watcher half: the still-open branch hands the PR to the
    conflict-resolve dispatcher AFTER the merge-gate (its record is the
    trigger), held while EITHER other dispatcher is busy on this PR."""
    from dataclasses import replace

    client = FakeLithosClient(agent_id="a")
    story, gate = await _gate_with_story(client)
    pr = replace(
        _pr(state="open", merged=False),
        head_sha="h" * 40,
        base_sha="b" * 40,
        base_ref="main",
        mergeable=False,
        mergeable_state="dirty",
    )
    order: list[str] = []
    seen: list[dict[str, Any]] = []

    class _MergeGate:
        async def consider(self, gate, spec, story_id, pr, ctx, *, hold):
            order.append("merge-gate")
            return "unchanged"

        def busy_on(self, pr_url: str) -> bool:
            return pr_url == _PR_URL

    class _Resolver:
        def debt_on(self, pr_url: str) -> bool:
            return False

        def busy_on(self, pr_url: str) -> bool:
            return False

        async def recover_debt(self, gate, spec, story_id, ctx):
            return None

        async def consider(self, gate, spec, story_id, pr, ctx, *, hold):
            order.append("conflict-resolve")
            seen.append({"story": story_id, "head": pr.head_sha, "hold": hold})
            return "dispatched"

    outcome = await reconcile_pr_gate(
        gate,
        _github(pr),
        _ctx(client),
        conflict_resolve=_Resolver(),  # type: ignore[arg-type]
    )
    assert outcome == "still_open"
    assert seen == [{"story": story, "head": "h" * 40, "hold": False}]

    seen.clear()
    order.clear()
    outcome = await reconcile_pr_gate(
        gate,
        _github(pr),
        _ctx(client),
        merge_gate=_MergeGate(),  # type: ignore[arg-type]
        conflict_resolve=_Resolver(),  # type: ignore[arg-type]
    )
    assert outcome == "still_open"
    assert order == ["merge-gate", "conflict-resolve"]
    assert seen == [{"story": story, "head": "h" * 40, "hold": True}]


async def test_still_open_branch_recovers_a_debt_before_observing_the_head() -> None:
    """A restarted resolver re-arms a held debt from the story breadcrumb
    BEFORE remediation observes the head — else the merge commit reads as a
    human push and the budget resets."""
    from dataclasses import replace

    client = FakeLithosClient(agent_id="a")
    story, gate = await _gate_with_story(client)
    pr = replace(
        _pr(state="open", merged=False),
        head_sha="h" * 40,
        base_sha="b" * 40,
        base_ref="main",
        mergeable=True,
        mergeable_state="clean",
    )
    order: list[str] = []

    class _Resolver:
        def debt_on(self, pr_url: str) -> bool:
            return False

        async def recover_debt(self, gate, spec, story_id, ctx):
            order.append("recover")

        async def consider(self, gate, spec, story_id, pr, ctx, *, hold):
            order.append("consider")
            return "no_conflict"

        def busy_on(self, pr_url: str) -> bool:
            return False

    class _Remediation:
        async def observe_head(self, gate, spec, pr, ctx):
            order.append("observe")
            return None

        def exhaustion_note(self, budget):
            return None

        def busy_on(self, pr_url: str) -> bool:
            return False

    outcome = await reconcile_pr_gate(
        gate,
        _github(pr),
        _ctx(client),
        ingest_reviews=True,
        remediation=_Remediation(),  # type: ignore[arg-type]
        conflict_resolve=_Resolver(),  # type: ignore[arg-type]
    )
    assert outcome == "still_open"
    assert order[:2] == ["recover", "observe"] and order[-1] == "consider"


# ── reconciliation state (PRD S7) ────────────────────────────────────────


async def test_still_open_sweep_records_the_reconciliation_state() -> None:
    from lithos_loom.subscriptions.reconciliation_state import (
        STATE_KEY,
        STATE_URL_KEY,
    )

    client = FakeLithosClient(agent_id="a")
    story, gate = await _gate_with_story(client)
    github = _github(_open_pr())

    outcome = await reconcile_pr_gate(gate, github, _ctx(client))

    assert outcome == "still_open"
    stored = await client.task_get(task_id=gate.id)
    assert stored is not None
    assert stored.metadata[STATE_KEY] in ("ready_to_merge", "awaiting_review")
    assert stored.metadata[STATE_URL_KEY] == _PR_URL


async def test_closed_pr_records_needs_human_in_the_same_marker_write() -> None:
    from lithos_loom.subscriptions.reconciliation_state import STATE_KEY

    client = FakeLithosClient(agent_id="a")
    story, gate = await _gate_with_story(client)
    github = _github(_pr(state="closed", merged=False))
    writes_before = len(client.calls_to("task_update"))

    outcome = await reconcile_pr_gate(gate, github, _ctx(client))

    assert outcome == "closed_unmerged"
    stored = await client.task_get(task_id=gate.id)
    assert stored is not None
    assert stored.metadata[STATE_KEY] == "needs_human"
    assert len(client.calls_to("task_update")) == writes_before + 1


async def test_an_unreadable_base_tip_never_records_ready_to_merge() -> None:
    """Review #369 F1: the re-gate cannot run without the live base tip,
    so a clean, mergeable PR is NOT ready — it is unevaluated."""
    from lithos_loom.subscriptions.reconciliation_state import STATE_KEY

    class _MergeGate:
        def busy_on(self, pr_url: str) -> bool:
            return False

        async def consider(self, gate, spec, story_id, pr, ctx, *, hold):
            return "unknown_shas"

    client = FakeLithosClient(agent_id="a")
    story, gate = await _gate_with_story(client)
    github = _github(_open_pr())
    github.get_branch_tip.side_effect = GitHubError("boom")

    await reconcile_pr_gate(
        gate,
        github,
        _ctx(client),
        merge_gate=_MergeGate(),  # type: ignore[arg-type]
    )

    stored = await client.task_get(task_id=gate.id)
    assert stored is not None
    assert stored.metadata[STATE_KEY] == "awaiting_review"


async def test_a_re_gate_deferred_behind_another_pr_records_reconciling() -> None:
    from lithos_loom.subscriptions.reconciliation_state import STATE_KEY

    class _MergeGate:
        def busy_on(self, pr_url: str) -> bool:
            return False

        async def consider(self, gate, spec, story_id, pr, ctx, *, hold):
            return "deferred_busy"

    client = FakeLithosClient(agent_id="a")
    story, gate = await _gate_with_story(client)

    await reconcile_pr_gate(
        gate,
        _github(_open_pr()),
        _ctx(client),
        merge_gate=_MergeGate(),  # type: ignore[arg-type]
    )

    stored = await client.task_get(task_id=gate.id)
    assert stored is not None
    assert stored.metadata[STATE_KEY] == "reconciling"


async def test_a_gate_closed_before_s7_is_backfilled_with_needs_human() -> None:
    """Review #369 F2: the terminal guard returns before any fetch; a gate
    marked closed before this shipped must still get its state — with no
    GitHub call."""
    from lithos_loom.subscriptions.reconciliation_state import STATE_KEY, STATE_URL_KEY

    client = FakeLithosClient(agent_id="a")
    story, gate = await _gate_with_story(client)
    await client.task_update(
        task_id=gate.id,
        metadata={MERGE_STATE_KEY: "closed_unmerged", MERGE_STATE_URL_KEY: _PR_URL},
    )
    gate = await _get(client, gate.id)
    github = _github(None)

    outcome = await reconcile_pr_gate(gate, github, _ctx(client))

    assert outcome is None
    github.get_pull_request.assert_not_awaited()
    stored = await client.task_get(task_id=gate.id)
    assert stored is not None
    assert stored.metadata[STATE_KEY] == "needs_human"
    assert stored.metadata[STATE_URL_KEY] == _PR_URL
    # and once backfilled, the guard is silent again
    writes = len(client.calls_to("task_update"))
    await reconcile_pr_gate(stored, github, _ctx(client))
    assert len(client.calls_to("task_update")) == writes
