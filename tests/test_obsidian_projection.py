"""Tests for the obsidian-projection handler (Slice 1 US8).

Drives the handler directly with synthetic Events against a
tmp_path-based vault. Idempotency, atomic write, and rendering rules
are exercised here; end-to-end wiring through the obsidian-sync child
is covered in ``test_obsidian_sync_child.py``.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from lithos_loom.bus import Event
from lithos_loom.config import (
    LoomConfig,
    ObsidianSyncConfig,
    OrchestratorConfig,
    RouteConfig,
    RouteMatch,
)
from lithos_loom.subscriptions._obsidian_projection import make_handler

# ── Fixtures ───────────────────────────────────────────────────────────


@dataclass
class _StubCtx:
    """Mimics the bits of SubscriptionContext the handler reads."""

    logger: logging.Logger


def _ctx() -> _StubCtx:
    return _StubCtx(logger=logging.getLogger("test.obsidian_projection"))


def _cfg(
    tmp_path: Path,
    *,
    routes: tuple[RouteConfig, ...] = (),
    include_blocked: bool = True,
    exclude_tags: tuple[str, ...] = (),
    tasks_file: Path = Path("_lithos/tasks.md"),
) -> LoomConfig:
    return LoomConfig(
        orchestrator=OrchestratorConfig(
            agent_id="lithos-orchestrator-test",
            lithos_url="http://localhost:8765",
        ),
        routes=routes,
        obsidian_sync=ObsidianSyncConfig(
            vault_path=tmp_path,
            tasks_file=tasks_file,
            include_blocked=include_blocked,
            exclude_tags=exclude_tags,
        ),
    )


def _event(
    event_type: str,
    *,
    task_id: str,
    title: str = "test task",
    status: str = "open",
    tags: tuple[str, ...] = (),
    metadata: Mapping[str, Any] | None = None,
    claims: tuple[Mapping[str, Any], ...] = (),
) -> Event:
    return Event(
        type=event_type,
        timestamp=datetime.now(UTC),
        payload={
            "id": task_id,
            "title": title,
            "status": status,
            "tags": list(tags),
            "metadata": dict(metadata or {}),
            "claims": list(claims),
        },
    )


# ── Handler behaviour ──────────────────────────────────────────────────


async def test_created_event_for_actionable_task_writes_line(tmp_path: Path) -> None:
    """Orphan task (no matching route) is human-actionable → line appears."""
    cfg = _cfg(tmp_path)
    handler = make_handler(cfg)
    await handler(
        _event("lithos.task.created", task_id="abc", title="Review PR"), _ctx()
    )

    content = (tmp_path / "_lithos/tasks.md").read_text()
    assert "- [ ] Review PR 🆔 lithos:abc" in content


async def test_created_event_for_autonomous_task_writes_nothing(
    tmp_path: Path,
) -> None:
    """Autonomous-route task (human_blocking=False) doesn't actionably change
    the projection. We skip the write entirely rather than rewriting the
    file with the same content — the file only appears once an actionable
    task is seen. Keeps the operator's first signal clean ("a file
    appeared = there's something to do")."""
    routes = (
        RouteConfig(
            name="auto",
            command="echo",
            match=RouteMatch(tags=("trigger:auto",)),
            human_blocking=False,
        ),
    )
    cfg = _cfg(tmp_path, routes=routes)
    handler = make_handler(cfg)
    await handler(
        _event("lithos.task.created", task_id="autonomous", tags=("trigger:auto",)),
        _ctx(),
    )

    # No actionable state change → no file written yet.
    assert not (tmp_path / "_lithos/tasks.md").exists()


async def test_updated_event_replaces_line_for_same_id(tmp_path: Path) -> None:
    """A title change on the same task replaces (not duplicates) the line.

    Note: the live LithosEventStream source doesn't actually emit
    lithos.task.updated today (verified at lithos_event_stream.py:63);
    the handler subscribes to it for forward-compat. The real runtime
    re-evaluation path is exercised by the claimed/released tests below.
    """
    cfg = _cfg(tmp_path)
    handler = make_handler(cfg)
    await handler(
        _event("lithos.task.created", task_id="t1", title="old title"), _ctx()
    )
    await handler(
        _event("lithos.task.updated", task_id="t1", title="new title"), _ctx()
    )

    content = (tmp_path / "_lithos/tasks.md").read_text()
    assert "new title" in content
    assert "old title" not in content
    assert content.count("🆔 lithos:t1") == 1


