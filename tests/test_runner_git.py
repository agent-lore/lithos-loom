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


def test_log_between_lists_commit_messages(tmp_git_repo: Path) -> None:
    # Feeds the converge cold-start prompt's {commit_log} so a fixer picking up a
    # PR it did not author sees the original author's narration.
    base = git.base_sha(tmp_git_repo)
    _commit(tmp_git_repo, "a.txt", "a")
    _commit(tmp_git_repo, "b.txt", "b")
    log = git.log_between(tmp_git_repo, base)
    assert "add a.txt" in log and "add b.txt" in log
    # oldest first (matches commits_since order)
    assert log.index("add a.txt") < log.index("add b.txt")


def test_log_between_empty_without_commits(tmp_git_repo: Path) -> None:
    base = git.base_sha(tmp_git_repo)
    assert git.log_between(tmp_git_repo, base) == ""  # base == HEAD


def test_raises_on_bad_repo(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError):
        git.base_sha(tmp_path)  # not a git repo


# ── merge-aware ranges (PRD S5c) ────────────────────────────────────────────
#
# Every range helper used to be two-dot against a `base_sha` captured once at
# worktree creation. The moment a branch absorbs a merge of its base, that sha
# is no longer the fork point: `diff base..HEAD` shows everything that landed
# on the base as if the branch had written it, and `rev-list` / `log` count the
# merged-in commits as round commits. The fixtures below are the first in this
# file with a merge commit in them — the two-dot helpers passed their own unit
# tests precisely because none had one.


