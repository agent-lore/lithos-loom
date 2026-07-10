"""GitHubIssueWatcher — polling source for the github-issue-watcher feature.

Slice 7.1 of ``docs/prd/github-issue-watcher.md``. Polls watched GitHub
repos on a timer, emits one ``github.issue.seen`` event per issue onto
the in-process bus, and persists per-repo ``updated_at`` cursors in a
Lithos coord doc so daemon restart doesn't re-walk every open issue.

Architecture mirrors :class:`LithosNoteStream` for the reconnect /
backoff shape but the work loop is poll-driven rather than SSE-driven:
there's no GitHub server-push surface that doesn't require a public
ingress (the PRD risk notes call this out — webhooks are deferred).

The watcher's watch list (which Lithos slugs map to which repos) is
derived from project-context metadata:

- ``github_watch_enabled = true`` on a project-context doc enables
  watching for that project.
- ``github_repos`` (a list of ``owner/name`` strings) carries the repo
  mappings — a project may track several repos. The CLI subcommands
  (``add-github-repo`` / ``remove-github-repo`` / ``enable-github`` /
  ``disable-github``) manage this metadata.

Mid-run, the watcher subscribes to ``lithos.note.{created,updated}``
on the in-process bus so an operator who runs ``project enable-github
<slug>`` doesn't have to restart the daemon for the watch list to pick
up the change.

Cursor persistence:

The coord doc's body is human-readable text — one line per repo, of
the form ``<owner>/<name> <iso-timestamp>``. Plain text avoids
introducing a YAML/JSON parser at this layer and the doc stays
operator-readable in the vault when the project-context-projection
picks it up. The coord doc is created lazily on first poll.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Protocol

from lithos_loom.bus import Event, EventBus, Subscription
from lithos_loom.cli._github_metadata import (
    GITHUB_WATCH_KEY,
    extract_exclude_authors,
    extract_exclude_labels,
    extract_github_repos,
)
from lithos_loom.errors import LithosClientError
from lithos_loom.github_client import (
    GitHubAuthError,
    GitHubClient,
    GitHubError,
    GitHubRepoNotFoundError,
    Issue,
)
from lithos_loom.lithos_client import Note, NoteSummary, WriteResult
from lithos_loom.sources.github_watch_state import GitHubWatchStateStore, _isoformat

__all__ = [
    "GITHUB_ISSUE_EVENT_TYPE",
    "GitHubIssueWatcher",
    "WatchedRepo",
    "WatcherLithosClient",
]


@dataclass(frozen=True)
class WatchedRepo:
    """Per-project watcher state derived from the project-context doc.

    ``repos`` is the project's ``owner/name`` mappings (one or more — a
    project may track several repos). ``exclude_labels`` and
    ``exclude_authors`` are the metadata-derived import filters that the
    sync handler uses to drop noise from automated issue creators
    (dependabot, renovate) before ``task_create``; they apply to every
    repo the project tracks. The filters are immutable per refresh cycle
    so a concurrent watch-list rebuild can't reshape them under a
    running poll.
    """

    repos: tuple[str, ...]
    exclude_labels: tuple[str, ...] = ()
    exclude_authors: tuple[str, ...] = ()


logger = logging.getLogger(__name__)

GITHUB_ISSUE_EVENT_TYPE = "github.issue.seen"
"""Bus event type emitted for every issue seen during a poll.

One event per issue per poll — the subscription handler decides whether
to create/update/close the corresponding Lithos task based on the
linkage marker + the issue's own state. Funnelling create+update through
one type means the source doesn't have to look up prior state."""

_BUS_QUEUE_SIZE = 256

# GitHub's ``since=`` filter is inclusive (>=). We persist the
# observed-max ``updated_at`` verbatim and accept that the boundary
# issue is re-fetched on the next poll: the sync handler is idempotent
# (marker → open-task path no-ops, drift compare short-circuits) so a
# bounded replay costs at most one extra task_list call per repo per
# poll. The earlier ``+1 second`` nudge avoided that cost but
# silently dropped any *other* issue updated within the same wall
# second as the boundary — the wrong tradeoff for a correctness-
# critical inbound mirror (PR-review finding 3).


