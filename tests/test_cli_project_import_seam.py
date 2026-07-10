"""Direct unit tests for the ``import_project`` orchestration seam (ARCH-11).

``project import``'s sequencing was lifted out of the Typer command into
``cli.project.import_project(cfg, source, ...) -> DryRunResult | AbortedResult |
ImportedResult`` (raising ``ProjectImportError(message, exit_code)`` on failure).
These tests exercise that seam *directly* — no ``CliRunner`` — which is the whole
point of the extraction: every exit path is a typed result or a typed error,
and the interactive confirmation is an injected callback.

The end-to-end CLI rendering (echo/exit/format) stays pinned by
``test_cli_project_import.py``.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from lithos_loom.cli._project_import_bulk import ProjectImportError
from lithos_loom.cli.project import (
    AbortedResult,
    DryRunResult,
    ImportedResult,
    import_project,
)
from lithos_loom.config import LoomConfig, load_config
from lithos_loom.lithos_client import Note, WriteResult

_CONFIG = """
[orchestrator]
agent_id = "test-agent"
lithos_url = "http://localhost:8765"

[obsidian_sync]
vault_path = "{vault}"
tasks_file = "_lithos/tasks.md"
projects_dir = "_lithos/projects"
"""

_CONFIG_NO_OBSIDIAN = """
[orchestrator]
agent_id = "test-agent"
lithos_url = "http://localhost:8765"
"""


def _cfg(tmp_path: Path, *, with_obsidian: bool = True) -> LoomConfig:
    cfg_path = tmp_path / "config.toml"
    body = (
        _CONFIG.format(vault=tmp_path / "vault")
        if with_obsidian
        else _CONFIG_NO_OBSIDIAN
    )
    cfg_path.write_text(body, encoding="utf-8")
    return load_config(cfg_path)


def _source(tmp_path: Path, text: str, name: str = "my-project.md") -> Path:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def _stub_client() -> Any:
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.__aexit__.return_value = None
    client.note_list.return_value = []
    client.note_write.return_value = WriteResult(
        status="created",
        note=Note(
            id="new-doc",
            title="x",
            body="x",
            version=1,
            updated_at=None,
            tags=("project-context",),
            status="active",
            note_type="concept",
            path="projects/my-project/my-project-project-context.md",
            slug="my-project",
        ),
    )
    return client


def _never(_message: str) -> bool:  # a confirm callback that must not be called
    raise AssertionError("confirm should not be called")


# ── Failure paths raise ProjectImportError(message, exit_code) ─────────


def test_flag_conflict_raises_exit_2(tmp_path: Path) -> None:
    with pytest.raises(ProjectImportError) as exc:
        import_project(
            cfg=_cfg(tmp_path),
            source=_source(tmp_path, "body"),
            slug=None,
            tags=None,
            tasks_only=True,
            no_tasks=True,
            force_tasks=False,
            yes=False,
            dry_run=False,
            confirm=_never,
        )
    assert exc.value.exit_code == 2
    assert "mutually exclusive" in exc.value.message


def test_missing_obsidian_sync_raises_exit_2(tmp_path: Path) -> None:
    with pytest.raises(ProjectImportError) as exc:
        import_project(
            cfg=_cfg(tmp_path, with_obsidian=False),
            source=_source(tmp_path, "body"),
            slug=None,
            tags=None,
            tasks_only=False,
            no_tasks=False,
            force_tasks=False,
            yes=False,
            dry_run=False,
            confirm=_never,
        )
    assert exc.value.exit_code == 2
    assert "[obsidian_sync]" in exc.value.message


def test_unreadable_source_raises_exit_2(tmp_path: Path) -> None:
    with pytest.raises(ProjectImportError) as exc:
        import_project(
            cfg=_cfg(tmp_path),
            source=tmp_path / "does-not-exist.md",
            slug=None,
            tags=None,
            tasks_only=False,
            no_tasks=False,
            force_tasks=False,
            yes=False,
            dry_run=False,
            confirm=_never,
        )
    assert exc.value.exit_code == 2
    assert "could not read" in exc.value.message


def test_greenfield_refuses_already_projected(tmp_path: Path) -> None:
    src = _source(tmp_path, "---\nlithos_id: abc-123\n---\nbody\n")
    with pytest.raises(ProjectImportError) as exc:
        import_project(
            cfg=_cfg(tmp_path),
            source=src,
            slug=None,
            tags=None,
            tasks_only=False,
            no_tasks=False,
            force_tasks=False,
            yes=False,
            dry_run=False,
            confirm=_never,
        )
    assert exc.value.exit_code == 2
    assert "already carries lithos_id" in exc.value.message


def test_invalid_slug_raises_exit_2(tmp_path: Path) -> None:
    with pytest.raises(ProjectImportError) as exc:
        import_project(
            cfg=_cfg(tmp_path),
            source=_source(tmp_path, "body"),
            slug="Not A Slug",
            tags=None,
            tasks_only=False,
            no_tasks=False,
            force_tasks=False,
            yes=False,
            dry_run=False,
            confirm=_never,
        )
    assert exc.value.exit_code == 2
    assert "invalid slug" in exc.value.message


# ── Success paths return typed outcomes ────────────────────────────────


def test_dry_run_returns_plan(tmp_path: Path) -> None:
    with patch("lithos_loom.cli.project.LithosClient", return_value=_stub_client()):
        result = import_project(
            cfg=_cfg(tmp_path),
            source=_source(tmp_path, "body"),
            slug=None,
            tags=None,
            tasks_only=False,
            no_tasks=False,
            force_tasks=False,
            yes=False,
            dry_run=True,
            confirm=_never,
        )
    assert isinstance(result, DryRunResult)
    assert "my-project" in result.plan_text


def test_greenfield_success_returns_imported_result(tmp_path: Path) -> None:
    with patch("lithos_loom.cli.project.LithosClient", return_value=_stub_client()):
        result = import_project(
            cfg=_cfg(tmp_path),
            source=_source(tmp_path, "# Heading\n\nbody\n"),
            slug=None,
            tags=None,
            tasks_only=False,
            no_tasks=False,
            force_tasks=False,
            yes=False,
            dry_run=False,
            confirm=_never,
        )
    assert isinstance(result, ImportedResult)
    assert result.id == "new-doc"
    assert result.slug == "my-project"
    assert result.tasks_created == 0
    assert result.vault_path.name == "my-project-project-context.md"


def test_declined_confirmation_returns_aborted(tmp_path: Path) -> None:
    """tasks-only + existing open tasks + --force-tasks, confirm declined →
    AbortedResult with a clean (exit-0) message. The confirm callback is the
    seam's injection point for the interactive prompt."""
    preflight = AsyncMock(return_value=("proj-id", [SimpleNamespace(status="open")]))
    with patch("lithos_loom.cli.project.check_tasks_only_preflight", preflight):
        result = import_project(
            cfg=_cfg(tmp_path),
            source=_source(tmp_path, "- [ ] a task\n"),
            slug="existing-proj",
            tags=None,
            tasks_only=True,
            no_tasks=False,
            force_tasks=True,
            yes=False,
            dry_run=False,
            confirm=lambda _message: False,
        )
    assert isinstance(result, AbortedResult)
    assert result.message == "aborted; no changes made"


def test_confirmed_force_tasks_proceeds(tmp_path: Path) -> None:
    """The same path with confirm accepted runs cleanup + create and returns
    an ImportedResult — proving the callback drives the branch."""
    preflight = AsyncMock(return_value=("proj-id", [SimpleNamespace(status="open")]))
    cleanup = AsyncMock(return_value=1)
    create = AsyncMock(return_value=1)
    with (
        patch("lithos_loom.cli.project.check_tasks_only_preflight", preflight),
        patch("lithos_loom.cli.project.force_tasks_cleanup", cleanup),
        patch("lithos_loom.cli.project.create_tasks", create),
    ):
        result = import_project(
            cfg=_cfg(tmp_path),
            source=_source(tmp_path, "- [ ] a task\n"),
            slug="existing-proj",
            tags=None,
            tasks_only=True,
            no_tasks=False,
            force_tasks=True,
            yes=False,
            dry_run=False,
            confirm=lambda _message: True,
        )
    assert isinstance(result, ImportedResult)
    assert result.id == "proj-id"
    assert result.tasks_created == 1
    cleanup.assert_awaited_once()
    create.assert_awaited_once()
