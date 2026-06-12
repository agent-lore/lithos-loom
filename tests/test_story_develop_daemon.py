"""Tests for story-develop daemon mode (T10).

Covers the three ``daemon_io`` concerns — task.json parsing, the
project-context config lookup contract, result.json construction — plus the
``__main__`` daemon-mode wiring. Every produced result payload is validated
against ``docs/result-schema.json`` via the runner's own validator, so the
plugin and the runner cannot drift apart silently.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from lithos_loom.plugin_runner import _validate_result_schema
from lithos_loom.plugins.story_develop.config import ReviewerSpec
from lithos_loom.plugins.story_develop.daemon_io import (
    BUILTIN_REVIEWERS,
    EXIT_BAD_INPUT,
    EXIT_FAILED,
    EXIT_INTERRUPTED,
    EXIT_SUCCEEDED,
    build_result_payload,
    read_task_payload,
    resolve_project_settings,
)
from lithos_loom.plugins.story_develop.develop import DevelopResult

# ── read_task_payload ──────────────────────────────────────────────────


def _write_task_json(path: Path, task: dict[str, Any]) -> Path:
    path.write_text(json.dumps({"task": task}), encoding="utf-8")
    return path


def test_read_task_payload_extracts_context(tmp_path: Path) -> None:
    p = _write_task_json(
        tmp_path / "task.json",
        {
            "id": "t-1",
            "title": "Add a flag",
            "description": "Body.",
            "metadata": {
                "project": "lithos-loom",
                "acceptance_criteria": "must have tests",
            },
        },
    )
    ctx = read_task_payload(p)
    assert ctx.task_id == "t-1"
    assert ctx.task_text == "Add a flag\n\nBody."
    assert ctx.acceptance_criteria == "must have tests"
    assert ctx.metadata["project"] == "lithos-loom"


def test_read_task_payload_blank_ac_is_none(tmp_path: Path) -> None:
    p = _write_task_json(
        tmp_path / "task.json",
        {"id": "t-1", "title": "T", "metadata": {"acceptance_criteria": "  "}},
    )
    assert read_task_payload(p).acceptance_criteria is None


@pytest.mark.parametrize(
    "task",
    [
        {"title": "no id"},
        {"id": "t-1"},  # no title
    ],
)
def test_read_task_payload_rejects_incomplete_task(
    tmp_path: Path, task: dict[str, Any]
) -> None:
    p = _write_task_json(tmp_path / "task.json", task)
    with pytest.raises(ValueError):
        read_task_payload(p)


def test_read_task_payload_rejects_missing_task_key(tmp_path: Path) -> None:
    p = tmp_path / "task.json"
    p.write_text(json.dumps({"not_task": {}}), encoding="utf-8")
    with pytest.raises(ValueError, match="task"):
        read_task_payload(p)


def test_read_task_payload_rejects_invalid_json(tmp_path: Path) -> None:
    p = tmp_path / "task.json"
    p.write_text("{nope", encoding="utf-8")
    with pytest.raises(ValueError):
        read_task_payload(p)


# ── resolve_project_settings ───────────────────────────────────────────


class _FakeNote:
    def __init__(self, path: str, metadata: dict[str, Any]) -> None:
        self.path = path
        self.metadata = metadata


class _FakeClient:
    """Stands in for LithosClient: canned note_read / note_list responses."""

    note: _FakeNote | None = None
    listing: list[_FakeNote] = []
    fail_connect: bool = False

    def __init__(self, url: str, *, agent_id: str) -> None:
        pass

    async def __aenter__(self) -> _FakeClient:
        if type(self).fail_connect:
            raise ConnectionError("lithos down")
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def note_read(self, *, path: str) -> _FakeNote | None:
        return type(self).note

    async def note_list(self, **kw: Any) -> list[_FakeNote]:
        return type(self).listing


@pytest.fixture
def fake_client(monkeypatch) -> type[_FakeClient]:
    class Client(_FakeClient):
        note = None
        listing = []
        fail_connect = False

    monkeypatch.setattr(
        "lithos_loom.plugins.story_develop.daemon_io.LithosClient", Client
    )
    return Client


def test_resolve_no_project_slug_uses_builtin_with_friction() -> None:
    settings = resolve_project_settings("http://x", {})
    assert settings.reviewers == BUILTIN_REVIEWERS
    assert any("metadata.project" in f for f in settings.frictions)


def test_resolve_lithos_unreachable_degrades(fake_client) -> None:
    fake_client.fail_connect = True
    settings = resolve_project_settings("http://x", {"project": "loom"})
    assert settings.reviewers == BUILTIN_REVIEWERS
    assert any("cannot read project-context" in f for f in settings.frictions)


def test_resolve_no_context_doc_degrades(fake_client) -> None:
    settings = resolve_project_settings("http://x", {"project": "loom"})
    assert settings.reviewers == BUILTIN_REVIEWERS
    assert any("no project-context doc" in f for f in settings.frictions)


def test_resolve_falls_back_to_smallest_tagged_doc(fake_client) -> None:
    fake_client.listing = [
        _FakeNote(
            "projects/loom/b-context.md",
            {"develop_default_reviewers": ["never-this"]},
        ),
        _FakeNote(
            "projects/loom/a-context.md",
            {
                "develop_reviewers": [{"name": "security"}],
                "develop_default_reviewers": ["security"],
            },
        ),
    ]
    settings = resolve_project_settings("http://x", {"project": "loom"})
    assert [s.name for s in settings.reviewers] == ["security"]


def test_resolve_stale_doc_without_keys_is_builtin_no_friction(fake_client) -> None:
    """Enabling story-develop on a project is purely additive (contract #5)."""
    fake_client.note = _FakeNote("projects/loom/loom-project-context.md", {})
    settings = resolve_project_settings("http://x", {"project": "loom"})
    assert settings.reviewers == BUILTIN_REVIEWERS
    assert settings.frictions == ()


def test_resolve_pool_without_defaults_is_builtin(fake_client) -> None:
    """Opting a reviewer into the pool does not auto-run it (contract #3/#5)."""
    fake_client.note = _FakeNote(
        "projects/loom/loom-project-context.md",
        {"develop_reviewers": [{"name": "security", "block_threshold": "minor"}]},
    )
    settings = resolve_project_settings("http://x", {"project": "loom"})
    assert settings.reviewers == BUILTIN_REVIEWERS


def test_resolve_full_config_with_task_override(fake_client) -> None:
    fake_client.note = _FakeNote(
        "projects/loom/loom-project-context.md",
        {
            "develop_reviewers": [
                {"name": "code-quality"},
                {"name": "security", "block_threshold": "minor"},
            ],
            "develop_default_reviewers": ["code-quality"],
            "develop_coder": {"tool": "claude"},
            "develop_fallback_chain": ["codex"],
            "develop_max_rounds": 3,
            "develop_max_cost_usd": 10,
        },
    )
    settings = resolve_project_settings(
        "http://x", {"project": "loom", "reviewers": ["security"]}
    )
    assert [s.name for s in settings.reviewers] == ["security"]
    assert settings.reviewers[0].block_threshold == "minor"
    assert settings.coder == "claude"
    assert settings.fallback_chain == ("codex",)
    assert settings.max_rounds == 3
    assert settings.max_cost_usd == 10.0
    assert settings.frictions == ()


def test_resolve_unknown_override_name_skipped_with_friction(fake_client) -> None:
    fake_client.note = _FakeNote(
        "projects/loom/loom-project-context.md",
        {
            "develop_reviewers": [{"name": "code-quality"}],
            "develop_default_reviewers": ["code-quality"],
        },
    )
    settings = resolve_project_settings(
        "http://x", {"project": "loom", "reviewers": ["nonesuch", "code-quality"]}
    )
    assert [s.name for s in settings.reviewers] == ["code-quality"]
    assert any("nonesuch" in f for f in settings.frictions)


def test_resolve_all_unknown_selection_falls_back_to_builtin(fake_client) -> None:
    fake_client.note = _FakeNote(
        "projects/loom/loom-project-context.md",
        {
            "develop_reviewers": [{"name": "code-quality"}],
            "develop_default_reviewers": ["nonesuch"],
        },
    )
    settings = resolve_project_settings("http://x", {"project": "loom"})
    assert settings.reviewers == BUILTIN_REVIEWERS
    assert any("resolved to no known reviewers" in f for f in settings.frictions)


def test_resolve_invalid_pool_entry_and_ceilings_frictioned(fake_client) -> None:
    fake_client.note = _FakeNote(
        "projects/loom/loom-project-context.md",
        {
            "develop_reviewers": [
                {"name": "BAD NAME"},
                {"name": "code-quality"},
            ],
            "develop_default_reviewers": ["code-quality"],
            "develop_coder": "claude",  # not an object
            "develop_max_rounds": 0,
            "develop_max_cost_usd": -1,
        },
    )
    settings = resolve_project_settings("http://x", {"project": "loom"})
    assert [s.name for s in settings.reviewers] == ["code-quality"]
    assert settings.max_rounds is None
    assert settings.max_cost_usd is None
    joined = "\n".join(settings.frictions)
    assert "invalid reviewer entry" in joined
    assert "develop_coder" in joined
    assert "develop_max_rounds" in joined
    assert "develop_max_cost_usd" in joined


# ── build_result_payload ───────────────────────────────────────────────


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


_NOW = datetime(2026, 6, 12, 12, 0, 0, tzinfo=UTC)


def test_build_result_payload_approved_is_succeeded(tmp_path: Path) -> None:
    payload, exit_code = build_result_payload(
        _result("approved", tmp_path),
        task_id="t-1",
        started_at=_NOW,
        finished_at=_NOW,
        run_dir=tmp_path,
    )
    assert exit_code == EXIT_SUCCEEDED
    assert payload["status"] == "succeeded"
    assert payload["error"] is None
    assert "resume" not in payload
    assert payload["artifacts"]["conversation_log"].endswith("conversation.md")
    _validate_result_schema(payload)


def test_build_result_payload_interrupted_carries_resume(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "state.json").write_text(
        json.dumps(
            {"reviewers": {"code-quality": {"session": "sess-r", "tool": "claude"}}}
        ),
        encoding="utf-8",
    )
    resume_at = datetime(2026, 6, 12, 15, 0, 0, tzinfo=UTC)
    payload, exit_code = build_result_payload(
        _result("interrupted", tmp_path, resume_after=resume_at),
        task_id="t-1",
        started_at=_NOW,
        finished_at=_NOW,
        run_dir=run_dir,
    )
    assert exit_code == EXIT_INTERRUPTED
    assert payload["status"] == "interrupted"
    assert payload["error"]["category"] == "usage_limited"
    assert payload["error"]["retriable"] is True
    resume = payload["resume"]
    assert resume["resume_after"] == "2026-06-12T15:00:00+00:00"
    assert resume["run_id"] == "r1"
    assert resume["coder_session"] == "sess-coder"
    assert resume["reviewer_sessions"] == {"code-quality": "sess-r"}
    _validate_result_schema(payload)


@pytest.mark.parametrize(
    "status", ["max_rounds", "stalled", "disputed", "cost_exceeded", "failed"]
)
def test_build_result_payload_other_stops_are_failed(
    tmp_path: Path, status: str
) -> None:
    payload, exit_code = build_result_payload(
        _result(status, tmp_path),
        task_id="t-1",
        started_at=_NOW,
        finished_at=_NOW,
        run_dir=tmp_path,
    )
    assert exit_code == EXIT_FAILED
    assert payload["status"] == "failed"
    assert payload["error"]["category"] == "agent"
    _validate_result_schema(payload)


# ── __main__ daemon-mode wiring ────────────────────────────────────────


def _daemon_args(
    tmp_git_repo: Path, tmp_path: Path, *extra: str
) -> tuple[list[str], Path]:
    task_json = _write_task_json(
        tmp_path / "task.json",
        {
            "id": "t-1",
            "title": "Add a flag",
            "description": "Body.",
            "metadata": {"project": "loom"},
        },
    )
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


def test_daemon_mode_rejects_standalone_task_flags(
    tmp_git_repo: Path, tmp_path: Path, capsys
) -> None:
    from lithos_loom.plugins.story_develop.__main__ import main

    argv, _ = _daemon_args(tmp_git_repo, tmp_path, "--description", "x")
    assert main(argv) == EXIT_BAD_INPUT
    assert "--description" in capsys.readouterr().err


def test_daemon_mode_requires_result_file_and_work_dir(
    tmp_git_repo: Path, tmp_path: Path, capsys
) -> None:
    from lithos_loom.plugins.story_develop.__main__ import main

    task_json = _write_task_json(tmp_path / "task.json", {"id": "t-1", "title": "T"})
    rc = main(["--repo", str(tmp_git_repo), "--task-json", str(task_json)])
    assert rc == EXIT_BAD_INPUT
    assert "requires --result-file and --work-dir" in capsys.readouterr().err


def test_result_file_without_task_json_rejected(
    tmp_git_repo: Path, tmp_path: Path, capsys
) -> None:
    from lithos_loom.plugins.story_develop.__main__ import main

    rc = main(
        [
            "--repo",
            str(tmp_git_repo),
            "--description",
            "x",
            "--result-file",
            str(tmp_path / "r.json"),
        ]
    )
    assert rc == 2
    assert "--result-file requires --task-json" in capsys.readouterr().err


def test_daemon_mode_happy_path_writes_result(
    tmp_git_repo: Path, tmp_path: Path, monkeypatch
) -> None:
    """Settings flow into the config; results post to Lithos; result.json is
    schema-valid and the exit code matches the contract."""
    from lithos_loom.plugins.story_develop import __main__ as main_mod
    from lithos_loom.plugins.story_develop.daemon_io import ProjectDevelopSettings

    captured: dict[str, Any] = {}
    settings = ProjectDevelopSettings(
        reviewers=(ReviewerSpec(name="security", block_threshold="minor"),),
        fallback_chain=("codex",),
        max_rounds=3,
        max_cost_usd=12.0,
        frictions=("note one",),
    )
    monkeypatch.setattr(
        main_mod, "resolve_project_settings", lambda url, meta: settings
    )
    monkeypatch.setattr(
        main_mod,
        "post_frictions",
        lambda url, task_id, frictions: captured.setdefault("frictions", frictions),
    )

    def fake_develop(config, **kw):
        captured["config"] = config
        return _result("approved", tmp_path)

    def fake_post(url, task_id, result, **kw):
        captured["posted"] = task_id
        return True

    monkeypatch.setattr(main_mod, "develop", fake_develop)
    monkeypatch.setattr(main_mod, "post_results", fake_post)

    argv, result_file = _daemon_args(tmp_git_repo, tmp_path)
    rc = main_mod.main(argv)
    assert rc == EXIT_SUCCEEDED

    cfg = captured["config"]
    assert cfg.description == "Add a flag\n\nBody."
    assert [s.name for s in cfg.reviewers] == ["security"]
    assert cfg.max_rounds == 3
    assert cfg.max_cost_usd == 12.0
    assert cfg.reviewer_fallback_chain == ("codex",)
    assert captured["frictions"] == ("note one",)
    assert captured["posted"] == "t-1"

    payload = json.loads(result_file.read_text(encoding="utf-8"))
    assert payload["task_id"] == "t-1"
    assert payload["status"] == "succeeded"
    _validate_result_schema(payload)


def test_daemon_mode_config_rejection_writes_failed_result(
    tmp_git_repo: Path, tmp_path: Path, monkeypatch
) -> None:
    """A core ValueError (e.g. metadata named an unsupported tool) is a
    do-not-retry config failure reported through result.json."""
    from lithos_loom.plugins.story_develop import __main__ as main_mod
    from lithos_loom.plugins.story_develop.daemon_io import ProjectDevelopSettings

    monkeypatch.setattr(
        main_mod,
        "resolve_project_settings",
        lambda url, meta: ProjectDevelopSettings(),
    )
    monkeypatch.setattr(main_mod, "post_frictions", lambda *a: None)

    def boom(config, **kw):
        raise ValueError("unsupported coder tool")

    monkeypatch.setattr(main_mod, "develop", boom)

    argv, result_file = _daemon_args(tmp_git_repo, tmp_path)
    rc = main_mod.main(argv)
    assert rc == EXIT_BAD_INPUT

    payload = json.loads(result_file.read_text(encoding="utf-8"))
    assert payload["status"] == "failed"
    assert payload["error"]["category"] == "config"
    assert "unsupported coder tool" in payload["error"]["message"]
    _validate_result_schema(payload)


def test_daemon_mode_interrupted_run_reports_resume(
    tmp_git_repo: Path, tmp_path: Path, monkeypatch
) -> None:
    from lithos_loom.plugins.story_develop import __main__ as main_mod
    from lithos_loom.plugins.story_develop.daemon_io import ProjectDevelopSettings

    monkeypatch.setattr(
        main_mod,
        "resolve_project_settings",
        lambda url, meta: ProjectDevelopSettings(),
    )
    monkeypatch.setattr(main_mod, "post_frictions", lambda *a: None)
    monkeypatch.setattr(main_mod, "post_results", lambda *a, **kw: True)
    resume_at = datetime(2026, 6, 12, 15, 0, 0, tzinfo=UTC)
    monkeypatch.setattr(
        main_mod,
        "develop",
        lambda c, **kw: _result("interrupted", tmp_path, resume_after=resume_at),
    )

    argv, result_file = _daemon_args(tmp_git_repo, tmp_path)
    rc = main_mod.main(argv)
    assert rc == EXIT_INTERRUPTED

    payload = json.loads(result_file.read_text(encoding="utf-8"))
    assert payload["status"] == "interrupted"
    assert payload["resume"]["resume_after"] == "2026-06-12T15:00:00+00:00"
    _validate_result_schema(payload)
