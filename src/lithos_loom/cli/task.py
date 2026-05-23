"""``lithos-loom task`` sub-app (Slice 3, US24-27).

Currently exposes only ``create``, which the capture-macro Templater
script shells out to. The CLI takes the prompted form fields, calls
``lithos_task_create`` (with metadata, post-lithos#295), and renders
the projected line via the shared :mod:`lithos_loom.render` module
so the output is byte-equal to what the projection will write on its
next pass — that's what makes US25's "born projected" guarantee work
end-to-end.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import typer

from lithos_loom.config import LoomConfig, load_config
from lithos_loom.errors import LithosLoomError
from lithos_loom.lithos_client import LithosClient, Task
from lithos_loom.render import PRIORITY_EMOJI, render_line, validated_priority

task_app = typer.Typer(
    name="task",
    help="Task-creation CLI helpers (Slice 3+).",
    no_args_is_help=True,
)


@task_app.command("create")
def task_create(
    project: str = typer.Option(
        ...,
        "--project",
        "-p",
        help="Project slug (must match a [projects.<name>] entry in TOML).",
    ),
    title: str = typer.Option(
        ...,
        "--title",
        "-t",
        help="Task title.",
    ),
    brief: str | None = typer.Option(
        None,
        "--brief",
        "-b",
        help="Optional task description / brief.",
    ),
    scheduled: str | None = typer.Option(
        None,
        "--scheduled",
        "-s",
        help="Optional scheduled date (YYYY-MM-DD).",
    ),
    priority: str | None = typer.Option(
        None,
        "--priority",
        help=(
            "Optional priority (one of: "
            + ", ".join(PRIORITY_EMOJI)
            + "). Stored as metadata.priority."
        ),
    ),
    tags: str | None = typer.Option(
        None,
        "--tags",
        help="Optional comma-separated tag list.",
    ),
    target_file: Path | None = typer.Option(
        None,
        "--target-file",
        help=(
            "Optional file to append the projected line to instead of "
            "printing to stdout (US27). Created if missing; the line "
            "is appended with a trailing newline. Mutually exclusive "
            "with --no-insert."
        ),
    ),
    no_insert: bool = typer.Option(
        False,
        "--no-insert",
        help=(
            "Don't emit the projected line anywhere — just create the "
            "task and print its id (US27). Useful for scripted flows "
            "that only need the task_id back. Mutually exclusive with "
            "--target-file."
        ),
    ),
    config: Path | None = typer.Option(
        None,
        "--config",
        "-c",
        help="Explicit TOML config path (overrides LITHOS_LOOM_CONFIG).",
    ),
) -> None:
    """Create a Lithos task and emit its projected line.

    Validates ``--project`` against the configured ``[projects]``
    table, validates ``--priority`` against the D18 enum, then calls
    ``lithos_task_create`` with the assembled metadata in a single
    RPC (lithos#295). On success, renders the projected line via
    the shared :func:`lithos_loom.render.render_line` so a macro-
    inserted line is byte-equal to what the projection will write.

    Output mode (US27):
    * Default: print the projected line to stdout (macro inserts at cursor).
    * ``--target-file PATH``: append the line to PATH; nothing to stdout.
    * ``--no-insert``: print just the new task's id to stdout; the
      projected line is discarded. Useful for "create-and-discard"
      scripted flows.

    Exit codes:
    * 0 — success.
    * 1 — config load / Lithos RPC failure / target-file write failure.
    * 2 — input validation error (unknown project, bad priority,
      mutually-exclusive flags).
    """
    try:
        cfg = load_config(config)
    except LithosLoomError as exc:
        typer.echo(f"lithos-loom: {exc}", err=True)
        sys.exit(1)

    if no_insert and target_file is not None:
        typer.echo(
            "lithos-loom: --no-insert and --target-file are mutually exclusive",
            err=True,
        )
        sys.exit(2)

    if project not in cfg.projects:
        configured = ", ".join(sorted(cfg.projects)) or "(none)"
        typer.echo(
            f"lithos-loom: unknown project {project!r}; "
            f"configured projects: {configured}",
            err=True,
        )
        sys.exit(2)

    if priority is not None and priority not in PRIORITY_EMOJI:
        typer.echo(
            f"lithos-loom: unknown priority {priority!r} "
            f"(expected one of: {', '.join(PRIORITY_EMOJI)})",
            err=True,
        )
        sys.exit(2)

    tag_list = _split_tags(tags)
    metadata = _build_metadata(project=project, priority=priority, scheduled=scheduled)

    try:
        task_id = asyncio.run(
            _create_task_async(
                cfg=cfg,
                title=title,
                description=brief,
                tags=tag_list,
                metadata=metadata,
            )
        )
    except LithosLoomError as exc:
        typer.echo(f"lithos-loom: task_create failed: {exc}", err=True)
        sys.exit(1)
    except OSError as exc:
        typer.echo(
            f"lithos-loom: could not reach Lithos at "
            f"{cfg.orchestrator.lithos_url} ({exc})",
            err=True,
        )
        sys.exit(1)

    # --no-insert short-circuits before we bother rendering a line
    # we're about to throw away. Print just the task_id so scripted
    # callers can capture it from stdout.
    if no_insert:
        typer.echo(task_id)
        return

    task = Task(
        id=task_id,
        title=title,
        status="open",
        tags=tuple(tag_list),
        metadata=metadata,
        claims=(),
    )
    # ``validated_priority`` deliberately silent on unknown enums —
    # the explicit ``priority not in PRIORITY_EMOJI`` check above
    # rejects bad values before they reach this point.
    _ = validated_priority(task)
    today = datetime.now(UTC).astimezone().date()
    line = render_line(task, cfg.routes, today)

    if target_file is not None:
        try:
            _append_line(target_file, line)
        except OSError as exc:
            typer.echo(
                f"lithos-loom: could not write to {target_file}: {exc}",
                err=True,
            )
            sys.exit(1)
    else:
        typer.echo(line)


async def _create_task_async(
    *,
    cfg: LoomConfig,
    title: str,
    description: str | None,
    tags: list[str],
    metadata: dict[str, Any],
) -> str:
    """One-shot ``async with LithosClient(...)`` wrapper around
    ``task_create``. Returns the new task's id."""
    async with LithosClient(
        cfg.orchestrator.lithos_url, agent_id=cfg.orchestrator.agent_id
    ) as client:
        return await client.task_create(
            title=title,
            description=description,
            tags=tags or None,
            metadata=metadata or None,
        )


def _split_tags(raw: str | None) -> list[str]:
    """Parse the comma-separated --tags string into a clean list.

    Strips whitespace and drops empty entries so ``"a, , b"`` becomes
    ``["a", "b"]``. Returns ``[]`` for ``None`` / empty input."""
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


def _build_metadata(
    *,
    project: str,
    priority: str | None,
    scheduled: str | None,
) -> dict[str, Any]:
    """Assemble the ``metadata`` dict sent to ``lithos_task_create``.

    Keys with ``None`` values are omitted entirely so the projection
    sees a clean metadata dict — present means set, absent means
    not given.
    """
    metadata: dict[str, Any] = {"project": project}
    if priority is not None:
        metadata["priority"] = priority
    if scheduled is not None:
        metadata["scheduled_for"] = scheduled
    return metadata


def _append_line(target: Path, line: str) -> None:
    """Append ``line + "\\n"`` to ``target``, creating parent dirs as
    needed. Atomic-ish: the open-append-close happens in one syscall
    each so partial writes from a crash are bounded to a single
    short line."""
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as fh:
        fh.write(line)
        fh.write("\n")
