"""Tests for ``lithos_loom.sources.obsidian_fs_watcher`` (Slice 2 US16 + US23).

Drives ``poll_once()`` directly instead of running the polling loop —
gives deterministic ordering between projection writes, file edits,
and watcher polls without any timing flakiness.

Each test wires a real :class:`EventBus`, subscribes to
``obsidian.task.status_changed``, and asserts on the queue.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from lithos_loom.bus import Event, EventBus, Subscription
from lithos_loom.sources.obsidian_fs_watcher import ObsidianFsWatcher
from lithos_loom.sync_state import ProjectionSyncState

# ── Helpers ────────────────────────────────────────────────────────────


def _subscribe(bus: EventBus) -> Subscription:
    return bus.subscribe(
        event_types=("obsidian.task.status_changed",),
        name="test-subscriber",
    )


def _drain(sub: Subscription) -> list[Event]:
    out: list[Event] = []
    while True:
        try:
            out.append(sub.queue.get_nowait())
        except asyncio.QueueEmpty:
            break
    return out


def _write_tasks_file(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(["%% header %%", "", *lines, ""])
    path.write_text(text, encoding="utf-8")


def _hash_of(path: Path) -> bytes:
    import hashlib

    return hashlib.sha256(path.read_bytes()).digest()


def _record_projection(
    sync_state: ProjectionSyncState,
    path: Path,
    markers: dict[str, str],
    priorities: dict[str, str | None] | None = None,
    due_dates: dict[str, str | None] | None = None,
) -> None:
    """Stand in for the projection's ``_flush`` call.

    Tests mutate the file directly to simulate either user edits or
    projection writes; this helper records the post-write state into
    ``sync_state`` so the watcher's US23 suppression has something
    to compare against, exactly matching what ``_flush`` does.

    ``priorities`` (Slice 2 US21) maps each task id to its priority
    enum (or ``None``). ``due_dates`` (Slice 3 round-trip) maps each
    task id to its rendered ``YYYY-MM-DD`` string (or ``None``).
    Both default to all-None for the given markers so existing tests
    don't need to care about the new fields.
    """
    if priorities is None:
        priorities = dict.fromkeys(markers, None)
    if due_dates is None:
        due_dates = dict.fromkeys(markers, None)
    sync_state.record_projection_write(
        content_hash=_hash_of(path),
        task_status_markers=markers,
        task_priority_markers=priorities,
        task_due_date_markers=due_dates,
    )


@pytest.fixture
def tasks_path(tmp_path: Path) -> Path:
    return tmp_path / "vault" / "_lithos" / "tasks.md"


@pytest.fixture
def bus() -> EventBus:
    return EventBus()


@pytest.fixture
def sub(bus: EventBus) -> Subscription:
    return _subscribe(bus)


# ── US16: detect user-driven status flips ──────────────────────────────


async def test_poll_emits_status_changed_when_user_ticks_open_task(
    bus: EventBus,
    sub: Subscription,
    tasks_path: Path,
) -> None:
    """User flips ``[ ]`` → ``[x]`` on a projection-known task → watcher emits."""
    sync_state = ProjectionSyncState()
    _write_tasks_file(tasks_path, ["- [ ] Review PR 🆔 lithos:abc"])
    _record_projection(sync_state, tasks_path, {"abc": "[ ]"})

    watcher = ObsidianFsWatcher(bus=bus, tasks_path=tasks_path, sync_state=sync_state)

    # Simulate the user editing the file.
    _write_tasks_file(tasks_path, ["- [x] Review PR 🆔 lithos:abc"])

    published = await watcher.poll_once()
    events = _drain(sub)
    assert published == 1
    assert len(events) == 1
    assert events[0].type == "obsidian.task.status_changed"
    assert events[0].payload == {"task_id": "abc", "prior": "[ ]", "new": "[x]"}


async def test_poll_emits_status_changed_for_cancel(
    bus: EventBus,
    sub: Subscription,
    tasks_path: Path,
) -> None:
    """User flips ``[ ]`` → ``[-]`` → watcher emits (powers US18)."""
    sync_state = ProjectionSyncState()
    _write_tasks_file(tasks_path, ["- [ ] Drop the old README 🆔 lithos:xyz"])
    _record_projection(sync_state, tasks_path, {"xyz": "[ ]"})

    watcher = ObsidianFsWatcher(bus=bus, tasks_path=tasks_path, sync_state=sync_state)
    _write_tasks_file(tasks_path, ["- [-] Drop the old README 🆔 lithos:xyz"])

    await watcher.poll_once()
    events = _drain(sub)
    assert len(events) == 1
    assert events[0].payload == {"task_id": "xyz", "prior": "[ ]", "new": "[-]"}


async def test_poll_emits_for_untick(
    bus: EventBus,
    sub: Subscription,
    tasks_path: Path,
) -> None:
    """User flips ``[x]`` → ``[ ]`` → watcher emits (powers US19 reopen)."""
    sync_state = ProjectionSyncState()
    _write_tasks_file(tasks_path, ["- [x] Done thing 🆔 lithos:done1"])
    _record_projection(sync_state, tasks_path, {"done1": "[x]"})

    watcher = ObsidianFsWatcher(bus=bus, tasks_path=tasks_path, sync_state=sync_state)
    _write_tasks_file(tasks_path, ["- [ ] Done thing 🆔 lithos:done1"])

    await watcher.poll_once()
    events = _drain(sub)
    assert events[0].payload == {"task_id": "done1", "prior": "[x]", "new": "[ ]"}


async def test_poll_emits_in_progress_and_rescheduled_markers(
    bus: EventBus,
    sub: Subscription,
    tasks_path: Path,
) -> None:
    """``[/]`` and ``[>]`` flips also emit (US20 handler will no-op them)."""
    sync_state = ProjectionSyncState()
    _write_tasks_file(tasks_path, ["- [ ] Task 🆔 lithos:a"])
    _record_projection(sync_state, tasks_path, {"a": "[ ]"})

    watcher = ObsidianFsWatcher(bus=bus, tasks_path=tasks_path, sync_state=sync_state)
    _write_tasks_file(tasks_path, ["- [/] Task 🆔 lithos:a"])

    await watcher.poll_once()
    events = _drain(sub)
    assert len(events) == 1
    assert events[0].payload["new"] == "[/]"

    # Now flip to rescheduled.
    sync_state.task_status_markers["a"] = "[/]"
    sync_state.last_written_hash = _hash_of(tasks_path)
    _write_tasks_file(tasks_path, ["- [>] Task 🆔 lithos:a"])
    await watcher.poll_once()
    events = _drain(sub)
    assert len(events) == 1
    assert events[0].payload == {"task_id": "a", "prior": "[/]", "new": "[>]"}


async def test_poll_emits_one_event_per_changed_task(
    bus: EventBus,
    sub: Subscription,
    tasks_path: Path,
) -> None:
    """Multiple tasks flipping in one edit batch → one event per task."""
    sync_state = ProjectionSyncState()
    _write_tasks_file(
        tasks_path,
        [
            "- [ ] Alpha 🆔 lithos:a",
            "- [ ] Beta 🆔 lithos:b",
            "- [ ] Gamma 🆔 lithos:c",
        ],
    )
    _record_projection(sync_state, tasks_path, {"a": "[ ]", "b": "[ ]", "c": "[ ]"})

    watcher = ObsidianFsWatcher(bus=bus, tasks_path=tasks_path, sync_state=sync_state)
    _write_tasks_file(
        tasks_path,
        [
            "- [x] Alpha 🆔 lithos:a",
            "- [-] Beta 🆔 lithos:b",
            "- [ ] Gamma 🆔 lithos:c",  # unchanged
        ],
    )

    published = await watcher.poll_once()
    events = _drain(sub)
    assert published == 2
    by_task = {e.payload["task_id"]: e.payload for e in events}
    assert by_task["a"]["new"] == "[x]"
    assert by_task["b"]["new"] == "[-]"
    assert "c" not in by_task  # unchanged → no event


# ── US21: priority emoji changes ───────────────────────────────────────


def _subscribe_priority(bus: EventBus) -> Subscription:
    return bus.subscribe(
        event_types=("obsidian.task.priority_changed",),
        name="test-priority-subscriber",
    )


async def test_priority_emoji_change_emits_priority_changed_event(
    bus: EventBus,
    tasks_path: Path,
) -> None:
    """User edits the priority emoji on a projected line → watcher
    emits ``obsidian.task.priority_changed`` with the prior + new
    enum strings (not the emoji literals)."""
    pri_sub = _subscribe_priority(bus)
    sync_state = ProjectionSyncState()
    _write_tasks_file(tasks_path, ["- [ ] Task ⏫ 🆔 lithos:a"])
    _record_projection(sync_state, tasks_path, {"a": "[ ]"}, priorities={"a": "high"})
    watcher = ObsidianFsWatcher(bus=bus, tasks_path=tasks_path, sync_state=sync_state)
    watcher._last_seen_hash = sync_state.last_written_hash
    watcher._last_processed_write_version = sync_state.write_version

    _write_tasks_file(tasks_path, ["- [ ] Task 🔺 🆔 lithos:a"])
    published = await watcher.poll_once()
    events = _drain(pri_sub)

    assert published == 1
    assert len(events) == 1
    assert events[0].type == "obsidian.task.priority_changed"
    assert events[0].payload == {"task_id": "a", "prior": "high", "new": "highest"}


async def test_priority_emoji_removed_emits_event_with_new_none(
    bus: EventBus,
    tasks_path: Path,
) -> None:
    """User deleting the emoji entirely → ``new=None``."""
    pri_sub = _subscribe_priority(bus)
    sync_state = ProjectionSyncState()
    _write_tasks_file(tasks_path, ["- [ ] Task ⏫ 🆔 lithos:a"])
    _record_projection(sync_state, tasks_path, {"a": "[ ]"}, priorities={"a": "high"})
    watcher = ObsidianFsWatcher(bus=bus, tasks_path=tasks_path, sync_state=sync_state)
    watcher._last_seen_hash = sync_state.last_written_hash
    watcher._last_processed_write_version = sync_state.write_version

    _write_tasks_file(tasks_path, ["- [ ] Task 🆔 lithos:a"])
    await watcher.poll_once()
    [event] = _drain(pri_sub)
    assert event.payload == {"task_id": "a", "prior": "high", "new": None}


async def test_priority_emoji_added_emits_event_with_prior_none(
    bus: EventBus,
    tasks_path: Path,
) -> None:
    """User adding an emoji where none existed → ``prior=None``."""
    pri_sub = _subscribe_priority(bus)
    sync_state = ProjectionSyncState()
    _write_tasks_file(tasks_path, ["- [ ] Task 🆔 lithos:a"])
    _record_projection(sync_state, tasks_path, {"a": "[ ]"}, priorities={"a": None})
    watcher = ObsidianFsWatcher(bus=bus, tasks_path=tasks_path, sync_state=sync_state)
    watcher._last_seen_hash = sync_state.last_written_hash
    watcher._last_processed_write_version = sync_state.write_version

    _write_tasks_file(tasks_path, ["- [ ] Task 🔽 🆔 lithos:a"])
    await watcher.poll_once()
    [event] = _drain(pri_sub)
    assert event.payload == {"task_id": "a", "prior": None, "new": "low"}


async def test_status_and_priority_change_in_same_save_emit_both_events(
    bus: EventBus,
    sub: Subscription,
    tasks_path: Path,
) -> None:
    """One save that flips BOTH ``[ ] → [x]`` AND the priority emoji
    emits two distinct events on the bus — one ``status_changed``
    and one ``priority_changed`` — neither suppresses the other."""
    pri_sub = _subscribe_priority(bus)
    sync_state = ProjectionSyncState()
    _write_tasks_file(tasks_path, ["- [ ] Task ⏫ 🆔 lithos:a"])
    _record_projection(sync_state, tasks_path, {"a": "[ ]"}, priorities={"a": "high"})
    watcher = ObsidianFsWatcher(bus=bus, tasks_path=tasks_path, sync_state=sync_state)
    watcher._last_seen_hash = sync_state.last_written_hash
    watcher._last_processed_write_version = sync_state.write_version

    _write_tasks_file(tasks_path, ["- [x] Task 🔺 🆔 lithos:a"])
    published = await watcher.poll_once()

    status_events = _drain(sub)
    priority_events = _drain(pri_sub)
    assert published == 2
    assert len(status_events) == 1
    assert status_events[0].payload == {
        "task_id": "a",
        "prior": "[ ]",
        "new": "[x]",
    }
    assert len(priority_events) == 1
    assert priority_events[0].payload == {
        "task_id": "a",
        "prior": "high",
        "new": "highest",
    }


async def test_priority_change_subject_to_self_write_suppression(
    bus: EventBus,
    tasks_path: Path,
) -> None:
    """When the projection's self-write fires layer 2, both
    ``_observed_markers`` AND ``_observed_priorities`` clear; no
    spurious event for the priority diff that the self-write
    introduced."""
    pri_sub = _subscribe_priority(bus)
    sync_state = ProjectionSyncState()
    _write_tasks_file(tasks_path, ["- [ ] Task ⏫ 🆔 lithos:a"])
    _record_projection(sync_state, tasks_path, {"a": "[ ]"}, priorities={"a": "high"})
    watcher = ObsidianFsWatcher(bus=bus, tasks_path=tasks_path, sync_state=sync_state)
    watcher._last_seen_hash = sync_state.last_written_hash
    watcher._last_processed_write_version = sync_state.write_version

    # Simulate a projection self-write: priority becomes "highest".
    _write_tasks_file(tasks_path, ["- [ ] Task 🔺 🆔 lithos:a"])
    _record_projection(
        sync_state, tasks_path, {"a": "[ ]"}, priorities={"a": "highest"}
    )

    assert await watcher.poll_once() == 0
    assert _drain(pri_sub) == []
    # _observed_priorities was cleared by the layer-2 path; future
    # user edits compare against the fresh sync_state baseline.
    assert watcher._observed_priorities == {}


async def test_priority_emoji_table_matches_projection_table() -> None:
    """Anti-drift: ``EMOJI_TO_PRIORITY`` (watcher) is the exact inverse
    of ``_PRIORITY_EMOJI`` (projection). If either is changed the
    other must change too — same enum, same emoji set, no missing
    keys, no swapped pairs. Anything else means an emoji edit would
    parse to a different enum than the projection rendered for."""
    from lithos_loom.render import PRIORITY_EMOJI as _PRIORITY_EMOJI
    from lithos_loom.sources.obsidian_fs_watcher import EMOJI_TO_PRIORITY

    assert set(EMOJI_TO_PRIORITY.values()) == set(_PRIORITY_EMOJI.keys())
    for enum_value, emoji in _PRIORITY_EMOJI.items():
        assert EMOJI_TO_PRIORITY[emoji] == enum_value, (
            f"emoji {emoji!r} → {EMOJI_TO_PRIORITY[emoji]!r} in watcher but "
            f"projection renders {enum_value!r} → {emoji!r}"
        )


async def test_user_priority_edit_followed_by_unrelated_save_does_not_re_emit(
    bus: EventBus,
    tasks_path: Path,
) -> None:
    """Transition semantics for priority (parallel to the status
    test above): once the user changes ⏫ → 🔺, a subsequent save
    that leaves 🔺 in place must NOT re-emit."""
    pri_sub = _subscribe_priority(bus)
    sync_state = ProjectionSyncState()
    _write_tasks_file(tasks_path, ["- [ ] Task ⏫ 🆔 lithos:a"])
    _record_projection(sync_state, tasks_path, {"a": "[ ]"}, priorities={"a": "high"})
    watcher = ObsidianFsWatcher(bus=bus, tasks_path=tasks_path, sync_state=sync_state)
    watcher._last_seen_hash = sync_state.last_written_hash
    watcher._last_processed_write_version = sync_state.write_version

    _write_tasks_file(tasks_path, ["- [ ] Task 🔺 🆔 lithos:a"])
    assert await watcher.poll_once() == 1
    _drain(pri_sub)

    # Subsequent save: same priority, whitespace tweak elsewhere.
    _write_tasks_file(
        tasks_path,
        [
            "- [ ] Task 🔺 🆔 lithos:a",
            "  comment line",
        ],
    )
    assert await watcher.poll_once() == 0
    assert _drain(pri_sub) == []


# ── Due-date changes (Slice 3 round-trip) ──────────────────────────────


def _subscribe_due_date(bus: EventBus) -> Subscription:
    return bus.subscribe(
        event_types=("obsidian.task.due_date_changed",),
        name="test-due-date-subscriber",
    )


async def test_due_date_change_emits_due_date_changed_event(
    bus: EventBus,
    tasks_path: Path,
) -> None:
    """User edits ``📅 YYYY-MM-DD`` on a projected line → watcher
    emits ``obsidian.task.due_date_changed`` with the verbatim date
    strings (or None)."""
    due_sub = _subscribe_due_date(bus)
    sync_state = ProjectionSyncState()
    _write_tasks_file(tasks_path, ["- [ ] Task 🆔 lithos:a 📅 2026-05-20"])
    _record_projection(
        sync_state, tasks_path, {"a": "[ ]"}, due_dates={"a": "2026-05-20"}
    )
    watcher = ObsidianFsWatcher(bus=bus, tasks_path=tasks_path, sync_state=sync_state)
    watcher._last_seen_hash = sync_state.last_written_hash
    watcher._last_processed_write_version = sync_state.write_version

    _write_tasks_file(tasks_path, ["- [ ] Task 🆔 lithos:a 📅 2026-06-15"])
    published = await watcher.poll_once()
    events = _drain(due_sub)

    assert published == 1
    assert len(events) == 1
    assert events[0].type == "obsidian.task.due_date_changed"
    assert events[0].payload == {
        "task_id": "a",
        "prior": "2026-05-20",
        "new": "2026-06-15",
    }


async def test_due_date_removed_emits_event_with_new_none(
    bus: EventBus,
    tasks_path: Path,
) -> None:
    """User deleting the 📅 marker entirely → ``new=None``."""
    due_sub = _subscribe_due_date(bus)
    sync_state = ProjectionSyncState()
    _write_tasks_file(tasks_path, ["- [ ] Task 🆔 lithos:a 📅 2026-05-20"])
    _record_projection(
        sync_state, tasks_path, {"a": "[ ]"}, due_dates={"a": "2026-05-20"}
    )
    watcher = ObsidianFsWatcher(bus=bus, tasks_path=tasks_path, sync_state=sync_state)
    watcher._last_seen_hash = sync_state.last_written_hash
    watcher._last_processed_write_version = sync_state.write_version

    _write_tasks_file(tasks_path, ["- [ ] Task 🆔 lithos:a"])
    await watcher.poll_once()
    [event] = _drain(due_sub)
    assert event.payload == {"task_id": "a", "prior": "2026-05-20", "new": None}


async def test_due_date_added_emits_event_with_prior_none(
    bus: EventBus,
    tasks_path: Path,
) -> None:
    """Inverse: user added a 📅 marker where none existed."""
    due_sub = _subscribe_due_date(bus)
    sync_state = ProjectionSyncState()
    _write_tasks_file(tasks_path, ["- [ ] Task 🆔 lithos:a"])
    _record_projection(sync_state, tasks_path, {"a": "[ ]"}, due_dates={"a": None})
    watcher = ObsidianFsWatcher(bus=bus, tasks_path=tasks_path, sync_state=sync_state)
    watcher._last_seen_hash = sync_state.last_written_hash
    watcher._last_processed_write_version = sync_state.write_version

    _write_tasks_file(tasks_path, ["- [ ] Task 🆔 lithos:a 📅 2026-07-01"])
    await watcher.poll_once()
    [event] = _drain(due_sub)
    assert event.payload == {"task_id": "a", "prior": None, "new": "2026-07-01"}


async def test_due_date_malformed_format_treated_as_no_date(
    bus: EventBus,
    tasks_path: Path,
) -> None:
    """``📅 next Friday`` doesn't match ``YYYY-MM-DD`` so the watcher
    treats it as "no date" and would emit a delete (None) if the
    projection had previously recorded a date. Guards against
    bouncing garbage values back to Lithos."""
    due_sub = _subscribe_due_date(bus)
    sync_state = ProjectionSyncState()
    _write_tasks_file(tasks_path, ["- [ ] Task 🆔 lithos:a 📅 2026-05-20"])
    _record_projection(
        sync_state, tasks_path, {"a": "[ ]"}, due_dates={"a": "2026-05-20"}
    )
    watcher = ObsidianFsWatcher(bus=bus, tasks_path=tasks_path, sync_state=sync_state)
    watcher._last_seen_hash = sync_state.last_written_hash
    watcher._last_processed_write_version = sync_state.write_version

    _write_tasks_file(tasks_path, ["- [ ] Task 🆔 lithos:a 📅 next Friday"])
    await watcher.poll_once()
    [event] = _drain(due_sub)
    # Malformed date parsed as None → handler sends scheduled_for=None
    # which deletes the key in Lithos. Operator's edit "failed" cleanly
    # rather than persisting a garbage value.
    assert event.payload == {"task_id": "a", "prior": "2026-05-20", "new": None}


async def test_due_date_change_subject_to_self_write_suppression(
    bus: EventBus,
    tasks_path: Path,
) -> None:
    """Layer-2 self-write suppression clears _observed_dates too."""
    due_sub = _subscribe_due_date(bus)
    sync_state = ProjectionSyncState()
    _write_tasks_file(tasks_path, ["- [ ] Task 🆔 lithos:a 📅 2026-05-20"])
    _record_projection(
        sync_state, tasks_path, {"a": "[ ]"}, due_dates={"a": "2026-05-20"}
    )
    watcher = ObsidianFsWatcher(bus=bus, tasks_path=tasks_path, sync_state=sync_state)
    watcher._last_seen_hash = sync_state.last_written_hash
    watcher._last_processed_write_version = sync_state.write_version

    # Projection writes a new content with a different date.
    _write_tasks_file(tasks_path, ["- [ ] Task 🆔 lithos:a 📅 2026-06-15"])
    _record_projection(
        sync_state, tasks_path, {"a": "[ ]"}, due_dates={"a": "2026-06-15"}
    )

    # Watcher polls — sees the projection write, suppresses.
    assert await watcher.poll_once() == 0
    assert _drain(due_sub) == []
    # _observed_dates should have been cleared by the layer-2 path.
    assert watcher._observed_dates == {}


async def test_due_date_combined_with_status_and_priority_in_one_save(
    bus: EventBus,
    sub: Subscription,
    tasks_path: Path,
) -> None:
    """One file save that changes status + priority + due date emits
    three independent events for the same task."""
    pri_sub = _subscribe_priority(bus)
    due_sub = _subscribe_due_date(bus)
    sync_state = ProjectionSyncState()
    _write_tasks_file(tasks_path, ["- [ ] Task 🆔 lithos:a ⏫ 📅 2026-05-20"])
    _record_projection(
        sync_state,
        tasks_path,
        {"a": "[ ]"},
        priorities={"a": "high"},
        due_dates={"a": "2026-05-20"},
    )
    watcher = ObsidianFsWatcher(bus=bus, tasks_path=tasks_path, sync_state=sync_state)
    watcher._last_seen_hash = sync_state.last_written_hash
    watcher._last_processed_write_version = sync_state.write_version

    _write_tasks_file(tasks_path, ["- [x] Task 🆔 lithos:a 🔺 📅 2026-06-15"])
    published = await watcher.poll_once()

    assert published == 3
    status_events = _drain(sub)
    pri_events = _drain(pri_sub)
    due_events = _drain(due_sub)
    assert len(status_events) == 1
    assert len(pri_events) == 1
    assert len(due_events) == 1
    assert status_events[0].payload["new"] == "[x]"
    assert pri_events[0].payload["new"] == "highest"
    assert due_events[0].payload["new"] == "2026-06-15"


# ── Transition semantics: each transition emits exactly once ───────────


async def test_subsequent_save_with_same_marker_does_not_re_emit(
    bus: EventBus,
    sub: Subscription,
    tasks_path: Path,
) -> None:
    """Once the user flips ``[ ] → [x]``, any subsequent file save that
    leaves the marker at ``[x]`` (whitespace change, edit to a sibling
    line, comment added) must NOT re-emit the same transition.

    Regression: the watcher previously compared parsed markers against
    ``sync_state.task_status_markers`` only, which advances only on
    projection writes. A user save that didn't trigger a projection
    write would re-trigger layer 3 with the same prior=[ ] / new=[x]
    diff every time.
    """
    sync_state = ProjectionSyncState()
    _write_tasks_file(tasks_path, ["- [ ] Review PR 🆔 lithos:abc"])
    _record_projection(sync_state, tasks_path, {"abc": "[ ]"})

    watcher = ObsidianFsWatcher(bus=bus, tasks_path=tasks_path, sync_state=sync_state)

    # First save: real transition → emit.
    _write_tasks_file(tasks_path, ["- [x] Review PR 🆔 lithos:abc"])
    assert await watcher.poll_once() == 1
    [first] = _drain(sub)
    assert first.payload == {"task_id": "abc", "prior": "[ ]", "new": "[x]"}

    # Second save: same marker, different surrounding content (the
    # user added a note line). Must NOT re-emit.
    _write_tasks_file(
        tasks_path,
        [
            "- [x] Review PR 🆔 lithos:abc",
            "  - the PR comment is here",
        ],
    )
    assert await watcher.poll_once() == 0
    assert _drain(sub) == []

    # Third save: yet another unrelated whitespace tweak. Still no
    # re-emit.
    _write_tasks_file(
        tasks_path,
        [
            "- [x] Review PR 🆔 lithos:abc",
            "  - the PR comment is here  ",  # trailing whitespace
        ],
    )
    assert await watcher.poll_once() == 0
    assert _drain(sub) == []


async def test_user_flip_then_flip_back_emits_both_transitions(
    bus: EventBus,
    sub: Subscription,
    tasks_path: Path,
) -> None:
    """``[ ] → [x] → [ ]`` produces two distinct events; the second one
    correctly uses ``[x]`` (the user's last observed marker) as
    ``prior``, not the stale projection-known ``[ ]``."""
    sync_state = ProjectionSyncState()
    _write_tasks_file(tasks_path, ["- [ ] Task 🆔 lithos:a"])
    _record_projection(sync_state, tasks_path, {"a": "[ ]"})

    watcher = ObsidianFsWatcher(bus=bus, tasks_path=tasks_path, sync_state=sync_state)

    _write_tasks_file(tasks_path, ["- [x] Task 🆔 lithos:a"])
    await watcher.poll_once()
    _write_tasks_file(tasks_path, ["- [ ] Task 🆔 lithos:a"])
    await watcher.poll_once()

    events = _drain(sub)
    assert len(events) == 2
    assert events[0].payload == {"task_id": "a", "prior": "[ ]", "new": "[x]"}
    assert events[1].payload == {"task_id": "a", "prior": "[x]", "new": "[ ]"}


async def test_projection_self_write_clears_observed_markers(
    bus: EventBus,
    sub: Subscription,
    tasks_path: Path,
) -> None:
    """When the projection re-renders the file (Lithos drove a change),
    its content is authoritative over any user edits we've previously
    observed. Subsequent user edits must therefore use the projection's
    fresh marker as ``prior``, not the stale user-observed one."""
    sync_state = ProjectionSyncState()
    _write_tasks_file(tasks_path, ["- [ ] Task 🆔 lithos:a"])
    _record_projection(sync_state, tasks_path, {"a": "[ ]"})

    watcher = ObsidianFsWatcher(bus=bus, tasks_path=tasks_path, sync_state=sync_state)

    # 1. User ticks to [x]. Watcher emits and records observed_markers[a]=[x].
    _write_tasks_file(tasks_path, ["- [x] Task 🆔 lithos:a"])
    assert await watcher.poll_once() == 1
    _drain(sub)

    # 2. Projection re-renders, e.g. reflects a status change Lithos
    #    initiated independently. Disk now matches the projection's
    #    view; layer 2 fires and clears observed_markers.
    _write_tasks_file(tasks_path, ["- [-] Task ❌ 2026-05-22 🆔 lithos:a"])
    _record_projection(sync_state, tasks_path, {"a": "[-]"})
    assert await watcher.poll_once() == 0

    # 3. User toggles to [x]. With observed_markers cleared in step 2,
    #    the prior must come from sync_state ([-]), NOT from the stale
    #    observed [x] from step 1.
    _write_tasks_file(tasks_path, ["- [x] Task ❌ 2026-05-22 🆔 lithos:a"])
    assert await watcher.poll_once() == 1
    [event] = _drain(sub)
    assert event.payload == {"task_id": "a", "prior": "[-]", "new": "[x]"}


# ── US23: self-write suppression ───────────────────────────────────────


async def test_unchanged_file_emits_nothing(
    bus: EventBus,
    sub: Subscription,
    tasks_path: Path,
) -> None:
    """Steady-state poll on an unchanging file → zero events, zero work."""
    sync_state = ProjectionSyncState()
    _write_tasks_file(tasks_path, ["- [ ] Task 🆔 lithos:a"])
    _record_projection(sync_state, tasks_path, {"a": "[ ]"})

    watcher = ObsidianFsWatcher(bus=bus, tasks_path=tasks_path, sync_state=sync_state)
    # Seed _last_seen_hash by calling run-style init logic — easiest
    # is one initial poll on the unchanged file.
    watcher._last_seen_hash = sync_state.last_written_hash

    published = await watcher.poll_once()
    assert published == 0
    assert _drain(sub) == []


async def test_projection_self_write_does_not_emit(
    bus: EventBus,
    sub: Subscription,
    tasks_path: Path,
) -> None:
    """File content changes to projection's known new hash → suppressed.

    Simulates the Lithos-task-completed-then-projection-writes flow:
    projection updates ``sync_state`` and the file together; the
    watcher's next poll must NOT emit, because the change came from
    us, not the user.
    """
    sync_state = ProjectionSyncState()
    _write_tasks_file(tasks_path, ["- [ ] Task 🆔 lithos:a"])
    _record_projection(sync_state, tasks_path, {"a": "[ ]"})

    watcher = ObsidianFsWatcher(bus=bus, tasks_path=tasks_path, sync_state=sync_state)
    watcher._last_seen_hash = sync_state.last_written_hash

    # Projection updates state AND file (Lithos completed the task).
    _write_tasks_file(tasks_path, ["- [x] Task ✅ 2026-05-22 🆔 lithos:a"])
    _record_projection(sync_state, tasks_path, {"a": "[x]"})

    published = await watcher.poll_once()
    assert published == 0
    assert _drain(sub) == []


async def test_user_edit_after_self_write_is_detected(
    bus: EventBus,
    sub: Subscription,
    tasks_path: Path,
) -> None:
    """Self-write suppression doesn't permanently silence the watcher."""
    sync_state = ProjectionSyncState()
    _write_tasks_file(tasks_path, ["- [ ] Task 🆔 lithos:a"])
    _record_projection(sync_state, tasks_path, {"a": "[ ]"})

    watcher = ObsidianFsWatcher(bus=bus, tasks_path=tasks_path, sync_state=sync_state)
    watcher._last_seen_hash = sync_state.last_written_hash

    # Self-write to [x].
    _write_tasks_file(tasks_path, ["- [x] Task ✅ 2026-05-22 🆔 lithos:a"])
    _record_projection(sync_state, tasks_path, {"a": "[x]"})
    assert await watcher.poll_once() == 0

    # User then unticks back to [ ].
    _write_tasks_file(tasks_path, ["- [ ] Task ✅ 2026-05-22 🆔 lithos:a"])
    published = await watcher.poll_once()
    events = _drain(sub)
    assert published == 1
    assert events[0].payload == {"task_id": "a", "prior": "[x]", "new": "[ ]"}


async def test_task_unknown_to_projection_is_ignored(
    bus: EventBus,
    sub: Subscription,
    tasks_path: Path,
) -> None:
    """Capture-macro lines (Slice 3+) the projection has never written
    are silently ignored — Slice 2 only owns projection-known tasks."""
    sync_state = ProjectionSyncState()
    _write_tasks_file(tasks_path, ["- [ ] Captured 🆔 lithos:cap1"])
    # NOTE: do NOT record into sync_state — simulate a file the
    # projection has never written.
    sync_state.last_written_hash = b""  # arbitrary non-matching hash
    sync_state.task_status_markers = {}

    watcher = ObsidianFsWatcher(bus=bus, tasks_path=tasks_path, sync_state=sync_state)

    # User ticks the unknown task.
    _write_tasks_file(tasks_path, ["- [x] Captured 🆔 lithos:cap1"])
    published = await watcher.poll_once()
    assert published == 0
    assert _drain(sub) == []


# ── US22: source-replay on restart is safe ────────────────────────────


async def test_cold_start_restart_with_unchanged_file_emits_nothing(
    bus: EventBus,
    sub: Subscription,
    tasks_path: Path,
) -> None:
    """US22 source-replay safety: after a daemon restart, the file on
    disk still has the pre-restart projected lines (status markers and
    priority emoji) but ``sync_state`` is empty because in-memory
    state was lost. The watcher's first poll must emit ZERO events —
    nothing actually changed; only Loom's coordinator state did.

    This is exercised by the same projection-known gate that
    :func:`test_task_unknown_to_projection_is_ignored` covers for
    capture-macro lines, but called out separately because the
    restart-replay phrasing is what US22 explicitly promises and
    a future change that "improves" the gate could regress this
    without breaking the capture-macro test."""
    pri_sub = _subscribe_priority(bus)

    # File written by a pre-restart projection: one open task with a
    # priority emoji (high), one completed task within the resolved
    # TTL. Both should be silent post-restart.
    _write_tasks_file(
        tasks_path,
        [
            "- [ ] Open thing ⏫ 🆔 lithos:cs1",
            "- [x] Done thing ✅ 2026-05-22 🆔 lithos:cs2",
        ],
    )

    # Cold-start invariant: in-memory sync_state was lost.
    sync_state = ProjectionSyncState()
    assert sync_state.task_status_markers == {}
    assert sync_state.task_priority_markers == {}

    watcher = ObsidianFsWatcher(bus=bus, tasks_path=tasks_path, sync_state=sync_state)
    published = await watcher.poll_once()

    assert published == 0, f"cold start emitted {published} events; expected 0"
    assert _drain(sub) == [], "no status_changed events on cold start"
    assert _drain(pri_sub) == [], "no priority_changed events on cold start"


async def test_cold_start_then_projection_settles_then_user_edit_emits(
    bus: EventBus,
    sub: Subscription,
    tasks_path: Path,
) -> None:
    """US22 follow-through: after cold start the projection re-processes
    Lithos events and populates ``sync_state``. A subsequent genuine
    user edit then emits the expected transition. Pins the "safe on
    restart, still alive after settle" contract — the cold-start
    suppression must not silently break legitimate edits once the
    coordinator catches up."""
    pri_sub = _subscribe_priority(bus)

    _write_tasks_file(tasks_path, ["- [ ] Settled task ⏫ 🆔 lithos:s1"])
    sync_state = ProjectionSyncState()

    watcher = ObsidianFsWatcher(bus=bus, tasks_path=tasks_path, sync_state=sync_state)

    # 1. Cold-start poll — silent.
    assert await watcher.poll_once() == 0
    assert _drain(sub) == []
    assert _drain(pri_sub) == []

    # 2. Projection settles: it re-renders the file (same content here,
    # so the hash matches what's on disk) and records the post-write
    # state into sync_state.
    _record_projection(sync_state, tasks_path, {"s1": "[ ]"}, priorities={"s1": "high"})

    # 3. User edits: ticks the task AND changes priority emoji.
    _write_tasks_file(tasks_path, ["- [x] Settled task 🔺 🆔 lithos:s1"])

    published = await watcher.poll_once()
    status_events = _drain(sub)
    priority_events = _drain(pri_sub)

    # One status_changed + one priority_changed — the projection-known
    # gate cleared and both diffs surfaced normally.
    assert published == 2
    assert len(status_events) == 1
    assert status_events[0].payload == {"task_id": "s1", "prior": "[ ]", "new": "[x]"}
    assert len(priority_events) == 1
    assert priority_events[0].payload == {
        "task_id": "s1",
        "prior": "high",
        "new": "highest",
    }


# ── Robustness: malformed input, missing file ──────────────────────────


async def test_poll_reads_file_exactly_once(
    bus: EventBus,
    sub: Subscription,
    tasks_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each ``poll_once`` invocation must read the file exactly once,
    not twice (once for hashing + once for parsing). Reading twice
    opens a TOCTOU window where the parsed content can disagree with
    the recorded hash on a rapidly-edited file. Regression for the
    Copilot review on PR #26.
    """
    sync_state = ProjectionSyncState()
    _write_tasks_file(tasks_path, ["- [ ] Task 🆔 lithos:a"])
    _record_projection(sync_state, tasks_path, {"a": "[ ]"})

    watcher = ObsidianFsWatcher(bus=bus, tasks_path=tasks_path, sync_state=sync_state)
    # User edit so layer 3 runs (otherwise we'd short-circuit before
    # the parse and not exercise both code paths).
    _write_tasks_file(tasks_path, ["- [x] Task 🆔 lithos:a"])

    read_calls: list[Path] = []
    real_read_bytes = Path.read_bytes

    def _counting_read(self: Path) -> bytes:
        if self == tasks_path:
            read_calls.append(self)
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", _counting_read)
    await watcher.poll_once()
    assert len(read_calls) == 1, (
        f"poll_once must read {tasks_path} exactly once; got {len(read_calls)} reads"
    )


async def test_missing_file_is_no_op(
    bus: EventBus,
    sub: Subscription,
    tasks_path: Path,
) -> None:
    """File doesn't exist yet → poll is a no-op (no crash, no events)."""
    sync_state = ProjectionSyncState()
    watcher = ObsidianFsWatcher(bus=bus, tasks_path=tasks_path, sync_state=sync_state)
    assert await watcher.poll_once() == 0
    assert _drain(sub) == []


async def test_lines_without_task_id_are_skipped(
    bus: EventBus,
    sub: Subscription,
    tasks_path: Path,
) -> None:
    """Task-shaped lines without ``🆔 lithos:<id>`` are silently skipped."""
    sync_state = ProjectionSyncState()
    _write_tasks_file(tasks_path, ["- [ ] Has known id 🆔 lithos:a"])
    _record_projection(sync_state, tasks_path, {"a": "[ ]"})

    watcher = ObsidianFsWatcher(bus=bus, tasks_path=tasks_path, sync_state=sync_state)
    watcher._last_seen_hash = sync_state.last_written_hash

    _write_tasks_file(
        tasks_path,
        [
            "- [x] Has known id 🆔 lithos:a",
            "- [ ] No id at all",
            "- [/] Random line with no id either",
        ],
    )
    published = await watcher.poll_once()
    events = _drain(sub)
    assert published == 1
    assert events[0].payload["task_id"] == "a"


async def test_unknown_checkbox_marker_is_skipped(
    bus: EventBus,
    sub: Subscription,
    tasks_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``[?]`` or other unknown checkbox markers are skipped, not emitted."""
    sync_state = ProjectionSyncState()
    _write_tasks_file(tasks_path, ["- [ ] Task 🆔 lithos:a"])
    _record_projection(sync_state, tasks_path, {"a": "[ ]"})

    watcher = ObsidianFsWatcher(bus=bus, tasks_path=tasks_path, sync_state=sync_state)
    watcher._last_seen_hash = sync_state.last_written_hash

    _write_tasks_file(tasks_path, ["- [?] Task 🆔 lithos:a"])

    with caplog.at_level(
        logging.DEBUG, logger="lithos_loom.sources.obsidian_fs_watcher"
    ):
        published = await watcher.poll_once()
    assert published == 0
    assert _drain(sub) == []


# ── Bootstrap (run() seeding via sync_state) ───────────────────────────


async def test_run_seeds_last_seen_from_sync_state(
    bus: EventBus,
    sub: Subscription,
    tasks_path: Path,
) -> None:
    """Watcher bootstraps from ``sync_state.last_written_hash``, not disk.

    Edge case: user edits file in the gap between projection-seed and
    watcher-start. Seeding from disk would silently swallow the edit
    (initial = user-edited content, first poll = no change). Seeding
    from sync_state means the first poll sees the user's edit.
    """
    sync_state = ProjectionSyncState()
    _write_tasks_file(tasks_path, ["- [ ] Task 🆔 lithos:a"])
    _record_projection(sync_state, tasks_path, {"a": "[ ]"})

    # User edits BEFORE watcher's run() starts.
    _write_tasks_file(tasks_path, ["- [x] Task 🆔 lithos:a"])

    watcher = ObsidianFsWatcher(bus=bus, tasks_path=tasks_path, sync_state=sync_state)
    # Stand in for run()'s bootstrap step.
    watcher._last_seen_hash = sync_state.last_written_hash

    published = await watcher.poll_once()
    events = _drain(sub)
    assert published == 1
    assert events[0].payload == {"task_id": "a", "prior": "[ ]", "new": "[x]"}


# ── Event payload + timestamp ───────────────────────────────────────────


async def test_event_timestamp_uses_now_provider(
    bus: EventBus,
    sub: Subscription,
    tasks_path: Path,
) -> None:
    """The injected ``_now_provider`` is honoured for event timestamps."""
    fixed = datetime(2026, 5, 22, 10, 30, tzinfo=UTC)
    sync_state = ProjectionSyncState()
    _write_tasks_file(tasks_path, ["- [ ] T 🆔 lithos:a"])
    _record_projection(sync_state, tasks_path, {"a": "[ ]"})

    watcher = ObsidianFsWatcher(
        bus=bus,
        tasks_path=tasks_path,
        sync_state=sync_state,
        _now_provider=lambda: fixed,
    )
    watcher._last_seen_hash = sync_state.last_written_hash

    _write_tasks_file(tasks_path, ["- [x] T 🆔 lithos:a"])
    await watcher.poll_once()
    [event] = _drain(sub)
    assert event.timestamp == fixed


async def test_event_payload_is_immutable_mapping(
    bus: EventBus,
    sub: Subscription,
    tasks_path: Path,
) -> None:
    """Event payload is a read-only Mapping (MappingProxyType) so a
    misbehaving consumer can't mutate it and trip sibling subscribers."""
    sync_state = ProjectionSyncState()
    _write_tasks_file(tasks_path, ["- [ ] T 🆔 lithos:a"])
    _record_projection(sync_state, tasks_path, {"a": "[ ]"})

    watcher = ObsidianFsWatcher(bus=bus, tasks_path=tasks_path, sync_state=sync_state)
    watcher._last_seen_hash = sync_state.last_written_hash

    _write_tasks_file(tasks_path, ["- [x] T 🆔 lithos:a"])
    await watcher.poll_once()
    [event] = _drain(sub)

    with pytest.raises(TypeError):
        event.payload["task_id"] = "mutated"  # type: ignore[index]


# ── run() loop wiring ──────────────────────────────────────────────────


async def test_run_polls_until_cancelled(
    bus: EventBus,
    sub: Subscription,
    tasks_path: Path,
) -> None:
    """The polling loop ticks at the configured interval and stops on
    cancellation (smoke test that ``run()`` actually loops + cleans up)."""
    sync_state = ProjectionSyncState()
    _write_tasks_file(tasks_path, ["- [ ] T 🆔 lithos:a"])
    _record_projection(sync_state, tasks_path, {"a": "[ ]"})

    watcher = ObsidianFsWatcher(
        bus=bus,
        tasks_path=tasks_path,
        sync_state=sync_state,
        poll_interval_seconds=0.01,
    )
    task = asyncio.create_task(watcher.run())
    # Give the loop a moment to do at least one no-op poll, then edit.
    await asyncio.sleep(0.03)
    _write_tasks_file(tasks_path, ["- [x] T 🆔 lithos:a"])
    # Let the next poll fire.
    await asyncio.sleep(0.05)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    events = _drain(sub)
    assert len(events) == 1
    assert events[0].payload == {"task_id": "a", "prior": "[ ]", "new": "[x]"}


# ── Quick utilities ────────────────────────────────────────────────────


def _scan_payload_keys(events: list[Event]) -> Iterator[str]:
    for e in events:
        yield from e.payload


def test_regex_does_not_match_header_lines() -> None:
    """The line-regex skips ``%% header %%`` and free text so the parser
    doesn't trip on metadata banners."""
    from lithos_loom.sources.obsidian_fs_watcher import _LINE_RE

    assert _LINE_RE.match("%% Auto-generated by lithos-loom %%") is None
    assert _LINE_RE.match("") is None
    assert _LINE_RE.match("Resolved tasks (within window):") is None
    assert _LINE_RE.match("- [x] Real line") is not None


def test_task_id_regex_extracts_uuid_like_strings() -> None:
    from lithos_loom.sources.obsidian_fs_watcher import _TASK_ID_RE

    m = _TASK_ID_RE.search("- [ ] Title 🆔 lithos:abc-123_XYZ #project/foo")
    assert m is not None
    assert m.group("task_id") == "abc-123_XYZ"


def test_module_exports_status_markers() -> None:
    from lithos_loom.sources.obsidian_fs_watcher import VALID_STATUS_MARKERS

    assert "[ ]" in VALID_STATUS_MARKERS
    assert "[x]" in VALID_STATUS_MARKERS
    assert "[-]" in VALID_STATUS_MARKERS
    assert "[/]" in VALID_STATUS_MARKERS
    assert "[>]" in VALID_STATUS_MARKERS
    assert "[?]" not in VALID_STATUS_MARKERS


def test_line_regex_only_matches_dash_bracket_prefix() -> None:
    from lithos_loom.sources.obsidian_fs_watcher import _LINE_RE

    # Asterisk bullet, indented bullet, no-bullet — none should match.
    assert _LINE_RE.match("  - [ ] indented") is None
    assert _LINE_RE.match("* [ ] asterisk") is None
    assert _LINE_RE.match("[ ] no dash") is None
    # And confirm the matched form is exactly "- [<m>] " with trailing space.
    assert _LINE_RE.match("- [x]no-space-after") is None
    assert _LINE_RE.match("- [x] with-space") is not None


def test_task_id_regex_stops_at_non_id_char() -> None:
    """``task.id`` charset is ``[A-Za-z0-9_-]+`` — periods, slashes,
    spaces break it (this matches Lithos task-id format)."""
    from lithos_loom.sources.obsidian_fs_watcher import _TASK_ID_RE

    m = _TASK_ID_RE.search("🆔 lithos:abc.def")
    assert m is not None
    assert m.group("task_id") == "abc"  # stops at .
    assert _TASK_ID_RE.search("🆔 lithos:") is None  # empty


def test_line_regex_compiles_and_pattern_visible() -> None:
    """Defensive: confirm the module-level regex objects are exposed for
    these tests to import (not leak-through-the-back-door)."""
    from lithos_loom.sources import obsidian_fs_watcher as mod

    assert isinstance(mod._LINE_RE, re.Pattern)
    assert isinstance(mod._TASK_ID_RE, re.Pattern)
