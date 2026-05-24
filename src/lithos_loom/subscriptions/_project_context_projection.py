"""``project-context-projection`` subscription handler (Slice 4 US29).

Consumes ``lithos.note.{created,updated,deleted}`` events emitted by
:class:`~lithos_loom.sources.lithos_note_stream.LithosNoteStream` and
writes/rewrites/removes per-project-context Markdown files under
``<vault>/<projects_dir>/<slug>/<filename>.md``.

D26 puts the filter (path-prefix + tag) at the subscription, not the
source — the source publishes ALL ``lithos.note.*`` events. The
projection drops any note whose ``path`` doesn't start with
``projects/`` or whose tags don't include ``project-context``.
Symmetric with the permissive task source.

The handler is intentionally simpler than the tasks projection
(:mod:`._obsidian_projection`):

- No in-memory ``_StateEntry`` map. The task projection accumulates
  all open tasks into one file; project context is one file per doc,
  so each event is self-contained. State lives in
  :class:`~lithos_loom.sync_state.ProjectionSyncState` (per-doc hash
  + version) rather than per-handler.
- No TTL eviction. Project-context docs persist until deleted in
  Lithos.
- No debouncing. Each event corresponds to a distinct file; there's
  no coalescing benefit.
- Per-doc dedup via the body hash recorded in sync_state — on
  bootstrap with N unchanged docs, N writes are short-circuited.

Lifecycle per event:

1. **Filter at boundary.** Skip notes not under ``projects/`` and
   skip those missing the ``project-context`` tag. Logged at DEBUG.
2. **Re-fetch.** The SSE payload carries only ``{id, title, path}``;
   we need the body + metadata. Call ``ctx.lithos.note_read(id=...)``.
3. **Filter again on the freshly fetched tags.** The SSE event's
   tags can be stale (the bootstrap path doesn't carry tags at all).
   Re-check after fetch.
4. **Render** via :func:`render_project_context.render_doc`.
5. **Dedup.** If the body hash matches ``sync_state.note_content_hashes[id]``
   skip the write — same content already on disk.
6. **Atomic write.** Record sync_state *before* committing the
   rename (same ordering invariant as the tasks projection).
7. **Deleted events** remove the local file (best-effort) and
   ``forget_project_context`` so a re-creation later isn't suppressed
   as a self-write.

The render module is pure; the atomic write reuses
:func:`._atomic_write.write_file_atomic` so the same temp + fsync +
rename contract (and load-bearing no-await-inside invariant) applies
to per-doc projection.
"""

from __future__ import annotations

import contextlib
import logging
from pathlib import Path
from typing import Any

from lithos_loom.bus import Event
from lithos_loom.config import LoomConfig
from lithos_loom.lithos_client import Note
from lithos_loom.render_project_context import compute_body_hash, render_doc
from lithos_loom.subscriptions import Handler, SubscriptionContext
from lithos_loom.subscriptions._atomic_write import write_file_atomic
from lithos_loom.sync_state import ProjectionSyncState

__all__ = ["make_handler"]

logger = logging.getLogger(__name__)


_PROJECTS_PATH_PREFIX = "projects/"
_PROJECT_CONTEXT_TAG = "project-context"

_REEVALUATE_EVENTS: frozenset[str] = frozenset(
    {"lithos.note.created", "lithos.note.updated"}
)
_REMOVAL_EVENTS: frozenset[str] = frozenset({"lithos.note.deleted"})


