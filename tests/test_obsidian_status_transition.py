"""Tests for ``lithos_loom.subscriptions._obsidian_status_transition``
(Slice 2 US17).

The handler is stateless; tests just call ``handle(event, ctx)``
directly with synthetic events and assert on a mocked
``ctx.lithos`` (``AsyncMock``).
"""

from __future__ import annotations

import itertools
import logging
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest

from lithos_loom.bus import Event
from lithos_loom.subscriptions import SubscriptionContext
from lithos_loom.subscriptions._obsidian_status_transition import (
    _CANCEL_REASON,
    _REOPEN_REQUEST_SUMMARY,
    handle,
)

# ── Helpers ────────────────────────────────────────────────────────────


def _ctx(
    lithos: Any | None = None,
    agent_id: str = "lithos-orchestrator-test",
) -> SubscriptionContext:
    return SubscriptionContext(
        lithos=lithos if lithos is not None else AsyncMock(),
        logger=logging.getLogger("test.obsidian_status_transition"),
        agent_id=agent_id,
    )


def _event(
    *,
    task_id: str = "abc",
    prior: str = "[ ]",
    new: str = "[x]",
    event_type: str = "obsidian.task.status_changed",
) -> Event:
    return Event(
        type=event_type,
        timestamp=datetime.now(UTC),
        payload={"task_id": task_id, "prior": prior, "new": new},
    )


# ── US17: [ ] → [x] → task_complete ────────────────────────────────────


async def test_open_to_done_calls_task_complete() -> None:
    """``[ ]`` → ``[x]`` for a known task → ``lithos.task_complete`` called
    with the task id and the context's agent id."""
    lithos = AsyncMock()
    ctx = _ctx(lithos=lithos, agent_id="lithos-orchestrator-samsara")

    await handle(_event(task_id="abc", prior="[ ]", new="[x]"), ctx)

    lithos.task_complete.assert_awaited_once_with(
        task_id="abc", agent="lithos-orchestrator-samsara"
    )


