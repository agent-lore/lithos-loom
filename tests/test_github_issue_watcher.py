"""Tests for ``lithos_loom.sources.github_issue_watcher``.

The watcher is a polling source; we exercise it by calling its private
loops directly (``_bootstrap`` / ``_poll_one_repo`` / etc) rather than
running ``run()`` to completion. Stubs replace both the github_client
and the Lithos surface so the tests neither hit the network nor depend
on a running Lithos.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from lithos_loom.bus import Event, EventBus
from lithos_loom.cli._github_metadata import GITHUB_REPO_TAG_PREFIX, GITHUB_WATCH_TAG
from lithos_loom.github_client import (
    GitHubAuthError,
    GitHubClient,
    GitHubRepoNotFoundError,
    Issue,
)
from lithos_loom.lithos_client import Note, NoteSummary, WriteResult
from lithos_loom.sources.github_issue_watcher import (
    GITHUB_ISSUE_EVENT_TYPE,
    GitHubIssueWatcher,
    format_cursors,
    parse_cursors,
)

# ── Cursor doc format ─────────────────────────────────────────────────


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


# ── Test plumbing ─────────────────────────────────────────────────────


def _summary(*, slug: str, repo: str | None, watching: bool) -> NoteSummary:
    tags: list[str] = ["project-context"]
    if repo is not None:
        tags.append(f"{GITHUB_REPO_TAG_PREFIX}{repo}")
    if watching:
        tags.append(GITHUB_WATCH_TAG)
    return NoteSummary(
        id=f"doc-{slug}",
        title=slug.title(),
        version=1,
        updated_at=datetime(2026, 5, 29, 12, 0, 0, tzinfo=UTC),
        tags=tuple(tags),
        status="active",
        note_type="concept",
        path=f"projects/{slug}/{slug}-project-context.md",
        slug=slug,
    )


def _make_issue(
    *,
    number: int = 1,
    repo: str = "agent-lore/lithos-loom",
    state: str = "open",
    state_reason: str | None = None,
    updated_at: datetime | None = None,
) -> Issue:
    return Issue(
        repo=repo,
        number=number,
        title=f"Issue {number}",
        body="body",
        state=state,
        state_reason=state_reason,
        labels=("bug",),
        author="alice",
        updated_at=updated_at or datetime(2026, 5, 29, 12, 0, 0, tzinfo=UTC),
        html_url=f"https://github.com/{repo}/issues/{number}",
    )


def _fake_github_client() -> Any:
    """An AsyncMock shaped like the GitHubClient surface the watcher uses."""
    gh = AsyncMock()
    gh.list_issues_since = AsyncMock(return_value=[])
    return gh


def _fake_lithos_client(
    *,
    note_list_return: list[NoteSummary] | None = None,
    note_read_return: Note | None = None,
    write_result: WriteResult | None = None,
) -> Any:
    client = AsyncMock()
    client.note_list = AsyncMock(return_value=note_list_return or [])
    client.note_read = AsyncMock(return_value=note_read_return)
    client.note_write = AsyncMock(
        return_value=write_result or WriteResult(status="updated")
    )
    return client


def _make_watcher(
    *,
    github: Any,
    lithos: Any,
    bus: EventBus | None = None,
) -> GitHubIssueWatcher:
    return GitHubIssueWatcher(
        github=cast(GitHubClient, github),
        lithos=lithos,
        bus=bus or EventBus(),
        poll_interval_seconds=60,
        coord_doc_path="projects/_lithos-loom-internal/github-watcher-state.md",
        agent_id="test-agent",
    )


async def _drain(bus: EventBus, queue_size: int = 64) -> list[Event]:
    """Subscribe broadly and drain whatever's queued (testing util)."""
    sub = bus.subscribe(
        event_types=(GITHUB_ISSUE_EVENT_TYPE,),
        queue_size=queue_size,
    )
    out: list[Event] = []
    while not sub.queue.empty():
        out.append(sub.queue.get_nowait())
    return out


