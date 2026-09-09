"""Git helpers — base SHA, commits-since, dirty detection (US-13).

Lifted from Ralph++ ``ralph_pp/steps/_git.py`` and trimmed to the three
primitives Loom needs. All functions shell out to ``git`` with an explicit
``cwd`` and raise :class:`RuntimeError` on non-zero exit so callers fail loudly
rather than acting on a half-read repo.

**Ranges are merge-aware (PRD S5c).** A story branch may absorb a merge of its
base mid-run (a conflict resolution, a base auto-update). After that, the sha
recorded at worktree creation is no longer the fork point, and a plain two-dot
range against it would present everything that landed on the base as the
branch's own work — the reviewer would be asked to review other people's
merged PRs. So callers carry a :class:`RangeBase` (the recorded start + the
live base ref) and measure from :func:`fork_point`; the commit enumerators walk
``--first-parent`` so merged-in commits are never counted as round commits.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path


def _git(worktree: Path, *args: str) -> str:
    """Run ``git *args`` in *worktree*; return stripped stdout or raise."""
    result = subprocess.run(
        ["git", *args],
        cwd=worktree,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed (exit {result.returncode}): "
            f"{result.stderr.strip()}"
        )
    return result.stdout.strip()


def base_sha(worktree: Path) -> str:
    """Return the current ``HEAD`` SHA of *worktree*.

    Callers record this immediately after worktree creation (before any agent
    commit) as the ``start_sha`` of their :class:`RangeBase`; it is the fork
    point until the branch absorbs a merge of its base.
    """
    return _git(worktree, "rev-parse", "HEAD")


@dataclass(frozen=True)
class RangeBase:
    """Where a branch's own work begins (PRD S5c).

    ``start_sha`` is ``HEAD`` at worktree creation (or the PR merge-base for a
    converge entry). ``ref`` is the LIVE base the branch will land on — the
    remote-tracking base branch, see :func:`base_ref_for` — or empty when there
    is none to consult (a bare ``base..head`` review, an operator-forced base).
    Resolve the pair to a sha with :func:`fork_point` at the moment of use, never
    once up front: a base merge in round 3 moves it.
    """

    start_sha: str
    ref: str = ""


def base_ref_for(worktree: Path, base_branch: str) -> str:
    """Name the live base ref for *base_branch* as seen from *worktree*.

    ``origin/<base_branch>`` when the remote-tracking ref exists — that is what
    a delivered PR lands against and what a base merge brings in — else the
    local branch name (a repo with no remote, the test fixtures).
    """
    remote = f"origin/{base_branch}"
    probe = subprocess.run(
        ["git", "rev-parse", "--verify", "-q", f"refs/remotes/{remote}"],
        cwd=worktree,
        capture_output=True,
        text=True,
    )
    return remote if probe.returncode == 0 else base_branch


def fork_point(worktree: Path, base: RangeBase) -> str:
    """Resolve *base* to the sha the branch's own work starts from, right now.

    The newer of ``start_sha`` and ``merge-base HEAD <ref>`` along HEAD's
    history: the recorded start until the branch absorbs a merge of the base,
    the merge-base after. "Newer" is by ancestry, not date — when the live ref
    LAGS the start (the branch was cut from a local tip ahead of the remote),
    the start stays, so a run that never merged its base is measured exactly as
    before S5c. No ref → the start sha, without touching git (a fake worktree
    in a unit test needs no repo behind it).
    """
    if not base.ref:
        return base.start_sha
    merge_base = _git(worktree, "merge-base", "HEAD", base.ref)
    # the live ref LAGS the recorded start → the start is still the fork point
    lagging = is_ancestor(worktree, merge_base, base.start_sha)
    return base.start_sha if lagging else merge_base


def is_ancestor(worktree: Path, ancestor: str, descendant: str) -> bool:
    """Whether *ancestor* is reachable from *descendant* (a commit is its own).

    ``git merge-base --is-ancestor`` answers with the exit code: 0 = yes, 1 =
    no, anything higher is a command error (an unresolvable ref) — which must
    raise, never read as "no" (PR #358 review).
    """
    probe = subprocess.run(
        ["git", "merge-base", "--is-ancestor", ancestor, descendant],
        cwd=worktree,
        capture_output=True,
        text=True,
    )
    if probe.returncode > 1:
        raise RuntimeError(
            f"git merge-base --is-ancestor {ancestor} {descendant} failed "
            f"(exit {probe.returncode}): {probe.stderr.strip()}"
        )
    return probe.returncode == 0


def commit_sha(worktree: Path, ref: str = "HEAD") -> str:
    """Resolve *ref* to its full commit SHA.

    Generalises :func:`base_sha` to any ref: two different declaration strings
    (a tag, a branch, an abbreviated sha) can name the same commit, so callers
    comparing "are these the same commit?" must compare resolved SHAs, not the
    strings they were handed.
    """
    return _git(worktree, "rev-parse", f"{ref}^{{commit}}")


def tree_sha(worktree: Path, ref: str = "HEAD") -> str:
    """Return the tree object *ref* points at — a commit's CONTENT identity.

    Two commits with different SHAs can describe byte-identical code (they
    differ only in message, author, or parent), so comparing commit SHAs is not
    a comparison of what the commits contain.
    """
    return _git(worktree, "rev-parse", f"{ref}^{{tree}}")


def commits_since(worktree: Path, since: str) -> list[str]:
    """Return full 40-char SHAs the BRANCH added since *since*, oldest first.

    First-parent only: a merge of the base into the branch contributes its
    merge commit (the branch's own), never the commits it brought in — those
    are reachable from the base and are not round commits. Pass the current
    :func:`fork_point` (or, for converge's fixer count, the PR head sha).
    """
    out = _git(worktree, "rev-list", "--reverse", "--first-parent", f"{since}..HEAD")
    return out.splitlines() if out else []


def has_uncommitted_changes(worktree: Path) -> bool:
    """Return True if *worktree* has staged or unstaged changes."""
    return bool(_git(worktree, "status", "--porcelain"))


def commit_all(
    worktree: Path, message: str, *, exclude: Sequence[str] = ()
) -> str | None:
    """Stage all changes (minus *exclude* pathspecs) and commit if any remain.

    *exclude* entries are git pathspecs (e.g. ``".handoff"``) kept out of the
    commit — used to keep orchestration scaffolding out of the deliverable
    branch. Returns the new commit SHA, or ``None`` when nothing was staged.
    """
    pathspec = [".", *(f":(exclude){p}" for p in exclude)]
    _git(worktree, "add", "-A", "--", *pathspec)
    # Defensively unstage excluded paths too, in case something was already
    # staged before this call (the agent is told not to, but must not be able
    # to leak .handoff/ into the deliverable commit).
    for p in exclude:
        _git(worktree, "reset", "-q", "--", p)
    # A merge in progress is ALWAYS committed (PRD S5): a resolution that keeps
    # HEAD's side everywhere leaves the staged tree equal to HEAD — an empty
    # cached diff — yet the two-parent commit is exactly what records the base
    # tip as an ancestor. Only outside a merge does "nothing staged" mean
    # "nothing to commit".
    if not merge_in_progress(worktree) and not _git(
        worktree, "diff", "--cached", "--name-only"
    ):
        return None
    _git(worktree, "commit", "-m", message)
    return base_sha(worktree)


def apply_patch(worktree: Path, patch_path: Path) -> None:
    """Apply the unified diff at *patch_path* to *worktree*'s working tree (#193).

    Used by the eval harness to materialise a case's head from a ``.patch`` instead
    of a pinned sha. A patch that doesn't apply cleanly (a drifted base) exits
    non-zero → :func:`_git` raises, so a bogus head can never be silently built.
    """
    _git(worktree, "apply", str(patch_path))


def log_between(worktree: Path, base: str, head: str = "HEAD") -> str:
    """Return the branch's commit log from *base* to *head*, oldest first.

    Feeds the converge cold-start prompt's ``{commit_log}`` slot so a fixer that
    did not author the PR sees the original author's narration of what was built
    and why. First-parent, like :func:`commits_since`: a merged-in base commit
    is not the PR author's narration. Empty when *base* is already *head*.
    """
    return _git(
        worktree,
        "log",
        "--reverse",
        "--first-parent",
        "--format=%h %s%n%b",
        f"{base}..{head}",
    )


def diff_stat(worktree: Path, base: str) -> str:
    """Return ``git diff --stat base...HEAD`` — the branch's cumulative change.

    Pre-injected into the reviewer prompt for orientation (#136). Three-dot: the
    diff is taken from the merge-base of *base* and HEAD, so a ref that has
    moved on (or a :func:`fork_point`, which is its own merge-base) yields the
    branch's own changes only. Empty string when *base* is already HEAD.
    """
    return _git(worktree, "diff", "--stat", f"{base}...HEAD")


def merge(worktree: Path, ref: str, *, message: str) -> list[str]:
    """Merge *ref* into *worktree*'s HEAD; return the conflicting paths.

    The trial merge behind PRD S3 / S5. Always ``--no-ff`` so a base move
    yields a real merge commit (parents: the branch head, then *ref*) rather
    than a fast-forward that would rewrite which commit the PR head *is*;
    ``git merge`` reports "already up to date" (nothing to do) as success with
    no commit. A conflict returns the unmerged paths — the only source of that
    list, the GitHub API does not provide it — and **aborts** the merge, so
    the tree is left exactly as found (HEAD unmoved, nothing staged, no
    ``MERGE_HEAD``); an empty list means the merge landed.
    """
    result = subprocess.run(
        ["git", "merge", "--no-ff", "--no-edit", "-m", message, ref],
        cwd=worktree,
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return []
    unmerged = _git(worktree, "diff", "--name-only", "--diff-filter=U")
    paths = unmerged.splitlines() if unmerged else []
    if not paths:
        # not a conflict — a bad ref, a dirty tree, an unrelated failure
        raise RuntimeError(
            f"git merge {ref} failed (exit {result.returncode}): "
            f"{result.stderr.strip()}"
        )
    _git(worktree, "merge", "--abort")
    return paths


def merge_no_commit(worktree: Path, ref: str) -> list[str]:
    """Merge *ref* into HEAD **without committing**; return the conflicting paths.

    The S5 conflict-resolution intake: unlike :func:`merge` this leaves a
    conflict IN PROGRESS — ``MERGE_HEAD`` set, the auto-merged paths staged,
    the returned paths carrying conflict markers in the working tree — so a
    coder can resolve them in place and :func:`commit_all` then produces a
    real merge commit (parents: HEAD, then *ref*). A clean merge is staged
    and uncommitted too (``MERGE_HEAD`` set, nothing to resolve); the caller
    decides between :func:`abort_merge` and a commit. Always ``--no-ff``.
    """
    result = subprocess.run(
        ["git", "merge", "--no-ff", "--no-commit", ref],
        cwd=worktree,
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return []
    unmerged = _git(worktree, "diff", "--name-only", "--diff-filter=U")
    paths = unmerged.splitlines() if unmerged else []
    if not paths:
        raise RuntimeError(
            f"git merge --no-commit {ref} failed (exit {result.returncode}): "
            f"{result.stderr.strip()}"
        )
    return paths


def abort_merge(worktree: Path) -> None:
    """Abandon an in-progress merge, restoring the pre-merge tree."""
    _git(worktree, "merge", "--abort")


def merge_in_progress(worktree: Path) -> bool:
    """Whether *worktree* has a merge in progress (``MERGE_HEAD`` set) —
    resolved through ``--git-path`` so a linked worktree's private git dir is
    the one consulted."""
    marker = Path(_git(worktree, "rev-parse", "--git-path", "MERGE_HEAD"))
    return (marker if marker.is_absolute() else worktree / marker).exists()


def _carries_markers(file: Path) -> bool:
    """A ``<<<<<<< `` or ``>>>>>>> `` line is a marker on its own (a half-removed
    conflict is still a broken file); a bare ``=======`` (a setext underline,
    a banner) never is — git only writes it between the other two."""
    with file.open("rb") as fh:
        for raw in fh:
            line = raw.rstrip(b"\r\n")
            if line.startswith(b"<<<<<<< ") or line.startswith(b">>>>>>> "):
                return True
    return False


def conflict_markers(worktree: Path, paths: Sequence[str]) -> list[str]:
    """Of *paths*, those whose working-tree content still carries conflict
    markers — the pre-commit guard for a resolution round (``git commit`` does
    not refuse markers; the guard must). A path deleted as its resolution
    carries none."""
    return [
        p for p in paths if (worktree / p).is_file() and _carries_markers(worktree / p)
    ]


def delete_branch(repo: Path, branch: str) -> None:
    """Delete local *branch* (``-D``: a throwaway trial-merge branch is never
    merged anywhere, so the safe ``-d`` would always refuse). Raises when the
    branch does not exist or is checked out."""
    _git(repo, "branch", "-D", branch)
