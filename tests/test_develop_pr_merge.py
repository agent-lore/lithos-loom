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
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from lithos_loom.errors import LithosClientError
from lithos_loom.gates import STORY_GATE_ID_KEY, create_pr_gate
from lithos_loom.github_client import GitHubError, PullRequest
from lithos_loom.lithos_client import Task
from lithos_loom.subscriptions import SubscriptionContext
from lithos_loom.subscriptions._develop_pr_merge import (
    DELIVERED_PR_CLOSED,
    GATE_RESOLVED,
    MERGE_STATE_KEY,
    MERGE_STATE_URL_KEY,
    _parse_pr_url,
    reconcile_develop_pr,
    reconcile_pr_gate,
)
from tests.support import FakeLithosClient

_PR_URL = "https://github.com/agent-lore/lithos-loom/pull/7"
_PR_URL_2 = "https://github.com/agent-lore/lithos-loom/pull/8"  # a replacement PR


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


def _task(metadata: dict[str, Any], *, task_id: str = "t-1") -> Any:
    return SimpleNamespace(id=task_id, metadata=metadata)


def _pr(*, state: str, merged: bool, sha: str | None = "abc123") -> PullRequest:
    return PullRequest(
        repo="agent-lore/lithos-loom",
        number=7,
        state=state,
        merged=merged,
        merged_at=datetime(2026, 6, 13, tzinfo=UTC) if merged else None,
        merge_commit_sha=sha if merged else None,
    )


class _StatefulLithos:
    """A fake Lithos that models post-lithos#303 semantics: ``task_update``
    applies its additive metadata merge **even on a terminal task**. Tracks
    ``status`` + the merged ``metadata`` so a test can assert the marker
    actually persists end-to-end — the ``AsyncMock`` fake never modelled #303,
    so it green-lit the merged marker even while the real write was rejected."""

    def __init__(self, metadata: dict[str, Any] | None = None) -> None:
        self.status = "open"
        self.metadata: dict[str, Any] = dict(metadata or {})
        self.findings: list[str] = []

    async def task_complete(self, *, task_id: str) -> None:
        if self.status != "open":
            raise LithosClientError("task_not_found", "already terminal")
        self.status = "completed"

    async def task_update(
        self, *, task_id: str, metadata: dict[str, Any] | None = None
    ) -> None:
        for key, value in (metadata or {}).items():
            if value is None:
                self.metadata.pop(key, None)
            else:
                self.metadata[key] = value

    async def finding_post(self, *, task_id: str, summary: str) -> None:
        self.findings.append(summary)


# ── _parse_pr_url ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (_PR_URL, ("agent-lore/lithos-loom", 7)),
        ("https://github.com/o/r/pull/123", ("o/r", 123)),
        ("https://github.com/o/r/issues/5", (None, None)),  # issue, not pull
        ("https://github.com/o/r/pull/notanum", (None, None)),
        ("https://example.com/o/r/pull/1", (None, None)),
        ("not a url", (None, None)),
        (None, (None, None)),
    ],
)
def test_parse_pr_url(url: object, expected: tuple[str | None, int | None]) -> None:
    assert _parse_pr_url(url) == expected


# ── reconcile_develop_pr ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_merged_pr_completes_task_and_marks() -> None:
    github = AsyncMock()
    github.get_pull_request.return_value = _pr(state="closed", merged=True)
    lithos = AsyncMock()
    task = _task({"develop_pr_url": _PR_URL})

    outcome = await reconcile_develop_pr(task, github, _ctx(lithos))

    assert outcome == "merged"
    lithos.task_complete.assert_awaited_once_with(task_id="t-1")
    lithos.task_update.assert_awaited_once_with(
        task_id="t-1",
        metadata={MERGE_STATE_KEY: "merged", MERGE_STATE_URL_KEY: _PR_URL},
    )
    lithos.finding_post.assert_not_awaited()


@pytest.mark.asyncio
async def test_merged_pr_persists_marker_on_completed_task() -> None:
    """#111: the ``merged`` marker must survive on the now-terminal task. Drives
    a stateful fake (post-lithos#303: ``task_update`` applies to a terminal
    task) end-to-end — the regression guard the lenient ``AsyncMock`` couldn't
    be: complete-then-mark with the mark landing on a completed task."""
    github = AsyncMock()
    github.get_pull_request.return_value = _pr(state="closed", merged=True)
    lithos = _StatefulLithos({"develop_pr_url": _PR_URL, "loom_delivered": True})
    task = _task({"develop_pr_url": _PR_URL})

    outcome = await reconcile_develop_pr(task, github, _ctx(lithos))

    assert outcome == "merged"
    assert lithos.status == "completed"
    assert lithos.metadata[MERGE_STATE_KEY] == "merged"
    assert lithos.metadata[MERGE_STATE_URL_KEY] == _PR_URL
    # the additive merge preserves pre-existing metadata
    assert lithos.metadata["develop_pr_url"] == _PR_URL
    assert lithos.metadata["loom_delivered"] is True
    assert lithos.findings == []


