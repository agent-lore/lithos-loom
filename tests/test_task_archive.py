"""Tests for the task-archive handler (Slice 6 US39–US46).

Drives the handler directly with synthetic terminal Events against a
tmp_path vault. The D38 surfaced gate, D36 ``_unassigned`` fallback,
slug path-safety, cold-start dedup, and the D39 archived-flag /
no-data-loss-on-failure contract are exercised here; the cross-handler
eviction coupling with the projection is covered in
``test_obsidian_sync_child.py`` and ``test_obsidian_projection.py``.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from lithos_loom.bus import Event
from lithos_loom.config import (
    LoomConfig,
    ObsidianSyncConfig,
    OrchestratorConfig,
)
from lithos_loom.subscriptions import SubscriptionContext, _task_archive
from lithos_loom.subscriptions._obsidian_projection import (
    make_handler as make_projection_handler,
)
from lithos_loom.subscriptions._task_archive import make_handler
from lithos_loom.sync_state import ArchiveGateState

# ── Fixtures ───────────────────────────────────────────────────────────


def _ctx() -> SubscriptionContext:
    # The archiver only reads ctx.logger; lithos/agent_id are unused.
    return SubscriptionContext(
        lithos=AsyncMock(),
        logger=logging.getLogger("test.task_archive"),
        agent_id="lithos-orchestrator-test",
    )


def _cfg(tmp_path: Path) -> LoomConfig:
    return LoomConfig(
        orchestrator=OrchestratorConfig(
            agent_id="lithos-orchestrator-test",
            lithos_url="http://localhost:8765",
        ),
        obsidian_sync=ObsidianSyncConfig(vault_path=tmp_path),
    )


def _terminal_event(
    *,
    task_id: str,
    title: str = "Ship the thing",
    completed: bool = True,
    project: str | None = "demo",
    resolved_at: str | None = "2026-05-20T09:00:00+00:00",
    metadata: Mapping[str, Any] | None = None,
) -> Event:
    meta: dict[str, Any] = dict(metadata or {})
    if project is not None and "project" not in meta:
        meta["project"] = project
    payload: dict[str, Any] = {
        "id": task_id,
        "title": title,
        "status": "completed" if completed else "cancelled",
        "tags": [],
        "metadata": meta,
        "claims": [],
    }
    if resolved_at is not None:
        payload["resolved_at"] = resolved_at
    return Event(
        type="lithos.task.completed" if completed else "lithos.task.cancelled",
        timestamp=datetime.now(UTC),
        payload=payload,
    )


def _open_event(
    task_id: str, *, title: str = "Ship it", project: str = "demo"
) -> Event:
    return Event(
        type="lithos.task.created",
        timestamp=datetime.now(UTC),
        payload={
            "id": task_id,
            "title": title,
            "status": "open",
            "tags": [],
            "metadata": {"project": project},
            "claims": [],
        },
    )


def _done_file(tmp_path: Path, slug: str) -> Path:
    return tmp_path / "_lithos/projects" / slug / f"{slug}-done.md"


def _surfaced(*task_ids: str) -> ArchiveGateState:
    state = ArchiveGateState()
    for tid in task_ids:
        state.surfaced[tid] = True
    return state


# ── US39: completed task appends a done line ───────────────────────────


async def test_completed_surfaced_task_appends_done_line(tmp_path: Path) -> None:
    state = _surfaced("t1")
    handler = make_handler(_cfg(tmp_path), archive_gate=state)
    await handler(_terminal_event(task_id="t1", title="Ship it"), _ctx())

    done = _done_file(tmp_path, "demo")
    content = done.read_text()
    assert "- [x] Ship it 🆔 lithos:t1 #project/demo ✅ 2026-05-20" in content
    assert state.archived["t1"] is True


# ── US40: cancelled task appends a [-] line ────────────────────────────


async def test_cancelled_surfaced_task_appends_cancel_line(tmp_path: Path) -> None:
    state = _surfaced("t2")
    handler = make_handler(_cfg(tmp_path), archive_gate=state)
    await handler(
        _terminal_event(task_id="t2", title="Drop it", completed=False), _ctx()
    )

    content = _done_file(tmp_path, "demo").read_text()
    assert "- [-] Drop it 🆔 lithos:t2 #project/demo ❌ 2026-05-20" in content
    assert state.archived["t2"] is True


# ── US41 / D38: never-surfaced tasks are skipped ───────────────────────


async def test_unsurfaced_task_skipped(tmp_path: Path) -> None:
    state = ArchiveGateState()  # surfaced is empty
    handler = make_handler(_cfg(tmp_path), archive_gate=state)
    await handler(_terminal_event(task_id="bg"), _ctx())

    assert not _done_file(tmp_path, "demo").exists()
    assert "bg" not in state.archived


# ── US43 / D36: missing project → _unassigned ──────────────────────────


async def test_missing_project_falls_back_to_unassigned(tmp_path: Path) -> None:
    state = _surfaced("t3")
    handler = make_handler(_cfg(tmp_path), archive_gate=state)
    await handler(
        _terminal_event(task_id="t3", title="Loose end", project=None), _ctx()
    )

    done = _done_file(tmp_path, "_unassigned")
    content = done.read_text()
    assert "- [x] Loose end 🆔 lithos:t3" in content
    # No #project tag when the slug is absent.
    assert "#project/" not in content
    assert not _done_file(tmp_path, "demo").exists()


@pytest.mark.parametrize("bad_slug", ["../etc", "foo/bar", ".hidden", "", "  "])
async def test_unsafe_slug_routed_to_unassigned(tmp_path: Path, bad_slug: str) -> None:
    """Path-traversal / nested-dir slugs never escape projects_root."""
    state = _surfaced("t4")
    handler = make_handler(_cfg(tmp_path), archive_gate=state)
    await handler(_terminal_event(task_id="t4", project=bad_slug), _ctx())

    # Write lands in the _unassigned bucket, inside projects_root.
    done = _done_file(tmp_path, "_unassigned")
    assert done.exists()
    assert "🆔 lithos:t4" in done.read_text()
    # Nothing escaped the vault's projects tree.
    projects_root = tmp_path / "_lithos/projects"
    written = list(projects_root.rglob("*-done.md"))
    assert written == [done]


# ── US44 / D34: cold-start dedup against on-disk ids ───────────────────


async def test_cold_start_dedup_skips_on_disk_id(tmp_path: Path) -> None:
    # Pre-seed the done file with t5 already archived (e.g. a prior run).
    done = _done_file(tmp_path, "demo")
    done.parent.mkdir(parents=True, exist_ok=True)
    done.write_text("- [x] Old line 🆔 lithos:t5 #project/demo ✅ 2026-05-01\n")

    state = _surfaced("t5")
    handler = make_handler(_cfg(tmp_path), archive_gate=state)
    await handler(_terminal_event(task_id="t5"), _ctx())

    content = done.read_text()
    # Not double-appended.
    assert content.count("🆔 lithos:t5") == 1
    # But the flag is still set so the projection still evicts the line.
    assert state.archived["t5"] is True


async def test_duplicate_event_blocked_by_surfaced_gate(tmp_path: Path) -> None:
    """In-session, a second terminal event for the same task can't
    double-append: the first success pops ``surfaced``, so the duplicate
    fails the D38 gate before it ever reaches the append (the dedup cache
    is the cold-start defence; the surfaced-pop is the in-session one)."""
    state = _surfaced("t6")
    handler = make_handler(_cfg(tmp_path), archive_gate=state)
    await handler(_terminal_event(task_id="t6"), _ctx())
    assert "t6" not in state.surfaced  # popped on success
    # A duplicate terminal event now no-ops at the surfaced gate.
    await handler(_terminal_event(task_id="t6"), _ctx())

    content = _done_file(tmp_path, "demo").read_text()
    assert content.count("🆔 lithos:t6") == 1


# ── D39: write failure leaves no flag, re-raises ───────────────────────


async def test_archive_write_failure_leaves_flag_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_open = os.open

    def _failing_open(path: Any, *args: Any, **kwargs: Any) -> int:
        if str(path).endswith("-done.md"):
            raise OSError("simulated append failure")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(_task_archive.os, "open", _failing_open)

    state = _surfaced("t7")
    handler = make_handler(_cfg(tmp_path), archive_gate=state)
    with pytest.raises(OSError, match="simulated append failure"):
        await handler(_terminal_event(task_id="t7"), _ctx())

    # No flag set (so the projection keeps the [x] line under TTL), and
    # surfaced is NOT popped (a retry must still pass the gate).
    assert "t7" not in state.archived
    assert state.surfaced.get("t7") is True


# ── Lazy cache: done file read once per slug ───────────────────────────


async def test_lazy_cache_loaded_once_per_slug(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reads: list[Path] = []
    real_loader = _task_archive._load_done_ids

    def _spy_loader(path: Path) -> set[str]:
        reads.append(path)
        return real_loader(path)

    monkeypatch.setattr(_task_archive, "_load_done_ids", _spy_loader)

    state = _surfaced("a", "b")
    handler = make_handler(_cfg(tmp_path), archive_gate=state)
    await handler(_terminal_event(task_id="a"), _ctx())
    await handler(_terminal_event(task_id="b"), _ctx())

    # Both events targeted the same slug → the done file is scanned once.
    assert len(reads) == 1


# ── Non-terminal events ignored ────────────────────────────────────────


async def test_non_terminal_event_ignored(tmp_path: Path) -> None:
    state = _surfaced("t8")
    handler = make_handler(_cfg(tmp_path), archive_gate=state)
    evt = Event(
        type="lithos.task.created",
        timestamp=datetime.now(UTC),
        payload={"id": "t8", "title": "x", "status": "open", "metadata": {}},
    )
    await handler(evt, _ctx())
    assert not _done_file(tmp_path, "demo").exists()
    assert "t8" not in state.archived


# ── F2: partial os.write must not truncate the archive line ────────────


async def test_append_loops_on_partial_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """os.write may return a short count; _append_line must loop so the
    line is never truncated, and archived is only set after it fully
    lands (D39)."""
    real_write = os.write

    def _short_write(fd: int, data: Any) -> int:
        b = bytes(data)
        # Force a short write only for our archive line, 4 bytes at a time.
        if b"lithos:" in b and len(b) > 4:
            return real_write(fd, b[:4])
        return real_write(fd, data)

    monkeypatch.setattr(_task_archive.os, "write", _short_write)

    state = _surfaced("t9")
    handler = make_handler(_cfg(tmp_path), archive_gate=state)
    await handler(
        _terminal_event(task_id="t9", title="A reasonably long title"), _ctx()
    )

    content = _done_file(tmp_path, "demo").read_text()
    assert (
        "- [x] A reasonably long title 🆔 lithos:t9 #project/demo ✅ 2026-05-20"
        in content
    )
    assert content.endswith("\n")
    assert state.archived["t9"] is True


# ── F1: eviction follows archiving causally, even if flush ran first ───


async def test_archiver_reflush_evicts_when_projection_flushed_first(
    tmp_path: Path,
) -> None:
    """If the projection renders the [x] line and flushes BEFORE the
    archiver appends, the archiver's reflush still evicts the line — it
    doesn't linger until an unrelated future event (D39 / review F1)."""
    state = ArchiveGateState()
    cfg = _cfg(tmp_path)
    # debounce=0 → projection flushes inline, so the [x] line is on disk
    # before the archiver runs. The archiver's request_projection_flush
    # call must still drive the eviction.
    projection = make_projection_handler(cfg, archive_gate=state)
    archiver = make_handler(cfg, archive_gate=state)
    tasks_file = tmp_path / "_lithos/tasks.md"

    await projection(_open_event("x"), _ctx())
    assert "🆔 lithos:x" in tasks_file.read_text()  # surfaced + on disk
    await projection(_terminal_event(task_id="x"), _ctx())
    # Projection wrote the [x] line; archived not set yet → still present.
    assert "🆔 lithos:x" in tasks_file.read_text()

    # Archiver runs after the flush: append + set archived + reflush.
    await archiver(_terminal_event(task_id="x"), _ctx())

    assert "🆔 lithos:x" not in tasks_file.read_text(), "reflush should evict the line"
    assert _done_file(tmp_path, "demo").read_text().count("🆔 lithos:x") == 1


