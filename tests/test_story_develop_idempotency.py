"""Tests for the story-develop ``--idempotency-key`` short-circuit (US-18).

Two layers: the ``idempotency`` store module in isolation (store-dir
resolution, record/lookup round-trip, the "only a completed record replays"
guard) and the ``__main__`` daemon-mode wiring (fresh key runs + records under
the *explicit* key; a repeat key replays without re-running; the ``--open-pr``
replay path never calls ``deliver``; the default key is the task id).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from lithos_loom.plugins.story_develop import idempotency
from lithos_loom.plugins.story_develop.daemon_io import EXIT_SUCCEEDED
from lithos_loom.plugins.story_develop.develop import DevelopResult
from lithos_loom.plugins.story_develop.idempotency import (
    lookup_completed,
    record_completion,
    store_dir,
)

# ── store module ───────────────────────────────────────────────────────


def _completed_payload(task_id: str = "t-1") -> dict[str, Any]:
    return {
        "schema_version": 1,
        "task_id": task_id,
        "status": "succeeded",
        "exit_code": 0,
        "worktree": "/tmp/wt",  # nosec B108
        "commits": ["a" * 40],
        "error": None,
    }


def test_store_dir_env_override_wins(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LITHOS_LOOM_IDEMPOTENCY_DIR", str(tmp_path / "store"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    assert store_dir() == tmp_path / "store"


def test_store_dir_falls_back_to_xdg_state_home(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("LITHOS_LOOM_IDEMPOTENCY_DIR", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    assert (
        store_dir()
        == tmp_path / "xdg" / "lithos-loom" / "story-develop" / "idempotency"
    )


def test_store_dir_default_is_local_state(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("LITHOS_LOOM_IDEMPOTENCY_DIR", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setattr(idempotency.Path, "home", classmethod(lambda cls: tmp_path))
    assert store_dir() == (
        tmp_path / ".local" / "state" / "lithos-loom" / "story-develop" / "idempotency"
    )


def test_record_then_lookup_round_trips(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LITHOS_LOOM_IDEMPOTENCY_DIR", str(tmp_path / "store"))
    payload = _completed_payload()
    record_completion("my-key", payload)
    assert lookup_completed("my-key") == payload


def test_lookup_missing_key_is_none(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LITHOS_LOOM_IDEMPOTENCY_DIR", str(tmp_path / "store"))
    assert lookup_completed("never-seen") is None


def test_record_keys_are_independent(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LITHOS_LOOM_IDEMPOTENCY_DIR", str(tmp_path / "store"))
    record_completion("key-a", _completed_payload("t-a"))
    assert lookup_completed("key-a") is not None
    assert lookup_completed("key-b") is None


@pytest.mark.parametrize(
    "payload",
    [
        {"status": "failed", "exit_code": 1},
        {"status": "interrupted", "exit_code": 30},
        # claims success but exit_code disagrees → not a real completion
        {"status": "succeeded", "exit_code": 1},
        # not even an object
        ["succeeded"],
        "succeeded",
        # AC4 boundary: claims success (status + exit_code BOTH say "completed")
        # but is schema-malformed — missing required result.json fields. A guard
        # that only checked status/exit_code would wrongly replay these.
        {"status": "succeeded", "exit_code": 0},  # missing schema_version+task_id
        {"status": "succeeded", "exit_code": 0, "task_id": "t-1"},  # no schema_version
        {"status": "succeeded", "exit_code": 0, "schema_version": 1},  # no task_id
        {  # all required fields present but a stray field (additionalProperties)
            "status": "succeeded",
            "exit_code": 0,
            "schema_version": 1,
            "task_id": "t-1",
            "bogus": "field",
        },
        {  # wrong schema_version const
            "status": "succeeded",
            "exit_code": 0,
            "schema_version": 2,
            "task_id": "t-1",
        },
    ],
)
def test_lookup_ignores_non_completed_records(
    tmp_path: Path, monkeypatch, payload: Any
) -> None:
    """AC4: failed / interrupted / malformed records — including ones that claim
    success but violate the result.json schema — are ignored so the task stays
    retriable."""
    monkeypatch.setenv("LITHOS_LOOM_IDEMPOTENCY_DIR", str(tmp_path / "store"))
    path = idempotency._record_path("k")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert lookup_completed("k") is None


def test_lookup_ignores_malformed_json(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LITHOS_LOOM_IDEMPOTENCY_DIR", str(tmp_path / "store"))
    path = idempotency._record_path("k")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert lookup_completed("k") is None


def test_lookup_binds_record_to_expected_task_id(tmp_path: Path, monkeypatch) -> None:
    """security/f-001 (CWE-345): a record is replayed only for the task it names
    — a mismatched expected_task_id (reused key / tampered store) never replays
    one task's result into another's."""
    monkeypatch.setenv("LITHOS_LOOM_IDEMPOTENCY_DIR", str(tmp_path / "store"))
    record_completion("shared-key", _completed_payload("t-1"))
    # Same task → replays; different task → ignored; no expectation → replays.
    assert lookup_completed("shared-key", expected_task_id="t-1") is not None
    assert lookup_completed("shared-key", expected_task_id="t-2") is None
    assert lookup_completed("shared-key") is not None


def test_lookup_completed_for_run_finds_record_under_explicit_key(
    tmp_path: Path, monkeypatch
) -> None:
    """correctness/f-003: a success recorded under an explicit --idempotency-key
    (not the task id) is still locatable by the run id embedded in the recorded
    conversation-log path, so a reaped run's outcome can be recovered."""
    monkeypatch.setenv("LITHOS_LOOM_IDEMPOTENCY_DIR", str(tmp_path / "store"))
    run_dir = tmp_path / "work" / "t-1" / "r1"
    payload = _completed_payload("t-1")
    payload["artifacts"] = {"conversation_log": str(run_dir / "conversation.md")}
    record_completion("explicit-key", payload)  # recorded NOT under the task id
    # not findable via the task-id key path (the f-003 bug) ...
    assert lookup_completed("t-1", expected_task_id="t-1") is None
    # ... but the run-id scan finds it regardless of the recording key
    assert idempotency.lookup_completed_for_run("t-1", "r1") == payload
    # a different run id or task id does not match
    assert idempotency.lookup_completed_for_run("t-1", "other-run") is None
    assert idempotency.lookup_completed_for_run("t-2", "r1") is None


