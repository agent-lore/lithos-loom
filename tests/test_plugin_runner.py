"""Tests for the atomic-write helper and subprocess plugin runner."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from textwrap import dedent

import pytest

from lithos_loom.errors import PluginContractError
from lithos_loom.plugin_runner import (
    run_plugin,
    validate_result_schema,
    write_result_atomically,
)


def test_result_schema_accepts_pr_url() -> None:
    # #188: pr_url is an optional contract field (an offline reader names the PR).
    validate_result_schema(
        {
            "schema_version": 1,
            "task_id": "t1",
            "status": "succeeded",
            "exit_code": 0,
            "pr_url": "https://github.com/o/r/pull/170",
        }
    )
    # additionalProperties:false still rejects an unknown field.
    with pytest.raises(PluginContractError):
        validate_result_schema(
            {
                "schema_version": 1,
                "task_id": "t1",
                "status": "succeeded",
                "exit_code": 0,
                "not_a_field": 1,
            }
        )


def test_result_schema_accepts_delivery_error_category() -> None:
    # #194: a PR-delivery failure on an approved run is a first-class error
    # category, so the failed result.json self-describes why the run stopped.
    validate_result_schema(
        {
            "schema_version": 1,
            "task_id": "t1",
            "status": "failed",
            "exit_code": 1,
            "error": {"category": "delivery", "message": "PR delivery failed: boom"},
        }
    )


def test_result_schema_accepts_rounds() -> None:
    # #196: rounds is an optional contract field so a reaped/replayed run's
    # recovered summary (state.json is gone) can still show the round count.
    validate_result_schema(
        {
            "schema_version": 1,
            "task_id": "t1",
            "status": "succeeded",
            "exit_code": 0,
            "rounds": 3,
        }
    )


def test_result_schema_accepts_run_id() -> None:
    # #198: run_id is an optional contract field so `develop attach` can bind the
    # shared result.json to THIS run (not a prior run's leftover) for terminal
    # detection — closing the best-effort reap/marker holes.
    validate_result_schema(
        {
            "schema_version": 1,
            "task_id": "t1",
            "status": "succeeded",
            "exit_code": 0,
            "run_id": "r1",
        }
    )


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


async def test_run_plugin_clears_stale_result_file_before_launch(
    tmp_path: Path,
) -> None:
    """A stale result.json from a previous attempt must not be re-applied.

    Regression: without an explicit unlink before launch, a plugin that
    exits without writing a fresh result file would leave run_plugin
    parsing the prior attempt's outcome — silently completing or failing
    a task on stale data.
    """
    work_dir = tmp_path / "wd"
    work_dir.mkdir()
    result_file = work_dir / "result.json"
    # Pre-populate with a stale "succeeded" result from an imagined prior run.
    result_file.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "task_id": "STALE",
                "status": "succeeded",
                "exit_code": 0,
            }
        )
    )

    plugin = _write_fake_plugin(tmp_path / "plugin.py", body="sys.exit(0)")
    with pytest.raises(PluginContractError, match="did not write"):
        await run_plugin(
            command=(
                f"{sys.executable} {plugin} "
                "--task-json {{task_json}} --work-dir {{work_dir}} "
                "--result-file {{result_file}}"
            ),
            task_json_path=work_dir / "task.json",
            work_dir=work_dir,
            result_file=result_file,
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


async def test_run_plugin_accepts_interrupted_result_with_resume_block(
    tmp_path: Path,
) -> None:
    """The T10 contract extension: status=interrupted may carry an
    error.category=usage_limited and a resume block (resume_after + session
    ids) — the runner validates and returns it for re-dispatch scheduling."""
    plugin = _write_fake_plugin(
        tmp_path / "plugin.py",
        body=dedent(
            """\
            payload = {
                "schema_version": 1,
                "task_id": "t1",
                "status": "interrupted",
                "exit_code": 30,
                "error": {
                    "category": "usage_limited",
                    "message": "coder usage-limited",
                    "retriable": True,
                },
                "resume": {
                    "resume_after": "2026-06-12T15:00:00+00:00",
                    "run_id": "r1",
                    "coder_session": "sess-c",
                    "reviewer_sessions": {"code-quality": "sess-r"},
                },
            }
            with open(args.result_file, "w") as fh:
                json.dump(payload, fh)
            sys.exit(30)
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
    assert result["status"] == "interrupted"
    assert result["resume"]["resume_after"] == "2026-06-12T15:00:00+00:00"
