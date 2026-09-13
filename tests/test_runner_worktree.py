"""Unit tests for ``lithos_loom.runner.worktree``."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from lithos_loom.runner import worktree


def _branch_of(path: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=path,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_create_makes_worktree_on_new_branch(
    tmp_git_repo: Path, tmp_path: Path
) -> None:
    parent = tmp_path / "wts"
    wt = worktree.create(tmp_git_repo, "main", "Add a CLI flag!", parent=parent)
    assert wt.is_dir()
    assert wt.parent == parent
    # branch name is the dir name, slugged + random suffix
    assert wt.name.startswith("add-a-cli-flag-")
    assert _branch_of(wt) == wt.name
    # worktree HEAD matches the base branch tip
    repo_head = subprocess.run(
        ["git", "rev-parse", "main"], cwd=tmp_git_repo, capture_output=True, text=True
    ).stdout.strip()
    wt_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=wt, capture_output=True, text=True
    ).stdout.strip()
    assert wt_head == repo_head


def test_create_is_unique(tmp_git_repo: Path, tmp_path: Path) -> None:
    a = worktree.create(tmp_git_repo, "main", "task", parent=tmp_path / "w")
    b = worktree.create(tmp_git_repo, "main", "task", parent=tmp_path / "w")
    assert a != b


def test_remove_deletes_clean_worktree(tmp_git_repo: Path, tmp_path: Path) -> None:
    wt = worktree.create(tmp_git_repo, "main", "task", parent=tmp_path / "w")
    worktree.remove(wt)
    assert not wt.exists()


def test_remove_refuses_dirty_without_force(tmp_git_repo: Path, tmp_path: Path) -> None:
    wt = worktree.create(tmp_git_repo, "main", "task", parent=tmp_path / "w")
    (wt / "untracked.txt").write_text("dirty")
    with pytest.raises(RuntimeError):
        worktree.remove(wt, force=False)
    worktree.remove(wt, force=True)
    assert not wt.exists()


def test_remove_rejects_non_worktree(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError):
        worktree.remove(tmp_path / "nope")


def test_git_common_dir_is_main_repo_git(tmp_git_repo: Path, tmp_path: Path) -> None:
    # A linked worktree's common dir is the main repo's `.git` (#109).
    wt = worktree.create(tmp_git_repo, "main", "task", parent=tmp_path / "w")
    common = worktree.git_common_dir(wt)
    assert common.is_absolute()
    assert common.resolve() == (tmp_git_repo / ".git").resolve()


def test_git_common_dir_rejects_non_worktree(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError):
        worktree.git_common_dir(tmp_path / "nope")


# --- create_at: materialise a worktree AT an existing commit (#154) ----------


def _sha(path: Path, ref: str) -> str:
    return subprocess.run(
        ["git", "rev-parse", ref], cwd=path, capture_output=True, text=True
    ).stdout.strip()


def _add_commit(repo: Path, filename: str, content: str) -> str:
    (repo / filename).write_text(content)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", f"add {filename}"], cwd=repo, check=True)
    return _sha(repo, "HEAD")


def _clone_with_remote(tmp_path: Path, origin: Path) -> Path:
    """A local clone whose ``main`` tracks *origin* — the operator's checkout."""
    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", "-q", str(origin), str(clone)], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.email", "t@example.com"], cwd=clone, check=True
    )
    subprocess.run(["git", "config", "user.name", "T"], cwd=clone, check=True)
    return clone


def test_create_starts_at_the_fetched_remote_base_not_the_stale_local_branch(
    tmp_git_repo: Path, tmp_path: Path
) -> None:
    """#390 (lens #85): the operator's local `main` was one commit behind
    origin when the story dispatched; the branch cut there delivered a PR
    born behind its base. The start point is the remote base, fetched first
    — the local branch is only what the operator last pulled."""
    clone = _clone_with_remote(tmp_path, tmp_git_repo)
    stale_local = _sha(clone, "main")
    # origin moves on (a PR merges); the clone has not pulled
    moved = _add_commit(tmp_git_repo, "landed.txt", "merged upstream\n")
    assert _sha(clone, "main") == stale_local

    wt = worktree.create(clone, "main", "task", parent=tmp_path / "w")

    assert _sha(wt, "HEAD") == moved  # the fetched origin/main, not local main
    assert _sha(clone, "main") == stale_local  # the operator's branch untouched
    assert _branch_of(wt).startswith("task-")


def test_create_falls_back_to_the_local_branch_without_a_remote(
    tmp_git_repo: Path, tmp_path: Path
) -> None:
    # the test fixtures / a repo with no origin: the local branch is the base
    wt = worktree.create(tmp_git_repo, "main", "task", parent=tmp_path / "w")
    assert _sha(wt, "HEAD") == _sha(tmp_git_repo, "main")


