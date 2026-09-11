"""Subprocess + smoke tests for the github-watcher child entry.

Confirms the supervisor can ``python -m`` the child cleanly. Without a
real Lithos and a real ``gh`` login the child can't actually do work,
but the disabled-gate path returns 0 immediately and is testable in
the CI sandbox.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from textwrap import dedent
from typing import Any

from lithos_loom.children.github_watcher import _run_reconcile_pass


def _no_watcher_config(tmp_path: Path) -> Path:
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        dedent(
            """
            [orchestrator]
            agent_id = "lithos-orchestrator-test"
            lithos_url = "http://localhost:8765"
            """
        )
    )
    return cfg


def _disabled_watcher_config(tmp_path: Path) -> Path:
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        dedent(
            """
            [orchestrator]
            agent_id = "lithos-orchestrator-test"
            lithos_url = "http://localhost:8765"

            [github_watcher]
            enabled = false
            """
        )
    )
    return cfg


async def test_github_watcher_child_exits_nonzero_without_section(
    tmp_path: Path,
) -> None:
    """Defensive: section missing → child exits non-zero so supervisor sees it."""
    cfg = _no_watcher_config(tmp_path)
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "lithos_loom.children.github_watcher",
        "--config",
        str(cfg),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    rc = await asyncio.wait_for(proc.wait(), timeout=10.0)
    assert rc == 1


async def test_github_watcher_child_exits_nonzero_when_disabled(
    tmp_path: Path,
) -> None:
    """Same defensive behaviour when the section is present but enabled=false."""
    cfg = _disabled_watcher_config(tmp_path)
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "lithos_loom.children.github_watcher",
        "--config",
        str(cfg),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    rc = await asyncio.wait_for(proc.wait(), timeout=10.0)
    assert rc == 1


async def test_github_watcher_child_module_is_importable() -> None:
    """The child module must expose ``main`` and be runnable via -m."""
    import lithos_loom.children.github_watcher as mod

    assert callable(mod.main)


async def test_github_watcher_child_wires_both_directions() -> None:
    """Slice 7.2: child imports both the GH→Lithos sync handler and the
    Lithos→GH push handler. A regression here (e.g. circular import or
    rename without updating the child) would crash at module load."""
    import lithos_loom.children.github_watcher as mod

    # Both handler factories must be in scope on the module.
    assert callable(mod.make_github_issue_sync_handler)
    assert callable(mod.make_github_issue_push_handler)
    # And both event-type constants used by the bus subscribe calls.
    assert mod.GITHUB_ISSUE_EVENT_TYPE == "github.issue.seen"
    assert "lithos.task.completed" in mod.LITHOS_TASK_EVENT_TYPES
    assert "lithos.task.cancelled" in mod.LITHOS_TASK_EVENT_TYPES
    assert "lithos.task.updated" in mod.LITHOS_TASK_EVENT_TYPES


async def test_reconcile_pass_redispatches_gh_linked_tasks() -> None:
    """PR-review finding 4 (round 5, 2026-05-30): the periodic
    reconciliation pass scans Lithos for tasks carrying
    metadata.github_issue_url and re-dispatches them through the push
    handler. GH-unlinked tasks must be skipped — they're noise.

    Round 6 update: terminal tasks now also fire ``task.updated`` so a
    rename dropped during a long outage gets reconciled alongside the
    close event."""
    import logging
    from datetime import UTC, datetime, timedelta
    from unittest.mock import AsyncMock

    from lithos_loom.lithos_client import Task
    from lithos_loom.subscriptions import SubscriptionContext

    open_linked = Task(
        id="task-a",
        title="Linked",
        status="open",
        tags=("github-issue",),
        metadata={"github_issue_url": "https://github.com/x/y/issues/1"},
        claims=(),
    )
    open_unlinked = Task(
        id="task-b",
        title="No GH link",
        status="open",
        tags=(),
        metadata={"project": "other"},
        claims=(),
    )
    completed_linked = Task(
        id="task-c",
        title="Done linked",
        status="completed",
        tags=("github-issue",),
        metadata={"github_issue_url": "https://github.com/x/y/issues/2"},
        claims=(),
        resolved_at=datetime(2026, 5, 29, tzinfo=UTC),
    )
    lithos = AsyncMock()
    lithos.task_list = AsyncMock(
        side_effect=[
            [open_linked, open_unlinked],  # open
            [completed_linked],  # completed
            [],  # cancelled
        ]
    )
    handler_calls: list[str] = []

    async def push_handler(event: Any, _ctx: Any) -> None:
        handler_calls.append(event.type)

    ctx = SubscriptionContext(
        lithos=lithos,
        logger=logging.getLogger("test-reconcile"),
        agent_id="test-agent",
    )
    await _run_reconcile_pass(
        lithos=lithos,
        push_handler=push_handler,
        ctx=ctx,
        resolved_window=timedelta(days=7),
        github=AsyncMock(),
        pr_merge_enabled=False,
    )
    # Open task → one updated event (title sync).
    # Terminal task → updated + close so title drift is reconciled too.
    # GH-unlinked task was filtered out.
    assert handler_calls == [
        "lithos.task.updated",  # open_linked
        "lithos.task.updated",  # completed_linked title
        "lithos.task.completed",  # completed_linked close
    ]


async def test_reconcile_pass_skips_terminal_scan_when_window_disabled() -> None:
    """PR-review finding 1 (round 6, 2026-05-30): resolved_replay_days=0
    means "operator opted out of resolved replay". The sweep used to
    treat that as resolved_since=None and walk *every* terminal task
    ever, which grows unboundedly. The terminal scans must skip
    entirely while the open-task title sweep still runs.
    """
    import logging
    from unittest.mock import AsyncMock

    from lithos_loom.lithos_client import Task
    from lithos_loom.subscriptions import SubscriptionContext

    open_linked = Task(
        id="task-a",
        title="Linked",
        status="open",
        tags=("github-issue",),
        metadata={"github_issue_url": "https://github.com/x/y/issues/1"},
        claims=(),
    )
    lithos = AsyncMock()
    lithos.task_list = AsyncMock(return_value=[open_linked])
    handler_calls: list[str] = []

    async def push_handler(event: Any, _ctx: Any) -> None:
        handler_calls.append(event.type)

    ctx = SubscriptionContext(
        lithos=lithos,
        logger=logging.getLogger("test-reconcile"),
        agent_id="test-agent",
    )
    await _run_reconcile_pass(
        lithos=lithos,
        push_handler=push_handler,
        ctx=ctx,
        resolved_window=None,
        github=AsyncMock(),
        pr_merge_enabled=False,
    )
    # Only the open-task scan ran; no completed / cancelled queries.
    assert lithos.task_list.await_count == 1
    assert lithos.task_list.await_args.kwargs["status"] == "open"
    # And only the open task's title sync fired.
    assert handler_calls == ["lithos.task.updated"]


async def test_reconcile_pass_resolves_open_pr_gates_when_enabled() -> None:
    """Epic H: the sweep enumerates open `pr` gates and resolves them — here a
    merged PR completes the gate. Routing correctness (the gate branch fires);
    resolution detail lives in test_develop_pr_merge.py."""
    import logging
    from unittest.mock import AsyncMock

    from lithos_loom.github_client import PullRequest
    from lithos_loom.lithos_client import Task
    from lithos_loom.subscriptions import SubscriptionContext

    gate = Task(
        id="gate-1",
        title="Awaiting merge: US9",
        status="open",
        tags=(),
        metadata={
            "gate_type": "pr",
            "repo": "o/r",
            "pr_number": 9,
            "pr_url": "https://github.com/o/r/pull/9",
        },
        claims=(),
        task_type="gate",
    )
    lithos = AsyncMock()
    lithos.task_list = AsyncMock(return_value=[gate])
    lithos.task_edge_list = AsyncMock(return_value=[])  # orphan gate — no story
    github = AsyncMock()
    github.get_pull_request = AsyncMock(
        return_value=PullRequest(
            repo="o/r",
            number=9,
            state="closed",
            merged=True,
            merged_at=None,
            merge_commit_sha="sha9",
        )
    )
    ctx = SubscriptionContext(
        lithos=lithos, logger=logging.getLogger("test-gate"), agent_id="a"
    )
    await _run_reconcile_pass(
        lithos=lithos,
        push_handler=AsyncMock(),
        ctx=ctx,
        resolved_window=None,
        github=github,
        pr_merge_enabled=True,
    )
    github.get_pull_request.assert_awaited_once_with("o/r", 9)
    lithos.task_complete.assert_awaited_once_with(task_id="gate-1")


async def test_reconcile_pass_skips_gates_when_pr_poll_disabled() -> None:
    """A `pr` gate is not resolved when pr_merge_poll_enabled=false."""
    import logging
    from unittest.mock import AsyncMock

    from lithos_loom.lithos_client import Task
    from lithos_loom.subscriptions import SubscriptionContext

    gate = Task(
        id="gate-1",
        title="Awaiting merge",
        status="open",
        tags=(),
        metadata={"gate_type": "pr", "repo": "o/r", "pr_number": 9, "pr_url": "u"},
        claims=(),
        task_type="gate",
    )
    lithos = AsyncMock()
    lithos.task_list = AsyncMock(return_value=[gate])
    github = AsyncMock()
    ctx = SubscriptionContext(
        lithos=lithos, logger=logging.getLogger("test-gate"), agent_id="a"
    )
    await _run_reconcile_pass(
        lithos=lithos,
        push_handler=AsyncMock(),
        ctx=ctx,
        resolved_window=None,
        github=github,
        pr_merge_enabled=False,
    )
    github.get_pull_request.assert_not_awaited()


async def test_reconcile_pass_ignores_bare_develop_pr_url_task() -> None:
    """US11: the legacy develop_pr_url story sweep is gone. A plain open task
    carrying develop_pr_url (but not a `pr` gate) is NOT swept — no PR fetch, no
    completion — even with pr_merge_enabled=True. Guards against silently
    reintroducing the develop_pr_url branch."""
    import logging
    from unittest.mock import AsyncMock

    from lithos_loom.lithos_client import Task
    from lithos_loom.subscriptions import SubscriptionContext

    develop_task = Task(
        id="task-pr",
        title="delivered PR task",
        status="open",
        tags=("trigger:story-develop",),
        metadata={"develop_pr_url": "https://github.com/o/r/pull/9"},
        claims=(),
    )
    lithos = AsyncMock()
    lithos.task_list = AsyncMock(return_value=[develop_task])
    github = AsyncMock()
    ctx = SubscriptionContext(
        lithos=lithos, logger=logging.getLogger("test-bare-pr"), agent_id="a"
    )
    await _run_reconcile_pass(
        lithos=lithos,
        push_handler=AsyncMock(),
        ctx=ctx,
        resolved_window=None,
        github=github,
        pr_merge_enabled=True,
    )
    github.get_pull_request.assert_not_awaited()
    lithos.task_complete.assert_not_awaited()


# The child's configure_logging boot code moved to children/_boot.py
# (ARCH-6); its MCP-SSE-pin behaviour is pinned once in
# tests/test_child_boot.py.


async def test_reconcile_pass_threads_external_review_ingestion() -> None:
    """PRD S2: with external_reviews_enabled the still-open gate branch also
    reads the PR's reviews; without it (default) it stays a pure merge poll.
    Ingestion detail lives in test_external_reviews.py."""
    import logging
    from unittest.mock import AsyncMock

    from lithos_loom.github_client import PullRequest
    from lithos_loom.lithos_client import Task
    from lithos_loom.subscriptions import SubscriptionContext

    def _gate() -> Task:
        return Task(
            id="gate-1",
            title="Awaiting merge: US9",
            status="open",
            tags=(),
            metadata={
                "gate_type": "pr",
                "repo": "o/r",
                "pr_number": 9,
                "pr_url": "https://github.com/o/r/pull/9",
            },
            claims=(),
            task_type="gate",
        )

    def _github() -> AsyncMock:
        github = AsyncMock()
        github.get_pull_request = AsyncMock(
            return_value=PullRequest(
                repo="o/r",
                number=9,
                state="open",
                merged=False,
                merged_at=None,
                merge_commit_sha=None,
            )
        )
        github.list_pull_request_reviews = AsyncMock(return_value=[])
        github.list_pull_request_review_comments = AsyncMock(return_value=[])
        return github

    for enabled, expect_review_fetch in ((True, True), (False, False)):
        lithos = AsyncMock()
        lithos.task_list = AsyncMock(return_value=[_gate()])
        lithos.task_edge_list = AsyncMock(return_value=[])
        github = _github()
        ctx = SubscriptionContext(
            lithos=lithos, logger=logging.getLogger("test-gate"), agent_id="a"
        )
        await _run_reconcile_pass(
            lithos=lithos,
            push_handler=AsyncMock(),
            ctx=ctx,
            resolved_window=None,
            github=github,
            pr_merge_enabled=True,
            external_reviews_enabled=enabled,
        )
        assert github.list_pull_request_reviews.await_count == (
            1 if expect_review_fetch else 0
        ), f"external_reviews_enabled={enabled}"


async def test_reconcile_pass_threads_the_merge_gate_to_the_gate_branch() -> None:
    """PRD S3 watcher half: a supplied merge-gate dispatcher sees every
    still-open pr gate's fetched PR."""
    import logging
    from unittest.mock import AsyncMock

    from lithos_loom.github_client import PullRequest
    from lithos_loom.lithos_client import Task
    from lithos_loom.subscriptions import SubscriptionContext

    gate = Task(
        id="gate-1",
        title="Awaiting merge: US9",
        status="open",
        tags=(),
        metadata={
            "gate_type": "pr",
            "repo": "o/r",
            "pr_number": 9,
            "pr_url": "https://github.com/o/r/pull/9",
        },
        claims=(),
        task_type="gate",
    )
    lithos = AsyncMock()
    lithos.task_list = AsyncMock(return_value=[gate])
    lithos.task_edge_list = AsyncMock(return_value=[])
    github = AsyncMock()
    github.get_pull_request = AsyncMock(
        return_value=PullRequest(
            repo="o/r",
            number=9,
            state="open",
            merged=False,
            merged_at=None,
            merge_commit_sha=None,
            head_sha="e" * 40,
            base_sha="b" * 40,
            mergeable=True,
            mergeable_state="behind",
        )
    )
    ctx = SubscriptionContext(
        lithos=lithos, logger=logging.getLogger("test-gate"), agent_id="a"
    )
    seen: list[str] = []

    class _MergeGate:
        async def consider(self, gate, spec, story_id, pr, ctx, *, hold):
            seen.append(pr.head_sha)
            return "unchanged"

    await _run_reconcile_pass(
        lithos=lithos,
        push_handler=AsyncMock(),
        ctx=ctx,
        resolved_window=None,
        github=github,
        pr_merge_enabled=True,
        merge_gate=_MergeGate(),  # type: ignore[arg-type]
    )
    assert seen == ["e" * 40]