# ── F3: created+completed within one debounce window is not archived ───


async def test_fast_open_complete_within_window_not_archived(tmp_path: Path) -> None:
    """A task whose open line was never written (created + completed
    coalesced inside one debounce window) is not surfaced, so the
    archiver correctly skips it — surfaced means 'written to tasks.md'
    (D38), not 'entered render state' (review F3)."""
    state = ArchiveGateState()
    cfg = _cfg(tmp_path)
    projection = make_projection_handler(cfg, archive_gate=state, debounce_seconds=0.05)
    archiver = make_handler(cfg, archive_gate=state)

    # Created then completed before the 50ms flush fires: no write yet.
    await projection(_open_event("fast"), _ctx())
    await projection(_terminal_event(task_id="fast"), _ctx())
    # Archiver sees the completed event before any flush has set surfaced.
    await archiver(_terminal_event(task_id="fast"), _ctx())
    assert "fast" not in state.surfaced
    assert not _done_file(tmp_path, "demo").exists()
    assert "fast" not in state.archived

    # Let the coalesced flush fire; the projection writes the [x] line and
    # only now flags surfaced — but the archiver already (correctly) ran
    # and skipped, so no archive line is ever produced.
    await asyncio.sleep(0.1)
    assert not _done_file(tmp_path, "demo").exists()