class WatcherLithosClient(Protocol):
    """Minimum Lithos surface the watcher source depends on.

    Pulled out as a Protocol so tests can pass a stub without
    constructing a real ``LithosClient`` + transport.
    """

    async def note_list(
        self,
        *,
        path_prefix: str | None = None,
        tags: list[str] | None = None,
        limit: int = 100,
        metadata_match: dict[str, Any] | None = None,
    ) -> list[NoteSummary]: ...

    async def note_read(
        self, *, id: str | None = None, path: str | None = None
    ) -> Note | None: ...

    async def note_write(
        self,
        *,
        agent: str | None = None,
        title: str,
        content: str,
        tags: list[str] | None = None,
        note_type: str = "concept",
        path: str | None = None,
        id: str | None = None,
        expected_version: int | None = None,
        status: str | None = None,
    ) -> WriteResult: ...


# ── Source ─────────────────────────────────────────────────────────────


@dataclass
class GitHubIssueWatcher:
    """Polling source: bus → bootstrap → poll-loop → cursor persistence."""

    github: GitHubClient
    lithos: WatcherLithosClient
    bus: EventBus
    poll_interval_seconds: int
    coord_doc_path: str
    agent_id: str
    # Inline dispatcher for the GH→Lithos sync handler. When set, the
    # watcher calls it per issue and ties cursor advancement to the
    # dispatcher's success — see ``_poll_one_repo`` and PR-review
    # finding 1 (2026-05-30). When ``None`` the watcher falls back to
    # publishing on the bus only (legacy path, used by tests that assert
    # on queue contents). Production wiring always injects a real
    # dispatcher; without one, a cursor advance gets out ahead of any
    # downstream reconciliation and dropped-queue events strand
    # permanently.
    dispatch: Callable[[Event], Awaitable[None]] | None = None
    # Backoff used after a polling-loop iteration that raised. Mirrors
    # :class:`LithosNoteStream` shape.
    reconnect_backoff_seconds: float = 1.0
    max_reconnect_backoff_seconds: float = 60.0
    # Seam for tests so they don't sleep for real.
    _sleep: Any = field(default=asyncio.sleep)

    # State derived at bootstrap.
    _watch_list: dict[str, WatchedRepo] = field(default_factory=dict)
    """``{slug: WatchedRepo}`` — the repos the watcher polls plus their
    import-time exclude filters.

    Rebuilt at bootstrap and on every relevant bus event so an operator
    toggling ``github_watch_enabled`` on a project doc takes effect
    within a poll interval at worst.
    """
    _coord_doc_subscription: Subscription | None = None
    # Owns the poll cursors + stuck-issue repair set + the CAS-with-tombstones
    # coord-doc write (ARCH-8). Built in ``__post_init__`` from the watcher's
    # lithos client / agent id / coord-doc path.
    _store: GitHubWatchStateStore = field(init=False)

    def __post_init__(self) -> None:
        self._store = GitHubWatchStateStore(
            lithos=self.lithos,
            agent_id=self.agent_id,
            coord_doc_path=self.coord_doc_path,
        )

    # ── Lifecycle ──────────────────────────────────────────────────────

    async def run(self) -> None:
        """Bootstrap once, then run the poll + refresh loops concurrently.

        Cancellable: ``asyncio.CancelledError`` propagates out, the
        gather call cancels its children, and the supervisor's
        shutdown drain finishes cleanly.
        """
        await self._bootstrap()
        try:
            await asyncio.gather(
                self._poll_loop(),
                self._refresh_loop(),
            )
        except asyncio.CancelledError:
            raise

    async def _bootstrap(self) -> None:
        """One-shot startup: load watch list + open the bus subscription.

        Logs the initial watch list size at INFO so an operator who set
        ``enabled = true`` but hasn't yet tagged any project can see the
        "watching nothing" state explicitly — without this they'd see
        the startup banner, the coord-doc check, and then nothing for
        every poll cycle, which reads identically to a stuck daemon.
        Subsequent transitions are covered by ``_refresh_watch_list``'s
        added/removed log.
        """
        await self._refresh_watch_list()
        if self._watch_list:
            logger.info(
                "github-watcher: watching %d repo(s): %s",
                sum(len(w.repos) for w in self._watch_list.values()),
                sorted(repo for w in self._watch_list.values() for repo in w.repos),
            )
        else:
            logger.info(
                "github-watcher: no watched repos configured — map a repo with "
                "`lithos-loom project add-github-repo <slug> owner/name` and turn "
                "it on with `enable-github <slug>`"
            )
        await self._store.load()
        # Subscribe BEFORE the first poll so any project-doc changes
        # during the first cycle don't get missed.
        self._coord_doc_subscription = self.bus.subscribe(
            event_types=("lithos.note.created", "lithos.note.updated"),
            name="github-watcher-refresh",
            queue_size=_BUS_QUEUE_SIZE,
        )

    # ── Watch-list management ─────────────────────────────────────────

    async def _refresh_watch_list(self) -> None:
        """Query Lithos for project docs with watching enabled.

        Selects project-context docs whose ``github_watch_enabled``
        metadata is ``True``; each carries a ``github_repos`` list (one
        or more ``owner/name`` strings) plus optional exclude filters,
        stashed in :attr:`_watch_list`. Docs with the flag on but an
        empty repo list are skipped (the CLI prevents this, but operator
        drift could).
        """
        try:
            summaries = await self.lithos.note_list(
                path_prefix="projects/",
                metadata_match={GITHUB_WATCH_KEY: True},
            )
        except (OSError, LithosClientError) as exc:
            logger.warning(
                "github-watcher: refresh failed (%s: %s); keeping previous "
                "watch list of %d project(s)",
                type(exc).__name__,
                exc,
                len(self._watch_list),
            )
            return

        new_list: dict[str, WatchedRepo] = {}
        for summary in summaries:
            slug = summary.slug
            if not slug:
                continue
            meta = dict(summary.metadata)
            repos = extract_github_repos(meta)
            if not repos:
                logger.info(
                    "github-watcher: project %s has %s but no github_repos "
                    "— skipping until a repo is added",
                    slug,
                    GITHUB_WATCH_KEY,
                )
                continue
            new_list[slug] = WatchedRepo(
                repos=tuple(repos),
                exclude_labels=tuple(extract_exclude_labels(meta)),
                exclude_authors=tuple(extract_exclude_authors(meta)),
            )
        added = set(new_list) - set(self._watch_list)
        removed = set(self._watch_list) - set(new_list)
        # Config drift on existing slugs (operator added a repo or an
        # exclude filter) — surface it at INFO so it's visible when the
        # next poll starts honouring it.
        changed = {
            slug
            for slug in new_list.keys() & self._watch_list.keys()
            if new_list[slug] != self._watch_list[slug]
        }
        if added or removed or changed:
            logger.info(
                "github-watcher: watch list refresh — added=%s removed=%s changed=%s",
                sorted(added),
                sorted(removed),
                sorted(changed),
            )
        # Cursors are keyed by repo (owner/name), so reset is computed
        # per repo, not per slug — adding a sibling repo to a project
        # must NOT reset the cursors of the repos it already tracks.
        # A repo's cursor is dropped when it is newly watched (so a
        # disable → re-enable cycle re-surfaces issues created while
        # paused, rather than re-loading a stale cursor), when its
        # project's exclude filters change (so re-included issues
        # surface), or when it leaves the watch set (cleanup).
        cursor_reset_repos: set[str] = set()
        for slug in added | removed | changed:
            old_entry = self._watch_list.get(slug)
            new_entry = new_list.get(slug)
            old_repos = set(old_entry.repos) if old_entry else set()
            new_repos = set(new_entry.repos) if new_entry else set()
            filters_changed = bool(
                old_entry
                and new_entry
                and (old_entry.exclude_labels, old_entry.exclude_authors)
                != (new_entry.exclude_labels, new_entry.exclude_authors)
            )
            if filters_changed:
                cursor_reset_repos |= new_repos
            else:
                cursor_reset_repos |= new_repos - old_repos
            cursor_reset_repos |= old_repos - new_repos
        for repo in cursor_reset_repos:
            if self._store.forget_cursor(repo):
                logger.info(
                    "github-watcher: resetting cursor for %s after watch-list "
                    "change so re-included issues surface",
                    repo,
                )
        self._watch_list = new_list

    # ── Refresh loop (bus subscriber) ─────────────────────────────────

    async def _refresh_loop(self) -> None:
        """Drain the bus subscription and refresh the watch list on relevance.

        Reacts to ``lithos.note.{created,updated}`` events whose
        ``path`` starts with ``projects/``. The lookup is path-prefix
        based because the event payload (per
        :class:`LithosNoteStream._publish`) carries ``{id, title, path}``
        — no metadata — so we can't filter by ``github_watch_enabled``
        directly and have to refresh on any project-doc change.
        Refreshes are cheap.
        """
        assert self._coord_doc_subscription is not None
        sub = self._coord_doc_subscription
        while True:
            event = await sub.queue.get()
            path = event.payload.get("path")
            if not isinstance(path, str) or not path.startswith("projects/"):
                continue
            # Avoid refresh-storms on writes to the coord doc itself.
            if path == self.coord_doc_path:
                continue
            await self._refresh_watch_list()

    # ── Polling loop ──────────────────────────────────────────────────

    async def _poll_loop(self) -> None:
        """Forever: poll every watched repo, advance cursors, sleep.

        A whole-pass error doesn't kill the source — it backs off and
        retries on the next iteration. Per-repo errors are absorbed
        inside :meth:`_poll_one_repo` so a single misconfigured repo
        doesn't block others.
        """
        backoff = self.reconnect_backoff_seconds
        while True:
            try:
                await self._poll_all_repos()
                # Single coord doc write after the full pass — one round-
                # trip per poll, not one per repo. PR-review finding 1
                # (round 4, 2026-05-30): always call through, even with
                # an empty cursor map. The previous `if self._cursors:`
                # guard short-circuited persistence after every slug got
                # removed, leaving stale rows in the coord doc; on
                # restart the daemon then resumed from those stale
                # cursors and could miss issues created during the
                # disabled window. The store's persist itself short-circuits
                # via the unchanged-cursors check, so an empty map that's
                # already on disk stays a no-op.
                await self._store.persist()
                backoff = self.reconnect_backoff_seconds
                await self._sleep(self.poll_interval_seconds)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "github-watcher: poll cycle failed (%s: %s); backing off %.1fs",
                    type(exc).__name__,
                    exc,
                    backoff,
                )
                await self._sleep(backoff)
                backoff = min(backoff * 2, self.max_reconnect_backoff_seconds)

    async def _poll_all_repos(self) -> None:
        items = list(self._watch_list.items())
        if not items:
            logger.debug("github-watcher: poll cycle skipped; watch list empty")
            return
        # A project may map several repos; flatten to (slug, repo) pairs.
        pairs = [(slug, repo) for slug, watched in items for repo in watched.repos]
        logger.info(
            "github-watcher: poll cycle starting (%d project(s), %d repo(s))",
            len(items),
            len(pairs),
        )
        for slug, repo in pairs:
            await self._poll_one_repo(slug=slug, repo=repo)

    def _drop_repo(self, *, slug: str, repo: str) -> None:
        """Remove a single repo from a project's watch entry (e.g. on a
        404) without disturbing the project's other repos.

        A project may map several repos; a 404 on one must not stop
        polling the siblings. Drops the repo from the entry's ``repos``
        tuple (removing the slug entirely only when it was the last
        repo) and clears that repo's cursor + stuck state. The next
        ``_refresh_watch_list`` re-reads the canonical metadata, so a
        repo that 404s but is still mapped will be re-added and
        re-attempted — same transient-drop behaviour as before, now
        scoped to the offending repo.
        """
        watched = self._watch_list.get(slug)
        if watched is not None:
            remaining = tuple(r for r in watched.repos if r != repo)
            if remaining:
                self._watch_list[slug] = replace(watched, repos=remaining)
            else:
                self._watch_list.pop(slug, None)
        self._store.drop_repo(repo)

    async def _retry_stuck_issues(self, *, slug: str, repo: str) -> bool:
        """Retry issues whose dispatch failed in a previous poll.

        Returns ``True`` if every stuck issue dispatched cleanly (or was
        deleted on GH), ``False`` if anything is still stuck. Callers
        defer the normal cursor-based fetch on ``False`` so a persistent
        failure doesn't accumulate fresh entries on top of the unresolved
        ones.

        Each issue is re-fetched via ``get_issue`` instead of relying on
        the cursor + state filter — the bootstrap path (state="open")
        wouldn't surface an issue that closed since the previous
        attempt, but the per-issue PATCH-equivalent ``GET`` does.
        """
        numbers = self._store.stuck_numbers(repo)
        if not numbers:
            return True
        for number in numbers:
            try:
                issue = await self.github.get_issue(repo, number)
            except GitHubError as exc:
                # PR-review finding 2 (round 5, 2026-05-30): auth errors
                # used to drop the entry here. They aren't actually
                # permanent — the operator might rotate `gh auth` and
                # come back. Keep the entry; only ``None`` (issue
                # genuinely deleted on GH, returned by ``get_issue``
                # short-circuit on 404) or a successful dispatch retires
                # the entry. ``get_issue`` itself never raises
                # ``GitHubRepoNotFoundError`` — the 404 short-circuits
                # to ``None`` — so all subclasses here are effectively
                # transient.
                logger.warning(
                    "github-watcher: re-fetch of stuck %s/#%d transient (%s: %s); "
                    "leaving in stuck set",
                    repo,
                    number,
                    type(exc).__name__,
                    exc,
                )
                return False
            if issue is None:
                # GH issue deleted in the interim — nothing to reconcile.
                logger.info(
                    "github-watcher: stuck %s/#%d gone on GH; dropping from stuck set",
                    repo,
                    number,
                )
                self._store.discard_stuck(repo, number)
                continue
            try:
                await self._publish_issue(slug=slug, issue=issue)
            except Exception as exc:
                logger.warning(
                    "[Friction] github-watcher: stuck %s/#%d dispatch still "
                    "fails (%s: %s); will retry next poll",
                    repo,
                    number,
                    type(exc).__name__,
                    exc,
                )
                return False
            self._store.discard_stuck(repo, number)
        return True

    async def _poll_one_repo(self, *, slug: str, repo: str) -> None:
        """Fetch issues for one repo, emit events, advance the cursor.

        Two distinct paths:

        - **Bootstrap** (no cursor yet for this repo): walks every open
          issue with ``state="open"``, fully paginated. This matches
          PRD US-56's "walk every open issue on daemon start" guarantee
          and avoids burning through closed history one 100-issue page
          per poll interval on a repo with hundreds of resolved issues.
        - **Incremental** (cursor present): uses ``state="all"`` since
          the cursor, fully paginated. State transitions (open → closed)
          surface alongside fresh opens because GH advances
          ``updated_at`` on close, so the cursor-based delta catches
          them.

        Errors are absorbed: a 404 drops the repo from the watch list
        (the project doc still owns the mapping; next refresh will
        re-add it if the operator fixes the typo). Auth/rate-limit
        errors are logged but don't propagate — the next pass retries.

        PR-review finding 2 (round 4, 2026-05-30): before the regular
        fetch, retry any issues that failed dispatch in a previous poll
        via ``get_issue`` directly. The bootstrap path uses
        ``state="open"`` and would lose a closed-before-retry issue
        otherwise; retrying by number is cursor-independent and survives
        the close transition.
        """
        if not await self._retry_stuck_issues(slug=slug, repo=repo):
            # A stuck issue still failed; defer the new-fetch this poll
            # so we don't keep racking up additional stuck entries while
            # the underlying problem persists.
            return
        since = self._store.cursor(repo)
        state = "open" if since is None else "all"
        try:
            issues = await self.github.list_issues_since(repo, since=since, state=state)
        except GitHubRepoNotFoundError:
            logger.warning(
                "[Friction] github-watcher: repo %s not found; "
                "drop from watch list (slug=%s)",
                repo,
                slug,
            )
            self._drop_repo(slug=slug, repo=repo)
            return
        except GitHubAuthError as exc:
            logger.warning(
                "[Friction] github-watcher: auth/permission denied on %s: %s",
                repo,
                exc,
            )
            return
        except GitHubError as exc:
            logger.warning(
                "github-watcher: %s on %s: %s",
                type(exc).__name__,
                repo,
                exc,
            )
            return

        prior_cursor = since
        # GitHub returns issues sorted by ``updated_at`` ascending
        # (``sort=updated&direction=asc`` in list_issues_since). Walk in
        # order so a mid-batch dispatch failure leaves the cursor at the
        # latest *successfully reconciled* issue rather than skipping
        # ahead — PR-review finding 1 (2026-05-30) was that the prior
        # max-after-the-loop pattern allowed bus drops AND handler
        # failures to permanently strand events.
        max_committed: datetime | None = None
        dispatch_failed_at: datetime | None = None
        for issue in issues:
            try:
                await self._publish_issue(slug=slug, issue=issue)
            except Exception as exc:
                dispatch_failed_at = issue.updated_at
                self._store.mark_stuck(repo, issue.number)
                logger.warning(
                    "[Friction] github-watcher: dispatch for %s/#%d failed "
                    "(%s: %s); holding cursor at %s and tagging issue for "
                    "by-number retry next poll",
                    repo,
                    issue.number,
                    type(exc).__name__,
                    exc,
                    _isoformat(max_committed) if max_committed else "<unchanged>",
                )
                break
            max_committed = issue.updated_at

        if max_committed is not None:
            self._store.set_cursor(repo, max_committed)
            logger.info(
                "github-watcher: %s — %d issue(s) %s (state=%s, cursor %s → %s)",
                repo,
                len(issues),
                "bootstrapped" if prior_cursor is None else "delta",
                state,
                _isoformat(prior_cursor) if prior_cursor is not None else "<first-run>",
                _isoformat(max_committed),
            )
        elif issues and dispatch_failed_at is not None:
            # First issue failed — cursor unchanged so the next poll
            # re-fetches and retries.
            logger.info(
                "github-watcher: %s — first dispatch failed at %s; "
                "cursor unchanged (will retry next poll)",
                repo,
                _isoformat(dispatch_failed_at),
            )
        else:
            logger.info(
                "github-watcher: %s — no changes (state=%s, since=%s)",
                repo,
                state,
                _isoformat(prior_cursor) if prior_cursor is not None else "<first-run>",
            )

    async def _publish_issue(self, *, slug: str, issue: Issue) -> None:
        """Build the event for ``issue`` and dispatch.

        When :attr:`dispatch` is injected (production), call it inline
        and propagate any exception so the caller can hold the cursor at
        the prior successful issue. When ``None`` (legacy / tests that
        assert on bus queue contents), publish onto the in-process bus
        which silently drops on queue-full — *not* a path the production
        wiring should rely on for correctness.
        """
        watched = self._watch_list.get(slug)
        # The slug being absent here is a defensive guard — _poll_all_repos
        # iterates the watch list, so a race with refresh is the only way
        # to land here. Treat as "no filters" rather than crashing.
        exclude_labels = list(watched.exclude_labels) if watched else []
        exclude_authors = list(watched.exclude_authors) if watched else []
        event = Event(
            type=GITHUB_ISSUE_EVENT_TYPE,
            timestamp=issue.updated_at,
            payload={
                "slug": slug,
                "repo": issue.repo,
                "number": issue.number,
                "title": issue.title,
                "body": issue.body,
                "state": issue.state,
                "state_reason": issue.state_reason,
                "labels": list(issue.labels),
                "author": issue.author,
                "html_url": issue.html_url,
                "updated_at": _isoformat(issue.updated_at),
                "exclude_labels": exclude_labels,
                "exclude_authors": exclude_authors,
            },
        )
        if self.dispatch is not None:
            await self.dispatch(event)
            return
        await self.bus.publish(event)
