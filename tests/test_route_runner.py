"""Tests for ``lithos_loom.subscriptions.route_runner`` (Slice 0 US5).

The RouteRunner is a claim-bound subscriber: it subscribes to bus
``lithos.task.created`` / ``lithos.task.updated`` events filtered by the
route's tag match, claims matching open tasks via Lithos, runs the plugin
subprocess, and applies the resulting status (complete on succeeded,
release + ``[BlockerFailed]`` finding on failed). Tests inject fake
``lithos`` and patched ``run_plugin`` to exercise the dispatch logic
without HTTP or real subprocesses.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any
from unittest.mock import AsyncMock

import pytest

from lithos_loom.bus import Event, EventBus
from lithos_loom.config import RouteConfig, RouteMatch
from lithos_loom.errors import LithosClientError, PluginContractError
from lithos_loom.lithos_client import Task
from lithos_loom.subscriptions.route_runner import RouteRunner

# ── Helpers ────────────────────────────────────────────────────────────


def _route(
    name: str = "story-implement",
    *,
    tags: tuple[str, ...] = ("trigger:story-implement",),
    command: str = "echo {{task_json}} {{work_dir}} {{result_file}}",
    max_runtime_seconds: int | None = None,
) -> RouteConfig:
    return RouteConfig(
        name=name,
        command=command,
        match=RouteMatch(tags=tags),
        max_runtime_seconds=max_runtime_seconds,
    )


def _payload(
    task_id: str = "task-1",
    *,
    status: str = "open",
    tags: tuple[str, ...] = ("trigger:story-implement",),
    metadata: Mapping[str, Any] | None = None,
    claims: tuple[Mapping[str, Any], ...] = (),
) -> Mapping[str, Any]:
    return MappingProxyType(
        {
            "id": task_id,
            "title": "t",
            "status": status,
            "tags": list(tags),
            "metadata": dict(metadata or {}),
            "claims": [dict(c) for c in claims],
        }
    )


def _evt(
    type_: str = "lithos.task.created",
    payload: Mapping[str, Any] | None = None,
) -> Event:
    return Event(
        type=type_,
        timestamp=datetime.now(UTC),
        payload=payload if payload is not None else _payload(),
    )


def _make_runner(
    *,
    bus: EventBus,
    route: RouteConfig | None = None,
    lithos: AsyncMock | None = None,
    work_dir: Path,
    succeeded_result: dict[str, Any] | None = None,
    plugin_runner: Any = None,
) -> tuple[RouteRunner, AsyncMock]:
    if lithos is None:
        lithos = AsyncMock()
        lithos.task_claim.return_value = "2026-05-13T13:00:00Z"
    runner = RouteRunner(
        route=route or _route(),
        bus=bus,
        lithos=lithos,
        agent_id="lithos-orchestrator-test",
        work_dir_base=work_dir,
        renew_interval_seconds=3600,  # never fires in unit tests
        plugin_runner=plugin_runner
        or AsyncMock(
            return_value=succeeded_result
            or {
                "schema_version": 1,
                "task_id": "task-1",
                "status": "succeeded",
                "exit_code": 0,
            }
        ),
    )
    return runner, lithos


async def _run_for(runner: RouteRunner, *, seconds: float = 0.1) -> None:
    """Run the subscriber loop briefly, then cancel cleanly."""
    task = asyncio.create_task(runner.run())
    await asyncio.sleep(seconds)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# ── Filter / match behaviour ───────────────────────────────────────────


async def test_runner_skips_tasks_with_non_matching_tags(tmp_path: Path) -> None:
    bus = EventBus()
    runner, lithos = _make_runner(bus=bus, work_dir=tmp_path)

    await bus.publish(_evt(payload=_payload(tags=("trigger:other",))))
    await _run_for(runner)
    lithos.task_claim.assert_not_called()


async def test_runner_skips_non_open_tasks(tmp_path: Path) -> None:
    bus = EventBus()
    runner, lithos = _make_runner(bus=bus, work_dir=tmp_path)

    await bus.publish(_evt(payload=_payload(status="completed")))
    await _run_for(runner)
    lithos.task_claim.assert_not_called()


async def test_runner_skips_when_dependencies_not_completed(tmp_path: Path) -> None:
    bus = EventBus()
    lithos = AsyncMock()
    # task_status for the dep returns an open dep task — not completed.
    lithos.task_status.return_value = Task(
        id="dep-1", title="t", status="open", tags=(), metadata={}, claims=()
    )
    runner, _ = _make_runner(
        bus=bus,
        lithos=lithos,
        work_dir=tmp_path,
    )

    await bus.publish(_evt(payload=_payload(metadata={"depends_on": ["dep-1"]})))
    await _run_for(runner)
    lithos.task_status.assert_awaited_with(task_id="dep-1")
    lithos.task_claim.assert_not_called()


async def test_runner_runs_when_dependencies_are_completed(tmp_path: Path) -> None:
    bus = EventBus()
    lithos = AsyncMock()
    lithos.task_claim.return_value = "expires"
    lithos.task_status.return_value = Task(
        id="dep-1",
        title="t",
        status="completed",
        tags=(),
        metadata={},
        claims=(),
    )
    runner, _ = _make_runner(bus=bus, lithos=lithos, work_dir=tmp_path)

    await bus.publish(_evt(payload=_payload(metadata={"depends_on": ["dep-1"]})))
    await _run_for(runner)
    lithos.task_claim.assert_awaited_once()
    lithos.task_complete.assert_awaited_once()


# ── Claim race ─────────────────────────────────────────────────────────


async def test_runner_lost_claim_race_does_not_run_plugin(tmp_path: Path) -> None:
    bus = EventBus()
    lithos = AsyncMock()
    lithos.task_claim.side_effect = LithosClientError("claim_failed", "aspect taken")
    plugin_runner = AsyncMock()
    runner, _ = _make_runner(
        bus=bus,
        lithos=lithos,
        work_dir=tmp_path,
        plugin_runner=plugin_runner,
    )

    await bus.publish(_evt())
    await _run_for(runner)
    plugin_runner.assert_not_called()
    lithos.task_complete.assert_not_called()


# ── Success path ───────────────────────────────────────────────────────


async def test_runner_claims_runs_plugin_then_completes_task(
    tmp_path: Path,
) -> None:
    bus = EventBus()
    runner, lithos = _make_runner(bus=bus, work_dir=tmp_path)

    await bus.publish(_evt())
    await _run_for(runner)

    lithos.task_claim.assert_awaited_once()
    claim_args = lithos.task_claim.await_args.kwargs
    assert claim_args["task_id"] == "task-1"
    assert claim_args["agent"] == "lithos-orchestrator-test"
    assert claim_args["aspect"] == "story-implement"

    lithos.task_complete.assert_awaited_once()
    complete_args = lithos.task_complete.await_args.kwargs
    assert complete_args["task_id"] == "task-1"


async def test_runner_writes_task_json_to_work_dir(tmp_path: Path) -> None:
    """The plugin sees task.json with the event payload at invocation time."""
    bus = EventBus()
    seen_body: dict[str, Any] = {}

    async def capturing_plugin(**kwargs: Any) -> dict[str, Any]:
        # Read the task.json the runner wrote, before success cleanup.
        import json as _json

        seen_body.update(_json.loads(kwargs["task_json_path"].read_text()))
        return {
            "schema_version": 1,
            "task_id": "task-77",
            "status": "succeeded",
            "exit_code": 0,
        }

    runner, _ = _make_runner(bus=bus, work_dir=tmp_path, plugin_runner=capturing_plugin)

    await bus.publish(_evt(payload=_payload(task_id="task-77")))
    await _run_for(runner)

    assert seen_body["task"]["id"] == "task-77"


# ── Failure paths ──────────────────────────────────────────────────────


async def test_runner_failed_result_releases_and_posts_finding(
    tmp_path: Path,
) -> None:
    bus = EventBus()
    plugin_runner = AsyncMock(
        return_value={
            "schema_version": 1,
            "task_id": "task-1",
            "status": "failed",
            "exit_code": 1,
            "error": {"category": "agent", "message": "plugin gave up"},
        }
    )
    runner, lithos = _make_runner(
        bus=bus, work_dir=tmp_path, plugin_runner=plugin_runner
    )

    await bus.publish(_evt())
    await _run_for(runner)

    lithos.task_complete.assert_not_called()
    lithos.task_release.assert_awaited_once()
    lithos.finding_post.assert_awaited_once()
    summary = lithos.finding_post.await_args.kwargs["summary"]
    assert summary.startswith("[BlockerFailed]")
    assert "story-implement" in summary
    assert "plugin gave up" in summary


async def test_runner_plugin_contract_violation_releases_and_posts(
    tmp_path: Path,
) -> None:
    bus = EventBus()
    plugin_runner = AsyncMock(side_effect=PluginContractError("malformed result.json"))
    runner, lithos = _make_runner(
        bus=bus, work_dir=tmp_path, plugin_runner=plugin_runner
    )

    await bus.publish(_evt())
    await _run_for(runner)

    lithos.task_complete.assert_not_called()
    lithos.task_release.assert_awaited_once()
    lithos.finding_post.assert_awaited_once()
    assert "[BlockerFailed]" in lithos.finding_post.await_args.kwargs["summary"]


async def test_runner_plugin_timeout_releases_and_posts(
    tmp_path: Path,
) -> None:
    bus = EventBus()
    plugin_runner = AsyncMock(side_effect=TimeoutError("ran too long"))
    runner, lithos = _make_runner(
        bus=bus, work_dir=tmp_path, plugin_runner=plugin_runner
    )

    await bus.publish(_evt())
    await _run_for(runner)

    lithos.task_release.assert_awaited_once()
    lithos.finding_post.assert_awaited_once()


async def test_runner_interrupted_result_releases_without_finding(
    tmp_path: Path,
) -> None:
    """Status=interrupted means the plugin caught a shutdown signal — release
    the claim so a future run can pick the task up again, but no [BlockerFailed]
    finding (it wasn't an error, the operator stopped us).
    """
    bus = EventBus()
    plugin_runner = AsyncMock(
        return_value={
            "schema_version": 1,
            "task_id": "task-1",
            "status": "interrupted",
            "exit_code": 30,
        }
    )
    runner, lithos = _make_runner(
        bus=bus, work_dir=tmp_path, plugin_runner=plugin_runner
    )

    await bus.publish(_evt())
    await _run_for(runner)

    lithos.task_release.assert_awaited_once()
    lithos.finding_post.assert_not_called()
    lithos.task_complete.assert_not_called()


# ── Resilience ─────────────────────────────────────────────────────────


async def test_runner_recovers_from_unexpected_exception_in_handler(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A bug in handle() must not stop the consumer loop."""
    bus = EventBus()
    plugin_runner = AsyncMock(
        side_effect=[
            RuntimeError("handler boom"),
            {
                "schema_version": 1,
                "task_id": "task-2",
                "status": "succeeded",
                "exit_code": 0,
            },
        ]
    )
    runner, lithos = _make_runner(
        bus=bus, work_dir=tmp_path, plugin_runner=plugin_runner
    )

    with caplog.at_level(logging.ERROR):
        await bus.publish(_evt(payload=_payload("task-1")))
        await asyncio.sleep(0.05)
        await bus.publish(_evt(payload=_payload("task-2")))
        await _run_for(runner, seconds=0.1)

    # Second event still got handled.
    assert lithos.task_complete.await_count == 1
    assert lithos.task_complete.await_args.kwargs["task_id"] == "task-2"


async def test_runner_subscribes_only_to_created_and_updated(
    tmp_path: Path,
) -> None:
    """Slice 0 contract: don't react to claimed/released/completed/cancelled."""
    bus = EventBus()
    runner, lithos = _make_runner(bus=bus, work_dir=tmp_path)

    for type_ in (
        "lithos.task.claimed",
        "lithos.task.released",
        "lithos.task.completed",
        "lithos.task.cancelled",
    ):
        await bus.publish(_evt(type_=type_))
    await _run_for(runner)
    lithos.task_claim.assert_not_called()
