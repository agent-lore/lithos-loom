"""Tests for ``lithos-loom project import`` bulk-task-import behaviour.

Covers all 17 PRD verification scenarios for bulk import (D56–D75 +
the 8 execution-resolved decisions E1–E8). Uses Typer's CliRunner +
mocked LithosClient.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

from typer.testing import CliRunner

from lithos_loom.cli.project import project_app
from lithos_loom.errors import LithosClientError
from lithos_loom.lithos_client import Note, NoteSummary, Task, WriteResult


@contextmanager
def _patched_client(client_stub: Any) -> Iterator[None]:
    """Patch LithosClient in BOTH modules that import it.

    ``cli.project`` uses it for the greenfield doc-create path
    (``_create_project_async``); ``cli._project_import_bulk`` uses it
    for the tasks-only preflight, force-cleanup, and bulk task-create
    paths. A single ``patch`` would only catch one.
    """
    with (
        patch("lithos_loom.cli.project.LithosClient", return_value=client_stub),
        patch(
            "lithos_loom.cli._project_import_bulk.LithosClient",
            return_value=client_stub,
        ),
    ):
        yield


# ── Fixtures and stubs ─────────────────────────────────────────────────


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


def _canonical_note(doc_id: str = "new-doc", slug: str = "imported") -> Note:
    return Note(
        id=doc_id,
        title="x",
        body="x",
        version=1,
        updated_at=datetime(2026, 5, 25, tzinfo=UTC),
        tags=("project-context",),
        status="active",
        note_type="concept",
        path=f"projects/{slug}/{slug}-project-context.md",
        slug=slug,
    )


def _canonical_summary(slug: str, doc_id: str | None = None) -> NoteSummary:
    return NoteSummary(
        id=doc_id or f"id-{slug}",
        title=slug.replace("-", " ").title(),
        version=1,
        updated_at=datetime(2026, 5, 25, tzinfo=UTC),
        tags=("project-context",),
        status="active",
        note_type="concept",
        path=f"projects/{slug}/{slug}-project-context.md",
        slug=slug,
    )


def _open_task(task_id: str, project_slug: str, title: str = "stub") -> Task:
    return Task(
        id=task_id,
        title=title,
        status="open",
        tags=(),
        metadata={"project": project_slug},
        claims=(),
    )


def _resolved_task(
    task_id: str,
    project_slug: str,
    status: str = "completed",
    title: str = "done",
) -> Task:
    return Task(
        id=task_id,
        title=title,
        status=status,
        tags=(),
        metadata={"project": project_slug},
        claims=(),
    )


def _stub_client(
    *,
    existing_project_summaries: list[NoteSummary] | None = None,
    existing_open_tasks: list[Task] | None = None,
    existing_resolved_tasks: list[Task] | None = None,
    task_create_ids: list[str] | None = None,
    task_create_side_effect: BaseException | None = None,
    task_create_fail_after: int | None = None,
    note_for_lithos_id: Note | None = None,
    all_project_summaries: list[NoteSummary] | None = None,
) -> Any:
    """Build an AsyncMock LithosClient stub for bulk-import tests.

    Knobs cover every state the import touches:

    * ``existing_project_summaries`` — what ``note_list`` returns for
      ``projects/{slug}/`` (default: empty → greenfield can create;
      tasks-only would fail preflight).
    * ``existing_open_tasks`` — what ``task_list(status="open")``
      returns (default: empty).
    * ``task_create_ids`` — task ids to return for successive
      ``task_create`` calls (default: ``["task-1", "task-2", ...]``).
    * ``task_create_side_effect`` — exception to raise on EVERY
      ``task_create`` call (use for "Lithos completely down" scenarios).
    * ``task_create_fail_after`` — succeed for this many calls then
      raise ``LithosClientError`` (use for mid-batch failure scenarios).
    * ``note_for_lithos_id`` — what ``note_read(id=...)`` returns when
      verifying frontmatter ``lithos_id`` in tasks-only mode.
    * ``all_project_summaries`` — what ``note_list(path_prefix="projects/")``
      returns when generating typo hints (default: same as
      ``existing_project_summaries``).
    """
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.__aexit__.return_value = None

    project_summaries = existing_project_summaries or []
    all_summaries = (
        all_project_summaries
        if all_project_summaries is not None
        else project_summaries
    )

    async def note_list(
        *,
        path_prefix: str | None = None,
        tags: list[str] | None = None,
        limit: int = 100,
    ) -> list[NoteSummary]:
        if path_prefix == "projects/":
            return list(all_summaries)
        if path_prefix and path_prefix.startswith("projects/"):
            target_slug = path_prefix.removeprefix("projects/").rstrip("/")
            return [s for s in project_summaries if s.slug == target_slug]
        return list(project_summaries)

    client.note_list.side_effect = note_list
    client.note_write.return_value = WriteResult(
        status="created", note=_canonical_note(slug="imported")
    )
    client.note_read.return_value = note_for_lithos_id

    open_tasks = list(existing_open_tasks or [])
    resolved_tasks = list(existing_resolved_tasks or [])
    all_tasks = [*open_tasks, *resolved_tasks]

    async def task_list(*, status=None, with_claims=False, resolved_since=None):  # type: ignore[no-untyped-def]
        if status == "open":
            return list(open_tasks)
        # status=None → all tasks (per LithosClient.task_list contract)
        return list(all_tasks)

    client.task_list.side_effect = task_list

    create_ids = (
        list(task_create_ids)
        if task_create_ids
        else [f"task-{i}" for i in range(1, 100)]
    )
    create_count = {"n": 0}

    async def task_create(**kwargs: Any) -> str:
        if task_create_side_effect is not None:
            raise task_create_side_effect
        create_count["n"] += 1
        if (
            task_create_fail_after is not None
            and create_count["n"] > task_create_fail_after
        ):
            raise LithosClientError("network", "simulated mid-batch failure")
        if create_count["n"] - 1 >= len(create_ids):
            raise LithosClientError("setup", "ran out of stubbed task ids")
        return create_ids[create_count["n"] - 1]

    client.task_create.side_effect = task_create
    client.task_cancel.return_value = None
    client.finding_post.return_value = "finding-id"
    return client


# ── 1. Greenfield happy path ──────────────────────────────────────────


def test_greenfield_three_flat_tasks(tmp_path: Path) -> None:
    cfg_path = _write_config(tmp_path)
    source = tmp_path / "demo.md"
    source.write_text(
        "Intro\n\n- [ ] First\n- [ ] Second\n- [ ] Third\n\nOutro\n",
        encoding="utf-8",
    )
    client = _stub_client()
    runner = CliRunner()
    with _patched_client(client):
        result = runner.invoke(
            project_app, ["import", str(source), "-c", str(cfg_path)]
        )
    assert result.exit_code == 0, result.stdout
    # Doc body strips the three task lines, keeps Intro + Outro
    written_body = client.note_write.await_args.kwargs["content"]
    assert "First" not in written_body
    assert "Intro" in written_body
    assert "Outro" in written_body
    # Three task_create calls happened
    assert client.task_create.await_count == 3
    # Each task tagged with the project slug via metadata
    for call in client.task_create.await_args_list:
        assert call.kwargs["metadata"]["project"] == "demo"


# ── 2. Greenfield with metadata ───────────────────────────────────────


def test_greenfield_priority_and_tags_extracted(tmp_path: Path) -> None:
    cfg_path = _write_config(tmp_path)
    source = tmp_path / "demo.md"
    source.write_text(
        "- [ ] Important #foo ⏫\n- [ ] Normal\n",
        encoding="utf-8",
    )
    client = _stub_client()
    runner = CliRunner()
    with _patched_client(client):
        result = runner.invoke(
            project_app, ["import", str(source), "-c", str(cfg_path)]
        )
    assert result.exit_code == 0, result.stdout
    first_call = client.task_create.await_args_list[0].kwargs
    assert first_call["title"] == "Important"
    # User tag preserved + auto-added project routing tag (US88 / D61)
    assert "foo" in first_call["tags"]
    assert "project/demo" in first_call["tags"]
    assert first_call["metadata"]["priority"] == "high"
    assert first_call["metadata"]["project"] == "demo"
    # Second task has no priority but still gets the project tag
    second_call = client.task_create.await_args_list[1].kwargs
    assert second_call["title"] == "Normal"
    assert "priority" not in second_call["metadata"]
    assert "project/demo" in second_call["tags"]


# ── 3. Indented children, parallel default ────────────────────────────


def test_indented_children_parallel_default(tmp_path: Path) -> None:
    cfg_path = _write_config(tmp_path)
    source = tmp_path / "demo.md"
    source.write_text(
        "- [ ] Parent\n  - [ ] Child A\n  - [ ] Child B\n",
        encoding="utf-8",
    )
    client = _stub_client(task_create_ids=["task-A", "task-B", "task-parent"])
    runner = CliRunner()
    with _patched_client(client):
        result = runner.invoke(
            project_app, ["import", str(source), "-c", str(cfg_path)]
        )
    assert result.exit_code == 0, result.stdout
    # 3 task_create calls — children first per E4
    calls = client.task_create.await_args_list
    assert calls[0].kwargs["title"] == "Child A"
    assert calls[0].kwargs["metadata"]["parallelizable"] is True
    assert calls[1].kwargs["title"] == "Child B"
    assert calls[1].kwargs["metadata"]["parallelizable"] is True
    # Parent has depends_on referencing the freshly-created child ids
    assert calls[2].kwargs["title"] == "Parent"
    assert set(calls[2].kwargs["metadata"]["depends_on"]) == {"task-A", "task-B"}


# ── 4. Sequential marker override ─────────────────────────────────────


def test_sequential_marker_creates_chain(tmp_path: Path) -> None:
    cfg_path = _write_config(tmp_path)
    source = tmp_path / "demo.md"
    source.write_text(
        "- [ ] Build [sequential]\n  - [ ] Step 1\n  - [ ] Step 2\n",
        encoding="utf-8",
    )
    client = _stub_client(task_create_ids=["task-step1", "task-step2", "task-parent"])
    runner = CliRunner()
    with _patched_client(client):
        result = runner.invoke(
            project_app, ["import", str(source), "-c", str(cfg_path)]
        )
    assert result.exit_code == 0, result.stdout
    calls = client.task_create.await_args_list
    step1_call = next(c for c in calls if c.kwargs["title"] == "Step 1")
    step2_call = next(c for c in calls if c.kwargs["title"] == "Step 2")
    parent_call = next(c for c in calls if c.kwargs["title"] == "Build")
    # Step 1: no depends_on; NOT parallelizable
    assert "depends_on" not in step1_call.kwargs["metadata"]
    assert "parallelizable" not in step1_call.kwargs["metadata"]
    # Step 2: depends on Step 1; NOT parallelizable
    assert step2_call.kwargs["metadata"]["depends_on"] == ["task-step1"]
    # Parent depends on both children (D64 unchanged)
    assert set(parent_call.kwargs["metadata"]["depends_on"]) == {
        "task-step1",
        "task-step2",
    }


# ── 5. Tasks-only happy path ──────────────────────────────────────────


def test_tasks_only_against_existing_project(tmp_path: Path) -> None:
    cfg_path = _write_config(tmp_path)
    source = tmp_path / "more-tasks.md"
    source.write_text("- [ ] One\n- [ ] Two\n", encoding="utf-8")
    client = _stub_client(
        existing_project_summaries=[_canonical_summary("existing-proj")]
    )
    runner = CliRunner()
    with _patched_client(client):
        result = runner.invoke(
            project_app,
            [
                "import",
                str(source),
                "-c",
                str(cfg_path),
                "--tasks-only",
                "--slug",
                "existing-proj",
            ],
        )
    assert result.exit_code == 0, result.stdout
    # No doc write happened
    client.note_write.assert_not_called()
    # Two tasks created
    assert client.task_create.await_count == 2


# ── 6. Tasks-only requires --slug ─────────────────────────────────────


def test_tasks_only_without_slug_exit_2(tmp_path: Path) -> None:
    cfg_path = _write_config(tmp_path)
    source = tmp_path / "demo.md"
    source.write_text("- [ ] T\n", encoding="utf-8")
    runner = CliRunner()
    result = runner.invoke(
        project_app, ["import", str(source), "-c", str(cfg_path), "--tasks-only"]
    )
    assert result.exit_code == 2
    combined = result.stdout + (result.stderr if hasattr(result, "stderr") else "")
    assert "--tasks-only requires --slug" in combined


# ── 7. --no-tasks skips extraction ────────────────────────────────────


def test_no_tasks_skips_task_extraction(tmp_path: Path) -> None:
    cfg_path = _write_config(tmp_path)
    source = tmp_path / "demo.md"
    source.write_text(
        "Intro\n- [ ] Should not be extracted\n- [ ] Also not\nOutro\n",
        encoding="utf-8",
    )
    client = _stub_client()
    runner = CliRunner()
    with _patched_client(client):
        result = runner.invoke(
            project_app,
            ["import", str(source), "-c", str(cfg_path), "--no-tasks"],
        )
    assert result.exit_code == 0, result.stdout
    # No task_create calls
    client.task_create.assert_not_called()
    # Body kept verbatim, task lines NOT stripped
    written_body = client.note_write.await_args.kwargs["content"]
    assert "Should not be extracted" in written_body


# ── 8. Greenfield + existing slug refusal ─────────────────────────────


def test_greenfield_existing_slug_suggests_tasks_only(tmp_path: Path) -> None:
    cfg_path = _write_config(tmp_path)
    source = tmp_path / "demo.md"
    source.write_text("- [ ] T\n", encoding="utf-8")
    client = _stub_client(
        existing_project_summaries=[_canonical_summary("demo", doc_id="existing-id")]
    )
    runner = CliRunner()
    with _patched_client(client):
        result = runner.invoke(
            project_app, ["import", str(source), "-c", str(cfg_path)]
        )
    assert result.exit_code == 1
    combined = result.stdout + (result.stderr if hasattr(result, "stderr") else "")
    assert "already exists" in combined
    assert "--tasks-only --slug demo" in combined
    # No task_create either (failed before tasks phase)
    client.task_create.assert_not_called()


# ── 9. Tasks-only + missing slug + typo hint ──────────────────────────


def test_tasks_only_missing_slug_typo_hint(tmp_path: Path) -> None:
    cfg_path = _write_config(tmp_path)
    source = tmp_path / "demo.md"
    source.write_text("- [ ] T\n", encoding="utf-8")
    # Project does NOT exist; but a similar slug exists
    client = _stub_client(
        existing_project_summaries=[],
        all_project_summaries=[
            _canonical_summary("project-x"),
            _canonical_summary("unrelated"),
        ],
    )
    runner = CliRunner()
    with _patched_client(client):
        result = runner.invoke(
            project_app,
            [
                "import",
                str(source),
                "-c",
                str(cfg_path),
                "--tasks-only",
                "--slug",
                "projetc-x",  # typo
            ],
        )
    assert result.exit_code == 1
    combined = result.stdout + (result.stderr if hasattr(result, "stderr") else "")
    assert "did you mean" in combined
    assert "project-x" in combined


# ── 10. lithos_id / --slug mismatch ───────────────────────────────────


def test_tasks_only_lithos_id_slug_mismatch(tmp_path: Path) -> None:
    cfg_path = _write_config(tmp_path)
    source = tmp_path / "demo.md"
    source.write_text("---\nlithos_id: foreign-doc\n---\n- [ ] T\n", encoding="utf-8")
    foreign_note = Note(
        id="foreign-doc",
        title="Foreign",
        body="b",
        version=1,
        updated_at=datetime(2026, 5, 25, tzinfo=UTC),
        tags=(),
        status="active",
        note_type="concept",
        path="projects/foreign-slug/foreign-slug-project-context.md",
        slug="foreign-slug",
    )
    client = _stub_client(
        existing_project_summaries=[_canonical_summary("target-slug")],
        note_for_lithos_id=foreign_note,
    )
    runner = CliRunner()
    with _patched_client(client):
        result = runner.invoke(
            project_app,
            [
                "import",
                str(source),
                "-c",
                str(cfg_path),
                "--tasks-only",
                "--slug",
                "target-slug",
            ],
        )
    assert result.exit_code == 2
    combined = result.stdout + (result.stderr if hasattr(result, "stderr") else "")
    assert "foreign-slug" in combined
    assert "target-slug" in combined


# ── 11. Cross-project tag refusal ─────────────────────────────────────


def test_cross_project_tag_aborts_with_lines(tmp_path: Path) -> None:
    cfg_path = _write_config(tmp_path)
    source = tmp_path / "demo.md"
    source.write_text(
        "- [ ] Good task\n- [ ] Bad task #project/other-slug\n",
        encoding="utf-8",
    )
    client = _stub_client()
    runner = CliRunner()
    with _patched_client(client):
        result = runner.invoke(
            project_app, ["import", str(source), "-c", str(cfg_path)]
        )
    assert result.exit_code == 2
    combined = result.stdout + (result.stderr if hasattr(result, "stderr") else "")
    assert "line 2" in combined
    assert "other-slug" in combined
    # No Lithos writes
    client.note_write.assert_not_called()
    client.task_create.assert_not_called()


# ── 12. Empty parent refusal ──────────────────────────────────────────


def test_empty_parent_aborts(tmp_path: Path) -> None:
    cfg_path = _write_config(tmp_path)
    source = tmp_path / "demo.md"
    source.write_text("- [ ]\n  - [ ] Real child\n", encoding="utf-8")
    client = _stub_client()
    runner = CliRunner()
    with _patched_client(client):
        result = runner.invoke(
            project_app, ["import", str(source), "-c", str(cfg_path)]
        )
    assert result.exit_code == 2
    combined = result.stdout + (result.stderr if hasattr(result, "stderr") else "")
    assert "line 1" in combined
    assert "empty" in combined.lower()


# ── 13. Validation aggregates all errors ──────────────────────────────


def test_validation_aggregates_multiple_errors(tmp_path: Path) -> None:
    cfg_path = _write_config(tmp_path)
    source = tmp_path / "demo.md"
    source.write_text(
        "- [ ] Cross #project/other-a\n- [ ]\n  - [ ] Real child\n",
        encoding="utf-8",
    )
    client = _stub_client()
    runner = CliRunner()
    with _patched_client(client):
        result = runner.invoke(
            project_app, ["import", str(source), "-c", str(cfg_path)]
        )
    assert result.exit_code == 2
    combined = result.stdout + (result.stderr if hasattr(result, "stderr") else "")
    # Both errors reported in one pass
    assert "other-a" in combined  # cross-project on line 1
    assert "line 1" in combined
    assert "line 2" in combined  # empty parent on line 2
    # No Lithos writes
    client.note_write.assert_not_called()


# ── 14. --dry-run preview ─────────────────────────────────────────────


def test_dry_run_shows_no_changes_made_at_start_and_end(tmp_path: Path) -> None:
    cfg_path = _write_config(tmp_path)
    source = tmp_path / "demo.md"
    source.write_text(
        "- [ ] One #foo ⏫\n- [ ] Two\n  - [ ] Sub\n",
        encoding="utf-8",
    )
    client = _stub_client()
    runner = CliRunner()
    with _patched_client(client):
        result = runner.invoke(
            project_app, ["import", str(source), "-c", str(cfg_path), "--dry-run"]
        )
    assert result.exit_code == 0, result.stdout
    # NO CHANGES MADE appears at both start and end
    lines = result.stdout.splitlines()
    assert "NO CHANGES MADE" in lines[0]
    assert "NO CHANGES MADE" in lines[-1]
    # No Lithos writes happened
    client.note_write.assert_not_called()
    client.task_create.assert_not_called()


def test_dry_run_includes_task_metadata(tmp_path: Path) -> None:
    cfg_path = _write_config(tmp_path)
    source = tmp_path / "demo.md"
    source.write_text("- [ ] One ⏫ #foo\n", encoding="utf-8")
    client = _stub_client()
    runner = CliRunner()
    with _patched_client(client):
        result = runner.invoke(
            project_app, ["import", str(source), "-c", str(cfg_path), "--dry-run"]
        )
    assert result.exit_code == 0
    assert "priority=high" in result.stdout
    assert "#foo" in result.stdout


def test_dry_run_shows_auto_added_project_tag(tmp_path: Path) -> None:
    """--dry-run must preview the `#project/<slug>` tag that create_tasks adds.

    Without this, the operator sees a plan that doesn't match what gets
    written (US88 — auto-add — is invisible in the preview). Per PRD
    line 208 the preview must reflect the full plan.
    """
    cfg_path = _write_config(tmp_path)
    source = tmp_path / "alpha.md"
    source.write_text("- [ ] Plain task with no tags\n", encoding="utf-8")
    client = _stub_client()
    runner = CliRunner()
    with _patched_client(client):
        result = runner.invoke(
            project_app, ["import", str(source), "-c", str(cfg_path), "--dry-run"]
        )
    assert result.exit_code == 0
    # The project routing tag appears in the preview even though the
    # source line had no `#project/...` reference.
    assert "#project/alpha" in result.stdout


def test_dry_run_does_not_duplicate_explicit_project_tag(tmp_path: Path) -> None:
    """When source carries `#project/<slug>` explicitly, preview shows it ONCE."""
    cfg_path = _write_config(tmp_path)
    source = tmp_path / "alpha.md"
    source.write_text("- [ ] Task #project/alpha #extra\n", encoding="utf-8")
    client = _stub_client()
    runner = CliRunner()
    with _patched_client(client):
        result = runner.invoke(
            project_app, ["import", str(source), "-c", str(cfg_path), "--dry-run"]
        )
    assert result.exit_code == 0
    assert result.stdout.count("#project/alpha") == 1
    assert "#extra" in result.stdout


# ── 15. --force-tasks interactive prompt ──────────────────────────────


def test_force_tasks_prompt_n_aborts(tmp_path: Path) -> None:
    cfg_path = _write_config(tmp_path)
    source = tmp_path / "demo.md"
    source.write_text("- [ ] T\n", encoding="utf-8")
    client = _stub_client(
        existing_project_summaries=[_canonical_summary("existing")],
        existing_open_tasks=[_open_task("old-1", "existing")],
    )
    runner = CliRunner()
    with _patched_client(client):
        result = runner.invoke(
            project_app,
            [
                "import",
                str(source),
                "-c",
                str(cfg_path),
                "--tasks-only",
                "--slug",
                "existing",
                "--force-tasks",
            ],
            input="n\n",
        )
    assert result.exit_code == 0  # clean abort
    combined = result.stdout + (result.stderr if hasattr(result, "stderr") else "")
    assert "aborted" in combined.lower()
    # No mutations
    client.task_cancel.assert_not_called()
    client.task_create.assert_not_called()


def test_force_tasks_prompt_y_deletes_and_creates(tmp_path: Path) -> None:
    cfg_path = _write_config(tmp_path)
    source = tmp_path / "demo.md"
    source.write_text("- [ ] new1\n- [ ] new2\n", encoding="utf-8")
    client = _stub_client(
        existing_project_summaries=[_canonical_summary("existing")],
        existing_open_tasks=[
            _open_task("old-1", "existing"),
            _open_task("old-2", "existing"),
        ],
    )
    runner = CliRunner()
    with _patched_client(client):
        result = runner.invoke(
            project_app,
            [
                "import",
                str(source),
                "-c",
                str(cfg_path),
                "--tasks-only",
                "--slug",
                "existing",
                "--force-tasks",
            ],
            input="y\n",
        )
    assert result.exit_code == 0, result.stdout
    assert client.task_cancel.await_count == 2
    assert client.task_create.await_count == 2


# ── 16. --force-tasks --yes bypass ────────────────────────────────────


def test_force_tasks_yes_bypasses_prompt(tmp_path: Path) -> None:
    cfg_path = _write_config(tmp_path)
    source = tmp_path / "demo.md"
    source.write_text("- [ ] new\n", encoding="utf-8")
    client = _stub_client(
        existing_project_summaries=[_canonical_summary("existing")],
        existing_open_tasks=[_open_task("old", "existing")],
    )
    runner = CliRunner()
    # No input= provided; --yes must bypass the prompt
    with _patched_client(client):
        result = runner.invoke(
            project_app,
            [
                "import",
                str(source),
                "-c",
                str(cfg_path),
                "--tasks-only",
                "--slug",
                "existing",
                "--force-tasks",
                "--yes",
            ],
        )
    assert result.exit_code == 0, result.stdout
    assert client.task_cancel.await_count == 1
    assert client.task_create.await_count == 1


# ── 17a. Mid-batch failure with finding posted (>0 created) ──────────


def test_mid_batch_failure_posts_finding(tmp_path: Path) -> None:
    cfg_path = _write_config(tmp_path)
    source = tmp_path / "big.md"
    source.write_text(
        "\n".join(f"- [ ] Task {i}" for i in range(1, 11)), encoding="utf-8"
    )
    client = _stub_client(task_create_fail_after=4)
    runner = CliRunner()
    with _patched_client(client):
        result = runner.invoke(
            project_app, ["import", str(source), "-c", str(cfg_path)]
        )
    assert result.exit_code == 1
    combined = result.stdout + (result.stderr if hasattr(result, "stderr") else "")
    assert "4/10" in combined
    assert "--force-tasks" in combined
    # Finding posted against the first successfully-created task
    client.finding_post.assert_awaited()
    finding_kwargs = client.finding_post.await_args.kwargs
    assert finding_kwargs["task_id"] == "task-1"
    assert "bulk-import partial-failure" in finding_kwargs["summary"]
    assert "--force-tasks" in finding_kwargs["summary"]


# ── 17b. Mid-batch failure with zero created (no finding to attach) ──


def test_mid_batch_failure_zero_tasks_logs_warning_only(tmp_path: Path) -> None:
    cfg_path = _write_config(tmp_path)
    source = tmp_path / "big.md"
    source.write_text("- [ ] First task\n- [ ] Second\n", encoding="utf-8")
    client = _stub_client(
        task_create_side_effect=LithosClientError("network", "down at first call")
    )
    runner = CliRunner()
    with _patched_client(client):
        result = runner.invoke(
            project_app, ["import", str(source), "-c", str(cfg_path)]
        )
    assert result.exit_code == 1
    # NO finding posted (no task to attach to)
    client.finding_post.assert_not_called()


# ── 18. D75 prefix-strip ──────────────────────────────────────────────


def test_d75_strip_project_prefix_from_stem(tmp_path: Path) -> None:
    cfg_path = _write_config(tmp_path)
    source = tmp_path / "project-organising-myself.md"
    source.write_text("- [ ] T\n", encoding="utf-8")
    client = _stub_client()
    runner = CliRunner()
    with _patched_client(client):
        result = runner.invoke(
            project_app, ["import", str(source), "-c", str(cfg_path)]
        )
    assert result.exit_code == 0, result.stdout
    # Resolved slug is "organising-myself" — prefix stripped
    written_path = client.note_write.await_args.kwargs["path"]
    assert written_path == (
        "projects/organising-myself/organising-myself-project-context.md"
    )


def test_d75_not_stripped_from_frontmatter_title(tmp_path: Path) -> None:
    """Frontmatter title is explicit operator intent — NOT prefix-stripped."""
    cfg_path = _write_config(tmp_path)
    source = tmp_path / "irrelevant-name.md"
    source.write_text("---\ntitle: Project Foo\n---\n- [ ] T\n", encoding="utf-8")
    client = _stub_client()
    runner = CliRunner()
    with _patched_client(client):
        result = runner.invoke(
            project_app, ["import", str(source), "-c", str(cfg_path)]
        )
    assert result.exit_code == 0, result.stdout
    # Slug derived from "Project Foo" → "project-foo" (NOT "foo")
    written_path = client.note_write.await_args.kwargs["path"]
    assert "project-foo" in written_path


def test_d75_explicit_slug_overrides_strip(tmp_path: Path) -> None:
    cfg_path = _write_config(tmp_path)
    source = tmp_path / "project-foo.md"
    source.write_text("- [ ] T\n", encoding="utf-8")
    client = _stub_client()
    runner = CliRunner()
    with _patched_client(client):
        result = runner.invoke(
            project_app,
            ["import", str(source), "-c", str(cfg_path), "--slug", "project-foo"],
        )
    assert result.exit_code == 0, result.stdout
    written_path = client.note_write.await_args.kwargs["path"]
    assert "projects/project-foo/" in written_path


# ── 19. Mutual-exclusion: --no-tasks + --tasks-only ──────────────────


def test_no_tasks_plus_tasks_only_exit_2(tmp_path: Path) -> None:
    cfg_path = _write_config(tmp_path)
    source = tmp_path / "demo.md"
    source.write_text("- [ ] T\n", encoding="utf-8")
    runner = CliRunner()
    result = runner.invoke(
        project_app,
        [
            "import",
            str(source),
            "-c",
            str(cfg_path),
            "--no-tasks",
            "--tasks-only",
            "--slug",
            "x",
        ],
    )
    assert result.exit_code == 2
    combined = result.stdout + (result.stderr if hasattr(result, "stderr") else "")
    assert "mutually exclusive" in combined


# ── 20. Mutual-exclusion: --no-tasks + --force-tasks ─────────────────


def test_no_tasks_plus_force_tasks_exit_2(tmp_path: Path) -> None:
    cfg_path = _write_config(tmp_path)
    source = tmp_path / "demo.md"
    source.write_text("- [ ] T\n", encoding="utf-8")
    runner = CliRunner()
    result = runner.invoke(
        project_app,
        [
            "import",
            str(source),
            "-c",
            str(cfg_path),
            "--no-tasks",
            "--force-tasks",
        ],
    )
    assert result.exit_code == 2
    combined = result.stdout + (result.stderr if hasattr(result, "stderr") else "")
    assert "mutually exclusive" in combined


# ── JSON output extended with tasks_created ──────────────────────────


def test_json_output_includes_tasks_created(tmp_path: Path) -> None:
    cfg_path = _write_config(tmp_path)
    source = tmp_path / "demo.md"
    source.write_text("- [ ] One\n- [ ] Two\n", encoding="utf-8")
    client = _stub_client()
    runner = CliRunner()
    with _patched_client(client):
        result = runner.invoke(
            project_app,
            ["import", str(source), "-c", str(cfg_path), "--format", "json"],
        )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["tasks_created"] == 2
    assert payload["id"] == "new-doc"


# ── --dry-run for tasks-only with existing project ───────────────────


def test_dry_run_tasks_only_existing_project(tmp_path: Path) -> None:
    cfg_path = _write_config(tmp_path)
    source = tmp_path / "demo.md"
    source.write_text("- [ ] T\n", encoding="utf-8")
    client = _stub_client(existing_project_summaries=[_canonical_summary("existing")])
    runner = CliRunner()
    with _patched_client(client):
        result = runner.invoke(
            project_app,
            [
                "import",
                str(source),
                "-c",
                str(cfg_path),
                "--tasks-only",
                "--slug",
                "existing",
                "--dry-run",
            ],
        )
    assert result.exit_code == 0, result.stdout
    assert "existing" in result.stdout
    assert "doc unchanged" in result.stdout
    client.task_create.assert_not_called()
    client.note_write.assert_not_called()


# ── --dry-run rejects tasks-only against missing project ─────────────


def test_dry_run_tasks_only_missing_project_exit_1(tmp_path: Path) -> None:
    cfg_path = _write_config(tmp_path)
    source = tmp_path / "demo.md"
    source.write_text("- [ ] T\n", encoding="utf-8")
    client = _stub_client(existing_project_summaries=[])
    runner = CliRunner()
    with _patched_client(client):
        result = runner.invoke(
            project_app,
            [
                "import",
                str(source),
                "-c",
                str(cfg_path),
                "--tasks-only",
                "--slug",
                "nonexistent",
                "--dry-run",
            ],
        )
    assert result.exit_code == 1
    client.task_create.assert_not_called()


# ── Code block / blockquote exclusion ────────────────────────────────


def test_tasks_in_code_blocks_not_extracted(tmp_path: Path) -> None:
    cfg_path = _write_config(tmp_path)
    source = tmp_path / "demo.md"
    source.write_text(
        "```\n- [ ] Example in code\n```\n- [ ] Real one\n",
        encoding="utf-8",
    )
    client = _stub_client()
    runner = CliRunner()
    with _patched_client(client):
        result = runner.invoke(
            project_app, ["import", str(source), "-c", str(cfg_path)]
        )
    assert result.exit_code == 0, result.stdout
    assert client.task_create.await_count == 1
    assert client.task_create.await_args.kwargs["title"] == "Real one"
    # Example task stays verbatim in the body
    assert "Example in code" in client.note_write.await_args.kwargs["content"]


# ── No body tasks: doc-only import still works ───────────────────────


def test_doc_only_import_no_tasks_in_body(tmp_path: Path) -> None:
    cfg_path = _write_config(tmp_path)
    source = tmp_path / "demo.md"
    source.write_text("Just a description, no tasks.\n", encoding="utf-8")
    client = _stub_client()
    runner = CliRunner()
    with _patched_client(client):
        result = runner.invoke(
            project_app, ["import", str(source), "-c", str(cfg_path)]
        )
    assert result.exit_code == 0, result.stdout
    client.task_create.assert_not_called()
    assert client.note_write.await_count == 1


# ── Existing tasks blocks tasks-only without --force-tasks ────────────


def test_tasks_only_existing_tasks_blocks_without_force(tmp_path: Path) -> None:
    cfg_path = _write_config(tmp_path)
    source = tmp_path / "demo.md"
    source.write_text("- [ ] new\n", encoding="utf-8")
    client = _stub_client(
        existing_project_summaries=[_canonical_summary("existing")],
        existing_open_tasks=[_open_task("old", "existing")],
    )
    runner = CliRunner()
    with _patched_client(client):
        result = runner.invoke(
            project_app,
            [
                "import",
                str(source),
                "-c",
                str(cfg_path),
                "--tasks-only",
                "--slug",
                "existing",
            ],
        )
    assert result.exit_code == 1
    combined = result.stdout + (result.stderr if hasattr(result, "stderr") else "")
    assert "1 existing task" in combined
    assert "1 open" in combined
    assert "--force-tasks" in combined
    client.task_cancel.assert_not_called()
    client.task_create.assert_not_called()


# ── --no-tasks plus body-with-tasks: tasks stay in body ──────────────


def test_no_tasks_preserves_tasks_in_body(tmp_path: Path) -> None:
    """--no-tasks doesn't strip; tasks stay as text in the projected doc."""
    cfg_path = _write_config(tmp_path)
    source = tmp_path / "demo.md"
    source.write_text(
        "Description\n- [ ] inline\n- [ ] another\nMore desc\n",
        encoding="utf-8",
    )
    client = _stub_client()
    runner = CliRunner()
    with _patched_client(client):
        result = runner.invoke(
            project_app,
            ["import", str(source), "-c", str(cfg_path), "--no-tasks"],
        )
    assert result.exit_code == 0, result.stdout
    body = client.note_write.await_args.kwargs["content"]
    assert "inline" in body
    assert "another" in body
    client.task_create.assert_not_called()


