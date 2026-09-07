"""Tests for ``lithos-loom develop merge-gate`` (PRD S3, CLI half).

The resolver and the core are stubbed (the core has its own real-git tests in
``test_story_develop_merge_gate.py``); these pin the flag → config wiring,
the exit-code table, the JSON record, and the fail-closed paths.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from lithos_loom.cli import merge_gate as cli
from lithos_loom.cli.develop import develop_app
from lithos_loom.plugins.story_develop.config import DEFAULT_IMAGE
from lithos_loom.plugins.story_develop.merge_gate import (
    MergeGateCheck,
    MergeGateResult,
)
from lithos_loom.plugins.story_develop.review_resolve import ResolvedChange

runner = CliRunner()


@pytest.fixture
def stubs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict:
    captured: dict = {}
    monkeypatch.setattr(
        cli,
        "load_config",
        lambda config=None: SimpleNamespace(
            orchestrator=SimpleNamespace(work_dir=tmp_path / "work")
        ),
    )

    def fake_resolve(repo, spec, *, base_branch="main", base_override=None):
        captured["resolve"] = {"repo": repo, "spec": spec}
        return ResolvedChange(
            base_sha="b" * 40,
            head_sha="h" * 40,
            head_ref=spec,
            head_branch=captured.get("head_branch", "feature"),
            base_ref="origin/main",
            is_fork=captured.get("is_fork", False),
        )

    monkeypatch.setattr(cli, "resolve_change", fake_resolve)

    def fake_run(config, change, *, push=True, keep_worktree=False):
        captured["config"] = config
        captured["push"] = push
        captured["keep_worktree"] = keep_worktree
        status = captured.get("status", "green")
        return MergeGateResult(
            status=status,
            change=change,
            base_ref="origin/main",
            base_sha="t" * 40,
            head_sha="h" * 40,
            merge_sha="m" * 40 if status != "conflict" else "",
            behind=True,
            conflicting_paths=("a.py",) if status == "conflict" else (),
            checks=(
                MergeGateCheck(
                    name="test",
                    command="make test",
                    state="required",
                    stage="fast",
                    outcome="ran",
                    passed=status != "red",
                    exit_code=0 if status != "red" else 1,
                ),
            )
            if status in ("green", "red")
            else (),
            verdict={"green": "GREEN", "red": "RED"}.get(status),
            config_fingerprint="f" * 16,
            pushed=status == "green" and push,
            pushed_sha="m" * 40 if status == "green" and push else "",
            message=f"stubbed {status}",
        )

    monkeypatch.setattr(cli, "run_merge_gate", fake_run)
    return captured


def test_flags_reach_the_develop_config(stubs: dict, tmp_path: Path) -> None:
    result = runner.invoke(
        develop_app,
        [
            "merge-gate",
            "#7",
            "--profile",
            "thorough",
            "--check-command",
            "lint=ruff check .",
            "--check-state",
            "lint=required",
            "--test-command",
            "make test",
            "--parity-command",
            "make parity",
            "--image",
            "custom:img",
            "--test-timeout",
            "90",
            "--repo",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 0, result.output
    cfg = stubs["config"]
    assert cfg.repo == tmp_path
    assert cfg.review_profile == "thorough"
    assert cfg.check_commands == {"lint": "ruff check ."}
    assert cfg.check_states == {"lint": "required"}
    assert cfg.test_command == "make test"
    assert cfg.parity_command == "make parity"
    assert cfg.image == "custom:img"
    assert cfg.test_timeout == 90
    assert cfg.work_dir == tmp_path / "work" / "merge-gate"
    assert cfg.description == "merge-gate #7"
    assert stubs["resolve"] == {"repo": tmp_path, "spec": "#7"}
    # zero-token: no agents, so no acceptance criteria or models are demanded
    assert stubs["push"] is True and stubs["keep_worktree"] is False


def test_defaults_push_with_the_default_image(stubs: dict) -> None:
    result = runner.invoke(develop_app, ["merge-gate", "42"])
    assert result.exit_code == 0, result.output
    assert stubs["config"].image == DEFAULT_IMAGE
    assert stubs["config"].review_profile == "standard"
    assert stubs["push"] is True
    assert "pushed mmmmmmmmmmmm → feature" in result.output


def test_no_push_and_keep_worktree_are_threaded(stubs: dict) -> None:
    result = runner.invoke(
        develop_app, ["merge-gate", "42", "--no-push", "--keep-worktree"]
    )
    assert result.exit_code == 0, result.output
    assert stubs["push"] is False and stubs["keep_worktree"] is True


@pytest.mark.parametrize(
    ("status", "code"),
    [
        ("green", 0),
        ("no_checks", 0),
        ("red", 1),
        ("errored", 1),
        ("conflict", 3),
        ("fork_unsupported", 2),
    ],
)
def test_exit_code_follows_the_gate_verdict(
    stubs: dict, status: str, code: int
) -> None:
    stubs["status"] = status
    result = runner.invoke(develop_app, ["merge-gate", "42"])
    assert result.exit_code == code, result.output
    assert f"merge-gate 42: {status}" in result.output


def test_conflict_output_names_the_paths(stubs: dict) -> None:
    stubs["status"] = "conflict"
    result = runner.invoke(develop_app, ["merge-gate", "42"])
    assert result.exit_code == 3
    assert "conflict: a.py" in result.output


def test_json_record_is_written(stubs: dict, tmp_path: Path) -> None:
    out = tmp_path / "nested" / "mg.json"
    result = runner.invoke(develop_app, ["merge-gate", "42", "--json", str(out)])
    assert result.exit_code == 0, result.output
    data = json.loads(out.read_text())
    assert data["status"] == "green"
    assert data["pushed"] is True and data["pushed_sha"] == "m" * 40
    assert data["config_fingerprint"] == "f" * 16
    assert data["checks"][0]["name"] == "test"
    assert data["conflicting_paths"] == []


# ── fail closed ──────────────────────────────────────────────────────────────


def test_range_spec_without_a_pr_is_rejected(stubs: dict) -> None:
    stubs["head_branch"] = ""
    result = runner.invoke(develop_app, ["merge-gate", "abc..def"])
    assert result.exit_code == 2
    assert "config" not in stubs  # the core never ran


def test_unknown_profile_is_rejected_before_resolving(stubs: dict) -> None:
    result = runner.invoke(develop_app, ["merge-gate", "42", "--profile", "nope"])
    assert result.exit_code == 2
    assert "resolve" not in stubs


def test_blank_test_command_is_rejected(stubs: dict) -> None:
    result = runner.invoke(develop_app, ["merge-gate", "42", "--test-command", "  "])
    assert result.exit_code == 2
    assert "resolve" not in stubs


def test_zero_timeout_is_rejected(stubs: dict) -> None:
    result = runner.invoke(develop_app, ["merge-gate", "42", "--test-timeout", "0"])
    assert result.exit_code == 2
    assert "resolve" not in stubs
