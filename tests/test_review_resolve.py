"""Tests for review-only change resolution (#154).

Range + branch forms run against a real throwaway git repo (no network); the
PR-number form stubs the ``gh`` / fetch wrappers so the test stays hermetic.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from lithos_loom.plugins.story_develop import review_resolve


def _sha(repo: Path, ref: str) -> str:
    return subprocess.run(
        ["git", "rev-parse", ref], cwd=repo, capture_output=True, text=True
    ).stdout.strip()


def _commit(repo: Path, filename: str) -> str:
    (repo / filename).write_text(f"{filename}\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", filename], cwd=repo, check=True)
    return _sha(repo, "HEAD")


# --- range form --------------------------------------------------------------


def test_resolves_explicit_ref_range(tmp_git_repo: Path) -> None:
    base = _sha(tmp_git_repo, "HEAD")
    head = _commit(tmp_git_repo, "feature.txt")

    change = review_resolve.resolve_change(tmp_git_repo, f"{base}..{head}")

    assert change.base_sha == base
    assert change.head_sha == head
    # a bare range carries no acceptance-criteria source
    assert change.title == ""
    assert change.body == ""


# --- branch form -------------------------------------------------------------


def test_resolves_local_branch_against_merge_base(tmp_git_repo: Path) -> None:
    main_tip = _sha(tmp_git_repo, "HEAD")
    subprocess.run(["git", "checkout", "-b", "feature"], cwd=tmp_git_repo, check=True)
    head = _commit(tmp_git_repo, "feature.txt")

    change = review_resolve.resolve_change(tmp_git_repo, "feature", base_branch="main")

    # base is the merge-base of main and the branch (here: the main tip)
    assert change.base_sha == main_tip
    assert change.head_sha == head
    assert change.head_ref == "feature"


def test_base_override_wins_for_branch(tmp_git_repo: Path) -> None:
    first = _sha(tmp_git_repo, "HEAD")
    second = _commit(tmp_git_repo, "second.txt")
    subprocess.run(["git", "checkout", "-b", "feature"], cwd=tmp_git_repo, check=True)
    head = _commit(tmp_git_repo, "feature.txt")

    change = review_resolve.resolve_change(
        tmp_git_repo, "feature", base_branch="main", base_override=second
    )
    assert change.base_sha == second
    assert change.head_sha == head
    assert first != second  # sanity: the override is not the default merge-base


def test_unknown_ref_raises(tmp_git_repo: Path) -> None:
    with pytest.raises(RuntimeError):
        review_resolve.resolve_change(tmp_git_repo, "no-such-ref..also-missing")


# --- PR form (gh stubbed) ----------------------------------------------------


@pytest.fixture
def stub_gh(monkeypatch: pytest.MonkeyPatch) -> list:
    fetches: list = []
    pr = {
        "baseRefOid": "b" * 40,
        "headRefOid": "h" * 40,
        "baseRefName": "main",
        "headRefName": "contributor:feature",
        "title": "Add a thing",
        "body": "This PR adds a thing.\n\n## Acceptance\n- it works",
    }
    monkeypatch.setattr(
        review_resolve, "_gh_pr_view", lambda repo, n: dict(pr, number=n)
    )
    monkeypatch.setattr(
        review_resolve, "_git_fetch", lambda repo, *refs: fetches.append(refs)
    )
    return fetches


def test_resolves_pr_number(stub_gh: list, tmp_path: Path) -> None:
    change = review_resolve.resolve_change(tmp_path, "#142")
    assert change.base_sha == "b" * 40
    assert change.head_sha == "h" * 40
    # the PR body is the default acceptance-criteria source
    assert "adds a thing" in change.body
    assert change.title == "Add a thing"
    assert "142" in change.head_ref
    # the PR head was fetched so the commit is local
    assert fetches_for(stub_gh, "142")


def test_resolves_bare_digits_as_pr(stub_gh: list, tmp_path: Path) -> None:
    change = review_resolve.resolve_change(tmp_path, "142")
    assert change.head_sha == "h" * 40


def test_resolves_pr_url(stub_gh: list, tmp_path: Path) -> None:
    change = review_resolve.resolve_change(
        tmp_path, "https://github.com/agent-lore/lithos-loom/pull/142"
    )
    assert change.head_sha == "h" * 40


def fetches_for(fetches: list, number: str) -> bool:
    return any(any(number in r for r in refs) for refs in fetches)
