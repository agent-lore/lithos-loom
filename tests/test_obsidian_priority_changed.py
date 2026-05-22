"""Tests for ``lithos_loom.subscriptions._obsidian_priority_changed``
(Slice 2 US21).

The handler is stateless; tests just call ``handle(event, ctx)``
directly with synthetic events and assert on a mocked
``ctx.lithos`` (``AsyncMock``). The handler calls
``ctx.lithos.task_update(task_id=..., agent=..., metadata={"priority": new_str})``
to push the change to Lithos via the per-key merge semantics
introduced in Lithos #290.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest

from lithos_loom.bus import Event
from lithos_loom.subscriptions import SubscriptionContext
from lithos_loom.subscriptions._obsidian_priority_changed import handle

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


async def test_change_to_high_calls_task_update_with_metadata_priority() -> None:
    """``medium → high`` calls ``task_update(metadata={"priority": "high"})``
    with the configured agent_id. ``finding_post`` is NOT called
    (post-#290 the handler uses the real Lithos surface, not the
    interim finding workaround)."""
    lithos = AsyncMock()
    ctx = _ctx(lithos=lithos, agent_id="lithos-orchestrator-samsara")

    await handle(_event(task_id="t1", prior="medium", new="high"), ctx)

    lithos.task_update.assert_awaited_once_with(
        task_id="t1",
        agent="lithos-orchestrator-samsara",
        metadata={"priority": "high"},
    )
    lithos.finding_post.assert_not_awaited()


async def test_change_to_none_sends_metadata_priority_null_to_delete_key() -> None:
    """User deleting the emoji: ``high → None`` → the handler sends
    ``metadata={"priority": None}`` which Lithos interprets (per
    #290's additive-merge semantics) as "delete the priority key".
    Other metadata keys (``depends_on`` etc) are preserved by
    Lithos because they're not mentioned in the patch."""
    lithos = AsyncMock()
    ctx = _ctx(lithos=lithos)

    await handle(_event(prior="high", new=None), ctx)

    lithos.task_update.assert_awaited_once_with(
        task_id="abc",
        agent="lithos-orchestrator-test",
        metadata={"priority": None},
    )


async def test_change_from_none_sets_priority_for_the_first_time() -> None:
    """Inverse: user adding an emoji where none existed:
    ``None → low``. The handler sends
    ``metadata={"priority": "low"}``; Lithos creates the key."""
    lithos = AsyncMock()
    ctx = _ctx(lithos=lithos)

    await handle(_event(prior=None, new="low"), ctx)

    lithos.task_update.assert_awaited_once_with(
        task_id="abc",
        agent="lithos-orchestrator-test",
        metadata={"priority": "low"},
    )


@pytest.mark.parametrize("enum_value", ["highest", "high", "medium", "low", "lowest"])
async def test_all_five_priority_enum_values_round_trip(enum_value: str) -> None:
    """Every D18 enum value forwards verbatim into the metadata patch."""
    lithos = AsyncMock()
    ctx = _ctx(lithos=lithos)

    await handle(_event(prior="medium", new=enum_value), ctx)

    args = lithos.task_update.await_args.kwargs
    assert args["metadata"] == {"priority": enum_value}


async def test_handler_uses_ctx_agent_id_not_hardcoded() -> None:
    """The agent passed to ``task_update`` comes from ``ctx.agent_id``,
    not a hardcoded string — different hosts (samsara, mac-mini, test)
    must each pass their own identity through unchanged."""
    lithos = AsyncMock()
    ctx = _ctx(lithos=lithos, agent_id="lithos-orchestrator-mac-mini")

    await handle(_event(), ctx)

    assert (
        lithos.task_update.await_args.kwargs["agent"] == "lithos-orchestrator-mac-mini"
    )


async def test_handler_logs_at_info(caplog: pytest.LogCaptureFixture) -> None:
    """The handler emits an INFO log with the task id and the
    prior → new transition so operators have a grep trail mirroring
    the status-transition handlers."""
    ctx = _ctx()
    with caplog.at_level(logging.INFO, logger="test.obsidian_priority_changed"):
        await handle(_event(task_id="lt7", prior="low", new="medium"), ctx)

    info_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert any(
        "updated task lt7 priority" in m and "low" in m and "medium" in m
        for m in info_msgs
    ), info_msgs


async def test_handler_does_not_touch_other_metadata_keys() -> None:
    """The patch only contains the ``priority`` key — Lithos's
    additive-per-key merge preserves every other metadata key on
    the task. Regression guard against accidentally widening the
    patch and clobbering ``depends_on`` / ``scheduled_for`` /
    ``story_doc_id`` / etc."""
    lithos = AsyncMock()
    ctx = _ctx(lithos=lithos)

    await handle(_event(prior="medium", new="high"), ctx)

    patch = lithos.task_update.await_args.kwargs["metadata"]
    assert list(patch.keys()) == ["priority"]


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

    lithos.task_update.assert_not_awaited()
    warn_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("malformed payload" in m for m in warn_msgs), warn_msgs


async def test_lithos_error_propagates() -> None:
    """A ``LithosClientError`` (or any exception) from ``task_update``
    must bubble up so the :class:`SubscriptionRunner` retry-with-backoff
    + on_persistent_failure=friction backstop can take over."""
    lithos = AsyncMock()
    lithos.task_update.side_effect = RuntimeError("simulated lithos error")
    ctx = _ctx(lithos=lithos)

    with pytest.raises(RuntimeError, match="simulated lithos error"):
        await handle(_event(), ctx)
