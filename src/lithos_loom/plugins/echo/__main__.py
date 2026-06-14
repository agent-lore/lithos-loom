"""echo — a minimal smoke-test route plugin.

Invoked by the daemon as::

    python -m lithos_loom.plugins.echo \\
        --task-json <path> --work-dir <path> --result-file <path>

It does **no work**: it reads the task id from the task envelope and writes a
schema-valid ``status="succeeded"`` ``result.json`` atomically, then exits 0.

Use it as a zero-cost route target to validate the dispatch / claim /
completion lifecycle — e.g. issue #86's "add a trigger tag to an already-open
task and watch it dispatch without a daemon restart" — without spinning up a
containerised agent. Not wired into any default config: add a route stanza
whose ``[routes.match] tags = ["trigger:echo"]`` and tag a task to fire it
(see ``examples/lithos-loom.toml``).

Self-contained (no ``lithos_loom`` imports) so it runs anywhere the daemon can
spawn ``python``; the atomic write mirrors the plugin contract
(temp + fsync + ``os.replace`` — a partial ``result.json`` must never be
observable).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def _read_task_id(task_json: Path) -> str:
    """Read the task id from the runner's ``{"task": {...}}`` envelope.

    Tolerates a bare task object (no ``task`` wrapper) and any malformed
    input — the id is best-effort, and the runner reads only ``status`` from
    the result anyway, so an empty id still completes the task cleanly.
    """
    try:
        data = json.loads(task_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    if not isinstance(data, dict):
        return ""
    task = data.get("task", data)
    if not isinstance(task, dict):
        return ""
    return str(task.get("id") or "")


def _write_result_atomic(result_file: Path, payload: dict) -> None:
    """Write *payload* to *result_file* atomically (temp + fsync + replace)."""
    result_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = result_file.parent / f".{result_file.name}.tmp.{os.getpid()}"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, result_file)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lithos-loom-echo")
    parser.add_argument("--task-json", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)  # unused; ignored
    parser.add_argument("--result-file", type=Path, required=True)
    args = parser.parse_args(argv)

    _write_result_atomic(
        args.result_file,
        {
            "schema_version": 1,
            "task_id": _read_task_id(args.task_json),
            "status": "succeeded",
            "exit_code": 0,
        },
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
