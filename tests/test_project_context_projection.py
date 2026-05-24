"""Tests for ``lithos_loom.subscriptions._project_context_projection``
(Slice 4 US29).

The handler consumes ``lithos.note.{created,updated,deleted}`` events,
filters at the boundary (path-prefix + tag per D26), re-fetches via
``note_read`` for the full body, renders, and atomic-writes per-doc
files under ``<vault>/<projects_dir>/<slug>/<filename>.md``.

Tests inject a fake LithosClient (via SubscriptionContext.lithos) and
a temp vault to exercise the handler end-to-end without an HTTP call.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any
from unittest.mock import AsyncMock

import pytest

from lithos_loom.bus import Event
from lithos_loom.config import (
    LoomConfig,
    ObsidianSyncConfig,
    OrchestratorConfig,
)
from lithos_loom.lithos_client import Note
from lithos_loom.render_project_context import (
    extract_frontmatter,
    render_doc,
)
from lithos_loom.subscriptions import SubscriptionContext
from lithos_loom.subscriptions._project_context_projection import make_handler
from lithos_loom.sync_state import ProjectionSyncState

# ── Test helpers ────────────────────────────────────────────────────────


def _note(
    *,
    id_: str = "doc-1",
    title: str = "Lithos Loom",
    body: str = "Body content.",
    version: int = 12,
    tags: tuple[str, ...] = ("project-context",),
    path: str = "projects/lithos-loom/context.md",
) -> Note:
    return Note(
        id=id_,
        title=title,
        body=body,
        version=version,
        updated_at=datetime(2026, 5, 24, 14, 30, tzinfo=UTC),
        tags=tags,
        status="active",
        note_type="concept",
        path=path,
        slug=path.split("/")[1] if path.startswith("projects/") else "",
    )


def _cfg(tmp_path: Path) -> LoomConfig:
    return LoomConfig(
        orchestrator=OrchestratorConfig(
            agent_id="lithos-orchestrator-test",
            lithos_url="http://localhost:8765",
        ),
        obsidian_sync=ObsidianSyncConfig(
            vault_path=tmp_path / "vault",
            tasks_file=Path("_lithos/tasks.md"),
            projects_dir=Path("_lithos/projects"),
        ),
    )


def _ctx(lithos: Any | None = None) -> SubscriptionContext:
    return SubscriptionContext(
        lithos=lithos if lithos is not None else AsyncMock(),
        logger=logging.getLogger("test.project_context_projection"),
        agent_id="lithos-orchestrator-test",
    )


def _event(
    event_type: str,
    *,
    id_: str = "doc-1",
    title: str = "Lithos Loom",
    path: str = "projects/lithos-loom/context.md",
) -> Event:
    payload = {"id": id_, "title": title, "path": path}
    return Event(
        type=event_type,
        timestamp=datetime.now(UTC),
        payload=MappingProxyType(payload),
    )


def _vault_path(tmp_path: Path, rel: str) -> Path:
    return tmp_path / "vault" / "_lithos" / "projects" / rel


# ── Created / updated happy path ───────────────────────────────────────


async def test_created_writes_projected_file(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    lithos = AsyncMock()
    lithos.note_read.return_value = _note()
    sync_state = ProjectionSyncState()
    handler = make_handler(cfg, sync_state=sync_state)

    await handler(_event("lithos.note.created"), _ctx(lithos))

    target = _vault_path(tmp_path, "lithos-loom/context.md")
    assert target.exists(), f"expected projection at {target}"
    body = target.read_text()
    assert "lithos_id: doc-1" in body
    assert "# Lithos Loom" in body


async def test_updated_overwrites_existing_file(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    lithos = AsyncMock()
    lithos.note_read.return_value = _note(version=1, body="Original.")
    sync_state = ProjectionSyncState()
    handler = make_handler(cfg, sync_state=sync_state)

    # First write
    await handler(_event("lithos.note.created"), _ctx(lithos))
    target = _vault_path(tmp_path, "lithos-loom/context.md")
    assert "Original." in target.read_text()

    # Updated version
    lithos.note_read.return_value = _note(version=2, body="Updated.")
    await handler(_event("lithos.note.updated"), _ctx(lithos))
    text = target.read_text()
    assert "Updated." in text
    assert "lithos_version: 2" in text
    assert "Original." not in text


async def test_re_fetches_via_note_read_on_each_event(tmp_path: Path) -> None:
    """The SSE payload only carries ``{id, title, path}``; the
    handler must call ``note_read(id=...)`` to get the body + tags +
    version. Otherwise the rendered file would be incomplete and
    re-projection on bootstrap would silently differ from live
    updates."""
    cfg = _cfg(tmp_path)
    lithos = AsyncMock()
    lithos.note_read.return_value = _note()
    sync_state = ProjectionSyncState()
    handler = make_handler(cfg, sync_state=sync_state)

    await handler(_event("lithos.note.created"), _ctx(lithos))

    lithos.note_read.assert_awaited_once_with(id="doc-1")


# ── Filters (D26) ──────────────────────────────────────────────────────


async def test_filters_event_with_path_outside_projects(tmp_path: Path) -> None:
    """SSE payloads from outside ``projects/`` are dropped before
    the ``note_read`` round-trip — saves the redundant lookup."""
    cfg = _cfg(tmp_path)
    lithos = AsyncMock()
    sync_state = ProjectionSyncState()
    handler = make_handler(cfg, sync_state=sync_state)

    event = _event("lithos.note.created", path="observations/inbox/foo.md")
    await handler(event, _ctx(lithos))

    lithos.note_read.assert_not_awaited()


async def test_filters_fetched_note_without_project_context_tag(
    tmp_path: Path,
) -> None:
    """The SSE payload may pass the cheap path-prefix filter but the
    fresh note from ``note_read`` may have a different tag set
    (e.g. operator removed the project-context tag). Re-check the
    authoritative tags post-fetch — drop without writing."""
    cfg = _cfg(tmp_path)
    lithos = AsyncMock()
    lithos.note_read.return_value = _note(tags=("other-tag",))
    sync_state = ProjectionSyncState()
    handler = make_handler(cfg, sync_state=sync_state)

    await handler(_event("lithos.note.created"), _ctx(lithos))

    target = _vault_path(tmp_path, "lithos-loom/context.md")
    assert not target.exists()


async def test_filters_fetched_note_whose_path_changed(tmp_path: Path) -> None:
    """Fetched path no longer under ``projects/`` (operator moved
    the doc) → drop, don't write to a stale slug location."""
    cfg = _cfg(tmp_path)
    lithos = AsyncMock()
    lithos.note_read.return_value = _note(path="observations/inbox/foo.md")
    sync_state = ProjectionSyncState()
    handler = make_handler(cfg, sync_state=sync_state)

    await handler(_event("lithos.note.created"), _ctx(lithos))

    assert not (tmp_path / "vault").exists() or not any(
        (tmp_path / "vault" / "_lithos" / "projects").rglob("*.md")
    )