async def test_reconcile_pass_threads_conflict_resolution_to_the_gate_branch() -> None:
    """PRD S5 watcher half: a supplied conflict-resolve dispatcher sees every
    still-open pr gate's fetched PR."""
    import logging
    from unittest.mock import AsyncMock

    from lithos_loom.github_client import PullRequest
    from lithos_loom.lithos_client import Task
    from lithos_loom.subscriptions import SubscriptionContext

    gate = Task(
        id="gate-1",
        title="Awaiting merge: US9",
        status="open",
        tags=(),
        metadata={
            "gate_type": "pr",
            "repo": "o/r",
            "pr_number": 9,
            "pr_url": "https://github.com/o/r/pull/9",
        },
        claims=(),
        task_type="gate",
    )
    lithos = AsyncMock()
    lithos.task_list = AsyncMock(return_value=[gate])
    lithos.task_edge_list = AsyncMock(return_value=[])
    github = AsyncMock()
    github.get_pull_request = AsyncMock(
        return_value=PullRequest(
            repo="o/r",
            number=9,
            state="open",
            merged=False,
            merged_at=None,
            merge_commit_sha=None,
            head_sha="e" * 40,
            base_sha="b" * 40,
            mergeable=True,
            mergeable_state="behind",
        )
    )
    ctx = SubscriptionContext(
        lithos=lithos, logger=logging.getLogger("test-gate"), agent_id="a"
    )
    seen: list[str] = []

    class _Resolver:
        async def recover_debt(self, gate, spec, story_id, ctx):
            return None

        async def consider(self, gate, spec, story_id, pr, ctx, *, hold):
            seen.append(pr.head_sha)
            return "unchanged"

    await _run_reconcile_pass(
        lithos=lithos,
        push_handler=AsyncMock(),
        ctx=ctx,
        resolved_window=None,
        github=github,
        pr_merge_enabled=True,
        conflict_resolve=_Resolver(),  # type: ignore[arg-type]
    )
    assert seen == ["e" * 40]


