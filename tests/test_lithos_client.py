"""Tests for ``lithos_loom.lithos_client`` (Slice 0 US3).

The slice-0 surface is intentionally narrow: only ``task_list`` plus the
envelope-decoding helpers. The MCP-over-SSE transport is exercised through
``LithosClient`` itself, but the wire-format unit tests target the pure
parse helpers so we don't have to spin up a real Lithos to verify shape.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock

import pytest
from mcp.types import CallToolResult, TextContent

from lithos_loom.errors import LithosClientError
from lithos_loom.lithos_client import (
    LithosClient,
    Task,
    _parse_task_list_response,
)

# ── _parse_task_list_response (pure helper) ────────────────────────────


def _content(data: dict) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=json.dumps(data))])


def test_parse_task_list_returns_typed_tasks() -> None:
    result = _content(
        {
            "tasks": [
                {
                    "id": "abc",
                    "title": "Build it",
                    "status": "open",
                    "tags": ["trigger:story-implement"],
                    "metadata": {"project": "lithos-loom"},
                    "claims": [],
                },
            ]
        }
    )
    tasks = _parse_task_list_response(result)
    assert len(tasks) == 1
    t = tasks[0]
    assert isinstance(t, Task)
    assert t.id == "abc"
    assert t.title == "Build it"
    assert t.status == "open"
    assert t.tags == ("trigger:story-implement",)
    assert t.metadata == {"project": "lithos-loom"}
    assert t.claims == ()


def test_parse_task_list_preserves_claims_when_with_claims_true() -> None:
    result = _content(
        {
            "tasks": [
                {
                    "id": "abc",
                    "title": "x",
                    "status": "open",
                    "tags": [],
                    "metadata": {},
                    "claims": [
                        {
                            "agent": "claude-code-1",
                            "aspect": "implementation",
                            "expires_at": "2026-05-15T12:00:00Z",
                        }
                    ],
                },
            ]
        }
    )
    tasks = _parse_task_list_response(result)
    assert len(tasks[0].claims) == 1
    assert tasks[0].claims[0]["agent"] == "claude-code-1"


def test_parse_task_list_returns_empty_list_for_empty_envelope() -> None:
    result = _content({"tasks": []})
    assert _parse_task_list_response(result) == []


def test_parse_task_list_raises_on_error_envelope() -> None:
    result = _content(
        {"status": "error", "code": "invalid_input", "message": "bad status filter"}
    )
    with pytest.raises(LithosClientError) as exc:
        _parse_task_list_response(result)
    assert exc.value.code == "invalid_input"
    assert "bad status filter" in str(exc.value)


def test_parse_task_list_raises_when_result_is_marked_error() -> None:
    """A FastMCP-side isError=True must surface as LithosClientError."""
    err_result = CallToolResult(
        content=[TextContent(type="text", text="upstream blew up")],
        isError=True,
    )
    with pytest.raises(LithosClientError):
        _parse_task_list_response(err_result)


def test_parse_task_list_raises_on_missing_tasks_key() -> None:
    result = _content({"unexpected": "shape"})
    with pytest.raises(LithosClientError, match="missing 'tasks'"):
        _parse_task_list_response(result)


def test_parse_task_list_tolerates_missing_optional_fields() -> None:
    """Some tasks may lack `tags` or `metadata` or `claims` keys."""
    result = _content({"tasks": [{"id": "x", "title": "t", "status": "open"}]})
    tasks = _parse_task_list_response(result)
    assert tasks[0].tags == ()
    assert tasks[0].metadata == {}
    assert tasks[0].claims == ()


# ── LithosClient.task_list (through-the-SDK happy-path) ────────────────


async def test_lithos_client_task_list_calls_correct_tool() -> None:
    """``task_list`` posts the right MCP tool name + arguments."""
    client = LithosClient(base_url="http://example.test:8765")
    fake_session = AsyncMock()
    fake_session.call_tool.return_value = _content({"tasks": []})
    client._session = fake_session  # type: ignore[assignment]

    await client.task_list(status="open", with_claims=True)

    fake_session.call_tool.assert_awaited_once_with(
        "lithos_task_list", arguments={"with_claims": True, "status": "open"}
    )


async def test_lithos_client_task_list_omits_none_filters() -> None:
    client = LithosClient(base_url="http://example.test:8765")
    fake_session = AsyncMock()
    fake_session.call_tool.return_value = _content({"tasks": []})
    client._session = fake_session  # type: ignore[assignment]

    await client.task_list()

    fake_session.call_tool.assert_awaited_once_with(
        "lithos_task_list", arguments={"with_claims": False}
    )


async def test_lithos_client_task_list_returns_parsed_tasks() -> None:
    client = LithosClient(base_url="http://example.test:8765")
    fake_session = AsyncMock()
    fake_session.call_tool.return_value = _content(
        {
            "tasks": [
                {
                    "id": "abc",
                    "title": "t",
                    "status": "open",
                    "tags": ["x"],
                    "metadata": {},
                    "claims": [],
                },
            ]
        }
    )
    client._session = fake_session  # type: ignore[assignment]

    tasks = await client.task_list()
    assert len(tasks) == 1
    assert tasks[0].id == "abc"


async def test_lithos_client_task_list_raises_when_not_initialized() -> None:
    client = LithosClient(base_url="http://example.test:8765")
    with pytest.raises(LithosClientError, match="not initialised"):
        await client.task_list()


async def test_lithos_client_task_list_passes_resolved_since_as_iso_string() -> None:
    """lithos#286: server-side resolved_since filter is sent as an
    ISO-8601 datetime string. Loom converts the datetime arg at the
    boundary so callers stay in Python time."""
    from datetime import UTC, datetime

    client = LithosClient(base_url="http://example.test:8765")
    fake_session = AsyncMock()
    fake_session.call_tool.return_value = _content({"tasks": []})
    client._session = fake_session  # type: ignore[assignment]

    cutoff = datetime(2026, 5, 14, 0, 0, 0, tzinfo=UTC)
    await client.task_list(status="completed", resolved_since=cutoff)

    fake_session.call_tool.assert_awaited_once_with(
        "lithos_task_list",
        arguments={
            "with_claims": False,
            "status": "completed",
            "resolved_since": cutoff.isoformat(),
        },
    )


async def test_lithos_client_task_list_omits_resolved_since_when_none() -> None:
    """Wire-identical to the pre-#286 contract when the new kwarg is
    not used — important during the staging→prod rollout window so an
    old Lithos doesn't trip on an unknown parameter."""
    client = LithosClient(base_url="http://example.test:8765")
    fake_session = AsyncMock()
    fake_session.call_tool.return_value = _content({"tasks": []})
    client._session = fake_session  # type: ignore[assignment]

    await client.task_list(status="open")

    fake_session.call_tool.assert_awaited_once_with(
        "lithos_task_list", arguments={"with_claims": False, "status": "open"}
    )


