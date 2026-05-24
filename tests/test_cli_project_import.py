"""Tests for ``lithos-loom project import`` (Slice 5 US37).

Same shape as ``test_cli_project_create.py`` — Typer CliRunner +
mocked LithosClient. Focus on the import-specific decisions:
frontmatter parsing, title derivation, tag union, lithos_id rejection.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

from typer.testing import CliRunner

from lithos_loom.cli.project import project_app
from lithos_loom.lithos_client import Note, WriteResult


def _write_config(tmp_path: Path) -> Path:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        f"""
[orchestrator]
agent_id = "test-agent"
lithos_url = "http://localhost:8765"

[obsidian_sync]
vault_path = "{tmp_path / "vault"}"
tasks_file = "_lithos/tasks.md"
projects_dir = "_lithos/projects"
""",
        encoding="utf-8",
    )
    return cfg_path


def _canonical_note(doc_id: str = "new-doc") -> Note:
    return Note(
        id=doc_id,
        title="x",
        body="x",
        version=1,
        updated_at=datetime(2026, 5, 24, tzinfo=UTC),
        tags=("project-context",),
        status="active",
        note_type="concept",
        path="projects/imported/imported-project-context.md",
        slug="imported",
    )


def _stub_client() -> Any:
    """Build an AsyncMock that masquerades as a LithosClient context manager.

    ``note_write`` returns the production-shaped envelope (``note=None``)
    because real Lithos doesn't populate a ``document`` field in the
    success response. The handler re-fetches via ``note_read`` to get
    the canonical doc id — the stub returns a populated Note from
    that re-fetch so happy-path tests get a real id back.
    """
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.__aexit__.return_value = None
    client.note_list.return_value = []
    client.note_write.return_value = WriteResult(status="created", note=None)
    client.note_read.return_value = _canonical_note()
    return client


# ── Happy paths ────────────────────────────────────────────────────────


def test_import_plain_file_uses_stem_as_title(tmp_path: Path) -> None:
    """No frontmatter → title comes from the file stem."""
    cfg_path = _write_config(tmp_path)
    source = tmp_path / "my-project.md"
    source.write_text("# Anything\n\nBody content\n", encoding="utf-8")
    runner = CliRunner()
    client = _stub_client()

    with patch("lithos_loom.cli.project.LithosClient", return_value=client):
        result = runner.invoke(
            project_app, ["import", str(source), "-c", str(cfg_path)]
        )

    assert result.exit_code == 0, result.stdout
    kwargs = client.note_write.await_args.kwargs
    assert kwargs["title"] == "My Project"
    assert kwargs["path"] == "projects/my-project/my-project-project-context.md"
    assert kwargs["content"] == "# Anything\n\nBody content\n"


def test_import_uses_frontmatter_title_when_present(tmp_path: Path) -> None:
    cfg_path = _write_config(tmp_path)
    source = tmp_path / "foo.md"
    source.write_text(
        "---\ntitle: Curated Project Title\n---\nBody\n", encoding="utf-8"
    )
    runner = CliRunner()
    client = _stub_client()

    with patch("lithos_loom.cli.project.LithosClient", return_value=client):
        result = runner.invoke(
            project_app, ["import", str(source), "-c", str(cfg_path)]
        )

    assert result.exit_code == 0, result.stdout
    kwargs = client.note_write.await_args.kwargs
    assert kwargs["title"] == "Curated Project Title"
    expected_path = (
        "projects/curated-project-title/curated-project-title-project-context.md"
    )
    assert kwargs["path"] == expected_path
    # Body excludes frontmatter.
    assert kwargs["content"] == "Body\n"


def test_import_merges_frontmatter_and_cli_tags(tmp_path: Path) -> None:
    """Frontmatter tags + --tags + project-context, deduplicated, order preserved."""
    cfg_path = _write_config(tmp_path)
    source = tmp_path / "foo.md"
    source.write_text(
        "---\ntitle: Foo\ntags:\n  - alpha\n  - beta\n---\nbody\n",
        encoding="utf-8",
    )
    runner = CliRunner()
    client = _stub_client()

    with patch("lithos_loom.cli.project.LithosClient", return_value=client):
        result = runner.invoke(
            project_app,
            [
                "import",
                str(source),
                "-c",
                str(cfg_path),
                "--tags",
                "beta,gamma",
            ],
        )

    assert result.exit_code == 0, result.stdout
    assert client.note_write.await_args.kwargs["tags"] == [
        "alpha",
        "beta",
        "gamma",
        "project-context",
    ]


def test_import_with_explicit_slug_override(tmp_path: Path) -> None:
    cfg_path = _write_config(tmp_path)
    source = tmp_path / "foo.md"
    source.write_text("body", encoding="utf-8")
    runner = CliRunner()
    client = _stub_client()

    with patch("lithos_loom.cli.project.LithosClient", return_value=client):
        result = runner.invoke(
            project_app,
            ["import", str(source), "-c", str(cfg_path), "--slug", "explicit-slug"],
        )

    assert result.exit_code == 0, result.stdout
    assert (
        client.note_write.await_args.kwargs["path"]
        == "projects/explicit-slug/explicit-slug-project-context.md"
    )


def test_import_json_output(tmp_path: Path) -> None:
    cfg_path = _write_config(tmp_path)
    source = tmp_path / "foo.md"
    source.write_text("body", encoding="utf-8")
    runner = CliRunner()
    client = _stub_client()

    with patch("lithos_loom.cli.project.LithosClient", return_value=client):
        result = runner.invoke(
            project_app,
            ["import", str(source), "-c", str(cfg_path), "--format", "json"],
        )

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["id"] == "new-doc"
    assert payload["slug"] == "foo"
    assert payload["vault_path"].endswith(".md")


# ── Rejection: already-projected file ──────────────────────────────────


def test_import_rejects_file_with_lithos_id_in_frontmatter(tmp_path: Path) -> None:
    """Refuse to re-import a file that already has a Lithos id —
    would create a duplicate doc."""
    cfg_path = _write_config(tmp_path)
    source = tmp_path / "exported.md"
    source.write_text(
        "---\nlithos_id: existing-uuid\nlithos_version: 5\n---\n# Existing\n\nBody\n",
        encoding="utf-8",
    )
    runner = CliRunner()
    client = _stub_client()

    with patch("lithos_loom.cli.project.LithosClient", return_value=client):
        result = runner.invoke(
            project_app, ["import", str(source), "-c", str(cfg_path)]
        )

    assert result.exit_code == 2
    combined = result.stdout + (result.stderr if hasattr(result, "stderr") else "")
    assert "lithos_id" in combined
    assert "existing-uuid" in combined
    # No write attempted.
    client.note_write.assert_not_called()


# ── Misc errors ────────────────────────────────────────────────────────


def test_import_rejects_missing_file(tmp_path: Path) -> None:
    cfg_path = _write_config(tmp_path)
    runner = CliRunner()

    result = runner.invoke(
        project_app,
        ["import", str(tmp_path / "nope.md"), "-c", str(cfg_path)],
    )

    assert result.exit_code == 2
    combined = result.stdout + (result.stderr if hasattr(result, "stderr") else "")
    assert "could not read" in combined


def test_import_rejects_missing_obsidian_sync(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        """
[orchestrator]
agent_id = "test-agent"
lithos_url = "http://localhost:8765"
""",
        encoding="utf-8",
    )
    source = tmp_path / "foo.md"
    source.write_text("body", encoding="utf-8")
    runner = CliRunner()

    result = runner.invoke(project_app, ["import", str(source), "-c", str(cfg_path)])

    assert result.exit_code == 2
    combined = result.stdout + (result.stderr if hasattr(result, "stderr") else "")
    assert "[obsidian_sync]" in combined
