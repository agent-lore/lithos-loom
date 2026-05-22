"""ObsidianFsWatcher — polling source for vault edits to ``_lithos/tasks.md``
(Slice 2 US16 + US23).

Watches a single projected file, polls its SHA-256 at
``poll_interval_seconds``, parses per-task ``[ ]/[x]/[-]`` markers,
compares against the projection's last known emission via
:class:`~lithos_loom.sync_state.ProjectionSyncState`, and publishes
``obsidian.task.status_changed`` events for tasks whose marker flipped
under user editing.

Why polling instead of ``watchdog``:

* Single file, human-scale edit cadence, 250ms latency budget.
* Polling is fully asyncio-native (``watchdog`` uses an OS-notify
  thread that we'd have to bridge to the event loop).
* No new runtime dependency.
* Deterministic tests — ``poll_once()`` is callable directly without
  installing OS-level fs handlers.

When Slice 5 needs to watch the multi-file ``_lithos/projects/<slug>/``
tree we may revisit; the architecture (source publishes events; sync
state coordinates self-writes) is the same regardless of mechanism.

US23 self-write suppression has two layers, cheapest-first:

1. **Unchanged hash.** ``current_hash == self._last_seen_hash`` →
   no edits since last poll → return without parsing.
2. **Projection self-write.** ``current_hash ==
   self.sync_state.last_written_hash`` → the projection committed this
   exact content → update ``_last_seen_hash`` and return without
   emitting. The projection updates ``sync_state.last_written_hash``
   *before* committing the atomic rename (see
   :meth:`ProjectionSyncState.record_projection_write`), so any poll
   that sees the new file always sees the matching coordination
   state.
3. **Per-task suppression.** When the file changed AND it wasn't a
   self-write, parse the lines and emit
   ``obsidian.task.status_changed`` for each task whose parsed marker
   differs from ``sync_state.task_status_markers[task_id]``. Tasks
   the projection has never written (``projection_marker is None``)
   are ignored — the capture-macro path that introduces those is
   Slice 3.

Event payload shape::

    {
        "task_id": "abc123",
        "prior": "[ ]",
        "new":   "[x]",
    }

``prior`` and ``new`` are the literal three-character checkbox forms
the projection emitted / the user typed, not their interpreted status
strings. Downstream subscriptions own the mapping
(``[x]`` → complete, ``[-]`` → cancel, ``[/]`` / ``[>]`` → no-op,
``[x]/[-]`` → ``[ ]`` → reopen request).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any

from lithos_loom.bus import Event, EventBus
from lithos_loom.sync_state import ProjectionSyncState

__all__ = ["ObsidianFsWatcher", "VALID_STATUS_MARKERS"]

logger = logging.getLogger(__name__)


VALID_STATUS_MARKERS: frozenset[str] = frozenset({"[ ]", "[x]", "[-]", "[/]", "[>]"})
"""Checkbox markers recognised by the user story (US16 enum).