# ── _refresh_watch_list ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_refresh_watch_list_picks_up_watched_projects() -> None:
    lithos = _fake_lithos_client(
        note_list_return=[
            _summary(slug="lithos-loom", repo="agent-lore/lithos-loom", watching=True),
            _summary(slug="lithos", repo="agent-lore/lithos", watching=True),
        ]
    )
    watcher = _make_watcher(github=_fake_github_client(), lithos=lithos)
    await watcher._refresh_watch_list()
    assert watcher._watch_list == {
        "lithos-loom": "agent-lore/lithos-loom",
        "lithos": "agent-lore/lithos",
    }
    # Watch-tag filter actually flows into the query.
    call = lithos.note_list.await_args
    assert call.kwargs["path_prefix"] == "projects/"
    assert GITHUB_WATCH_TAG in call.kwargs["tags"]


@pytest.mark.asyncio
async def test_refresh_watch_list_skips_projects_without_repo_tag() -> None:
    """Operator drift: an enabled doc lacking a repo tag is logged + skipped."""
    lithos = _fake_lithos_client(
        note_list_return=[
            _summary(slug="lithos-loom", repo=None, watching=True),
            _summary(slug="lithos", repo="agent-lore/lithos", watching=True),
        ]
    )
    watcher = _make_watcher(github=_fake_github_client(), lithos=lithos)
    await watcher._refresh_watch_list()
    assert watcher._watch_list == {"lithos": "agent-lore/lithos"}


@pytest.mark.asyncio
async def test_refresh_watch_list_preserves_state_on_transport_failure() -> None:
    """Refresh failure shouldn't blank the watch list operators rely on."""
    lithos = _fake_lithos_client(
        note_list_return=[
            _summary(slug="lithos-loom", repo="agent-lore/lithos-loom", watching=True),
        ]
    )
    watcher = _make_watcher(github=_fake_github_client(), lithos=lithos)
    await watcher._refresh_watch_list()
    assert watcher._watch_list == {"lithos-loom": "agent-lore/lithos-loom"}
    # Second call raises transport error.
    lithos.note_list.side_effect = OSError("connection refused")
    await watcher._refresh_watch_list()
    # State preserved.
    assert watcher._watch_list == {"lithos-loom": "agent-lore/lithos-loom"}


# ── _load_cursors_from_coord_doc ──────────────────────────────────────


@pytest.mark.asyncio
async def test_load_cursors_missing_doc_treats_as_first_run() -> None:
    lithos = _fake_lithos_client(note_read_return=None)
    watcher = _make_watcher(github=_fake_github_client(), lithos=lithos)
    await watcher._load_cursors_from_coord_doc()
    assert watcher._cursors == {}
    assert watcher._coord_doc_id is None


@pytest.mark.asyncio
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
    watcher = _make_watcher(github=_fake_github_client(), lithos=lithos)
    await watcher._load_cursors_from_coord_doc()
    assert watcher._cursors == {
        "agent-lore/lithos-loom": datetime(2026, 5, 29, 12, 0, 0, tzinfo=UTC)
    }
    assert watcher._coord_doc_id == "coord-id"
    assert watcher._coord_doc_version == 7


# ── _poll_one_repo ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_poll_one_repo_publishes_issue_events() -> None:
    bus = EventBus()
    sub = bus.subscribe(event_types=(GITHUB_ISSUE_EVENT_TYPE,), queue_size=16)
    issue = _make_issue(number=42)
    github = _fake_github_client()
    github.list_issues_since = AsyncMock(return_value=[issue])
    watcher = _make_watcher(github=github, lithos=_fake_lithos_client(), bus=bus)
    watcher._watch_list = {"lithos-loom": "agent-lore/lithos-loom"}

    await watcher._poll_one_repo(slug="lithos-loom", repo="agent-lore/lithos-loom")

    assert sub.queue.qsize() == 1
    event = sub.queue.get_nowait()
    assert event.type == GITHUB_ISSUE_EVENT_TYPE
    assert event.payload["slug"] == "lithos-loom"
    assert event.payload["repo"] == "agent-lore/lithos-loom"
    assert event.payload["number"] == 42
    # Cursor advanced to the issue's updated_at.
    assert watcher._cursors["agent-lore/lithos-loom"] == issue.updated_at


