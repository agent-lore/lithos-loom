"""Tests for ``lithos-loom task create`` (Slice 3 US24+).

The CLI calls ``LithosClient.task_create`` via an async context
manager. We patch ``LithosClient`` in :mod:`lithos_loom.cli.task`
with the shared :class:`tests.support.FakeLithosClient` — an in-memory,
async-context-manager drop-in that records every call — so we can
assert on the wire-shape that hits Lithos without a live server.

The renderer's correctness is covered by ``tests/test_render.py``;
these tests verify CLI plumbing: argv → LithosClient args, line
formatting via the shared renderer, target-file write semantics,
input validation, and exit codes.
"""

from __future__ import annotations

import json
from pathlib import Path
from textwrap import dedent
from typing import Any

import pytest
from typer.testing import CliRunner

from lithos_loom.cli import task as cli_task_mod
from lithos_loom.lithos_client import Note
from lithos_loom.main import app
from tests.support import FakeLithosClient, make_note

runner = CliRunner()


# ── Helpers ────────────────────────────────────────────────────────────


def _install_fake(
    monkeypatch: pytest.MonkeyPatch,
    *,
    notes: tuple[Note, ...] = (),
) -> FakeLithosClient:
    """Build a fresh shared :class:`FakeLithosClient` and patch it in as
    ``cli_task_mod.LithosClient``.

    Production does ``async with LithosClient(url, agent_id=...) as client``.
    The factory yields the pre-seeded fake and reflects the ``agent_id`` the
    CLI wires into the constructor onto it: production passes ``agent_id`` to
    the *client* (the real client injects it into per-call args at the RPC
    layer), not to ``task_create`` — so the injected-agent assertion is
    checked on ``fake.agent_id`` rather than the un-injected per-call arg.
    """
    fake = FakeLithosClient(notes=notes)

    def _factory(*args: Any, **kwargs: Any) -> FakeLithosClient:
        if "agent_id" in kwargs:
            fake.agent_id = kwargs["agent_id"]
        return fake

    monkeypatch.setattr(cli_task_mod, "LithosClient", _factory)
    return fake


def _write_config(
    tmp_path: Path, *, projects: tuple[str, ...] = ("lithos-loom",)
) -> Path:
    """Write a config with the given project slugs."""
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    project_blocks = "\n".join(
        f'[projects.{slug}]\nrepo = "{repo}"\n' for slug in projects
    )
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        dedent(
            f"""
            [orchestrator]
            agent_id = "lithos-orchestrator-test"
            lithos_url = "http://localhost:8765"

            {project_blocks}
            """
        )
    )
    return config_path


def _project_note(slug: str) -> Note:
    """A Lithos project-context doc as ``note_list`` would surface it.

    Seeded into the fake so its ``path_prefix="projects/"`` + ``project-context``
    tag filter matches (unlike the old hand-rolled stub, the shared fake really
    filters ``note_list``), and its ``slug`` becomes a known project."""
    return make_note(
        f"doc-{slug}",
        title=slug,
        tags=("project-context",),
        note_type="project_context",
        path=f"projects/{slug}/{slug}-project-context.md",
        slug=slug,
    )


# ── Happy path ─────────────────────────────────────────────────────────


