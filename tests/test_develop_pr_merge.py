"""Tests for ``lithos_loom.subscriptions._develop_pr_merge`` (#87).

The reconcile polls a delivered PR's merge state and acts on the open Lithos
task: merged → complete; closed-unmerged / deleted → one-shot
``[DeliveredPRClosed]`` finding + leave open; still-open → no-op. A
``develop_pr_merge_state`` + ``develop_pr_merge_url`` marker scoped to the
resolved PR de-dups across sweeps while letting a replacement PR recover.
GitHub + Lithos are stubbed; the ``task`` is a minimal id + metadata object.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

from lithos_loom.errors import LithosClientError
from lithos_loom.gates import create_pr_gate
from lithos_loom.github_client import GitHubError, PullRequest
from lithos_loom.lithos_client import Task
from lithos_loom.subscriptions import SubscriptionContext
from lithos_loom.subscriptions._develop_pr_merge import (
    DELIVERED_PR_CLOSED,
    GATE_RESOLVED,
    MERGE_STATE_KEY,
    MERGE_STATE_URL_KEY,
    reconcile_pr_gate,
)
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


def _github(pr: PullRequest | None) -> AsyncMock:
    github = AsyncMock()
    github.get_pull_request.return_value = pr
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