# ── _parse_task resolved_at handling ───────────────────────────────────


def test_parse_task_reads_resolved_at_field() -> None:
    """lithos#286 renamed the column to resolved_at; loom reads the new
    payload key into Task.resolved_at as a parsed datetime."""
    from datetime import datetime

    result = _content(
        {
            "tasks": [
                {
                    "id": "abc",
                    "title": "t",
                    "status": "completed",
                    "resolved_at": "2026-05-21T10:00:00+00:00",
                }
            ]
        }
    )
    tasks = _parse_task_list_response(result)
    assert tasks[0].resolved_at == datetime.fromisoformat("2026-05-21T10:00:00+00:00")


def test_parse_task_resolved_at_absent_is_none() -> None:
    """Open tasks (no resolved_at) parse to Task.resolved_at == None."""
    result = _content(
        {
            "tasks": [
                {"id": "x", "title": "t", "status": "open"},
            ]
        }
    )
    tasks = _parse_task_list_response(result)
    assert tasks[0].resolved_at is None


def test_parse_task_ignores_legacy_completed_at_key() -> None:
    """Defence in depth: an old Lithos server emitting completed_at
    instead of resolved_at must not crash; the field stays None and the
    projection layer falls back to event.timestamp. (Loom can roll out
    against a still-old server during staging → prod transitions.)"""
    result = _content(
        {
            "tasks": [
                {
                    "id": "x",
                    "title": "t",
                    "status": "completed",
                    "completed_at": "2026-05-21T10:00:00+00:00",
                }
            ]
        }
    )
    tasks = _parse_task_list_response(result)
    assert tasks[0].resolved_at is None


# ── LithosClient.task_status ──────────────────────────────────────────


async def test_lithos_client_task_status_returns_parsed_task() -> None:
    client = LithosClient(base_url="http://example.test:8765")
    fake_session = AsyncMock()
    fake_session.call_tool.return_value = _content(
        {
            "tasks": [
                {
                    "id": "abc",
                    "title": "t",
                    "status": "completed",
                    "claims": [],
                }
            ]
        }
    )
    client._session = fake_session  # type: ignore[assignment]

    task = await client.task_status(task_id="abc")
    assert task is not None
    assert task.id == "abc"
    assert task.status == "completed"
    fake_session.call_tool.assert_awaited_once_with(
        "lithos_task_status", arguments={"task_id": "abc"}
    )