def make_handler(
    cfg: LoomConfig,
    *,
    sync_state: ProjectionSyncState | None = None,
) -> Handler:
    """Build a stateful ``project-context-projection`` handler bound to ``cfg``.

    The returned coroutine captures the vault path + projects_dir
    from ``cfg.obsidian_sync`` and the per-doc state living in
    ``sync_state``. ``sync_state=None`` (test default) constructs a
    fresh isolated state — the projection still works, just without
    a dir-watcher consumer to coordinate with (relevant once Slice 5
    lands).

    ``cfg.obsidian_sync`` must be set; the obsidian-sync child's
    spawn gate guarantees this, but we assert for defensive
    readability (same shape as the tasks projection).
    """
    obs = cfg.obsidian_sync
    if obs is None:
        raise RuntimeError(
            "make_handler called without [obsidian_sync] config; the "
            "supervisor's spawn gate should have prevented this"
        )
    projects_root = obs.vault_path / obs.projects_dir
    sync_state = sync_state if sync_state is not None else ProjectionSyncState()

    async def handle(event: Event, ctx: SubscriptionContext) -> None:
        # Branch on event type first — guards against malformed payloads
        # on unknown event types (same pattern as the tasks projection).
        if event.type not in _REEVALUATE_EVENTS and event.type not in _REMOVAL_EVENTS:
            ctx.logger.debug(
                "project-context-projection: ignoring unexpected event type %s",
                event.type,
            )
            return

        try:
            note_id = str(event.payload["id"])
        except (KeyError, TypeError) as exc:
            ctx.logger.warning(
                "project-context-projection: malformed payload for %s: %r",
                event.type,
                exc,
            )
            return

        if event.type in _REMOVAL_EVENTS:
            # Removal events carry ``path`` per the source's hard
            # requirement (see LithosNoteStream._handle_sse_event —
            # we fail closed at the source if path is missing). The
            # path is what tells us which on-disk file to remove
            # since the doc is gone from Lithos by the time we react.
            try:
                path = str(event.payload["path"])
            except (KeyError, TypeError) as exc:
                ctx.logger.warning(
                    "project-context-projection: malformed deleted payload "
                    "(missing path) for %s: %r",
                    event.type,
                    exc,
                )
                return
            await _handle_deleted(note_id, path, projects_root, sync_state, ctx)
            return

        # Path-prefix filter at the boundary (D26). The source publishes
        # all note events; we only project docs under ``projects/``.
        # Source-emitted ``path`` may be empty for bootstrap-via-note_list
        # entries that lack a path field — re-fetch and check post-read.
        sse_path = str(event.payload.get("path") or "")
        if sse_path and not sse_path.startswith(_PROJECTS_PATH_PREFIX):
            ctx.logger.debug(
                "project-context-projection: skipping note %s — path %r "
                "outside projects/",
                note_id,
                sse_path,
            )
            return

        # Re-fetch for the full body + metadata (tags, version,
        # updated_at). The SSE payload only carries
        # ``{id, title, path}``, which is insufficient for rendering.
        note = await ctx.lithos.note_read(id=note_id)
        if note is None:
            ctx.logger.info(
                "project-context-projection: note %s not found in Lithos "
                "(possibly deleted between event and read); skipping",
                note_id,
            )
            return

        # Re-check filters on the FRESHLY fetched note. The SSE event's
        # tag set can be stale (bootstrap paths carry partial metadata,
        # tags may have changed). This is the authoritative filter.
        if not note.path.startswith(_PROJECTS_PATH_PREFIX):
            ctx.logger.debug(
                "project-context-projection: skipping note %s — fetched "
                "path %r outside projects/",
                note_id,
                note.path,
            )
            return
        if _PROJECT_CONTEXT_TAG not in note.tags:
            ctx.logger.debug(
                "project-context-projection: skipping note %s — fetched "
                "tags %s do not include %r",
                note_id,
                list(note.tags),
                _PROJECT_CONTEXT_TAG,
            )
            return

        await _project_note(note, projects_root, sync_state, ctx)

    return handle


async def _project_note(
    note: Note,
    projects_root: Path,
    sync_state: ProjectionSyncState,
    ctx: Any,
) -> None:
    """Render and write a single project-context note to the vault.

    Per-doc dedup: if the body hash matches the projection's last
    recorded hash for this doc, skip the write entirely. This is
    what makes bootstrap a near no-op when nothing has changed since
    last run (N notes → 0 writes if all match).

    Self-write coordination: record_project_context_write fires
    *before* the atomic rename so a concurrent Slice 5 dir-watcher
    poll that sees the new file also sees the matching hash entry.
    """
    rendered = render_doc(note)
    rendered_body_hash = compute_body_hash(rendered)

    last_hash = sync_state.note_content_hashes.get(note.id)
    if last_hash == rendered_body_hash:
        ctx.logger.debug(
            "project-context-projection: skipping note %s — body hash "
            "matches last write (no-op)",
            note.id,
        )
        return

    # Lithos path is ``projects/<slug>/<filename>.md``; strip the
    # ``projects/`` prefix so the vault path is
    # ``<projects_root>/<slug>/<filename>.md``. This makes the slug +
    # filename map 1:1 across Lithos and vault.
    rel_path = note.path[len(_PROJECTS_PATH_PREFIX) :]
    target = projects_root / rel_path

    # Coordination state BEFORE the write — same ordering invariant as
    # the tasks projection so any concurrent dir-watcher poll that
    # sees new bytes also sees matching state.
    sync_state.record_project_context_write(
        doc_id=note.id,
        body_hash=rendered_body_hash,
        version=note.version,
    )
    try:
        await write_file_atomic(target, rendered)
    except Exception:
        # Roll back coordination state on write failure so the next
        # event retries cleanly rather than thinking the file matched.
        sync_state.forget_project_context(doc_id=note.id)
        raise

    ctx.logger.info(
        "project-context-projection: wrote %s (slug=%s, version=%d)",
        target,
        note.slug,
        note.version,
    )


async def _handle_deleted(
    note_id: str,
    lithos_path: str,
    projects_root: Path,
    sync_state: ProjectionSyncState,
    ctx: Any,
) -> None:
    """Remove the local file and forget the projection state.

    Best-effort delete: missing file (operator manually removed,
    earlier failed write) is fine. The sync_state forget is what
    prevents a subsequent re-creation of the same doc from being
    suppressed as a self-write.
    """
    if not lithos_path.startswith(_PROJECTS_PATH_PREFIX):
        ctx.logger.debug(
            "project-context-projection: skipping delete for note %s — "
            "path %r outside projects/",
            note_id,
            lithos_path,
        )
        return
    rel_path = lithos_path[len(_PROJECTS_PATH_PREFIX) :]
    target = projects_root / rel_path

    with contextlib.suppress(FileNotFoundError):
        target.unlink()
    sync_state.forget_project_context(doc_id=note_id)

    ctx.logger.info(
        "project-context-projection: removed %s (note %s deleted in Lithos)",
        target,
        note_id,
    )
