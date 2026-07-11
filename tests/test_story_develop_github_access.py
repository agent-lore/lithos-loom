"""Tests for the story-develop GitHub access bridge (ARCH-7c).

``github_call`` bridges the async typed GitHubClient into the sync plugin core;
``repo_name_with_owner`` is the shared gh-CLI convenience. The typed methods
themselves are respx-tested in test_github_client.py — here we pin the bridge
(runs an op, returns its result, propagates typed errors) and the convenience.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import respx

from lithos_loom import github_client as gh_mod
from lithos_loom.github_client import GitHubError
from lithos_loom.plugins.story_develop import github_access


async def _fake_token() -> str:
    return "tok"


@respx.mock
def test_github_call_runs_op_against_typed_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Patch token resolution so create() doesn't shell out to ``gh auth token``.
    monkeypatch.setattr(gh_mod, "_resolve_gh_token", _fake_token)
    respx.get("https://api.github.com/repos/o/r/pulls/1").mock(
        return_value=httpx.Response(
            200,
            json={"number": 1, "state": "open", "merged": False, "merged_at": None},
        )
    )
    pr = github_access.github_call(lambda c: c.get_pull_request("o/r", 1))
    assert pr is not None and pr.number == 1


@respx.mock
def test_github_call_propagates_typed_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gh_mod, "_resolve_gh_token", _fake_token)
    respx.post("https://api.github.com/repos/o/r/issues/1/comments").mock(
        return_value=httpx.Response(422, json={"message": "boom"})
    )
    with pytest.raises(GitHubError, match="boom"):
        github_access.github_call(lambda c: c.create_issue_comment("o/r", 1, "hi"))


def test_repo_name_with_owner_returns_slug(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fake_run(*args: object, **kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            returncode=0, stdout="agent-lore/lithos-loom\n", stderr=""
        )

    monkeypatch.setattr(github_access.subprocess, "run", fake_run)
    assert github_access.repo_name_with_owner(tmp_path) == "agent-lore/lithos-loom"


def test_repo_name_with_owner_raises_on_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fake_run(*args: object, **kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(returncode=1, stdout="", stderr="no remote")

    monkeypatch.setattr(github_access.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="gh repo view failed"):
        github_access.repo_name_with_owner(tmp_path)