def _sha(repo: Path, ref: str = "HEAD") -> str:
    return subprocess.run(
        ["git", "rev-parse", ref], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


def _run(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _story_with_base_merge(repo: Path, *, conflict: bool) -> tuple[str, str, str]:
    """Cut a story branch, advance ``main`` behind it, merge ``main`` back in.

    Returns ``(start_sha, story_commit, base_commit)``. The story touches
    ``own.txt`` (and ``shared.txt``); the base touches ``base.txt`` (and, when
    *conflict*, ``shared.txt`` on the same line — resolved on the story side).
    """
    (repo / "shared.txt").write_text("v0\n")
    _run(repo, "add", "-A")
    _run(repo, "commit", "-m", "seed shared")
    start = _sha(repo)

    _run(repo, "switch", "-c", "story", "-q")
    (repo / "own.txt").write_text("story work\n")
    (repo / "shared.txt").write_text("story edit\n")
    _run(repo, "add", "-A")
    _run(repo, "commit", "-m", "story: own change")
    story_commit = _sha(repo)

    _run(repo, "switch", "main", "-q")
    (repo / "base.txt").write_text("someone else's merged PR\n")
    if conflict:
        (repo / "shared.txt").write_text("base edit\n")
    _run(repo, "add", "-A")
    _run(repo, "commit", "-m", "base: other PR landed")
    base_commit = _sha(repo)

    _run(repo, "switch", "story", "-q")
    merge = subprocess.run(
        ["git", "merge", "--no-ff", "main", "-m", "merge main into story"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    if conflict:
        assert merge.returncode != 0, "fixture expected a conflict on shared.txt"
        (repo / "shared.txt").write_text("resolved: story edit\n")
        _run(repo, "add", "shared.txt")
        _run(repo, "commit", "--no-edit", "-m", "merge main into story")
    else:
        assert merge.returncode == 0, merge.stderr
    return start, story_commit, base_commit


def test_fork_point_is_the_start_sha_until_the_branch_absorbs_a_base_merge(
    tmp_git_repo: Path,
) -> None:
    start = git.base_sha(tmp_git_repo)
    _run(tmp_git_repo, "switch", "-c", "story", "-q")
    _commit(tmp_git_repo, "own.txt", "x")
    base = git.RangeBase(start_sha=start, ref="main")
    # no merge yet: the recorded start IS the fork point (S5c changes nothing
    # for a run that never merged its base)
    assert git.fork_point(tmp_git_repo, base) == start


def test_fork_point_moves_to_the_merge_base_after_a_base_merge(
    tmp_git_repo: Path,
) -> None:
    start, _story, base_commit = _story_with_base_merge(tmp_git_repo, conflict=False)
    base = git.RangeBase(start_sha=start, ref="main")
    assert git.fork_point(tmp_git_repo, base) == base_commit


def test_fork_point_keeps_the_start_when_the_live_ref_lags_it(
    tmp_git_repo: Path,
) -> None:
    # The operator's local main is AHEAD of the ref we measure against (an
    # unpushed local commit): the branch was cut from the newer tip, so the
    # recorded start is still the fork point — never rewind to the lagging ref.
    lagging = git.base_sha(tmp_git_repo)
    _run(tmp_git_repo, "tag", "lagging-base", lagging)
    _commit(tmp_git_repo, "local.txt", "unpushed")
    start = git.base_sha(tmp_git_repo)
    _run(tmp_git_repo, "switch", "-c", "story", "-q")
    _commit(tmp_git_repo, "own.txt", "x")
    base = git.RangeBase(start_sha=start, ref="lagging-base")
    assert git.fork_point(tmp_git_repo, base) == start


def test_fork_point_without_a_ref_is_the_start_sha_and_touches_no_git(
    tmp_path: Path,
) -> None:
    # An entry with no live ref (a bare `base..head` review, the eval harness)
    # keeps today's behaviour exactly — and never shells out, so a fake worktree
    # in a unit test needs no repo behind it.
    base = git.RangeBase(start_sha="0" * 40)
    assert git.fork_point(tmp_path / "not-a-repo", base) == "0" * 40


def test_fork_point_raises_on_an_unresolvable_ref(tmp_git_repo: Path) -> None:
    base = git.RangeBase(start_sha=git.base_sha(tmp_git_repo), ref="origin/nope")
    with pytest.raises(RuntimeError):
        git.fork_point(tmp_git_repo, base)


def test_base_ref_for_prefers_the_remote_tracking_branch(
    tmp_git_repo: Path, tmp_path: Path
) -> None:
    # No remote: the local branch is the only base there is.
    assert git.base_ref_for(tmp_git_repo, "main") == "main"
    bare = tmp_path / "origin.git"
    _run(tmp_git_repo, "init", "--bare", "-q", str(bare))
    _run(tmp_git_repo, "remote", "add", "origin", str(bare))
    _run(tmp_git_repo, "push", "-q", "origin", "main")
    # With origin/<base> present it is the ref a delivered PR lands against —
    # and the one S5 merges — so ranges measure against it.
    assert git.base_ref_for(tmp_git_repo, "main") == "origin/main"
    # An unrelated branch name still falls back to the local name.
    assert git.base_ref_for(tmp_git_repo, "develop") == "develop"


def test_pair_review_diff_is_unchanged_by_a_clean_base_merge(
    tmp_git_repo: Path,
) -> None:
    # THE S5c guard: a branch that absorbed a base merge produces the same
    # review diff as before the merge. Snapshot the pre-merge stat first.
    (tmp_git_repo / "shared.txt").write_text("v0\n")
    _run(tmp_git_repo, "add", "-A")
    _run(tmp_git_repo, "commit", "-m", "seed shared")
    start = git.base_sha(tmp_git_repo)
    _run(tmp_git_repo, "switch", "-c", "story", "-q")
    (tmp_git_repo / "own.txt").write_text("story work\n")
    (tmp_git_repo / "shared.txt").write_text("story edit\n")
    _run(tmp_git_repo, "add", "-A")
    _run(tmp_git_repo, "commit", "-m", "story: own change")
    base = git.RangeBase(start_sha=start, ref="main")
    before = git.diff_stat(tmp_git_repo, git.fork_point(tmp_git_repo, base))
    commits_before = git.commits_since(tmp_git_repo, git.fork_point(tmp_git_repo, base))

    _run(tmp_git_repo, "switch", "main", "-q")
    _commit(tmp_git_repo, "base.txt", "someone else's merged PR\n")
    _run(tmp_git_repo, "switch", "story", "-q")
    _run(tmp_git_repo, "merge", "--no-ff", "main", "-m", "merge main into story")

    fork = git.fork_point(tmp_git_repo, base)
    after = git.diff_stat(tmp_git_repo, fork)
    assert after == before  # base.txt never appears: it is the base's work
    # The merge commit is the branch's own commit; the merged-in one is not.
    commits_after = git.commits_since(tmp_git_repo, fork)
    assert commits_after == [*commits_before, _sha(tmp_git_repo)]


def test_pair_review_diff_after_a_conflicting_merge_shows_only_the_resolution(
    tmp_git_repo: Path,
) -> None:
    # "Modulo the conflict resolution": the resolved file is (rightly) in the
    # branch's diff, the base's own file is not, and no base commit is counted.
    start, story_commit, base_commit = _story_with_base_merge(
        tmp_git_repo, conflict=True
    )
    base = git.RangeBase(start_sha=start, ref="main")
    fork = git.fork_point(tmp_git_repo, base)
    stat = git.diff_stat(tmp_git_repo, fork)
    assert "own.txt" in stat and "shared.txt" in stat
    assert "base.txt" not in stat
    assert "2 files changed" in stat

    commits = git.commits_since(tmp_git_repo, fork)
    assert story_commit in commits
    assert base_commit not in commits
    assert commits[-1] == _sha(tmp_git_repo)  # the merge commit, branch-side

    log = git.log_between(tmp_git_repo, fork)
    assert "story: own change" in log
    assert "merge main into story" in log
    assert "other PR landed" not in log


def test_commits_since_walks_first_parent_past_a_merge(tmp_git_repo: Path) -> None:
    # converge measures the FIXER's commits from the PR head. After the fixer
    # merges the base, `rev-list head..HEAD` would count every merged-in base
    # commit as fixer work; first-parent keeps the branch side only.
    _start, story_commit, base_commit = _story_with_base_merge(
        tmp_git_repo, conflict=False
    )
    since_head = git.commits_since(tmp_git_repo, story_commit)
    assert since_head == [_sha(tmp_git_repo)]
    assert base_commit not in since_head
