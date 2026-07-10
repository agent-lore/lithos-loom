"""Tests for :class:`lithos_loom.sources.github_watch_state.GitHubWatchStateStore`.

Extracted from ``test_github_issue_watcher.py`` (ARCH-8): the coord-doc grammar,
the ``load`` seeding, and the CAS-with-tombstones ``persist`` are the store's
own logic and are pinned here directly against a ``GitHubWatchStateStore``. The
watcher's tests exercise the poll loop through the store's public interface.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

from lithos_loom.lithos_client import Note, WriteResult
from lithos_loom.sources.github_watch_state import (
    GitHubWatchStateStore,
    format_cursors,
    parse_cursors,
    parse_stuck,
)

_COORD_DOC_PATH = "projects/_lithos-loom-internal/github-watcher-state.md"


def _fake_lithos_client(
    *,
    note_read_return: Note | None = None,
    write_result: WriteResult | None = None,
) -> Any:
    client = AsyncMock()
    client.note_read = AsyncMock(return_value=note_read_return)
    client.note_write = AsyncMock(
        return_value=write_result or WriteResult(status="updated")
    )
    return client


def _make_store(lithos: Any) -> GitHubWatchStateStore:
    return GitHubWatchStateStore(
        lithos=lithos, agent_id="test-agent", coord_doc_path=_COORD_DOC_PATH
    )


# ── Public interface ──────────────────────────────────────────────────


def test_cursor_roundtrips_through_set_and_get() -> None:
    store = _make_store(_fake_lithos_client())
    assert store.cursor("owner/x") is None
    ts = datetime(2026, 5, 29, tzinfo=UTC)
    store.set_cursor("owner/x", ts)
    assert store.cursor("owner/x") == ts


def test_forget_cursor_reports_presence() -> None:
    store = _make_store(_fake_lithos_client())
    assert store.forget_cursor("owner/x") is False  # absent → no reset to log
    store.set_cursor("owner/x", datetime(2026, 5, 29, tzinfo=UTC))
    assert store.forget_cursor("owner/x") is True
    assert store.cursor("owner/x") is None


def test_mark_and_discard_stuck() -> None:
    store = _make_store(_fake_lithos_client())
    assert store.stuck_numbers("owner/x") == []
    store.mark_stuck("owner/x", 42)
    store.mark_stuck("owner/x", 7)
    assert store.stuck_numbers("owner/x") == [7, 42]  # sorted snapshot
    store.discard_stuck("owner/x", 7)
    assert store.stuck_numbers("owner/x") == [42]


def test_discard_stuck_drops_repo_key_when_drained() -> None:
    store = _make_store(_fake_lithos_client())
    store.mark_stuck("owner/x", 42)
    store.discard_stuck("owner/x", 42)
    assert "owner/x" not in store._stuck_issues  # key gone, not an empty set
    store.discard_stuck("owner/x", 99)  # absent repo → no-op, no raise


def test_drop_repo_clears_cursor_and_stuck() -> None:
    store = _make_store(_fake_lithos_client())
    store.set_cursor("owner/x", datetime(2026, 5, 29, tzinfo=UTC))
    store.mark_stuck("owner/x", 42)
    store.drop_repo("owner/x")
    assert store.cursor("owner/x") is None
    assert store.stuck_numbers("owner/x") == []


# ── Coord doc grammar ───────────────────────────────────────────────────


def test_format_then_parse_round_trips() -> None:
    cursors = {
        "agent-lore/lithos-loom": datetime(2026, 5, 29, 12, 0, 0, tzinfo=UTC),
        "agent-lore/lithos": datetime(2026, 5, 28, 11, 30, 0, tzinfo=UTC),
    }
    body = format_cursors(cursors)
    parsed = parse_cursors(body)
    assert parsed == cursors


def test_parse_cursors_handles_empty_body() -> None:
    assert parse_cursors("") == {}


def test_parse_cursors_skips_comment_and_blank_lines() -> None:
    body = (
        "# header\n"
        "Daemon-owned coordination doc.\n"
        "\n"
        "agent-lore/lithos-loom 2026-05-29T12:00:00+00:00\n"
    )
    assert parse_cursors(body) == {
        "agent-lore/lithos-loom": datetime(2026, 5, 29, 12, 0, 0, tzinfo=UTC)
    }


def test_parse_cursors_ignores_unparseable_lines() -> None:
    body = (
        "valid/repo 2026-05-29T12:00:00Z\n"
        "noslashtimestamp invalid\n"
        "owner/name not-a-timestamp\n"
    )
    assert parse_cursors(body) == {
        "valid/repo": datetime(2026, 5, 29, 12, 0, 0, tzinfo=UTC)
    }


def test_parse_cursors_accepts_z_suffix() -> None:
    assert parse_cursors("owner/name 2026-05-29T12:00:00Z") == {
        "owner/name": datetime(2026, 5, 29, 12, 0, 0, tzinfo=UTC)
    }


async def test_stuck_issues_persist_and_reload_through_coord_doc() -> None:
    """PR-review finding 3 (round 5, 2026-05-30): the stuck-issue set
    rides on the coord doc so daemon restart preserves repair records.
    Without persistence, an issue stuck between an incomplete
    task_create + marker write and the next retry can be lost when
    the daemon restarts."""
    body = format_cursors(
        {"owner/x": datetime(2026, 5, 29, tzinfo=UTC)},
        stuck={"owner/x": {42, 99}, "owner/y": {7}},
    )
    assert "stuck:owner/x#42" in body
    assert "stuck:owner/x#99" in body
    assert "stuck:owner/y#7" in body
    # And it round-trips through the parser.
    assert parse_stuck(body) == {
        "owner/x": {42, 99},
        "owner/y": {7},
    }
    # Cursors are still parseable too — stuck rows are ignored by parse_cursors.
    assert parse_cursors(body) == {"owner/x": datetime(2026, 5, 29, tzinfo=UTC)}


def test_cursor_format_handles_future_timestamps() -> None:
    """No special-casing for issues from the future (clock skew) — just round-trip."""
    future = datetime(2030, 1, 1, tzinfo=UTC) + timedelta(seconds=1)
    assert parse_cursors(format_cursors({"x/y": future})) == {"x/y": future}


# ── load ────────────────────────────────────────────────────────────────


async def test_load_cursors_missing_doc_treats_as_first_run() -> None:
    lithos = _fake_lithos_client(note_read_return=None)
    store = _make_store(lithos)
    await store.load()
    assert store._cursors == {}
    assert store._coord_doc_id is None


async def test_load_cursors_parses_existing_doc() -> None:
    body = format_cursors(
        {"agent-lore/lithos-loom": datetime(2026, 5, 29, 12, 0, 0, tzinfo=UTC)}
    )
    note = Note(
        id="coord-id",
        title="GitHub Watcher State",
        body=body,
        version=7,
        updated_at=None,
        tags=(),
        status="active",
        note_type="concept",
        path="projects/_lithos-loom-internal/github-watcher-state.md",
        slug="_lithos-loom-internal",
    )
    lithos = _fake_lithos_client(note_read_return=note)
    store = _make_store(lithos)
    await store.load()
    assert store._cursors == {
        "agent-lore/lithos-loom": datetime(2026, 5, 29, 12, 0, 0, tzinfo=UTC)
    }
    assert store._coord_doc_id == "coord-id"
    assert store._coord_doc_version == 7


async def test_load_cursors_marks_them_as_already_persisted() -> None:
    """A fresh load from the coord doc means the remote already holds
    what we just read — the first poll-cycle's persist must not write
    those cursors back unchanged."""
    body = format_cursors(
        {"agent-lore/lithos-loom": datetime(2026, 5, 29, 12, 0, 0, tzinfo=UTC)}
    )
    note = Note(
        id="coord-id",
        title="GitHub Watcher State",
        body=body,
        version=7,
        updated_at=None,
        tags=(),
        status="active",
        note_type="concept",
        path="projects/_lithos-loom-internal/github-watcher-state.md",
        slug="_lithos-loom-internal",
    )
    lithos = _fake_lithos_client(note_read_return=note)
    store = _make_store(lithos)

    await store.load()
    # Immediate persist must be a no-op — what we'd write equals what's
    # already on disk.
    await store.persist()
    lithos.note_write.assert_not_called()


# ── persist (CAS + tombstones) ──────────────────────────────────────────


async def test_persist_cursors_writes_coord_doc_via_cas() -> None:
    lithos = _fake_lithos_client(
        write_result=WriteResult(
            status="updated",
            note=Note(
                id="coord-id",
                title="GitHub Watcher State",
                body="ignored",
                version=8,
                updated_at=None,
                tags=(),
                status="active",
                note_type="concept",
                path="projects/_lithos-loom-internal/github-watcher-state.md",
                slug="_lithos-loom-internal",
            ),
        )
    )
    store = _make_store(lithos)
    store._coord_doc_id = "coord-id"
    store._coord_doc_version = 7
    store._cursors = {
        "agent-lore/lithos-loom": datetime(2026, 5, 29, 12, 0, 0, tzinfo=UTC)
    }

    await store.persist()

    call = lithos.note_write.await_args
    assert call.kwargs["id"] == "coord-id"
    assert call.kwargs["expected_version"] == 7
    assert "agent-lore/lithos-loom 2026-05-29T12:00:00+00:00" in call.kwargs["content"]
    # Version map advanced to what the write returned.
    assert store._coord_doc_version == 8


async def test_persist_cursors_merges_pending_advances_on_version_conflict() -> None:
    """Regression for PR-review finding 3: a single version_conflict
    used to overwrite ``_cursors`` from the remote and return, dropping
    every cursor advance the current poll observed. The fix merges our
    pending cursors back over the remote view (latest wins per repo),
    then retries the write so the merged cursors actually persist.
    """
    # Remote coord doc holds an older cursor for repo A and an unrelated
    # cursor for repo B (concurrent writer landed for B).
    older_a = datetime(2026, 5, 28, tzinfo=UTC)
    other_b = datetime(2026, 5, 29, 8, 0, 0, tzinfo=UTC)
    remote_body = format_cursors({"owner/a": older_a, "owner/b": other_b})
    remote_note = Note(
        id="coord-id",
        title="GitHub Watcher State",
        body=remote_body,
        version=9,
        updated_at=None,
        tags=(),
        status="active",
        note_type="concept",
        path="projects/_lithos-loom-internal/github-watcher-state.md",
        slug="_lithos-loom-internal",
    )
    # Our just-observed advance for A is later than remote's A; we hold
    # no opinion on B.
    fresher_a = datetime(2026, 5, 29, 12, 0, 0, tzinfo=UTC)

    lithos = _fake_lithos_client(note_read_return=remote_note)
    # First write: conflict. Second write: success.
    final_note = Note(
        id="coord-id",
        title="GitHub Watcher State",
        body="",
        version=10,
        updated_at=None,
        tags=(),
        status="active",
        note_type="concept",
        path="projects/_lithos-loom-internal/github-watcher-state.md",
        slug="_lithos-loom-internal",
    )
    lithos.note_write = AsyncMock(
        side_effect=[
            WriteResult(status="version_conflict", current_version=9),
            WriteResult(status="updated", note=final_note),
        ]
    )

    store = _make_store(lithos)
    store._coord_doc_id = "coord-id"
    store._coord_doc_version = 7
    store._cursors = {"owner/a": fresher_a}

    await store.persist()

    # Second write happened (so cursors actually landed in Lithos).
    assert lithos.note_write.await_count == 2
    second = lithos.note_write.await_args_list[1].kwargs
    # Used the fresh version from the conflict response.
    assert second["expected_version"] == 9
    # Merge: our advance for A wins, remote's B is preserved.
    body_written = second["content"]
    assert f"owner/a {fresher_a.isoformat()}" in body_written
    assert f"owner/b {other_b.isoformat()}" in body_written
    # In-memory cursors reflect the merge.
    assert store._cursors == {"owner/a": fresher_a, "owner/b": other_b}
    assert store._coord_doc_version == 10


async def test_persist_cursors_keeps_stuck_deletions_through_version_conflict() -> None:
    """PR-review finding 3 (round 6, 2026-05-30): a stuck row drained
    locally must stay deleted even when a CAS conflict reloads the
    remote stuck-set that still carries it. Without the per-number
    tombstone, the union-merge re-adds the row from the remote and
    the next write resurrects it.
    """
    T1 = datetime(2026, 5, 29, tzinfo=UTC)
    # Remote coord doc still has stuck entry that we already drained locally.
    remote_body = format_cursors({"owner/x": T1}, stuck={"owner/x": {42, 99}})
    remote_note = Note(
        id="coord-id",
        title="GitHub Watcher State",
        body=remote_body,
        version=9,
        updated_at=None,
        tags=(),
        status="active",
        note_type="concept",
        path="projects/_lithos-loom-internal/github-watcher-state.md",
        slug="_lithos-loom-internal",
    )
    final_note = Note(
        id="coord-id",
        title="GitHub Watcher State",
        body="",
        version=10,
        updated_at=None,
        tags=(),
        status="active",
        note_type="concept",
        path="projects/_lithos-loom-internal/github-watcher-state.md",
        slug="_lithos-loom-internal",
    )
    lithos = _fake_lithos_client(note_read_return=remote_note)
    lithos.note_write = AsyncMock(
        side_effect=[
            WriteResult(status="version_conflict", current_version=9),
            WriteResult(status="updated", note=final_note),
        ]
    )
    store = _make_store(lithos)
    store._coord_doc_id = "coord-id"
    store._coord_doc_version = 7
    store._cursors = {"owner/x": T1}
    store._last_persisted_cursors = {"owner/x": T1}
    # We had {42, 99} stuck persisted; we just drained #42 locally,
    # leaving #99. Remote still carries both.
    store._stuck_issues = {"owner/x": {99}}
    store._last_persisted_stuck = {"owner/x": {42, 99}}

    await store.persist()

    body_written = lithos.note_write.await_args_list[1].kwargs["content"]
    # #42 is gone from the persisted body — the local drain survived
    # the CAS conflict.
    assert "stuck:owner/x#42" not in body_written
    # #99 is still there.
    assert "stuck:owner/x#99" in body_written
    # In-memory matches.
    assert store._stuck_issues == {"owner/x": {99}}


async def test_persist_cursors_keeps_deletions_through_version_conflict() -> None:
    """PR-review finding 1 (round 5, 2026-05-30): a cursor we intend to
    delete must not silently come back when a version_conflict triggers
    reload-then-merge. Without tracking deletion tombstones, the reload
    re-populates ``_cursors`` from the remote (which still contains the
    row we wanted gone) and the next write persists the stale row.

    Scenario: in-memory has dropped repo X (operator disabled watching).
    Remote coord doc still has X→T1. The persist conflicts, reloads X
    back, merges pending (empty) — without the fix, X resurrects.
    """
    T1 = datetime(2026, 5, 28, tzinfo=UTC)
    remote_body = format_cursors({"owner/x": T1})
    remote_note = Note(
        id="coord-id",
        title="GitHub Watcher State",
        body=remote_body,
        version=9,
        updated_at=None,
        tags=(),
        status="active",
        note_type="concept",
        path="projects/_lithos-loom-internal/github-watcher-state.md",
        slug="_lithos-loom-internal",
    )
    final_note = Note(
        id="coord-id",
        title="GitHub Watcher State",
        body="",
        version=10,
        updated_at=None,
        tags=(),
        status="active",
        note_type="concept",
        path="projects/_lithos-loom-internal/github-watcher-state.md",
        slug="_lithos-loom-internal",
    )
    lithos = _fake_lithos_client(note_read_return=remote_note)
    lithos.note_write = AsyncMock(
        side_effect=[
            WriteResult(status="version_conflict", current_version=9),
            WriteResult(status="updated", note=final_note),
        ]
    )
    store = _make_store(lithos)
    store._coord_doc_id = "coord-id"
    store._coord_doc_version = 7
    # Operator just disabled watching for X — in-memory is empty, but the
    # _last_persisted snapshot still carries X (it was persisted earlier).
    store._cursors = {}
    store._last_persisted_cursors = {"owner/x": T1}

    await store.persist()

    # Two writes: first conflicted, second succeeded with X *gone*.
    assert lithos.note_write.await_count == 2
    body_written = lithos.note_write.await_args_list[1].kwargs["content"]
    assert "owner/x" not in body_written
    # In-memory state confirms the deletion stuck.
    assert "owner/x" not in store._cursors


async def test_persist_cursors_gives_up_after_max_cas_attempts() -> None:
    """Three back-to-back conflicts surface a warning and bail without
    spinning forever; the next poll will retry."""
    remote_note = Note(
        id="coord-id",
        title="GitHub Watcher State",
        body="",
        version=9,
        updated_at=None,
        tags=(),
        status="active",
        note_type="concept",
        path="projects/_lithos-loom-internal/github-watcher-state.md",
        slug="_lithos-loom-internal",
    )
    lithos = _fake_lithos_client(note_read_return=remote_note)
    lithos.note_write = AsyncMock(
        return_value=WriteResult(status="version_conflict", current_version=9)
    )
    store = _make_store(lithos)
    store._coord_doc_id = "coord-id"
    store._coord_doc_version = 7
    store._cursors = {"owner/a": datetime(2026, 5, 29, tzinfo=UTC)}

    await store.persist()

    # Exhausted at _MAX_COORD_DOC_CAS_ATTEMPTS=3 attempts, returns cleanly.
    assert lithos.note_write.await_count == 3


async def test_persist_cursors_creates_doc_when_no_id_yet() -> None:
    """First-run path: no _coord_doc_id → write with path= instead of id=."""
    lithos = _fake_lithos_client(
        write_result=WriteResult(
            status="created",
            note=Note(
                id="new-id",
                title="GitHub Watcher State",
                body="",
                version=1,
                updated_at=None,
                tags=(),
                status="active",
                note_type="concept",
                path="projects/_lithos-loom-internal/github-watcher-state.md",
                slug="_lithos-loom-internal",
            ),
        )
    )
    store = _make_store(lithos)
    store._cursors = {"x/y": datetime(2026, 5, 29, tzinfo=UTC)}

    await store.persist()

    call = lithos.note_write.await_args
    expected_path = "projects/_lithos-loom-internal/github-watcher-state.md"
    assert call.kwargs.get("id") is None
    assert call.kwargs["path"] == expected_path
    assert store._coord_doc_id == "new-id"
    assert store._coord_doc_version == 1


async def test_persist_cursors_is_noop_when_unchanged_since_last_write() -> None:
    """Soak 2026-05-29: the watcher was re-writing the coord doc every
    poll regardless of whether any cursor advanced — Lithos version
    crept up minute by minute and fired two SSE note.updated events per
    minute for no benefit. After a successful write, a follow-up persist
    with the same cursor map must skip the write entirely.
    """
    written_note = Note(
        id="coord-id",
        title="GitHub Watcher State",
        body="",
        version=2,
        updated_at=None,
        tags=(),
        status="active",
        note_type="concept",
        path="projects/_lithos-loom-internal/github-watcher-state.md",
        slug="_lithos-loom-internal",
    )
    lithos = _fake_lithos_client(
        write_result=WriteResult(status="updated", note=written_note)
    )
    store = _make_store(lithos)
    store._coord_doc_id = "coord-id"
    store._coord_doc_version = 1
    store._cursors = {"x/y": datetime(2026, 5, 29, tzinfo=UTC)}

    # First persist writes once.
    await store.persist()
    assert lithos.note_write.await_count == 1

    # Second persist with the same cursor map skips the write entirely
    # — no Lithos round-trip, no version bump.
    await store.persist()
    assert lithos.note_write.await_count == 1


async def test_persist_cursors_writes_empty_map_when_slug_removed() -> None:
    """PR-review finding 1 (round 4, 2026-05-30): when the last watched
    slug is disabled, the in-memory cursor map empties — but the coord
    doc still holds the prior cursor rows. Without persisting the
    empty map, a daemon restart re-loads the stale rows; a subsequent
    re-enable resumes from the stale timestamp and can miss issues
    created during the disabled window.
    """
    written_note = Note(
        id="coord-id",
        title="GitHub Watcher State",
        body="",
        version=3,
        updated_at=None,
        tags=(),
        status="active",
        note_type="concept",
        path="projects/_lithos-loom-internal/github-watcher-state.md",
        slug="_lithos-loom-internal",
    )
    lithos = _fake_lithos_client(
        write_result=WriteResult(status="updated", note=written_note)
    )
    store = _make_store(lithos)
    store._coord_doc_id = "coord-id"
    store._coord_doc_version = 2
    # Coord doc had a cursor; the watch list was just cleared, dropping
    # the in-memory cursor too.
    store._last_persisted_cursors = {"x/y": datetime(2026, 5, 29, tzinfo=UTC)}
    store._cursors = {}

    await store.persist()
    # Coord doc rewritten with the empty cursor map — stale row is gone.
    assert lithos.note_write.await_count == 1
    write_kwargs = lithos.note_write.await_args.kwargs
    assert "x/y" not in write_kwargs["content"]


async def test_persist_cursors_writes_again_when_cursor_advances() -> None:
    """After the no-op short-circuit lands, a subsequent cursor advance
    must still trigger a write — otherwise the watcher would silently
    stop persisting after the first poll."""
    note_v2 = Note(
        id="coord-id",
        title="GitHub Watcher State",
        body="",
        version=2,
        updated_at=None,
        tags=(),
        status="active",
        note_type="concept",
        path="projects/_lithos-loom-internal/github-watcher-state.md",
        slug="_lithos-loom-internal",
    )
    note_v3 = Note(
        id="coord-id",
        title="GitHub Watcher State",
        body="",
        version=3,
        updated_at=None,
        tags=(),
        status="active",
        note_type="concept",
        path="projects/_lithos-loom-internal/github-watcher-state.md",
        slug="_lithos-loom-internal",
    )
    lithos = _fake_lithos_client(
        write_result=WriteResult(status="updated", note=note_v2)
    )
    lithos.note_write = AsyncMock(
        side_effect=[
            WriteResult(status="updated", note=note_v2),
            WriteResult(status="updated", note=note_v3),
        ]
    )
    store = _make_store(lithos)
    store._coord_doc_id = "coord-id"
    store._coord_doc_version = 1
    store._cursors = {"x/y": datetime(2026, 5, 29, 10, tzinfo=UTC)}

    await store.persist()
    assert lithos.note_write.await_count == 1

    # Cursor advances → next persist actually writes.
    store._cursors["x/y"] = datetime(2026, 5, 29, 11, tzinfo=UTC)
    await store.persist()
    assert lithos.note_write.await_count == 2


async def test_persist_cursors_skips_retry_when_conflict_resolves_to_unchanged() -> (
    None
):
    """PR-review finding (round 2 on PR #64): the no-op short-circuit
    was at function entry, OUTSIDE the CAS loop. On version_conflict
    the watcher re-reads the remote, merges, then ``continue``s back to
    the top of ``while True`` — bypassing the entry guard. If the
    remote already held the same (or newer) cursors than the watcher
    wanted to write, the merge produced no change, but the retry
    iteration wrote anyway and bumped the coord-doc version. The
    in-loop check at the top of every iteration catches this.
    """
    cursor = datetime(2026, 5, 29, 12, 0, 0, tzinfo=UTC)
    remote_body = format_cursors({"agent-lore/lithos-loom": cursor})
    remote_note = Note(
        id="coord-id",
        title="GitHub Watcher State",
        body=remote_body,
        version=9,
        updated_at=None,
        tags=(),
        status="active",
        note_type="concept",
        path="projects/_lithos-loom-internal/github-watcher-state.md",
        slug="_lithos-loom-internal",
    )
    lithos = _fake_lithos_client(note_read_return=remote_note)
    # First write: version_conflict. If the bug returns, a second write
    # would hit this side_effect list and pass.
    lithos.note_write = AsyncMock(
        side_effect=[
            WriteResult(status="version_conflict", current_version=9),
            WriteResult(status="updated", note=remote_note),
        ]
    )
    store = _make_store(lithos)
    store._coord_doc_id = "coord-id"
    store._coord_doc_version = 7
    # Entry guard would not fire: empty _last_persisted_cursors != our
    # cursor map. Only the in-loop check after the merge can save us.
    store._cursors = {"agent-lore/lithos-loom": cursor}

    await store.persist()

    # Exactly one write: the conflict-then-merge collapsed our pending
    # advance into "no change vs remote", and the retry was skipped.
    assert lithos.note_write.await_count == 1