def test_lookup_completed_for_run_ignores_unbound_and_missing(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("LITHOS_LOOM_IDEMPOTENCY_DIR", str(tmp_path / "store"))
    # a completed record without an artifacts.conversation_log can't be bound
    record_completion("k", _completed_payload("t-1"))
    assert idempotency.lookup_completed_for_run("t-1", "r1") is None
    # a missing store dir yields None rather than raising
    monkeypatch.setenv("LITHOS_LOOM_IDEMPOTENCY_DIR", str(tmp_path / "absent"))
    assert idempotency.lookup_completed_for_run("t-1", "r1") is None


def test_record_completion_prunes_store_to_bound(tmp_path: Path, monkeypatch) -> None:
    """security/f-002 (CWE-770): the store is bounded — each write prunes back to
    LITHOS_LOOM_IDEMPOTENCY_MAX_RECORDS so it cannot grow without limit."""
    monkeypatch.setenv("LITHOS_LOOM_IDEMPOTENCY_DIR", str(tmp_path / "store"))
    monkeypatch.setenv("LITHOS_LOOM_IDEMPOTENCY_MAX_RECORDS", "2")
    for i in range(5):
        record_completion(f"key-{i}", _completed_payload(f"t-{i}"))
    remaining = list((tmp_path / "store").glob("*.json"))
    assert len(remaining) == 2


# ── daemon-mode wiring ─────────────────────────────────────────────────


def _result(status: str, tmp_path: Path, **kw: Any) -> DevelopResult:
    defaults: dict[str, Any] = dict(
        status=status,
        run_id="r1",
        worktree=tmp_path / "wt",
        branch="b",
        base_sha="0" * 40,
        commits=["a" * 40],
        rounds=2,
        handoff_present=True,
        coder_cost_usd=0.5,
        review_cost_usd=0.5,
        message="msg",
        coder_session="sess-coder",
        conversation_log=tmp_path / "conversation.md",
    )
    defaults.update(kw)
    return DevelopResult(**defaults)


def _write_task_json(path: Path, task_id: str = "t-1") -> Path:
    path.write_text(
        json.dumps(
            {
                "task": {
                    "id": task_id,
                    "title": "Add a flag",
                    "description": "Body.",
                    "metadata": {"project": "loom"},
                }
            }
        ),
        encoding="utf-8",
    )
    return path


def _daemon_args(
    tmp_git_repo: Path, tmp_path: Path, *extra: str, task_id: str = "t-1"
) -> tuple[list[str], Path]:
    task_json = _write_task_json(tmp_path / "task.json", task_id)
    result_file = tmp_path / "result.json"
    argv = [
        "--repo",
        str(tmp_git_repo),
        "--task-json",
        str(task_json),
        "--work-dir",
        str(tmp_path / "work"),
        "--result-file",
        str(result_file),
        *extra,
    ]
    return argv, result_file


def _stub_daemon(monkeypatch, tmp_path: Path) -> dict[str, Any]:
    """Stub everything around the agent loop so the daemon path is pure I/O.

    Returns a ``captured`` dict the test can inspect: ``develop_calls`` /
    ``deliver_calls`` counters, the approved-run result, etc.
    """
    from lithos_loom.plugins.story_develop import __main__ as main_mod
    from lithos_loom.plugins.story_develop.daemon_io import ProjectDevelopSettings

    captured: dict[str, Any] = {"develop_calls": 0, "deliver_calls": 0}

    monkeypatch.setattr(
        main_mod, "resolve_project_settings", lambda url, meta: ProjectDevelopSettings()
    )
    monkeypatch.setattr(main_mod, "load_tool_default_models", lambda: ({}, ()))
    monkeypatch.setattr(main_mod, "post_frictions", lambda *a, **kw: None)
    monkeypatch.setattr(main_mod, "post_results", lambda *a, **kw: True)

    def fake_develop(config, **kw):
        captured["develop_calls"] += 1
        captured["config"] = config
        return _result("approved", tmp_path)

    def fake_deliver(config, result, **kw):
        captured["deliver_calls"] += 1
        return None

    monkeypatch.setattr(main_mod, "develop", fake_develop)
    monkeypatch.setattr(main_mod, "deliver", fake_deliver)
    return captured


def test_fresh_key_runs_and_records_under_explicit_key(
    tmp_git_repo: Path, tmp_path: Path, monkeypatch
) -> None:
    """AC3: a fresh run develops normally and records the completion keyed off
    the EXPLICIT --idempotency-key — not the task id."""
    from lithos_loom.plugins.story_develop import __main__ as main_mod

    captured = _stub_daemon(monkeypatch, tmp_path)
    argv, result_file = _daemon_args(
        tmp_git_repo, tmp_path, "--idempotency-key", "explicit-key", task_id="t-1"
    )
    rc = main_mod.main(argv)

    assert rc == EXIT_SUCCEEDED
    assert captured["develop_calls"] == 1
    payload = json.loads(result_file.read_text(encoding="utf-8"))
    assert payload["status"] == "succeeded"

    # Recorded under the explicit key, and NOT under the task id — proving the
    # recorder keys off --idempotency-key, not ctx.task_id.
    assert lookup_completed("explicit-key") is not None
    assert lookup_completed("t-1") is None


def test_repeat_key_short_circuits_without_rerun(
    tmp_git_repo: Path, tmp_path: Path, monkeypatch
) -> None:
    """AC2: a second run under a recorded key replays the prior result.json and
    exits without re-running the agent loop."""
    from lithos_loom.plugins.story_develop import __main__ as main_mod

    captured = _stub_daemon(monkeypatch, tmp_path)

    # First run records the completion.
    argv1, result_file1 = _daemon_args(
        tmp_git_repo, tmp_path, "--idempotency-key", "k", task_id="t-1"
    )
    assert main_mod.main(argv1) == EXIT_SUCCEEDED
    assert captured["develop_calls"] == 1
    first_payload = json.loads(result_file1.read_text(encoding="utf-8"))

    # Second run under the same key short-circuits: develop is NOT called again,
    # and the replayed result equals the recorded one.
    result_file2 = tmp_path / "result2.json"
    argv2 = [
        "--repo",
        str(tmp_git_repo),
        "--task-json",
        str(tmp_path / "task.json"),
        "--work-dir",
        str(tmp_path / "work"),
        "--result-file",
        str(result_file2),
        "--idempotency-key",
        "k",
    ]
    assert main_mod.main(argv2) == EXIT_SUCCEEDED
    assert captured["develop_calls"] == 1  # unchanged: no second agent run
    assert json.loads(result_file2.read_text(encoding="utf-8")) == first_payload


def test_open_pr_replay_does_not_deliver(
    tmp_git_repo: Path, tmp_path: Path, monkeypatch
) -> None:
    """AC2/AC5: replaying a recorded run with --open-pr never calls deliver()
    (no second PR)."""
    from lithos_loom.plugins.story_develop import __main__ as main_mod

    captured = _stub_daemon(monkeypatch, tmp_path)

    # Pre-seed the store so the run is a pure replay.
    record_completion("k", _completed_payload("t-1"))

    argv, result_file = _daemon_args(
        tmp_git_repo, tmp_path, "--open-pr", "--idempotency-key", "k", task_id="t-1"
    )
    rc = main_mod.main(argv)

    assert rc == EXIT_SUCCEEDED
    assert captured["develop_calls"] == 0  # short-circuit before the agent loop
    assert captured["deliver_calls"] == 0  # and before delivery → no second PR
    assert json.loads(result_file.read_text(encoding="utf-8")) == _completed_payload(
        "t-1"
    )


def test_default_idempotency_key_is_task_id(
    tmp_git_repo: Path, tmp_path: Path, monkeypatch
) -> None:
    """AC1: with no --idempotency-key, the run is keyed by the task id."""
    from lithos_loom.plugins.story_develop import __main__ as main_mod

    captured = _stub_daemon(monkeypatch, tmp_path)
    argv, _ = _daemon_args(tmp_git_repo, tmp_path, task_id="t-99")
    assert main_mod.main(argv) == EXIT_SUCCEEDED
    assert captured["develop_calls"] == 1
    assert lookup_completed("t-99") is not None


def test_reused_key_across_tasks_does_not_replay(
    tmp_git_repo: Path, tmp_path: Path, monkeypatch
) -> None:
    """security/f-001: a second, DIFFERENT task reusing one --idempotency-key
    does not replay the first task's result — the task-id binding forces a real
    run so the second task gets its own result.json."""
    from lithos_loom.plugins.story_develop import __main__ as main_mod

    captured = _stub_daemon(monkeypatch, tmp_path)
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()

    # Task t-1 records under the shared key.
    argv1, _ = _daemon_args(
        tmp_git_repo,
        tmp_path / "a",
        "--idempotency-key",
        "shared",
        task_id="t-1",
    )
    assert main_mod.main(argv1) == EXIT_SUCCEEDED
    assert captured["develop_calls"] == 1

    # Task t-2 reuses the SAME key: binding mismatch → real run, own result.
    result_file2 = tmp_path / "b" / "result.json"
    argv2 = [
        "--repo",
        str(tmp_git_repo),
        "--task-json",
        str(_write_task_json(tmp_path / "b" / "task.json", "t-2")),
        "--work-dir",
        str(tmp_path / "b" / "work"),
        "--result-file",
        str(result_file2),
        "--idempotency-key",
        "shared",
    ]
    assert main_mod.main(argv2) == EXIT_SUCCEEDED
    assert captured["develop_calls"] == 2  # ran again, not replayed
    assert json.loads(result_file2.read_text(encoding="utf-8"))["task_id"] == "t-2"
