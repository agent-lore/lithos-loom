"""Tests for ``lithos-loom project list`` (Slice 3 / US31-forward)."""

from __future__ import annotations

import json
from pathlib import Path
from textwrap import dedent

import pytest
from typer.testing import CliRunner

from lithos_loom.main import app

runner = CliRunner()


def _write_config(tmp_path: Path, projects_toml: str) -> Path:
    """Write a minimal config with the given [projects.*] block."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        dedent(
            f"""
            [orchestrator]
            agent_id = "lithos-orchestrator-test"
            lithos_url = "http://localhost:8765"

            {projects_toml}
            """
        )
    )
    return config_path


def test_project_list_plain_format(tmp_path: Path) -> None:
    """Default text output: one slug per line, sorted alphabetically."""
    repo = tmp_path / "repo"
    repo.mkdir()
    config_path = _write_config(
        tmp_path,
        f'[projects.zeta]\nrepo = "{repo}"\n\n[projects.alpha]\nrepo = "{repo}"\n',
    )

    result = runner.invoke(app, ["project", "list", "--config", str(config_path)])
    assert result.exit_code == 0
    # Sorted alphabetically, one per line.
    assert result.stdout.strip().splitlines() == ["alpha", "zeta"]


def test_project_list_json_format(tmp_path: Path) -> None:
    """``--format json`` returns a JSON array — what the macro consumes."""
    repo = tmp_path / "repo"
    repo.mkdir()
    projects_toml = (
        f'[projects.lithos-loom]\nrepo = "{repo}"\n\n'
        f'[projects.lithos-lens]\nrepo = "{repo}"\n'
    )
    config_path = _write_config(tmp_path, projects_toml)

    result = runner.invoke(
        app, ["project", "list", "--config", str(config_path), "--format", "json"]
    )
    assert result.exit_code == 0
    assert json.loads(result.stdout.strip()) == ["lithos-lens", "lithos-loom"]


def test_project_list_empty_projects_text(tmp_path: Path) -> None:
    """No ``[projects]`` table → empty text output, exit 0."""
    config_path = _write_config(tmp_path, "# no projects")
    result = runner.invoke(app, ["project", "list", "--config", str(config_path)])
    assert result.exit_code == 0
    assert result.stdout.strip() == ""


def test_project_list_empty_projects_json(tmp_path: Path) -> None:
    """No ``[projects]`` table → empty JSON array, exit 0. The macro
    relies on this to detect the "no projects configured" branch."""
    config_path = _write_config(tmp_path, "# no projects")
    result = runner.invoke(
        app, ["project", "list", "--config", str(config_path), "--format", "json"]
    )
    assert result.exit_code == 0
    assert json.loads(result.stdout.strip()) == []


def test_project_list_unknown_format(tmp_path: Path) -> None:
    """Unknown ``--format`` exits 2 with a clear message."""
    config_path = _write_config(tmp_path, "# no projects")
    result = runner.invoke(
        app,
        ["project", "list", "--config", str(config_path), "--format", "yaml"],
    )
    assert result.exit_code == 2
    assert "unknown --format 'yaml'" in result.stderr or "yaml" in result.stderr


def test_project_list_missing_config(tmp_path: Path) -> None:
    """A bogus config path exits non-zero with a clear message."""
    result = runner.invoke(
        app,
        ["project", "list", "--config", str(tmp_path / "nope.toml")],
    )
    assert result.exit_code != 0


def test_project_list_uses_env_var_when_no_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``LITHOS_LOOM_CONFIG`` is the standard env-var seam (per
    ``load_config``); the CLI honors it when ``--config`` is omitted."""
    repo = tmp_path / "repo"
    repo.mkdir()
    config_path = _write_config(tmp_path, f'[projects.demo]\nrepo = "{repo}"\n')
    monkeypatch.setenv("LITHOS_LOOM_CONFIG", str(config_path))

    result = runner.invoke(app, ["project", "list"])
    assert result.exit_code == 0
    assert result.stdout.strip() == "demo"
