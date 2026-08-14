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


def seed_case_artifacts(case: Case, config: DevelopConfig, *, head_sha: str) -> int:
    """Copy *case*'s artifacts for *head_sha* under ``config.artifacts_dir``.

    Returns the file count. The captures are picked by which head is under
    review (RH-1): the defect captures for ``case.head``, the known-good ones
    for ``case.known_good_head``. Seeding the defect captures for the fixed
    head would make the false-positive rate measure the captures rather than
    the review, so an unrecognised head — or a known-good head with no
    known-good captures — fails closed rather than guessing.

    Raises :class:`ValueError` when the case is not an artifact case, or when
    the root/files fail the shared checks (:func:`resolve_artifacts_root` /
    :func:`iter_artifact_files` — absolute/``..``/symlinked root, symlinks or
    empty files anywhere, nothing to seed) — fail closed **before** any file is
    copied and before any paid reviewer turn, even on an unvalidated ``Case``.
    """
    if case.case_dir is None or case.artifacts_dir is None:
        raise ValueError(f"case {case.id}: not an artifact case (no artifacts_dir)")
    artifacts_dir, label = _variant_for(case, head_sha)
    root = resolve_artifacts_root(case.case_dir, artifacts_dir, case.id, label=label)
    files = iter_artifact_files(root, case.id, label=label)

    dest = config.artifacts_dir / _SEED_ROUND / _SEED_CHECK
    for src in files:
        target = dest / src.relative_to(root)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)
    return len(files)


def _variant_for(case: Case, head_sha: str) -> tuple[str, str]:
    """The ``(artifacts_dir, label)`` whose captures belong to *head_sha*.

    ``case.artifacts_dir`` is non-None by the time this is called.
    """
    # Ambiguity first: with head == known_good_head a positional check would
    # silently pick the defect captures for BOTH arms (#306 review). load_case
    # rejects such a case, but the seeder is the writer — resolving the
    # ambiguity by picking one is what decides what actually gets measured.
    if head_sha == case.head and head_sha == case.known_good_head:
        raise ValueError(
            f"case {case.id}: head {head_sha!r} is both the defect and the "
            "known-good head — refusing to choose which captures it reviews"
        )
    if head_sha == case.head:
        return str(case.artifacts_dir), "artifacts_dir"
    if case.known_good_head is not None and head_sha == case.known_good_head:
        if case.known_good_artifacts_dir is None:
            raise ValueError(
                f"case {case.id}: no known-good captures for the known-good head "
                "— seeding the defect captures would make the false-positive "
                "rate meaningless"
            )
        return case.known_good_artifacts_dir, "[known_good] artifacts_dir"
    raise ValueError(
        f"case {case.id}: head {head_sha!r} is neither the case head nor the "
        "known-good head — refusing to guess which captures it should review"
    )