async def test_claimed_by_autonomous_route_drops_orphan_line(tmp_path: Path) -> None:
    """If a task that was a projected orphan gets claimed by an
    autonomous route, drop its line — automation now owns it."""
    routes = (
        RouteConfig(
            name="auto",
            command="echo",
            match=RouteMatch(tags=("trigger:auto",)),
            human_blocking=False,
        ),
    )
    cfg = _cfg(tmp_path, routes=routes)
    handler = make_handler(cfg)
    # Initially orphan (actionable) → line appears.
    await handler(_event("lithos.task.created", task_id="t1", title="x"), _ctx())
    assert "🆔 lithos:t1" in (tmp_path / "_lithos/tasks.md").read_text()
    # Now claimed by the autonomous route → line removed.
    await handler(
        _event(
            "lithos.task.claimed",
            task_id="t1",
            title="x",
            tags=("trigger:auto",),
            claims=({"agent": "automation", "aspect": "auto"},),
        ),
        _ctx(),
    )
    assert "🆔 lithos:t1" not in (tmp_path / "_lithos/tasks.md").read_text()


async def test_claimed_by_human_blocking_route_promotes_task(tmp_path: Path) -> None:
    """D6's second disjunct in action: a task that was claimable-but-
    hidden (autonomous-route-claimable) becomes actionable the moment a
    human_blocking route claims it. This is the real runtime path the
    projection needs to react to — without it, US8 would never surface
    story-review-human tasks until they hit a created/updated event."""
    routes = (
        RouteConfig(
            name="review-human",
            command="echo",
            match=RouteMatch(tags=("trigger:review",)),
            human_blocking=True,
        ),
    )
    cfg = _cfg(tmp_path, routes=routes)
    handler = make_handler(cfg)
    # Created: tag matches human_blocking route but no claim yet → hidden.
    await handler(
        _event(
            "lithos.task.created",
            task_id="rev",
            title="Review PR #42",
            tags=("trigger:review",),
        ),
        _ctx(),
    )
    assert not (tmp_path / "_lithos/tasks.md").exists()

    # Claimed by review-human → promote.
    await handler(
        _event(
            "lithos.task.claimed",
            task_id="rev",
            title="Review PR #42",
            tags=("trigger:review",),
            claims=({"agent": "loom", "aspect": "review-human"},),
        ),
        _ctx(),
    )
    assert (
        "- [ ] Review PR #42 🆔 lithos:rev"
        in (tmp_path / "_lithos/tasks.md").read_text()
    )


async def test_released_by_human_blocking_route_demotes_task(tmp_path: Path) -> None:
    """Inverse of the claim promotion: when the human_blocking route
    releases the claim and no other claim makes the task actionable,
    the line disappears."""
    routes = (
        RouteConfig(
            name="review-human",
            command="echo",
            match=RouteMatch(tags=("trigger:review",)),
            human_blocking=True,
        ),
    )
    cfg = _cfg(tmp_path, routes=routes)
    handler = make_handler(cfg)
    # Created + claimed → projected.
    await handler(
        _event(
            "lithos.task.created",
            task_id="rev",
            tags=("trigger:review",),
        ),
        _ctx(),
    )
    await handler(
        _event(
            "lithos.task.claimed",
            task_id="rev",
            tags=("trigger:review",),
            claims=({"agent": "loom", "aspect": "review-human"},),
        ),
        _ctx(),
    )
    assert "lithos:rev" in (tmp_path / "_lithos/tasks.md").read_text()
    # Released → claims=() → no longer actionable → drop line.
    await handler(
        _event(
            "lithos.task.released",
            task_id="rev",
            tags=("trigger:review",),
            claims=(),
        ),
        _ctx(),
    )
    assert "lithos:rev" not in (tmp_path / "_lithos/tasks.md").read_text()