async def test_reconcile_pass_threads_remediation_to_the_gate_branch() -> None:
    """Slice C: when a remediation dispatcher is supplied, the still-open gate
    branch observes the PR head through it (the S5b budget seam). Dispatch
    detail lives in test_external_remediation.py."""
    import logging
    from pathlib import Path
    from unittest.mock import AsyncMock

    from lithos_loom.github_client import PullRequest
    from lithos_loom.lithos_client import Task
    from lithos_loom.subscriptions import SubscriptionContext
    from lithos_loom.subscriptions.external_remediation import (
        ExternalRemediation,
        RemediationSettings,
    )

    gate = Task(
        id="gate-1",
        title="Awaiting merge: US9",
        status="open",
        tags=(),
        metadata={
            "gate_type": "pr",
            "repo": "o/r",
            "pr_number": 9,
            "pr_url": "https://github.com/o/r/pull/9",
        },
        claims=(),
        task_type="gate",
    )
    lithos = AsyncMock()
    lithos.task_list = AsyncMock(return_value=[gate])
    lithos.task_edge_list = AsyncMock(return_value=[])
    github = AsyncMock()
    github.get_pull_request = AsyncMock(
        return_value=PullRequest(
            repo="o/r",
            number=9,
            state="open",
            merged=False,
            merged_at=None,
            merge_commit_sha=None,
            head_sha="e" * 40,
        )
    )
    github.list_pull_request_reviews = AsyncMock(return_value=[])
    github.list_pull_request_review_comments = AsyncMock(return_value=[])
    ctx = SubscriptionContext(
        lithos=lithos, logger=logging.getLogger("test-gate"), agent_id="a"
    )

    observed: list[str] = []
    rem = ExternalRemediation(
        RemediationSettings(
            trusted_bots=(), budget=2, projects={}, work_dir=Path("/tmp/x")
        )
    )

    original = rem.observe_head

    async def spying_observe(gate, spec, pr, ctx):
        observed.append(pr.head_sha)
        return await original(gate, spec, pr, ctx)

    rem.observe_head = spying_observe  # type: ignore[method-assign]

    await _run_reconcile_pass(
        lithos=lithos,
        push_handler=AsyncMock(),
        ctx=ctx,
        resolved_window=None,
        github=github,
        pr_merge_enabled=True,
        external_reviews_enabled=True,
        remediation=rem,
    )

    assert observed == ["e" * 40]


