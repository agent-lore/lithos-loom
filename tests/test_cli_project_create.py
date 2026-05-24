"""Tests for ``lithos-loom project create`` (Slice 5 US36 CLI half).

Pure unit tests for the slugify + tag helpers, plus end-to-end CLI
tests that mock ``LithosClient`` so they don't shell out to a real
Lithos. Uses Typer's ``CliRunner`` for assertions on exit codes /
stdout / stderr.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from typer.testing import CliRunner

from lithos_loom.cli.project import (
    _merge_tags,
    _project_tags,
    _slugify,
    _title_from_stem,
    project_app,
)
from lithos_loom.errors import LithosClientError
from lithos_loom.lithos_client import Note, NoteSummary, WriteResult

# ── Pure helper tests ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "title,expected",
    [
        ("Lithos Loom", "lithos-loom"),
        ("My Cool Project!", "my-cool-project"),
        ("  spaced  ", "spaced"),
        ("MixedCASE", "mixedcase"),
        ("with_underscore", "with-underscore"),
        ("café", "cafe"),
        ("---weird---", "weird"),
        ("multi   spaces", "multi-spaces"),
        ("digits-123", "digits-123"),
        ("only-symbols-!@#$", "only-symbols"),
    ],
)
def test_slugify(title: str, expected: str) -> None:
    assert _slugify(title) == expected


def test_slugify_empty_returns_empty() -> None:
    """All-symbols input collapses to empty — caller validates against
    _SLUG_RE which rejects empty."""
    assert _slugify("!!!") == ""
    assert _slugify("") == ""


def test_project_tags_always_appends_project_context() -> None:
    assert _project_tags(None) == ["project-context"]
    assert _project_tags("") == ["project-context"]
    assert _project_tags("foo,bar") == ["foo", "bar", "project-context"]


def test_project_tags_deduplicates_existing_project_context() -> None:
    """Operator-supplied project-context doesn't produce a duplicate."""
    tags = _project_tags("project-context,foo")
    assert tags == ["project-context", "foo"]
    assert tags.count("project-context") == 1


def test_merge_tags_preserves_order_and_dedups() -> None:
    """Frontmatter → extra → project-context, no duplicates."""
    result = _merge_tags(["alpha", "beta"], "beta, gamma")
    assert result == ["alpha", "beta", "gamma", "project-context"]


def test_merge_tags_empty_inputs() -> None:
    assert _merge_tags([], None) == ["project-context"]
    assert _merge_tags([], "") == ["project-context"]


def test_title_from_stem_handles_dashes_and_underscores() -> None:
    assert _title_from_stem("my-project") == "My Project"
    assert _title_from_stem("foo_bar") == "Foo Bar"
    assert _title_from_stem("MixedCase") == "Mixedcase"


# ── CLI helpers ────────────────────────────────────────────────────────


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


_DEFAULT_PATH = "projects/my-slug/my-slug-project-context.md"


def _make_note(doc_id: str = "doc-1", path: str = _DEFAULT_PATH) -> Note:
    from datetime import UTC, datetime

    return Note(
        id=doc_id,
        title="My Slug",
        body="body",
        version=1,
        updated_at=datetime(2026, 5, 24, 14, 30, 0, tzinfo=UTC),
        tags=("project-context",),
        status="active",
        note_type="concept",
        path=path,
        slug="my-slug",
    )


def _stub_client(
    note_list_return: list[NoteSummary] | None = None,
    note_write_return: WriteResult | None = None,
) -> Any:
    """Build an AsyncMock that masquerades as a LithosClient context manager.

    The default ``note_write`` return value is a fully-populated
    ``WriteResult`` (``note=_make_note()``) because that's what
    ``LithosClient.note_write`` produces in production after stitching
    the top-level response's id/path/version with the request inputs.
    Tests that exercise the stitch directly live in
    ``tests/test_lithos_client.py``; here we just need the result the
    caller would actually see.
    """
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.__aexit__.return_value = None
    client.note_list.return_value = note_list_return or []
    client.note_write.return_value = note_write_return or WriteResult(
        status="created", note=_make_note()
    )
    return client


# ── Happy path ─────────────────────────────────────────────────────────


def test_create_happy_path_text_output(tmp_path: Path) -> None:
    cfg_path = _write_config(tmp_path)
    runner = CliRunner()
    client = _stub_client()

    with patch("lithos_loom.cli.project.LithosClient", return_value=client):
        result = runner.invoke(
            project_app,
            ["create", "-c", str(cfg_path), "--title", "My Slug"],
        )

    assert result.exit_code == 0, result.stdout
    expected_path = (
        tmp_path
        / "vault"
        / "_lithos"
        / "projects"
        / "my-slug"
        / "my-slug-project-context.md"
    )
    assert result.stdout.strip() == str(expected_path)

    # note_write called with the right shape.
    write_kwargs = client.note_write.await_args.kwargs
    assert write_kwargs["path"] == "projects/my-slug/my-slug-project-context.md"
    assert write_kwargs["title"] == "My Slug"
    assert write_kwargs["tags"] == ["project-context"]
    assert write_kwargs["note_type"] == "concept"