# ── PR #51 review fix: US88 / D61 — auto-add #project/<slug> tag ──────


def test_imported_task_always_carries_project_tag(tmp_path: Path) -> None:
    """Every imported task gets `project/<slug>` in its tags list (US88).

    The auto-add is required even when the source line has no tags and
    no `#project/<slug>` reference at all — the projection layer uses
    metadata.project, but Lithos-side task_list tag filters and other
    Lithos consumers look at the literal tag.
    """
    cfg_path = _write_config(tmp_path)
    source = tmp_path / "alpha.md"
    source.write_text("- [ ] First\n- [ ] Second\n", encoding="utf-8")
    client = _stub_client()
    runner = CliRunner()
    with _patched_client(client):
        result = runner.invoke(
            project_app, ["import", str(source), "-c", str(cfg_path)]
        )
    assert result.exit_code == 0, result.stdout
    for call in client.task_create.await_args_list:
        assert "project/alpha" in call.kwargs["tags"], (
            f"task {call.kwargs['title']!r} missing project routing tag"
        )


def test_self_project_tag_in_source_not_duplicated(tmp_path: Path) -> None:
    """When source already carries `#project/<slug>`, no duplicate tag."""
    cfg_path = _write_config(tmp_path)
    source = tmp_path / "alpha.md"
    source.write_text("- [ ] Task #project/alpha #extra\n", encoding="utf-8")
    client = _stub_client()
    runner = CliRunner()
    with _patched_client(client):
        result = runner.invoke(
            project_app, ["import", str(source), "-c", str(cfg_path)]
        )
    assert result.exit_code == 0, result.stdout
    tags = client.task_create.await_args.kwargs["tags"]
    # Exactly one `project/alpha`, plus the user's `extra` tag
    assert tags.count("project/alpha") == 1
    assert "extra" in tags


