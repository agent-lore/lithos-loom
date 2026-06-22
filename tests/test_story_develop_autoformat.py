"""Tests for the auto-format-before-review pass (#134, ADR 0003 §4).

The formatter runs in the sandbox immediately after the coder's commit; any change
it makes is a SEPARATE commit on the round, and the gate + reviewers then see that
exact formatted tree. The pass is best-effort — an absent/erroring formatter is
skipped, never fatal. The container run is monkeypatched (no Docker): the fake
formatter mutates worktree files in place so real ``git`` captures the diff.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from lithos_loom.plugins.story_develop import autoformat, test_gate
from lithos_loom.plugins.story_develop.config import DevelopConfig
from lithos_loom.plugins.story_develop.test_gate import GateResult


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def config(tmp_git_repo: Path, tmp_path: Path) -> DevelopConfig:
    cfg_dir = tmp_path / "fake-claude"
    cfg_dir.mkdir()
    return DevelopConfig(
        repo=tmp_git_repo,
        description="Add a greeting file",
        work_dir=tmp_path / "work",
        claude_config_dir=cfg_dir,
    )


# --- resolve_formatters: detection + image probe -----------------------------


def test_resolve_formatters_python_present(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig, tmp_git_repo: Path
) -> None:
    (tmp_git_repo / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    monkeypatch.setattr(test_gate, "probe_tools", lambda image, tools: ["ruff"])
    assert autoformat.resolve_formatters(config, tmp_git_repo) == ["ruff format"]


def test_resolve_formatters_markerless_repo_skips_probe(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig, tmp_git_repo: Path
) -> None:
    # No ecosystem marker -> no formatter, and the image is never probed.
    def _no_probe(image: str, tools: list[str]) -> list[str]:
        raise AssertionError("must not probe when there is nothing to format")

    monkeypatch.setattr(test_gate, "probe_tools", _no_probe)
    assert autoformat.resolve_formatters(config, tmp_git_repo) == []


def test_resolve_formatters_drops_absent_tool(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig, tmp_git_repo: Path
) -> None:
    # ruff applies but is absent from the image -> dropped (the pass is a no-op,
    # not a blocking placeholder — formatting is best-effort).
    (tmp_git_repo / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    monkeypatch.setattr(test_gate, "probe_tools", lambda image, tools: [])
    assert autoformat.resolve_formatters(config, tmp_git_repo) == []


# --- run_format_pass: separate commit on change ------------------------------


def test_run_format_pass_commits_reformatted_tree(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig, tmp_git_repo: Path
) -> None:
    wt = tmp_git_repo
    (wt / "greeting.txt").write_text("unformatted\n")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-m", "coder round commit")
    head_before = _git(wt, "rev-parse", "HEAD")

    def _fake_run(gate_cmd, *, name, command, timeout) -> GateResult:
        # A real formatter rewrites source in place; simulate that here.
        (wt / "greeting.txt").write_text("formatted\n")
        return GateResult(command=command, exit_code=0, passed=True, output_tail="")

    monkeypatch.setattr(test_gate, "run_gate_container", _fake_run)

    sha = autoformat.run_format_pass(config, wt, round_no=1, formatters=["ruff format"])

    assert sha is not None and sha != head_before
    assert _git(wt, "rev-parse", "HEAD") == sha
    # The formatting is its OWN commit on top of the coder's, not a rewrite of it.
    assert _git(wt, "rev-parse", "HEAD~1") == head_before
    assert "auto-format" in _git(wt, "log", "-1", "--format=%s")
    assert (wt / "greeting.txt").read_text() == "formatted\n"


def test_run_format_pass_noop_when_already_clean(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig, tmp_git_repo: Path
) -> None:
    wt = tmp_git_repo
    head_before = _git(wt, "rev-parse", "HEAD")

    # Formatter runs but changes nothing -> no commit, no SHA.
    monkeypatch.setattr(
        test_gate,
        "run_gate_container",
        lambda *a, command, **k: GateResult(
            command=command, exit_code=0, passed=True, output_tail=""
        ),
    )

    sha = autoformat.run_format_pass(config, wt, round_no=1, formatters=["ruff format"])

    assert sha is None
    assert _git(wt, "rev-parse", "HEAD") == head_before


def test_run_format_pass_no_formatters_runs_no_container(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig, tmp_git_repo: Path
) -> None:
    def _boom(*a, **k):
        raise AssertionError("must not run a container with no formatters")

    monkeypatch.setattr(test_gate, "run_gate_container", _boom)
    assert autoformat.run_format_pass(config, tmp_git_repo, 1, []) is None


def test_run_format_pass_container_error_is_skipped(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig, tmp_git_repo: Path
) -> None:
    # An infra failure (e.g. Docker down) must not crash the run — formatting is
    # best-effort; the pass returns None and the round proceeds unformatted.
    def _raise(*a, **k) -> GateResult:
        raise RuntimeError("simulated docker failure")

    monkeypatch.setattr(test_gate, "run_gate_container", _raise)
    head_before = _git(tmp_git_repo, "rev-parse", "HEAD")

    sha = autoformat.run_format_pass(config, tmp_git_repo, 1, ["ruff format"])

    assert sha is None
    assert _git(tmp_git_repo, "rev-parse", "HEAD") == head_before
