"""Seed a case's checked-in artifacts into a run's artifacts dir (RH-3 / #294).

An artifact case measures the approval-hold artifact-review pass, so its
captures must appear where :func:`~....plugins.story_develop.check_artifacts.
render_artifacts_note` walks — ``config.artifacts_dir/round_01/seeded/…`` —
exactly as if the gate's collector had produced them. ``artifacts_dir`` is
otherwise host-collector-only by design (symlink-hardened, PR #289); this is
the one other sanctioned writer, so it re-applies the same posture itself:
regular files only, no symlinks, everything resolved inside the case dir.
"""

from __future__ import annotations

import shutil

from ...plugins.story_develop.config import DevelopConfig
from .case import Case

# The synthetic round/check the seeded files impersonate. "seeded" (not a real
# check name) keeps the reviewer prompt honest about where the captures came
# from while landing on the exact layout the artifact pass renders.
_SEED_ROUND = "round_01"
_SEED_CHECK = "seeded"


def seed_case_artifacts(case: Case, config: DevelopConfig) -> int:
    """Copy *case*'s artifacts under ``config.artifacts_dir``; return the count.

    Raises :class:`ValueError` when the case is not an artifact case, the
    directory is missing/empty, or any entry is a symlink or escapes the case's
    artifacts dir — fail closed before any paid reviewer turn.
    """
    if case.case_dir is None or case.artifacts_dir is None:
        raise ValueError(f"case {case.id}: not an artifact case (no artifacts_dir)")
    root = (case.case_dir / case.artifacts_dir).resolve()
    if not root.is_dir():
        raise ValueError(f"case {case.id}: artifacts dir {root} is not a directory")

    dest = config.artifacts_dir / _SEED_ROUND / _SEED_CHECK
    count = 0
    for src in sorted(root.rglob("*")):
        rel = src.relative_to(root)
        if src.is_symlink():
            raise ValueError(
                f"case {case.id}: artifact {rel} is a symlink — refusing to seed"
            )
        if src.is_dir():
            continue
        if not src.is_file():
            raise ValueError(f"case {case.id}: artifact {rel} is not a regular file")
        if not src.resolve().is_relative_to(root):
            raise ValueError(f"case {case.id}: artifact {rel} escapes the case dir")
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)
        count += 1
    if count == 0:
        raise ValueError(f"case {case.id}: artifacts dir {root} contains no files")
    return count
