"""Tests for ``lithos_loom.subscriptions`` (Slice 0 US4).

The subscription registry sits between the bus and the user's TOML config:
each ``[[subscriptions]]`` stanza becomes a ``SubscriptionConfig``;
``build_runners`` resolves the action name to a registered handler,
compiles any ``where`` predicate with restricted globals, registers the
subscription on the bus, and returns a ``SubscriptionRunner`` that
consumes events with retry-and-friction semantics.

No real handlers ship in Slice 0 — only the bundled ``_noop`` handler used
by tests and the entry-point discovery test. Real handlers arrive in
Slice 1 (``obsidian-projection`` etc.).
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import UTC, datetime
from textwrap import dedent
from types import MappingProxyType
from typing import Any
from unittest.mock import AsyncMock

import pytest

from lithos_loom.bus import Event, EventBus
from lithos_loom.config import (
    RetryPolicy,
    SubscriptionConfig,
    load_config,
)
from lithos_loom.errors import ConfigError, LithosLoomError
from lithos_loom.subscriptions import (
    SUBSCRIPTION_ACTIONS,
    SubscriptionContext,
    SubscriptionRunner,
    build_runners,
)
from lithos_loom.subscriptions._noop import handle as noop_handle

# ── Fixtures + helpers ─────────────────────────────────────────────────


def _evt(
    type_: str = "lithos.task.created",
    payload: dict[str, Any] | None = None,
) -> Event:
    return Event(
        type=type_,
        timestamp=datetime.now(UTC),
        payload=MappingProxyType(payload or {"id": "task-1"}),
    )


def _ctx(agent_id: str = "lithos-orchestrator-test") -> SubscriptionContext:
    import logging

    return SubscriptionContext(
        lithos=AsyncMock(),
        logger=logging.getLogger("test"),
        agent_id=agent_id,
    )


def _spec(
    *,
    name: str = "noop",
    on: tuple[str, ...] = ("lithos.task.created",),
    action: str = "noop",
    match: Mapping[str, Any] | None = None,
    where: str | None = None,
    retry_attempts: int = 1,
    on_persistent_failure: str = "friction",
) -> SubscriptionConfig:
    return SubscriptionConfig(
        name=name,
        event_types=on,
        match=match,
        where=where,
        action=action,
        retry=RetryPolicy(attempts=retry_attempts, initial_delay_seconds=0.001),
        on_persistent_failure=on_persistent_failure,  # type: ignore[arg-type]
    )


# ── Config-side: TOML parsing ──────────────────────────────────────────


def test_subscription_block_parses_from_toml(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        dedent(
            """
            [orchestrator]
            agent_id = "lithos-orchestrator-test"
            lithos_url = "http://localhost:8765"

            [[subscriptions]]
            name = "obsidian-projection"
            on = ["lithos.task.created", "lithos.task.updated"]
            action = "noop"
            match.tags = ["trigger:story-implement"]
            where = "task.get('priority') == 'high'"
            on_persistent_failure = "friction"

            [subscriptions.retry]
            attempts = 5
            backoff = "exponential"
            initial_delay_seconds = 0.5
            max_delay_seconds = 30.0
            """
        )
    )
    monkeypatch.setenv("LITHOS_LOOM_CONFIG", str(cfg_path))
    cfg = load_config()
    assert len(cfg.subscriptions) == 1
    sub = cfg.subscriptions[0]
    assert sub.name == "obsidian-projection"
    assert sub.event_types == ("lithos.task.created", "lithos.task.updated")
    assert sub.action == "noop"
    assert sub.match == {"tags": ["trigger:story-implement"]}
    assert sub.where == "task.get('priority') == 'high'"
    assert sub.retry.attempts == 5
    assert sub.retry.backoff == "exponential"
    assert sub.on_persistent_failure == "friction"


def test_subscription_block_accepts_single_string_for_on(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Convenience: ``on = "lithos.task.created"`` → tuple of one."""
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        dedent(
            """
            [orchestrator]
            agent_id = "x"
            lithos_url = "http://x:1"

            [[subscriptions]]
            name = "n"
            on = "lithos.task.created"
            action = "noop"
            """
        )
    )
    monkeypatch.setenv("LITHOS_LOOM_CONFIG", str(cfg_path))
    cfg = load_config()
    assert cfg.subscriptions[0].event_types == ("lithos.task.created",)


def test_subscription_block_defaults_retry_and_failure_mode(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        dedent(
            """
            [orchestrator]
            agent_id = "x"
            lithos_url = "http://x:1"

            [[subscriptions]]
            name = "n"
            on = "lithos.task.created"
            action = "noop"
            """
        )
    )
    monkeypatch.setenv("LITHOS_LOOM_CONFIG", str(cfg_path))
    cfg = load_config()
    sub = cfg.subscriptions[0]
    assert sub.retry.attempts == 5
    assert sub.retry.backoff == "exponential"
    assert sub.on_persistent_failure == "friction"


