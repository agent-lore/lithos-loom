"""Tests for ``lithos_loom.sources.github_issue_watcher``.

The watcher is a polling source; we exercise it by calling its private
loops directly (``_bootstrap`` / ``_poll_one_repo`` / etc) rather than
running ``run()`` to completion. Stubs replace both the github_client
and the Lithos surface so the tests neither hit the network nor depend
on a running Lithos.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from lithos_loom.bus import Event, EventBus
from lithos_loom.cli._github_metadata import (
    GITHUB_EXCLUDE_AUTHORS_KEY,
    GITHUB_EXCLUDE_LABELS_KEY,
    GITHUB_REPOS_KEY,
    GITHUB_WATCH_KEY,
)
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
    WatchedRepo,
)

# ── Test plumbing ─────────────────────────────────────────────────────


def _summary(
    *,
    slug: str,
    repo: str | None = None,
    repos: tuple[str, ...] | None = None,
    watching: bool,
    exclude_labels: tuple[str, ...] = (),
    exclude_authors: tuple[str, ...] = (),
) -> NoteSummary:
    """Build a project-context ``NoteSummary`` carrying github-watcher
    config in metadata. Pass ``repo`` for the single-repo case or
    ``repos`` for a project tracking several."""
    if repos is not None:
        repo_list = list(repos)
    elif repo is not None:
        repo_list = [repo]
    else:
        repo_list = []
    metadata: dict[str, Any] = {GITHUB_WATCH_KEY: watching}
    if repo_list:
        metadata[GITHUB_REPOS_KEY] = repo_list
    if exclude_labels:
        metadata[GITHUB_EXCLUDE_LABELS_KEY] = list(exclude_labels)
    if exclude_authors:
        metadata[GITHUB_EXCLUDE_AUTHORS_KEY] = list(exclude_authors)
    return NoteSummary(
        id=f"doc-{slug}",
        title=slug.title(),
        version=1,
        updated_at=datetime(2026, 5, 29, 12, 0, 0, tzinfo=UTC),
        tags=("project-context",),
        status="active",
        note_type="concept",
        path=f"projects/{slug}/{slug}-project-context.md",
        slug=slug,
        metadata=metadata,
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
    dispatch: Any = None,
) -> GitHubIssueWatcher:
    # dispatch is required (#234); tests that don't assert on dispatched
    # events get a recording no-op so cursor / coord-doc behaviour still
    # exercises the inline path.
    return GitHubIssueWatcher(
        github=cast(GitHubClient, github),
        lithos=lithos,
        bus=bus or EventBus(),
        poll_interval_seconds=60,
        coord_doc_path="projects/_lithos-loom-internal/github-watcher-state.md",
        agent_id="test-agent",
        dispatch=dispatch or _CapturingDispatch(),
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


class _CapturingDispatch:
    """Inline dispatcher that records the events it is handed.

    Replaces the removed bus-publish fallback (#234): production always
    injects a real dispatcher, so the watcher tests inject this and assert
    on ``.events`` rather than on the in-process bus queue.
    """

    def __init__(self) -> None:
        self.events: list[Event] = []

    async def __call__(self, event: Event) -> None:
        self.events.append(event)


# ── construction contract ─────────────────────────────────────────────


def test_watcher_requires_a_dispatch() -> None:
    """``dispatch`` is a required dependency (#234). Production wiring
    always injects one; the test-only ``dispatch is None`` → bus-publish
    fallback was removed, so constructing without a dispatch is an error.
    """
    kwargs: dict[str, Any] = {
        "github": cast(GitHubClient, _fake_github_client()),
        "lithos": _fake_lithos_client(),
        "bus": EventBus(),
        "poll_interval_seconds": 60,
        "coord_doc_path": "projects/_lithos-loom-internal/github-watcher-state.md",
        "agent_id": "test-agent",
    }
    with pytest.raises(TypeError):
        GitHubIssueWatcher(**kwargs)  # dispatch omitted


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
        "lithos-loom": WatchedRepo(repos=("agent-lore/lithos-loom",)),
        "lithos": WatchedRepo(repos=("agent-lore/lithos",)),
    }
    # The watch-enabled metadata filter flows into the query.
    call = lithos.note_list.await_args
    assert call.kwargs["path_prefix"] == "projects/"
    assert call.kwargs["metadata_match"] == {GITHUB_WATCH_KEY: True}


@pytest.mark.asyncio
async def test_refresh_watch_list_skips_projects_without_repos() -> None:
    """Operator drift: an enabled doc with no github_repos is logged + skipped."""
    lithos = _fake_lithos_client(
        note_list_return=[
            _summary(slug="lithos-loom", repo=None, watching=True),
            _summary(slug="lithos", repo="agent-lore/lithos", watching=True),
        ]
    )
    watcher = _make_watcher(github=_fake_github_client(), lithos=lithos)
    await watcher._refresh_watch_list()
    assert watcher._watch_list == {"lithos": WatchedRepo(repos=("agent-lore/lithos",))}


@pytest.mark.asyncio
async def test_refresh_watch_list_maps_multiple_repos_per_project() -> None:
    """A project may track several repos; all land in one WatchedRepo and
    each is polled independently."""
    lithos = _fake_lithos_client(
        note_list_return=[
            _summary(
                slug="kindred-code",
                repos=("kindred/web", "kindred/api", "kindred/infra"),
                watching=True,
            ),
        ]
    )
    issue = _make_issue(number=7)
    github = _fake_github_client()
    github.list_issues_since = AsyncMock(return_value=[issue])
    watcher = _make_watcher(github=github, lithos=lithos)
    await watcher._refresh_watch_list()
    assert watcher._watch_list == {
        "kindred-code": WatchedRepo(
            repos=("kindred/web", "kindred/api", "kindred/infra")
        )
    }
    # The poll cycle fans out to one fetch per repo.
    await watcher._poll_all_repos()
    polled = {call.args[0] for call in github.list_issues_since.await_args_list}
    assert polled == {"kindred/web", "kindred/api", "kindred/infra"}


@pytest.mark.asyncio
async def test_refresh_resets_cursor_when_exclude_filter_changes() -> None:
    """PR-review finding 5 (round 3, 2026-05-30): when the operator
    relaxes a ``github_exclude_labels`` entry, the watcher must drop the
    repo cursor so previously-skipped issues re-surface. Otherwise the
    cursor sits past their ``updated_at`` and the next poll won't see
    them until someone edits them on GitHub.
    """
    lithos = _fake_lithos_client(
        note_list_return=[
            _summary(
                slug="lithos-loom",
                repo="agent-lore/lithos-loom",
                watching=True,
                exclude_labels=("automated",),
            )
        ]
    )
    watcher = _make_watcher(github=_fake_github_client(), lithos=lithos)
    await watcher._refresh_watch_list()
    # Watcher polled for a while; cursor is set.
    watcher._store._cursors["agent-lore/lithos-loom"] = datetime(
        2026, 5, 29, tzinfo=UTC
    )

    # Operator removes the exclude tag.
    lithos.note_list = AsyncMock(
        return_value=[
            _summary(
                slug="lithos-loom",
                repo="agent-lore/lithos-loom",
                watching=True,
                exclude_labels=(),
            )
        ]
    )
    await watcher._refresh_watch_list()

    # Cursor reset — next poll bootstrap-walks open issues so the
    # previously-excluded ones surface.
    assert "agent-lore/lithos-loom" not in watcher._store._cursors


@pytest.mark.asyncio
async def test_refresh_resets_cursor_when_repo_unwatched_and_rewatched() -> None:
    """Removing the watch enrolment and re-adding it later must not
    silently resume from the stale cursor — operator might have meant
    a clean re-bootstrap."""
    lithos = _fake_lithos_client(
        note_list_return=[
            _summary(slug="lithos-loom", repo="agent-lore/lithos-loom", watching=True)
        ]
    )
    watcher = _make_watcher(github=_fake_github_client(), lithos=lithos)
    await watcher._refresh_watch_list()
    watcher._store._cursors["agent-lore/lithos-loom"] = datetime(
        2026, 5, 29, tzinfo=UTC
    )

    # Disable watching.
    lithos.note_list = AsyncMock(return_value=[])
    await watcher._refresh_watch_list()
    assert "agent-lore/lithos-loom" not in watcher._store._cursors


@pytest.mark.asyncio
async def test_refresh_adding_sibling_repo_keeps_existing_cursor() -> None:
    """Adding a second repo to a project must NOT reset the cursor of the
    repo it already tracks — only the newly-added repo bootstraps."""
    lithos = _fake_lithos_client(
        note_list_return=[
            _summary(slug="kindred-code", repos=("kindred/web",), watching=True)
        ]
    )
    watcher = _make_watcher(github=_fake_github_client(), lithos=lithos)
    await watcher._refresh_watch_list()
    watcher._store._cursors["kindred/web"] = datetime(2026, 5, 29, tzinfo=UTC)

    # Operator adds a sibling repo to the same project.
    lithos.note_list = AsyncMock(
        return_value=[
            _summary(
                slug="kindred-code",
                repos=("kindred/web", "kindred/api"),
                watching=True,
            )
        ]
    )
    await watcher._refresh_watch_list()

    # Existing repo's cursor is untouched; the new repo has none yet.
    assert watcher._store._cursors["kindred/web"] == datetime(2026, 5, 29, tzinfo=UTC)
    assert "kindred/api" not in watcher._store._cursors


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
    assert watcher._watch_list == {
        "lithos-loom": WatchedRepo(repos=("agent-lore/lithos-loom",))
    }
    # Second call raises transport error.
    lithos.note_list.side_effect = OSError("connection refused")
    await watcher._refresh_watch_list()
    # State preserved.
    assert watcher._watch_list == {
        "lithos-loom": WatchedRepo(repos=("agent-lore/lithos-loom",))
    }


@pytest.mark.asyncio
@pytest.mark.asyncio
# ── _poll_one_repo ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_poll_one_repo_dispatches_issue_events() -> None:
    dispatch = _CapturingDispatch()
    issue = _make_issue(number=42)
    github = _fake_github_client()
    github.list_issues_since = AsyncMock(return_value=[issue])
    watcher = _make_watcher(
        github=github, lithos=_fake_lithos_client(), dispatch=dispatch
    )
    watcher._watch_list = {
        "lithos-loom": WatchedRepo(repos=("agent-lore/lithos-loom",))
    }

    await watcher._poll_one_repo(slug="lithos-loom", repo="agent-lore/lithos-loom")

    assert len(dispatch.events) == 1
    event = dispatch.events[0]
    assert event.type == GITHUB_ISSUE_EVENT_TYPE
    assert event.payload["slug"] == "lithos-loom"
    assert event.payload["repo"] == "agent-lore/lithos-loom"
    assert event.payload["number"] == 42
    # Cursor sits exactly at the boundary issue's updated_at; the +1s
    # nudge that used to live here was removed because it silently
    # dropped same-second sibling updates (PR-review finding 3,
    # 2026-05-30). Idempotent replay is the safer tradeoff.
    assert watcher._store._cursors["agent-lore/lithos-loom"] == issue.updated_at


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
    watcher._store._cursors["agent-lore/lithos-loom"] = datetime(
        2026, 5, 29, tzinfo=UTC
    )

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
    dispatch = _CapturingDispatch()
    closed = _make_issue(number=99, state="closed", state_reason="completed")
    github = _fake_github_client()
    github.list_issues_since = AsyncMock(return_value=[closed])
    watcher = _make_watcher(
        github=github, lithos=_fake_lithos_client(), dispatch=dispatch
    )
    # Cursor present → incremental (state="all") path, which is where
    # closes naturally surface.
    watcher._store._cursors["agent-lore/lithos-loom"] = datetime(
        2026, 5, 29, tzinfo=UTC
    )

    await watcher._poll_one_repo(slug="x", repo="agent-lore/lithos-loom")

    assert len(dispatch.events) == 1
    event = dispatch.events[0]
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
    # PR-review finding 3 (2026-05-30): cursor is exactly max(updated_at).
    # The earlier +1s nudge silently dropped any *other* issue updated
    # within the same wall second; correctness beats one extra idempotent
    # task_list call.
    assert watcher._store._cursors["agent-lore/lithos-loom"] == late.updated_at


@pytest.mark.asyncio
async def test_poll_one_repo_holds_cursor_when_dispatch_fails_mid_batch() -> None:
    """PR-review finding 1 (2026-05-30): with the bus path, a queue-full
    drop or a handler exception silently advanced the cursor past
    issues that were never reconciled. With an injected inline
    dispatcher the watcher walks issues in updated_at-asc order and
    holds the cursor at the last successfully dispatched issue's
    timestamp; the failed issue's updated_at is re-fetched next poll.
    """
    bus = EventBus()
    bus.subscribe(event_types=(GITHUB_ISSUE_EVENT_TYPE,), queue_size=16)
    first = _make_issue(
        number=1, updated_at=datetime(2026, 5, 29, 10, 0, 0, tzinfo=UTC)
    )
    second = _make_issue(
        number=2, updated_at=datetime(2026, 5, 29, 11, 0, 0, tzinfo=UTC)
    )
    third = _make_issue(
        number=3, updated_at=datetime(2026, 5, 29, 12, 0, 0, tzinfo=UTC)
    )
    github = _fake_github_client()
    github.list_issues_since = AsyncMock(return_value=[first, second, third])

    dispatched: list[int] = []

    async def flaky_dispatch(event: Any) -> None:
        n = event.payload["number"]
        dispatched.append(n)
        if n == 2:
            raise RuntimeError("Lithos went away")

    watcher = _make_watcher(
        github=github,
        lithos=_fake_lithos_client(),
        bus=bus,
        dispatch=flaky_dispatch,
    )

    await watcher._poll_one_repo(slug="x", repo="agent-lore/lithos-loom")

    # Issue 1 dispatched, issue 2 failed → loop stopped; issue 3 never tried.
    assert dispatched == [1, 2]
    # Cursor sits at the latest successful issue (1), not the failed one
    # (2) and not the latest seen (3) — next poll re-fetches 2 onward.
    assert watcher._store._cursors["agent-lore/lithos-loom"] == first.updated_at


@pytest.mark.asyncio
async def test_poll_one_repo_does_not_advance_cursor_when_first_issue_fails() -> None:
    """First-issue dispatch failure: cursor stays at its prior value
    (or absent) so the next poll re-fetches the same boundary.
    """
    bus = EventBus()
    bus.subscribe(event_types=(GITHUB_ISSUE_EVENT_TYPE,), queue_size=16)
    issue = _make_issue(
        number=1, updated_at=datetime(2026, 5, 29, 10, 0, 0, tzinfo=UTC)
    )
    github = _fake_github_client()
    github.list_issues_since = AsyncMock(return_value=[issue])

    async def failing_dispatch(_: Any) -> None:
        raise RuntimeError("Lithos went away")

    watcher = _make_watcher(
        github=github,
        lithos=_fake_lithos_client(),
        bus=bus,
        dispatch=failing_dispatch,
    )
    prior = datetime(2026, 5, 1, tzinfo=UTC)
    watcher._store._cursors["agent-lore/lithos-loom"] = prior

    await watcher._poll_one_repo(slug="x", repo="agent-lore/lithos-loom")

    assert watcher._store._cursors["agent-lore/lithos-loom"] == prior


@pytest.mark.asyncio
async def test_poll_one_repo_404_drops_only_that_repo() -> None:
    """A 404 on one repo of a multi-repo project must drop only that
    repo (and its cursor) — the project's other repos keep being
    polled."""
    github = _fake_github_client()
    github.list_issues_since = AsyncMock(side_effect=GitHubRepoNotFoundError("gone"))
    watcher = _make_watcher(github=github, lithos=_fake_lithos_client())
    watcher._watch_list = {
        "kindred-code": WatchedRepo(repos=("kindred/web", "kindred/gone"))
    }
    watcher._store._cursors["kindred/gone"] = datetime(2026, 5, 29, tzinfo=UTC)
    watcher._store._cursors["kindred/web"] = datetime(2026, 5, 28, tzinfo=UTC)

    await watcher._poll_one_repo(slug="kindred-code", repo="kindred/gone")

    # Only the 404 repo is dropped; the sibling and its cursor survive.
    assert watcher._watch_list == {"kindred-code": WatchedRepo(repos=("kindred/web",))}
    assert "kindred/gone" not in watcher._store._cursors
    assert watcher._store._cursors["kindred/web"] == datetime(2026, 5, 28, tzinfo=UTC)


@pytest.mark.asyncio
async def test_poll_one_repo_404_on_last_repo_drops_slug() -> None:
    """When the 404 repo was the project's only repo, the slug is
    dropped entirely."""
    github = _fake_github_client()
    github.list_issues_since = AsyncMock(side_effect=GitHubRepoNotFoundError("gone"))
    watcher = _make_watcher(github=github, lithos=_fake_lithos_client())
    watcher._watch_list = {"solo": WatchedRepo(repos=("owner/only",))}

    await watcher._poll_one_repo(slug="solo", repo="owner/only")

    assert watcher._watch_list == {}


@pytest.mark.asyncio
async def test_stuck_issue_retried_by_number_next_poll() -> None:
    """PR-review finding 2 (round 4, 2026-05-30): an issue that failed
    dispatch during bootstrap (cursor None) and then closes on GH
    before the next poll would disappear from the next state="open"
    walk. The watcher now re-fetches stuck issues by number via
    ``get_issue`` so the close-before-retry race no longer strands the
    linked Lithos task.
    """
    bus = EventBus()
    bus.subscribe(event_types=(GITHUB_ISSUE_EVENT_TYPE,), queue_size=16)
    open_issue = _make_issue(
        number=42, updated_at=datetime(2026, 5, 29, 10, 0, 0, tzinfo=UTC)
    )
    closed_issue = _make_issue(
        number=42,
        state="closed",
        state_reason="completed",
        updated_at=datetime(2026, 5, 29, 11, 0, 0, tzinfo=UTC),
    )
    github = _fake_github_client()
    # First poll fetches open, dispatch fails, issue gets stuck.
    # Second poll's retry-by-number sees the closed state.
    github.list_issues_since = AsyncMock(side_effect=[[open_issue], []])
    github.get_issue = AsyncMock(return_value=closed_issue)

    attempt_count = 0

    async def flaky_dispatch(event: Any) -> None:
        nonlocal attempt_count
        attempt_count += 1
        # First call (during initial bootstrap) raises.
        # Second call (the by-number retry) succeeds.
        if attempt_count == 1:
            raise RuntimeError("transient")

    watcher = _make_watcher(
        github=github,
        lithos=_fake_lithos_client(),
        bus=bus,
        dispatch=flaky_dispatch,
    )

    # First poll: bootstrap, fails, issue 42 is stuck.
    await watcher._poll_one_repo(slug="x", repo="agent-lore/lithos-loom")
    assert 42 in watcher._store._stuck_issues.get("agent-lore/lithos-loom", set())

    # Second poll: get_issue returns closed state, dispatch succeeds,
    # stuck set drains.
    await watcher._poll_one_repo(slug="x", repo="agent-lore/lithos-loom")
    github.get_issue.assert_awaited_with("agent-lore/lithos-loom", 42)
    assert watcher._store._stuck_issues.get("agent-lore/lithos-loom", set()) == set()


@pytest.mark.asyncio
async def test_stuck_issue_dropped_when_gh_returns_404() -> None:
    """Operator deleted the issue between polls. get_issue returns None;
    the stuck entry drops without further retry."""
    bus = EventBus()
    bus.subscribe(event_types=(GITHUB_ISSUE_EVENT_TYPE,), queue_size=16)
    github = _fake_github_client()
    github.list_issues_since = AsyncMock(return_value=[])
    github.get_issue = AsyncMock(return_value=None)

    async def dispatch_ok(_: Any) -> None:
        return None

    watcher = _make_watcher(
        github=github,
        lithos=_fake_lithos_client(),
        bus=bus,
        dispatch=dispatch_ok,
    )
    watcher._store._stuck_issues["agent-lore/lithos-loom"] = {42}

    await watcher._poll_one_repo(slug="x", repo="agent-lore/lithos-loom")

    assert "agent-lore/lithos-loom" not in watcher._store._stuck_issues


@pytest.mark.asyncio
async def test_stuck_issue_auth_error_does_not_drop_entry() -> None:
    """PR-review finding 2 (round 5, 2026-05-30): an auth failure on
    get_issue used to drop the stuck entry as if it were permanent.
    Credentials can be rotated later — the entry must stay so the
    eventual recovery picks it up. Only None (issue genuinely deleted
    on GH) or a successful dispatch retires the entry."""
    from lithos_loom.github_client import GitHubAuthError

    bus = EventBus()
    bus.subscribe(event_types=(GITHUB_ISSUE_EVENT_TYPE,), queue_size=16)
    github = _fake_github_client()
    github.list_issues_since = AsyncMock(return_value=[])
    github.get_issue = AsyncMock(side_effect=GitHubAuthError("403 denied"))

    async def dispatch_ok(_: Any) -> None:
        return None

    watcher = _make_watcher(
        github=github,
        lithos=_fake_lithos_client(),
        bus=bus,
        dispatch=dispatch_ok,
    )
    watcher._store._stuck_issues["agent-lore/lithos-loom"] = {42}

    await watcher._poll_one_repo(slug="x", repo="agent-lore/lithos-loom")

    # Stuck entry preserved despite the auth error — operator might
    # repair credentials and the next poll picks it up.
    assert 42 in watcher._store._stuck_issues["agent-lore/lithos-loom"]


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_stuck_issue_still_failing_defers_new_fetch() -> None:
    """If a stuck retry still fails, the watcher skips the regular fetch
    so we don't accumulate fresh stuck entries on top of the unresolved
    ones."""
    bus = EventBus()
    bus.subscribe(event_types=(GITHUB_ISSUE_EVENT_TYPE,), queue_size=16)
    github = _fake_github_client()
    github.list_issues_since = AsyncMock(return_value=[])
    github.get_issue = AsyncMock(
        return_value=_make_issue(
            number=42, updated_at=datetime(2026, 5, 29, tzinfo=UTC)
        )
    )

    async def still_failing(_: Any) -> None:
        raise RuntimeError("still down")

    watcher = _make_watcher(
        github=github,
        lithos=_fake_lithos_client(),
        bus=bus,
        dispatch=still_failing,
    )
    watcher._store._stuck_issues["agent-lore/lithos-loom"] = {42}

    await watcher._poll_one_repo(slug="x", repo="agent-lore/lithos-loom")

    # 42 stays in the stuck set; regular fetch was skipped this poll.
    assert 42 in watcher._store._stuck_issues["agent-lore/lithos-loom"]
    github.list_issues_since.assert_not_awaited()


@pytest.mark.asyncio
async def test_poll_one_repo_boundary_replay_is_accepted() -> None:
    """The cursor is held at the boundary timestamp rather than nudged
    past it. PR-review finding 3 (2026-05-30): the earlier +1s nudge
    avoided a single idempotent re-fetch but dropped same-second sibling
    updates outright. Same-second drops are a correctness failure for an
    inbound mirror; idempotent replay is not. The handler short-circuits
    on the marker → open-task path so the cost is at most one extra
    Lithos round-trip per repo per poll.
    """
    bus = EventBus()
    bus.subscribe(event_types=(GITHUB_ISSUE_EVENT_TYPE,), queue_size=16)
    boundary = _make_issue(
        number=42, updated_at=datetime(2026, 5, 29, 19, 7, 35, tzinfo=UTC)
    )
    github = _fake_github_client()
    github.list_issues_since = AsyncMock(return_value=[boundary])
    watcher = _make_watcher(github=github, lithos=_fake_lithos_client(), bus=bus)
    watcher._store._cursors["agent-lore/lithos-loom"] = boundary.updated_at

    await watcher._poll_one_repo(slug="x", repo="agent-lore/lithos-loom")

    # Cursor stays at the boundary — same-second sibling updates still
    # get pulled on the next poll.
    assert watcher._store._cursors["agent-lore/lithos-loom"] == boundary.updated_at


@pytest.mark.asyncio
async def test_poll_one_repo_uses_existing_cursor_as_since_param() -> None:
    bus = EventBus()
    bus.subscribe(event_types=(GITHUB_ISSUE_EVENT_TYPE,), queue_size=16)
    github = _fake_github_client()
    github.list_issues_since = AsyncMock(return_value=[])
    watcher = _make_watcher(github=github, lithos=_fake_lithos_client(), bus=bus)
    prior = datetime(2026, 5, 29, 8, 0, 0, tzinfo=UTC)
    watcher._store._cursors["agent-lore/lithos-loom"] = prior

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
        "ghost": WatchedRepo(repos=("missing/repo",)),
        "real": WatchedRepo(repos=("agent-lore/lithos-loom",)),
    }
    watcher._store._cursors["missing/repo"] = datetime(2026, 5, 28, tzinfo=UTC)

    await watcher._poll_one_repo(slug="ghost", repo="missing/repo")

    assert "ghost" not in watcher._watch_list
    assert "missing/repo" not in watcher._store._cursors
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


@pytest.mark.asyncio
@pytest.mark.asyncio
@pytest.mark.asyncio
@pytest.mark.asyncio
@pytest.mark.asyncio
@pytest.mark.asyncio
@pytest.mark.asyncio
@pytest.mark.asyncio
@pytest.mark.asyncio
@pytest.mark.asyncio
@pytest.mark.asyncio
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
    assert watcher._watch_list == {"x": WatchedRepo(repos=("agent-lore/x",))}
    assert watcher._coord_doc_subscription is not None
    # Subscribed to lithos.note.* events.
    assert watcher._coord_doc_subscription.event_types == frozenset(
        {"lithos.note.created", "lithos.note.updated"}
    )


_WATCHER_LOGGER = "lithos_loom.sources.github_issue_watcher"


@pytest.mark.asyncio
async def test_bootstrap_logs_watching_count_at_info(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Regression for soak-time review: an operator with `enabled = true`
    and any watched project needs to see "watching N repo(s)" once at
    INFO so the daemon's state is unambiguous at startup."""
    import logging as _logging

    bus = EventBus()
    lithos = _fake_lithos_client(
        note_list_return=[
            _summary(slug="x", repo="agent-lore/x", watching=True),
            _summary(slug="y", repo="agent-lore/y", watching=True),
        ],
    )
    watcher = _make_watcher(github=_fake_github_client(), lithos=lithos, bus=bus)
    with caplog.at_level(_logging.INFO, logger=_WATCHER_LOGGER):
        await watcher._bootstrap()
    assert any("watching 2 repo(s)" in record.message for record in caplog.records), (
        caplog.text
    )


@pytest.mark.asyncio
async def test_bootstrap_logs_empty_watch_list_at_info(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Regression: empty watch list at startup must surface at INFO, with
    actionable guidance — otherwise the "enabled but nothing tagged"
    state reads identically to a stuck daemon (silent for every poll
    cycle that follows)."""
    import logging as _logging

    bus = EventBus()
    lithos = _fake_lithos_client(note_list_return=[])
    watcher = _make_watcher(github=_fake_github_client(), lithos=lithos, bus=bus)
    with caplog.at_level(_logging.INFO, logger=_WATCHER_LOGGER):
        await watcher._bootstrap()
    assert any(
        "no watched repos configured" in record.message for record in caplog.records
    ), caplog.text
    # And the message names the actionable CLI command.
    assert any("add-github-repo" in record.message for record in caplog.records), (
        caplog.text
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
    dispatch = _CapturingDispatch()
    github = _fake_github_client()

    def fake_list(
        repo: str, *, since: datetime | None, state: str = "all"
    ) -> list[Issue]:
        return [_make_issue(number=1, repo=repo)]

    github.list_issues_since = AsyncMock(side_effect=fake_list)
    watcher = _make_watcher(
        github=github, lithos=_fake_lithos_client(), dispatch=dispatch
    )
    watcher._watch_list = {
        "a": WatchedRepo(repos=("owner/a",)),
        "b": WatchedRepo(repos=("owner/b",)),
    }

    await watcher._poll_all_repos()

    assert github.list_issues_since.await_count == 2
    assert len(dispatch.events) == 2


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
    watcher._watch_list = {"a": WatchedRepo(repos=("owner/a",))}

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
    watcher._watch_list = {"a": WatchedRepo(repos=("owner/a",))}
    watcher._store._cursors = {"owner/a": datetime(2026, 5, 29, tzinfo=UTC)}

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
    """``github.issue.seen`` is the watcher/sync-handler event contract."""
    assert GITHUB_ISSUE_EVENT_TYPE == "github.issue.seen"
