"""PRD S5 — conflict convergence: the merge IS the intake.

``converge --resolve-conflicts`` enters the fix loop from a merge left **in
progress** in a throwaway worktree at the PR head: the base's current tip
merged with ``--no-commit``, the conflicted paths carrying markers. Round 1's
coder resolves them (``resolve_coder_init.md``, seeded with the brief built
here); the round commit then IS the merge commit (``MERGE_HEAD`` is set, so
``git commit`` records two parents); the project's check-set and the panel
judge the composed tree — the fork point moves to the base tip once the
merge commit exists (S5c) — and an approved run pushes the merge commit
onto the PR branch, append-only. A tree still carrying markers is refused
by the pre-commit guard, never gated, reviewed or pushed.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ...runner import git, worktree
from .config import DevelopConfig
from .review_resolve import ResolvedChange

logger = logging.getLogger(__name__)

__all__ = [
    "ConflictIntake",
    "markers_guard",
    "prepare_conflict_intake",
    "render_conflict_brief",
]

# Bounds on the brief: a conflict in a generated file can be thousands of
# lines; the coder reads the whole file in the sandbox anyway.
_HUNK_LINES_PER_PATH = 120
_LANDED_COMMITS = 40


@dataclass(frozen=True)
class ConflictIntake:
    """A merge in progress, ready for the resolution round."""

    worktree: Path
    base_ref: str
    base_sha: str
    merge_base: str
    paths: tuple[str, ...]
    brief: str


def prepare_conflict_intake(
    config: DevelopConfig, change: ResolvedChange
) -> ConflictIntake | None:
    """Merge the base's current tip into a throwaway worktree at the PR head,
    without committing. ``None`` when there is nothing to resolve — the head
    already contains the base tip, or the merge is clean (the base-move
    re-gate's job, PRD S3): the worktree is removed again and no agent runs.
    """
    base_ref = change.base_ref or f"origin/{config.base_branch}"
    base_sha = git.commit_sha(config.repo, base_ref)
    if git.is_ancestor(config.repo, base_sha, change.head_sha):
        return None
    wt = worktree.create_on_branch(
        config.repo,
        change.head_sha,
        f"resolve {change.head_ref}",
        parent=config.worktree_parent,
    )
    try:
        paths = git.merge_no_commit(wt, base_sha)
        if not paths:
            git.abort_merge(wt)
            raise _CleanMerge
        brief = render_conflict_brief(
            wt, change, base_ref=base_ref, base_sha=base_sha, paths=paths
        )
    except _CleanMerge:
        _discard(config, wt)
        return None
    except Exception:
        _discard(config, wt)
        raise
    logger.info(
        "resolve %s: merge of %s @ %s into %s conflicts in %d path(s): %s",
        config.run_id,
        base_ref,
        base_sha[:12],
        change.head_ref,
        len(paths),
        ", ".join(paths),
    )
    return ConflictIntake(
        worktree=wt,
        base_ref=base_ref,
        base_sha=base_sha,
        merge_base=change.base_sha,
        paths=tuple(paths),
        brief=brief,
    )


class _CleanMerge(Exception):
    pass


def _discard(config: DevelopConfig, wt: Path) -> None:
    branch = wt.name
    try:
        worktree.remove(wt, force=True)
        git.delete_branch(config.repo, branch)
    except Exception:  # noqa: BLE001 — cleanup is best-effort
        logger.warning("resolve %s: could not remove worktree %s", config.run_id, wt)


def render_conflict_brief(
    wt: Path,
    change: ResolvedChange,
    *,
    base_ref: str,
    base_sha: str,
    paths: list[str],
) -> str:
    """The round-1 brief: the PR's intent, what landed on the base since the
    merge-base, and the conflicted hunks (bounded per path)."""
    landed = git.log_between(wt, change.base_sha, base_sha).splitlines()
    if len(landed) > _LANDED_COMMITS:
        landed = landed[:_LANDED_COMMITS] + [
            f"… and {len(landed) - _LANDED_COMMITS} more"
        ]
    lines = [
        "## The conflict",
        "",
        f"Merging `{base_ref}` @ `{base_sha[:12]}` into the PR branch "
        f"`{change.head_branch}` (head `{change.head_sha[:12]}`) conflicts in "
        f"{len(paths)} path(s):",
        "",
        *(f"- `{p}`" for p in paths),
        "",
        "### The PR's intent",
        "",
        f"**{change.title}**",
        "",
        change.body.strip() or "(no description)",
        "",
        "### Landed on the base since the PR's merge-base",
        "",
        *(landed or ["(no commits in range)"]),
        "",
        "### Conflicted hunks (bounded; read the whole files in /workspace)",
        "",
    ]
    for path in paths:
        hunk = _conflicted_region(wt / path)
        lines += [f"#### `{path}`", "", "```", *hunk, "```", ""]
    return "\n".join(lines)


def _conflicted_region(file: Path) -> list[str]:
    if not file.is_file():
        return ["(deleted on one side)"]
    try:
        text = file.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ["(unreadable)"]
    starts = [i for i, line in enumerate(text) if line.startswith("<<<<<<< ")]
    if not starts:
        return text[:_HUNK_LINES_PER_PATH]
    out: list[str] = []
    for start in starts:
        end = next(
            (i for i in range(start, len(text)) if text[i].startswith(">>>>>>> ")),
            len(text) - 1,
        )
        region = text[start : end + 1]
        if len(region) > _HUNK_LINES_PER_PATH:
            region = region[:_HUNK_LINES_PER_PATH] + [
                f"… ({len(region) - _HUNK_LINES_PER_PATH} more lines in this hunk)"
            ]
        out += region + [""]
        if len(out) > _HUNK_LINES_PER_PATH * 3:
            out.append("… (further hunks omitted)")
            break
    return out


def markers_guard(
    paths: tuple[str, ...], *, head_sha: str
) -> Callable[[Path], str | None]:
    """The pre-commit guard: refuse a tree where any conflicted path still
    carries markers — and refuse a tree at the PR head with NO merge in
    progress (the coder abandoned the merge; a plain commit there would
    "converge" without the base, and only the next re-gate would notice)."""

    def guard(wt: Path) -> str | None:
        marked = git.conflict_markers(wt, paths)
        if marked:
            return "conflict markers remain in: " + ", ".join(marked)
        if git.commit_sha(wt) == head_sha and not git.merge_in_progress(wt):
            return (
                "the in-progress merge was abandoned in the worktree (no "
                "MERGE_HEAD at the PR head) — nothing to commit as the merge"
            )
        return None

    return guard