@pytest.mark.asyncio
async def test_poll_one_repo_bootstrap_uses_state_open() -> None:
    """Regression for PR-review finding (round 3): without a cursor,
    bootstrap must list open issues only — using state=all means the
    paginated listing leads with the oldest closed history and the
    watcher spends multiple poll cycles burning through historic
    closures before reaching live open issues, breaking PRD US-56.
    """
    bus = EventBus()
    bus.subscribe(event_types=(GITHUB_ISSUE_EVENT_TYPE,), queue_size=16)
    github = _fake_github_client()
    github.list_issues_since = AsyncMock(return_value=[])
    watcher = _make_watcher(github=github, lithos=_fake_lithos_client(), bus=bus)
    # No cursor for the repo → bootstrap path.

    await watcher._poll_one_repo(slug="x", repo="agent-lore/lithos-loom")

    call = github.list_issues_since.await_args
    assert call is not None
    assert call.kwargs["since"] is None
    assert call.kwargs["state"] == "open"


@pytest.mark.asyncio
async def test_poll_one_repo_incremental_uses_state_all() -> None:
    """With a cursor present, the poll must use state=all so state
    transitions (open→closed) on previously-seen issues surface."""
    bus = EventBus()
    bus.subscribe(event_types=(GITHUB_ISSUE_EVENT_TYPE,), queue_size=16)
    github = _fake_github_client()
    github.list_issues_since = AsyncMock(return_value=[])
    watcher = _make_watcher(github=github, lithos=_fake_lithos_client(), bus=bus)
    watcher._cursors["agent-lore/lithos-loom"] = datetime(2026, 5, 29, tzinfo=UTC)

    await watcher._poll_one_repo(slug="x", repo="agent-lore/lithos-loom")

    call = github.list_issues_since.await_args
    assert call is not None
    assert call.kwargs["state"] == "all"


@pytest.mark.asyncio
async def test_poll_one_repo_surfaces_closed_issue_state_to_handler() -> None:
    """Regression for PR-review finding 1: the source was hard-coded to
    state="open", so close events never reached the subscription handler
    and the GH→Lithos close mirror was effectively unimplemented.

    The watcher must surface state="closed" issues (with their
    state_reason) so the handler can drive task_complete / task_cancel.
    Incremental poll path (cursor present), which uses state="all".
    """
    bus = EventBus()
    sub = bus.subscribe(event_types=(GITHUB_ISSUE_EVENT_TYPE,), queue_size=16)
    closed = _make_issue(number=99, state="closed", state_reason="completed")
    github = _fake_github_client()
    github.list_issues_since = AsyncMock(return_value=[closed])
    watcher = _make_watcher(github=github, lithos=_fake_lithos_client(), bus=bus)
    # Cursor present → incremental (state="all") path, which is where
    # closes naturally surface.
    watcher._cursors["agent-lore/lithos-loom"] = datetime(2026, 5, 29, tzinfo=UTC)

    await watcher._poll_one_repo(slug="x", repo="agent-lore/lithos-loom")

    assert sub.queue.qsize() == 1
    event = sub.queue.get_nowait()
    assert event.payload["state"] == "closed"
    assert event.payload["state_reason"] == "completed"


@pytest.mark.asyncio
async def test_poll_one_repo_advances_cursor_to_latest_when_multiple_issues() -> None:
    bus = EventBus()
    bus.subscribe(event_types=(GITHUB_ISSUE_EVENT_TYPE,), queue_size=16)
    early = _make_issue(
        number=1, updated_at=datetime(2026, 5, 29, 10, 0, 0, tzinfo=UTC)
    )
    late = _make_issue(number=2, updated_at=datetime(2026, 5, 29, 13, 0, 0, tzinfo=UTC))
    github = _fake_github_client()
    github.list_issues_since = AsyncMock(return_value=[early, late])
    watcher = _make_watcher(github=github, lithos=_fake_lithos_client(), bus=bus)

    await watcher._poll_one_repo(slug="x", repo="agent-lore/lithos-loom")
    assert watcher._cursors["agent-lore/lithos-loom"] == late.updated_at