``[ ]`` open · ``[x]`` completed · ``[-]`` cancelled · ``[/]`` in
progress · ``[>]`` rescheduled. The watcher emits events for any
flip among these; the status-transition subscription decides which
ones map to Lithos calls (US17–US20)."""


# `- [<m>] ...` where <m> is exactly one character (PRD line shapes
# use single-char markers; the regex deliberately rejects multi-char
# weirdness rather than guessing).
_LINE_RE = re.compile(r"^- \[(?P<marker>.)\] ")
_TASK_ID_RE = re.compile(r"🆔 lithos:(?P<task_id>[A-Za-z0-9_-]+)")


@dataclass
class ObsidianFsWatcher:
    """Polling-based filesystem source for the projected tasks file.

    Constructed by the ``obsidian-sync`` child with a bus + a shared
    :class:`ProjectionSyncState` instance also handed to the
    projection. ``run()`` loops forever; cancel the task to stop.
    """

    bus: EventBus
    tasks_path: Path
    sync_state: ProjectionSyncState
    poll_interval_seconds: float = 0.25
    _now_provider: Any = field(default=lambda: datetime.now(UTC))
    """Wall-clock seam for tests so emitted event timestamps are
    deterministic. Production callers leave at the default."""

    def __post_init__(self) -> None:
        # Seeded by the first poll (or by run()'s init read). Tracking
        # the last hash we processed lets the cheap unchanged-since-last-
        # poll path short-circuit before consulting sync_state or
        # parsing.
        self._last_seen_hash: bytes | None = None
        # Per-task marker memory layered on top of
        # ``sync_state.task_status_markers``. The sync_state map only
        # advances on projection writes — without local memory of what
        # we've already observed and emitted for, a user edit followed
        # by any subsequent file save (unrelated whitespace change,
        # edit to another line) would re-trigger layer 3 with the same
        # ``[ ] → [x]`` diff and re-emit the same transition. The
        # local map records the marker we last saw the user *commit* to
        # disk; emission compares against (and updates) this map so we
        # publish at most one event per actual transition. Cleared on
        # projection self-write — the projection's re-rendered file is
        # authoritative over any user edits sitting on top of it.
        self._observed_markers: dict[str, str] = {}
        # Snapshot of ``sync_state.write_version`` from our last poll.
        # If it's advanced, the projection has committed a re-render
        # since we last looked. We use that signal to distinguish
        # genuine projection self-writes (file matches new
        # ``last_written_hash`` AND version advanced → suppress) from
        # user reverts that happen to match the projection's last
        # written content (version unchanged → real user transition,
        # let layer 3 emit). Without this, a flip-then-flip-back was
        # silently absorbed by the layer-2 hash compare.
        self._last_processed_write_version: int = 0

    async def run(self) -> None:
        """Poll forever. Cancellable.

        Seeds ``_last_seen_hash`` from
        ``sync_state.last_written_hash`` — i.e. what the projection
        believes is on disk — rather than re-reading disk directly.
        That closes a small startup-race window: if a user edited the
        file in the gap between projection-seed and watcher-start,
        seeding from current disk content would silently swallow that
        edit (initial hash matches the user's edited content, no
        emit). Seeding from sync_state means the first poll sees the
        user's edit as a real change and emits the expected event.
        """
        self._last_seen_hash = self.sync_state.last_written_hash
        self._last_processed_write_version = self.sync_state.write_version
        logger.info(
            "ObsidianFsWatcher: watching %s (poll=%.3fs, seeded_hash=%s, "
            "seeded_write_version=%d)",
            self.tasks_path,
            self.poll_interval_seconds,
            "<none>" if self._last_seen_hash is None else "<seeded>",
            self._last_processed_write_version,
        )
        while True:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "ObsidianFsWatcher: poll failed for %s; continuing",
                    self.tasks_path,
                )
            await asyncio.sleep(self.poll_interval_seconds)

    async def poll_once(self) -> int:
        """Read the file once, emit events for user-driven status flips.

        Returns the count of ``obsidian.task.status_changed`` bus events
        published this poll — zero when the file is unchanged, matches
        the projection's last write, or contains only no-op flips
        (markers unchanged, unknown task ids, line not parseable).

        The file is read exactly once per call — the bytes are hashed
        for the layer-1/2 short-circuits and reused as text for
        layer-3 parsing. Reading twice would open a small TOCTOU
        window where the parsed content might disagree with the
        recorded hash on a rapidly-edited file.
        """
        raw = _read_file(self.tasks_path)
        current_hash = hashlib.sha256(raw).digest() if raw is not None else None

        # Layer 1: nothing changed since last poll. Single hash compare,
        # no file re-read or parsing. The cheap steady-state path.
        if current_hash == self._last_seen_hash:
            return 0

        # Layer 2: a genuine projection self-write — projection
        # committed a re-render since our last poll AND the file
        # currently matches that write. Distinguishing on the version
        # counter (not just the hash) is what makes the flip-then-
        # flip-back case work: if the user reverts to projection-known
        # content without the projection writing in between, the
        # version is unchanged and we fall through to layer 3 so the
        # real transition is emitted.
        projection_wrote_since_last_poll = (
            self.sync_state.write_version > self._last_processed_write_version
        )
        if (
            projection_wrote_since_last_poll
            and current_hash is not None
            and current_hash == self.sync_state.last_written_hash
        ):
            logger.debug(
                "ObsidianFsWatcher: %s changed to projection-known content; "
                "suppressing self-write (write_version=%d)",
                self.tasks_path,
                self.sync_state.write_version,
            )
            # The projection's re-rendered file is authoritative over
            # any user edits we'd previously observed — drop them so
            # the next real user edit measures against the projection's
            # fresh view, not a stale user marker.
            self._observed_markers.clear()
            self._last_seen_hash = current_hash
            self._last_processed_write_version = self.sync_state.write_version
            return 0

        # If the projection wrote but the file doesn't match (user
        # raced in between with their own edit, or two projection
        # writes coalesced and we missed one), still drop _observed —
        # the projection's view is the new baseline — and update the
        # version cursor so we don't keep re-clearing on every poll.
        if projection_wrote_since_last_poll:
            self._observed_markers.clear()
            self._last_processed_write_version = self.sync_state.write_version

        # Layer 3: real user edit. Parse + per-task transition detection.
        # The "prior" is the marker the user last committed to disk for
        # this task — falling back to the projection's view when we've
        # never seen a user edit for it. Emitting against this layered
        # baseline gives transition semantics: re-saving the same file
        # content (whitespace change, edit to a sibling line) does NOT
        # re-trigger an already-emitted transition.
        published = 0
        # Reuse the bytes we already read for hashing — avoids the
        # second disk read and the TOCTOU window it would open.
        # ``raw is None`` means missing/unreadable; nothing to parse.
        text = raw.decode("utf-8", errors="replace") if raw is not None else ""
        for task_id, marker in _parse_status_markers(text):
            prior = self._observed_markers.get(
                task_id, self.sync_state.task_status_markers.get(task_id)
            )
            if prior is None:
                # Task not in the projection's last-known render and
                # we haven't observed it before. Stale line, or a
                # Slice 3+ capture-macro line. Suppress — Slice 2
                # only owns projection-known tasks.
                continue
            if marker == prior:
                continue
            await self._publish_status_change(task_id, prior, marker)
            self._observed_markers[task_id] = marker
            published += 1

        self._last_seen_hash = current_hash
        return published

    async def _publish_status_change(self, task_id: str, prior: str, new: str) -> None:
        event = Event(
            type="obsidian.task.status_changed",
            timestamp=self._now_provider(),
            payload=MappingProxyType({"task_id": task_id, "prior": prior, "new": new}),
        )
        await self.bus.publish(event)
        logger.info(
            "ObsidianFsWatcher: published obsidian.task.status_changed task=%s %s→%s",
            task_id,
            prior,
            new,
        )


# ── helpers ────────────────────────────────────────────────────────────


def _read_file(path: Path) -> bytes | None:
    """Read ``path``'s current contents, or ``None`` when absent /
    unreadable.

    Returning raw bytes (not a hash or decoded text) lets the caller
    reuse the same bytes for both hashing and parsing without a
    second disk read — eliminates the TOCTOU window where the parsed
    content could disagree with the recorded hash on a rapidly-edited
    file.
    """
    try:
        return path.read_bytes()
    except (FileNotFoundError, OSError):
        return None


def _parse_status_markers(text: str) -> Iterator[tuple[str, str]]:
    """Yield ``(task_id, marker)`` pairs for every parseable task line.

    Takes already-decoded text rather than a path so the caller (see
    :meth:`ObsidianFsWatcher.poll_once`) can hash and parse the same
    bytes it read in one pass.

    Format expected (matches the projection's renderer):

        - [<m>] <title> ... 🆔 lithos:<id> ...

    Lines that don't start with ``- [<m>] `` (header comments, blank
    lines, free-text) are skipped silently. A matching prefix without
    a ``🆔 lithos:<id>`` marker is also skipped — it's a task-shaped
    line the projection didn't write, which is out of scope for
    Slice 2.

    Unknown checkbox markers (anything outside :data:`VALID_STATUS_MARKERS`)
    are skipped with a debug log; the user typed something we don't
    recognise, treat as no-op rather than emit a confusing event.
    """
    for line in text.splitlines():
        m = _LINE_RE.match(line)
        if m is None:
            continue
        marker = f"[{m.group('marker')}]"
        if marker not in VALID_STATUS_MARKERS:
            logger.debug(
                "ObsidianFsWatcher: unknown checkbox marker %r on line %r; skipping",
                marker,
                line,
            )
            continue
        id_match = _TASK_ID_RE.search(line)
        if id_match is None:
            continue
        yield id_match.group("task_id"), marker
