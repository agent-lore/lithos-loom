"""Tests for the ``lithos-loom develop converge`` command (converge PR 3/3).

``converge_pr`` (the heavy orchestration) is stubbed; these tests cover the CLI
wiring: PR resolution, the non-PR rejection, acceptance-criteria precedence,
reviewer/profile/coder selection, ``--no-push`` / ``--max-rounds`` threading,
``--json`` output, and the exit code following the converge status.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from lithos_loom.cli import converge as converge_cli
from lithos_loom.cli.develop import develop_app
from lithos_loom.plugins.story_develop.converge import ConvergeResult
from lithos_loom.plugins.story_develop.review_resolve import ResolvedChange

runner = CliRunner()


@pytest.fixture
def stubs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict:
    captured: dict = {}

    monkeypatch.setattr(
        converge_cli,
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
            body=captured.get("pr_body", "the intent"),
            head_branch=captured.get("head_branch", "feature"),
            is_fork=captured.get("is_fork", False),
        )

    monkeypatch.setattr(converge_cli, "resolve_change", fake_resolve)

    def fake_converge_pr(config, change, *, no_push=False):
        captured["config"] = config
        captured["no_push"] = no_push
        return ConvergeResult(
            status=captured.get("status", "converged"),
            change=change,
            fixer_commits=("fix1",),
            pushed=not no_push,
            pushed_sha="p" * 40,
            message="converged and pushed to feature",
        )

    monkeypatch.setattr(converge_cli, "converge_pr", fake_converge_pr)
    return captured


def test_resolves_pr_and_passes_ac(stubs: dict) -> None:
    result = runner.invoke(develop_app, ["converge", "#142", "--ac", "make it correct"])
    assert result.exit_code == 0, result.output
    assert stubs["resolve"]["spec"] == "#142"
    assert stubs["config"].acceptance_criteria == "make it correct"


def test_pr_body_is_default_ac(stubs: dict) -> None:
    stubs["pr_body"] = "Fix the leak so the handle closes on error."
    result = runner.invoke(develop_app, ["converge", "#142"])
    assert result.exit_code == 0, result.output
    assert "handle closes on error" in stubs["config"].acceptance_criteria


def test_non_pr_spec_is_rejected(stubs: dict) -> None:
    # a range / branch has no pushable head branch — converge pushes to a PR.
    stubs["head_branch"] = ""
    result = runner.invoke(develop_app, ["converge", "abc..def", "--ac", "x"])
    assert result.exit_code != 0
    assert "requires a pr" in result.output.lower()
    assert "config" not in stubs  # never entered the orchestrator


def test_no_push_flag_threads_through(stubs: dict) -> None:
    result = runner.invoke(develop_app, ["converge", "#142", "--ac", "x", "--no-push"])
    assert result.exit_code == 0, result.output
    assert stubs["no_push"] is True


def test_coder_and_max_rounds_override_config(stubs: dict) -> None:
    result = runner.invoke(
        develop_app,
        ["converge", "#142", "--ac", "x", "--coder", "codex", "--max-rounds", "3"],
    )
    assert result.exit_code == 0, result.output
    assert stubs["config"].coder == "codex"
    assert stubs["config"].max_rounds == 3


def test_unsupported_coder_fails_closed(stubs: dict) -> None:
    result = runner.invoke(
        develop_app, ["converge", "#142", "--ac", "x", "--coder", "gpt5"]
    )
    assert result.exit_code != 0
    assert "unsupported coder" in result.output.lower()
    assert "config" not in stubs


def test_non_positive_max_cost_fails_closed(stubs: dict) -> None:
    # validated before any container work — a nonsensical ceiling must fail fast
    result = runner.invoke(
        develop_app, ["converge", "#142", "--ac", "x", "--max-cost", "0"]
    )
    assert result.exit_code != 0
    assert "max-cost" in result.output.lower()
    assert "config" not in stubs  # never entered the orchestrator


def test_max_rounds_below_one_fails_closed(stubs: dict) -> None:
    result = runner.invoke(
        develop_app, ["converge", "#142", "--ac", "x", "--max-rounds", "0"]
    )
    assert result.exit_code != 0
    assert "max-rounds" in result.output.lower()
    assert "config" not in stubs


def test_unknown_profile_fails_closed(stubs: dict) -> None:
    result = runner.invoke(
        develop_app, ["converge", "#142", "--ac", "x", "--profile", "thorogh"]
    )
    assert result.exit_code != 0
    assert "unknown profile" in result.output.lower()
    assert "config" not in stubs


def test_reviewer_override_and_profile(stubs: dict) -> None:
    result = runner.invoke(
        develop_app,
        [
            "converge",
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
    specs = stubs["config"].reviewers
    assert [r.name for r in specs] == ["correctness"]
    assert specs[0].tool == "codex"


def test_json_summary_written(stubs: dict, tmp_path: Path) -> None:
    out = tmp_path / "converge.json"
    result = runner.invoke(
        develop_app, ["converge", "#142", "--ac", "x", "--json", str(out)]
    )
    assert result.exit_code == 0, result.output
    data = json.loads(out.read_text())
    assert data["status"] == "converged"
    assert data["head_branch"] == "feature"
    assert data["pushed"] is True


@pytest.mark.parametrize(
    "status,code",
    [
        ("already_clean", 0),
        ("converged", 0),
        ("not_converged", 1),
        ("merge_race", 1),
        ("failed", 1),
        ("fork_unsupported", 2),
    ],
)
def test_exit_code_follows_status(stubs: dict, status: str, code: int) -> None:
    stubs["status"] = status
    result = runner.invoke(develop_app, ["converge", "#142", "--ac", "x"])
    assert result.exit_code == code, result.output