@pytest.mark.asyncio
async def test_poll_one_repo_uses_existing_cursor_as_since_param() -> None:
    bus = EventBus()
    bus.subscribe(event_types=(GITHUB_ISSUE_EVENT_TYPE,), queue_size=16)
    github = _fake_github_client()
    github.list_issues_since = AsyncMock(return_value=[])
    watcher = _make_watcher(github=github, lithos=_fake_lithos_client(), bus=bus)
    prior = datetime(2026, 5, 29, 8, 0, 0, tzinfo=UTC)
    watcher._cursors["agent-lore/lithos-loom"] = prior

    await watcher._poll_one_repo(slug="x", repo="agent-lore/lithos-loom")
    call = github.list_issues_since.await_args
    assert call is not None
    assert call.kwargs["since"] == prior


@pytest.mark.asyncio
async def test_poll_one_repo_drops_repo_on_404() -> None:
    """D49: an unmapped/missing repo drops + logs a [Friction] line."""
    github = _fake_github_client()
    github.list_issues_since = AsyncMock(
        side_effect=GitHubRepoNotFoundError("missing/repo")
    )
    bus = EventBus()
    watcher = _make_watcher(github=github, lithos=_fake_lithos_client(), bus=bus)
    watcher._watch_list = {
        "ghost": "missing/repo",
        "real": "agent-lore/lithos-loom",
    }
    watcher._cursors["missing/repo"] = datetime(2026, 5, 28, tzinfo=UTC)

    await watcher._poll_one_repo(slug="ghost", repo="missing/repo")

    assert "ghost" not in watcher._watch_list
    assert "missing/repo" not in watcher._cursors
    # Sibling project untouched.
    assert "real" in watcher._watch_list


@pytest.mark.asyncio
async def test_poll_one_repo_swallows_auth_error() -> None:
    github = _fake_github_client()
    github.list_issues_since = AsyncMock(
        side_effect=GitHubAuthError("401 Bad credentials")
    )
    bus = EventBus()
    watcher = _make_watcher(github=github, lithos=_fake_lithos_client(), bus=bus)

    # Should not raise.
    await watcher._poll_one_repo(slug="x", repo="agent-lore/lithos-loom")
    # No events published.
    events = await _drain(bus)
    assert events == []


# ── _persist_cursors ──────────────────────────────────────────────────


@pytest.mark.asyncio
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
    watcher = _make_watcher(github=_fake_github_client(), lithos=lithos)
    watcher._coord_doc_id = "coord-id"
    watcher._coord_doc_version = 7
    watcher._cursors = {
        "agent-lore/lithos-loom": datetime(2026, 5, 29, 12, 0, 0, tzinfo=UTC)
    }

    await watcher._persist_cursors()

    call = lithos.note_write.await_args
    assert call.kwargs["id"] == "coord-id"
    assert call.kwargs["expected_version"] == 7
    assert "agent-lore/lithos-loom 2026-05-29T12:00:00+00:00" in call.kwargs["content"]
    # Version map advanced to what the write returned.
    assert watcher._coord_doc_version == 8


@pytest.mark.asyncio
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

    watcher = _make_watcher(github=_fake_github_client(), lithos=lithos)
    watcher._coord_doc_id = "coord-id"
    watcher._coord_doc_version = 7
    watcher._cursors = {"owner/a": fresher_a}

    await watcher._persist_cursors()

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
    assert watcher._cursors == {"owner/a": fresher_a, "owner/b": other_b}
    assert watcher._coord_doc_version == 10


@pytest.mark.asyncio
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
    watcher = _make_watcher(github=_fake_github_client(), lithos=lithos)
    watcher._coord_doc_id = "coord-id"
    watcher._coord_doc_version = 7
    watcher._cursors = {"owner/a": datetime(2026, 5, 29, tzinfo=UTC)}

    await watcher._persist_cursors()

    # Exhausted at _MAX_COORD_DOC_CAS_ATTEMPTS=3 attempts, returns cleanly.
    assert lithos.note_write.await_count == 3


