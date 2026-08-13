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
    # #304: the command fails closed when a reviewer resolves to no explicit
    # model; the tests provide per-tool defaults (fail-closed tests override).
    monkeypatch.setattr(
        review_cli,
        "load_tool_default_models",
        lambda: ({"codex": "gpt-test", "claude": "claude-test"}, ()),
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
    assert specs[0].model == "gpt-test"  # #304: default model applied


def test_missing_default_model_fails_closed(stubs: dict, monkeypatch) -> None:
    # #304: a reviewer with no explicit model must not reach a container —
    # the sandbox CLI's builtin default is invisible and drifts with rebuilds.
    monkeypatch.setattr(review_cli, "load_tool_default_models", lambda: ({}, ()))
    result = runner.invoke(develop_app, ["review", "#142", "--ac", "x"])
    assert result.exit_code != 0
    assert "[story_develop.default_models]" in result.output
    assert "config" not in stubs  # failed before review_change ran


def test_builtin_fallback_reviewer_gets_a_default_model(stubs: dict) -> None:
    # The `minimal` profile resolves to an empty panel; the folded-in built-in
    # reviewer is an agent invocation too, so the policy covers it (#304).
    result = runner.invoke(
        develop_app, ["review", "#142", "--ac", "x", "--profile", "minimal"]
    )
    assert result.exit_code == 0, result.output
    (spec,) = stubs["config"].reviewers
    assert spec.model is not None


def test_test_timeout_overrides_config(stubs: dict) -> None:
    # A repo whose non-integration suite exceeds the 900s default needs this
    # escape hatch for the gate floor to run to completion (issue #275).
    result = runner.invoke(
        develop_app, ["review", "#142", "--ac", "x", "--test-timeout", "2400"]
    )
    assert result.exit_code == 0, result.output
    assert stubs["config"].test_timeout == 2400


def test_test_timeout_defaults_to_900(stubs: dict) -> None:
    result = runner.invoke(develop_app, ["review", "#142", "--ac", "x"])
    assert result.exit_code == 0, result.output
    assert stubs["config"].test_timeout == 900


def test_non_positive_test_timeout_fails_closed(stubs: dict) -> None:
    result = runner.invoke(
        develop_app, ["review", "#142", "--ac", "x", "--test-timeout", "0"]
    )
    assert result.exit_code == 2  # click UsageError (BadParameter)
    assert "config" not in stubs


def test_unknown_profile_fails_closed(stubs: dict) -> None:
    result = runner.invoke(
        develop_app, ["review", "#142", "--ac", "x", "--profile", "thorogh"]
    )
    assert result.exit_code != 0
    assert "unknown profile" in result.output.lower()
    # the live review must NOT run under a silently-substituted profile
    assert "config" not in stubs


def test_check_command_override_threads_through(stubs: dict) -> None:
    # #273: repeatable `--check-command NAME=CMD` reaches config.check_commands so a
    # repo's own scoped command (e.g. `make typecheck`) beats the catalog canonical.
    result = runner.invoke(
        develop_app,
        [
            "review",
            "#142",
            "--ac",
            "x",
            "--check-command",
            "typecheck=make typecheck",
            "--check-command",
            "lint=make lint",
        ],
    )
    assert result.exit_code == 0, result.output
    assert stubs["config"].check_commands == {
        "typecheck": "make typecheck",
        "lint": "make lint",
    }


def test_check_command_defaults_to_empty(stubs: dict) -> None:
    result = runner.invoke(develop_app, ["review", "#142", "--ac", "x"])
    assert result.exit_code == 0, result.output
    assert stubs["config"].check_commands == {}


def test_test_command_threads_through(stubs: dict) -> None:
    # #273 review finding 3: the `test` check's command has a dedicated flag here (the
    # help/error text points to it), so it must actually exist and reach the config.
    result = runner.invoke(
        develop_app, ["review", "#142", "--ac", "x", "--test-command", "make test"]
    )
    assert result.exit_code == 0, result.output
    assert stubs["config"].test_command == "make test"


def test_whitespace_test_command_fails_closed(stubs: dict) -> None:
    # #278 review finding 2: a blank / whitespace --test-command would otherwise reach
    # `sh -c`, do no work, exit 0, and false-green the required test check. Reject it.
    result = runner.invoke(
        develop_app, ["review", "#142", "--ac", "x", "--test-command", "   "]
    )
    assert result.exit_code == 2
    assert "config" not in stubs


def test_check_state_override_threads_through(stubs: dict) -> None:
    # #273 slice 2: repeatable --check-state NAME=STATE reaches config.check_states.
    result = runner.invoke(
        develop_app,
        [
            "review",
            "#142",
            "--ac",
            "x",
            "--check-state",
            "sast=off",
            "--check-state",
            "lint=required",
        ],
    )
    assert result.exit_code == 0, result.output
    assert stubs["config"].check_states == {"sast": "off", "lint": "required"}


def test_check_state_defaults_to_empty(stubs: dict) -> None:
    result = runner.invoke(develop_app, ["review", "#142", "--ac", "x"])
    assert result.exit_code == 0, result.output
    assert stubs["config"].check_states == {}


def test_bad_check_state_fails_closed(stubs: dict) -> None:
    result = runner.invoke(
        develop_app, ["review", "#142", "--ac", "x", "--check-state", "sast=advisory"]
    )
    assert result.exit_code == 2
    assert "config" not in stubs


def test_parity_command_threads_through(stubs: dict) -> None:
    # #273 slice 3: --parity-command reaches config.parity_command.
    result = runner.invoke(
        develop_app, ["review", "#142", "--ac", "x", "--parity-command", "make check"]
    )
    assert result.exit_code == 0, result.output
    assert stubs["config"].parity_command == "make check"


def test_whitespace_parity_command_fails_closed(stubs: dict) -> None:
    result = runner.invoke(
        develop_app, ["review", "#142", "--ac", "x", "--parity-command", "   "]
    )
    assert result.exit_code == 2
    assert "config" not in stubs


def test_malformed_check_command_fails_closed(stubs: dict) -> None:
    # no `=` → not a NAME=COMMAND pair → fail closed before any container work.
    result = runner.invoke(
        develop_app,
        ["review", "#142", "--ac", "x", "--check-command", "make typecheck"],
    )
    assert result.exit_code == 2
    assert "config" not in stubs


def test_unknown_check_command_fails_closed(stubs: dict) -> None:
    result = runner.invoke(
        develop_app,
        ["review", "#142", "--ac", "x", "--check-command", "typcheck=make typecheck"],
    )
    assert result.exit_code == 2
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