async def test_reconcile_pass_settles_probing_gates_after_every_probe_launched() -> (
    None
):
    """Review #369 round 3: a gate whose re-gate is `probing` gets its state
    written from the probe's answer, after the loop — every probe is out
    before the sweep waits on any of them (review #362 F5)."""
    import logging
    from unittest.mock import AsyncMock

    from lithos_loom.github_client import PullRequest
    from lithos_loom.lithos_client import Task
    from lithos_loom.subscriptions import SubscriptionContext
    from lithos_loom.subscriptions.merge_gate_record import MergeGateRecord

    def _gate(n: int) -> Task:
        url = f"https://github.com/o/r/pull/{n}"
        # a prior green the probe is out re-verifying
        green = MergeGateRecord(
            pr_url=url,
            head_sha="e" * 40,
            base_sha="b" * 40,
            status="green",
            verdict="GREEN",
        )
        return Task(
            id=f"gate-{n}",
            title=f"Awaiting merge: US{n}",
            status="open",
            tags=(),
            metadata={
                "gate_type": "pr",
                "repo": "o/r",
                "pr_number": n,
                "pr_url": url,
                "merge_gate": green.as_marker(),
            },
            claims=(),
            task_type="gate",
        )

    gates = [_gate(1), _gate(2)]
    lithos = AsyncMock()
    lithos.task_list = AsyncMock(return_value=gates)
    lithos.task_edge_list = AsyncMock(return_value=[])
    lithos.task_get = AsyncMock(
        side_effect=lambda task_id: next(g for g in gates if g.id == task_id)
    )
    github = AsyncMock()
    github.get_pull_request = AsyncMock(
        side_effect=lambda repo, number: PullRequest(
            repo="o/r",
            number=number,
            state="open",
            merged=False,
            merged_at=None,
            merge_commit_sha=None,
            head_sha="e" * 40,
            base_ref="main",
            base_sha="b" * 40,
            mergeable=True,
            mergeable_state="clean",
        )
    )
    github.get_branch_tip = AsyncMock(return_value="b" * 40)
    ctx = SubscriptionContext(
        lithos=lithos, logger=logging.getLogger("test-gate"), agent_id="a"
    )
    order: list[str] = []

    import asyncio

    gate_2_settled = asyncio.Event()

    class _MergeGate:
        def busy_on(self, pr_url: str) -> bool:
            return False

        async def consider(self, gate, spec, story_id, pr, ctx, *, hold):
            order.append(f"consider {gate.id}")
            return "probing"

        async def settle_probe(self, gate_id: str) -> str:
            order.append(f"settle {gate_id}")
            if gate_id == "gate-1":
                # a slow probe on the first gate must not hold the second's
                # answer: the settles are awaited concurrently
                await asyncio.wait_for(gate_2_settled.wait(), 2)
            else:
                gate_2_settled.set()
            return "unchanged"

    await _run_reconcile_pass(
        lithos=lithos,
        push_handler=AsyncMock(),
        ctx=ctx,
        resolved_window=None,
        github=github,
        pr_merge_enabled=True,
        merge_gate=_MergeGate(),  # type: ignore[arg-type]
    )
    assert order == [
        "consider gate-1",
        "consider gate-2",
        "settle gate-1",
        "settle gate-2",
    ]
    written = [
        c.kwargs.get("metadata", {}).get("reconciliation_state")
        for c in lithos.task_update.call_args_list
    ]
    assert written.count("ready_to_merge") == 2