@pytest.mark.asyncio
async def test_closed_unmerged_leaves_open_with_finding() -> None:
    github = AsyncMock()
    github.get_pull_request.return_value = _pr(state="closed", merged=False)
    lithos = AsyncMock()
    task = _task({"develop_pr_url": _PR_URL})

    outcome = await reconcile_develop_pr(task, github, _ctx(lithos))

    assert outcome == "closed_unmerged"
    lithos.task_complete.assert_not_awaited()
    summary = lithos.finding_post.await_args.kwargs["summary"]
    assert summary.startswith(DELIVERED_PR_CLOSED)
    lithos.task_update.assert_awaited_once_with(
        task_id="t-1",
        metadata={MERGE_STATE_KEY: "closed_unmerged", MERGE_STATE_URL_KEY: _PR_URL},
    )


@pytest.mark.asyncio
async def test_open_pr_is_noop() -> None:
    github = AsyncMock()
    github.get_pull_request.return_value = _pr(state="open", merged=False)
    lithos = AsyncMock()

    outcome = await reconcile_develop_pr(
        _task({"develop_pr_url": _PR_URL}), github, _ctx(lithos)
    )

    assert outcome == "still_open"
    lithos.task_complete.assert_not_awaited()
    lithos.finding_post.assert_not_awaited()
    lithos.task_update.assert_not_awaited()  # no marker → re-poll next sweep


@pytest.mark.asyncio
async def test_deleted_pr_marks_gone_with_finding() -> None:
    github = AsyncMock()
    github.get_pull_request.return_value = None  # 404
    lithos = AsyncMock()

    outcome = await reconcile_develop_pr(
        _task({"develop_pr_url": _PR_URL}), github, _ctx(lithos)
    )

    assert outcome == "gone"
    lithos.task_complete.assert_not_awaited()
    assert DELIVERED_PR_CLOSED in lithos.finding_post.await_args.kwargs["summary"]
    lithos.task_update.assert_awaited_once_with(
        task_id="t-1", metadata={MERGE_STATE_KEY: "gone", MERGE_STATE_URL_KEY: _PR_URL}
    )


@pytest.mark.asyncio
async def test_terminal_marker_for_same_url_skips_without_github_call() -> None:
    """De-dup: a marker terminal for the CURRENT develop_pr_url is a skip."""
    github = AsyncMock()
    lithos = AsyncMock()
    task = _task(
        {
            "develop_pr_url": _PR_URL,
            MERGE_STATE_KEY: "merged",
            MERGE_STATE_URL_KEY: _PR_URL,  # same url it resolved
        }
    )

    outcome = await reconcile_develop_pr(task, github, _ctx(lithos))

    assert outcome is None
    github.get_pull_request.assert_not_awaited()  # de-dup: no re-poll
    lithos.task_complete.assert_not_awaited()


@pytest.mark.asyncio
async def test_replacement_pr_recovers_after_abandoned_one() -> None:
    """The natural recovery path: an old PR was abandoned (marker
    closed_unmerged for the OLD url), the task was re-developed into a NEW PR
    (develop_pr_url changed). The stale marker must NOT suppress the new PR —
    it's re-evaluated and, on merge, completes."""
    github = AsyncMock()
    github.get_pull_request.return_value = _pr(state="closed", merged=True)
    lithos = AsyncMock()
    task = _task(
        {
            "develop_pr_url": _PR_URL_2,  # the replacement PR
            MERGE_STATE_KEY: "closed_unmerged",
            MERGE_STATE_URL_KEY: _PR_URL,  # stale: points at the abandoned PR
        }
    )

    outcome = await reconcile_develop_pr(task, github, _ctx(lithos))

    assert outcome == "merged"
    github.get_pull_request.assert_awaited_once_with("agent-lore/lithos-loom", 8)
    lithos.task_complete.assert_awaited_once_with(task_id="t-1")
    lithos.task_update.assert_awaited_once_with(
        task_id="t-1",
        metadata={MERGE_STATE_KEY: "merged", MERGE_STATE_URL_KEY: _PR_URL_2},
    )


