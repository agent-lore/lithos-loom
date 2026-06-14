"""Tests for the ``echo`` smoke-test plugin.

``echo`` is the zero-cost route target used to validate dispatch/claim/complete
without a containerised agent (e.g. issue #86). It must emit a
schema-valid ``status="succeeded"`` result for any task envelope.
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from lithos_loom.plugins.echo.__main__ import main

_SCHEMA = json.loads(
    (Path(__file__).resolve().parents[1] / "docs" / "result-schema.json").read_text()
)


def _run(tmp_path: Path, task_json_obj: object) -> dict:
    task_json = tmp_path / "task.json"
    task_json.write_text(json.dumps(task_json_obj))
    result_file = tmp_path / "result.json"
    rc = main(
        [
            "--task-json",
            str(task_json),
            "--work-dir",
            str(tmp_path),
            "--result-file",
            str(result_file),
        ]
    )
    assert rc == 0
    return json.loads(result_file.read_text())


def test_echo_emits_schema_valid_succeeded_result(tmp_path: Path) -> None:
    result = _run(tmp_path, {"task": {"id": "t-42", "title": "x", "status": "open"}})
    jsonschema.validate(result, _SCHEMA)  # additionalProperties:false etc.
    assert result == {
        "schema_version": 1,
        "task_id": "t-42",
        "status": "succeeded",
        "exit_code": 0,
    }


def test_echo_tolerates_bare_task_object(tmp_path: Path) -> None:
    # No {"task": ...} wrapper — still extracts the id.
    result = _run(tmp_path, {"id": "bare-1", "tags": []})
    assert result["task_id"] == "bare-1"
    assert result["status"] == "succeeded"


def test_echo_tolerates_missing_id(tmp_path: Path) -> None:
    # Malformed envelope -> empty id (still a valid string) and a clean success;
    # the runner reads only `status`, so the task completes regardless.
    result = _run(tmp_path, {"task": {"title": "no id here"}})
    jsonschema.validate(result, _SCHEMA)
    assert result["task_id"] == ""
    assert result["status"] == "succeeded"


def test_echo_requires_all_three_path_flags(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        main(["--task-json", str(tmp_path / "t.json")])  # missing the others
