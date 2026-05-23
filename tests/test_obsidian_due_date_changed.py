"""Tests for ``lithos_loom.subscriptions._obsidian_due_date_changed``
(Slice 3 round-trip).

The handler is stateless; tests just call ``handle(event, ctx)``
directly with synthetic events and assert on a mocked
``ctx.lithos`` (``AsyncMock``). Mirrors the structure of
``test_obsidian_priority_changed.py``.
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
from lithos_loom.subscriptions._obsidian_due_date_changed import handle


def _default_open_task(task_id: str = "abc") -> Task:
    """Synthetic open task with empty metadata — the default
    response from ``task_get`` so the strict pre-check doesn't
    compare against an AsyncMock attribute."""
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
        logger=logging.getLogger("test.obsidian_due_date_changed"),
        agent_id=agent_id,
    )


def _event(
    *,
    task_id: str = "abc",
    prior: str | None = "2026-05-20",
    new: str | None = "2026-06-15",
) -> Event:
    return Event(
        type="obsidian.task.due_date_changed",
        timestamp=datetime.now(UTC),
        payload={"task_id": task_id, "prior": prior, "new": new},
    )


# ── Happy path ─────────────────────────────────────────────────────────


async def test_change_calls_task_update_with_scheduled_for() -> None:
    """``"2026-05-20" → "2026-06-15"`` calls
    ``task_update(metadata={"scheduled_for": "2026-06-15"})`` with the
    configured agent."""
    lithos = AsyncMock()
    ctx = _ctx(lithos=lithos, agent_id="lithos-orchestrator-samsara")

    await handle(_event(task_id="t1", prior="2026-05-20", new="2026-06-15"), ctx)

    lithos.task_update.assert_awaited_once_with(
        task_id="t1",
        agent="lithos-orchestrator-samsara",
        metadata={"scheduled_for": "2026-06-15"},
    )


async def test_change_to_none_sends_metadata_null_to_delete_key() -> None:
    """User deleting the 📅 marker (``new=None``) sends
    ``metadata={"scheduled_for": None}`` which Lithos interprets as
    "delete the key" per #290 additive-merge semantics. Other metadata
    keys (priority, project, depends_on, etc.) are preserved."""
    lithos = AsyncMock()
    lithos.task_get.return_value = Task(
        id="abc",
        title="t",
        status="open",
        tags=(),
        metadata={"scheduled_for": "2026-05-20"},
        claims=(),
    )
    ctx = _ctx(lithos=lithos)

    await handle(_event(prior="2026-05-20", new=None), ctx)

    lithos.task_update.assert_awaited_once_with(
        task_id="abc",
        agent="lithos-orchestrator-test",
        metadata={"scheduled_for": None},
    )


async def test_change_from_none_sets_date_for_the_first_time() -> None:
    """Inverse: user added a 📅 marker where none existed.
    ``None → "2026-07-01"`` creates the key."""
    lithos = AsyncMock()
    ctx = _ctx(lithos=lithos)

    await handle(_event(prior=None, new="2026-07-01"), ctx)

    lithos.task_update.assert_awaited_once_with(
        task_id="abc",
        agent="lithos-orchestrator-test",
        metadata={"scheduled_for": "2026-07-01"},
    )


async def test_handler_uses_ctx_agent_id_not_hardcoded() -> None:
    lithos = AsyncMock()
    ctx = _ctx(lithos=lithos, agent_id="lithos-orchestrator-mac-mini")

    await handle(_event(), ctx)

    assert (
        lithos.task_update.await_args.kwargs["agent"] == "lithos-orchestrator-mac-mini"
    )


async def test_handler_logs_at_info(caplog: pytest.LogCaptureFixture) -> None:
    ctx = _ctx()
    with caplog.at_level(logging.INFO, logger="test.obsidian_due_date_changed"):
        await handle(_event(task_id="d1", prior="2026-05-20", new="2026-06-15"), ctx)

    info_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert any(
        "updated task d1 scheduled_for" in m and "2026-05-20" in m and "2026-06-15" in m
        for m in info_msgs
    ), info_msgs


async def test_handler_does_not_touch_other_metadata_keys() -> None:
    """The patch only contains the ``scheduled_for`` key — Lithos's
    additive-per-key merge preserves every other metadata key on the
    task. Regression guard against accidentally widening the patch."""
    lithos = AsyncMock()
    ctx = _ctx(lithos=lithos)

    await handle(_event(prior="2026-05-20", new="2026-06-15"), ctx)

    patch = lithos.task_update.await_args.kwargs["metadata"]
    assert list(patch.keys()) == ["scheduled_for"]


# ── Robustness ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "bad_payload",
    [
        {},
        {"task_id": "x"},
        {"prior": "2026-05-20", "new": "2026-06-15"},
        {"task_id": "x", "prior": "2026-05-20"},
        {"task_id": "x", "new": "2026-06-15"},
    ],
)
async def test_malformed_payload_warns_and_returns(
    bad_payload: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    lithos = AsyncMock()
    ctx = _ctx(lithos=lithos)
    event = Event(
        type="obsidian.task.due_date_changed",
        timestamp=datetime.now(UTC),
        payload=bad_payload,
    )

    with caplog.at_level(logging.WARNING, logger="test.obsidian_due_date_changed"):
        await handle(event, ctx)

    lithos.task_update.assert_not_awaited()
    warn_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("malformed payload" in m for m in warn_msgs), warn_msgs


async def test_lithos_error_propagates() -> None:
    lithos = AsyncMock()
    lithos.task_update.side_effect = RuntimeError("simulated lithos error")
    ctx = _ctx(lithos=lithos)

    with pytest.raises(RuntimeError, match="simulated lithos error"):
        await handle(_event(), ctx)


# ── Idempotency: payload-only short-circuit ───────────────────────────


async def test_skips_when_prior_equals_new(caplog: pytest.LogCaptureFixture) -> None:
    """``prior == new`` (same date) → no Lithos call."""
    lithos = AsyncMock()
    ctx = _ctx(lithos=lithos)

    with caplog.at_level(logging.INFO, logger="test.obsidian_due_date_changed"):
        await handle(_event(task_id="abc", prior="2026-05-20", new="2026-05-20"), ctx)

    lithos.task_update.assert_not_awaited()
    info_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert any(
        "payload prior==new (2026-05-20)" in m
        and "skipping idempotent update for task abc" in m
        for m in info_msgs
    ), info_msgs


async def test_skips_when_both_prior_and_new_are_none() -> None:
    """``None → None`` degenerate case skips before any RPC."""
    lithos = AsyncMock()
    ctx = _ctx(lithos=lithos)

    await handle(_event(prior=None, new=None), ctx)

    lithos.task_update.assert_not_awaited()


# ── Idempotency: Lithos-side strict pre-check ─────────────────────────


async def test_skips_when_lithos_scheduled_for_already_matches_new(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Payload says ``"2026-05-20" → "2026-06-15"`` (genuine change
    from watcher view), but Lithos already has ``scheduled_for =
    "2026-06-15"`` (another agent updated it, or sync_state drifted).
    The strict pre-check skips the redundant update."""
    lithos = AsyncMock()
    lithos.task_get.return_value = Task(
        id="abc",
        title="t",
        status="open",
        tags=(),
        metadata={"scheduled_for": "2026-06-15"},
        claims=(),
    )
    ctx = _ctx(lithos=lithos)

    with caplog.at_level(logging.INFO, logger="test.obsidian_due_date_changed"):
        await handle(_event(task_id="abc", prior="2026-05-20", new="2026-06-15"), ctx)

    lithos.task_update.assert_not_awaited()
    info_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert any(
        "task abc already at scheduled_for=" in m
        and "2026-06-15" in m
        and "skipping idempotent update" in m
        for m in info_msgs
    ), info_msgs


