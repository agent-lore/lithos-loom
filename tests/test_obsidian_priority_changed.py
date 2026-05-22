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
from lithos_loom.lithos_client import Task
from lithos_loom.subscriptions import SubscriptionContext
from lithos_loom.subscriptions._obsidian_priority_changed import handle

# ── Helpers ────────────────────────────────────────────────────────────


def _default_open_task(task_id: str = "abc") -> Task:
    """A synthetic open task with empty metadata — the default
    response from ``task_get`` so the strict-priority pre-check
    (post-lithos#294) doesn't compare against an AsyncMock attribute
    and leak un-awaited-coroutine warnings."""
    return Task(
        id=task_id,
        title="t",
        status="open",
        tags=(),
        metadata={},
        claims=(),
    )


def _ctx(
    lithos: Any | None = None,
    agent_id: str = "lithos-orchestrator-test",
) -> SubscriptionContext:
    """Build a SubscriptionContext with ``task_get`` pre-wired to a
    synthetic open task by default. Tests that need a specific
    metadata.priority value override ``lithos.task_get.return_value``
    explicitly before calling _ctx (or after — _ctx only fills in the
    default when nothing's configured)."""
    if lithos is None:
        lithos = AsyncMock()
    task_get_mock = lithos.task_get
    if (
        task_get_mock.return_value is None
        or isinstance(task_get_mock.return_value, AsyncMock)
    ) and task_get_mock.side_effect is None:
        task_get_mock.return_value = _default_open_task()
    return SubscriptionContext(
        lithos=lithos,
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
    Lithos because they're not mentioned in the patch.

    Wires ``task_get`` so the strict pre-check (post-lithos#294)
    sees the task with ``priority=high`` in metadata — required so
    the pre-check observes a genuine mismatch (``"high" != None``)
    and proceeds to the update."""
    lithos = AsyncMock()
    lithos.task_get.return_value = Task(
        id="abc",
        title="t",
        status="open",
        tags=(),
        metadata={"priority": "high"},
        claims=(),
    )
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
    """Every D18 enum value forwards verbatim into the metadata patch.

    Uses ``prior=None`` so the US22 ``prior == new`` short-circuit
    can't fire for any of the parametrized values (every enum_value
    is ``!= None``)."""
    lithos = AsyncMock()
    ctx = _ctx(lithos=lithos)

    await handle(_event(prior=None, new=enum_value), ctx)

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


# ── US22: payload-only idempotency short-circuit ───────────────────────


async def test_skips_when_prior_equals_new(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``prior == new`` (both ``"high"``) → ``task_update`` NOT
    called; INFO log mentions the idempotent skip. The fs-watcher
    won't naturally emit prior==new in steady state (layer-3 diff
    suppresses it) but a third-party producer or restart-replay
    degenerate case might."""
    lithos = AsyncMock()
    ctx = _ctx(lithos=lithos)

    with caplog.at_level(logging.INFO, logger="test.obsidian_priority_changed"):
        await handle(_event(task_id="abc", prior="high", new="high"), ctx)

    lithos.task_update.assert_not_awaited()
    info_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert any(
        "payload prior==new (high)" in m
        and "skipping idempotent update for task abc" in m
        for m in info_msgs
    ), info_msgs


async def test_skips_when_both_prior_and_new_are_none(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``None → None`` degenerate case: the watcher would never emit
    this (no observable change), but the short-circuit must still
    catch it if a third party publishes one."""
    lithos = AsyncMock()
    ctx = _ctx(lithos=lithos)

    with caplog.at_level(logging.INFO, logger="test.obsidian_priority_changed"):
        await handle(_event(task_id="abc", prior=None, new=None), ctx)

    lithos.task_update.assert_not_awaited()
    info_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert any(
        "payload prior==new (None)" in m
        and "skipping idempotent update for task abc" in m
        for m in info_msgs
    ), info_msgs


async def test_does_not_skip_when_prior_none_new_set() -> None:
    """``None → "high"`` (user added an emoji where none existed) is
    a genuine change; the short-circuit must NOT trigger."""
    lithos = AsyncMock()
    ctx = _ctx(lithos=lithos)

    await handle(_event(task_id="abc", prior=None, new="high"), ctx)

    lithos.task_update.assert_awaited_once_with(
        task_id="abc",
        agent="lithos-orchestrator-test",
        metadata={"priority": "high"},
    )


async def test_does_not_skip_when_prior_set_new_none() -> None:
    """``"high" → None`` (user deleted the emoji) is a genuine
    change — the delete-semantics patch must still go through.

    Lithos has ``priority=high`` so the strict pre-check
    (post-lithos#294) sees a mismatch (``"high" != None``) and
    proceeds. The companion test
    :func:`test_strict_check_skips_when_lithos_has_no_priority_and_new_is_none`
    covers the symmetric case where Lithos already has no priority
    and the handler correctly skips."""
    lithos = AsyncMock()
    lithos.task_get.return_value = Task(
        id="abc",
        title="t",
        status="open",
        tags=(),
        metadata={"priority": "high"},
        claims=(),
    )
    ctx = _ctx(lithos=lithos)

    await handle(_event(task_id="abc", prior="high", new=None), ctx)

    lithos.task_update.assert_awaited_once_with(
        task_id="abc",
        agent="lithos-orchestrator-test",
        metadata={"priority": None},
    )


# ── US22: Lithos-side strict idempotency check (lithos#294) ───────────


def _task(
    task_id: str = "abc", *, priority: str | None = None, status: str = "open"
) -> Task:
    """Construct a Task with the minimum fields needed for the strict
    priority check. Metadata is empty unless ``priority`` is given."""
    metadata: dict[str, Any] = {}
    if priority is not None:
        metadata["priority"] = priority
    return Task(
        id=task_id,
        title="t",
        status=status,
        tags=(),
        metadata=metadata,
        claims=(),
    )


async def test_skips_when_lithos_priority_already_matches_new(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Payload says ``low → high`` (genuine change from watcher view),
    but Lithos already has ``priority=high`` (another agent updated it,
    or sync_state drifted). The strict pre-check skips the redundant
    update."""
    lithos = AsyncMock()
    lithos.task_get.return_value = _task("abc", priority="high")
    ctx = _ctx(lithos=lithos)

    with caplog.at_level(logging.INFO, logger="test.obsidian_priority_changed"):
        await handle(_event(task_id="abc", prior="low", new="high"), ctx)

    lithos.task_update.assert_not_awaited()
    info_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert any(
        "task abc already at priority high" in m and "skipping idempotent update" in m
        for m in info_msgs
    ), info_msgs


async def test_proceeds_when_lithos_priority_differs_from_new() -> None:
    """Happy path: Lithos has ``medium``, payload says ``low → high``.
    Pre-check sees a real mismatch, ``task_update`` fires. Guards
    against an over-strict predicate that would block legitimate
    updates."""
    lithos = AsyncMock()
    lithos.task_get.return_value = _task("abc", priority="medium")
    ctx = _ctx(lithos=lithos)

    await handle(_event(task_id="abc", prior="low", new="high"), ctx)

    lithos.task_update.assert_awaited_once_with(
        task_id="abc",
        agent="lithos-orchestrator-test",
        metadata={"priority": "high"},
    )


async def test_skips_when_task_not_found_in_lithos(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``task_get`` returns ``None`` (task deleted upstream) → no
    ``task_update``; INFO log mentions "not found".

    Sets ``return_value = None`` AFTER calling ``_ctx`` so the
    helper's default-task wiring doesn't overwrite the explicit
    ``None``."""
    lithos = AsyncMock()
    ctx = _ctx(lithos=lithos)
    lithos.task_get.return_value = None

    with caplog.at_level(logging.INFO, logger="test.obsidian_priority_changed"):
        await handle(_event(task_id="gone1", prior="low", new="high"), ctx)

    lithos.task_update.assert_not_awaited()
    info_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert any(
        "task gone1 not found in Lithos" in m and "skipping" in m for m in info_msgs
    ), info_msgs


async def test_strict_check_skips_when_lithos_has_no_priority_and_new_is_none(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``prior="high", new=None`` (user deleted emoji). Lithos already
    has no ``priority`` key in metadata → ``current_priority`` is None,
    equals ``new_str=None`` → skip. Symmetric to the explicit
    ``priority=value`` match."""
    lithos = AsyncMock()
    # Task with NO priority key in metadata.
    lithos.task_get.return_value = _task("abc")
    ctx = _ctx(lithos=lithos)

    with caplog.at_level(logging.INFO, logger="test.obsidian_priority_changed"):
        await handle(_event(task_id="abc", prior="high", new=None), ctx)

    lithos.task_update.assert_not_awaited()
    info_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert any(
        "task abc already at priority None" in m and "skipping" in m for m in info_msgs
    ), info_msgs


async def test_strict_check_proceeds_when_prior_none_and_lithos_has_no_priority() -> (
    None
):
    """``prior=None, new="high"`` (user added an emoji). Lithos also
    has no priority → ``current_priority=None != "high"=new_str`` →
    proceed. Guards against an over-strict predicate that would block
    the "user adds priority where none existed" case."""
    lithos = AsyncMock()
    lithos.task_get.return_value = _task("abc")
    ctx = _ctx(lithos=lithos)

    await handle(_event(task_id="abc", prior=None, new="high"), ctx)

    lithos.task_update.assert_awaited_once_with(
        task_id="abc",
        agent="lithos-orchestrator-test",
        metadata={"priority": "high"},
    )


async def test_strict_check_pre_check_uses_task_get_not_task_status() -> None:
    """The handler uses ``task_get`` (no claims needed) for the
    strict pre-check, not ``task_status`` (heavier). Regression
    guard against a future change that re-adds task_status."""
    lithos = AsyncMock()
    lithos.task_get.return_value = _task("abc", priority="medium")
    ctx = _ctx(lithos=lithos)

    await handle(_event(task_id="abc", prior="low", new="high"), ctx)

    lithos.task_get.assert_awaited_once_with(task_id="abc")
    lithos.task_status.assert_not_awaited()