async def test_lithos_client_task_status_returns_none_when_task_not_found() -> None:
    """``task_not_found`` is a routine outcome, not an exception."""
    client = LithosClient(base_url="http://example.test:8765")
    fake_session = AsyncMock()
    fake_session.call_tool.return_value = _content(
        {"status": "error", "code": "task_not_found", "message": "no such task"}
    )
    client._session = fake_session  # type: ignore[assignment]

    assert await client.task_status(task_id="missing") is None


async def test_lithos_client_task_status_propagates_other_errors() -> None:
    client = LithosClient(base_url="http://example.test:8765")
    fake_session = AsyncMock()
    fake_session.call_tool.return_value = _content(
        {"status": "error", "code": "invalid_input", "message": "bad id"}
    )
    client._session = fake_session  # type: ignore[assignment]

    with pytest.raises(LithosClientError) as exc:
        await client.task_status(task_id="x")
    assert exc.value.code == "invalid_input"


# ── task_claim / task_renew / task_release / task_complete / task_update ─


def _client_with_session(response: Any) -> tuple[LithosClient, AsyncMock]:
    client = LithosClient(
        base_url="http://example.test:8765", agent_id="lithos-orchestrator-test"
    )
    fake_session = AsyncMock()
    fake_session.call_tool.return_value = response
    client._session = fake_session  # type: ignore[assignment]
    return client, fake_session


async def test_task_claim_returns_expires_at_and_passes_arguments() -> None:
    client, session = _client_with_session(
        _content({"success": True, "expires_at": "2026-05-13T12:00:00Z"})
    )
    expires = await client.task_claim(task_id="t-1", aspect="impl", ttl_minutes=30)
    assert expires == "2026-05-13T12:00:00Z"
    session.call_tool.assert_awaited_once_with(
        "lithos_task_claim",
        arguments={
            "task_id": "t-1",
            "aspect": "impl",
            "agent": "lithos-orchestrator-test",
            "ttl_minutes": 30,
        },
    )


async def test_task_claim_raises_claim_failed_when_aspect_taken() -> None:
    client, _ = _client_with_session(
        _content({"status": "error", "code": "claim_failed", "message": "aspect taken"})
    )
    with pytest.raises(LithosClientError) as exc:
        await client.task_claim(task_id="t-1", aspect="impl")
    assert exc.value.code == "claim_failed"


async def test_task_renew_returns_new_expires_at() -> None:
    client, _ = _client_with_session(
        _content({"success": True, "new_expires_at": "2026-05-13T13:00:00Z"})
    )
    expires = await client.task_renew(task_id="t-1", aspect="impl", ttl_minutes=15)
    assert expires == "2026-05-13T13:00:00Z"


async def test_task_release_treats_claim_not_found_as_noop() -> None:
    """Routine outcome — a missing claim on release is not an error."""
    client, _ = _client_with_session(
        _content({"status": "error", "code": "claim_not_found", "message": "no claim"})
    )
    # Must not raise.
    await client.task_release(task_id="t-1", aspect="impl")


async def test_task_release_propagates_other_errors() -> None:
    client, _ = _client_with_session(
        _content({"status": "error", "code": "task_not_found", "message": "x"})
    )
    with pytest.raises(LithosClientError):
        await client.task_release(task_id="t-1", aspect="impl")


async def test_task_complete_invokes_correct_tool() -> None:
    client, session = _client_with_session(_content({"success": True}))
    await client.task_complete(task_id="t-1")
    session.call_tool.assert_awaited_once_with(
        "lithos_task_complete",
        arguments={"task_id": "t-1", "agent": "lithos-orchestrator-test"},
    )


async def test_task_cancel_invokes_correct_tool() -> None:
    """``task_cancel(task_id=...)`` with no explicit agent or reason
    sends just ``{task_id, agent: <client default>}`` to the MCP tool."""
    client, session = _client_with_session(_content({"success": True}))
    await client.task_cancel(task_id="t-1")
    session.call_tool.assert_awaited_once_with(
        "lithos_task_cancel",
        arguments={"task_id": "t-1", "agent": "lithos-orchestrator-test"},
    )


async def test_task_cancel_passes_reason_when_provided() -> None:
    """Explicit ``reason`` is forwarded to Lithos so MCP-level logs
    carry the breadcrumb (Lithos doesn't persist it but accepts it)."""
    client, session = _client_with_session(_content({"success": True}))
    await client.task_cancel(task_id="t-1", reason="user request")
    session.call_tool.assert_awaited_once_with(
        "lithos_task_cancel",
        arguments={
            "task_id": "t-1",
            "agent": "lithos-orchestrator-test",
            "reason": "user request",
        },
    )