async def test_skips_when_note_not_found_in_lithos(tmp_path: Path) -> None:
    """``note_read`` returns ``None`` (race: doc deleted between
    event and read) → skip cleanly, no crash."""
    cfg = _cfg(tmp_path)
    lithos = AsyncMock()
    lithos.note_read.return_value = None
    sync_state = ProjectionSyncState()
    handler = make_handler(cfg, sync_state=sync_state)

    await handler(_event("lithos.note.created"), _ctx(lithos))

    assert not (tmp_path / "vault").exists() or not any(
        (tmp_path / "vault" / "_lithos" / "projects").rglob("*.md")
    )


# ── Sync-state coordination ────────────────────────────────────────────


async def test_records_body_hash_and_version_in_sync_state(tmp_path: Path) -> None:
    """The dir-watcher (Slice 5) reads these to suppress self-writes
    and to provide ``expected_version`` for optimistic locking."""
    cfg = _cfg(tmp_path)
    lithos = AsyncMock()
    lithos.note_read.return_value = _note(version=7)
    sync_state = ProjectionSyncState()
    handler = make_handler(cfg, sync_state=sync_state)

    await handler(_event("lithos.note.created"), _ctx(lithos))

    assert "doc-1" in sync_state.note_content_hashes
    assert sync_state.note_versions["doc-1"] == 7


async def test_skips_write_when_body_hash_matches_last_write(tmp_path: Path) -> None:
    """Per-doc dedup. Re-firing ``created`` with the same body is a
    no-op — important for bootstrap on cold restart (N notes →
    0 disk writes when nothing has changed)."""
    cfg = _cfg(tmp_path)
    lithos = AsyncMock()
    lithos.note_read.return_value = _note()
    sync_state = ProjectionSyncState()
    handler = make_handler(cfg, sync_state=sync_state)

    await handler(_event("lithos.note.created"), _ctx(lithos))
    target = _vault_path(tmp_path, "lithos-loom/context.md")
    first_mtime = target.stat().st_mtime_ns

    # Second event: same body → no-op.
    await handler(_event("lithos.note.created"), _ctx(lithos))

    assert target.stat().st_mtime_ns == first_mtime