@pytest.mark.asyncio
async def test_replacement_pr_also_closed_reposts_for_new_url() -> None:
    """A replacement PR that's also closed-unmerged re-posts the finding (it's a
    different url than the recorded marker) and re-marks for the new url."""
    github = AsyncMock()
    github.get_pull_request.return_value = _pr(state="closed", merged=False)
    lithos = AsyncMock()
    task = _task(
        {
            "develop_pr_url": _PR_URL_2,
            MERGE_STATE_KEY: "closed_unmerged",
            MERGE_STATE_URL_KEY: _PR_URL,
        }
    )

    outcome = await reconcile_develop_pr(task, github, _ctx(lithos))

    assert outcome == "closed_unmerged"
    assert DELIVERED_PR_CLOSED in lithos.finding_post.await_args.kwargs["summary"]
    lithos.task_update.assert_awaited_once_with(
        task_id="t-1",
        metadata={MERGE_STATE_KEY: "closed_unmerged", MERGE_STATE_URL_KEY: _PR_URL_2},
    )


@pytest.mark.asyncio
async def test_issue_linked_task_is_skipped() -> None:
    """Issue-linked tasks close via the existing issue mirror — don't double-handle."""
    github = AsyncMock()
    lithos = AsyncMock()
    task = _task(
        {
            "develop_pr_url": _PR_URL,
            "github_issue_url": "https://github.com/o/r/issues/3",
        }
    )

    assert await reconcile_develop_pr(task, github, _ctx(lithos)) is None
    github.get_pull_request.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_develop_pr_url_is_skipped() -> None:
    github = AsyncMock()
    assert await reconcile_develop_pr(_task({}), github, _ctx(AsyncMock())) is None
    github.get_pull_request.assert_not_awaited()


@pytest.mark.asyncio
async def test_complete_swallows_task_not_found_and_still_marks() -> None:
    """An already-terminal task (operator completed it first) is a no-op, not crash."""
    github = AsyncMock()
    github.get_pull_request.return_value = _pr(state="closed", merged=True)
    lithos = AsyncMock()
    lithos.task_complete.side_effect = LithosClientError("task_not_found", "gone")
    task = _task({"develop_pr_url": _PR_URL})

    outcome = await reconcile_develop_pr(task, github, _ctx(lithos))

    assert outcome == "merged"  # no exception escaped
    lithos.task_update.assert_awaited_once_with(
        task_id="t-1",
        metadata={MERGE_STATE_KEY: "merged", MERGE_STATE_URL_KEY: _PR_URL},
    )


@pytest.mark.asyncio
async def test_malformed_url_frictions_and_marks_unparseable() -> None:
    github = AsyncMock()
    lithos = AsyncMock()
    task = _task({"develop_pr_url": "https://github.com/o/r/pull/notanum"})

    outcome = await reconcile_develop_pr(task, github, _ctx(lithos))

    assert outcome == "unparseable"
    github.get_pull_request.assert_not_awaited()
    assert "[Friction]" in lithos.finding_post.await_args.kwargs["summary"]
    lithos.task_update.assert_awaited_once_with(
        task_id="t-1",
        metadata={
            MERGE_STATE_KEY: "unparseable",
            MERGE_STATE_URL_KEY: "https://github.com/o/r/pull/notanum",
        },
    )


@pytest.mark.asyncio
async def test_transient_github_error_leaves_marker_unset() -> None:
    """A GH 5xx/auth blip must not mark the task terminal — retry next sweep."""
    github = AsyncMock()
    github.get_pull_request.side_effect = GitHubError("503 server error")
    lithos = AsyncMock()
    task = _task({"develop_pr_url": _PR_URL})

    outcome = await reconcile_develop_pr(task, github, _ctx(lithos))  # no raise

    assert outcome == "error"
    lithos.task_complete.assert_not_awaited()
    lithos.task_update.assert_not_awaited()  # marker unset → retried next sweep
    lithos.finding_post.assert_not_awaited()


# ── reconcile_develop_pr: pr_gate_id skip (Epic H soak) ────────────────


async def test_develop_sweep_skips_a_task_owned_by_a_pr_gate() -> None:
    """A delivered task that also carries pr_gate_id is owned by the gate
    resolver; the legacy url sweep stands aside so the two never both act."""
    github = AsyncMock()
    lithos = AsyncMock()
    task = _task({"develop_pr_url": _PR_URL, STORY_GATE_ID_KEY: "gate-1"})

    outcome = await reconcile_develop_pr(task, github, _ctx(lithos))

    assert outcome is None
    github.get_pull_request.assert_not_awaited()
    lithos.task_complete.assert_not_awaited()


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