async def test_task_cancel_omits_reason_when_none() -> None:
    """``reason=None`` (the default) must NOT add a ``"reason": None``
    key — older/strict Lithos servers shouldn't see the field at all.
    Mirrors the ``resolved_since``-omit-when-none pattern in ``task_list``."""
    client, session = _client_with_session(_content({"success": True}))
    await client.task_cancel(task_id="t-1", reason=None)
    args = session.call_tool.await_args.kwargs["arguments"]
    assert "reason" not in args, args


async def test_task_cancel_uses_explicit_agent_over_default() -> None:
    """Explicit ``agent=`` overrides the client's default ``agent_id``."""
    client, session = _client_with_session(_content({"success": True}))
    await client.task_cancel(task_id="t-1", agent="alt-agent")
    args = session.call_tool.await_args.kwargs["arguments"]
    assert args["agent"] == "alt-agent"


async def test_task_cancel_raises_when_no_agent_anywhere() -> None:
    """Client with no ``agent_id`` AND no explicit agent arg → raises."""
    client = LithosClient(base_url="http://example.test:8765")  # no agent_id
    fake_session = AsyncMock()
    client._session = fake_session  # type: ignore[assignment]
    with pytest.raises(LithosClientError, match="agent"):
        await client.task_cancel(task_id="t-1")


async def test_task_update_omits_unset_fields() -> None:
    client, session = _client_with_session(_content({"success": True}))
    await client.task_update(task_id="t-1", tags=["a", "b"])
    session.call_tool.assert_awaited_once_with(
        "lithos_task_update",
        arguments={
            "task_id": "t-1",
            "agent": "lithos-orchestrator-test",
            "tags": ["a", "b"],
        },
    )


async def test_task_update_rejects_empty_call() -> None:
    """Lithos requires at least one of title/description/tags/metadata
    (post-#290 adds metadata to the at-least-one list)."""
    client, _ = _client_with_session(_content({"success": True}))
    with pytest.raises(LithosClientError, match="at least one"):
        await client.task_update(task_id="t-1")


async def test_task_update_passes_metadata_when_provided() -> None:
    """``metadata`` kwarg (Lithos #290) is forwarded as the
    per-key merge patch on the MCP call."""
    client, session = _client_with_session(_content({"success": True}))
    await client.task_update(task_id="t-1", metadata={"priority": "high"})
    session.call_tool.assert_awaited_once_with(
        "lithos_task_update",
        arguments={
            "task_id": "t-1",
            "agent": "lithos-orchestrator-test",
            "metadata": {"priority": "high"},
        },
    )


async def test_task_update_metadata_with_none_value_passes_through() -> None:
    """A ``None`` value inside the metadata dict (Python ``None`` →
    JSON ``null``) is preserved on the wire. Lithos's merge
    semantics interpret null as "delete this key" — the client
    doesn't filter it out."""
    client, session = _client_with_session(_content({"success": True}))
    await client.task_update(task_id="t-1", metadata={"priority": None})
    args = session.call_tool.await_args.kwargs["arguments"]
    assert args["metadata"] == {"priority": None}


async def test_task_update_omits_metadata_arg_when_none() -> None:
    """``metadata=None`` (default) → no ``"metadata"`` key in the MCP
    args. Distinct from ``metadata={}`` (which Lithos treats as a
    no-op patch) or ``metadata={"k": None}`` (delete the key).
    Mirrors the pattern other optional args use."""
    client, session = _client_with_session(_content({"success": True}))
    await client.task_update(task_id="t-1", tags=["x"])  # no metadata
    args = session.call_tool.await_args.kwargs["arguments"]
    assert "metadata" not in args


async def test_task_update_metadata_alone_satisfies_at_least_one() -> None:
    """Per Lithos #290, the at-least-one constraint now accepts
    metadata as the satisfier — title/description/tags can all be
    omitted if metadata is provided."""
    client, session = _client_with_session(_content({"success": True}))
    await client.task_update(task_id="t-1", metadata={"priority": "low"})
    session.call_tool.assert_awaited_once()


async def test_task_lifecycle_methods_require_agent_id() -> None:
    client = LithosClient(base_url="http://example.test:8765")  # no agent_id
    fake_session = AsyncMock()
    client._session = fake_session  # type: ignore[assignment]
    with pytest.raises(LithosClientError, match="agent"):
        await client.task_claim(task_id="t-1", aspect="impl")
    with pytest.raises(LithosClientError, match="agent"):
        await client.task_complete(task_id="t-1")
