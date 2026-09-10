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
import re
import shlex
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ...runner import git, worktree
from .config import DevelopConfig
from .review_resolve import ResolvedChange

logger = logging.getLogger(__name__)

__all__ = [
    "ConflictIntake",
    "UnsupportedConflict",
    "markers_guard",
    "prepare_conflict_intake",
    "render_conflict_brief",
    "render_review_context",
]


class UnsupportedConflict(Exception):
    """The merge conflicts in a shape this mode cannot resolve by editing
    (PR #364 review round 3): a binary file, a modify/delete, a path git left
    without textual markers. Nothing distinguishes a coder's resolution of
    such a path from an untouched one, and `git add -A` would then silently
    take whichever side git left in the tree — so the intake refuses before
    any agent runs, naming every such path."""

    def __init__(self, reasons: dict[str, str], *, base_sha: str = "") -> None:
        self.paths = tuple(reasons)
        self.reasons = dict(reasons)
        self.base_sha = base_sha
        super().__init__(
            "conflict shape this mode cannot resolve by editing: "
            + "; ".join(f"{p} ({r})" for p, r in reasons.items())
        )


def _unsupported(wt: Path, paths: list[str]) -> dict[str, str]:
    """Why each of *paths* is NOT a text conflict with markers (empty = all fine)."""
    reasons: dict[str, str] = {}
    for path in paths:
        file = wt / path
        entries = git.unmerged_entries(wt, path)
        modes = set(entries.values())
        if not {2, 3} <= set(entries):
            reasons[path] = "deleted on one side (modify/delete)"
        elif "120000" in modes or file.is_symlink():
            reasons[path] = "symlink"
        elif "160000" in modes:
            reasons[path] = "submodule"
        elif not file.is_file():
            reasons[path] = "not a regular file"
        elif _looks_binary(file):
            reasons[path] = "binary"
        elif not git.conflict_markers(wt, [path]):
            reasons[path] = "no conflict markers to resolve"
    return reasons


def _looks_binary(file: Path) -> bool:
    with file.open("rb") as fh:
        return b"\0" in fh.read(8000)


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
    # `--base <sha>` resolves to an empty base_ref with the sha in base_sha;
    # the mode then merges THAT (PR #364 review F3), never `origin/<sha>`.
    base_ref = change.base_ref or change.base_sha
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
        unsupported = _unsupported(wt, paths)
        if unsupported:
            git.abort_merge(wt)
            raise UnsupportedConflict(unsupported, base_sha=base_sha)
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
    paths_fence = fence("\n".join(paths))
    lines = [
        "## The conflict",
        "",
        f"Merging `{base_ref}` @ `{base_sha[:12]}` into the PR branch "
        f"`{change.head_branch}` (head `{change.head_sha[:12]}`) conflicts in "
        f"{len(paths)} path(s), one per line:",
        "",
        paths_fence,
        *paths,
        paths_fence,
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
        label = fence(path)
        body = fence("\n".join(hunk))
        lines += ["#### path:", label, path, label, "", body, *hunk, body, ""]
    return "\n".join(lines)


def fence(content: str) -> str:
    """A Markdown fence the *content* cannot close: one backtick longer than
    the longest backtick run inside it (PR #364 review round 3 — a path or a
    hunk containing ``` closed a fixed fence and became prompt prose)."""
    longest = max((len(m) for m in re.findall(r"`+", content)), default=0)
    return "`" * max(3, longest + 1)


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
    paths: tuple[str, ...], *, head_sha: str, base_sha: str
) -> Callable[[Path], str | None]:
    """The pre-commit guard — the host-side enforcement behind the prompt's
    "never touch git state" (PR #364 review F2). It proves the INTENDED base
    is what gets merged: no conflicted path may still carry markers; while a
    merge is in progress it must be a merge of exactly *base_sha* onto the PR
    head; and once HEAD has moved past the PR head, *base_sha* must be an
    ancestor of it. Anything else — an aborted merge, a merge of something
    else, an agent commit that skipped the merge — fails the round, so the
    tree is never gated, reviewed or pushed."""

    def guard(wt: Path) -> str | None:
        marked = git.conflict_markers(wt, paths)
        if marked:
            return "conflict markers remain in: " + ", ".join(marked)
        merging = git.merge_head(wt)
        if merging is not None:
            # belt to the intake's braces (PR #364 review round 3): whatever
            # is still unmerged must be a text conflict the coder was given;
            # any other unmerged entry is one `git add -A` would resolve blind
            stray = [p for p in git.unmerged_paths(wt) if p not in paths]
            if stray:
                return (
                    "unmerged path(s) outside the resolvable set: "
                    + ", ".join(stray)
                    + " — a conflict shape this mode cannot resolve by editing"
                )
        head = git.commit_sha(wt)
        if merging is not None:
            if merging != base_sha:
                return (
                    f"MERGE_HEAD names {merging[:12]}, not the intended base "
                    f"{base_sha[:12]} — the merge in progress is not the base merge"
                )
            if head != head_sha:
                return (
                    f"HEAD moved to {head[:12]} before the base merge was committed "
                    f"(expected the PR head {head_sha[:12]})"
                )
            return None
        if git.is_ancestor(wt, base_sha, head):
            return None
        if head == head_sha:
            return (
                "the in-progress merge was abandoned in the worktree (no "
                "MERGE_HEAD at the PR head) — nothing to commit as the merge"
            )
        return (
            f"the intended base {base_sha[:12]} is not an ancestor of HEAD "
            f"{head[:12]} — the merge was abandoned and something else committed"
        )

    return guard