async def test_open_to_done_logs_at_info(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The happy path emits an INFO log naming the task id so operators
    have a grep-able trail."""
    ctx = _ctx()
    with caplog.at_level(logging.INFO, logger="test.obsidian_status_transition"):
        await handle(_event(task_id="abc123"), ctx)

    info_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert any("completed task abc123 via Obsidian tick" in m for m in info_msgs), (
        info_msgs
    )


# ── US18: [ ] → [-] → task_cancel ──────────────────────────────────────


async def test_open_to_cancelled_calls_task_cancel() -> None:
    """``[ ]`` → ``[-]`` for a known task → ``lithos.task_cancel`` called
    with the task id, the context's agent id, and the constant
    breadcrumb reason."""
    lithos = AsyncMock()
    ctx = _ctx(lithos=lithos, agent_id="lithos-orchestrator-samsara")

    await handle(_event(task_id="xyz", prior="[ ]", new="[-]"), ctx)

    lithos.task_cancel.assert_awaited_once_with(
        task_id="xyz",
        agent="lithos-orchestrator-samsara",
        reason=_CANCEL_REASON,
    )
    # And task_complete must not have been called for this transition.
    lithos.task_complete.assert_not_awaited()


async def test_open_to_cancelled_logs_at_info(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The cancel path emits an INFO log naming the task id."""
    ctx = _ctx()
    with caplog.at_level(logging.INFO, logger="test.obsidian_status_transition"):
        await handle(_event(task_id="xyz789", prior="[ ]", new="[-]"), ctx)

    info_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert any("cancelled task xyz789 via Obsidian flip" in m for m in info_msgs), (
        info_msgs
    )


# ── US19: [x] → [ ] → [ReopenRequested] finding ────────────────────────


async def test_untick_posts_reopen_request_finding() -> None:
    """``[x]`` → ``[ ]`` for a known task → ``lithos.finding_post``
    awaited once with the constant ``[ReopenRequested]`` summary and
    the context's agent id. ``task_complete`` and ``task_cancel`` must
    NOT be called — untick is a reopen-request, not a state
    transition."""
    lithos = AsyncMock()
    ctx = _ctx(lithos=lithos, agent_id="lithos-orchestrator-samsara")

    await handle(_event(task_id="done1", prior="[x]", new="[ ]"), ctx)

    lithos.finding_post.assert_awaited_once_with(
        task_id="done1",
        summary=_REOPEN_REQUEST_SUMMARY,
        agent="lithos-orchestrator-samsara",
    )
    lithos.task_complete.assert_not_awaited()
    lithos.task_cancel.assert_not_awaited()


async def test_untick_logs_at_info(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The reopen-request path emits an INFO log naming the task id
    and the ``[ReopenRequested]`` marker so operators can grep for it."""
    ctx = _ctx()
    with caplog.at_level(logging.INFO, logger="test.obsidian_status_transition"):
        await handle(_event(task_id="done7", prior="[x]", new="[ ]"), ctx)

    info_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert any("[ReopenRequested]" in m and "done7" in m for m in info_msgs), info_msgs


def test_reopen_request_summary_uses_reopen_prefix() -> None:
    """The summary starts with the exact ``[ReopenRequested]`` prefix
    the AGENTS.md stable-prefix table specifies (PascalCase, bracketed,
    no hyphens / underscores). Pins the wire-format that lithos-lens
    will scan for."""
    assert _REOPEN_REQUEST_SUMMARY.startswith("[ReopenRequested] ")


# ── US20: [/] and [>] are Obsidian-only conventions, always silent ──────


_US20_MARKERS = ("[/]", "[>]")
_ALL_MARKERS = ("[ ]", "[x]", "[-]", "[/]", "[>]")

# Every (prior, new) where at least one side is [/] or [>]. Sorted for
# deterministic parametrise IDs. Includes same-marker pairs even though
# the fs-watcher source filters them out — the handler must still
# no-op safely if one ever shows up via a future source.
_US20_TRANSITIONS = sorted(
    {
        (p, n)
        for p, n in itertools.product(_ALL_MARKERS, _ALL_MARKERS)
        if p in _US20_MARKERS or n in _US20_MARKERS
    }
)


@pytest.mark.parametrize(("prior", "new"), _US20_TRANSITIONS)
async def test_in_progress_and_rescheduled_markers_are_silent_no_ops(
    prior: str, new: str
) -> None:
    """US20: every transition involving ``[/]`` (in progress) or ``[>]``
    (rescheduled) is an Obsidian-only convention and must never trigger
    a Lithos call.

    Anti-regression for any future change that tries to map, say,
    ``[/] → [x]`` to ``task_complete`` — that would break this test
    rather than slip past the generic catch-all below.
    """
    lithos = AsyncMock()
    ctx = _ctx(lithos=lithos)

    await handle(_event(prior=prior, new=new), ctx)

    lithos.task_complete.assert_not_awaited()
    lithos.task_cancel.assert_not_awaited()
    lithos.finding_post.assert_not_awaited()


@pytest.mark.parametrize("new_marker", _US20_MARKERS)
async def test_unknown_transition_logged_at_debug(
    new_marker: str, caplog: pytest.LogCaptureFixture
) -> None:
    """Skipped transitions leave a DEBUG breadcrumb so operators can
    enable verbose logging to see what's flowing through. Parametrised
    over both US20 markers (``[/]`` and ``[>]``) so the exact format
    is pinned for both — a US20-regression guard alongside the
    no-Lithos-call test above.
    """
    ctx = _ctx()
    with caplog.at_level(logging.DEBUG, logger="test.obsidian_status_transition"):
        await handle(_event(task_id="xyz", prior="[ ]", new=new_marker), ctx)

    debug_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG]
    expected = f"no handler for transition [ ]→{new_marker} on task xyz"
    assert any(expected in m for m in debug_msgs), debug_msgs


# ── Other weird transitions stay silent (catch-all anti-regression) ────


@pytest.mark.parametrize(
    ("prior", "new"),
    [
        ("[-]", "[ ]"),  # un-cancel — not in scope; PRD only covers [x]→[ ]
        ("[x]", "[-]"),  # done → cancelled (weird but possible)
        ("[ ]", "[ ]"),  # same-marker (won't fire from source, but safe)
    ],
)
async def test_other_transitions_are_silent_no_ops(prior: str, new: str) -> None:
    """Catch-all for transitions outside the dispatch table that are
    NOT US20's ``[/]``/``[>]`` cases — those are covered by
    ``test_in_progress_and_rescheduled_markers_are_silent_no_ops``
    above. This test guards the remaining "weird but possible"
    transitions (un-cancel, done→cancelled, same-marker) against
    silently growing a Lithos side effect."""
    lithos = AsyncMock()
    ctx = _ctx(lithos=lithos)

    await handle(_event(prior=prior, new=new), ctx)

    lithos.task_complete.assert_not_awaited()
    lithos.task_cancel.assert_not_awaited()
    lithos.finding_post.assert_not_awaited()


# ── Robustness ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "bad_payload",
    [
        {},  # all keys missing
        {"task_id": "x"},  # missing prior + new
        {"prior": "[ ]", "new": "[x]"},  # missing task_id
        {"task_id": "x", "prior": "[ ]"},  # missing new
        {"task_id": "x", "new": "[x]"},  # missing prior
    ],
)
async def test_malformed_payload_warns_and_returns(
    bad_payload: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    """Missing payload keys → handler logs a warning, makes no Lithos
    calls, doesn't raise. Matches the silent-degradation contract the
    rest of the subscription layer follows for malformed bus events."""
    lithos = AsyncMock()
    ctx = _ctx(lithos=lithos)
    event = Event(
        type="obsidian.task.status_changed",
        timestamp=datetime.now(UTC),
        payload=bad_payload,
    )

    with caplog.at_level(logging.WARNING, logger="test.obsidian_status_transition"):
        await handle(event, ctx)  # must not raise

    lithos.task_complete.assert_not_awaited()
    warn_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("malformed payload" in m for m in warn_msgs), warn_msgs


async def test_lithos_error_propagates() -> None:
    """A ``LithosClientError`` (or any exception) from ``task_complete``
    must bubble up so the :class:`SubscriptionRunner` retry-with-backoff
    + on_persistent_failure=friction backstop can take over."""
    lithos = AsyncMock()
    lithos.task_complete.side_effect = RuntimeError("simulated lithos error")
    ctx = _ctx(lithos=lithos)

    with pytest.raises(RuntimeError, match="simulated lithos error"):
        await handle(_event(prior="[ ]", new="[x]"), ctx)


async def test_handler_uses_ctx_agent_id_not_hardcoded() -> None:
    """The agent passed to ``task_complete`` comes from ``ctx.agent_id``,
    not a hardcoded string — different deployments (samsara, mac-mini,
    test) must each pass their own identity through unchanged."""
    lithos = AsyncMock()
    ctx = _ctx(lithos=lithos, agent_id="lithos-orchestrator-mac-mini")

    await handle(_event(), ctx)

    lithos.task_complete.assert_awaited_once()
    assert lithos.task_complete.await_args.kwargs["agent"] == (
        "lithos-orchestrator-mac-mini"
    )
