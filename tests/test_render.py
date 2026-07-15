"""Tests for ``lithos_loom.render`` (Slice 3 extraction).

The projection's existing tests already exercise
:func:`render_line` and :func:`render_resolved_line` end-to-end via
``_render_file``; this file targets the pure-function surface
directly so the capture-macro CLI's "born projected" guarantee
(US25) has a focused regression suite.

Most of these are pinning tests — verify the line shape the macro
inserts is byte-equal to what the projection writes for the same
task.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

import pytest

from lithos_loom.config import RouteConfig, RouteMatch
from lithos_loom.lithos_client import Blocker, Task
from lithos_loom.render import (
    dep_markers,
    due_date_str,
    parse_scheduled_for,
    priority_marker,
    render_line,
    render_resolved_line,
    validated_priority,
)

# ── Helpers ────────────────────────────────────────────────────────────


def _task(
    *,
    task_id: str = "abc",
    title: str = "Review PR",
    status: str = "open",
    tags: tuple[str, ...] = (),
    metadata: dict[str, Any] | None = None,
) -> Task:
    return Task(
        id=task_id,
        title=title,
        status=status,
        tags=tags,
        metadata=metadata or {},
        claims=(),
    )


_TODAY = date(2026, 5, 22)


# ── render_line ────────────────────────────────────────────────────────


def test_render_line_minimum_shape() -> None:
    """An open task with only id+title renders the bare line."""
    line = render_line(_task(), routes=(), today=_TODAY)
    assert line == "- [ ] Review PR 🆔 lithos:abc"


def test_render_line_with_priority_and_project() -> None:
    """Priority emoji + project tag in canonical positions."""
    task = _task(metadata={"priority": "high", "project": "lithos-loom"})
    line = render_line(task, routes=(), today=_TODAY)
    # Priority emoji is at the END for Tasks-plugin sort recognition;
    # mid-line emoji is treated as part of the description by the
    # plugin's parser and silently ignored for sort.
    assert line == "- [ ] Review PR 🆔 lithos:abc #project/lithos-loom ⏫"


def test_render_line_with_blockers() -> None:
    """Each Lithos blocker renders a ``⛔ lithos:<id>`` marker (US8)."""
    blockers = (
        Blocker(kind="task", message="", task_id="dep1", status="open"),
        Blocker(kind="task", message="", task_id="dep2", status="open"),
    )
    line = render_line(_task(), routes=(), today=_TODAY, blockers=blockers)
    assert "⛔ lithos:dep1" in line
    assert "⛔ lithos:dep2" in line


def test_render_line_omits_blocker_markers_when_caller_has_no_sweep() -> None:
    """The capture CLI renders a just-created task with no blocked-set in
    hand; it must not resurrect the old metadata.depends_on markers."""
    task = _task(metadata={"depends_on": ["dep1"]})
    assert "⛔" not in render_line(task, routes=(), today=_TODAY)


def test_render_line_with_scheduled_for() -> None:
    """``metadata.scheduled_for`` becomes the ``📅`` marker."""
    task = _task(metadata={"scheduled_for": "2026-06-15"})
    line = render_line(task, routes=(), today=_TODAY)
    assert "📅 2026-06-15" in line


def test_render_line_title_collapses_whitespace() -> None:
    """Embedded newlines / runs of whitespace in the title get
    collapsed to single spaces so the markdown line stays single-line."""
    task = _task(title="Multi\nline\ttitle  with  runs")
    line = render_line(task, routes=(), today=_TODAY)
    assert "Multi line title with runs" in line
    assert "\n" not in line


@pytest.mark.parametrize(
    ("enum_value", "expected_emoji"),
    [
        ("highest", "🔺"),
        ("high", "⏫"),
        ("medium", "🔼"),
        ("low", "🔽"),
        ("lowest", "⏬"),
    ],
)
def test_render_line_all_priority_emoji(enum_value: str, expected_emoji: str) -> None:
    """Every D18 enum value renders its canonical emoji."""
    task = _task(metadata={"priority": enum_value})
    line = render_line(task, routes=(), today=_TODAY)
    assert expected_emoji in line


# ── render_resolved_line ───────────────────────────────────────────────


def test_render_resolved_line_completed() -> None:
    """Completed task renders ``[x] ... ✅ <date> 🆔 lithos:<id>``."""
    task = _task(status="completed")
    line = render_resolved_line(task, status="completed", resolved_at=_TODAY)
    # Done date at the END (Tasks-plugin convention) — see render.py docstring.
    assert line == "- [x] Review PR 🆔 lithos:abc ✅ 2026-05-22"


def test_render_resolved_line_cancelled() -> None:
    """Cancelled task renders ``[-] ... ❌ <date> 🆔 lithos:<id>``."""
    task = _task(status="cancelled")
    line = render_resolved_line(task, status="cancelled", resolved_at=_TODAY)
    # Cancelled date at the END (Tasks-plugin convention).
    assert line == "- [-] Review PR 🆔 lithos:abc ❌ 2026-05-22"


def test_render_resolved_line_keeps_project_tag() -> None:
    """Resolved lines drop priority/dep/due markers but keep
    ``#project/<slug>`` so "done-this-week-for-X" queries still
    cluster correctly."""
    task = _task(
        status="completed",
        metadata={
            "project": "lithos-loom",
            "priority": "high",  # must NOT appear
            "depends_on": ["x"],  # must NOT appear
        },
    )
    line = render_resolved_line(task, status="completed", resolved_at=_TODAY)
    assert "#project/lithos-loom" in line
    assert "⏫" not in line
    assert "⛔" not in line


# ── priority_marker ────────────────────────────────────────────────────


def test_priority_marker_returns_none_when_absent() -> None:
    assert priority_marker(_task()) is None


def test_priority_marker_returns_none_for_unknown_enum(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Unknown enum value → ``None`` + a single warn so the user has
    a breadcrumb but the projection doesn't crash."""
    task = _task(metadata={"priority": "urgent"})  # not in PRIORITY_EMOJI
    with caplog.at_level(logging.WARNING, logger="lithos_loom.render"):
        assert priority_marker(task) is None
    assert any("unknown metadata.priority" in r.getMessage() for r in caplog.records)


