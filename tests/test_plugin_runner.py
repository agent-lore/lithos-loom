"""Tests for the atomic-write helper and subprocess plugin runner."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from textwrap import dedent

import pytest

from lithos_loom.errors import PluginContractError
from lithos_loom.plugin_runner import run_plugin, write_result_atomically

# ── write_result_atomically ────────────────────────────────────────────


def test_write_result_atomically_round_trips(tmp_path: Path) -> None:
    target = tmp_path / "result.json"
    payload = {"schema_version": 1, "task_id": "t1", "status": "succeeded"}
    write_result_atomically(target, payload)
    assert target.exists()
    assert json.loads(target.read_text()) == payload


def test_write_result_atomically_creates_parent_dirs(tmp_path: Path) -> None:
    target = tmp_path / "deep" / "nested" / "result.json"
    write_result_atomically(target, {"k": "v"})
    assert target.exists()


def test_write_result_atomically_overwrites_existing(tmp_path: Path) -> None:
    target = tmp_path / "result.json"
    write_result_atomically(target, {"k": "v1"})
    write_result_atomically(target, {"k": "v2"})
    assert json.loads(target.read_text()) == {"k": "v2"}


# ── run_plugin (subprocess + token sub + result.json) ─────────────────


_PLUGIN_PRELUDE = (
    "import sys, json, argparse\n"
    "parser = argparse.ArgumentParser()\n"
    "parser.add_argument('--task-json')\n"
    "parser.add_argument('--work-dir')\n"
    "parser.add_argument('--result-file')\n"
    "args = parser.parse_args()\n"
)


def _write_fake_plugin(path: Path, *, body: str) -> Path:
    """Write a Python file usable as a plugin entry. Returns the path."""
    path.write_text(_PLUGIN_PRELUDE + dedent(body))
    return path


async def test_run_plugin_substitutes_tokens_and_returns_result(
    tmp_path: Path,
) -> None:
    plugin = _write_fake_plugin(
        tmp_path / "plugin.py",
        body=dedent(
            """\
            payload = {
                "schema_version": 1,
                "task_id": "task-77",
                "status": "succeeded",
                "exit_code": 0,
            }
            with open(args.result_file, "w") as fh:
                json.dump(payload, fh)
            sys.exit(0)
            """
        ),
    )
    work_dir = tmp_path / "wd"
    work_dir.mkdir()
    result_file = work_dir / "result.json"
    task_json = work_dir / "task.json"
    task_json.write_text("{}")

    result = await run_plugin(
        command=(
            f"{sys.executable} {plugin} --task-json {{{{task_json}}}} "
            "--work-dir {{work_dir}} --result-file {{result_file}}"
        ),
        task_json_path=task_json,
        work_dir=work_dir,
        result_file=result_file,
    )
    assert result["task_id"] == "task-77"
    assert result["status"] == "succeeded"
    assert result["exit_code"] == 0


async def test_run_plugin_validates_result_against_schema(tmp_path: Path) -> None:
    plugin = _write_fake_plugin(
        tmp_path / "plugin.py",
        body=dedent(
            """\
            payload = {"schema_version": 999, "missing": "fields"}
            with open(args.result_file, "w") as fh:
                json.dump(payload, fh)
            sys.exit(0)
            """
        ),
    )
    work_dir = tmp_path / "wd"
    work_dir.mkdir()
    with pytest.raises(PluginContractError):
        await run_plugin(
            command=(
                f"{sys.executable} {plugin} "
                "--task-json {{task_json}} --work-dir {{work_dir}} "
                "--result-file {{result_file}}"
            ),
            task_json_path=work_dir / "task.json",
            work_dir=work_dir,
            result_file=work_dir / "result.json",
        )


async def test_run_plugin_raises_when_result_file_missing(tmp_path: Path) -> None:
    plugin = _write_fake_plugin(
        tmp_path / "plugin.py",
        body="sys.exit(0)",  # never writes result.json
    )
    work_dir = tmp_path / "wd"
    work_dir.mkdir()
    with pytest.raises(PluginContractError, match="did not write"):
        await run_plugin(
            command=(
                f"{sys.executable} {plugin} "
                "--task-json {{task_json}} --work-dir {{work_dir}} "
                "--result-file {{result_file}}"
            ),
            task_json_path=work_dir / "task.json",
            work_dir=work_dir,
            result_file=work_dir / "result.json",
        )


async def test_run_plugin_enforces_max_runtime(tmp_path: Path) -> None:
    plugin = _write_fake_plugin(
        tmp_path / "plugin.py",
        body=dedent(
            """\
            import time
            time.sleep(10)
            """
        ),
    )
    work_dir = tmp_path / "wd"
    work_dir.mkdir()
    with pytest.raises(TimeoutError):
        await run_plugin(
            command=(
                f"{sys.executable} {plugin} "
                "--task-json {{task_json}} --work-dir {{work_dir}} "
                "--result-file {{result_file}}"
            ),
            task_json_path=work_dir / "task.json",
            work_dir=work_dir,
            result_file=work_dir / "result.json",
            max_runtime_seconds=1,
        )


async def test_run_plugin_returns_failed_result_on_nonzero_exit_with_result(
    tmp_path: Path,
) -> None:
    """A plugin that writes a 'failed' result.json then exits non-zero is OK —
    the runner returns the result for the caller to apply.
    """
    plugin = _write_fake_plugin(
        tmp_path / "plugin.py",
        body=dedent(
            """\
            payload = {
                "schema_version": 1,
                "task_id": "t-1",
                "status": "failed",
                "exit_code": 1,
            }
            with open(args.result_file, "w") as fh:
                json.dump(payload, fh)
            sys.exit(1)
            """
        ),
    )
    work_dir = tmp_path / "wd"
    work_dir.mkdir()
    result = await run_plugin(
        command=(
            f"{sys.executable} {plugin} "
            "--task-json {{task_json}} --work-dir {{work_dir}} "
            "--result-file {{result_file}}"
        ),
        task_json_path=work_dir / "task.json",
        work_dir=work_dir,
        result_file=work_dir / "result.json",
    )
    assert result["status"] == "failed"
    assert result["exit_code"] == 1
