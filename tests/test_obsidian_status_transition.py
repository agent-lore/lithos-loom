"""Tests for ``lithos_loom.subscriptions._obsidian_status_transition``
(Slice 2 US17-US22).

The handler is stateless; tests just call ``handle(event, ctx)``
directly with synthetic events and assert on a mocked
``ctx.lithos`` (``AsyncMock``).

US22 added a pre-check via ``lithos_task_get`` (post-lithos#294,
swapped from ``task_status`` for lighter RPC) at the top of
``handle``. The :func:`_ctx` helper below wires
``lithos.task_get.return_value`` to an open ``Task`` by default so
existing happy-path tests reach their mutating call without
per-test boilerplate; tests that exercise the skip predicates
override the return value (or set ``side_effect``) explicitly.
"""

from __future__ import annotations

import itertools
import logging
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest

from lithos_loom.bus import Event
from lithos_loom.lithos_client import Task
from lithos_loom.subscriptions import SubscriptionContext
from lithos_loom.subscriptions._obsidian_status_transition import (
    _CANCEL_REASON,
    _REOPEN_REQUEST_SUMMARY,
    handle,
)

# ── Helpers ────────────────────────────────────────────────────────────


def _task(task_id: str = "abc", *, status: str = "open") -> Task:
    """Construct a Task matching the envelope ``lithos_task_status``
    returns (id, title, status, claims — no tags / metadata).

    Default ``status="open"`` so happy-path tests skip the pre-check
    branch and reach their mutating call. Override per-test to
    exercise the skip predicates."""
    return Task(
        id=task_id,
        title="t",
        status=status,
        tags=(),
        metadata={},
        claims=(),
    )


