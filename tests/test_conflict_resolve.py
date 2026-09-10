"""PRD S5 conflict convergence — the intake module, guard and reviewer context.

PR #364 review: (1) a resolution that takes the BASE version of a conflicted
path is invisible in the ordinary base-to-HEAD review diff, so the panel
needs a merge-shaped context naming the paths and both parents; (2) the
pre-commit guard must prove the INTENDED base was merged, not just that
markers are gone; (3) ``--base <sha>`` (an empty ``base_ref``) must merge the
resolved sha, not ``origin/<sha>``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from lithos_loom.plugins.story_develop.config import DevelopConfig
from lithos_loom.plugins.story_develop.conflict_resolve import (
    markers_guard,
    prepare_conflict_intake,
    render_review_context,
)
from lithos_loom.plugins.story_develop.review_resolve import ResolvedChange
from lithos_loom.runner import git


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


def _seed(repo: Path) -> tuple[str, str, str]:
    """main + feature both edit shared.txt; returns (merge-base, head, base tip)."""
    (repo / "shared.txt").write_text("v0\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "seed")
    merge_base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "switch", "-q", "-c", "feature")
    (repo / "shared.txt").write_text("feature\n")
    _git(repo, "commit", "-q", "-am", "feature work")
    head = _git(repo, "rev-parse", "HEAD")
    _git(repo, "switch", "-q", "main")
    (repo / "shared.txt").write_text("base\n")
    _git(repo, "commit", "-q", "-am", "feat: landed on main")
    base_tip = _git(repo, "rev-parse", "HEAD")
    _git(repo, "switch", "-q", "feature")
    return merge_base, head, base_tip


def _change(merge_base: str, head: str, *, base_ref: str = "main") -> ResolvedChange:
    return ResolvedChange(
        base_sha=merge_base,
        head_sha=head,
        head_ref="#1 (feature)",
        base_ref=base_ref,
        title="A PR",
        body="do the thing",
        head_branch="feature",
    )


# ── (1) the reviewer's merge-shaped context ───────────────────────────────────


def test_a_base_side_resolution_is_invisible_to_the_ordinary_diff(
    tmp_git_repo: Path,
) -> None:
    """The blind spot: resolve shared.txt to the BASE version and the
    fork-point diff (base tip → HEAD) no longer mentions it at all — only a
    diff against the PR-head parent shows the PR's change was dropped."""
    _merge_base, head, base_tip = _seed(tmp_git_repo)
    assert git.merge_no_commit(tmp_git_repo, base_tip) == ["shared.txt"]
    (tmp_git_repo / "shared.txt").write_text("base\n")  # take theirs
    assert git.commit_all(tmp_git_repo, "resolve: take base") is not None

    fork = git.fork_point(tmp_git_repo, git.RangeBase(_merge_base, "main"))
    assert fork == base_tip  # the composed-tree review base (S5c)
    assert "shared.txt" not in git.diff_stat(tmp_git_repo, fork)
    assert "shared.txt" in _git(tmp_git_repo, "diff", "--stat", f"{head}..HEAD")


def test_review_context_names_paths_and_both_parent_diffs() -> None:
    text = render_review_context(
        ("shared.txt", "docs/x.md"),
        head_sha="h" * 40,
        base_sha="b" * 40,
        base_ref="origin/main",
    )
    assert "`shared.txt`" in text and "`docs/x.md`" in text
    # both parents in full, and a per-side diff command the reviewer can run
    # verbatim (pathspec magic off, `--` before the paths)
    assert f"--literal-pathspecs diff {'h' * 40} HEAD -- shared.txt docs/x.md" in text
    assert f"--literal-pathspecs diff {'b' * 40} HEAD -- shared.txt docs/x.md" in text
    assert "origin/main" in text
    # the trap is spelled out: a path absent from the base-side diff was
    # resolved to the base version — check the PR's intent survived
    assert "absent" in text.lower() and "intent" in text.lower()


# ── (2) the guard proves the INTENDED base was merged ─────────────────────────


