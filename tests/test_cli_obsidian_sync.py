"""Tests for ``lithos-loom obsidian-sync show`` (Slice 3 / macro
config-discovery helper)."""

from __future__ import annotations

import json
from pathlib import Path
from textwrap import dedent

import pytest
from typer.testing import CliRunner

from lithos_loom.main import app

runner = CliRunner()


def _write_config(
    tmp_path: Path,
    *,
    obsidian_sync_block: str | None = (
        '[obsidian_sync]\nvault_path = "{vault}"\ntasks_file = "_lithos/tasks.md"\n'
    ),
) -> Path:
    """Write a config with a configurable [obsidian_sync] block.

    Pass ``obsidian_sync_block=None`` to omit the section entirely
    (simulates a headless host that doesn't run obsidian-sync).
    """
    vault = tmp_path / "vault"
    vault.mkdir(exist_ok=True)
    base = dedent(
        """
        [orchestrator]
        agent_id = "lithos-orchestrator-test"
        lithos_url = "http://localhost:8765"
        """
    )
    if obsidian_sync_block is not None:
        base += "\n" + obsidian_sync_block.format(vault=vault)
    config_path = tmp_path / "config.toml"
    config_path.write_text(base)
    return config_path


def test_obsidian_sync_show_text_format(tmp_path: Path) -> None:
    """Default text output: ``key: value`` lines, one per field."""
    config_path = _write_config(tmp_path)
    result = runner.invoke(app, ["obsidian-sync", "show", "--config", str(config_path)])
    assert result.exit_code == 0, result.stdout
    out = result.stdout
    assert "vault_path:" in out
    assert "tasks_file: _lithos/tasks.md" in out
    assert "resolved_ttl_days:" in out
    assert "include_blocked:" in out
    assert "exclude_tags:" in out


def test_obsidian_sync_show_json_format(tmp_path: Path) -> None:
    """JSON output: single object, all fields. This is what the macro
    consumes to discover the configured ``tasks_file`` path so the
    wikilink targets the right file on hosts that customise it."""
    config_path = _write_config(tmp_path)
    result = runner.invoke(
        app,
        ["obsidian-sync", "show", "--config", str(config_path), "--format", "json"],
    )
    assert result.exit_code == 0, result.stdout
    parsed = json.loads(result.stdout)
    assert parsed["tasks_file"] == "_lithos/tasks.md"
    assert parsed["resolved_ttl_days"] == 7  # default
    assert parsed["include_blocked"] is True  # default
    assert parsed["exclude_tags"] == []  # default
    assert "vault_path" in parsed


def test_obsidian_sync_show_reflects_custom_tasks_file(tmp_path: Path) -> None:
    """The whole point: a host that sets ``tasks_file`` to a non-default
    value gets that value back from ``show``. Pins the macro's
    config-discovery contract."""
    config_path = _write_config(
        tmp_path,
        obsidian_sync_block=(
            "[obsidian_sync]\n"
            'vault_path = "{vault}"\n'
            'tasks_file = "_inbox/lithos-queue.md"\n'
        ),
    )
    result = runner.invoke(
        app,
        ["obsidian-sync", "show", "--config", str(config_path), "--format", "json"],
    )
    assert result.exit_code == 0, result.stdout
    parsed = json.loads(result.stdout)
    assert parsed["tasks_file"] == "_inbox/lithos-queue.md"


def test_obsidian_sync_show_reflects_custom_filter_knobs(tmp_path: Path) -> None:
    """Non-default ``resolved_ttl_days``, ``include_blocked``, and
    ``exclude_tags`` round-trip through ``show``."""
    config_path = _write_config(
        tmp_path,
        obsidian_sync_block=(
            "[obsidian_sync]\n"
            'vault_path = "{vault}"\n'
            "resolved_ttl_days = 30\n"
            "include_blocked = false\n"
            'exclude_tags = ["debug:trace", "noisy"]\n'
        ),
    )
    result = runner.invoke(
        app,
        ["obsidian-sync", "show", "--config", str(config_path), "--format", "json"],
    )
    assert result.exit_code == 0
    parsed = json.loads(result.stdout)
    assert parsed["resolved_ttl_days"] == 30
    assert parsed["include_blocked"] is False
    assert parsed["exclude_tags"] == ["debug:trace", "noisy"]


def test_obsidian_sync_show_errors_when_section_absent(tmp_path: Path) -> None:
    """A host without ``[obsidian_sync]`` (headless / non-vault host)
    can't answer the macro — fail with a clear stderr message and
    exit non-zero so the macro's error popup tells the operator
    they're on the wrong host."""
    config_path = _write_config(tmp_path, obsidian_sync_block=None)
    result = runner.invoke(app, ["obsidian-sync", "show", "--config", str(config_path)])
    assert result.exit_code == 1
    assert "[obsidian_sync] is not configured" in result.stderr


def test_obsidian_sync_show_unknown_format_exits_two(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path)
    result = runner.invoke(
        app,
        ["obsidian-sync", "show", "--config", str(config_path), "--format", "yaml"],
    )
    assert result.exit_code == 2
    assert "unknown --format 'yaml'" in result.stderr


def test_obsidian_sync_show_missing_config(tmp_path: Path) -> None:
    """A bogus config path exits non-zero with a clear message."""
    result = runner.invoke(
        app,
        ["obsidian-sync", "show", "--config", str(tmp_path / "nope.toml")],
    )
    assert result.exit_code != 0


def test_obsidian_sync_show_uses_env_var_when_no_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The macro relies on env-var-or-XDG config discovery in the
    Obsidian launcher session; this is the same lookup path the
    daemon uses."""
    config_path = _write_config(tmp_path)
    monkeypatch.setenv("LITHOS_LOOM_CONFIG", str(config_path))

    result = runner.invoke(app, ["obsidian-sync", "show", "--format", "json"])
    assert result.exit_code == 0
    parsed = json.loads(result.stdout)
    assert parsed["tasks_file"] == "_lithos/tasks.md"