async def test_body_change_writes_new_content(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    lithos = AsyncMock()
    lithos.note_read.return_value = _note(body="v1")
    sync_state = ProjectionSyncState()
    handler = make_handler(cfg, sync_state=sync_state)

    await handler(_event("lithos.note.created"), _ctx(lithos))
    target = _vault_path(tmp_path, "lithos-loom/context.md")

    lithos.note_read.return_value = _note(body="v2")
    await handler(_event("lithos.note.updated"), _ctx(lithos))

    assert "v2" in target.read_text()
    assert "v1" not in target.read_text()


# ── Slug + path mapping ────────────────────────────────────────────────


async def test_slug_drives_subdirectory(tmp_path: Path) -> None:
    """Slug is the first path segment after ``projects/`` — both the
    vault subdir and the rendered ``slug:`` frontmatter."""
    cfg = _cfg(tmp_path)
    lithos = AsyncMock()
    lithos.note_read.return_value = _note(
        id_="doc-influx",
        path="projects/influx/context.md",
    )
    sync_state = ProjectionSyncState()
    handler = make_handler(cfg, sync_state=sync_state)

    await handler(
        _event(
            "lithos.note.created",
            id_="doc-influx",
            path="projects/influx/context.md",
        ),
        _ctx(lithos),
    )

    target = _vault_path(tmp_path, "influx/context.md")
    assert target.exists()
    fm, _ = extract_frontmatter(target.read_text())
    assert fm["slug"] == "influx"


async def test_nested_filename_preserved_under_slug(tmp_path: Path) -> None:
    """A doc at ``projects/lithos-loom/architecture/design.md``
    lands at ``<vault>/_lithos/projects/lithos-loom/architecture/design.md``
    — multi-segment filenames keep their structure."""
    cfg = _cfg(tmp_path)
    lithos = AsyncMock()
    lithos.note_read.return_value = _note(
        path="projects/lithos-loom/architecture/design.md"
    )
    sync_state = ProjectionSyncState()
    handler = make_handler(cfg, sync_state=sync_state)

    await handler(
        _event(
            "lithos.note.created",
            path="projects/lithos-loom/architecture/design.md",
        ),
        _ctx(lithos),
    )

    target = _vault_path(tmp_path, "lithos-loom/architecture/design.md")
    assert target.exists()


# ── Deleted events ─────────────────────────────────────────────────────


async def test_deleted_removes_local_file(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    lithos = AsyncMock()
    lithos.note_read.return_value = _note()
    sync_state = ProjectionSyncState()
    handler = make_handler(cfg, sync_state=sync_state)

    # Create first
    await handler(_event("lithos.note.created"), _ctx(lithos))
    target = _vault_path(tmp_path, "lithos-loom/context.md")
    assert target.exists()

    # Now delete
    await handler(_event("lithos.note.deleted"), _ctx(lithos))

    assert not target.exists()


async def test_deleted_forgets_sync_state(tmp_path: Path) -> None:
    """After delete, ``forget_project_context`` must clear the
    per-doc hash so a subsequent re-creation of the same doc is
    NOT suppressed as a self-write."""
    cfg = _cfg(tmp_path)
    lithos = AsyncMock()
    lithos.note_read.return_value = _note()
    sync_state = ProjectionSyncState()
    handler = make_handler(cfg, sync_state=sync_state)

    await handler(_event("lithos.note.created"), _ctx(lithos))
    assert "doc-1" in sync_state.note_content_hashes

    await handler(_event("lithos.note.deleted"), _ctx(lithos))

    assert "doc-1" not in sync_state.note_content_hashes
    assert "doc-1" not in sync_state.note_versions


async def test_deleted_missing_file_is_silent(tmp_path: Path) -> None:
    """Best-effort delete — file already absent (operator removed
    manually, or earlier failed write) is fine."""
    cfg = _cfg(tmp_path)
    lithos = AsyncMock()
    sync_state = ProjectionSyncState()
    handler = make_handler(cfg, sync_state=sync_state)

    # No prior create — straight to delete.
    await handler(_event("lithos.note.deleted"), _ctx(lithos))

    # No exception, sync_state still clean.
    assert sync_state.note_content_hashes == {}


async def test_deleted_does_not_call_note_read(tmp_path: Path) -> None:
    """The note is gone from Lithos by the time we react —
    ``note_read`` would return None anyway. Skip the round-trip."""
    cfg = _cfg(tmp_path)
    lithos = AsyncMock()
    sync_state = ProjectionSyncState()
    handler = make_handler(cfg, sync_state=sync_state)

    await handler(_event("lithos.note.deleted"), _ctx(lithos))

    lithos.note_read.assert_not_awaited()


async def test_deleted_skips_path_outside_projects(tmp_path: Path) -> None:
    """A deleted event for a non-project-context doc shouldn't
    even attempt the unlink — the path is outside our managed
    directory tree."""
    cfg = _cfg(tmp_path)
    lithos = AsyncMock()
    sync_state = ProjectionSyncState()
    handler = make_handler(cfg, sync_state=sync_state)

    await handler(
        _event(
            "lithos.note.deleted",
            id_="other",
            path="observations/inbox/foo.md",
        ),
        _ctx(lithos),
    )

    # No assertion needed beyond no-crash; "outside projects/" is
    # debug-logged.


# ── Robustness ─────────────────────────────────────────────────────────


async def test_unknown_event_type_is_silently_ignored(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    lithos = AsyncMock()
    sync_state = ProjectionSyncState()
    handler = make_handler(cfg, sync_state=sync_state)

    await handler(
        _event("lithos.task.created"),  # wrong namespace
        _ctx(lithos),
    )

    lithos.note_read.assert_not_awaited()


async def test_malformed_payload_warns_and_returns(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Missing ``id`` in payload → warn-log + drop, no crash."""
    cfg = _cfg(tmp_path)
    lithos = AsyncMock()
    sync_state = ProjectionSyncState()
    handler = make_handler(cfg, sync_state=sync_state)

    event = Event(
        type="lithos.note.created",
        timestamp=datetime.now(UTC),
        payload=MappingProxyType({"title": "no id"}),
    )
    with caplog.at_level(logging.WARNING, logger="test.project_context_projection"):
        await handler(event, _ctx(lithos))

    warn_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("malformed payload" in m for m in warn_msgs), warn_msgs


async def test_write_failure_rolls_back_sync_state(tmp_path: Path) -> None:
    """If the atomic write raises, the per-doc hash must NOT be
    recorded — otherwise the next event would see "matches last
    write" and skip, leaving the disk content stale forever."""
    cfg = _cfg(tmp_path)
    lithos = AsyncMock()
    lithos.note_read.return_value = _note()
    sync_state = ProjectionSyncState()
    handler = make_handler(cfg, sync_state=sync_state)

    # Make the projects_root unwritable to force write_file_atomic to fail.
    # mkdir-then-chmod the parent so the recursive parent creation can't
    # succeed.
    projects_root = tmp_path / "vault" / "_lithos" / "projects"
    projects_root.parent.mkdir(parents=True)
    projects_root.mkdir()
    projects_root.chmod(0o400)  # read-only

    try:
        with pytest.raises(Exception):  # noqa: B017
            await handler(_event("lithos.note.created"), _ctx(lithos))

        # State rolled back — re-firing must retry the write.
        assert "doc-1" not in sync_state.note_content_hashes
    finally:
        projects_root.chmod(0o755)  # restore for cleanup


# ── make_handler defensive checks ──────────────────────────────────────


def test_make_handler_raises_without_obsidian_sync_config() -> None:
    """The spawn gate is upstream; this is a defensive belt-and-
    braces check."""
    cfg = LoomConfig(
        orchestrator=OrchestratorConfig(
            agent_id="x",
            lithos_url="http://localhost:8765",
        ),
        # no obsidian_sync
    )
    with pytest.raises(RuntimeError, match="without \\[obsidian_sync\\]"):
        make_handler(cfg)


def test_make_handler_creates_fresh_sync_state_when_none() -> None:
    """Test convenience: passing None constructs a fresh state so
    tests don't have to wire one when they don't care about
    cross-handler coordination."""
    cfg = LoomConfig(
        orchestrator=OrchestratorConfig(
            agent_id="x",
            lithos_url="http://localhost:8765",
        ),
        obsidian_sync=ObsidianSyncConfig(vault_path=Path("/tmp/v")),
    )
    handler = make_handler(cfg)  # no sync_state
    # Should construct without raising.
    assert callable(handler)


def test_round_trip_render_then_extract(tmp_path: Path) -> None:
    """End-to-end sanity: a rendered file parses back to recover
    the same id, version, slug, tags."""
    note = _note()
    rendered = render_doc(note)
    fm, _ = extract_frontmatter(rendered)
    assert fm["lithos_id"] == note.id
    assert fm["lithos_version"] == note.version
    assert fm["slug"] == note.slug
    assert fm["tags"] == list(note.tags)