async def test_proceeds_when_lithos_scheduled_for_differs() -> None:
    """Happy path: Lithos has a different date, payload's ``new``
    doesn't match Lithos → proceed with the update."""
    lithos = AsyncMock()
    lithos.task_get.return_value = Task(
        id="abc",
        title="t",
        status="open",
        tags=(),
        metadata={"scheduled_for": "2026-05-20"},
        claims=(),
    )
    ctx = _ctx(lithos=lithos)

    await handle(_event(task_id="abc", prior="2026-05-20", new="2026-06-15"), ctx)

    lithos.task_update.assert_awaited_once_with(
        task_id="abc",
        agent="lithos-orchestrator-test",
        metadata={"scheduled_for": "2026-06-15"},
    )


async def test_skips_when_task_not_found_in_lithos(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """task_get returns None → skip with a clear log."""
    lithos = AsyncMock()
    ctx = _ctx(lithos=lithos)
    lithos.task_get.return_value = None

    with caplog.at_level(logging.INFO, logger="test.obsidian_due_date_changed"):
        await handle(_event(task_id="gone1", prior="2026-05-20", new="2026-06-15"), ctx)

    lithos.task_update.assert_not_awaited()
    info_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert any(
        "task gone1 not found in Lithos" in m and "skipping" in m for m in info_msgs
    ), info_msgs


async def test_strict_check_skips_when_lithos_has_no_date_and_new_is_none() -> None:
    """``prior="2026-05-20", new=None`` (user deleted marker), Lithos
    already has no ``scheduled_for`` → ``current=None == new=None`` →
    skip."""
    lithos = AsyncMock()
    lithos.task_get.return_value = _default_open_task("abc")  # no scheduled_for
    ctx = _ctx(lithos=lithos)

    await handle(_event(task_id="abc", prior="2026-05-20", new=None), ctx)

    lithos.task_update.assert_not_awaited()


async def test_pre_check_uses_task_get_not_task_status() -> None:
    """Regression guard: the handler uses ``task_get`` (light, no
    claims) rather than ``task_status``."""
    lithos = AsyncMock()
    lithos.task_get.return_value = _default_open_task("abc")
    ctx = _ctx(lithos=lithos)

    await handle(_event(task_id="abc", prior="2026-05-20", new="2026-06-15"), ctx)

    lithos.task_get.assert_awaited_once_with(task_id="abc")
    lithos.task_status.assert_not_awaited()


# ── Idempotency: ISO datetime normalisation (reviewer-finding regression) ─


async def test_skips_when_lithos_scheduled_for_is_iso_datetime_matching_new() -> None:
    """Lithos holds ``scheduled_for="2026-06-15T09:00:00Z"`` (full ISO
    datetime); watcher emits ``new="2026-06-15"`` (date-only — that's
    all the renderer projects and all the user can type into the
    ``📅`` marker). Without datetime normalisation, the string compare
    fails and the handler pushes back a date-only patch that silently
    drops the time component. With normalisation, both sides parse to
    ``date(2026, 6, 15)`` and the handler skips — Lithos's datetime
    is preserved."""
    lithos = AsyncMock()
    lithos.task_get.return_value = Task(
        id="abc",
        title="t",
        status="open",
        tags=(),
        metadata={"scheduled_for": "2026-06-15T09:00:00Z"},
        claims=(),
    )
    ctx = _ctx(lithos=lithos)

    await handle(_event(task_id="abc", prior="2026-05-20", new="2026-06-15"), ctx)

    lithos.task_update.assert_not_awaited()


async def test_proceeds_when_lithos_iso_datetime_date_differs_from_new() -> None:
    """Lithos holds ``"2026-06-15T09:00:00Z"`` but the user changed
    the marker to a different date. The dates don't match → the
    handler proceeds (with the documented limitation that the time
    component is lost — the projection has no way to round-trip it)."""
    lithos = AsyncMock()
    lithos.task_get.return_value = Task(
        id="abc",
        title="t",
        status="open",
        tags=(),
        metadata={"scheduled_for": "2026-06-15T09:00:00Z"},
        claims=(),
    )
    ctx = _ctx(lithos=lithos)

    await handle(_event(task_id="abc", prior="2026-06-15", new="2026-06-20"), ctx)

    lithos.task_update.assert_awaited_once_with(
        task_id="abc",
        agent="lithos-orchestrator-test",
        metadata={"scheduled_for": "2026-06-20"},
    )


async def test_proceeds_when_lithos_has_malformed_value_and_user_deletes() -> None:
    """Lithos holds an unparseable value (``"garbage"``); user removes
    the marker (``new=None``). Both ``parse_scheduled_for`` to
    ``None``, but the raw-not-None guard prevents the "matches new=None"
    skip — handler proceeds with ``scheduled_for=None`` to clean up the
    garbage. Without the guard, the handler would silently leave the
    bad value in place."""
    lithos = AsyncMock()
    lithos.task_get.return_value = Task(
        id="abc",
        title="t",
        status="open",
        tags=(),
        metadata={"scheduled_for": "garbage"},
        claims=(),
    )
    ctx = _ctx(lithos=lithos)

    await handle(_event(task_id="abc", prior="2026-06-15", new=None), ctx)

    lithos.task_update.assert_awaited_once_with(
        task_id="abc",
        agent="lithos-orchestrator-test",
        metadata={"scheduled_for": None},
    )


async def test_skips_when_lithos_absent_and_new_is_none_after_normalisation() -> None:
    """Lithos has no ``scheduled_for`` (raw=None), user removes the
    marker (new=None). Both raw values are None; both parse to None;
    ``both_absent`` skip path applies. Tightens the equivalent test
    above by exercising the path AFTER the datetime-normalisation
    change."""
    lithos = AsyncMock()
    lithos.task_get.return_value = _default_open_task("abc")
    ctx = _ctx(lithos=lithos)

    await handle(_event(task_id="abc", prior="2026-06-15", new=None), ctx)

    lithos.task_update.assert_not_awaited()
