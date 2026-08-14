"""Materialise a patch-based eval case head (#193).

A case can define its head as a ``.patch`` applied to ``base`` at runtime instead
of a pinned commit sha — so the case needs no off-branch commit + tag (only
``base`` stays a real reachable commit, and the seeded change is a reviewable diff
in the case dir). We build an *ephemeral* commit (a throwaway worktree detached at
``base``, ``git apply`` the patch, commit), use its sha as the review head, and
keep the build worktree alive until the run ends so the otherwise-dangling commit
can't be gc'd out from under the review worktrees the harness creates at it.
"""

from __future__ import annotations

import contextlib
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from ...runner import git, worktree
from .case import Case


def _materialise_patched_head(
    repo: Path, base_sha: str, patch_path: Path, *, parent: Path
) -> tuple[str, Path]:
    """Build ``base + patch`` as an ephemeral commit.

    Returns ``(head_sha, build_worktree)``. The build worktree is **kept** (HEAD
    detached at the new commit) so the commit stays reachable; the caller removes
    it. A patch that applies to no net change raises :class:`ValueError` (it must
    not silently make ``head == base``); a patch that doesn't apply raises
    :class:`RuntimeError` from ``git apply``.
    """
    wt = worktree.create_at(repo, base_sha, "eval-patch", parent=parent)
    try:
        git.apply_patch(wt, patch_path)
        head_sha = git.commit_all(wt, f"eval patch: {patch_path.name}")
        if head_sha is None:
            raise ValueError(
                f"eval patch {patch_path.name} applied to no change — "
                "the head would equal the base"
            )
        return head_sha, wt
    except Exception:
        worktree.remove(wt, force=True)
        raise


def _require_distinct_content(
    repo: Path, case_id: str, head: str, known_good_head: str | None
) -> None:
    """Fail closed when a case's two heads describe the same code (#306 review).

    ``load_case`` rejects the *declared* forms of this (the same sha or the same
    patch file twice), but two different patches can still build byte-identical
    trees — and then the commit shas differ (different commit messages) while
    the known-good arm reviews the defect. Every "false positive" it reports
    would really be a catch, which is the corruption the known-good pairing
    exists to prevent, so this raises rather than reporting a number.
    """
    if not known_good_head:
        return
    if head == known_good_head:
        raise ValueError(
            f"case {case_id}: the defect and known-good heads resolved to the "
            "same commit — the false-positive arm would review the defect itself"
        )
    if git.tree_sha(repo, head) == git.tree_sha(repo, known_good_head):
        raise ValueError(
            f"case {case_id}: the defect and known-good heads have identical "
            "trees — different commits, byte-identical code, so the "
            "false-positive arm would review the defect itself"
        )


def materialise_patch_heads(case: Case) -> tuple[Case, Callable[[], None]]:
    """Resolve a case's patch-defined head(s) to ephemeral-commit shas (#193).

    For a sha-based case this is **identity + a no-op cleanup**. For a patch-based
    case it builds the buggy (and known-good, if any) ephemeral commits, returns a
    :class:`Case` whose ``head`` / ``known_good_head`` are the ephemeral shas, and a
    ``cleanup`` that tears down the build worktrees + their tmp parent. Call this
    **once per ``run_case``** so the ephemeral commits are built once and reused
    across all K samples (and stay reachable for the run's whole lifetime).
    """
    if not case.head_patch and not case.known_good_head_patch:
        return case, lambda: None

    repo = Path(case.repo).resolve()  # cwd-relative, like live_review — NOT case_dir
    case_dir = case.case_dir
    if case_dir is None:  # pragma: no cover - load_case always sets it
        raise ValueError(
            f"case {case.id}: case_dir is required to resolve a patch head"
        )
    # Absolute: `git apply` runs with cwd=build-worktree, so a case_dir relative to
    # the launch cwd (the shipped cases pass `evals/review/cases/<id>`) would not be
    # found from there. Resolve against the current cwd, where the case dir lives.
    case_dir = case_dir.resolve()
    parent = Path(tempfile.mkdtemp(prefix="loom-eval-patch-"))
    built: list[Path] = []

    def cleanup() -> None:
        for wt in built:
            with contextlib.suppress(Exception):  # best-effort teardown
                worktree.remove(wt, force=True)
        shutil.rmtree(parent, ignore_errors=True)

    try:
        replacements: dict[str, str] = {}
        if case.head_patch:
            head_sha, wt = _materialise_patched_head(
                repo, case.base, case_dir / case.head_patch, parent=parent
            )
            built.append(wt)
            replacements["head"] = head_sha
        if case.known_good_head_patch:
            kg_base = case.known_good_base or case.base
            kg_sha, wt = _materialise_patched_head(
                repo, kg_base, case_dir / case.known_good_head_patch, parent=parent
            )
            built.append(wt)
            replacements["known_good_head"] = kg_sha
        _require_distinct_content(
            repo,
            case.id,
            replacements.get("head", case.head),
            replacements.get("known_good_head", case.known_good_head),
        )
        return replace(case, **replacements), cleanup
    except Exception:
        cleanup()
        raise