def test_create_json_output(tmp_path: Path) -> None:
    cfg_path = _write_config(tmp_path)
    runner = CliRunner()
    client = _stub_client()

    with patch("lithos_loom.cli.project.LithosClient", return_value=client):
        result = runner.invoke(
            project_app,
            ["create", "-c", str(cfg_path), "--title", "My Slug", "--format", "json"],
        )

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["id"] == "doc-1"
    assert payload["slug"] == "my-slug"
    assert payload["vault_path"].endswith("my-slug-project-context.md")


def test_create_with_body_inline(tmp_path: Path) -> None:
    cfg_path = _write_config(tmp_path)
    runner = CliRunner()
    client = _stub_client()

    with patch("lithos_loom.cli.project.LithosClient", return_value=client):
        result = runner.invoke(
            project_app,
            [
                "create",
                "-c",
                str(cfg_path),
                "--title",
                "My Slug",
                "--body",
                "Hello world",
            ],
        )

    assert result.exit_code == 0, result.stdout
    assert client.note_write.await_args.kwargs["content"] == "Hello world"


def test_create_with_body_file(tmp_path: Path) -> None:
    cfg_path = _write_config(tmp_path)
    body_file = tmp_path / "body.md"
    body_file.write_text("multiline\nbody\n", encoding="utf-8")
    runner = CliRunner()
    client = _stub_client()

    with patch("lithos_loom.cli.project.LithosClient", return_value=client):
        result = runner.invoke(
            project_app,
            [
                "create",
                "-c",
                str(cfg_path),
                "--title",
                "My Slug",
                "--body-file",
                str(body_file),
            ],
        )

    assert result.exit_code == 0, result.stdout
    assert client.note_write.await_args.kwargs["content"] == "multiline\nbody\n"


def test_create_with_explicit_slug_and_tags(tmp_path: Path) -> None:
    cfg_path = _write_config(tmp_path)
    runner = CliRunner()
    client = _stub_client(
        note_write_return=WriteResult(
            status="created",
            note=_make_note(path="projects/custom-slug/custom-slug-project-context.md"),
        )
    )

    with patch("lithos_loom.cli.project.LithosClient", return_value=client):
        result = runner.invoke(
            project_app,
            [
                "create",
                "-c",
                str(cfg_path),
                "--title",
                "Anything",
                "--slug",
                "custom-slug",
                "--tags",
                "track-1,prio",
            ],
        )

    assert result.exit_code == 0, result.stdout
    kwargs = client.note_write.await_args.kwargs
    assert kwargs["tags"] == ["track-1", "prio", "project-context"]
    assert kwargs["path"] == "projects/custom-slug/custom-slug-project-context.md"


# ── Validation failures ────────────────────────────────────────────────


def test_create_rejects_mutually_exclusive_body_flags(tmp_path: Path) -> None:
    cfg_path = _write_config(tmp_path)
    body_file = tmp_path / "body.md"
    body_file.write_text("x")
    runner = CliRunner()

    result = runner.invoke(
        project_app,
        [
            "create",
            "-c",
            str(cfg_path),
            "--title",
            "T",
            "--body",
            "x",
            "--body-file",
            str(body_file),
        ],
    )

    assert result.exit_code == 2
    # Typer's CliRunner with mix_stderr=True puts stderr in stdout.
    combined = result.stdout + (result.stderr if hasattr(result, "stderr") else "")
    assert "mutually exclusive" in combined


def test_create_rejects_invalid_slug(tmp_path: Path) -> None:
    cfg_path = _write_config(tmp_path)
    runner = CliRunner()

    result = runner.invoke(
        project_app,
        [
            "create",
            "-c",
            str(cfg_path),
            "--title",
            "anything",
            "--slug",
            "-leading-hyphen",
        ],
    )

    assert result.exit_code == 2
    combined = result.stdout + (result.stderr if hasattr(result, "stderr") else "")
    assert "invalid slug" in combined


def test_create_rejects_slug_collision(tmp_path: Path) -> None:
    """Pre-flight ``note_list`` finds an existing doc → exit 1 with
    a clear message pointing at the existing id."""
    cfg_path = _write_config(tmp_path)
    runner = CliRunner()
    from datetime import UTC, datetime

    existing_summary = NoteSummary(
        id="existing-doc-id",
        title="Existing",
        version=1,
        updated_at=datetime(2026, 5, 24, tzinfo=UTC),
        tags=("project-context",),
        status="active",
        note_type="concept",
        path="projects/my-slug/my-slug-project-context.md",
        slug="my-slug",
    )
    client = _stub_client(note_list_return=[existing_summary])

    with patch("lithos_loom.cli.project.LithosClient", return_value=client):
        result = runner.invoke(
            project_app, ["create", "-c", str(cfg_path), "--title", "My Slug"]
        )

    assert result.exit_code == 1
    combined = result.stdout + (result.stderr if hasattr(result, "stderr") else "")
    assert "already exists" in combined
    assert "existing-doc-id" in combined
    # Pre-flight blocked the write.
    client.note_write.assert_not_called()