# ── loom human gates in the sweep: hygiene + the stranding poll (04c2448b) ──


async def _sweep_client() -> Any:
    from tests.support import FakeLithosClient

    return FakeLithosClient(agent_id="a")


def _sweep_ctx(lithos: Any) -> Any:
    import logging

    from lithos_loom.subscriptions import SubscriptionContext

    return SubscriptionContext(
        lithos=lithos, logger=logging.getLogger("test-human-gate"), agent_id="a"
    )


async def _loom_human_gate(
    client: Any, *, reason: str = "max_rounds", brief: dict[str, Any] | None = None
) -> tuple[str, str]:
    from lithos_loom.gates import create_human_gate

    story = await client.task_create(title="US7", metadata={"project": "p"})
    gate_id = await create_human_gate(
        client,
        story_id=story,
        story_title="US7",
        project="p",
        agent="a",
        route="story-develop",
        reason=reason,
        summary="stopped",
        brief=brief,
    )
    return story, gate_id


async def test_reconcile_pass_completes_loom_human_gates_whose_waiter_resolved() -> (
    None
):
    """The sweep keeps loom's own gates honest: a story completed or cancelled
    by hand leaves its human gate open on every board until this runs. The
    operator's own human gates are never touched."""
    from unittest.mock import AsyncMock

    client = await _sweep_client()
    done_story, done_gate = await _loom_human_gate(client)
    await client.task_complete(task_id=done_story)
    live_story, live_gate = await _loom_human_gate(client)
    # an operator's own human gate on a resolved story: theirs to run
    own_story = await client.task_create(title="mine")
    own_gate = await client.task_create(
        title="my gate", task_type="gate", metadata={"gate_type": "human"}
    )
    await client.task_edge_upsert(
        from_task_id=own_gate, to_task_id=own_story, type="waits_on_gate", agent="a"
    )
    await client.task_complete(task_id=own_story)

    await _run_reconcile_pass(
        lithos=client,
        push_handler=AsyncMock(),
        ctx=_sweep_ctx(client),
        resolved_window=None,
        github=AsyncMock(),
        pr_merge_enabled=False,
    )

    async def _status(task_id: str) -> str:
        task = await client.task_get(task_id=task_id)
        assert task is not None
        return task.status

    assert await _status(done_gate) == "completed"
    assert await _status(live_gate) == "open"
    assert await _status(own_gate) == "open"


