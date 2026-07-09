"""Round-trip property tests for the shared task-line grammar (ARCH-5).

This module replaces two crutches that existed only because the grammar
was encoded in three places:

* ``test_priority_emoji_table_matches_projection_table`` (was in
  ``test_obsidian_fs_watcher.py``) — pinned the watcher's
  ``EMOJI_TO_PRIORITY`` against the projection's ``PRIORITY_EMOJI``. The
  inverse is now *derived*, so drift is structurally impossible; the
  bijection round-trip below pins the one remaining table's values.
* the ``PRIORITY_EMOJI_MAP`` value-set assertion (was in
  ``test_task_line_parser.py``) and the ``_TASK_ID_RE`` charset tests
  (were in ``test_obsidian_fs_watcher.py``) — the grammar they exercised
  now lives here.

The headline test is :func:`test_render_line_round_trips_through_the_grammar`:
render a real line with the projection writer, parse it back with the
grammar's atoms, and assert the fields survive. That is strictly stronger
than the old table-agreement test — it proves the writer's output is
parseable by the reader's grammar, not merely that two tables share keys.
"""

from __future__ import annotations

import re
from datetime import date

from lithos_loom.lithos_client import Task
from lithos_loom.render import render_line
from lithos_loom.task_line import (
    EMOJI_TO_PRIORITY,
    PRIORITY_EMOJI,
    TASK_ID_RE,
    extract_task_ids,
    parse_due_date,
    parse_priority,
    render_task_id,
)

_ENUMS = ("highest", "high", "medium", "low", "lowest")
_EMOJI = ("🔺", "⏫", "🔼", "🔽", "⏬")


# ── priority bijection ─────────────────────────────────────────────────


def test_priority_emoji_pins_the_enum_and_emoji_sets() -> None:
    """The one hand-written table's keys and values are the canonical
    enum and Tasks-plugin emoji sets — a typo in either is caught here."""
    assert set(PRIORITY_EMOJI.keys()) == set(_ENUMS)
    assert set(PRIORITY_EMOJI.values()) == set(_EMOJI)


def test_priority_tables_round_trip_both_directions() -> None:
    """``EMOJI_TO_PRIORITY`` is the exact inverse of ``PRIORITY_EMOJI``.

    This is the old anti-drift test's intent, but the inverse is now
    derived rather than hand-maintained, so the two can never disagree."""
    for enum, emoji in PRIORITY_EMOJI.items():
        assert EMOJI_TO_PRIORITY[emoji] == enum
    for emoji, enum in EMOJI_TO_PRIORITY.items():
        assert PRIORITY_EMOJI[enum] == emoji


def test_emoji_to_priority_preserves_highest_to_lowest_order() -> None:
    """Iteration order is load-bearing: the importer iterates
    ``EMOJI_TO_PRIORITY.items()`` and takes the first hit as the winning
    priority, so highest must come first."""
    assert list(EMOJI_TO_PRIORITY.values()) == list(_ENUMS)
    assert list(EMOJI_TO_PRIORITY.keys()) == list(_EMOJI)


# ── id marker: render / parse inverses ─────────────────────────────────


def test_render_task_id_round_trips_through_extract() -> None:
    for task_id in ("abc", "abc-123_XYZ", "a", "0"):
        rendered = render_task_id(task_id)
        assert rendered == f"🆔 lithos:{task_id}"
        assert extract_task_ids(f"- [ ] Title {rendered} #project/foo") == {task_id}


def test_extract_task_ids_finds_every_marker_and_ignores_prose() -> None:
    text = (
        "%% header %%\n"
        "- [ ] One 🆔 lithos:aaa\n"
        "free text, no marker\n"
        "- [x] Two 🆔 lithos:bbb ✅ 2026-01-01\n"
    )
    assert extract_task_ids(text) == {"aaa", "bbb"}
    assert extract_task_ids("no markers here") == set()


def test_task_id_regex_charset_and_boundaries() -> None:
    """Lithos ids are ``[A-Za-z0-9_-]+`` — the match stops at a '.' and
    an empty id does not match (moved from test_obsidian_fs_watcher)."""
    m = TASK_ID_RE.search("- [ ] Title 🆔 lithos:abc-123_XYZ #project/foo")
    assert m is not None and m.group("task_id") == "abc-123_XYZ"

    stops = TASK_ID_RE.search("🆔 lithos:abc.def")
    assert stops is not None and stops.group("task_id") == "abc"  # stops at .

    assert TASK_ID_RE.search("🆔 lithos:") is None  # empty id
    assert isinstance(TASK_ID_RE, re.Pattern)


# ── priority marker: parse ─────────────────────────────────────────────


def test_parse_priority_maps_each_emoji_to_its_enum() -> None:
    for enum, emoji in PRIORITY_EMOJI.items():
        assert parse_priority(f"#project/foo {emoji} 📅 2026-01-01") == enum


def test_parse_priority_returns_none_without_a_marker() -> None:
    assert parse_priority("#project/foo 📅 2026-01-01") is None
    assert parse_priority("") is None


def test_parse_priority_takes_the_first_emoji_positionally() -> None:
    """The reader's positional first-match: valid because the projection
    emits at most one priority emoji per line."""
    assert parse_priority("⏫ then 🔺") == "high"


# ── due-date marker: parse ─────────────────────────────────────────────


def test_parse_due_date_reads_the_canonical_form() -> None:
    assert parse_due_date("#lithos/route 📅 2026-06-15") == "2026-06-15"


def test_parse_due_date_rejects_non_canonical_forms() -> None:
    assert parse_due_date("📅 next Friday") is None
    assert parse_due_date("📅 2026-06-15T09:00Z") == "2026-06-15"  # date prefix only
    assert parse_due_date("no marker") is None


# ── writer ↔ grammar round-trip (the headline) ─────────────────────────


def _task(task_id: str, metadata: dict[str, object]) -> Task:
    return Task(
        id=task_id,
        title="Review PR",
        status="open",
        tags=(),
        metadata=metadata,
        claims=(),
    )


def test_render_line_round_trips_through_the_grammar() -> None:
    """Render a real projected line, then recover its id / priority / due
    date with the grammar's parse atoms — the projection's output must be
    parseable by the reader's grammar."""
    task = _task(
        "task-42_A",
        {"priority": "high", "project": "loom", "scheduled_for": "2026-06-15"},
    )
    line = render_line(task, routes=(), today=date(2026, 5, 22))

    ids = extract_task_ids(line)
    assert ids == {"task-42_A"}
    (task_id,) = ids
    zone = line[TASK_ID_RE.search(line).end() :]  # type: ignore[union-attr]
    assert parse_priority(zone) == "high"
    assert parse_due_date(zone) == "2026-06-15"


def test_render_line_without_optional_markers_parses_to_none() -> None:
    task = _task("bare", {})
    line = render_line(task, routes=(), today=date(2026, 5, 22))
    zone = line[TASK_ID_RE.search(line).end() :]  # type: ignore[union-attr]
    assert parse_priority(zone) is None
    assert parse_due_date(zone) is None
