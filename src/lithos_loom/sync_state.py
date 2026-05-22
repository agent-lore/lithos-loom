"""Coordination state shared between the obsidian-projection writer and
the obsidian-fs-watcher source (Slice 2 US23).

The fs watcher and projection live in the same subprocess (the
``obsidian-sync`` child). The projection writes ``_lithos/tasks.md``;
the watcher polls the same file. Without coordination, every projection
write would trip the watcher and emit a spurious
``obsidian.task.status_changed`` event that the status-transition
subscription would then echo back to Lithos — the feedback loop US23
explicitly forbids.

This module is the coordination seam: a single :class:`ProjectionSyncState`
instance is constructed by the child and handed to both sides. The
projection updates it *before* committing each write; the watcher reads
it on every poll and short-circuits when the on-disk content matches the
projection's last known emission.

Two pieces of state matter:

* ``last_written_hash`` — SHA-256 of the projection's most recent
  successful write. Lets the watcher cheaply skip the parse step when
  the file content is byte-identical to what the projection just wrote
  (the common case immediately after any Lithos event).
* ``task_status_markers`` — per-task ``[ ]/[x]/[-]`` checkbox marker
  the projection most recently emitted. Lets the watcher distinguish
  user edits from projection-driven status changes on a per-task basis
  when the file content does differ (e.g. user edited an unrelated
  line, projection added a new task, etc.).

Both updates happen in :meth:`ProjectionSyncState.record_projection_write`
before the projection commits its atomic rename, so a watcher poll that
sees the new file always sees consistent state for it. Single-threaded
asyncio (no locks needed).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

__all__ = ["ProjectionSyncState"]


@dataclass
class ProjectionSyncState:
    """In-process coordination state between projection writer and fs watcher.

    Constructed by the ``obsidian-sync`` child and shared by reference
    with both the :func:`~lithos_loom.subscriptions._obsidian_projection.make_handler`
    handler and the :class:`~lithos_loom.sources.obsidian_fs_watcher.ObsidianFsWatcher`
    source. Not thread-safe; mutated only on the event loop.
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

    def record_projection_write(
        self,
        *,
        content_hash: bytes,
        task_status_markers: Mapping[str, str],
    ) -> None:
        """Capture the post-render state the projection is about to commit.

        Called by the projection's ``_flush`` *before* it commits the
        atomic rename, so any concurrent watcher poll that sees the new
        file content also sees the matching coordination state.

        ``task_status_markers`` is copied into a fresh dict so subsequent
        mutation of the projection's render-state dict cannot silently
        change suppression behaviour after this point.
        """
        self.last_written_hash = content_hash
        self.task_status_markers = dict(task_status_markers)
