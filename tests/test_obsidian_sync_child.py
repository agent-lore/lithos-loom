"""Tests for ``lithos_loom.children.obsidian_sync`` (Slice 1 US7+US8).

These tests drive ``_amain`` directly with a fabricated ``LoomConfig``
so they don't shell out to subprocess. The supervisor-level
end-to-end gating is exercised in ``test_supervisor.py``.

US8 replaced the SIGTERM-park with a real wiring chain (LithosClient
+ LithosEventStream + SubscriptionRunner). We monkeypatch the client
and source so tests stay in-process without a real Lithos to connect
to; the bus is captured so tests can publish events directly and
observe the projection handler's file writes.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from lithos_loom.bus import Event, EventBus
from lithos_loom.children import obsidian_sync as obs_sync_mod
from lithos_loom.children.obsidian_sync import _amain
from lithos_loom.config import (
    LoomConfig,
    ObsidianSyncConfig,
    OrchestratorConfig,
    RetryPolicy,
    SubscriptionConfig,
)

# ── Helpers ────────────────────────────────────────────────────────────


async def _cancel_and_drain(task: asyncio.Task[Any]) -> None:
    """Cancel a helper task and await its completion so it can't leak past
    the test (Copilot review on #16)."""
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def _sigterm_soon(delay: float = 0.1) -> None:
    """Send SIGTERM to this process after a short delay. Used to unblock
    the stop_event inside _amain so the test can complete."""
    await asyncio.sleep(delay)
    os.kill(os.getpid(), signal.SIGTERM)


def _cfg_with_obsidian(
    tmp_path: Path,
    *,
    subscriptions: tuple[SubscriptionConfig, ...] = (),
) -> LoomConfig:
    return LoomConfig(
        orchestrator=OrchestratorConfig(
            agent_id="lithos-orchestrator-test",
            lithos_url="http://localhost:8765",
        ),
        subscriptions=subscriptions,
        obsidian_sync=ObsidianSyncConfig(
            vault_path=tmp_path / "vault",
            tasks_file=Path("_lithos/tasks.md"),
            resolved_ttl_days=7,
            include_blocked=False,
            exclude_tags=("debug:trace",),
        ),
    )


def _cfg_without_obsidian(tmp_path: Path) -> LoomConfig:
    return LoomConfig(
        orchestrator=OrchestratorConfig(
            agent_id="lithos-orchestrator-test",
            lithos_url="http://localhost:8765",
        ),
    )


def _projection_subscription(
    name: str = "obsidian-tasks",
    action: str = "obsidian-projection",
) -> SubscriptionConfig:
    return SubscriptionConfig(
        name=name,
        event_types=(
            "lithos.task.created",
            "lithos.task.updated",
            "lithos.task.completed",
            "lithos.task.cancelled",
        ),
        action=action,
        retry=RetryPolicy(attempts=1, initial_delay_seconds=0.0, max_delay_seconds=0.0),
        on_persistent_failure="ignore",
    )


def _event(
    event_type: str,
    *,
    task_id: str,
    title: str = "test task",
    tags: tuple[str, ...] = (),
    metadata: Mapping[str, Any] | None = None,
) -> Event:
    return Event(
        type=event_type,
        timestamp=datetime.now(UTC),
        payload={
            "id": task_id,
            "title": title,
            "status": "open",
            "tags": list(tags),
            "metadata": dict(metadata or {}),
            "claims": [],
        },
    )


# ── Stubs for LithosClient + LithosEventStream ─────────────────────────


class _StubLithosClient:
    """Async-context-manager stand-in for ``LithosClient``.

    The real client does an MCP/SSE handshake on __aenter__; tests
    can't reach a real Lithos, so we substitute this no-op.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> _StubLithosClient:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def finding_post(self, **kwargs: Any) -> None:
        # In case persistent-failure handling triggers in a test, no-op.
        return None


class _StubSource:
    """Stand-in for ``LithosEventStream`` that exposes its bus and idles."""

    def __init__(
        self, *, client: Any, bus: EventBus, events_url: str, **_: Any
    ) -> None:
        self.bus = bus
        self.events_url = events_url

    async def run(self) -> None:
        await asyncio.sleep(3600)  # park; the source contract is "run forever"


@pytest.fixture
def stub_io(monkeypatch: pytest.MonkeyPatch) -> list[EventBus]:
    """Replace LithosClient + LithosEventStream in the obsidian_sync
    module and return a list that captures the bus each _StubSource was
    constructed with — tests publish to ``captured_buses[-1]``."""
    captured: list[EventBus] = []

    class _CapturingSource(_StubSource):
        def __init__(self, *, client: Any, bus: EventBus, events_url: str, **kw: Any):
            super().__init__(client=client, bus=bus, events_url=events_url, **kw)
            captured.append(bus)

    monkeypatch.setattr(obs_sync_mod, "LithosClient", _StubLithosClient)
    monkeypatch.setattr(obs_sync_mod, "LithosEventStream", _CapturingSource)
    return captured


# ── US7 behaviour (still asserted under US8 wiring) ─────────────────────


async def test_obsidian_sync_main_exits_zero_on_stop_event(
    tmp_path: Path, stub_io: list[EventBus]
) -> None:
    """``_amain`` parks until SIGTERM regardless of subscription config."""
    cfg = _cfg_with_obsidian(tmp_path)
    sender = asyncio.create_task(_sigterm_soon())
    try:
        rc = await asyncio.wait_for(_amain(cfg), timeout=2.0)
    finally:
        await _cancel_and_drain(sender)
    assert rc == 0


async def test_obsidian_sync_main_exits_one_when_config_missing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Defensive guard: ``_amain`` returns 1 when ``obsidian_sync`` is None,
    rather than parking on a stop event under an undefined config.

    No stub_io needed — _amain returns before reaching the LithosClient.
    """
    cfg = _cfg_without_obsidian(tmp_path)
    source_logger = "lithos_loom.children.obsidian_sync"
    with caplog.at_level(logging.ERROR, logger=source_logger):
        rc = await _amain(cfg)
    assert rc == 1
    error_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert any("obsidian-sync spawned without" in m for m in error_msgs), error_msgs


async def test_obsidian_sync_logs_config_on_startup(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, stub_io: list[EventBus]
) -> None:
    """The startup INFO log names vault / tasks_file / resolved_ttl_days /
    include_blocked / exclude_tags so an operator can grep-confirm."""
    cfg = _cfg_with_obsidian(tmp_path)
    source_logger = "lithos_loom.children.obsidian_sync"

    sender = asyncio.create_task(_sigterm_soon())
    try:
        with caplog.at_level(logging.INFO, logger=source_logger):
            await asyncio.wait_for(_amain(cfg), timeout=2.0)
    finally:
        await _cancel_and_drain(sender)

    info_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    started = next((m for m in info_msgs if "obsidian-sync child started" in m), None)
    assert started is not None, f"no startup log; got {info_msgs}"
    assert str(cfg.obsidian_sync.vault_path) in started  # type: ignore[union-attr]
    assert "_lithos/tasks.md" in started
    assert "resolved_ttl_days=7" in started
    assert "include_blocked=False" in started
    assert "debug:trace" in started


# ── US8 wiring ─────────────────────────────────────────────────────────


async def test_obsidian_sync_child_wires_projection_subscription(
    tmp_path: Path, stub_io: list[EventBus]
) -> None:
    """End-to-end through _amain: configure an obsidian-projection
    subscription, publish a task.created event onto the captured bus,
    confirm the projection file gets written."""
    cfg = _cfg_with_obsidian(
        tmp_path,
        subscriptions=(_projection_subscription(),),
    )

    async def _drive() -> None:
        # Let _amain reach the await stop_event.wait() — by then the
        # subscription is wired on the captured bus.
        await asyncio.sleep(0.1)
        bus = stub_io[-1]
        await bus.publish(
            _event("lithos.task.created", task_id="abc", title="Review PR")
        )
        # Give the SubscriptionRunner a beat to drain and the handler
        # to write the file.
        await asyncio.sleep(0.1)
        os.kill(os.getpid(), signal.SIGTERM)

    driver = asyncio.create_task(_drive())
    try:
        rc = await asyncio.wait_for(_amain(cfg), timeout=3.0)
    finally:
        await _cancel_and_drain(driver)
    assert rc == 0

    tasks_file = cfg.obsidian_sync.vault_path / cfg.obsidian_sync.tasks_file  # type: ignore[union-attr]
    assert tasks_file.exists(), "projection file was not written"
    content = tasks_file.read_text()
    assert "- [ ] Review PR 🆔 lithos:abc" in content


async def test_obsidian_sync_child_idles_when_no_obsidian_subscription(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, stub_io: list[EventBus]
) -> None:
    """Config has [obsidian_sync] but no matching subscription — the
    child still parks cleanly (preserves US7 behaviour) and warns that
    projection is unconfigured."""
    cfg = _cfg_with_obsidian(tmp_path)  # no subscriptions
    source_logger = "lithos_loom.children.obsidian_sync"

    sender = asyncio.create_task(_sigterm_soon())
    try:
        with caplog.at_level(logging.WARNING, logger=source_logger):
            rc = await asyncio.wait_for(_amain(cfg), timeout=2.0)
    finally:
        await _cancel_and_drain(sender)
    assert rc == 0

    warn_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("no obsidian-* subscriptions configured" in m for m in warn_msgs), (
        warn_msgs
    )


async def test_obsidian_sync_child_ignores_non_obsidian_subscription_actions(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, stub_io: list[EventBus]
) -> None:
    """Config with `noop` and `obsidian-projection` subscriptions — only
    the obsidian one is wired. The noop one is silently skipped here
    (it's some other child's job; routing comes in a future story)."""
    cfg = _cfg_with_obsidian(
        tmp_path,
        subscriptions=(
            _projection_subscription("obs-tasks", action="obsidian-projection"),
            _projection_subscription("noop-smoke", action="noop"),
        ),
    )
    source_logger = "lithos_loom.children.obsidian_sync"

    sender = asyncio.create_task(_sigterm_soon())
    try:
        with caplog.at_level(logging.INFO, logger=source_logger):
            rc = await asyncio.wait_for(_amain(cfg), timeout=2.0)
    finally:
        await _cancel_and_drain(sender)
    assert rc == 0

    # The wiring log line names exactly the obsidian one — not noop.
    info_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    wiring = next((m for m in info_msgs if "wiring" in m and "subscription" in m), None)
    assert wiring is not None, f"no wiring log; got {info_msgs}"
    assert "obs-tasks" in wiring
    assert "noop-smoke" not in wiring