def render_review_context(
    paths: tuple[str, ...], *, head_sha: str, base_sha: str, base_ref: str
) -> str:
    """The panel's merge-shaped context (PR #364 review F1). The fork-point
    diff the reviewers start from runs base tip → HEAD, so a conflicted path
    resolved to the BASE version is absent from it — the PR's change silently
    dropped. Name the paths and both parents and give each side's diff.

    Every command is a shell-quoted argv (paths are repository-controlled:
    spaces, quotes, ``;``, ``$()``, globs, a leading ``:`` — and the reviewer
    is told to run these verbatim), with pathspec magic disabled and the shas
    in full (PR #364 review round 2).
    """
    git = ["git", "-C", "/workspace"]
    pr_side = shlex.join(
        [*git, "--literal-pathspecs", "diff", head_sha, "HEAD", "--", *paths]
    )
    base_side = shlex.join(
        [*git, "--literal-pathspecs", "diff", base_sha, "HEAD", "--", *paths]
    )
    # the merge commit is the first first-parent commit after the PR head; the
    # inner rev-list is fixed text, so the substitution is safe to run as is
    merge_commit = shlex.join(
        [*git, "rev-list", "--first-parent", "--reverse", f"{head_sha}..HEAD"]
    )
    show_merge = f'{shlex.join([*git, "show", "--cc"])} "$({merge_commit} | head -n 1)"'
    paths_fence = fence("\n".join(paths))
    cmd_fence = fence("\n".join((pr_side, base_side, show_merge)))
    lines = [
        "## This is a conflict-resolution merge",
        "",
        (
            f"HEAD composes the PR branch (head `{head_sha[:12]}`) with its base "
            f"`{base_ref}` @ `{base_sha[:12]}`; the coder resolved conflicts in "
            "these paths (one per line):"
        ),
        "",
        paths_fence,
        *paths,
        paths_fence,
        "",
        (
            "The `git diff` above runs from the base tip, so it shows the PR's "
            "work on the new base — and a conflicted path resolved to the base's "
            "version is **absent** from it. Inspect the resolution from BOTH "
            "parents, running these exactly as written:"
        ),
        "",
        (
            "What the resolution did to the PR's side (a path missing from the "
            "base-side diff but present here was resolved to the base version "
            "— confirm the PR's intent survived, or was genuinely superseded, "
            "before approving):"
        ),
        "",
        cmd_fence,
        pr_side,
        cmd_fence,
        "",
        "What it did to the base's landed work:",
        "",
        cmd_fence,
        base_side,
        cmd_fence,
        "",
        (
            "The merge commit's own combined diff (only hunks differing from "
            "BOTH parents):"
        ),
        "",
        cmd_fence,
        show_merge,
        cmd_fence,
        "",
        "Approve only if both intents survive, correctly composed.",
    ]
    return "\n".join(lines)
