"""The Lithos wire types — the frozen records every Loom layer reads.

Split out of :mod:`lithos_loom.lithos_client` (the stop-loss module budget
asked for exactly this extraction) so the data shapes a handler, projection
or test depends on are importable without the MCP session machinery. The
client re-exports them, so ``from lithos_loom.lithos_client import Task``
keeps working; new code may import from here directly.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

__all__ = [
    "BlockedTask",
    "Blocker",
    "Note",
    "NoteSummary",
    "Task",
    "TaskEdge",
    "WriteResult",
    "WriteStatus",
]


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
    resolution-date anchor for ``✅``/``❌`` markers and TTL eviction.

    ``description``, ``created_by``, ``created_at``, and ``outcome``
    were added in lithos#294 (full task record on status + new
    ``lithos_task_get`` tool). They default to falsy values so the
    parser stays backwards-compatible with pre-#294 servers that
    don't return them.

    ``task_type`` (Epic G task-graph extension: ``task`` | ``epic`` |
    ``gate``) is carried on every graph response (``task_ready`` /
    ``task_blocked`` / ``task_children``); it defaults to ``task`` so
    a pre-extension server that omits it still parses.
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
    task_type: str = "task"
    # Server-side last-modified stamp (lithos#415): bumped by any mutation
    # (task_update, cancel/complete) but NOT by finding_post or claim/release
    # (measured 2026-08-29). ``None`` on a pre-#415 server that omits it —
    # consumers (the failed-retry guard, #339) must fall back, not fail.
    updated_at: datetime | None = None


@dataclass(frozen=True)
class Note:
    """A full Lithos KB document as returned by ``lithos_read``.

    Field set carries everything the projection layer needs to render
    a vault file with frontmatter: identity (``id``, ``path``,
    ``slug``), versioning (``version``, ``updated_at``), body, and
    the metadata fields the operator's queries rely on (``status``,
    ``tags``, ``note_type``). ``slug`` is derived server-side from
    the path's first segment under ``projects/`` and exposed here as
    a convenience so callers don't have to re-parse it.

    Frozen + Mapping-typed ``metadata.extra`` so subscription handlers
    can read additional persisted fields without a client plumbing PR
    (mirrors the :class:`Task` design).
    """

    id: str
    title: str
    body: str
    version: int
    updated_at: datetime | None
    tags: tuple[str, ...]
    status: str | None  # active | archived | quarantined | None
    note_type: str | None
    path: str  # e.g. "projects/lithos-loom/context.md"
    slug: str  # derived: first path segment after "projects/"
    metadata: Mapping[str, Any] = field(default_factory=dict)
    """Free-form key/value metadata (Lithos ``extra``). Persisted via
    ``lithos_write(metadata=...)`` and returned by ``lithos_read`` /
    ``lithos_list``. The github-watcher stores its per-project config
    here (repo list, watch flag, exclude filters)."""


@dataclass(frozen=True)
class NoteSummary:
    """Lightweight ``Note`` projection returned by ``lithos_list``.

    Same identity + version + metadata fields as :class:`Note` but
    without the body — `lithos_list` doesn't return content by
    default and pulling it for an enumeration view would be wasteful.
    Use :meth:`LithosClient.note_read` to fetch the full body for a
    specific id once the caller has decided which docs to project.
    """

    id: str
    title: str
    version: int
    updated_at: datetime | None
    tags: tuple[str, ...]
    status: str | None
    note_type: str | None
    path: str
    slug: str
    metadata: Mapping[str, Any] = field(default_factory=dict)
    """Free-form key/value metadata (Lithos ``extra``) — present on
    ``lithos_list`` items, so the github-watcher reads its per-project
    config straight from the enumeration without a follow-up read."""


WriteStatus = Literal[
    "created",
    "updated",
    "duplicate",
    "version_conflict",
    "slug_collision",
    "invalid_input",
    "content_too_large",
    "error",
]
"""All terminal status values ``lithos_write`` can return.

Mirrors the Lithos-side ``WriteOutcome`` enum (``lithos/src/lithos/intake.py``).
``created`` / ``updated`` are success paths; everything else means the
caller has to decide what to do. ``version_conflict`` and
``slug_collision`` are the two cases the bidirectional-sync path
reacts to programmatically.
"""


@dataclass(frozen=True)
class WriteResult:
    """Result envelope from :meth:`LithosClient.note_write`.

    Mirrors Lithos's ``WriteResult`` / ``WriteOutcome`` (see
    ``lithos/src/lithos/knowledge.py`` and ``intake.py``). The handler
    inspects ``.status`` to branch: ``"created"`` / ``"updated"`` are
    success paths; ``"version_conflict"`` carries ``current_version``
    so the caller can re-fetch and resolve; ``"slug_collision"``
    carries ``slug_collision_existing_id`` so the caller can surface
    the conflicting doc to the operator.

    Critically: a version_conflict response does NOT raise — the
    caller MUST check ``.status``. Raising would force every push-back
    site to try/except, masking the intent that a conflict is an
    expected branch of bidirectional sync.
    """

    status: WriteStatus
    note: Note | None = None
    """Set on ``"created"`` / ``"updated"`` (the persisted doc)."""
    current_version: int | None = None
    """Set on ``"version_conflict"`` — the version Lithos actually has,
    which the caller pulls + diffs against to resolve."""
    slug_collision_existing_id: str | None = None
    """Set on ``"slug_collision"`` — the id of the doc that already
    owns this slug, for operator surfacing."""
    message: str | None = None
    """Operator-readable message on non-success outcomes."""
    warnings: tuple[str, ...] = field(default_factory=tuple)


# ── Task-graph types (Epic G) ──────────────────────────────────────────────


@dataclass(frozen=True)
class TaskEdge:
    """One typed relation between two tasks, as returned by
    ``lithos_task_edge_list``.

    ``type`` is the edge type (``blocks`` | ``parent_child`` |
    ``discovered_from`` | ``waits_on_gate``); ``direction`` is relative to
    the queried task (``incoming`` = the queried task is ``to_task_id``,
    ``outgoing`` = it is ``from_task_id``). ``created_by`` / ``created_at``
    are provenance and default to falsy so a lean edge record still parses.
    """

    from_task_id: str
    to_task_id: str
    type: str
    direction: str
    metadata: Mapping[str, Any] = field(default_factory=dict)
    created_by: str = ""
    created_at: datetime | None = None


@dataclass(frozen=True)
class Blocker:
    """Why a task is not ready, as returned inside ``lithos_task_blocked``.

    ``kind`` is the structured reason: ``task`` (predecessor still open —
    just waiting), ``gate`` (waiting on an unresolved gate),
    ``blocker_unsatisfiable`` (predecessor / gate was cancelled — needs
    intervention), or ``cycle`` (the dependency chain forms a cycle).
    ``task_id`` / ``type`` / ``status`` describe the blocking predecessor
    or gate and default to falsy (a ``cycle`` blocker may omit them).
    """

    kind: str
    message: str
    task_id: str = ""
    type: str = ""
    status: str = ""


@dataclass(frozen=True)
class BlockedTask:
    """A not-ready task plus its structured blocker reasons.

    Pairs the full :class:`Task` record with the ``blockers`` list from
    ``lithos_task_blocked`` so callers (the dry-run report, the projection's
    ⛔ markers) render *why* a task is deferred without a second round-trip.
    """

    task: Task
    blockers: tuple[Blocker, ...]
