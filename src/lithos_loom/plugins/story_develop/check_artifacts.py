"""Gate-check artifact collection (#283 slice 1).

Rescues a check's rendered artifacts (e.g. e2e screenshots) from its doomed
per-check tree export into the run's host-controlled artifacts dir, as an
exact, symlink-free, atomic snapshot. See :func:`collect_check_artifacts` for
the threat model — the export's contents and the handoff are both untrusted,
so the host-privileged copy neither follows symlinks nor writes anywhere an
agent container can reach read-write.
"""

from __future__ import annotations

import logging
import os
import shutil
import uuid
from pathlib import Path

from .config import DevelopConfig

logger = logging.getLogger(__name__)


def collect_check_artifacts(
    config: DevelopConfig, tree: Path, round_no: int, check_name: str
) -> None:
    """Rescue a check's artifacts dir from its doomed tree export (#283).

    The per-check export is deleted right after the check runs (#282
    isolation), destroying anything the check rendered there — e.g. the e2e
    screenshots a repo-parity ``make e2e`` writes. When the project declares
    ``develop_artifacts_path``, snapshot that dir into the HOST-CONTROLLED
    ``config.artifacts_dir`` under ``round_NN/<check>/`` (agents see it via a
    read-only mount at ``.handoff/artifacts`` — never a host-privileged write
    into an agent-writable dir). Unconditional on the check's verdict — a RED
    e2e run's screenshots are exactly what a reviewer needs.

    Hardening (PR #289 review): the export's contents are repo/check-
    controlled, so the copy NEVER follows symlinks — the artifacts root must
    be a real directory resolving inside the export, and only regular,
    non-symlink files are copied (anything else is skipped and counted).
    Each collection is an exact atomic snapshot: files land in a temp dir
    that is renamed over any prior ``round_NN/<check>`` — stale files from an
    earlier execution never merge in. Best-effort throughout: failure is
    logged, never fatal, and never blocks the tree cleanup that follows.
    """
    if not config.artifacts_path:
        return
    src = tree / config.artifacts_path
    tmp: Path | None = None
    try:
        if src.is_symlink() or not src.is_dir():
            return
        if not src.resolve().is_relative_to(tree.resolve()):
            logger.warning(
                "story-develop %s: round %d %s artifacts root resolves outside "
                "the export (skipping): %s",
                config.run_id,
                round_no,
                check_name,
                src,
            )
            return
        config.artifacts_dir.mkdir(parents=True, exist_ok=True)
        tmp = config.artifacts_dir / f".tmp-{check_name}-{uuid.uuid4().hex}"
        copied = 0
        skipped = 0
        for dirpath, dirnames, filenames in os.walk(src, followlinks=False):
            rel = Path(dirpath).relative_to(src)
            for fname in filenames:
                entry = Path(dirpath) / fname
                if entry.is_symlink() or not entry.is_file():
                    skipped += 1
                    continue
                target_dir = tmp / rel
                target_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(entry, target_dir / fname)
                copied += 1
            # prune symlinked subdirs from the walk (followlinks=False stops
            # descent, but they'd still be listed; count them as skipped)
            links = [d for d in dirnames if (Path(dirpath) / d).is_symlink()]
            skipped += len(links)
            dirnames[:] = [d for d in dirnames if d not in links]
        if copied == 0:
            return
        dest = config.artifacts_dir / f"round_{round_no:02d}" / check_name
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            shutil.rmtree(dest)
        os.rename(tmp, dest)
        tmp = None
        logger.info(
            "story-develop %s: round %d %s artifacts collected to %s (%d files%s)",
            config.run_id,
            round_no,
            check_name,
            dest,
            copied,
            f", {skipped} non-regular entries skipped" if skipped else "",
        )
    except OSError as exc:
        logger.warning(
            "story-develop %s: round %d %s artifact collection failed (continuing): %s",
            config.run_id,
            round_no,
            check_name,
            exc,
        )
    finally:
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)
