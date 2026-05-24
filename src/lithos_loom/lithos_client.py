"""Thin async client over the Lithos MCP surface.

The Lithos server speaks MCP over SSE at ``<base_url>/sse`` (FastMCP
default). This module wraps the ``mcp`` SDK's ``sse_client`` +
``ClientSession`` into a lifetime-managed object that exposes only the
calls Loom actually needs.

Slice 0 surface (US3):

* :class:`Task` — frozen dataclass with the fields the poller reads.
* :class:`LithosClient` — async context manager owning an MCP session.
* :meth:`LithosClient.task_list` — minimum ``lithos_task_list`` call.

Story 5 (route-runner subscriber) will extend this with ``task_claim``,
``task_release``, ``task_renew``, ``task_complete``, ``task_update``, and
``finding_post``. Other tools land as they're needed.

Errors returned by Lithos as ``{status: "error", code, message}`` envelopes
surface as :class:`LithosClientError` so callers can switch on
``exc.code``. Unhealthy MCP transport surfaces are left to propagate as the
underlying ``mcp`` SDK exceptions — the daemon's outer loop logs and
continues on transient failures.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from types import TracebackType
from typing import Any

from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.types import CallToolResult

from lithos_loom.errors import LithosClientError

__all__ = ["LithosClient", "Task"]


@dataclass(frozen=True)
class Task:
    """A Lithos task as returned by ``lithos_task_list``,
    ``lithos_task_status``, and ``lithos_task_get`` (lithos#294).

    Field set mirrors the full Lithos task envelope so handlers can
    read any persisted field without a plumbing PR. ``resolved_at`` is
    the canonical Lithos timestamp for terminal-state transitions —
    written by both ``complete_task`` and ``cancel_task`` (lithos#286
    / PR #288, which also renamed it from ``completed_at`` server-side
    with no BC alias). The obsidian-projection handler uses it as the
    resolution-date anchor for ``✅``/``❌`` markers and TTL eviction
    (US13).

    ``description``, ``created_by``, ``created_at``, and ``outcome``
    were added in lithos#294 (full task record on status + new
    ``lithos_task_get`` tool). They default to falsy values so the
    parser stays backwards-compatible with pre-#294 servers that
    don't return them.
    """

    id: str
    title: str
    status: str  # open | completed | cancelled
    tags: tuple[str, ...]
    metadata: Mapping[str, Any]
    claims: tuple[Mapping[str, Any], ...]
    resolved_at: datetime | None = None
    description: str | None = None
    created_by: str = ""
    created_at: datetime | None = None
    outcome: str | None = None


class LithosClient:
    """MCP-over-SSE client for the Lithos server.

    Usage::

        async with LithosClient("http://localhost:8765") as client:
            tasks = await client.task_list(with_claims=True)
    """

    def __init__(self, base_url: str, *, agent_id: str | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.agent_id = agent_id
        self._sse_ctx: Any = None
        self._session_ctx: Any = None
        self._session: ClientSession | None = None

    async def __aenter__(self) -> LithosClient:
        sse_url = f"{self.base_url}/sse"
        self._sse_ctx = sse_client(sse_url)
        read, write = await self._sse_ctx.__aenter__()
        self._session_ctx = ClientSession(read, write)
        session = await self._session_ctx.__aenter__()
        await session.initialize()
        self._session = session
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        try:
            if self._session_ctx is not None:
                await self._session_ctx.__aexit__(exc_type, exc, tb)
        finally:
            self._session_ctx = None
            self._session = None
            if self._sse_ctx is not None:
                try:
                    await self._sse_ctx.__aexit__(exc_type, exc, tb)
                finally:
                    self._sse_ctx = None

    async def _call(self, tool: str, arguments: dict[str, Any]) -> Any:
        """Invoke an MCP tool, decode the FastMCP content envelope, raise on errors.

        Returns the decoded JSON payload (typically a ``dict``). Domain
        errors with a ``{status: "error", code, message}`` envelope raise
        :class:`LithosClientError`; transport-level failures from the
        ``mcp`` SDK propagate untouched.
        """
        if self._session is None:
            raise LithosClientError(
                "client_not_initialised",
                "LithosClient not initialised; use 'async with LithosClient(...) as c'",
            )
        result = await self._session.call_tool(tool, arguments=arguments)
        payload = _payload_from_result(result)
        if isinstance(payload, dict):
            _raise_if_error_envelope(payload)
        return payload

    async def task_list(
        self,
        *,
        status: str | None = None,
        with_claims: bool = False,
        resolved_since: datetime | None = None,
    ) -> list[Task]:
        """Return tasks matching the filters. Omitted ``status`` = all.

        ``resolved_since`` (lithos#286 / PR #288) is converted to an
        ISO-8601 string before passing through; Lithos compares it
        against ``tasks.resolved_at >= ?`` so open tasks (whose
        ``resolved_at`` is NULL) are excluded automatically. Omitting
        the parameter keeps the call wire-identical to the pre-#286
        contract so loom can roll out ahead of a staging Lithos that
        doesn't yet recognise the kwarg.
        """
        if self._session is None:
            raise LithosClientError(
                "client_not_initialised",
                "LithosClient not initialised; use 'async with LithosClient(...) as c'",
            )
        arguments: dict[str, Any] = {"with_claims": with_claims}
        if status is not None:
            arguments["status"] = status
        if resolved_since is not None:
            arguments["resolved_since"] = resolved_since.isoformat()
        result = await self._session.call_tool("lithos_task_list", arguments=arguments)
        return _parse_task_list_response(result)

    async def finding_post(
        self,
        *,
        task_id: str,
        summary: str,
        agent: str | None = None,
        knowledge_id: str | None = None,
    ) -> str | None:
        """Post a finding to a task. Returns the new ``finding_id``.

        ``agent`` defaults to ``self.agent_id`` (set at client construction).
        Subscription handlers typically don't have a meaningful task_id for
        their own friction findings — pass an empty string for the
        ``[Friction]`` posting and Lithos will surface it cluster-wide via
        finding listings rather than per-task.
        """
        if self._session is None:
            raise LithosClientError(
                "client_not_initialised",
                "LithosClient not initialised; use 'async with LithosClient(...) as c'",
            )
        agent_id = agent or self.agent_id
        if not agent_id:
            raise LithosClientError(
                "missing_agent",
                "finding_post needs an agent id; pass agent= or set agent_id",
            )
        if not task_id:
            # Lithos requires a task_id; punt to a logged warning instead
            # of raising, so [Friction] postings without a task scope don't
            # crash the runner. Caller should avoid this when possible.
            return None
        arguments: dict[str, Any] = {
            "task_id": task_id,
            "agent": agent_id,
            "summary": summary,
        }
        if knowledge_id is not None:
            arguments["knowledge_id"] = knowledge_id
        result = await self._session.call_tool(
            "lithos_finding_post", arguments=arguments
        )
        payload = _payload_from_result(result)
        if isinstance(payload, dict):
            _raise_if_error_envelope(payload)
            finding_id = payload.get("finding_id")
            if isinstance(finding_id, str):
                return finding_id
        return None

    async def task_claim(
        self,
        *,
        task_id: str,
        aspect: str,
        ttl_minutes: int = 60,
        agent: str | None = None,
    ) -> str:
        """Claim ``aspect`` of ``task_id``. Returns the claim's ``expires_at``.

        Raises :class:`LithosClientError` with ``code="claim_failed"`` when
        the aspect is already claimed (or the task isn't open). Callers
        treat that as "skip — another runner won the race".
        """
        agent_id = agent or self.agent_id
        if not agent_id:
            raise LithosClientError("missing_agent", "task_claim needs an agent id")
        payload = await self._call(
            "lithos_task_claim",
            {
                "task_id": task_id,
                "aspect": aspect,
                "agent": agent_id,
                "ttl_minutes": ttl_minutes,
            },
        )
        expires = payload.get("expires_at") if isinstance(payload, dict) else None
        if not isinstance(expires, str):
            raise LithosClientError(
                "invalid_response", "task_claim response missing expires_at"
            )
        return expires

    async def task_renew(
        self,
        *,
        task_id: str,
        aspect: str,
        ttl_minutes: int = 60,
        agent: str | None = None,
    ) -> str:
        """Extend an existing claim. Returns the new ``expires_at``."""
        agent_id = agent or self.agent_id
        if not agent_id:
            raise LithosClientError("missing_agent", "task_renew needs an agent id")
        payload = await self._call(
            "lithos_task_renew",
            {
                "task_id": task_id,
                "aspect": aspect,
                "agent": agent_id,
                "ttl_minutes": ttl_minutes,
            },
        )
        expires = payload.get("new_expires_at") if isinstance(payload, dict) else None
        if not isinstance(expires, str):
            raise LithosClientError(
                "invalid_response", "task_renew response missing new_expires_at"
            )
        return expires

    async def task_release(
        self,
        *,
        task_id: str,
        aspect: str,
        agent: str | None = None,
    ) -> None:
        """Release a claim. ``code="claim_not_found"`` is folded into a no-op."""
        agent_id = agent or self.agent_id
        if not agent_id:
            raise LithosClientError("missing_agent", "task_release needs an agent id")
        try:
            await self._call(
                "lithos_task_release",
                {"task_id": task_id, "aspect": aspect, "agent": agent_id},
            )
        except LithosClientError as exc:
            if exc.code == "claim_not_found":
                return
            raise

    async def task_create(
        self,
        *,
        title: str,
        agent: str | None = None,
        description: str | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Create a coordination task. Returns the new task's ``id``.

        ``metadata`` (lithos#295) is a one-shot initial set — there's
        no merge to think about because the task doesn't exist yet.
        Omitted when ``None``, matching :meth:`task_update`'s
        omit-when-default pattern so old/strict Lithos servers don't
        reject an unexpected key.

        Wraps the ``lithos_task_create`` MCP tool's
        ``{task_id: string}`` response shape. Domain errors
        (``invalid_input`` etc.) raise :class:`LithosClientError`.
        """
        agent_id = agent or self.agent_id
        if not agent_id:
            raise LithosClientError("missing_agent", "task_create needs an agent id")
        arguments: dict[str, Any] = {"title": title, "agent": agent_id}
        if description is not None:
            arguments["description"] = description
        if tags is not None:
            arguments["tags"] = tags
        if metadata is not None:
            arguments["metadata"] = metadata
        payload = await self._call("lithos_task_create", arguments)
        task_id = payload.get("task_id") if isinstance(payload, dict) else None
        if not isinstance(task_id, str) or not task_id:
            raise LithosClientError(
                "invalid_response", "task_create response missing task_id"
            )
        return task_id

    async def task_complete(
        self,
        *,
        task_id: str,
        agent: str | None = None,
    ) -> None:
        """Mark a task as completed. Releases all claims as a side effect."""
        agent_id = agent or self.agent_id
        if not agent_id:
            raise LithosClientError("missing_agent", "task_complete needs an agent id")
        await self._call(
            "lithos_task_complete", {"task_id": task_id, "agent": agent_id}
        )

    async def task_cancel(
        self,
        *,
        task_id: str,
        agent: str | None = None,
        reason: str | None = None,
    ) -> None:
        """Cancel a task and release all claims.

        Mirrors :meth:`task_complete` — both terminal transitions
        populate ``tasks.resolved_at`` upstream (lithos#286). ``reason``
        is accepted by the MCP surface but per the Lithos spec is not
        persisted in SQLite; pass a short breadcrumb so the origin
        surfaces in MCP-level logs/traces. Omit it from the MCP
        arguments when ``None`` so old/strict Lithos servers don't
        choke on an explicit-null (mirrors the ``resolved_since``
        pattern from :meth:`task_list`).
        """
        agent_id = agent or self.agent_id
        if not agent_id:
            raise LithosClientError("missing_agent", "task_cancel needs an agent id")
        arguments: dict[str, Any] = {"task_id": task_id, "agent": agent_id}
        if reason is not None:
            arguments["reason"] = reason
        await self._call("lithos_task_cancel", arguments)

    async def task_update(
        self,
        *,
        task_id: str,
        agent: str | None = None,
        title: str | None = None,
        description: str | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Update mutable task fields.

        At least one of ``title`` / ``description`` / ``tags`` /
        ``metadata`` must be provided.

        ``metadata`` (Lithos #290) is applied as an **additive per-key
        merge**: keys with non-null values overwrite, keys with the
        literal Python ``None`` (JSON ``null``) delete the existing
        key, and keys not mentioned are preserved. ``metadata={}``
        passes through to Lithos as a no-op; if you want to skip
        sending metadata at all, leave the kwarg at its default
        ``None``.
        """
        if title is None and description is None and tags is None and metadata is None:
            raise LithosClientError(
                "invalid_input",
                "task_update requires at least one of title/description/tags/metadata",
            )
        agent_id = agent or self.agent_id
        if not agent_id:
            raise LithosClientError("missing_agent", "task_update needs an agent id")
        arguments: dict[str, Any] = {"task_id": task_id, "agent": agent_id}
        if title is not None:
            arguments["title"] = title
        if description is not None:
            arguments["description"] = description
        if tags is not None:
            arguments["tags"] = tags
        if metadata is not None:
            arguments["metadata"] = metadata
        await self._call("lithos_task_update", arguments)

    async def task_status(self, *, task_id: str) -> Task | None:
        """Return the full record of a single task, including its
        active claims.

        Post-lithos#294 the response envelope is the full task record
        (``id, title, description, status, created_by, created_at,
        resolved_at, tags, metadata, outcome``) wrapped in a single-
        element ``tasks`` list, plus the embedded ``claims`` array.
        Returns ``None`` when Lithos reports the task as not found
        (the historical ``{tasks: []}`` shape).

        Prefer :meth:`task_get` when you don't need claims — it
        returns the same record without the list wrapper or the
        claim serialization cost, and uses an explicit
        ``task_not_found`` error envelope instead of an empty list.
        """
        if self._session is None:
            raise LithosClientError(
                "client_not_initialised",
                "LithosClient not initialised; use 'async with LithosClient(...) as c'",
            )
        result = await self._session.call_tool(
            "lithos_task_status", arguments={"task_id": task_id}
        )
        try:
            tasks = _parse_task_list_response(result)
        except LithosClientError as exc:
            if exc.code == "task_not_found":
                return None
            raise
        return tasks[0] if tasks else None

    async def task_get(self, *, task_id: str) -> Task | None:
        """Return the full record of a single task without its claims.

        Added in lithos#294 as the lightweight counterpart to
        :meth:`task_status`: same task envelope, no claims, single-
        object response shape (``{task: {...}}``), and an explicit
        ``task_not_found`` error envelope on miss (mapped here to
        ``None`` to match the :meth:`task_status` convention).

        Use this for pre-checks where only the persisted task fields
        matter — dependency resolution, idempotency gates,
        ``metadata.priority`` comparisons — and reserve
        :meth:`task_status` for callers that need claims.
        """
        if self._session is None:
            raise LithosClientError(
                "client_not_initialised",
                "LithosClient not initialised; use 'async with LithosClient(...) as c'",
            )
        result = await self._session.call_tool(
            "lithos_task_get", arguments={"task_id": task_id}
        )
        try:
            return _parse_task_get_response(result)
        except LithosClientError as exc:
            if exc.code == "task_not_found":
                return None
            raise


# ── Pure parse helpers (heavily unit-tested) ───────────────────────────


def _parse_task_list_response(result: CallToolResult) -> list[Task]:
    payload = _payload_from_result(result)
    if not isinstance(payload, dict):
        raise LithosClientError(
            "invalid_response",
            f"expected dict response, got {type(payload).__name__}",
        )
    _raise_if_error_envelope(payload)
    if "tasks" not in payload:
        raise LithosClientError(
            "invalid_response", "missing 'tasks' key in lithos_task_list response"
        )
    raw_tasks = payload["tasks"]
    if not isinstance(raw_tasks, list):
        raise LithosClientError(
            "invalid_response", "'tasks' must be a list in lithos_task_list response"
        )
    return [_parse_task(t) for t in raw_tasks]


def _parse_task(raw: Any) -> Task:
    if not isinstance(raw, dict):
        raise LithosClientError(
            "invalid_response", f"task entry must be a dict, got {type(raw).__name__}"
        )
    try:
        tags_raw = raw.get("tags") or []
        claims_raw = raw.get("claims") or []
        description_raw = raw.get("description")
        outcome_raw = raw.get("outcome")
        return Task(
            id=str(raw["id"]),
            title=str(raw["title"]),
            status=str(raw["status"]),
            tags=tuple(tags_raw),
            metadata=dict(raw.get("metadata") or {}),
            claims=tuple(dict(c) for c in claims_raw),
            resolved_at=_parse_iso_datetime(raw.get("resolved_at")),
            description=str(description_raw) if description_raw is not None else None,
            created_by=str(raw.get("created_by") or ""),
            created_at=_parse_iso_datetime(raw.get("created_at")),
            outcome=str(outcome_raw) if outcome_raw is not None else None,
        )
    except KeyError as exc:
        raise LithosClientError(
            "invalid_response", f"task entry missing required field: {exc.args[0]}"
        ) from exc


def _parse_task_get_response(result: CallToolResult) -> Task:
    """Parse the ``lithos_task_get`` single-object envelope (lithos#294).

    Shape: ``{"task": {...}}`` on success;
    ``{"status": "error", "code": "task_not_found", ...}`` when the
    task doesn't exist (handled here by re-raising — the caller
    method maps it to ``None``).
    """
    payload = _payload_from_result(result)
    if not isinstance(payload, dict):
        raise LithosClientError(
            "invalid_response",
            f"expected dict response, got {type(payload).__name__}",
        )
    _raise_if_error_envelope(payload)
    if "task" not in payload:
        raise LithosClientError(
            "invalid_response", "missing 'task' key in lithos_task_get response"
        )
    return _parse_task(payload["task"])


def _parse_iso_datetime(value: Any) -> datetime | None:
    """Best-effort ISO-8601 datetime parse. Returns ``None`` for absent
    or unparseable values so a Lithos schema drift on optional fields
    doesn't crash the client (US13 only needs ``resolved_at`` for
    terminal-state rendering; missing values fall back to the bus event
    timestamp at the projection layer)."""
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _payload_from_result(result: CallToolResult) -> Any:
    """Extract the JSON payload from a FastMCP-shaped ``CallToolResult``.

    FastMCP wraps tool returns in a list of content blocks; the first
    text block carries the JSON-serialised return value. ``isError=True``
    surfaces as an exception regardless of payload shape.
    """
    if not result.content:
        if result.isError:
            raise LithosClientError("tool_error", "tool returned isError with no body")
        raise LithosClientError("invalid_response", "tool returned empty content list")
    block = result.content[0]
    text = getattr(block, "text", None)
    if not isinstance(text, str):
        raise LithosClientError(
            "invalid_response",
            f"first content block has no text payload (type={type(block).__name__})",
        )
    if result.isError:
        raise LithosClientError("tool_error", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise LithosClientError(
            "invalid_response", f"tool returned non-JSON text: {exc}"
        ) from exc


def _raise_if_error_envelope(payload: Mapping[str, Any]) -> None:
    """Raise ``LithosClientError`` if ``payload`` is an error envelope."""
    if payload.get("status") != "error":
        return
    raise LithosClientError(
        code=str(payload.get("code") or "error"),
        message=str(payload.get("message") or ""),
    )