def test_create_falls_back_to_the_local_branch_when_the_fetch_fails(
    tmp_git_repo: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Offline (or a dead remote) must not kill the run: the last-fetched
    remote ref is the base (never behind the operator's branch, unlike a
    local commit on main), and the fallback is logged so a stale run is
    explicable."""
    clone = _clone_with_remote(tmp_path, tmp_git_repo)
    subprocess.run(
        ["git", "remote", "set-url", "origin", str(tmp_path / "gone")],
        cwd=clone,
        check=True,
    )
    with caplog.at_level("WARNING", logger="lithos_loom.runner.worktree"):
        wt = worktree.create(clone, "main", "task", parent=tmp_path / "w")
    assert _sha(wt, "HEAD") == _sha(clone, "origin/main")
    assert any("fetch" in r.message and "local" in r.message for r in caplog.records)


def test_create_at_checks_out_detached_at_ref(
    tmp_git_repo: Path, tmp_path: Path
) -> None:
    """Review-only materialises a worktree AT the change head (detached), unlike
    ``create`` which branches fresh off a base."""
    first = _sha(tmp_git_repo, "HEAD")
    _add_commit(tmp_git_repo, "feature.txt", "the change\n")

    wt = worktree.create_at(tmp_git_repo, first, "review pr 1", parent=tmp_path / "w")

    assert wt.is_dir()
    assert wt.parent == tmp_path / "w"
    # HEAD is exactly the requested commit, and it is DETACHED (no branch)
    assert _sha(wt, "HEAD") == first
    assert _branch_of(wt) == "HEAD"
    # the tree reflects that commit — the later file is absent
    assert not (wt / "feature.txt").exists()


def test_create_at_reflects_head_ref(tmp_git_repo: Path, tmp_path: Path) -> None:
    head = _add_commit(tmp_git_repo, "feature.txt", "the change\n")
    wt = worktree.create_at(tmp_git_repo, head, "review", parent=tmp_path / "w")
    assert _sha(wt, "HEAD") == head
    assert (wt / "feature.txt").read_text() == "the change\n"


def test_create_at_is_unique(tmp_git_repo: Path, tmp_path: Path) -> None:
    head = _sha(tmp_git_repo, "HEAD")
    a = worktree.create_at(tmp_git_repo, head, "task", parent=tmp_path / "w")
    b = worktree.create_at(tmp_git_repo, head, "task", parent=tmp_path / "w")
    assert a != b


def test_create_at_rejects_unknown_ref(tmp_git_repo: Path, tmp_path: Path) -> None:
    with pytest.raises(RuntimeError):
        worktree.create_at(tmp_git_repo, "deadbeef" * 5, "task", parent=tmp_path / "w")


# --- create_on_branch: a committable branch AT an existing commit (converge) --


def test_create_on_branch_makes_committable_branch_at_commit(
    tmp_git_repo: Path, tmp_path: Path
) -> None:
    """Converge materialises a committable local branch AT the PR head so the
    fixer can commit onto it and it can be pushed back — unlike ``create_at``
    (detached, no branch) and ``create`` (branched off a base)."""
    first = _sha(tmp_git_repo, "HEAD")
    _add_commit(tmp_git_repo, "later.txt", "later\n")

    wt = worktree.create_on_branch(
        tmp_git_repo, first, "converge pr 1", parent=tmp_path / "w"
    )

    assert wt.is_dir()
    # a real branch (named after the dir), NOT detached HEAD
    assert _branch_of(wt) == wt.name
    assert wt.name.startswith("converge-pr-1-")
    # positioned at the requested commit — the later file is absent
    assert _sha(wt, "HEAD") == first
    assert not (wt / "later.txt").exists()
    # and it is committable: a commit lands on the branch
    (wt / "fix.txt").write_text("fix\n")
    subprocess.run(["git", "add", "-A"], cwd=wt, check=True)
    subprocess.run(["git", "commit", "-m", "fix"], cwd=wt, check=True)
    assert _sha(wt, "HEAD") != first


def test_create_on_branch_is_unique(tmp_git_repo: Path, tmp_path: Path) -> None:
    head = _sha(tmp_git_repo, "HEAD")
    a = worktree.create_on_branch(tmp_git_repo, head, "task", parent=tmp_path / "w")
    b = worktree.create_on_branch(tmp_git_repo, head, "task", parent=tmp_path / "w")
    assert a != b


def test_create_on_branch_rejects_unknown_ref(
    tmp_git_repo: Path, tmp_path: Path
) -> None:
    with pytest.raises(RuntimeError):
        worktree.create_on_branch(
            tmp_git_repo, "deadbeef" * 5, "task", parent=tmp_path / "w"
        )