# ── PR #51 review fix: D60 — preflight counts ALL tasks not just open ─


def test_tasks_only_refused_when_only_resolved_tasks_exist(tmp_path: Path) -> None:
    """Project with only completed/cancelled tasks still triggers the refusal.

    Per D60, "tasks for project exist" means ANY tasks — including
    history. Without --force-tasks, the operator should be required
    to acknowledge they want to add to a project that has a track
    record.
    """
    cfg_path = _write_config(tmp_path)
    source = tmp_path / "new.md"
    source.write_text("- [ ] new\n", encoding="utf-8")
    client = _stub_client(
        existing_project_summaries=[_canonical_summary("existing")],
        existing_open_tasks=[],
        existing_resolved_tasks=[
            _resolved_task("old-1", "existing", status="completed"),
            _resolved_task("old-2", "existing", status="cancelled"),
        ],
    )
    runner = CliRunner()
    with _patched_client(client):
        result = runner.invoke(
            project_app,
            [
                "import",
                str(source),
                "-c",
                str(cfg_path),
                "--tasks-only",
                "--slug",
                "existing",
            ],
        )
    assert result.exit_code == 1
    combined = result.stdout + (result.stderr if hasattr(result, "stderr") else "")
    assert "2 existing task" in combined
    assert "0 open" in combined
    assert "2 resolved (history)" in combined
    assert "--force-tasks" in combined
    client.task_cancel.assert_not_called()
    client.task_create.assert_not_called()


