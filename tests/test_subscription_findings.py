"""Tests for the shared one-shot finding + de-dup marker helpers (ARCH-7).

``_develop_pr_merge`` (a delivered PR reached a closed end-state →
``[DeliveredPRClosed]``) and ``_github_issue_push`` (a linked issue was deleted
→ ``[LinkedIssueGone]``) both post a one-shot ``[Friction]``/prefixed finding
and then write a url-scoped metadata marker so the breadcrumb fires once. That
finding-then-mark idiom now lives once in ``subscriptions/_findings.py``; these
tests pin the extracted behaviour directly (the callers' own tests exercise it
end-to-end through ``reconcile_develop_pr`` / the push handler).
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock

from lithos_loom.errors import LithosClientError
from lithos_loom.subscriptions import SubscriptionContext
from lithos_loom.subscriptions._findings import post_finding_then_mark, write_marker


def _ctx(lithos: AsyncMock) -> SubscriptionContext:
    return SubscriptionContext(
        lithos=lithos, logger=logging.getLogger("test.findings"), agent_id="agent-x"
    )


# ── write_marker ──────────────────────────────────────────────────────


async def test_write_marker_writes_via_task_update() -> None:
    lithos = AsyncMock()
    await write_marker(_ctx(lithos), task_id="T1", marker={"k": "v"}, subsystem="sub")
    lithos.task_update.assert_awaited_once_with(task_id="T1", metadata={"k": "v"})


async def test_write_marker_swallows_task_not_found() -> None:
    """A genuinely-deleted task: nothing left to mark, must not raise."""
    lithos = AsyncMock()
    lithos.task_update.side_effect = LithosClientError("task_not_found", "gone")
    await write_marker(_ctx(lithos), task_id="T1", marker={"k": "v"}, subsystem="sub")


async def test_write_marker_swallows_other_errors() -> None:
    """Any other Lithos error warns and leaves the marker unset — never raises."""
    lithos = AsyncMock()
    lithos.task_update.side_effect = LithosClientError("server_error", "boom")
    await write_marker(_ctx(lithos), task_id="T1", marker={"k": "v"}, subsystem="sub")


# ── post_finding_then_mark ────────────────────────────────────────────


async def test_post_finding_then_mark_posts_then_marks() -> None:
    lithos = AsyncMock()
    await post_finding_then_mark(
        _ctx(lithos),
        task_id="T1",
        summary="[X] something happened",
        marker={"state": "gone", "url": "u"},
        subsystem="sub",
        retry_hint="will retry next sweep",
    )
    lithos.finding_post.assert_awaited_once_with(
        task_id="T1", summary="[X] something happened"
    )
    lithos.task_update.assert_awaited_once_with(
        task_id="T1", metadata={"state": "gone", "url": "u"}
    )


async def test_marker_task_id_marks_a_different_task_than_the_finding() -> None:
    """The pr-gate resolver posts [DeliveredPRClosed] on the STORY but marks the
    GATE (the gate is what stays open and would be re-swept)."""
    lithos = AsyncMock()
    await post_finding_then_mark(
        _ctx(lithos),
        task_id="story",
        summary="[DeliveredPRClosed] …",
        marker={"state": "closed_unmerged"},
        subsystem="pr-gate",
        retry_hint="hint",
        marker_task_id="gate",
    )
    lithos.finding_post.assert_awaited_once_with(
        task_id="story", summary="[DeliveredPRClosed] …"
    )
    lithos.task_update.assert_awaited_once_with(
        task_id="gate", metadata={"state": "closed_unmerged"}
    )


async def test_task_not_found_on_finding_still_marks() -> None:
    """post-lithos#303 a terminal task still accepts the marker, so a
    ``task_not_found`` on the finding falls through to the mark."""
    lithos = AsyncMock()
    lithos.finding_post.side_effect = LithosClientError("task_not_found", "terminal")
    await post_finding_then_mark(
        _ctx(lithos),
        task_id="T1",
        summary="s",
        marker={"k": "v"},
        subsystem="sub",
        retry_hint="hint",
    )
    lithos.task_update.assert_awaited_once_with(task_id="T1", metadata={"k": "v"})


async def test_other_error_on_finding_leaves_marker_unset() -> None:
    """A transient finding failure returns WITHOUT marking so the whole
    breadcrumb retries next cycle."""
    lithos = AsyncMock()
    lithos.finding_post.side_effect = LithosClientError("server_error", "boom")
    await post_finding_then_mark(
        _ctx(lithos),
        task_id="T1",
        summary="s",
        marker={"k": "v"},
        subsystem="sub",
        retry_hint="hint",
    )
    lithos.task_update.assert_not_awaited()


async def test_task_not_found_on_mark_is_swallowed() -> None:
    lithos = AsyncMock()
    lithos.task_update.side_effect = LithosClientError("task_not_found", "gone")
    await post_finding_then_mark(
        _ctx(lithos),
        task_id="T1",
        summary="s",
        marker={"k": "v"},
        subsystem="sub",
        retry_hint="hint",
    )
    lithos.finding_post.assert_awaited_once()
    lithos.task_update.assert_awaited_once()


def test_findings_helpers_are_exported() -> None:
    from lithos_loom.subscriptions import _findings

    assert set(_findings.__all__) == {"post_finding_then_mark", "write_marker"}
