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

    def fake_resolve(repo, spec, *, base_branch="main", base_override=None, **kw):
        captured["resolve"] = {"repo": repo, "spec": spec, **kw}
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
    assert stubs["resolve"] == {"repo": tmp_path, "spec": "#7", "allow_fork": False}
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


# ── PR #360 review F3 + F5 ───────────────────────────────────────────────────


def test_forks_are_refused_at_resolve_time_without_a_fetch(
    stubs: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict = {}

    def fake_resolve(repo, spec, *, base_branch="main", base_override=None, **kw):
        captured["allow_fork"] = kw.get("allow_fork", True)
        return ResolvedChange(
            base_sha="", head_sha="h" * 40, head_ref=spec, head_branch="f", is_fork=True
        )

    monkeypatch.setattr(cli, "resolve_change", fake_resolve)
    stubs["status"] = "fork_unsupported"
    result = runner.invoke(develop_app, ["merge-gate", "42"])
    assert result.exit_code == 2, result.output
    assert captured["allow_fork"] is False


@pytest.fixture
def story_stubs(stubs: dict, monkeypatch: pytest.MonkeyPatch) -> dict:
    from lithos_loom.cli import review as review_cli
    from lithos_loom.plugins.story_develop.daemon_io import ProjectDevelopSettings

    monkeypatch.setattr(
        review_cli,
        "fetch_task_metadata",
        lambda url, task_id: (
            stubs.setdefault("fetched", []).append((url, task_id)) or "T",
            {"project": "lens", "develop_review_profile": "thorough"},
        ),
    )
    monkeypatch.setattr(
        review_cli,
        "resolve_project_settings",
        lambda url, meta: ProjectDevelopSettings(
            image="ralph-sandbox:lens",
            test_command="make check",
            parity_command="make parity",
            check_states={"lint": "informational"},
        ),
    )
    monkeypatch.setattr(
        cli,
        "load_config",
        lambda config=None: SimpleNamespace(
            orchestrator=SimpleNamespace(
                work_dir=Path("/tmp/w"), lithos_url="http://lithos.test"
            ),
            story_develop=SimpleNamespace(
                default_models={},
                default_review_profile="standard",
                unknown_profile="halt",
            ),
        ),
    )
    return stubs


def test_story_resolves_the_projects_current_check_set(story_stubs: dict) -> None:
    result = runner.invoke(develop_app, ["merge-gate", "42", "--story", "story-9"])
    assert result.exit_code == 0, result.output
    assert story_stubs["fetched"] == [("http://lithos.test", "story-9")]
    cfg = story_stubs["config"]
    assert cfg.review_profile == "thorough"  # the task's profile
    assert cfg.image == "ralph-sandbox:lens"
    assert cfg.test_command == "make check"
    assert cfg.parity_command == "make parity"
    assert cfg.check_states == {"lint": "informational"}


def test_explicit_flags_win_over_the_story_for_merge_gate(story_stubs: dict) -> None:
    result = runner.invoke(
        develop_app,
        [
            "merge-gate",
            "42",
            "--story",
            "story-9",
            "--profile",
            "minimal",
            "--image",
            "custom:img",
            "--test-command",
            "make test",
        ],
    )
    assert result.exit_code == 0, result.output
    cfg = story_stubs["config"]
    assert cfg.review_profile == "minimal"
    assert cfg.image == "custom:img"
    assert cfg.test_command == "make test"
    assert cfg.parity_command == "make parity"  # untouched story value survives


def test_without_story_the_host_default_profile_applies(
    stubs: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        cli,
        "load_config",
        lambda config=None: SimpleNamespace(
            orchestrator=SimpleNamespace(work_dir=Path("/tmp/w")),
            story_develop=SimpleNamespace(
                default_models={},
                default_review_profile="thorough",
                unknown_profile="halt",
            ),
        ),
    )
    result = runner.invoke(develop_app, ["merge-gate", "42"])
    assert result.exit_code == 0, result.output
    assert stubs["config"].review_profile == "thorough"


# ── PR #360 re-review ────────────────────────────────────────────────────────


def _degraded(*frictions: str):
    from lithos_loom.plugins.story_develop.daemon_io import ProjectDevelopSettings

    return ProjectDevelopSettings(frictions=tuple(frictions), degraded=True)


@pytest.mark.parametrize(
    "friction",
    [
        "task has no metadata.project slug; using built-in develop defaults",
        "no project-context doc for 'lens'; using built-in develop defaults",
        "cannot read project-context doc for 'lens' (boom); "
        + "using built-in develop defaults",
    ],
)
def test_story_with_unresolvable_project_never_gates(
    story_stubs: dict, monkeypatch: pytest.MonkeyPatch, friction: str
) -> None:
    # F1: S3 gates with the project's CURRENT config or not at all — the
    # daemon's fail-open degrade to built-ins (right for a run a reviewer
    # attends) would gate and PUSH on a check-set known not to be the
    # project's. A gate with no resolvable project is skipped loudly.
    from lithos_loom.cli import review as review_cli

    monkeypatch.setattr(
        review_cli, "resolve_project_settings", lambda url, meta: _degraded(friction)
    )
    result = runner.invoke(develop_app, ["merge-gate", "42", "--story", "story-9"])
    assert result.exit_code == 4, result.output
    assert "config" not in story_stubs  # the core never ran
    assert "merge-gate 42: config_unresolved" in result.output
    assert "using built-in develop defaults" in result.output


def test_story_with_a_malformed_gate_setting_never_gates(
    story_stubs: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lithos_loom.cli import review as review_cli
    from lithos_loom.plugins.story_develop.daemon_io import ProjectDevelopSettings

    monkeypatch.setattr(
        review_cli,
        "resolve_project_settings",
        lambda url, meta: ProjectDevelopSettings(
            frictions=("develop_parity_command is not a string; ignoring",)
        ),
    )
    result = runner.invoke(develop_app, ["merge-gate", "42", "--story", "story-9"])
    assert result.exit_code == 4, result.output
    assert "config" not in story_stubs


def test_host_profile_goes_through_the_unknown_profile_policy(
    stubs: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    # F3: an unknown host default under "strongest" selects thorough; under
    # "halt" it stops before any git work.
    def host(policy: str):
        return lambda config=None: SimpleNamespace(
            orchestrator=SimpleNamespace(work_dir=Path("/tmp/w")),
            story_develop=SimpleNamespace(
                default_models={}, default_review_profile="nope", unknown_profile=policy
            ),
        )

    monkeypatch.setattr(cli, "load_config", host("strongest"))
    result = runner.invoke(develop_app, ["merge-gate", "42"])
    assert result.exit_code == 0, result.output
    assert stubs["config"].review_profile == "thorough"

    stubs.pop("config")
    stubs.pop("resolve")
    monkeypatch.setattr(cli, "load_config", host("halt"))
    result = runner.invoke(develop_app, ["merge-gate", "42"])
    assert result.exit_code == 2, result.output
    assert "config" not in stubs and "resolve" not in stubs


def test_blank_image_is_rejected(stubs: dict) -> None:
    # F5: `--image ""` must fail closed like every other blank value, never
    # fall through to the story / default image.
    result = runner.invoke(develop_app, ["merge-gate", "42", "--image", ""])
    assert result.exit_code == 2
    assert "resolve" not in stubs


# ── PR #360 re-review 2: strict means GATE config, not every friction ────────


def _story_settings(monkeypatch: pytest.MonkeyPatch, **fields):
    from lithos_loom.cli import review as review_cli
    from lithos_loom.plugins.story_develop.daemon_io import ProjectDevelopSettings

    monkeypatch.setattr(
        review_cli,
        "resolve_project_settings",
        lambda url, meta: ProjectDevelopSettings(**fields),
    )


def test_minimal_profile_gates_even_though_its_panel_note_is_a_friction(
    story_stubs: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    # the REAL layering adds "gate-only (no panel)" for minimal — an agent
    # note; merge-gate runs no panel and the check-set is fully known
    from lithos_loom.cli import review as review_cli

    monkeypatch.setattr(
        review_cli, "fetch_task_metadata", lambda url, task_id: ("T", {"project": "p"})
    )
    _story_settings(monkeypatch, review_profile_project="minimal")
    result = runner.invoke(develop_app, ["merge-gate", "42", "--story", "story-9"])
    assert result.exit_code == 0, result.output
    assert story_stubs["config"].review_profile == "minimal"


def test_strongest_policy_fallback_gates_with_the_strongest_profile(
    story_stubs: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lithos_loom.cli import review as review_cli

    monkeypatch.setattr(
        review_cli, "fetch_task_metadata", lambda url, task_id: ("T", {"project": "p"})
    )
    monkeypatch.setattr(
        cli,
        "load_config",
        lambda config=None: SimpleNamespace(
            orchestrator=SimpleNamespace(
                work_dir=Path("/tmp/w"), lithos_url="http://lithos.test"
            ),
            story_develop=SimpleNamespace(
                default_models={},
                default_review_profile="standard",
                unknown_profile="strongest",
            ),
        ),
    )
    _story_settings(monkeypatch, review_profile_project="nope")
    result = runner.invoke(develop_app, ["merge-gate", "42", "--story", "story-9"])
    assert result.exit_code == 0, result.output
    assert story_stubs["config"].review_profile == "thorough"


def test_a_non_gate_friction_does_not_skip_the_gate(
    story_stubs: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    # a rejected coder / rounds value changes nothing a gate runs
    _story_settings(
        monkeypatch,
        image="ralph-sandbox:lens",
        frictions=(
            "develop_coder must be an object with optional tool/model/effort; ignoring",
            "develop_max_rounds 'x' invalid; ignoring",
        ),
    )
    result = runner.invoke(develop_app, ["merge-gate", "42", "--story", "story-9"])
    assert result.exit_code == 0, result.output
    assert story_stubs["config"].image == "ralph-sandbox:lens"


@pytest.mark.parametrize(
    "friction",
    [
        "develop_check_states: state for check 'lint' must be one of informational, "
        + "off, required (got 'bogus'); ignoring",
        "develop_image: image must be a non-empty string (got '  '); ignoring",
        "task metadata.develop_test_command: must be a non-empty string; "
        + "keeping project default",
    ],
)
def test_a_rejected_gate_setting_skips_the_gate(
    story_stubs: dict, monkeypatch: pytest.MonkeyPatch, friction: str
) -> None:
    _story_settings(monkeypatch, frictions=(friction,))
    result = runner.invoke(develop_app, ["merge-gate", "42", "--story", "story-9"])
    assert result.exit_code == 4, result.output
    assert "config" not in story_stubs
    assert friction.split(";")[0][:30] in result.output
