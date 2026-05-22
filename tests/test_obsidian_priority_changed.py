"""Tests for ``lithos_loom.subscriptions._obsidian_priority_changed``
(Slice 2 US21).

The handler is stateless; tests just call ``handle(event, ctx)``
directly with synthetic events and assert on a mocked
``ctx.lithos`` (``AsyncMock``).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest

from lithos_loom.bus import Event
from lithos_loom.subscriptions import SubscriptionContext
from lithos_loom.subscriptions._obsidian_priority_changed import (
    _PRIORITY_CHANGE_PREFIX,
    handle,
)

# ── Helpers ────────────────────────────────────────────────────────────


def _ctx(
    lithos: Any | None = None,
    agent_id: str = "lithos-orchestrator-test",
) -> SubscriptionContext:
    return SubscriptionContext(
        lithos=lithos if lithos is not None else AsyncMock(),
        logger=logging.getLogger("test.obsidian_priority_changed"),
        agent_id=agent_id,
    )


def _event(
    *,
    task_id: str = "abc",
    prior: str | None = "medium",
    new: str | None = "high",
) -> Event:
    return Event(
        type="obsidian.task.priority_changed",
        timestamp=datetime.now(UTC),
        payload={"task_id": task_id, "prior": prior, "new": new},
    )


# ── Happy path ─────────────────────────────────────────────────────────


async def test_change_to_high_posts_priority_change_finding() -> None:
    """``medium → high`` posts a finding with the configured agent id,
    a summary starting with the stable ``[PriorityChangeRequested]``
    prefix, and both enum values rendered in the message body."""
    lithos = AsyncMock()
    ctx = _ctx(lithos=lithos, agent_id="lithos-orchestrator-samsara")

    await handle(_event(task_id="t1", prior="medium", new="high"), ctx)

    lithos.finding_post.assert_awaited_once()
    call = lithos.finding_post.await_args.kwargs
    assert call["task_id"] == "t1"
    assert call["agent"] == "lithos-orchestrator-samsara"
    assert call["summary"].startswith(_PRIORITY_CHANGE_PREFIX)
    assert "medium" in call["summary"]
    assert "high" in call["summary"]


async def test_change_to_none_renders_none_in_summary() -> None:
    """User deleting the priority emoji: ``high → None`` → summary
    contains the literal string ``none`` for the new value so a
    grep on the finding makes the deletion visible."""
    lithos = AsyncMock()
    ctx = _ctx(lithos=lithos)

    await handle(_event(prior="high", new=None), ctx)

    summary = lithos.finding_post.await_args.kwargs["summary"]
    assert "high → none" in summary, summary


async def test_change_from_none_renders_none_in_summary() -> None:
    """Inverse: user adding an emoji where none existed:
    ``None → low``."""
    lithos = AsyncMock()
    ctx = _ctx(lithos=lithos)

    await handle(_event(prior=None, new="low"), ctx)

    summary = lithos.finding_post.await_args.kwargs["summary"]
    assert "none → low" in summary, summary


async def test_handler_uses_ctx_agent_id_not_hardcoded() -> None:
    """The agent passed to ``finding_post`` comes from ``ctx.agent_id``,
    not a hardcoded string — different hosts (samsara, mac-mini, test)
    must each pass their own identity through unchanged."""
    lithos = AsyncMock()
    ctx = _ctx(lithos=lithos, agent_id="lithos-orchestrator-mac-mini")

    await handle(_event(), ctx)

    assert (
        lithos.finding_post.await_args.kwargs["agent"] == "lithos-orchestrator-mac-mini"
    )


async def test_handler_logs_at_info(caplog: pytest.LogCaptureFixture) -> None:
    """The handler emits an INFO log with the prefix and task id so
    operators have a grep trail mirroring the other status-transition
    handlers."""
    ctx = _ctx()
    with caplog.at_level(logging.INFO, logger="test.obsidian_priority_changed"):
        await handle(_event(task_id="lt7", prior="low", new="medium"), ctx)

    info_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert any("[PriorityChangeRequested]" in m and "lt7" in m for m in info_msgs), (
        info_msgs
    )


def test_priority_change_prefix_is_stable() -> None:
    """Pin the exact prefix string — lithos-lens and operators grep
    for this. Reword only with a coordinated change downstream."""
    assert _PRIORITY_CHANGE_PREFIX == "[PriorityChangeRequested]"


# ── Robustness ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "bad_payload",
    [
        {},  # all keys missing
        {"task_id": "x"},  # missing prior + new
        {"prior": "high", "new": "low"},  # missing task_id
        {"task_id": "x", "prior": "high"},  # missing new
        {"task_id": "x", "new": "low"},  # missing prior
    ],
)
async def test_malformed_payload_warns_and_returns(
    bad_payload: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    """Missing payload keys → handler logs a warning, makes no Lithos
    calls, doesn't raise."""
    lithos = AsyncMock()
    ctx = _ctx(lithos=lithos)
    event = Event(
        type="obsidian.task.priority_changed",
        timestamp=datetime.now(UTC),
        payload=bad_payload,
    )

    with caplog.at_level(logging.WARNING, logger="test.obsidian_priority_changed"):
        await handle(event, ctx)  # must not raise

    lithos.finding_post.assert_not_awaited()
    warn_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("malformed payload" in m for m in warn_msgs), warn_msgs


async def test_lithos_error_propagates() -> None:
    """A ``LithosClientError`` (or any exception) from ``finding_post``
    must bubble up so the :class:`SubscriptionRunner` retry-with-backoff
    + on_persistent_failure=friction backstop can take over."""
    lithos = AsyncMock()
    lithos.finding_post.side_effect = RuntimeError("simulated lithos error")
    ctx = _ctx(lithos=lithos)

    with pytest.raises(RuntimeError, match="simulated lithos error"):
        await handle(_event(), ctx)
