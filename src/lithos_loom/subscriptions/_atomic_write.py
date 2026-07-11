"""Shared atomic file-write helper for projection subscriptions.

Extracted from :mod:`._obsidian_projection` so the project-context
projection and any future per-file projection can share the same
temp + fsync + rename contract without copy-paste. The strategy and
load-bearing invariants are unchanged from the original site — only
the import surface moved.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path

__all__ = ["write_file_atomic"]


async def write_file_atomic(path: Path, content: str) -> None:
    """Atomically rewrite ``path`` with ``content``.

    Strategy: write to a dot-prefixed sibling ``.<name>.tmp``, fsync,
    then ``os.replace`` onto the final path. ``os.replace`` is atomic
    on POSIX. Creates ``path.parent`` if absent. If anything between
    the temp-write and the replace raises, the temp file is best-effort
    cleaned up so a failed write doesn't litter the vault with
    ``.<name>.md.tmp`` (Copilot review on lithos-loom#17, mirroring
    ``write_result_atomically`` in plugin_runner.py).

    The leading dot is load-bearing: Obsidian Sync ignores dot-prefixed
    files, so the transient temp file never triggers a sync upload/race
    (lithos-loom#52). The temp file stays in ``path.parent`` so
    ``os.replace`` is a same-filesystem rename and therefore atomic.

    **No internal** ``await`` **points** — load-bearing invariant for
    every projection that uses this. Callers pair their sync-state
    update with the rename in one of two orderings, and both rely on
    this function never yielding:

    1. The watcher's self-write suppression. There must be no yield
       between the caller's sync-state record and ``os.replace`` — else
       a poll could see the new file content without the matching state
       (or vice-versa), mis-firing per-task suppression. The task
       projection records *after* this returns (relying on no yield
       between the rename and the return); the project-context
       projection records *before* calling this (relying on no yield
       between the record and the rename). The no-internal-await
       property closes both windows.
    2. The record-before caller's failure-rollback contract. A caller
       that records *before* the write (the project-context projection)
       catches ``Exception`` to roll back its sync-state object when the
       rename didn't apply, and lets ``CancelledError`` propagate
       without rolling back on the grounds that cancellation cannot fire
       mid-rename. That reasoning requires this function to have no
       suspension points where cancellation could fire after the
       rename but before this function returns. (The record-after task
       projection has nothing to roll back — a failed write never
       reaches its record call.)

    Don't add ``await`` here without re-deriving both invariants. If
    write latency becomes an issue, ``asyncio.to_thread`` wraps this
    whole synchronous body in one yield-after-completion shot rather
    than introducing yields inside it.

    Synchronous I/O inside an async function — fine for the
    vault-sized files this serves (<10kB typical for tasks files;
    project-context bodies may be larger but still bounded by KB scale).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    try:
        tmp.write_text(content, encoding="utf-8")
        fd = os.open(tmp, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)
    except Exception:
        # Best-effort cleanup. If unlink itself fails (already gone,
        # permission flip), swallow — the original exception is more
        # informative for the operator.
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
