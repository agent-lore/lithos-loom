"""Tests for review-only change resolution (#154).

Range + branch forms run against a real throwaway git repo (no network); the
PR-number form stubs the ``gh`` / fetch wrappers so the test stays hermetic.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from lithos_loom.github_client import PullRequest
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


def _stub_pr(number: str) -> PullRequest:
    return PullRequest(
        repo="agent-lore/lithos-loom",
        number=int(number),
        state="open",
        merged=False,
        merged_at=None,
        merge_commit_sha=None,
        head_sha="h" * 40,
        base_ref="main",
        head_ref="contributor:feature",
        title="Add a thing",
        body="This PR adds a thing.\n\n## Acceptance\n- it works",
    )


@pytest.fixture
def stub_gh(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    # ``_gh_pr_view`` now returns a typed PullRequest. The base sha is still
    # derived locally via merge-base (never from the PR object's base ref — the
    # reason #207 is moot), so the stub records the merge-base call.
    calls = SimpleNamespace(fetches=[], merge_base=[])
    monkeypatch.setattr(review_resolve, "_gh_pr_view", lambda repo, n: _stub_pr(n))
    monkeypatch.setattr(
        review_resolve, "_git_fetch", lambda repo, *refs: calls.fetches.append(refs)
    )

    def _fake_merge_base(repo: Path, a: str, b: str) -> str:
        calls.merge_base.append((a, b))
        return "m" * 40

    monkeypatch.setattr(review_resolve, "_merge_base", _fake_merge_base)
    return calls


def test_resolves_pr_number(stub_gh: SimpleNamespace, tmp_path: Path) -> None:
    change = review_resolve.resolve_change(tmp_path, "#142")
    # base is the merge-base of the base branch and the PR head — the real diff
    # base GitHub shows — derived locally, NOT from the PR object's base ref
    # (which is why #207's missing baseRefOid never mattered).
    assert change.base_sha == "m" * 40
    assert stub_gh.merge_base == [("origin/main", "h" * 40)]
    assert change.head_sha == "h" * 40
    # the PR body is the default acceptance-criteria source
    assert "adds a thing" in change.body
    assert change.title == "Add a thing"
    assert "142" in change.head_ref
    # the PR head was fetched so the commit is local
    assert fetches_for(stub_gh.fetches, "142")


def test_resolves_bare_digits_as_pr(stub_gh: SimpleNamespace, tmp_path: Path) -> None:
    change = review_resolve.resolve_change(tmp_path, "142")
    assert change.head_sha == "h" * 40


def test_resolves_pr_url(stub_gh: SimpleNamespace, tmp_path: Path) -> None:
    change = review_resolve.resolve_change(
        tmp_path, "https://github.com/agent-lore/lithos-loom/pull/142"
    )
    assert change.head_sha == "h" * 40


def fetches_for(fetches: list, number: str) -> bool:
    return any(any(number in r for r in refs) for refs in fetches)


def test_gh_pr_view_resolves_local_owner_then_fetches(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # gh pr view always resolved the PR against the LOCAL checkout; the typed
    # path preserves that — resolve owner/repo from the tree, fetch that repo's
    # PR by number (the number, not any URL, selects the PR).
    seen: dict[str, object] = {}
    monkeypatch.setattr(
        review_resolve, "repo_name_with_owner", lambda repo: "agent-lore/lithos-loom"
    )

    def fake_call(op: object) -> PullRequest:
        # Run the op against a stub client to capture (repo, number).
        class _Stub:
            async def get_pull_request(self, repo: str, number: int) -> PullRequest:
                seen["repo"], seen["number"] = repo, number
                return _stub_pr(str(number))

        import asyncio

        return asyncio.run(op(_Stub()))  # type: ignore[arg-type]

    monkeypatch.setattr(review_resolve, "github_call", fake_call)
    pr = review_resolve._gh_pr_view(tmp_path, "142")
    assert (seen["repo"], seen["number"]) == ("agent-lore/lithos-loom", 142)
    assert pr.head_sha == "h" * 40


def test_gh_pr_view_missing_pr_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(review_resolve, "repo_name_with_owner", lambda repo: "o/r")
    monkeypatch.setattr(review_resolve, "github_call", lambda op: None)
    with pytest.raises(RuntimeError, match="PR #999 not found in o/r"):
        review_resolve._gh_pr_view(tmp_path, "999")
