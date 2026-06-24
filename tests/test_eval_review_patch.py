"""Tests for patch-based eval case head materialisation (#193).

The git-real tests use the ``tmp_git_repo`` fixture; none run an agent/docker, so
this stays in ``make check``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from lithos_loom.evals.review import patch
from lithos_loom.evals.review.case import Case, Expected
from lithos_loom.runner import git, worktree

_EXPECTED = Expected(file="x.py", keywords=("bug",), min_severity="critical")


def _case(tmp_git_repo: Path, case_dir: Path, base: str, **kw) -> Case:
    return Case(
        id="194-x",
        description="",
        repo=str(tmp_git_repo),
        base=base,
        head=kw.pop("head", ""),
        acceptance_criteria="ac",
        personas=("correctness",),
        profile="standard",
        expected=(_EXPECTED,),
        case_dir=case_dir,
        **kw,
    )


def _seed_tracked_file(repo: Path) -> str:
    (repo / "mod.py").write_text("ok = True\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "seed"], cwd=repo, check=True, capture_output=True
    )
    return git.base_sha(repo)


def _patch_editing_mod(
    repo: Path, dest: Path, new: str = "ok = False  # BUG\n"
) -> Path:
    (repo / "mod.py").write_text(new)
    diff = subprocess.run(
        ["git", "diff"], cwd=repo, capture_output=True, text=True
    ).stdout
    subprocess.run(
        ["git", "checkout", "--", "."], cwd=repo, check=True, capture_output=True
    )
    dest.write_text(diff)
    return dest


def test_materialise_patch_heads_is_identity_for_a_sha_case(tmp_path: Path) -> None:
    # a sha-based case needs no git work: identity + a no-op cleanup.
    case = Case(
        id="c",
        description="",
        repo=".",
        base="aaaa",
        head="bbbb",
        acceptance_criteria="ac",
        personas=("correctness",),
        profile="standard",
        expected=(_EXPECTED,),
        case_dir=tmp_path,
    )
    out, cleanup = patch.materialise_patch_heads(case)
    assert out is case
    cleanup()  # must not raise


def test_materialise_patch_heads_resolves_a_patch_head_and_cleans_up(
    tmp_git_repo: Path, tmp_path: Path
) -> None:
    base = _seed_tracked_file(tmp_git_repo)
    case_dir = tmp_path / "case"
    case_dir.mkdir()
    _patch_editing_mod(tmp_git_repo, case_dir / "head.patch")
    case = _case(tmp_git_repo, case_dir, base, head_patch="head.patch")

    out, cleanup = patch.materialise_patch_heads(case)
    try:
        assert out.head and out.head != base  # head is now an ephemeral sha
        assert out.head_patch == "head.patch"  # the original spec is preserved
        # the ephemeral commit is base + the patch, and is reachable...
        diff = subprocess.run(
            ["git", "diff", f"{base}..{out.head}"],
            cwd=tmp_git_repo,
            capture_output=True,
            text=True,
        ).stdout
        assert "BUG" in diff
        # ...reachable enough that a review worktree can be created at it.
        wt = worktree.create_at(tmp_git_repo, out.head, "probe", parent=tmp_path / "wt")
        worktree.remove(wt, force=True)
    finally:
        cleanup()


def test_materialise_patch_heads_resolves_known_good_patch(
    tmp_git_repo: Path, tmp_path: Path
) -> None:
    base = _seed_tracked_file(tmp_git_repo)
    case_dir = tmp_path / "case"
    case_dir.mkdir()
    _patch_editing_mod(tmp_git_repo, case_dir / "head.patch")
    _patch_editing_mod(
        tmp_git_repo, case_dir / "clean.patch", new="ok = True  # tidy\n"
    )
    case = _case(
        tmp_git_repo,
        case_dir,
        base,
        head_patch="head.patch",
        known_good_head_patch="clean.patch",
    )

    out, cleanup = patch.materialise_patch_heads(case)
    try:
        assert out.head and out.known_good_head
        assert out.head != out.known_good_head
    finally:
        cleanup()


def test_materialise_patch_heads_works_with_a_relative_case_dir(
    tmp_git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The shipped cases pass a case_dir RELATIVE to the launch cwd
    # (`evals/review/cases/<id>`); `git apply` runs with cwd=build-worktree, so the
    # patch path must be made absolute or it's not found (a live-eval regression).
    base = _seed_tracked_file(tmp_git_repo)
    (tmp_path / "case").mkdir()
    _patch_editing_mod(tmp_git_repo, tmp_path / "case" / "head.patch")
    monkeypatch.chdir(tmp_path)
    case = _case(tmp_git_repo, Path("case"), base, head_patch="head.patch")

    out, cleanup = patch.materialise_patch_heads(case)
    try:
        assert out.head and out.head != base
    finally:
        cleanup()


def test_materialise_patched_head_raises_when_patch_nets_no_change(
    tmp_git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # a patch that applies but changes nothing must NOT silently make head == base.
    base = git.base_sha(tmp_git_repo)
    monkeypatch.setattr(patch.git, "apply_patch", lambda wt, p: None)  # apply nothing
    with pytest.raises(ValueError, match="no change"):
        patch._materialise_patched_head(
            tmp_git_repo, base, tmp_path / "x.patch", parent=tmp_path / "p"
        )


def test_materialise_patched_head_raises_on_unapplyable_patch(
    tmp_git_repo: Path, tmp_path: Path
) -> None:
    base = git.base_sha(tmp_git_repo)
    bad = tmp_path / "bad.patch"
    bad.write_text("--- a/nope.txt\n+++ b/nope.txt\n@@ -1 +1 @@\n-x\n+y\n")
    with pytest.raises(RuntimeError):
        patch._materialise_patched_head(tmp_git_repo, base, bad, parent=tmp_path / "p")