async def test_reconcile_pass_polls_stranding_human_gates_for_a_merge() -> None:
    """A human gate raised for a closed PR watches that PR: reopened and merged
    → the story is completed (first), then the gate."""
    from unittest.mock import AsyncMock

    from lithos_loom.github_client import PullRequest

    client = await _sweep_client()
    story, gate_id = await _loom_human_gate(
        client,
        reason="pr_closed_unmerged",
        brief={"pr_url": "https://github.com/o/r/pull/9"},
    )
    github = AsyncMock()
    github.get_pull_request = AsyncMock(
        return_value=PullRequest(
            repo="o/r",
            number=9,
            state="closed",
            merged=True,
            merged_at=None,
            merge_commit_sha="sha9",
        )
    )

    await _run_reconcile_pass(
        lithos=client,
        push_handler=AsyncMock(),
        ctx=_sweep_ctx(client),
        resolved_window=None,
        github=github,
        pr_merge_enabled=True,
    )

    github.get_pull_request.assert_awaited_once_with("o/r", 9)
    assert [c["task_id"] for c in client.calls_to("task_complete")] == [story, gate_id]


async def test_reconcile_pass_does_not_poll_stranding_gates_when_pr_poll_disabled() -> (
    None
):
    from unittest.mock import AsyncMock

    client = await _sweep_client()
    await _loom_human_gate(
        client,
        reason="pr_closed_unmerged",
        brief={"pr_url": "https://github.com/o/r/pull/9"},
    )
    github = AsyncMock()

    await _run_reconcile_pass(
        lithos=client,
        push_handler=AsyncMock(),
        ctx=_sweep_ctx(client),
        resolved_window=None,
        github=github,
        pr_merge_enabled=False,
    )

    github.get_pull_request.assert_not_awaited()