def test_task_create_minimum_args_prints_projected_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--project`` + ``--title`` → calls task_create with title +
    metadata.project, prints the projected line via the shared
    renderer."""
    config_path = _write_config(tmp_path)
    fake = _install_fake(monkeypatch)

    result = runner.invoke(
        app,
        [
            "task",
            "create",
            "--project",
            "lithos-loom",
            "--title",
            "Review PR",
            "--config",
            str(config_path),
        ],
    )
    assert result.exit_code == 0, result.stdout

    # LithosClient.task_create was called with the right shape.
    calls = fake.calls_to("task_create")
    assert len(calls) == 1
    call = calls[0]
    assert call["title"] == "Review PR"
    # The CLI wires cfg.orchestrator.agent_id into the LithosClient
    # constructor; the real client injects it into per-call args at the RPC
    # layer, which the shared fake doesn't model on the recorded call — so
    # assert the wired constructor agent_id, not the un-injected per-call None.
    assert fake.agent_id == "lithos-orchestrator-test"
    assert call["description"] is None
    assert call["tags"] is None
    assert call["metadata"] == {"project": "lithos-loom"}

    # Output is the projected line (the fake mints task ids as ``task-N``).
    assert result.stdout.strip() == (
        "- [ ] Review PR 🆔 lithos:task-1 #project/lithos-loom"
    )


def test_task_create_full_form_passes_all_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """All optional flags → forwarded to task_create + reflected in
    the rendered line."""
    config_path = _write_config(tmp_path)
    fake = _install_fake(monkeypatch)

    result = runner.invoke(
        app,
        [
            "task",
            "create",
            "--project",
            "lithos-loom",
            "--title",
            "Full form",
            "--brief",
            "Some context",
            "--scheduled",
            "2026-06-01",
            "--priority",
            "high",
            "--tags",
            "code-review, urgent",
            "--config",
            str(config_path),
        ],
    )
    assert result.exit_code == 0, result.stdout

    call = fake.calls_to("task_create")[0]
    assert call["description"] == "Some context"
    assert call["tags"] == ["code-review", "urgent"]
    assert call["metadata"] == {
        "project": "lithos-loom",
        "priority": "high",
        "scheduled_for": "2026-06-01",
    }

    # Line carries priority emoji + scheduled date + project tag (the fake
    # mints task ids as ``task-N`` → ``task-1`` for this single create).
    line = result.stdout.strip()
    assert "⏫" in line
    assert "🆔 lithos:task-1" in line
    assert "📅 2026-06-01" in line
    assert "#project/lithos-loom" in line


def test_task_create_target_file_appends_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--target-file PATH`` writes the line to the file and prints
    nothing to stdout (US27 composable-with-daily-notes flow)."""
    config_path = _write_config(tmp_path)
    _install_fake(monkeypatch)
    target = tmp_path / "daily" / "2026-05-22.md"

    result = runner.invoke(
        app,
        [
            "task",
            "create",
            "--project",
            "lithos-loom",
            "--title",
            "Captured",
            "--target-file",
            str(target),
            "--config",
            str(config_path),
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert result.stdout.strip() == ""

    assert target.exists()
    content = target.read_text(encoding="utf-8")
    assert content.endswith("\n")
    assert "- [ ] Captured 🆔 lithos:task-1 #project/lithos-loom" in content


def test_task_create_no_insert_prints_task_id_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--no-insert`` (US27) prints just the task_id and discards the
    projected line. Scripted callers use this to capture the id from
    stdout without dealing with the line."""
    config_path = _write_config(tmp_path)
    fake = _install_fake(monkeypatch)

    result = runner.invoke(
        app,
        [
            "task",
            "create",
            "--project",
            "lithos-loom",
            "--title",
            "Scripted create",
            "--no-insert",
            "--config",
            str(config_path),
        ],
    )
    assert result.exit_code == 0, result.stdout

    # Task was still created upstream.
    assert len(fake.calls_to("task_create")) == 1

    # Stdout is just the minted task_id — no projected-line markers.
    assert result.stdout.strip() == "task-1"
    assert "🆔" not in result.stdout
    assert "#project/" not in result.stdout


def test_task_create_no_insert_mutually_exclusive_with_target_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Passing both ``--no-insert`` and ``--target-file`` is a usage
    error (exit 2); the CLI rejects before reaching Lithos so the
    operator's intent is unambiguous."""
    config_path = _write_config(tmp_path)
    fake = _install_fake(monkeypatch)
    result = runner.invoke(
        app,
        [
            "task",
            "create",
            "--project",
            "lithos-loom",
            "--title",
            "x",
            "--no-insert",
            "--target-file",
            str(tmp_path / "out.md"),
            "--config",
            str(config_path),
        ],
    )
    assert result.exit_code == 2
    assert "mutually exclusive" in result.stderr
    assert fake.calls_to("task_create") == []


def test_task_create_target_file_appends_to_existing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Existing target file is appended to, not overwritten."""
    config_path = _write_config(tmp_path)
    _install_fake(monkeypatch)
    target = tmp_path / "inbox.md"
    target.write_text("existing line\n", encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "task",
            "create",
            "--project",
            "lithos-loom",
            "--title",
            "Appended",
            "--target-file",
            str(target),
            "--config",
            str(config_path),
        ],
    )
    assert result.exit_code == 0
    content = target.read_text(encoding="utf-8")
    assert content.startswith("existing line\n")
    assert "Appended" in content


def test_task_create_strips_whitespace_from_tags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``"a, , b"`` → ``["a", "b"]``. Empty entries are dropped so
    operators don't accidentally tag tasks with empty strings."""
    config_path = _write_config(tmp_path)
    fake = _install_fake(monkeypatch)

    result = runner.invoke(
        app,
        [
            "task",
            "create",
            "--project",
            "lithos-loom",
            "--title",
            "Tag whitespace",
            "--tags",
            "  alpha , , beta ",
            "--config",
            str(config_path),
        ],
    )
    assert result.exit_code == 0
    call = fake.calls_to("task_create")[0]
    assert call["tags"] == ["alpha", "beta"]


@pytest.mark.parametrize("enum_value", ["highest", "high", "medium", "low", "lowest"])
def test_task_create_all_priority_values_pass_through(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    enum_value: str,
) -> None:
    """Every D18 enum value is accepted and forwarded verbatim."""
    config_path = _write_config(tmp_path)
    fake = _install_fake(monkeypatch)

    result = runner.invoke(
        app,
        [
            "task",
            "create",
            "--project",
            "lithos-loom",
            "--title",
            "Priority probe",
            "--priority",
            enum_value,
            "--config",
            str(config_path),
        ],
    )
    assert result.exit_code == 0
    call = fake.calls_to("task_create")[0]
    assert call["metadata"]["priority"] == enum_value


# ── Validation errors ──────────────────────────────────────────────────


def test_task_create_unknown_project_exits_two(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Slug in neither Lithos (note_list) nor TOML → exit 2, error names
    the known projects, no task created."""
    config_path = _write_config(tmp_path, projects=("lithos-loom",))
    # No Lithos project-context docs; only the TOML slug is known.
    fake = _install_fake(monkeypatch)

    result = runner.invoke(
        app,
        [
            "task",
            "create",
            "--project",
            "nonexistent",
            "--title",
            "x",
            "--config",
            str(config_path),
        ],
    )
    assert result.exit_code == 2
    assert "unknown project 'nonexistent'" in result.stderr
    assert "lithos-loom" in result.stderr
    assert fake.calls_to("task_create") == []


def test_task_create_accepts_lithos_only_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bug regression guard: a project created via the macro exists
    in Lithos but NOT in TOML [projects] — task create must accept it."""
    # TOML has a different project; the macro-created one is Lithos-only.
    config_path = _write_config(tmp_path, projects=("other-project",))
    fake = _install_fake(monkeypatch, notes=(_project_note("macro-made"),))

    result = runner.invoke(
        app,
        [
            "task",
            "create",
            "--project",
            "macro-made",
            "--title",
            "Do the thing",
            "--config",
            str(config_path),
        ],
    )
    assert result.exit_code == 0, result.stderr
    calls = fake.calls_to("task_create")
    assert len(calls) == 1
    assert calls[0]["metadata"] == {"project": "macro-made"}


def test_task_create_accepts_toml_only_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Union leniency: a slug configured in TOML but with no Lithos
    project-context doc still validates (offline / local overlay)."""
    config_path = _write_config(tmp_path, projects=("local-only",))
    fake = _install_fake(monkeypatch)  # nothing in Lithos

    result = runner.invoke(
        app,
        [
            "task",
            "create",
            "--project",
            "local-only",
            "--title",
            "x",
            "--config",
            str(config_path),
        ],
    )
    assert result.exit_code == 0, result.stderr
    assert len(fake.calls_to("task_create")) == 1


def test_task_create_unknown_priority_exits_two(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Non-D18 priority → exit 2 with a list of the valid enum values."""
    config_path = _write_config(tmp_path)
    fake = _install_fake(monkeypatch)

    result = runner.invoke(
        app,
        [
            "task",
            "create",
            "--project",
            "lithos-loom",
            "--title",
            "x",
            "--priority",
            "urgent",  # not in PRIORITY_EMOJI
            "--config",
            str(config_path),
        ],
    )
    assert result.exit_code == 2
    assert "unknown priority 'urgent'" in result.stderr
    assert fake.calls_to("task_create") == []


def test_task_create_missing_required_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Typer's normal "missing required option" error path.

    Asserts on exit code (Typer uses 2 for usage errors) + the
    absence of a Lithos call rather than the text of Typer's error
    message — the latter gets word-wrapped to the terminal width on
    CI's narrow runner, where the option name doesn't make it into
    the rendered panel."""
    config_path = _write_config(tmp_path)
    fake = _install_fake(monkeypatch)
    result = runner.invoke(
        app,
        ["task", "create", "--title", "x", "--config", str(config_path)],
    )
    assert result.exit_code == 2
    assert fake.calls_to("task_create") == []


def test_task_create_missing_required_title(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Typer's normal "missing required option" error path.

    See :func:`test_task_create_missing_required_project` for the
    rationale behind asserting on exit code + no-Lithos-call rather
    than Typer's error text."""
    config_path = _write_config(tmp_path)
    fake = _install_fake(monkeypatch)
    result = runner.invoke(
        app,
        ["task", "create", "--project", "lithos-loom", "--config", str(config_path)],
    )
    assert result.exit_code == 2
    assert fake.calls_to("task_create") == []


# ── Lithos / I/O failure surfacing ─────────────────────────────────────


def test_task_create_lithos_error_exits_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A LithosClientError from task_create surfaces with exit 1 and
    a structured stderr message the macro can display."""
    from lithos_loom.errors import LithosClientError

    config_path = _write_config(tmp_path)
    fake = _install_fake(monkeypatch)
    fake.raise_on["task_create"] = LithosClientError(
        "invalid_input", "title cannot be empty"
    )

    result = runner.invoke(
        app,
        [
            "task",
            "create",
            "--project",
            "lithos-loom",
            "--title",
            "x",
            "--config",
            str(config_path),
        ],
    )
    assert result.exit_code == 1
    assert "task_create failed" in result.stderr
    assert "title cannot be empty" in result.stderr


def test_task_create_connection_failure_exits_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``OSError`` (e.g. Lithos daemon down) exits 1 with a connection-
    error message naming the configured URL."""
    config_path = _write_config(tmp_path)
    fake = _install_fake(monkeypatch)
    fake.raise_on["task_create"] = OSError("Connection refused")

    result = runner.invoke(
        app,
        [
            "task",
            "create",
            "--project",
            "lithos-loom",
            "--title",
            "x",
            "--config",
            str(config_path),
        ],
    )
    assert result.exit_code == 1
    assert "could not reach Lithos" in result.stderr
    assert "http://localhost:8765" in result.stderr


def test_task_create_target_file_write_failure_exits_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A write failure on the target file exits 1 with a clear
    message. Simulated by patching ``_append_line`` to raise."""
    config_path = _write_config(tmp_path)
    _install_fake(monkeypatch)

    def _boom(target: Path, line: str) -> None:
        raise PermissionError(f"denied: {target}")

    monkeypatch.setattr(cli_task_mod, "_append_line", _boom)

    result = runner.invoke(
        app,
        [
            "task",
            "create",
            "--project",
            "lithos-loom",
            "--title",
            "x",
            "--target-file",
            str(tmp_path / "out.md"),
            "--config",
            str(config_path),
        ],
    )
    assert result.exit_code == 1
    assert "could not write to" in result.stderr


# ── JSON-format invariants (used by the Templater macro) ───────────────


def test_project_list_json_is_machine_parseable(tmp_path: Path) -> None:
    """``lithos-loom project list --format json`` returns clean JSON
    the macro can ``JSON.parse``. Tested here (CLI-level) so a future
    formatting change has to break this loudly.

    Pinned via ``--source toml`` because Slice 4 (D23) flipped the
    default to ``--source lithos`` (which requires a live Lithos
    connection). The macro itself can use either source — the JSON
    shape is invariant (array of slugs); this test just locks the
    machine-parseable contract."""
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        dedent(
            f"""
            [orchestrator]
            agent_id = "lithos-orchestrator-test"
            lithos_url = "http://localhost:8765"

            [projects.alpha]
            repo = "{repo}"
            """
        )
    )

    result = runner.invoke(
        app,
        [
            "project",
            "list",
            "--config",
            str(config_path),
            "--source",
            "toml",
            "--format",
            "json",
        ],
    )
    assert result.exit_code == 0
    parsed = json.loads(result.stdout)
    assert parsed == ["alpha"]