async def test_completed_event_removes_line(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    handler = make_handler(cfg)
    await handler(_event("lithos.task.created", task_id="done"), _ctx())
    await handler(_event("lithos.task.completed", task_id="done"), _ctx())
    assert "🆔 lithos:done" not in (tmp_path / "_lithos/tasks.md").read_text()


async def test_cancelled_event_removes_line(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    handler = make_handler(cfg)
    await handler(_event("lithos.task.created", task_id="cx"), _ctx())
    await handler(_event("lithos.task.cancelled", task_id="cx"), _ctx())
    assert "🆔 lithos:cx" not in (tmp_path / "_lithos/tasks.md").read_text()


async def test_title_with_newlines_collapsed_to_spaces(tmp_path: Path) -> None:
    """Multi-line titles would break the single-line markdown task syntax —
    collapse whitespace so the projection stays parseable."""
    cfg = _cfg(tmp_path)
    handler = make_handler(cfg)
    await handler(
        _event("lithos.task.created", task_id="nl", title="foo\nbar\tbaz"),
        _ctx(),
    )
    content = (tmp_path / "_lithos/tasks.md").read_text()
    assert "- [ ] foo bar baz 🆔 lithos:nl" in content
    # No newline mid-title.
    lines_with_id = [ln for ln in content.splitlines() if "lithos:nl" in ln]
    assert len(lines_with_id) == 1


async def test_atomic_write_uses_temp_then_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The handler must write to a .tmp file then os.replace onto the
    final path, not write the final path directly. Verifies the
    atomicity contract that survives crashes/partial reads."""
    real_replace = os.replace
    calls: list[tuple[str, str]] = []

    def _spy_replace(src: str | Path, dst: str | Path) -> None:
        calls.append((str(src), str(dst)))
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", _spy_replace)

    cfg = _cfg(tmp_path)
    handler = make_handler(cfg)
    await handler(_event("lithos.task.created", task_id="atomic"), _ctx())

    assert len(calls) == 1
    src, dst = calls[0]
    assert src.endswith("tasks.md.tmp")
    assert dst.endswith("tasks.md")
    # Final file exists; tmp file does not linger.
    assert (tmp_path / "_lithos/tasks.md").exists()
    assert not (tmp_path / "_lithos/tasks.md.tmp").exists()


async def test_parent_directory_created_when_absent(tmp_path: Path) -> None:
    """Vault exists but the _lithos/ subdirectory does not yet — the
    handler must create it on first write."""
    assert not (tmp_path / "_lithos").exists()
    cfg = _cfg(tmp_path)
    handler = make_handler(cfg)
    await handler(_event("lithos.task.created", task_id="first"), _ctx())
    assert (tmp_path / "_lithos").is_dir()
    assert (tmp_path / "_lithos/tasks.md").is_file()


async def test_multiple_tasks_sorted_by_id_deterministic(tmp_path: Path) -> None:
    """Three tasks added in arbitrary order render in id-sorted order so
    file content is stable across runs (helpful for US14 dedup)."""
    cfg = _cfg(tmp_path)
    handler = make_handler(cfg)
    await handler(_event("lithos.task.created", task_id="c", title="C"), _ctx())
    await handler(_event("lithos.task.created", task_id="a", title="A"), _ctx())
    await handler(_event("lithos.task.created", task_id="b", title="B"), _ctx())

    content = (tmp_path / "_lithos/tasks.md").read_text()
    task_lines = [ln for ln in content.splitlines() if ln.startswith("- [ ]")]
    assert task_lines == [
        "- [ ] A 🆔 lithos:a",
        "- [ ] B 🆔 lithos:b",
        "- [ ] C 🆔 lithos:c",
    ]


async def test_file_includes_auto_generated_header(tmp_path: Path) -> None:
    """First line of the file is a clear hand-off warning so a curious
    operator who opens the file sees it's machine-managed."""
    cfg = _cfg(tmp_path)
    handler = make_handler(cfg)
    await handler(_event("lithos.task.created", task_id="t"), _ctx())
    content = (tmp_path / "_lithos/tasks.md").read_text()
    first_line = content.splitlines()[0]
    assert first_line.startswith("%%")
    assert "Auto-generated" in first_line


async def test_idempotent_repeated_event_yields_same_file(tmp_path: Path) -> None:
    """Replaying the same created event twice produces identical file
    content — necessary because the SSE source's bootstrap replays
    created events on every daemon restart."""
    cfg = _cfg(tmp_path)
    handler = make_handler(cfg)
    await handler(_event("lithos.task.created", task_id="r"), _ctx())
    first = (tmp_path / "_lithos/tasks.md").read_text()
    await handler(_event("lithos.task.created", task_id="r"), _ctx())
    second = (tmp_path / "_lithos/tasks.md").read_text()
    assert first == second


# ── Copilot review fixes (#17) ─────────────────────────────────────────


async def test_removal_of_untracked_task_skips_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Removing a task that was never in state is a no-op — no disk I/O.
    Without this, every spurious completed/cancelled event for an
    autonomous task would touch the file's mtime and ripple through
    Obsidian Sync."""
    calls: list[tuple[str, str]] = []
    real_replace = os.replace

    def _spy(src: str | Path, dst: str | Path) -> None:
        calls.append((str(src), str(dst)))
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", _spy)
    cfg = _cfg(tmp_path)
    handler = make_handler(cfg)
    # Never created — straight to completed.
    await handler(_event("lithos.task.completed", task_id="ghost"), _ctx())
    assert calls == [], "completed-for-untracked should not have written"
    assert not (tmp_path / "_lithos/tasks.md").exists()


async def test_upsert_with_identical_line_skips_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Replaying a created event for a task already in state yields the
    same rendered line — skip the write (Copilot review on #17). Also
    sets up US14's content-hash dedup work for later."""
    cfg = _cfg(tmp_path)
    handler = make_handler(cfg)
    await handler(_event("lithos.task.created", task_id="dup"), _ctx())

    calls: list[tuple[str, str]] = []
    real_replace = os.replace

    def _spy(src: str | Path, dst: str | Path) -> None:
        calls.append((str(src), str(dst)))
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", _spy)
    # Same event again — identical line, should not write.
    await handler(_event("lithos.task.created", task_id="dup"), _ctx())
    assert calls == [], "second identical event triggered a write"


async def test_unknown_event_type_no_op(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """An event type outside the known sets is debug-logged and dropped
    — payload parsing is NOT attempted (Copilot review on #17 flagged
    that a foreign payload could raise KeyError on 'id' otherwise)."""
    import logging as _logging

    cfg = _cfg(tmp_path)
    handler = make_handler(cfg)
    # Construct a foreign event with a payload that lacks 'id' — proof
    # the handler doesn't try to parse it.
    foreign = Event(
        type="obsidian.note.modified",
        timestamp=datetime.now(UTC),
        payload={"path": "/some/note.md"},
    )
    with caplog.at_level(_logging.DEBUG):
        await handler(foreign, _ctx())
    # No file written; no exception raised.
    assert not (tmp_path / "_lithos/tasks.md").exists()


async def test_malformed_task_payload_warns_no_crash(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A task-typed event whose payload is missing 'id' (programming
    error somewhere upstream) should warn and drop, not crash the
    subscription loop."""
    import logging as _logging

    cfg = _cfg(tmp_path)
    handler = make_handler(cfg)
    bad = Event(
        type="lithos.task.created",
        timestamp=datetime.now(UTC),
        payload={"title": "no id here"},
    )
    with caplog.at_level(_logging.WARNING):
        await handler(bad, _ctx())  # must not raise

    warns = [r.getMessage() for r in caplog.records if r.levelno == _logging.WARNING]
    assert any("malformed payload" in m for m in warns), warns
    assert not (tmp_path / "_lithos/tasks.md").exists()


async def test_atomic_write_cleans_up_tmp_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If os.replace raises (disk full, perms flip, etc.) the .tmp file
    must NOT be left behind to litter the vault (Copilot review on #17,
    mirrors plugin_runner.write_result_atomically)."""

    def _failing_replace(src: str | Path, dst: str | Path) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr(os, "replace", _failing_replace)

    cfg = _cfg(tmp_path)
    handler = make_handler(cfg)
    with pytest.raises(OSError, match="simulated replace failure"):
        await handler(_event("lithos.task.created", task_id="x"), _ctx())

    # The real file was never written, and the tmp file was cleaned up.
    assert not (tmp_path / "_lithos/tasks.md").exists()
    assert not (tmp_path / "_lithos/tasks.md.tmp").exists()
