"""Tests for the ``lithos-loom develop review`` command (#154).

``review_change`` (the heavy orchestration) is stubbed; these tests cover the
CLI wiring: input routing, acceptance-criteria precedence, reviewer/profile
selection, ``--json`` output, and the exit code following ``blocking``.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from lithos_loom.cli import review as review_cli
from lithos_loom.cli.develop import develop_app
from lithos_loom.plugins.story_develop.review_report import ReviewReport
from lithos_loom.plugins.story_develop.review_resolve import ResolvedChange

runner = CliRunner()


@pytest.fixture
def stubs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict:
    captured: dict = {}

    monkeypatch.setattr(
        review_cli,
        "load_config",
        lambda config=None: SimpleNamespace(
            orchestrator=SimpleNamespace(work_dir=tmp_path / "work")
        ),
    )

    def fake_resolve(repo, spec, *, base_branch="main", base_override=None):
        captured["resolve"] = {"spec": spec, "base_override": base_override}
        return ResolvedChange(
            base_sha="b" * 40,
            head_sha="h" * 40,
            head_ref=spec,
            title="A PR title",
            body=captured.get("pr_body", ""),
        )

    monkeypatch.setattr(review_cli, "resolve_change", fake_resolve)

    def fake_review_change(
        config, change, *, reviewer_timeout=3600, keep_worktree=False
    ):
        captured["config"] = config
        captured["keep_worktree"] = keep_worktree
        return ReviewReport(
            head_ref=change.head_ref,
            base_sha=change.base_sha,
            head_sha=change.head_sha,
            profile=config.review_profile,
            reviewers=[],
            gate=[],
            blocking=captured.get("blocking", False),
        )

    monkeypatch.setattr(review_cli, "review_change", fake_review_change)
    return captured


def test_resolves_input_and_passes_ac(stubs: dict, tmp_path: Path) -> None:
    result = runner.invoke(
        develop_app, ["review", "abc..def", "--ac", "make it correct"]
    )
    assert result.exit_code == 0, result.output
    assert stubs["resolve"]["spec"] == "abc..def"
    assert stubs["config"].acceptance_criteria == "make it correct"


def test_pr_body_is_default_ac(stubs: dict) -> None:
    stubs["pr_body"] = "Fix the thing so attach waits for delivery."
    result = runner.invoke(develop_app, ["review", "#142"])
    assert result.exit_code == 0, result.output
    assert "attach waits for delivery" in stubs["config"].acceptance_criteria


def test_bare_range_without_ac_errors(stubs: dict) -> None:
    # no --ac, no PR body -> a reviewer with no criteria is useless; fail loud
    result = runner.invoke(develop_app, ["review", "abc..def"])
    assert result.exit_code != 0
    assert "acceptance" in result.output.lower()


def test_ac_file_wins(stubs: dict, tmp_path: Path) -> None:
    ac = tmp_path / "ac.md"
    ac.write_text("criteria from a file")
    result = runner.invoke(develop_app, ["review", "#142", "--ac-file", str(ac)])
    assert result.exit_code == 0, result.output
    assert stubs["config"].acceptance_criteria == "criteria from a file"


def test_reviewer_override_and_profile(stubs: dict) -> None:
    result = runner.invoke(
        develop_app,
        [
            "review",
            "#142",
            "--ac",
            "x",
            "--profile",
            "thorough",
            "--reviewer",
            "correctness",
        ],
    )
    assert result.exit_code == 0, result.output
    assert stubs["config"].review_profile == "thorough"
    # --reviewer resolves to the CANONICAL persona (codex + focus prompt),
    # not a bare generic reviewer.
    specs = stubs["config"].reviewers
    assert [r.name for r in specs] == ["correctness"]
    assert specs[0].tool == "codex"
    assert specs[0].system_prompt  # the correctness focus brief is baked in


def test_unknown_profile_fails_closed(stubs: dict) -> None:
    result = runner.invoke(
        develop_app, ["review", "#142", "--ac", "x", "--profile", "thorogh"]
    )
    assert result.exit_code != 0
    assert "unknown profile" in result.output.lower()
    # the live review must NOT run under a silently-substituted profile
    assert "config" not in stubs


def test_unknown_reviewer_fails_closed(stubs: dict) -> None:
    result = runner.invoke(
        develop_app, ["review", "#142", "--ac", "x", "--reviewer", "corectness"]
    )
    assert result.exit_code != 0
    assert "unknown reviewer" in result.output.lower()
    assert "config" not in stubs


def test_json_output_written(stubs: dict, tmp_path: Path) -> None:
    out = tmp_path / "report.json"
    result = runner.invoke(
        develop_app, ["review", "#142", "--ac", "x", "--json", str(out)]
    )
    assert result.exit_code == 0, result.output
    data = json.loads(out.read_text())
    assert data["head_ref"] == "#142"
    assert data["blocking"] is False


def test_exit_code_follows_blocking(stubs: dict) -> None:
    stubs["blocking"] = True
    result = runner.invoke(develop_app, ["review", "#142", "--ac", "x"])
    assert result.exit_code == 1


def test_keep_worktree_flag(stubs: dict) -> None:
    runner.invoke(develop_app, ["review", "#142", "--ac", "x", "--keep-worktree"])
    assert stubs["keep_worktree"] is True