def test_subscription_block_rejects_unknown_failure_mode(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        dedent(
            """
            [orchestrator]
            agent_id = "x"
            lithos_url = "http://x:1"

            [[subscriptions]]
            name = "n"
            on = "lithos.task.created"
            action = "noop"
            on_persistent_failure = "explode"
            """
        )
    )
    monkeypatch.setenv("LITHOS_LOOM_CONFIG", str(cfg_path))
    with pytest.raises(ConfigError, match="on_persistent_failure"):
        load_config()


def _retry_config_with(retry_block: str) -> str:
    """Build a minimal config TOML with the supplied [subscriptions.retry] body."""
    return dedent(
        f"""
        [orchestrator]
        agent_id = "x"
        lithos_url = "http://x:1"

        [[subscriptions]]
        name = "n"
        on = "lithos.task.created"
        action = "noop"
        [subscriptions.retry]
        {retry_block}
        """
    )


def test_subscription_retry_rejects_negative_initial_delay(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(_retry_config_with("initial_delay_seconds = -1"))
    monkeypatch.setenv("LITHOS_LOOM_CONFIG", str(cfg_path))
    with pytest.raises(ConfigError, match="initial_delay_seconds must be >= 0"):
        load_config()


def test_subscription_retry_rejects_negative_max_delay(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        _retry_config_with("initial_delay_seconds = 0\nmax_delay_seconds = -2")
    )
    monkeypatch.setenv("LITHOS_LOOM_CONFIG", str(cfg_path))
    with pytest.raises(ConfigError, match="max_delay_seconds must be >= 0"):
        load_config()


def test_subscription_retry_rejects_max_less_than_initial(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        _retry_config_with("initial_delay_seconds = 5\nmax_delay_seconds = 1")
    )
    monkeypatch.setenv("LITHOS_LOOM_CONFIG", str(cfg_path))
    with pytest.raises(ConfigError, match="must be >= initial_delay_seconds"):
        load_config()


def test_subscription_retry_rejects_attempts_zero(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(_retry_config_with("attempts = 0"))
    monkeypatch.setenv("LITHOS_LOOM_CONFIG", str(cfg_path))
    with pytest.raises(ConfigError, match="attempts must be >= 1"):
        load_config()


def test_subscription_block_rejects_unknown_backoff(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        dedent(
            """
            [orchestrator]
            agent_id = "x"
            lithos_url = "http://x:1"

            [[subscriptions]]
            name = "n"
            on = "lithos.task.created"
            action = "noop"
            [subscriptions.retry]
            backoff = "fibonacci"
            """
        )
    )
    monkeypatch.setenv("LITHOS_LOOM_CONFIG", str(cfg_path))
    with pytest.raises(ConfigError, match="backoff"):
        load_config()


# ── Registry: build_runners ────────────────────────────────────────────


def test_build_runners_resolves_handler_and_returns_runner() -> None:
    bus = EventBus()
    runners = build_runners(
        bus=bus,
        specs=(_spec(),),
        handlers={"noop": noop_handle},
        ctx=_ctx(),
    )
    assert len(runners) == 1
    assert isinstance(runners[0], SubscriptionRunner)


def test_build_runners_rejects_unknown_handler_action() -> None:
    bus = EventBus()
    with pytest.raises(LithosLoomError, match="unknown handler"):
        build_runners(
            bus=bus,
            specs=(_spec(action="missing-handler"),),
            handlers={"noop": noop_handle},
            ctx=_ctx(),
        )


def test_build_runners_compiles_where_expression() -> None:
    bus = EventBus()
    runners = build_runners(
        bus=bus,
        specs=(_spec(where="task.get('priority') == 'high'"),),
        handlers={"noop": noop_handle},
        ctx=_ctx(),
    )
    assert len(runners) == 1


def test_build_runners_rejects_invalid_where_expression() -> None:
    bus = EventBus()
    with pytest.raises(LithosLoomError, match="where"):
        build_runners(
            bus=bus,
            specs=(_spec(where="this is not python (("),),
            handlers={"noop": noop_handle},
            ctx=_ctx(),
        )


# ── SubscriptionRunner behaviour ───────────────────────────────────────


async def test_runner_invokes_handler_for_matching_event() -> None:
    bus = EventBus()
    seen: list[Event] = []

    async def capture(event: Event, ctx: SubscriptionContext) -> None:
        seen.append(event)

    [runner] = build_runners(
        bus=bus, specs=(_spec(),), handlers={"noop": capture}, ctx=_ctx()
    )
    task = asyncio.create_task(runner.run())

    event = _evt()
    await bus.publish(event)
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert seen == [event]


async def test_runner_skips_unmatched_events() -> None:
    bus = EventBus()
    seen: list[Event] = []

    async def capture(event: Event, ctx: SubscriptionContext) -> None:
        seen.append(event)

    [runner] = build_runners(
        bus=bus,
        specs=(_spec(on=("lithos.task.created",)),),
        handlers={"noop": capture},
        ctx=_ctx(),
    )
    task = asyncio.create_task(runner.run())

    await bus.publish(_evt(type_="lithos.task.cancelled"))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert seen == []


async def test_runner_retries_handler_until_success() -> None:
    bus = EventBus()
    attempts: list[int] = []

    async def flaky(event: Event, ctx: SubscriptionContext) -> None:
        attempts.append(len(attempts) + 1)
        if len(attempts) < 3:
            raise RuntimeError("transient")

    [runner] = build_runners(
        bus=bus,
        specs=(_spec(retry_attempts=5),),
        handlers={"noop": flaky},
        ctx=_ctx(),
    )
    task = asyncio.create_task(runner.run())

    await bus.publish(_evt())
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert attempts == [1, 2, 3]


async def test_runner_posts_friction_finding_after_persistent_failure() -> None:
    bus = EventBus()
    ctx = _ctx(agent_id="lithos-orchestrator-samsara")
    finding_post = ctx.lithos.finding_post  # type: ignore[attr-defined]

    async def always_fails(event: Event, ctx: SubscriptionContext) -> None:
        raise RuntimeError("nope")

    [runner] = build_runners(
        bus=bus,
        specs=(_spec(name="bad-sub", retry_attempts=2),),
        handlers={"noop": always_fails},
        ctx=ctx,
    )
    task = asyncio.create_task(runner.run())

    await bus.publish(_evt(payload={"id": "task-77"}))
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    finding_post.assert_awaited_once()
    kwargs = finding_post.await_args.kwargs
    assert kwargs["task_id"] == "task-77"
    # The Lithos `lithos_finding_post` tool requires `agent`; the runner
    # must thread the orchestrator agent_id through from the context. Pin
    # this so we don't regress against the real Lithos contract.
    assert kwargs["agent"] == "lithos-orchestrator-samsara"
    assert kwargs["summary"].startswith("[Friction]")
    assert "bad-sub" in kwargs["summary"]


async def test_runner_logs_friction_when_event_has_no_task_id(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Non-task event → no finding_post call (would be invalid per the Lithos
    spec); log a [Friction]-prefixed warning instead so the signal isn't lost.
    """
    bus = EventBus()
    ctx = _ctx()
    finding_post = ctx.lithos.finding_post  # type: ignore[attr-defined]

    async def always_fails(event: Event, ctx: SubscriptionContext) -> None:
        raise RuntimeError("nope")

    [runner] = build_runners(
        bus=bus,
        specs=(
            _spec(
                name="non-task-sub",
                on=("obsidian.note.modified",),
                retry_attempts=2,
            ),
        ),
        handlers={"noop": always_fails},
        ctx=ctx,
    )
    task = asyncio.create_task(runner.run())

    with caplog.at_level("WARNING", logger="test"):
        # Payload with no "id" key — typical for non-task events.
        await bus.publish(
            _evt(type_="obsidian.note.modified", payload={"path": "x.md"})
        )
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    finding_post.assert_not_called()
    matching = [r for r in caplog.records if "[Friction]" in r.message]
    assert matching, f"expected a [Friction] warning, got: {caplog.records}"
    assert "no task_id" in matching[0].message


async def test_runner_does_not_post_finding_in_ignore_mode() -> None:
    bus = EventBus()
    ctx = _ctx()
    finding_post = ctx.lithos.finding_post  # type: ignore[attr-defined]

    async def always_fails(event: Event, ctx: SubscriptionContext) -> None:
        raise RuntimeError("nope")

    [runner] = build_runners(
        bus=bus,
        specs=(_spec(retry_attempts=2, on_persistent_failure="ignore"),),
        handlers={"noop": always_fails},
        ctx=ctx,
    )
    task = asyncio.create_task(runner.run())
    await bus.publish(_evt())
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    finding_post.assert_not_called()


# ── Action catalog ─────────────────────────────────────────────────────


def test_subscription_actions_catalog_includes_noop_and_real_actions() -> None:
    """The catalog is the single source of truth for known action names.

    ``noop`` is the bundled test/smoke placeholder; the real actions are
    hand-wired in their hosting child. A typo'd config action is rejected
    by ``build_runners`` because it's absent from the map derived here."""
    assert "noop" in SUBSCRIPTION_ACTIONS
    assert "obsidian-projection" in SUBSCRIPTION_ACTIONS
    assert "task-archive" in SUBSCRIPTION_ACTIONS
    # A made-up action is not in the catalog, so the dry-run + child wiring
    # both reject it rather than silently no-op.
    assert "obsidian-projction" not in SUBSCRIPTION_ACTIONS
