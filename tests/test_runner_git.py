"""Unit tests for ``lithos_loom.runner.git``."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from lithos_loom.runner import git


def _commit(repo: Path, name: str, content: str) -> None:
    (repo / name).write_text(content)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", f"add {name}"],
        cwd=repo,
        check=True,
        capture_output=True,
    )


def test_base_sha_is_current_head(tmp_git_repo: Path) -> None:
    sha = git.base_sha(tmp_git_repo)
    assert len(sha) == 40
    rev = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_git_repo, capture_output=True, text=True
    ).stdout.strip()
    assert sha == rev


def test_commits_since_enumerates_in_order(tmp_git_repo: Path) -> None:
    base = git.base_sha(tmp_git_repo)
    assert git.commits_since(tmp_git_repo, base) == []
    _commit(tmp_git_repo, "a.txt", "a")
    _commit(tmp_git_repo, "b.txt", "b")
    commits = git.commits_since(tmp_git_repo, base)
    assert len(commits) == 2
    assert commits[-1] == git.base_sha(tmp_git_repo)  # newest is current HEAD


def test_has_uncommitted_changes(tmp_git_repo: Path) -> None:
    assert git.has_uncommitted_changes(tmp_git_repo) is False
    (tmp_git_repo / "dirty.txt").write_text("x")
    assert git.has_uncommitted_changes(tmp_git_repo) is True


def test_commit_all_commits_when_dirty_and_noops_when_clean(tmp_git_repo: Path) -> None:
    assert git.commit_all(tmp_git_repo, "noop") is None
    (tmp_git_repo / "new.txt").write_text("hi")
    sha = git.commit_all(tmp_git_repo, "feat: new")
    assert sha is not None and sha == git.base_sha(tmp_git_repo)
    assert git.has_uncommitted_changes(tmp_git_repo) is False


def test_commit_all_excludes_even_already_staged_paths(tmp_git_repo: Path) -> None:
    # An excluded path that was already staged before commit_all must still be
    # kept out of the commit (defends the .handoff/ guarantee).
    (tmp_git_repo / ".handoff").mkdir()
    (tmp_git_repo / ".handoff" / "note.md").write_text("scaffolding")
    subprocess.run(
        ["git", "add", "-A"], cwd=tmp_git_repo, check=True, capture_output=True
    )
    (tmp_git_repo / "real.txt").write_text("code")
    sha = git.commit_all(tmp_git_repo, "feat", exclude=[".handoff"])
    assert sha is not None
    tree = subprocess.run(
        ["git", "show", "--name-only", "--format=", "HEAD"],
        cwd=tmp_git_repo,
        capture_output=True,
        text=True,
    ).stdout
    assert "real.txt" in tree
    assert ".handoff" not in tree


def test_commit_all_returns_none_when_only_excluded_changes(tmp_git_repo: Path) -> None:
    (tmp_git_repo / ".handoff").mkdir()
    (tmp_git_repo / ".handoff" / "note.md").write_text("scaffolding")
    assert git.commit_all(tmp_git_repo, "feat", exclude=[".handoff"]) is None


def test_diff_stat_lists_changed_files(tmp_git_repo: Path) -> None:
    base = git.base_sha(tmp_git_repo)
    _commit(tmp_git_repo, "a.txt", "a\n")
    _commit(tmp_git_repo, "b.txt", "b\n")
    out = git.diff_stat(tmp_git_repo, base)  # base..HEAD
    assert "a.txt" in out and "b.txt" in out
    assert "2 files changed" in out


def test_diff_stat_empty_without_changes(tmp_git_repo: Path) -> None:
    base = git.base_sha(tmp_git_repo)
    assert git.diff_stat(tmp_git_repo, base) == ""  # base == HEAD


def test_apply_patch_applies_a_clean_diff(tmp_git_repo: Path, tmp_path: Path) -> None:
    # #193: produce a real `git diff` patch, then apply it onto a clean tree.
    _commit(tmp_git_repo, "f.txt", "one\n")
    (tmp_git_repo / "f.txt").write_text("two\n")
    patch = subprocess.run(
        ["git", "diff"], cwd=tmp_git_repo, capture_output=True, text=True
    ).stdout
    subprocess.run(
        ["git", "checkout", "--", "."],
        cwd=tmp_git_repo,
        check=True,
        capture_output=True,
    )
    patch_file = tmp_path / "change.patch"
    patch_file.write_text(patch)

    git.apply_patch(tmp_git_repo, patch_file)
    assert (tmp_git_repo / "f.txt").read_text() == "two\n"


def test_apply_patch_raises_on_conflict(tmp_git_repo: Path, tmp_path: Path) -> None:
    # a patch that doesn't apply (targets a file/contents that aren't there) must
    # fail loudly so a drifted base can't silently produce a bogus head.
    patch_file = tmp_path / "bad.patch"
    patch_file.write_text("--- a/nope.txt\n+++ b/nope.txt\n@@ -1 +1 @@\n-x\n+y\n")
    with pytest.raises(RuntimeError):
        git.apply_patch(tmp_git_repo, patch_file)


def test_raises_on_bad_repo(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError):
        git.base_sha(tmp_path)  # not a git repo
