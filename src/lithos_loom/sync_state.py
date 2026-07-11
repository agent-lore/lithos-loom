"""Concern-scoped coordination state shared inside the ``obsidian-sync`` child.

The child runs the projection writers and the fs/dir watchers in one subprocess.
The projections write vault files; the watchers poll those same files. Without
coordination, every projection write would trip a watcher and emit a spurious
``obsidian.*`` event that a status/note subscription would then echo back to
Lithos — a feedback loop this state is designed to prevent.

Historically this was one 12-field ``ProjectionSyncState`` object handed by
reference to every consumer, even though each consumer only touched one concern's
fields. It is now split into three concern-scoped objects, so each consumer depends
only on the interface it uses (ARCH-10):

* :class:`TaskSyncState` — the ``_lithos/tasks.md`` projection ↔ fs-watcher seam
  (whole-file hash, per-task markers, the write counter).
* :class:`NoteSyncState` — the project-context-doc projection ↔ dir-watcher seam
  (per-doc file/body hashes, versions, projected paths).
* :class:`ArchiveGateState` — the surfaced/archived handshake between the task
  projection and the task-archive subscription, plus the projection's re-flush hook.

The ``obsidian-sync`` child constructs one of each and wires the relevant object(s)
into each handler/source. All three are mutated only on the single asyncio event
loop (no locks needed); the record-before-rename ordering invariant each documents
is what makes a concurrent watcher poll always see coordination state consistent
with the file it just read.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["TaskSyncState", "NoteSyncState", "ArchiveGateState"]


@dataclass
class TaskSyncState:
    """Coordination between the ``_lithos/tasks.md`` projection and the fs watcher.

    The projection updates this *before* committing each write; the fs watcher reads
    it on every poll and short-circuits when the on-disk content matches the
    projection's last known emission. Constructed by the ``obsidian-sync`` child and
    shared by reference with :func:`_obsidian_projection.make_handler` and
    :class:`~lithos_loom.sources.obsidian_fs_watcher.ObsidianFsWatcher`. Not
    thread-safe; mutated only on the event loop.
    """

    last_written_hash: bytes | None = None
    """SHA-256 of the projection's most recent successful write. ``None``
    before the projection has ever written. The fs watcher compares the
    current on-disk hash against this to short-circuit the parse step
    when the file is byte-identical to the projection's last emission."""

    task_status_markers: dict[str, str] = field(default_factory=dict)
    """Per-task ``[ ]/[x]/[-]`` marker the projection most recently
    emitted, keyed by Lithos task id. The fs watcher consults this when
    deciding whether a parsed status came from itself (matches the
    marker → projection-driven, suppress) or a user edit (differs →
    real change, emit ``obsidian.task.status_changed``).

    Tasks dropped from the projection (e.g. completed-and-TTL-expired,
    no-longer-actionable) are removed from this dict so re-additions
    later don't trip on stale markers."""

    task_priority_markers: dict[str, str | None] = field(default_factory=dict)
    """Per-task priority enum (``highest``/``high``/``medium``/
    ``low``/``lowest``) or ``None`` the projection most recently
    emitted, keyed by Lithos task id. Same role for the
    ``obsidian.task.priority_changed`` event as ``task_status_markers``
    plays for ``status_changed``. ``None`` means "task is open but has
    no priority"; a key being absent means the projection has never
    written that task. Resolved tasks are not added here — the renderer
    drops priority on resolved lines, so there's no projection baseline
    to compare against."""

    task_due_date_markers: dict[str, str | None] = field(default_factory=dict)
    """Per-task due date (``YYYY-MM-DD`` string, matching what the
    renderer emits in the ``📅`` marker) or ``None`` the projection
    most recently emitted. Same role for the
    ``obsidian.task.due_date_changed`` event as ``task_status_markers``
    plays for ``status_changed``. ``None`` means "task is open but has
    no due date on the projected line"; a key being absent means the
    projection has never written that task. Resolved tasks are not
    added here — the renderer drops the due date on resolved lines."""

    write_version: int = 0
    """Monotonically incremented on each ``record_projection_write``
    call. The fs watcher snapshots this on every poll; a tick since
    last poll means the projection wrote in the meantime. Lets the
    watcher distinguish two hash-identical scenarios that look the
    same to a naive ``last_written_hash`` compare: (a) the projection
    re-rendered and committed (genuine self-write — suppress, clear
    observed markers) versus (b) the user manually reverted the file
    to whatever the projection had last written (a real user
    transition that must NOT be suppressed). Without this counter the
    flip-then-flip-back case was silently dropped."""

    def record_projection_write(
        self,
        *,
        content_hash: bytes,
        task_status_markers: Mapping[str, str],
        task_priority_markers: Mapping[str, str | None],
        task_due_date_markers: Mapping[str, str | None],
    ) -> None:
        """Capture the post-render state the projection is about to commit.

        Called by the projection's ``_flush`` *before* it commits the
        atomic rename, so any concurrent watcher poll that sees the new
        file content also sees the matching coordination state.

        ``task_status_markers`` / ``task_priority_markers`` /
        ``task_due_date_markers`` are each copied into fresh dicts so
        subsequent mutation of the projection's render-state dicts
        cannot silently change suppression behaviour after this point.

        ``write_version`` increments unconditionally — even
        same-content overwrites bump it, so the watcher's "did
        projection write since last poll" check stays accurate. (In
        practice ``_flush`` short-circuits on hash-match before
        calling this, so the counter only advances when content
        actually changed.)
        """
        self.last_written_hash = content_hash
        self.task_status_markers = dict(task_status_markers)
        self.task_priority_markers = dict(task_priority_markers)
        self.task_due_date_markers = dict(task_due_date_markers)
        self.write_version += 1