@pytest.mark.asyncio
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
    watcher = _make_watcher(github=_fake_github_client(), lithos=lithos)
    watcher._cursors = {"x/y": datetime(2026, 5, 29, tzinfo=UTC)}

    await watcher._persist_cursors()

    call = lithos.note_write.await_args
    expected_path = "projects/_lithos-loom-internal/github-watcher-state.md"
    assert call.kwargs.get("id") is None
    assert call.kwargs["path"] == expected_path
    assert watcher._coord_doc_id == "new-id"
    assert watcher._coord_doc_version == 1


# ── End-to-end: bootstrap + one poll cycle ────────────────────────────


@pytest.mark.asyncio
async def test_bootstrap_loads_watch_list_and_subscribes_bus() -> None:
    bus = EventBus()
    lithos = _fake_lithos_client(
        note_list_return=[
            _summary(slug="x", repo="agent-lore/x", watching=True),
        ],
        note_read_return=None,
    )
    watcher = _make_watcher(github=_fake_github_client(), lithos=lithos, bus=bus)
    await watcher._bootstrap()
    assert watcher._watch_list == {"x": "agent-lore/x"}
    assert watcher._coord_doc_subscription is not None
    # Subscribed to lithos.note.* events.
    assert watcher._coord_doc_subscription.event_types == frozenset(
        {"lithos.note.created", "lithos.note.updated"}
    )


@pytest.mark.asyncio
async def test_refresh_loop_reacts_to_project_doc_changes() -> None:
    """Publishing a lithos.note.updated for a project path triggers refresh."""
    bus = EventBus()
    lithos = _fake_lithos_client(
        note_list_return=[
            _summary(slug="x", repo="agent-lore/x", watching=True),
        ]
    )
    watcher = _make_watcher(github=_fake_github_client(), lithos=lithos, bus=bus)
    await watcher._bootstrap()
    # Two refresh calls so far: one at bootstrap.
    initial_count = lithos.note_list.await_count

    # Publish a relevant event.
    await bus.publish(
        Event(
            type="lithos.note.updated",
            timestamp=datetime(2026, 5, 29, tzinfo=UTC),
            payload={"id": "doc-1", "path": "projects/y/y-project-context.md"},
        )
    )
    # Drain one event by running the refresh loop until it processes one.
    import asyncio

    task = asyncio.create_task(watcher._refresh_loop())
    # Yield until the loop processes the queued event.
    for _ in range(10):
        await asyncio.sleep(0)
        if lithos.note_list.await_count > initial_count:
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert lithos.note_list.await_count > initial_count


@pytest.mark.asyncio
async def test_refresh_loop_ignores_unrelated_events() -> None:
    bus = EventBus()
    lithos = _fake_lithos_client()
    watcher = _make_watcher(github=_fake_github_client(), lithos=lithos, bus=bus)
    await watcher._bootstrap()
    initial_count = lithos.note_list.await_count

    # Non-projects path → no refresh.
    await bus.publish(
        Event(
            type="lithos.note.updated",
            timestamp=datetime(2026, 5, 29, tzinfo=UTC),
            payload={"id": "doc-1", "path": "notes/unrelated.md"},
        )
    )
    import asyncio

    task = asyncio.create_task(watcher._refresh_loop())
    # Give it a few ticks. If it triggers, the count would change.
    for _ in range(10):
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert lithos.note_list.await_count == initial_count


@pytest.mark.asyncio
async def test_refresh_loop_ignores_coord_doc_writes() -> None:
    """Self-write protection: an event for the coord doc itself doesn't loop."""
    bus = EventBus()
    coord_path = "projects/_lithos-loom-internal/github-watcher-state.md"
    lithos = _fake_lithos_client()
    watcher = _make_watcher(github=_fake_github_client(), lithos=lithos, bus=bus)
    await watcher._bootstrap()
    initial_count = lithos.note_list.await_count

    await bus.publish(
        Event(
            type="lithos.note.updated",
            timestamp=datetime(2026, 5, 29, tzinfo=UTC),
            payload={"id": "coord", "path": coord_path},
        )
    )
    import asyncio

    task = asyncio.create_task(watcher._refresh_loop())
    for _ in range(10):
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert lithos.note_list.await_count == initial_count