async def test_reconcile_pass_threads_the_notifier_to_the_gate_branch() -> None:
    """The stranding conversion fires the push sinks — the same notifier the
    other two dispatchers hold, handed to the sweep."""
    from unittest.mock import AsyncMock

    from lithos_loom.gates import create_pr_gate
    from lithos_loom.github_client import PullRequest

    class _Notifier:
        def __init__(self) -> None:
            self.notices: list[Any] = []

        async def needs_human(self, notice: Any) -> list[str]:
            self.notices.append(notice)
            return []

    client = await _sweep_client()
    story = await client.task_create(title="US7", metadata={"project": "p"})
    await create_pr_gate(
        client,
        story_id=story,
        story_title="US7",
        pr_url="https://github.com/o/r/pull/9",
        project="p",
        agent="a",
    )
    github = AsyncMock()
    github.get_pull_request = AsyncMock(
        return_value=PullRequest(
            repo="o/r",
            number=9,
            state="closed",
            merged=False,
            merged_at=None,
            merge_commit_sha=None,
        )
    )
    notifier = _Notifier()

    await _run_reconcile_pass(
        lithos=client,
        push_handler=AsyncMock(),
        ctx=_sweep_ctx(client),
        resolved_window=None,
        github=github,
        pr_merge_enabled=True,
        notifier=notifier,  # type: ignore[arg-type]
    )

    (notice,) = notifier.notices
    assert notice.story_id == story
    assert notice.reason == "pr_closed_unmerged"