@dataclass
class NoteSyncState:
    """Coordination between the project-context-doc projection and the dir watcher.

    Per-doc state (keyed by Lithos doc id) the projection captures before each
    atomic write, and the dir watcher reads on every poll to suppress self-writes
    without mis-classifying a frontmatter-only rewrite as an operator body edit.
    Shared by reference with the project-context projection, the note-push and
    note-conflict handlers (writers), and
    :class:`~lithos_loom.sources.obsidian_dir_watcher.ObsidianDirWatcher` (reader).
    Not thread-safe; mutated only on the event loop.
    """

    note_file_hashes: dict[str, bytes] = field(default_factory=dict)
    """Per-project-context-doc **full-file** hash the projection most
    recently emitted (SHA-256 of the entire rendered output —
    frontmatter + body), keyed by Lithos doc id.

    Two purposes:

    * Projection self-dedup: skip the write if the freshly-rendered
      file would be byte-identical to what we last wrote. Must be
      whole-file (not body-only) because frontmatter fields
      (``lithos_version``, ``status``, ``tags``, ``lithos_updated_at``)
      must mirror Lithos — a version bump with unchanged body MUST still
      rewrite the frontmatter, otherwise the optimistic-lock contract
      for bidirectional sync breaks.
    * Dir-watcher self-write suppression: the watcher computes the
      on-disk hash and compares against this; a match means "the
      projection wrote these exact bytes, suppress as self-write."

    Body-only hash (``compute_body_hash``) is a separate concept used
    by the dir-watcher's body-only diff (operator frontmatter edits
    must not push back to Lithos). It is NOT stored here — the
    dir-watcher computes it on the fly when needed."""

    note_versions: dict[str, int] = field(default_factory=dict)
    """Per-project-context-doc ``lithos_version`` the projection most
    recently wrote into vault frontmatter, keyed by Lithos doc id.

    Written by every note writer for symmetry with the other three maps
    and restored on a write-failure rollback, but note: it has no live
    *reader* today — the note-push handler takes ``expected_version``
    from the on-disk frontmatter the dir-watcher parsed, not from here.
    Kept as the recorded baseline (and to keep the four per-doc maps in
    lock-step) rather than removed."""

    note_body_hashes: dict[str, bytes] = field(default_factory=dict)
    """Per-project-context-doc **body-only** hash (SHA-256 of the
    Markdown body, frontmatter excluded), keyed by Lithos doc id.

    Drives the dir-watcher's body-only diff (frontmatter edits must
    never push back to Lithos). The watcher computes the on-disk body
    hash every poll and compares against this baseline:

    * Match → projection wrote this body (or operator edited only the
      frontmatter); suppress, no push.
    * Mismatch combined with a whole-file hash that does NOT match
      :attr:`note_file_hashes` → real operator body edit; emit
      ``obsidian.note.modified``.

    Distinct from :attr:`note_file_hashes` because the note-push
    round-trip rewrites frontmatter (version bump) without changing
    body, and the projection itself rewrites frontmatter fields like
    ``lithos_updated_at`` on docs the operator didn't touch. Whole-file
    hash alone would mis-classify both as user body edits and trigger
    feedback-loop pushes."""

    note_projected_paths: dict[str, Path] = field(default_factory=dict)
    """Per-project-context-doc **absolute vault path** the projection
    most recently wrote to, keyed by Lithos doc id.

    Required for stale-file cleanup when a note's address changes
    while the projection's view of "where to write next" diverges
    from "where the previous file lives". Three scenarios this
    enables cleanup for, all surfaced by reviewer feedback on PR #37:

    * Path migration within ``projects/``: doc moves from
      ``projects/foo/context.md`` to ``projects/bar/context.md`` —
      unlink the old file before writing the new.
    * Tag removal: doc loses ``project-context`` tag — unlink the
      stale projection (was actionable, now isn't).
    * Path moved out of ``projects/``: doc moves to e.g.
      ``observations/...`` — unlink the stale projection.

    Without the prior path stored here we'd have no way to find the
    old file from the current event payload (Lithos sends the NEW
    path, not the OLD one). Cleared by ``forget_project_context``
    on delete + on cleanup-driven-by-filter-rejection."""

    def record_project_context_write(
        self,
        *,
        doc_id: str,
        file_hash: bytes,
        body_hash: bytes,
        version: int,
        projected_path: Path,
    ) -> None:
        """Capture the post-render state for a single project-context
        doc the projection is about to commit.

        Per-doc state lives in four parallel maps keyed by doc id:
        ``note_file_hashes`` (whole-file hash — used by the projection
        for self-dedup), ``note_body_hashes`` (body-only hash — used
        by the dir-watcher to suppress self-writes without false-positive
        matches against frontmatter-only changes),
        ``note_versions`` (the recorded version baseline) and
        ``note_projected_paths`` (the absolute vault path of the
        current projection — used for stale-file cleanup on path
        migration / tag-removal / out-of-projects-move).

        Called by the project-context projection per doc, before the
        atomic rename — same ordering invariant as
        :meth:`TaskSyncState.record_projection_write` so any concurrent
        dir-watcher poll that sees the new file also sees the matching
        coordination state.

        Unlike the task projection's ``write_version`` (one counter
        shared across all tasks in a single file), per-doc projection
        is naturally file-scoped — re-rendering one doc doesn't
        invalidate the dir-watcher's view of any other doc — so no
        global counter is needed here. The dir-watcher compares
        per-file hash against the per-doc entry directly.
        """
        self.note_file_hashes[doc_id] = file_hash
        self.note_body_hashes[doc_id] = body_hash
        self.note_versions[doc_id] = version
        self.note_projected_paths[doc_id] = projected_path

    def forget_project_context(self, *, doc_id: str) -> None:
        """Drop a doc's projection state (called on
        ``lithos.note.deleted`` after the local file is removed, and
        on filter-rejection-driven cleanup after the stale file is
        unlinked).

        Keeping a stale hash here would cause the dir-watcher to
        suppress a subsequent re-creation of the same doc (e.g. if
        the operator restores it from KB, or re-adds the
        ``project-context`` tag) as a self-write. Idempotent — silent
        no-op when the id isn't tracked. Clears all four parallel
        maps in one shot."""
        self.note_file_hashes.pop(doc_id, None)
        self.note_body_hashes.pop(doc_id, None)
        self.note_versions.pop(doc_id, None)
        self.note_projected_paths.pop(doc_id, None)