def test_guard_refuses_a_merge_of_the_wrong_base(tmp_git_repo: Path) -> None:
    merge_base, head, base_tip = _seed(tmp_git_repo)
    guard = markers_guard(("shared.txt",), head_sha=head, base_sha=base_tip)
    # a merge in progress of some OTHER commit (the coder aborted and merged
    # something else): MERGE_HEAD is not the intended base
    _git(tmp_git_repo, "switch", "-q", "-c", "other", merge_base)
    (tmp_git_repo / "other.txt").write_text("elsewhere\n")
    _git(tmp_git_repo, "add", "-A")
    _git(tmp_git_repo, "commit", "-q", "-m", "other")
    other = _git(tmp_git_repo, "rev-parse", "HEAD")
    _git(tmp_git_repo, "switch", "-q", "feature")
    assert git.merge_no_commit(tmp_git_repo, other) == []
    refused = guard(tmp_git_repo)
    assert refused is not None and "MERGE_HEAD" in refused and base_tip[:12] in refused


def test_guard_refuses_an_aborted_merge_followed_by_a_commit(
    tmp_git_repo: Path,
) -> None:
    _merge_base, head, base_tip = _seed(tmp_git_repo)
    guard = markers_guard(("shared.txt",), head_sha=head, base_sha=base_tip)
    assert git.merge_no_commit(tmp_git_repo, base_tip) == ["shared.txt"]
    git.abort_merge(tmp_git_repo)
    (tmp_git_repo / "other.txt").write_text("agent commit\n")
    _git(tmp_git_repo, "add", "-A")
    _git(tmp_git_repo, "commit", "-q", "-m", "coder committed on its own")
    assert _git(tmp_git_repo, "rev-parse", "HEAD") != head
    refused = guard(tmp_git_repo)
    assert refused is not None and "not an ancestor" in refused


def test_guard_passes_the_intended_merge_before_and_after_its_commit(
    tmp_git_repo: Path,
) -> None:
    _merge_base, head, base_tip = _seed(tmp_git_repo)
    guard = markers_guard(("shared.txt",), head_sha=head, base_sha=base_tip)
    assert git.merge_no_commit(tmp_git_repo, base_tip) == ["shared.txt"]
    (tmp_git_repo / "shared.txt").write_text("both\n")
    assert guard(tmp_git_repo) is None
    assert git.commit_all(tmp_git_repo, "resolve") is not None
    assert guard(tmp_git_repo) is None  # later rounds: base is an ancestor
    (tmp_git_repo / "fix.txt").write_text("round 2\n")
    assert guard(tmp_git_repo) is None


# ── (3) `--base <sha>`: an empty base_ref merges the resolved sha ─────────────


def test_intake_merges_the_resolved_base_sha_when_base_ref_is_empty(
    tmp_path: Path, tmp_git_repo: Path
) -> None:
    _merge_base, head, base_tip = _seed(tmp_git_repo)
    config = DevelopConfig(
        repo=tmp_git_repo,
        description="A PR",
        work_dir=tmp_path / "work",
        acceptance_criteria="do the thing",
        base_branch=base_tip,  # what the CLI stores for --base <sha>
    )
    intake = prepare_conflict_intake(config, _change(base_tip, head, base_ref=""))
    assert intake is not None
    assert intake.base_sha == base_tip and intake.paths == ("shared.txt",)
    assert git.merge_in_progress(intake.worktree)


def test_review_context_commands_are_shell_safe_and_literal() -> None:
    """PR #364 review round 2: conflicted paths are repository-controlled and
    the reviewer is told to run the commands verbatim — every argv must be
    shell-quoted, pathspec magic disabled, shas in full, and the merge-commit
    command runnable as written."""
    import shlex

    paths = (
        "docs/My File.md",
        "weird;echo INJECTED",
        "glob*.txt",
        ":leading-colon.txt",
        "quote'd.txt",
        "dollar$(id).txt",
    )
    text = render_review_context(
        paths, head_sha="h" * 40, base_sha="b" * 40, base_ref="origin/main"
    )
    commands = [
        line.strip()
        for line in text.splitlines()
        if line.strip().startswith("git -C /workspace")
    ]
    assert len(commands) >= 3
    for cmd in commands[:2]:
        argv = shlex.split(cmd)
        assert argv[:4] == ["git", "-C", "/workspace", "--literal-pathspecs"]
        assert "--" in argv and argv[argv.index("--") + 1 :] == list(paths)
        assert ("h" * 40) in argv or ("b" * 40) in argv  # full shas, not prefixes
    assert not any("echo INJECTED" in c and ";" in shlex.split(c)[-1] for c in [])
    assert "<merge commit>" not in text and "<" not in "".join(commands)
    # the merge-commit command is one runnable line naming the PR head in full
    assert any("show --cc" in c and ("h" * 40) in c for c in commands)