def test_priority_marker_returns_none_for_non_string(
    caplog: pytest.LogCaptureFixture,
) -> None:
    task = _task(metadata={"priority": 5})  # type: ignore[dict-item]
    with caplog.at_level(logging.WARNING, logger="lithos_loom.render"):
        assert priority_marker(task) is None
    assert any("non-string metadata.priority" in r.getMessage() for r in caplog.records)


# ── validated_priority ─────────────────────────────────────────────────


def test_validated_priority_returns_enum_string_for_known_value() -> None:
    """Returns the enum string, not the emoji, so callers can store
    the canonical D18 value (used by the projection's _StateEntry)."""
    assert validated_priority(_task(metadata={"priority": "high"})) == "high"


def test_validated_priority_returns_none_silently_for_unknown(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Unknown / non-string values return ``None`` WITHOUT warning —
    priority_marker already warns on the same code path, so duplicating
    would be noisy."""
    with caplog.at_level(logging.WARNING, logger="lithos_loom.render"):
        assert validated_priority(_task(metadata={"priority": "urgent"})) is None
        assert validated_priority(_task(metadata={"priority": 5})) is None  # type: ignore[dict-item]
    assert caplog.records == []


# ── dep_markers ────────────────────────────────────────────────────────


def _blocker(task_id: str, kind: str = "task") -> Blocker:
    return Blocker(
        kind=kind,
        message=f"waiting on predecessor {task_id}",
        task_id=task_id,
        type="blocks",
        status="open",
    )


def test_dep_markers_empty_when_not_blocked() -> None:
    assert dep_markers(_task()) == []


def test_dep_markers_ignores_metadata_depends_on() -> None:
    """US8: the marker reflects Lithos's CURRENT blockers, not the static
    list the task declared — that list stayed true even after a dep finished."""
    task = _task(metadata={"depends_on": ["stale-dep"]})
    assert dep_markers(task) == []


def test_dep_markers_renders_each_blocker() -> None:
    task = _task()
    blockers = (_blocker("a"), _blocker("b"), _blocker("c"))
    assert dep_markers(task, blockers) == [
        "⛔ lithos:a",
        "⛔ lithos:b",
        "⛔ lithos:c",
    ]


def test_dep_markers_renders_gate_blockers() -> None:
    """A gate blocker names a real task id, so it earns a marker — one of the
    kinds the old metadata mirror could not see at all."""
    assert dep_markers(_task(), (_blocker("gate-1", kind="gate"),)) == [
        "⛔ lithos:gate-1"
    ]


def test_dep_markers_dedups_duplicates() -> None:
    """First occurrence wins; the marker is a signal, not a count."""
    task = _task()
    assert dep_markers(task, (_blocker("a"), _blocker("a"), _blocker("b"))) == [
        "⛔ lithos:a",
        "⛔ lithos:b",
    ]


def test_dep_markers_skips_self_referencing_cycle_blocker() -> None:
    """A `cycle` blocker names the task itself; ⛔ points at another line's 🆔,
    so a self-reference would be nonsense."""
    task = _task()
    cycle = Blocker(kind="cycle", message="dependency cycle", task_id=task.id)
    assert dep_markers(task, (cycle,)) == []


def test_dep_markers_skips_blocker_without_a_task_id() -> None:
    assert dep_markers(_task(), (Blocker(kind="cycle", message="cycle"),)) == []


# ── due_date_str / parse_scheduled_for ─────────────────────────────────


def test_due_date_str_returns_none_for_orphan_task() -> None:
    """Orphan (no scheduled_for, no human-blocking route) → no
    ``📅`` marker; user's Inbox query picks it up naturally."""
    assert due_date_str(_task(), routes=(), today=_TODAY) is None


def test_due_date_str_uses_scheduled_for_override() -> None:
    """``metadata.scheduled_for`` always wins."""
    task = _task(metadata={"scheduled_for": "2026-06-15"})
    assert due_date_str(task, routes=(), today=_TODAY) == "2026-06-15"


def test_due_date_str_falls_back_to_today_for_human_blocking_route() -> None:
    """A task claimed by a human_blocking route surfaces with
    ``📅 today`` so it lifts to the operator's daily view. The
    "claim" lives in ``task.claims`` (not tags); the route's name
    matches the claim's ``aspect`` per the routing convention."""
    route = RouteConfig(
        name="review-human",
        command="noop",
        match=RouteMatch(tags=("review",)),
        human_blocking=True,
    )
    task = Task(
        id="abc",
        title="Review PR",
        status="open",
        tags=("review",),
        metadata={},
        claims=({"agent": "lithos-orchestrator-test", "aspect": "review-human"},),
    )
    assert due_date_str(task, routes=(route,), today=_TODAY) == "2026-05-22"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-06-15", date(2026, 6, 15)),
        ("2026-06-15T09:00:00", date(2026, 6, 15)),
        ("2026-06-15T09:00:00+00:00", date(2026, 6, 15)),
    ],
)
def test_parse_scheduled_for_accepts_iso_forms(value: str, expected: date) -> None:
    assert parse_scheduled_for(value) == expected


def test_parse_scheduled_for_returns_none_for_malformed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Malformed values → ``None`` + warn; projection must never crash
    on bad metadata."""
    with caplog.at_level(logging.WARNING, logger="lithos_loom.render"):
        assert parse_scheduled_for("not-a-date") is None
    assert any(
        "malformed metadata.scheduled_for" in r.getMessage() for r in caplog.records
    )


@pytest.mark.parametrize("bad", [None, "", 42, [], {}])
def test_parse_scheduled_for_returns_none_for_non_string(bad: Any) -> None:
    """Non-string / empty inputs are silently absent (not malformed)."""
    assert parse_scheduled_for(bad) is None


# The ``PRIORITY_EMOJI`` table now lives in :mod:`lithos_loom.task_line`
# (the shared grammar); its enum/emoji sets are pinned by
# ``tests/test_task_line.py`` rather than here.