def test_create_rejects_unreadable_body_file(tmp_path: Path) -> None:
    cfg_path = _write_config(tmp_path)
    nonexistent = tmp_path / "no-such-file.md"
    runner = CliRunner()

    result = runner.invoke(
        project_app,
        [
            "create",
            "-c",
            str(cfg_path),
            "--title",
            "T",
            "--body-file",
            str(nonexistent),
        ],
    )

    assert result.exit_code == 2
    combined = result.stdout + (result.stderr if hasattr(result, "stderr") else "")
    assert "could not read" in combined


def test_create_rejects_missing_obsidian_sync(tmp_path: Path) -> None:
    """Without [obsidian_sync] we can't compute the projected vault path
    for output. Refuse rather than printing a wrong path."""
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        """
[orchestrator]
agent_id = "test-agent"
lithos_url = "http://localhost:8765"
""",
        encoding="utf-8",
    )
    runner = CliRunner()

    result = runner.invoke(project_app, ["create", "-c", str(cfg_path), "--title", "T"])

    assert result.exit_code == 2
    combined = result.stdout + (result.stderr if hasattr(result, "stderr") else "")
    assert "[obsidian_sync]" in combined


def test_create_rejects_unknown_format(tmp_path: Path) -> None:
    cfg_path = _write_config(tmp_path)
    runner = CliRunner()
    client = _stub_client()

    with patch("lithos_loom.cli.project.LithosClient", return_value=client):
        result = runner.invoke(
            project_app,
            [
                "create",
                "-c",
                str(cfg_path),
                "--title",
                "T",
                "--format",
                "yaml",
            ],
        )

    assert result.exit_code == 2


# ── Server-side error propagation ──────────────────────────────────────


def test_create_propagates_lithos_client_error(tmp_path: Path) -> None:
    cfg_path = _write_config(tmp_path)
    runner = CliRunner()
    client = _stub_client()
    client.note_write.side_effect = LithosClientError("server_error", "boom")

    with patch("lithos_loom.cli.project.LithosClient", return_value=client):
        result = runner.invoke(
            project_app, ["create", "-c", str(cfg_path), "--title", "T"]
        )

    assert result.exit_code == 1
    combined = result.stdout + (result.stderr if hasattr(result, "stderr") else "")
    assert "note_write failed" in combined


def test_create_uses_id_from_writeresult_note(tmp_path: Path) -> None:
    """End-to-end check that the JSON output's ``id`` is the canonical
    Lithos doc id. ``LithosClient.note_write`` is responsible for
    stitching the top-level response's id into ``WriteResult.note``
    (the parser-side fix); the CLI just consumes ``result.note.id``.
    Pinned regression for the PR #46 reviewer finding."""
    cfg_path = _write_config(tmp_path)
    runner = CliRunner()
    client = _stub_client(
        note_write_return=WriteResult(
            status="created", note=_make_note(doc_id="real-canonical-id")
        ),
    )

    with patch("lithos_loom.cli.project.LithosClient", return_value=client):
        result = runner.invoke(
            project_app,
            ["create", "-c", str(cfg_path), "--title", "T", "--format", "json"],
        )

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["id"] == "real-canonical-id"
    # No re-fetch — note_write's stitched result.note is the source of truth.
    client.note_read.assert_not_called()


def test_create_raises_when_writeresult_note_unexpectedly_missing(
    tmp_path: Path,
) -> None:
    """Defensive: if ``WriteResult.note`` is None on a created/updated
    outcome (would mean Lithos changed response shape AND the
    note_write fix-up failed to detect it), surface explicitly rather
    than returning an empty id."""
    cfg_path = _write_config(tmp_path)
    runner = CliRunner()
    client = _stub_client(
        note_write_return=WriteResult(status="created", note=None),
    )

    with patch("lithos_loom.cli.project.LithosClient", return_value=client):
        result = runner.invoke(
            project_app, ["create", "-c", str(cfg_path), "--title", "T"]
        )

    assert result.exit_code == 1
    combined = result.stdout + (result.stderr if hasattr(result, "stderr") else "")
    assert "neither a 'document' field nor a top-level id" in combined


def test_create_treats_non_created_status_as_failure(tmp_path: Path) -> None:
    """A write that returns e.g. ``invalid_input`` is surfaced as
    a Lithos error (exit 1). We don't silently report success on
    drift statuses."""
    cfg_path = _write_config(tmp_path)
    runner = CliRunner()
    client = _stub_client(
        note_write_return=WriteResult(status="invalid_input", message="bad title")
    )

    with patch("lithos_loom.cli.project.LithosClient", return_value=client):
        result = runner.invoke(
            project_app, ["create", "-c", str(cfg_path), "--title", "T"]
        )

    assert result.exit_code == 1
