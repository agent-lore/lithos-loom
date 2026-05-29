"""Tests for the github-watcher per-project CLI subcommands (Slice 7.1).

Three Typer subcommands plumb tag mutations on a project-context doc:

- ``project set-github-repo`` adds/replaces a ``github-repo:owner/name`` tag.
- ``project enable-github`` adds a ``github-watch`` tag (requires repo set).
- ``project disable-github`` removes the ``github-watch`` tag.

All three share ``mutate_project_context_tags`` which handles read →
mutate → CAS-write with version-conflict retry. Tests cover both the
pure helpers (validation, tag inspection) and the CLI integration with
a stubbed ``LithosClient``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from typer.testing import CliRunner

from lithos_loom.cli._github_metadata import (
    GITHUB_REPO_TAG_PREFIX,
    GITHUB_WATCH_TAG,
    GithubMetadataError,
    extract_github_repo,
    is_github_watching,
    validate_github_repo,
)
from lithos_loom.cli.project import project_app
from lithos_loom.errors import LithosClientError
from lithos_loom.lithos_client import Note, WriteResult

# ── Pure helpers ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "value",
    [
        "agent-lore/lithos-loom",
        "a/b",
        "Owner-1/repo_with.dots",
        "ORG/Name.With-Dashes",
    ],
)
def test_validate_github_repo_accepts_valid(value: str) -> None:
    assert validate_github_repo(value) == value


@pytest.mark.parametrize(
    "value",
    [
        "",
        "no-slash",
        "/no-owner",
        "no-name/",
        "with spaces/repo",
        "-leading-hyphen/repo",
        "owner/",
        "double//slash",
        "owner/repo/extra",
    ],
)
def test_validate_github_repo_rejects_invalid(value: str) -> None:
    with pytest.raises(GithubMetadataError, match="invalid github repo"):
        validate_github_repo(value)


def test_extract_github_repo_finds_prefixed_tag() -> None:
    tags = ("project-context", f"{GITHUB_REPO_TAG_PREFIX}agent-lore/lithos-loom")
    assert extract_github_repo(tags) == "agent-lore/lithos-loom"


def test_extract_github_repo_returns_none_when_absent() -> None:
    assert extract_github_repo(("project-context", "other-tag")) is None
    assert extract_github_repo(()) is None


def test_is_github_watching_checks_tag_presence() -> None:
    assert is_github_watching((GITHUB_WATCH_TAG, "x")) is True
    assert is_github_watching(("project-context",)) is False


# ── CLI test plumbing ─────────────────────────────────────────────────


def _write_config(tmp_path: Path) -> Path:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        f"""
[orchestrator]
agent_id = "test-agent"
lithos_url = "http://localhost:8765"