@pytest.mark.asyncio
async def test_poll_all_repos_iterates_watch_list() -> None:
    bus = EventBus()
    sub = bus.subscribe(event_types=(GITHUB_ISSUE_EVENT_TYPE,), queue_size=16)
    github = _fake_github_client()

    def fake_list(
        repo: str, *, since: datetime | None, state: str = "all"
    ) -> list[Issue]:
        return [_make_issue(number=1, repo=repo)]

    github.list_issues_since = AsyncMock(side_effect=fake_list)
    watcher = _make_watcher(github=github, lithos=_fake_lithos_client(), bus=bus)
    watcher._watch_list = {
        "a": "owner/a",
        "b": "owner/b",
    }

    await watcher._poll_all_repos()

    assert github.list_issues_since.await_count == 2
    assert sub.queue.qsize() == 2


@pytest.mark.asyncio
async def test_poll_loop_persists_cursors_after_pass() -> None:
    """After a polling pass with new issues, the coord doc gets written."""
    bus = EventBus()
    bus.subscribe(event_types=(GITHUB_ISSUE_EVENT_TYPE,), queue_size=16)
    github = _fake_github_client()
    github.list_issues_since = AsyncMock(
        return_value=[_make_issue(number=1, repo="owner/a")]
    )
    lithos = _fake_lithos_client(
        write_result=WriteResult(
            status="created",
            note=Note(
                id="new",
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

    # Make _sleep raise after the first pass so we exit the loop.
    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        raise StopAsyncIteration

    watcher = _make_watcher(github=github, lithos=lithos, bus=bus)
    watcher._sleep = fake_sleep
    watcher._watch_list = {"a": "owner/a"}

    with pytest.raises(StopAsyncIteration):
        await watcher._poll_loop()

    # Coord doc was persisted after the pass.
    assert lithos.note_write.await_count == 1
    # The cursor was set, and the doc body reflects it.
    body = lithos.note_write.await_args.kwargs["content"]
    assert "owner/a" in body


@pytest.mark.asyncio
async def test_poll_loop_skips_cursor_write_when_no_cursors() -> None:
    """First poll with empty watch list — nothing to persist, no write."""
    bus = EventBus()
    github = _fake_github_client()
    lithos = _fake_lithos_client()

    async def fake_sleep(seconds: float) -> None:
        raise StopAsyncIteration

    watcher = _make_watcher(github=github, lithos=lithos, bus=bus)
    watcher._sleep = fake_sleep

    with pytest.raises(StopAsyncIteration):
        await watcher._poll_loop()

    lithos.note_write.assert_not_awaited()


@pytest.mark.asyncio
async def test_poll_loop_backs_off_on_exception() -> None:
    """A poll-cycle exception triggers exponential backoff, not source death."""
    bus = EventBus()
    github = _fake_github_client()
    lithos = _fake_lithos_client()
    # Force a crash inside the poll pass.
    lithos.note_write.side_effect = RuntimeError("boom")
    watcher = _make_watcher(github=github, lithos=lithos, bus=bus)
    watcher._watch_list = {"a": "owner/a"}
    watcher._cursors = {"owner/a": datetime(2026, 5, 29, tzinfo=UTC)}

    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        # Stop after the first backoff sleep.
        if len(sleep_calls) >= 1:
            raise StopAsyncIteration

    watcher._sleep = fake_sleep

    with pytest.raises(StopAsyncIteration):
        await watcher._poll_loop()

    # The backoff sleep ran (1.0s by default).
    assert len(sleep_calls) >= 1
    assert sleep_calls[0] == pytest.approx(1.0)


# ── Edge: coord doc subscription queue size ───────────────────────────


def test_event_type_constant_is_namespaced() -> None:
    """``github.issue.seen`` is the bus contract; subscription handler binds to it."""
    assert GITHUB_ISSUE_EVENT_TYPE == "github.issue.seen"


def test_cursor_format_handles_future_timestamps() -> None:
    """No special-casing for issues from the future (clock skew) — just round-trip."""
    future = datetime(2030, 1, 1, tzinfo=UTC) + timedelta(seconds=1)
    assert parse_cursors(format_cursors({"x/y": future})) == {"x/y": future}