def _ctx(
    lithos: Any | None = None,
    agent_id: str = "lithos-orchestrator-test",
    *,
    current_status: str | None = "open",
) -> SubscriptionContext:
    """Build a SubscriptionContext with a default-wired ``task_get``.

    Post-lithos#294 the status-transition handler pre-checks via
    ``task_get`` (no claims overhead) rather than ``task_status``.

    When ``current_status`` is a string, ``lithos.task_get`` returns
    a Task with that status. When ``current_status is None``,
    ``task_get`` returns ``None`` (the deleted-upstream case).

    If the caller passes a pre-configured ``lithos`` AsyncMock, the
    helper does NOT overwrite an existing ``task_get.return_value``
    or ``side_effect`` — it only fills in a default when the mock
    has no configuration. This lets individual tests prepare a
    bespoke ``side_effect`` (e.g., for sequencing assertions) by
    constructing the AsyncMock themselves before calling _ctx."""
    if lithos is None:
        lithos = AsyncMock()
    # Only set a default when the test hasn't already configured one.
    task_get_mock = lithos.task_get
    if (
        task_get_mock.return_value is None
        or isinstance(task_get_mock.return_value, AsyncMock)
    ) and task_get_mock.side_effect is None:
        task_get_mock.return_value = (
            _task(status=current_status) if current_status is not None else None
        )
    return SubscriptionContext(
        lithos=lithos,
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
    transition.

    US22 pre-check: requires the task to actually be in
    ``status=completed`` upstream, else the finding is nonsensical.
    """
    lithos = AsyncMock()
    # The pre-check sees a completed task → finding-post proceeds.
    ctx = _ctx(
        lithos=lithos,
        agent_id="lithos-orchestrator-samsara",
        current_status="completed",
    )

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
    ctx = _ctx(current_status="completed")
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
    a Lithos call. The dispatch-table miss must also short-circuit
    BEFORE the US22 pre-check — calling task_get for a no-op
    transition would be wasteful.

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
    # US22: dispatch-table miss short-circuits before the RPC.
    lithos.task_get.assert_not_awaited()


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


# ── US22: idempotency pre-check via task_get (lithos#294) ──────────────


async def test_complete_pre_checks_task_get() -> None:
    """The dispatch layer calls ``task_get`` once before invoking
    the transition function. The happy path (open → task_complete)
    must include the pre-check RPC."""
    lithos = AsyncMock()
    ctx = _ctx(lithos=lithos)

    await handle(_event(task_id="abc", prior="[ ]", new="[x]"), ctx)

    lithos.task_get.assert_awaited_once_with(task_id="abc")
    lithos.task_complete.assert_awaited_once()
    # Regression guard: must NOT use task_status (heavier — returns
    # claims we don't need).
    lithos.task_status.assert_not_awaited()


async def test_complete_skips_when_already_completed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Pre-check sees status=completed → ``task_complete`` NOT called,
    INFO log mentions the idempotent skip."""
    lithos = AsyncMock()
    ctx = _ctx(lithos=lithos, current_status="completed")

    with caplog.at_level(logging.INFO, logger="test.obsidian_status_transition"):
        await handle(_event(task_id="abc", prior="[ ]", new="[x]"), ctx)

    lithos.task_complete.assert_not_awaited()
    info_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert any(
        "task abc already completed" in m and "idempotent skip" in m for m in info_msgs
    ), info_msgs


async def test_complete_skips_when_already_cancelled(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A cancelled task cannot be completed; the pre-check skips it."""
    lithos = AsyncMock()
    ctx = _ctx(lithos=lithos, current_status="cancelled")

    with caplog.at_level(logging.INFO, logger="test.obsidian_status_transition"):
        await handle(_event(task_id="xyz", prior="[ ]", new="[x]"), ctx)

    lithos.task_complete.assert_not_awaited()
    info_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert any(
        "task xyz already cancelled" in m and "idempotent skip" in m for m in info_msgs
    ), info_msgs


async def test_complete_skips_when_task_not_found(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """task_get returns None (deleted upstream) → no mutating call;
    INFO log mentions "not found"."""
    lithos = AsyncMock()
    ctx = _ctx(lithos=lithos, current_status=None)

    with caplog.at_level(logging.INFO, logger="test.obsidian_status_transition"):
        await handle(_event(task_id="gone1", prior="[ ]", new="[x]"), ctx)

    lithos.task_complete.assert_not_awaited()
    info_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert any("task gone1 not found in Lithos" in m for m in info_msgs), info_msgs


async def test_cancel_skips_when_already_completed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Cancelling a completed task is meaningless; the pre-check skips
    it. Mirrors ``test_complete_skips_when_already_cancelled``."""
    lithos = AsyncMock()
    ctx = _ctx(lithos=lithos, current_status="completed")

    with caplog.at_level(logging.INFO, logger="test.obsidian_status_transition"):
        await handle(_event(task_id="abc", prior="[ ]", new="[-]"), ctx)

    lithos.task_cancel.assert_not_awaited()
    info_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert any(
        "task abc already completed" in m and "idempotent skip" in m for m in info_msgs
    ), info_msgs


async def test_cancel_skips_when_already_cancelled(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Re-cancelling an already-cancelled task is a no-op."""
    lithos = AsyncMock()
    ctx = _ctx(lithos=lithos, current_status="cancelled")

    with caplog.at_level(logging.INFO, logger="test.obsidian_status_transition"):
        await handle(_event(task_id="xyz", prior="[ ]", new="[-]"), ctx)

    lithos.task_cancel.assert_not_awaited()
    info_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert any(
        "task xyz already cancelled" in m and "idempotent skip" in m for m in info_msgs
    ), info_msgs


async def test_cancel_skips_when_task_not_found(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Cancel on a deleted-upstream task short-circuits at the
    task_get pre-check."""
    lithos = AsyncMock()
    ctx = _ctx(lithos=lithos, current_status=None)

    with caplog.at_level(logging.INFO, logger="test.obsidian_status_transition"):
        await handle(_event(task_id="gone2", prior="[ ]", new="[-]"), ctx)

    lithos.task_cancel.assert_not_awaited()
    info_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert any("task gone2 not found in Lithos" in m for m in info_msgs), info_msgs


async def test_reopen_request_skips_when_task_is_open(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``[x]→[ ]`` on a task Lithos shows as open is the projection-lag
    case — the task isn't actually completed upstream, so posting
    ``[ReopenRequested]`` would be nonsensical."""
    lithos = AsyncMock()
    ctx = _ctx(lithos=lithos, current_status="open")

    with caplog.at_level(logging.INFO, logger="test.obsidian_status_transition"):
        await handle(_event(task_id="op1", prior="[x]", new="[ ]"), ctx)

    lithos.finding_post.assert_not_awaited()
    info_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert any(
        "task op1 is open (not completed)" in m and "skipping [ReopenRequested]" in m
        for m in info_msgs
    ), info_msgs


async def test_reopen_request_skips_when_task_is_cancelled(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A cancelled task isn't a reopen candidate either; skip without
    posting the finding."""
    lithos = AsyncMock()
    ctx = _ctx(lithos=lithos, current_status="cancelled")

    with caplog.at_level(logging.INFO, logger="test.obsidian_status_transition"):
        await handle(_event(task_id="cn1", prior="[x]", new="[ ]"), ctx)

    lithos.finding_post.assert_not_awaited()
    info_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert any(
        "task cn1 is cancelled (not completed)" in m
        and "skipping [ReopenRequested]" in m
        for m in info_msgs
    ), info_msgs


async def test_reopen_request_skips_when_task_not_found(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Untick on a deleted-upstream task short-circuits at the
    task_get pre-check."""
    lithos = AsyncMock()
    ctx = _ctx(lithos=lithos, current_status=None)

    with caplog.at_level(logging.INFO, logger="test.obsidian_status_transition"):
        await handle(_event(task_id="gone3", prior="[x]", new="[ ]"), ctx)

    lithos.finding_post.assert_not_awaited()
    info_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert any("task gone3 not found in Lithos" in m for m in info_msgs), info_msgs


async def test_reopen_request_posts_finding_when_task_is_completed() -> None:
    """Happy path: ``[x]→[ ]`` on a task that IS completed upstream
    → finding_post fires (verifies the predicate isn't over-strict)."""
    lithos = AsyncMock()
    ctx = _ctx(lithos=lithos, current_status="completed")

    await handle(_event(task_id="done5", prior="[x]", new="[ ]"), ctx)

    lithos.finding_post.assert_awaited_once_with(
        task_id="done5",
        summary=_REOPEN_REQUEST_SUMMARY,
        agent="lithos-orchestrator-test",
    )


async def test_pre_check_happens_once_per_event() -> None:
    """The dispatch layer makes exactly one ``task_get`` call per
    event, regardless of which transition fires. Catches a regression
    where the per-transition fn might double-dip on the RPC."""
    lithos = AsyncMock()
    ctx = _ctx(lithos=lithos, current_status="completed")

    await handle(_event(task_id="abc", prior="[ ]", new="[x]"), ctx)

    assert lithos.task_get.await_count == 1


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
    # Malformed payloads short-circuit before the pre-check.
    lithos.task_get.assert_not_awaited()
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