[obsidian_sync]
vault_path = "{tmp_path / "vault"}"
""",
        encoding="utf-8",
    )
    return cfg_path


def _make_note(
    *,
    doc_id: str = "doc-1",
    tags: tuple[str, ...] = ("project-context",),
    version: int = 1,
    path: str = "projects/my-slug/my-slug-project-context.md",
) -> Note:
    return Note(
        id=doc_id,
        title="My Slug",
        body="body",
        version=version,
        updated_at=datetime(2026, 5, 29, 12, 0, 0, tzinfo=UTC),
        tags=tags,
        status="active",
        note_type="concept",
        path=path,
        slug="my-slug",
    )


def _stub_client(
    *,
    initial_note: Note,
    write_result: WriteResult | None = None,
    note_read_sequence: list[Note] | None = None,
    write_sequence: list[WriteResult] | None = None,
) -> AsyncMock:
    """Build an AsyncMock LithosClient with the typical happy-path defaults.

    Pass ``note_read_sequence`` / ``write_sequence`` to drive CAS-retry
    scenarios (multiple reads, multiple writes).
    """
    client = AsyncMock()
    client.__aenter__.return_value = client

    async def aexit(exc_type: type | None, exc: BaseException | None, tb: Any) -> None:
        # Mirror the anyio.TaskGroup wrap so tests can see whether
        # exceptions escape inside or outside the async-with block.
        if exc is not None:
            raise BaseExceptionGroup("simulated anyio wrap", [exc])

    client.__aexit__.side_effect = aexit

    if note_read_sequence is not None:
        client.note_read.side_effect = note_read_sequence
    else:
        client.note_read.return_value = initial_note

    if write_sequence is not None:
        client.note_write.side_effect = write_sequence
    elif write_result is not None:
        client.note_write.return_value = write_result
    else:
        client.note_write.return_value = WriteResult(
            status="updated", note=initial_note
        )
    return client


# ── set-github-repo ───────────────────────────────────────────────────


def test_set_github_repo_writes_new_tag(tmp_path: Path) -> None:
    cfg_path = _write_config(tmp_path)
    initial = _make_note(tags=("project-context",))
    client = _stub_client(initial_note=initial)
    runner = CliRunner()

    with patch("lithos_loom.cli._github_metadata.LithosClient", return_value=client):
        result = runner.invoke(
            project_app,
            [
                "set-github-repo",
                "-c",
                str(cfg_path),
                "my-slug",
                "agent-lore/lithos-loom",
            ],
        )
    assert result.exit_code == 0, result.stdout
    client.note_write.assert_awaited_once()
    written = client.note_write.await_args.kwargs
    assert written["id"] == "doc-1"
    assert written["expected_version"] == 1
    assert "github-repo:agent-lore/lithos-loom" in written["tags"]
    assert "github repo set" in result.stdout


def test_set_github_repo_replaces_existing_tag(tmp_path: Path) -> None:
    cfg_path = _write_config(tmp_path)
    initial = _make_note(tags=("project-context", "github-repo:old-owner/old-repo"))
    client = _stub_client(initial_note=initial)
    runner = CliRunner()

    with patch("lithos_loom.cli._github_metadata.LithosClient", return_value=client):
        result = runner.invoke(
            project_app,
            ["set-github-repo", "-c", str(cfg_path), "my-slug", "new-owner/new-repo"],
        )
    assert result.exit_code == 0, result.stdout
    written_tags = client.note_write.await_args.kwargs["tags"]
    assert "github-repo:old-owner/old-repo" not in written_tags
    assert "github-repo:new-owner/new-repo" in written_tags


def test_set_github_repo_idempotent_when_already_correct(tmp_path: Path) -> None:
    """Re-running with the same repo should print success and skip the write."""
    cfg_path = _write_config(tmp_path)
    initial = _make_note(tags=("project-context", "github-repo:agent-lore/lithos-loom"))
    client = _stub_client(initial_note=initial)
    runner = CliRunner()

    with patch("lithos_loom.cli._github_metadata.LithosClient", return_value=client):
        result = runner.invoke(
            project_app,
            [
                "set-github-repo",
                "-c",
                str(cfg_path),
                "my-slug",
                "agent-lore/lithos-loom",
            ],
        )
    assert result.exit_code == 0, result.stdout
    client.note_write.assert_not_called()
    assert "already set" in result.stdout


def test_set_github_repo_invalid_repo_format(tmp_path: Path) -> None:
    cfg_path = _write_config(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        project_app,
        ["set-github-repo", "-c", str(cfg_path), "my-slug", "not-a-valid-repo"],
    )
    assert result.exit_code == 2
    combined = result.stdout + (result.stderr if hasattr(result, "stderr") else "")
    assert "invalid github repo" in combined


def test_set_github_repo_doc_not_found(tmp_path: Path) -> None:
    cfg_path = _write_config(tmp_path)
    # note_read returns None → canonical doc missing.
    client = AsyncMock()
    client.__aenter__.return_value = client

    async def aexit(exc_type: type | None, exc: BaseException | None, tb: Any) -> None:
        if exc is not None:
            raise BaseExceptionGroup("wrap", [exc])

    client.__aexit__.side_effect = aexit
    client.note_read.return_value = None
    runner = CliRunner()

    with patch("lithos_loom.cli._github_metadata.LithosClient", return_value=client):
        result = runner.invoke(
            project_app,
            ["set-github-repo", "-c", str(cfg_path), "my-slug", "x/y"],
        )
    assert result.exit_code == 2
    combined = result.stdout + (result.stderr if hasattr(result, "stderr") else "")
    assert "no canonical project-context doc" in combined


def test_set_github_repo_invalid_slug(tmp_path: Path) -> None:
    cfg_path = _write_config(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        project_app,
        ["set-github-repo", "-c", str(cfg_path), "BadSlug!", "x/y"],
    )
    assert result.exit_code == 2
    combined = result.stdout + (result.stderr if hasattr(result, "stderr") else "")
    assert "invalid slug" in combined


# ── enable-github ─────────────────────────────────────────────────────


def test_enable_github_adds_watch_tag(tmp_path: Path) -> None:
    cfg_path = _write_config(tmp_path)
    initial = _make_note(tags=("project-context", "github-repo:agent-lore/lithos-loom"))
    client = _stub_client(initial_note=initial)
    runner = CliRunner()

    with patch("lithos_loom.cli._github_metadata.LithosClient", return_value=client):
        result = runner.invoke(
            project_app,
            ["enable-github", "-c", str(cfg_path), "my-slug"],
        )
    assert result.exit_code == 0, result.stdout
    written_tags = client.note_write.await_args.kwargs["tags"]
    assert GITHUB_WATCH_TAG in written_tags
    assert "github-repo:agent-lore/lithos-loom" in written_tags  # preserved


def test_enable_github_idempotent_when_already_watching(tmp_path: Path) -> None:
    cfg_path = _write_config(tmp_path)
    initial = _make_note(
        tags=(
            "project-context",
            "github-repo:agent-lore/lithos-loom",
            GITHUB_WATCH_TAG,
        )
    )
    client = _stub_client(initial_note=initial)
    runner = CliRunner()

    with patch("lithos_loom.cli._github_metadata.LithosClient", return_value=client):
        result = runner.invoke(
            project_app,
            ["enable-github", "-c", str(cfg_path), "my-slug"],
        )
    assert result.exit_code == 0
    client.note_write.assert_not_called()
    assert "already enabled" in result.stdout


def test_enable_github_requires_repo_set_first(tmp_path: Path) -> None:
    """No `github-repo:*` tag → operator-actionable error, no write."""
    cfg_path = _write_config(tmp_path)
    initial = _make_note(tags=("project-context",))
    client = _stub_client(initial_note=initial)
    runner = CliRunner()

    with patch("lithos_loom.cli._github_metadata.LithosClient", return_value=client):
        result = runner.invoke(
            project_app,
            ["enable-github", "-c", str(cfg_path), "my-slug"],
        )
    assert result.exit_code == 2
    client.note_write.assert_not_called()
    combined = result.stdout + (result.stderr if hasattr(result, "stderr") else "")
    assert "no github-repo tag" in combined


# ── disable-github ────────────────────────────────────────────────────


def test_disable_github_removes_watch_tag(tmp_path: Path) -> None:
    cfg_path = _write_config(tmp_path)
    initial = _make_note(
        tags=(
            "project-context",
            "github-repo:agent-lore/lithos-loom",
            GITHUB_WATCH_TAG,
        )
    )
    client = _stub_client(initial_note=initial)
    runner = CliRunner()

    with patch("lithos_loom.cli._github_metadata.LithosClient", return_value=client):
        result = runner.invoke(
            project_app,
            ["disable-github", "-c", str(cfg_path), "my-slug"],
        )
    assert result.exit_code == 0, result.stdout
    written_tags = client.note_write.await_args.kwargs["tags"]
    assert GITHUB_WATCH_TAG not in written_tags
    # Repo mapping preserved.
    assert "github-repo:agent-lore/lithos-loom" in written_tags


def test_disable_github_idempotent_when_already_disabled(tmp_path: Path) -> None:
    cfg_path = _write_config(tmp_path)
    initial = _make_note(tags=("project-context", "github-repo:agent-lore/lithos-loom"))
    client = _stub_client(initial_note=initial)
    runner = CliRunner()

    with patch("lithos_loom.cli._github_metadata.LithosClient", return_value=client):
        result = runner.invoke(
            project_app,
            ["disable-github", "-c", str(cfg_path), "my-slug"],
        )
    assert result.exit_code == 0
    client.note_write.assert_not_called()
    assert "already disabled" in result.stdout


# ── CAS retry ─────────────────────────────────────────────────────────


def test_cas_retries_on_version_conflict(tmp_path: Path) -> None:
    """A version_conflict on first write triggers re-read + retry."""
    cfg_path = _write_config(tmp_path)
    note_v1 = _make_note(version=1, tags=("project-context",))
    # Concurrent writer landed; v2 has different tags but no github-repo yet.
    note_v2 = _make_note(version=2, tags=("project-context", "extra"))

    client = _stub_client(
        initial_note=note_v1,
        note_read_sequence=[note_v1, note_v2],
        write_sequence=[
            WriteResult(status="version_conflict", current_version=2),
            WriteResult(status="updated", note=note_v2),
        ],
    )
    runner = CliRunner()

    with patch("lithos_loom.cli._github_metadata.LithosClient", return_value=client):
        result = runner.invoke(
            project_app,
            ["set-github-repo", "-c", str(cfg_path), "my-slug", "x/y"],
        )
    assert result.exit_code == 0, result.stdout
    assert client.note_read.await_count == 2
    assert client.note_write.await_count == 2
    # Second write used the fresh version + included the concurrent writer's tag.
    second_write = client.note_write.await_args_list[1].kwargs
    assert second_write["expected_version"] == 2
    assert "extra" in second_write["tags"]
    assert "github-repo:x/y" in second_write["tags"]


def test_cas_exhausts_after_three_conflicts(tmp_path: Path) -> None:
    """Three back-to-back conflicts surface a friendly error, no spinning."""
    cfg_path = _write_config(tmp_path)
    note = _make_note()
    client = _stub_client(
        initial_note=note,
        note_read_sequence=[note, note, note],
        write_sequence=[
            WriteResult(status="version_conflict", current_version=1),
            WriteResult(status="version_conflict", current_version=1),
            WriteResult(status="version_conflict", current_version=1),
        ],
    )
    runner = CliRunner()

    with patch("lithos_loom.cli._github_metadata.LithosClient", return_value=client):
        result = runner.invoke(
            project_app,
            ["set-github-repo", "-c", str(cfg_path), "my-slug", "x/y"],
        )
    assert result.exit_code == 2
    combined = result.stdout + (result.stderr if hasattr(result, "stderr") else "")
    assert "CAS attempts" in combined


def test_unexpected_write_status_raises(tmp_path: Path) -> None:
    """A write status outside the documented set surfaces a typed error."""
    cfg_path = _write_config(tmp_path)
    note = _make_note()
    client = _stub_client(
        initial_note=note,
        write_result=WriteResult(
            status="content_too_large",
            message="body exceeds 1MB limit",
        ),
    )
    runner = CliRunner()

    with patch("lithos_loom.cli._github_metadata.LithosClient", return_value=client):
        result = runner.invoke(
            project_app,
            ["set-github-repo", "-c", str(cfg_path), "my-slug", "x/y"],
        )
    # LithosClientError → exit 1 (not user-actionable validation).
    assert result.exit_code == 1


def test_oserror_during_read_surfaces_cleanly(tmp_path: Path) -> None:
    """Transport failure during note_read still reaches the typed handler."""
    cfg_path = _write_config(tmp_path)
    client = AsyncMock()
    client.__aenter__.return_value = client

    async def aexit(exc_type: type | None, exc: BaseException | None, tb: Any) -> None:
        if exc is not None:
            raise BaseExceptionGroup("wrap", [exc])

    client.__aexit__.side_effect = aexit
    client.note_read.side_effect = OSError("connection refused")
    runner = CliRunner()

    with patch("lithos_loom.cli._github_metadata.LithosClient", return_value=client):
        result = runner.invoke(
            project_app,
            ["set-github-repo", "-c", str(cfg_path), "my-slug", "x/y"],
        )
    assert result.exit_code == 1
    combined = result.stdout + (result.stderr if hasattr(result, "stderr") else "")
    assert "connection refused" in combined
    # __aexit__ saw a clean exit (the error was caught inside the with block).
    exit_call = client.__aexit__.await_args
    assert exit_call is not None
    assert exit_call.args[0] is None


def test_lithos_client_error_during_read_surfaces_cleanly(tmp_path: Path) -> None:
    cfg_path = _write_config(tmp_path)
    client = AsyncMock()
    client.__aenter__.return_value = client

    async def aexit(exc_type: type | None, exc: BaseException | None, tb: Any) -> None:
        if exc is not None:
            raise BaseExceptionGroup("wrap", [exc])

    client.__aexit__.side_effect = aexit
    client.note_read.side_effect = LithosClientError("invalid_input", "bad path")
    runner = CliRunner()

    with patch("lithos_loom.cli._github_metadata.LithosClient", return_value=client):
        result = runner.invoke(
            project_app,
            ["set-github-repo", "-c", str(cfg_path), "my-slug", "x/y"],
        )
    assert result.exit_code == 1