def test_force_tasks_skips_already_resolved_tasks(tmp_path: Path) -> None:
    """--force-tasks cancels only open tasks (E5: no hard-delete in Lithos).

    Completed/cancelled tasks remain as history; the new import
    creates a fresh open set alongside them. This is the practical
    interpretation of D60 given Lithos's E5 cancel-only constraint.
    """
    cfg_path = _write_config(tmp_path)
    source = tmp_path / "demo.md"
    source.write_text("- [ ] new\n", encoding="utf-8")
    client = _stub_client(
        existing_project_summaries=[_canonical_summary("existing")],
        existing_open_tasks=[_open_task("open-1", "existing")],
        existing_resolved_tasks=[
            _resolved_task("done-1", "existing"),
            _resolved_task("done-2", "existing", status="cancelled"),
        ],
    )
    runner = CliRunner()
    with _patched_client(client):
        result = runner.invoke(
            project_app,
            [
                "import",
                str(source),
                "-c",
                str(cfg_path),
                "--tasks-only",
                "--slug",
                "existing",
                "--force-tasks",
                "--yes",
            ],
        )
    assert result.exit_code == 0, result.stdout
    # Only the open task was cancelled — the two resolved tasks were skipped
    assert client.task_cancel.await_count == 1
    cancelled_ids = [
        call.kwargs["task_id"] for call in client.task_cancel.await_args_list
    ]
    assert cancelled_ids == ["open-1"]
    assert "done-1" not in cancelled_ids
    assert "done-2" not in cancelled_ids


def test_force_tasks_prompt_shows_history_count(tmp_path: Path) -> None:
    """Prompt distinguishes "will-be-cancelled" from "preserved as history"."""
    cfg_path = _write_config(tmp_path)
    source = tmp_path / "demo.md"
    source.write_text("- [ ] new\n", encoding="utf-8")
    client = _stub_client(
        existing_project_summaries=[_canonical_summary("existing")],
        existing_open_tasks=[
            _open_task("open-1", "existing"),
            _open_task("open-2", "existing"),
        ],
        existing_resolved_tasks=[_resolved_task("done-1", "existing")],
    )
    runner = CliRunner()
    with _patched_client(client):
        # Decline the prompt to keep things isolated; we just want to
        # see the prompt text.
        result = runner.invoke(
            project_app,
            [
                "import",
                str(source),
                "-c",
                str(cfg_path),
                "--tasks-only",
                "--slug",
                "existing",
                "--force-tasks",
            ],
            input="n\n",
        )
    assert result.exit_code == 0
    combined = result.stdout + (result.stderr if hasattr(result, "stderr") else "")
    # Prompt mentions both counts
    assert "2 open task" in combined
    assert "1 resolved task" in combined
    assert "will remain as history" in combined
    client.task_cancel.assert_not_called()
