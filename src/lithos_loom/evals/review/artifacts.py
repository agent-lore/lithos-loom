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
from .case import Case, iter_artifact_files, resolve_artifacts_root

# The synthetic round/check the seeded files impersonate. "seeded" (not a real
# check name) keeps the reviewer prompt honest about where the captures came
# from while landing on the exact layout the artifact pass renders.
_SEED_ROUND = "round_01"
_SEED_CHECK = "seeded"


def seed_case_artifacts(case: Case, config: DevelopConfig) -> int:
    """Copy *case*'s artifacts under ``config.artifacts_dir``; return the count.

    Raises :class:`ValueError` when the case is not an artifact case, or when
    the root/files fail the shared checks (:func:`resolve_artifacts_root` /
    :func:`iter_artifact_files` — absolute/``..``/symlinked root, symlinks or
    empty files anywhere, nothing to seed) — fail closed **before** any file is
    copied and before any paid reviewer turn, even on an unvalidated ``Case``.
    """
    if case.case_dir is None or case.artifacts_dir is None:
        raise ValueError(f"case {case.id}: not an artifact case (no artifacts_dir)")
    root = resolve_artifacts_root(case.case_dir, case.artifacts_dir, case.id)
    files = iter_artifact_files(root, case.id)

    dest = config.artifacts_dir / _SEED_ROUND / _SEED_CHECK
    for src in files:
        target = dest / src.relative_to(root)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)
    return len(files)