@dataclass
class ArchiveGateState:
    """The surfaced/archived handshake between the task projection and the
    task-archive subscription, plus the projection's re-flush hook.

    The task projection sets :attr:`surfaced` when it writes a task's line and reads
    :attr:`archived` for its flush-time eviction predicate; the task-archive
    subscription reads :attr:`surfaced` as its gate and sets :attr:`archived` after a
    durable append, then asks the projection to re-flush via :meth:`request_flush`.
    Shared by reference between :func:`_obsidian_projection.make_handler` and
    :func:`_task_archive.make_handler`. Not thread-safe; mutated only on the event
    loop.
    """

    surfaced: dict[str, bool] = field(default_factory=dict)
    """Per-task "was this task ever written into the global
    ``_lithos/tasks.md`` projection" flag, keyed by Lithos task id.
    Set ``True`` by the obsidian-projection handler the moment an open
    actionable task is added to render state, and seeded at init from
    the task ids already on disk in ``tasks.md`` (so tasks visible
    before a restart survive the replay of their ``completed`` event).

    Read by the ``task-archive`` subscription as its surfaced-gate: a
    task that resolves without this flag set was never operator-visible
    (background / route-claimed-only) and is NOT archived. Dropped by
    the archiver after a successful append (memory stays bounded by the
    open-task count).

    Caveat: the surfaced-gate, like every subscription, only sees events
    the bus actually delivered. If the bus drops a terminal event for the
    archiver's queue (back-pressure), that task is never archived — the
    line stays in ``tasks.md`` under the TTL fallback but no done-file
    entry is written. The no-data-loss contract covers archive-write
    *failures* (retry/friction), not bus drops."""

    archived: dict[str, bool] = field(default_factory=dict)
    """Per-task "the task-archive subscription has durably appended this
    task to its per-project ``<slug>-done.md`` file" flag, keyed by
    Lithos task id. Set ``True`` by the archiver only after the O_APPEND
    write succeeds (or when a replayed task is found already on disk).

    Read by the obsidian-projection's flush-time eviction predicate:
    an archived resolved task is evicted from ``tasks.md`` immediately,
    rather than lingering for ``resolved_ttl_days``. The TTL remains a
    fallback so an un-archived resolved task (archive write failed, or
    the archiver isn't configured) still drops eventually — no permanent
    linger, no regression when task-archive is disabled.

    Unlike ``surfaced``, this map is NOT pruned: the projection's
    eviction may consult it on any later flush (a duplicate terminal
    event can re-add an evicted task to render state), so entries persist
    for the process lifetime. Growth is bounded by the count of distinct
    tasks resolved during the session — negligible for the daemon's
    throughput; revisit only if a soak surfaces real memory pressure."""

    _flush_hook: Callable[[], Awaitable[None]] | None = field(default=None, repr=False)
    """The projection's debounced flush-scheduler, installed via
    :meth:`install_flush_hook`. Private — callers go through
    :meth:`request_flush` so the "no projection wired → no-op" case is handled
    in one place instead of at every call site."""

    def install_flush_hook(self, hook: Callable[[], Awaitable[None]]) -> None:
        """Register the projection's (debounced) flush-scheduler.

        Called by the projection's ``make_handler`` so a sibling handler can ask
        it to (re-)flush ``tasks.md``. Exactly one projection installs the hook;
        it stays ``None`` when no projection is wired."""
        self._flush_hook = hook

    async def request_flush(self) -> None:
        """Ask the projection to (re-)flush ``tasks.md``, if a projection is wired.

        The task-archive subscription calls this *after* it sets
        ``archived[id]``, so the resulting flush is guaranteed to see the flag and
        evict the line — making eviction causally follow archiving instead of
        relying on the archiver winning a race against the projection's own
        debounce timer. (Both handlers share one event loop; the archiver's append
        is synchronous, so the projection usually evicts on its own scheduled
        flush, but under a backlog or a slow disk the archiver can finish after
        that flush has already run — this hook closes that window.)

        A no-op when no projection is wired (no hook installed) — the archiver
        runs standalone in that case."""
        if self._flush_hook is not None:
            await self._flush_hook()
