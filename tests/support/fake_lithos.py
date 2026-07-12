"""One shared in-memory ``LithosClient`` fake for tests (ARCH-4).

Replaces the ~10 hand-rolled ``_Fake``/``_Stub`` doubles that each defined the
client surface by example. ``FakeLithosClient`` is a drop-in for the concrete
``LithosClient``: same constructor (``base_url``, ``agent_id``), an async
context manager, and the full task/note/finding method surface backed by
in-memory dicts. It records every call (``calls`` / :meth:`calls_to` /
:meth:`called` / :attr:`mutating_calls`) and supports injected failures
(:attr:`fail_connect` for the ``async with`` entry, :attr:`raise_on` per
method), so a test seeds state + asserts calls without a live Lithos.

Two patch styles both work:

* pass the fake directly to a function that takes a client arg, or
* ``monkeypatch.setattr(mod, "LithosClient", lambda *a, **k: fake)`` so the
  production ``async with LithosClient(url) as client`` yields the seeded fake.

It structurally satisfies :class:`~lithos_loom.lithos_client.LithosClientProtocol`
(pyright-checked below), so a test can type it as any role Protocol.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from lithos_loom.errors import LithosClientError
from lithos_loom.lithos_client import (
    BlockedTask,
    Blocker,
    Note,
    NoteSummary,
    Task,
    TaskEdge,
    WriteResult,
)

# Deterministic resolution timestamp for completed/cancelled tasks — a fixed
# value keeps ``resolved_at`` assertions stable (no wall-clock in the fake).
RESOLVED_AT = datetime(2024, 1, 1, tzinfo=UTC)

# Edge types that gate readiness (a ``blocks`` predecessor / an unresolved
# gate). ``parent_child`` / ``discovered_from`` are purely structural.
_BLOCKING_EDGE_TYPES = frozenset({"blocks", "waits_on_gate"})
# Task types that are never offered as ready work and never counted as blocked
# frontier work (they are structural / external waits).
_NON_WORK_TASK_TYPES = frozenset({"gate", "epic"})

_MUTATING = frozenset(
    {
        "task_create",
        "task_update",
        "task_complete",
        "task_cancel",
        "task_claim",
        "task_renew",
        "task_release",
        "task_edge_upsert",
        "task_spawn",
        "note_write",
        "note_delete",
        "finding_post",
    }
)


@dataclass(frozen=True)
class _Edge:
    """One in-memory task edge. ``direction`` is derived per query, so the
    store keeps only the canonical from/to/type/metadata + creator."""

    from_task_id: str
    to_task_id: str
    type: str
    metadata: dict[str, Any]
    created_by: str


@dataclass(frozen=True)
class Call:
    """One recorded method invocation: the method name + its keyword args."""

    method: str
    kwargs: dict[str, Any]


def make_task(
    task_id: str,
    *,
    title: str = "",
    status: str = "open",
    tags: tuple[str, ...] = (),
    metadata: dict[str, Any] | None = None,
    claims: tuple[dict[str, Any], ...] = (),
    resolved_at: datetime | None = None,
    description: str | None = None,
    task_type: str = "task",
) -> Task:
    """Build a :class:`Task` with test-friendly defaults."""
    return Task(
        id=task_id,
        title=title or task_id,
        status=status,
        tags=tags,
        metadata=metadata or {},
        claims=claims,
        resolved_at=resolved_at,
        description=description,
        task_type=task_type,
    )


def make_note(
    note_id: str,
    *,
    title: str = "",
    body: str = "",
    version: int = 1,
    tags: tuple[str, ...] = (),
    status: str | None = "active",
    note_type: str | None = "concept",
    path: str | None = None,
    slug: str = "",
    metadata: dict[str, Any] | None = None,
    updated_at: datetime | None = None,
) -> Note:
    """Build a :class:`Note` with test-friendly defaults."""
    return Note(
        id=note_id,
        title=title or note_id,
        body=body,
        version=version,
        updated_at=updated_at,
        tags=tags,
        status=status,
        note_type=note_type,
        path=path if path is not None else f"projects/{slug or note_id}/{note_id}.md",
        slug=slug,
        metadata=metadata or {},
    )


def _summary_of(note: Note) -> NoteSummary:
    return NoteSummary(
        id=note.id,
        title=note.title,
        version=note.version,
        updated_at=note.updated_at,
        tags=note.tags,
        status=note.status,
        note_type=note.note_type,
        path=note.path,
        slug=note.slug,
        metadata=note.metadata,
    )


class FakeLithosClient:
    """In-memory stand-in for :class:`~lithos_loom.lithos_client.LithosClient`."""

    def __init__(
        self,
        base_url: str = "",
        *,
        agent_id: str | None = None,
        tasks: Sequence[Task] = (),
        notes: Sequence[Note] = (),
        fail_connect: BaseException | None = None,
    ) -> None:
        self.base_url = base_url
        self.agent_id = agent_id
        self._tasks: dict[str, Task] = {t.id: t for t in tasks}
        self._notes: dict[str, Note] = {n.id: n for n in notes}
        self._edges: list[_Edge] = []
        self._findings: list[dict[str, Any]] = []
        #: raise the mapped exception when the named method is called
        self.raise_on: dict[str, BaseException] = {}
        #: raise on ``async with`` entry (transport-failure path)
        self.fail_connect = fail_connect
        #: every call in order
        self.calls: list[Call] = []
        self._id_seq = 0

    # ── seeding (post-construction) ────────────────────────────────────
    def add_task(self, task: Task) -> Task:
        """Seed (or replace) a task in the store after construction."""
        self._tasks[task.id] = task
        return task

    def add_note(self, note: Note) -> Note:
        """Seed (or replace) a note in the store after construction."""
        self._notes[note.id] = note
        return note

    def add_edge(
        self,
        *,
        from_task_id: str,
        to_task_id: str,
        type: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Seed an edge WITHOUT validation (bypasses the self-edge / missing-
        task / cycle checks that :meth:`task_edge_upsert` enforces). Use this
        to inject an otherwise-unbuildable shape — e.g. a dependency cycle —
        so :meth:`task_blocked` can be pinned against the ``cycle`` reason."""
        self._add_edge(from_task_id, to_task_id, type, metadata or {}, validate=False)

    # ── call recording / inspection ────────────────────────────────────
    def _record(self, method: str, **kwargs: Any) -> None:
        self.calls.append(Call(method, kwargs))
        exc = self.raise_on.get(method)
        if exc is not None:
            raise exc

    def calls_to(self, method: str) -> list[dict[str, Any]]:
        """The kwargs of every recorded call to *method*, in order."""
        return [c.kwargs for c in self.calls if c.method == method]

    def called(self, method: str) -> bool:
        """Whether *method* was called at least once."""
        return any(c.method == method for c in self.calls)

    @property
    def mutating_calls(self) -> list[str]:
        """Names of every state-changing call (for dry-run assertions)."""
        return [c.method for c in self.calls if c.method in _MUTATING]

    def _mint(self, prefix: str) -> str:
        self._id_seq += 1
        return f"{prefix}-{self._id_seq}"

    # ── async context manager ──────────────────────────────────────────
    async def __aenter__(self) -> FakeLithosClient:
        if self.fail_connect is not None:
            raise self.fail_connect
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    # ── task surface ───────────────────────────────────────────────────
    async def task_list(
        self,
        *,
        status: str | None = None,
        with_claims: bool = False,
        resolved_since: datetime | None = None,
    ) -> list[Task]:
        self._record(
            "task_list",
            status=status,
            with_claims=with_claims,
            resolved_since=resolved_since,
        )
        tasks = list(self._tasks.values())
        if status is not None:
            tasks = [t for t in tasks if t.status == status]
        if resolved_since is not None:
            tasks = [
                t
                for t in tasks
                if t.resolved_at is not None and t.resolved_at >= resolved_since
            ]
        return tasks

    async def task_get(self, *, task_id: str) -> Task | None:
        self._record("task_get", task_id=task_id)
        return self._tasks.get(task_id)

    async def task_status(self, *, task_id: str) -> Task | None:
        self._record("task_status", task_id=task_id)
        return self._tasks.get(task_id)

    async def task_create(
        self,
        *,
        title: str,
        agent: str | None = None,
        description: str | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        task_type: str | None = None,
        parent_task_id: str | None = None,
        depends_on: list[str] | None = None,
    ) -> str:
        self._record(
            "task_create",
            title=title,
            agent=agent,
            description=description,
            tags=tags,
            metadata=metadata,
            task_type=task_type,
            parent_task_id=parent_task_id,
            depends_on=depends_on,
        )
        task_id = self._mint("task")
        self._tasks[task_id] = make_task(
            task_id,
            title=title,
            tags=tuple(tags or ()),
            metadata=dict(metadata or {}),
            description=description,
            task_type=task_type or "task",
        )
        # depends_on predecessors become blocks edges (predecessor -> this);
        # a parent becomes a parent_child edge (parent -> this).
        for predecessor in depends_on or []:
            self._add_edge(predecessor, task_id, "blocks", {}, validate=False)
        if parent_task_id is not None:
            self._add_edge(parent_task_id, task_id, "parent_child", {}, validate=False)
        return task_id

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
        self._record(
            "task_update",
            task_id=task_id,
            agent=agent,
            title=title,
            description=description,
            tags=tags,
            metadata=metadata,
        )
        existing = self._tasks.get(task_id)
        if existing is None:
            return
        changes: dict[str, Any] = {}
        if title is not None:
            changes["title"] = title
        if description is not None:
            changes["description"] = description
        if tags is not None:
            changes["tags"] = tuple(tags)
        if metadata is not None:
            # Lithos task_update metadata is an additive per-key merge; `{}` is a
            # no-op there (unlike note_write). _merge_metadata gives both.
            changes["metadata"] = _merge_metadata(existing.metadata, metadata)
        self._tasks[task_id] = dataclasses.replace(existing, **changes)

    async def task_complete(
        self, *, task_id: str, agent: str | None = None
    ) -> list[str]:
        self._record("task_complete", task_id=task_id, agent=agent)
        self._resolve(task_id, "completed")
        # Newly-unblocked = each dependent of a resolved blocking edge that is
        # now ready (mirrors Lithos's ``unblocked`` set). Order = edge order.
        unblocked: list[str] = []
        for edge in self._edges:
            if edge.from_task_id != task_id or edge.type not in _BLOCKING_EDGE_TYPES:
                continue
            dependent = self._tasks.get(edge.to_task_id)
            if (
                dependent is not None
                and dependent.status == "open"
                and not self._blockers_for(edge.to_task_id)
                and edge.to_task_id not in unblocked
            ):
                unblocked.append(edge.to_task_id)
        return unblocked

    async def task_cancel(
        self, *, task_id: str, agent: str | None = None, reason: str | None = None
    ) -> None:
        self._record("task_cancel", task_id=task_id, agent=agent, reason=reason)
        self._resolve(task_id, "cancelled")

    def _resolve(self, task_id: str, status: str) -> None:
        existing = self._tasks.get(task_id)
        if existing is not None:
            self._tasks[task_id] = dataclasses.replace(
                existing, status=status, resolved_at=RESOLVED_AT
            )

    async def task_claim(
        self,
        *,
        task_id: str,
        aspect: str,
        ttl_minutes: int = 60,
        agent: str | None = None,
    ) -> str:
        self._record(
            "task_claim",
            task_id=task_id,
            aspect=aspect,
            ttl_minutes=ttl_minutes,
            agent=agent,
        )
        return self._mint("receipt")

    async def task_renew(
        self,
        *,
        task_id: str,
        aspect: str,
        ttl_minutes: int = 60,
        agent: str | None = None,
    ) -> str:
        self._record(
            "task_renew",
            task_id=task_id,
            aspect=aspect,
            ttl_minutes=ttl_minutes,
            agent=agent,
        )
        return self._mint("receipt")

    async def task_release(
        self, *, task_id: str, aspect: str, agent: str | None = None
    ) -> None:
        self._record("task_release", task_id=task_id, aspect=aspect, agent=agent)

    # ── task-graph surface (Epic G) ────────────────────────────────────
    async def task_edge_upsert(
        self,
        *,
        from_task_id: str,
        to_task_id: str,
        type: str,
        agent: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self._record(
            "task_edge_upsert",
            from_task_id=from_task_id,
            to_task_id=to_task_id,
            type=type,
            agent=agent,
            metadata=metadata,
        )
        self._add_edge(
            from_task_id, to_task_id, type, dict(metadata or {}), validate=True
        )

    async def task_edge_list(
        self,
        *,
        task_id: str,
        direction: str = "both",
        types: list[str] | None = None,
    ) -> list[TaskEdge]:
        self._record(
            "task_edge_list", task_id=task_id, direction=direction, types=types
        )
        out: list[TaskEdge] = []
        for edge in self._edges:
            if edge.from_task_id == task_id:
                edge_direction = "outgoing"
            elif edge.to_task_id == task_id:
                edge_direction = "incoming"
            else:
                continue
            if direction != "both" and edge_direction != direction:
                continue
            if types is not None and edge.type not in types:
                continue
            out.append(
                TaskEdge(
                    from_task_id=edge.from_task_id,
                    to_task_id=edge.to_task_id,
                    type=edge.type,
                    direction=edge_direction,
                    metadata=dict(edge.metadata),
                    created_by=edge.created_by,
                )
            )
        return out

    async def task_ready(
        self,
        *,
        project: str | None = None,
        tags: list[str] | None = None,
        metadata_match: dict[str, Any] | None = None,
        limit: int = 50,
        with_claims: bool = True,
    ) -> list[Task]:
        self._record(
            "task_ready",
            project=project,
            tags=tags,
            metadata_match=metadata_match,
            limit=limit,
            with_claims=with_claims,
        )
        ready = [
            task
            for task in self._work_candidates(project, tags, metadata_match)
            if not self._blockers_for(task.id)
        ]
        return ready[:limit]

    async def task_blocked(
        self,
        *,
        project: str | None = None,
        tags: list[str] | None = None,
        metadata_match: dict[str, Any] | None = None,
        limit: int = 50,
    ) -> list[BlockedTask]:
        self._record(
            "task_blocked",
            project=project,
            tags=tags,
            metadata_match=metadata_match,
            limit=limit,
        )
        blocked: list[BlockedTask] = []
        for task in self._work_candidates(project, tags, metadata_match):
            blockers = self._blockers_for(task.id)
            if blockers:
                blocked.append(BlockedTask(task=task, blockers=tuple(blockers)))
        return blocked[:limit]

    async def task_spawn(
        self,
        *,
        source_task_id: str,
        title: str,
        agent: str | None = None,
        description: str | None = None,
        relation_type: str = "discovered_from",
        inherit_project: bool = True,
        inherit_tags: bool = True,
        inherit_context: bool = True,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        self._record(
            "task_spawn",
            source_task_id=source_task_id,
            title=title,
            agent=agent,
            description=description,
            relation_type=relation_type,
            inherit_project=inherit_project,
            inherit_tags=inherit_tags,
            inherit_context=inherit_context,
            metadata=metadata,
        )
        source = self._tasks.get(source_task_id)
        inherited: dict[str, Any] = {}
        if source is not None:
            if inherit_project and "project" in source.metadata:
                inherited["project"] = source.metadata["project"]
            if inherit_context:
                for key in ("priority", "parallelizable", "phase"):
                    if key in source.metadata:
                        inherited[key] = source.metadata[key]
        if metadata:
            inherited.update(metadata)  # explicit keys override inherited ones
        inherited_tags = tuple(source.tags) if (inherit_tags and source) else ()
        task_id = self._mint("task")
        self._tasks[task_id] = make_task(
            task_id,
            title=title,
            tags=inherited_tags,
            metadata=inherited,
            description=description,
        )
        edge_type = "blocks" if relation_type == "blocks" else "discovered_from"
        self._add_edge(source_task_id, task_id, edge_type, {}, validate=False)
        return task_id

    async def task_children(
        self,
        *,
        task_id: str,
        recursive: bool = False,
        include_closed: bool = False,
    ) -> list[Task]:
        self._record(
            "task_children",
            task_id=task_id,
            recursive=recursive,
            include_closed=include_closed,
        )
        result: list[Task] = []
        seen: set[str] = set()
        frontier = self._child_ids(task_id)
        while frontier:
            child_id = frontier.pop(0)
            if child_id in seen:
                continue
            seen.add(child_id)
            child = self._tasks.get(child_id)
            if child is None:
                continue
            if recursive:
                frontier.extend(self._child_ids(child_id))
            if include_closed or child.status == "open":
                result.append(child)
        return result

    # ── graph internals ────────────────────────────────────────────────
    def _child_ids(self, parent_id: str) -> list[str]:
        return [
            edge.to_task_id
            for edge in self._edges
            if edge.type == "parent_child" and edge.from_task_id == parent_id
        ]

    def _add_edge(
        self,
        from_task_id: str,
        to_task_id: str,
        edge_type: str,
        metadata: dict[str, Any],
        *,
        validate: bool,
    ) -> None:
        if validate:
            if from_task_id == to_task_id:
                raise LithosClientError("invalid_input", "self-edge is not allowed")
            if from_task_id not in self._tasks or to_task_id not in self._tasks:
                raise LithosClientError(
                    "task_not_found", "edge references unknown task"
                )
            if edge_type == "blocks" and self._blocks_reaches(to_task_id, from_task_id):
                raise LithosClientError(
                    "edge_cycle", "blocks edge would create a dependency cycle"
                )
        self._edges.append(
            _Edge(
                from_task_id=from_task_id,
                to_task_id=to_task_id,
                type=edge_type,
                metadata=metadata,
                created_by=self.agent_id or "",
            )
        )

    def _work_candidates(
        self,
        project: str | None,
        tags: list[str] | None,
        metadata_match: dict[str, Any] | None,
    ) -> list[Task]:
        """Open, non-gate/epic tasks matching the ready/blocked filter surface."""
        candidates: list[Task] = []
        for task in self._tasks.values():
            if task.status != "open" or task.task_type in _NON_WORK_TASK_TYPES:
                continue
            if project is not None and task.metadata.get("project") != project:
                continue
            if tags is not None and not all(t in task.tags for t in tags):
                continue
            if metadata_match and not _metadata_matches(task.metadata, metadata_match):
                continue
            candidates.append(task)
        return candidates

    def _blockers_for(self, task_id: str) -> list[Blocker]:
        """Structured reasons ``task_id`` is not ready (empty = ready)."""
        if self._blocks_reaches(task_id, task_id):
            return [Blocker(kind="cycle", message="dependency cycle", task_id=task_id)]
        blockers: list[Blocker] = []
        for edge in self._edges:
            if edge.to_task_id != task_id or edge.type not in _BLOCKING_EDGE_TYPES:
                continue
            predecessor = self._tasks.get(edge.from_task_id)
            status = predecessor.status if predecessor is not None else "open"
            if status == "completed":
                continue  # satisfied blocker
            if status == "cancelled":
                kind = "blocker_unsatisfiable"
                message = f"predecessor {edge.from_task_id} was cancelled"
            elif edge.type == "waits_on_gate":
                kind = "gate"
                message = f"waiting on gate {edge.from_task_id}"
            else:
                kind = "task"
                message = f"waiting on predecessor {edge.from_task_id}"
            blockers.append(
                Blocker(
                    kind=kind,
                    message=message,
                    task_id=edge.from_task_id,
                    type=edge.type,
                    status=status,
                )
            )
        return blockers

    def _blocks_reaches(self, start: str, target: str) -> bool:
        """Whether ``target`` is reachable from ``start`` following ``blocks``
        edges forward (``from -> to``). ``start == target`` reachable through
        ≥1 edge means ``start`` sits on a blocks cycle."""
        frontier = [
            edge.to_task_id
            for edge in self._edges
            if edge.type == "blocks" and edge.from_task_id == start
        ]
        seen: set[str] = set()
        while frontier:
            node = frontier.pop()
            if node == target:
                return True
            if node in seen:
                continue
            seen.add(node)
            frontier.extend(
                edge.to_task_id
                for edge in self._edges
                if edge.type == "blocks" and edge.from_task_id == node
            )
        return False

    # ── note surface ───────────────────────────────────────────────────
    async def note_read(
        self, *, id: str | None = None, path: str | None = None
    ) -> Note | None:
        self._record("note_read", id=id, path=path)
        if id is not None:
            return self._notes.get(id)
        if path is not None:
            return next((n for n in self._notes.values() if n.path == path), None)
        return None

    async def note_write(
        self,
        *,
        agent: str | None = None,
        title: str,
        content: str,
        tags: list[str] | None = None,
        note_type: str = "concept",
        path: str | None = None,
        id: str | None = None,
        expected_version: int | None = None,
        status: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> WriteResult:
        self._record(
            "note_write",
            agent=agent,
            title=title,
            content=content,
            tags=tags,
            note_type=note_type,
            path=path,
            id=id,
            expected_version=expected_version,
            status=status,
            metadata=metadata,
        )
        existing = self._notes.get(id) if id is not None else None
        if existing is not None:
            # Update: title/content/note_type are always sent by the real client
            # (note_type defaults to "concept"); tags/path/status/metadata are
            # omitted-when-None, so an omitted field PRESERVES the existing value.
            changes: dict[str, Any] = {
                "title": title,
                "body": content,
                "note_type": note_type or "concept",
                "version": existing.version + 1,
            }
            if tags is not None:
                changes["tags"] = tuple(tags)
            if path is not None:
                changes["path"] = path
            if status is not None:
                changes["status"] = status
            if metadata is not None:
                # note_write treats `{}` as "clear all"; a non-empty dict is a
                # per-key merge (None deletes). `metadata=None` preserves.
                changes["metadata"] = (
                    {}
                    if metadata == {}
                    else _merge_metadata(existing.metadata, metadata)
                )
            note = dataclasses.replace(existing, **changes)
            self._notes[note.id] = note
            return WriteResult(status="updated", note=note)
        note = make_note(
            id or self._mint("note"),
            title=title,
            body=content,
            version=1,
            tags=tuple(tags or ()),
            status=status,
            note_type=note_type or "concept",
            path=path,
            metadata=dict(metadata or {}),
        )
        self._notes[note.id] = note
        return WriteResult(status="created", note=note)

    async def note_list(
        self,
        *,
        path_prefix: str | None = None,
        tags: list[str] | None = None,
        limit: int = 100,
        metadata_match: dict[str, Any] | None = None,
    ) -> list[NoteSummary]:
        self._record(
            "note_list",
            path_prefix=path_prefix,
            tags=tags,
            limit=limit,
            metadata_match=metadata_match,
        )
        notes = list(self._notes.values())
        if path_prefix is not None:
            notes = [n for n in notes if n.path.startswith(path_prefix)]
        if tags:
            notes = [n for n in notes if all(t in n.tags for t in tags)]
        if metadata_match:
            notes = [n for n in notes if _metadata_matches(n.metadata, metadata_match)]
        return [_summary_of(n) for n in notes[:limit]]

    async def note_delete(self, *, id: str, agent: str | None = None) -> bool:
        self._record("note_delete", id=id, agent=agent)
        return self._notes.pop(id, None) is not None

    # ── finding surface ────────────────────────────────────────────────
    async def finding_post(
        self,
        *,
        task_id: str,
        summary: str,
        agent: str | None = None,
        knowledge_id: str | None = None,
    ) -> str | None:
        self._record(
            "finding_post",
            task_id=task_id,
            summary=summary,
            agent=agent,
            knowledge_id=knowledge_id,
        )
        finding_id = self._mint("finding")
        self._findings.append(
            {"id": finding_id, "task_id": task_id, "summary": summary}
        )
        return finding_id

    @property
    def findings(self) -> list[dict[str, Any]]:
        """Every posted finding, in order (``id`` / ``task_id`` / ``summary``)."""
        return list(self._findings)


def _merge_metadata(
    existing: Mapping[str, Any], provided: dict[str, Any]
) -> dict[str, Any]:
    """Lithos additive per-key metadata merge (#290): a key whose value is
    ``None`` deletes it, a non-null value overwrites, and unmentioned keys are
    preserved. An empty ``provided`` is a no-op (yields ``existing`` unchanged) —
    the note_write "``{}`` clears all" case is handled by its caller."""
    merged = dict(existing)
    for key, value in provided.items():
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = value
    return merged


def _metadata_matches(metadata: Any, query: dict[str, Any]) -> bool:
    """Mirror Lithos ``metadata_match``: key equals the query value, or is a
    list containing it (AND across keys)."""
    for key, want in query.items():
        got = metadata.get(key)
        if got == want:
            continue
        if isinstance(got, (list, tuple)) and want in got:
            continue
        return False
    return True


if TYPE_CHECKING:
    from lithos_loom.lithos_client import LithosClient, LithosClientProtocol

    # Both the real client and the fake satisfy the full role Protocol.
    _fake_conforms: LithosClientProtocol = FakeLithosClient()
    _real_conforms: LithosClientProtocol = LithosClient("")
