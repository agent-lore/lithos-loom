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
derived from project-context tags:

- ``github-watch`` on a project-context doc enables watching for that
  project.
- ``github-repo:<owner>/<name>`` on the same doc carries the repo
  mapping. The CLI subcommands in Phase A3 manage these tags.

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
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from lithos_loom.bus import Event, EventBus, Subscription
from lithos_loom.cli._github_metadata import (
    GITHUB_WATCH_TAG,
    extract_github_repo,
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

__all__ = [
    "GITHUB_ISSUE_EVENT_TYPE",
    "GitHubIssueWatcher",
    "WatcherLithosClient",
    "format_cursors",
    "parse_cursors",
]

logger = logging.getLogger(__name__)

GITHUB_ISSUE_EVENT_TYPE = "github.issue.seen"
"""Bus event type emitted for every issue seen during a poll.

One event per issue per poll — the subscription handler decides whether
to create/update/close the corresponding Lithos task based on the
linkage marker + the issue's own state. Funnelling create+update through
one type means the source doesn't have to look up prior state."""

_COORD_DOC_TITLE = "GitHub Watcher State"
_COORD_DOC_BODY_HEADER = (
    "Daemon-owned coordination doc. Do not edit by hand —\n"
    "the github-issue-watcher overwrites this file on every successful poll.\n\n"
    "Format: one line per watched repo, '<owner>/<name> <ISO-8601 cursor>'.\n"
)
_BUS_QUEUE_SIZE = 256
_MAX_COORD_DOC_CAS_ATTEMPTS = 3


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


# ── Cursor doc parser ──────────────────────────────────────────────────


def parse_cursors(body: str) -> dict[str, datetime]:
    """Parse the coord doc body into a ``{repo: cursor}`` map.

    Tolerates blank lines, comment lines (anything not matching the
    ``owner/name <iso>`` shape is skipped with a debug log) and either
    UTC ``Z`` or explicit ``+00:00`` timezone suffixes.

    Returns an empty dict for a fresh / unparseable doc — that's
    indistinguishable from "first poll" and the caller falls through
    to a full re-walk per repo.
    """
    out: dict[str, datetime] = {}
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith("Daemon"):
            continue
        parts = line.split(maxsplit=1)
        if len(parts) != 2:
            continue
        repo, cursor_raw = parts
        if "/" not in repo:
            continue
        try:
            cursor = _parse_iso(cursor_raw)
        except ValueError:
            logger.debug("github-watcher: ignoring unparseable cursor line %r", line)
            continue
        out[repo] = cursor
    return out


def format_cursors(cursors: dict[str, datetime]) -> str:
    """Render a cursor map into the canonical coord doc body."""
    lines = [_COORD_DOC_BODY_HEADER]
    for repo in sorted(cursors):
        lines.append(f"{repo} {_isoformat(cursors[repo])}")
    lines.append("")  # trailing newline
    return "\n".join(lines)


def _parse_iso(value: str) -> datetime:
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _isoformat(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat()


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
    # Backoff used after a polling-loop iteration that raised. Mirrors
    # :class:`LithosNoteStream` shape.
    reconnect_backoff_seconds: float = 1.0
    max_reconnect_backoff_seconds: float = 60.0
    # Seam for tests so they don't sleep for real.
    _sleep: Any = field(default=asyncio.sleep)

    # State derived at bootstrap.
    _watch_list: dict[str, str] = field(default_factory=dict)
    """``{slug: owner/name}`` — the repos the watcher polls.

    Rebuilt at bootstrap and on every relevant bus event so an operator
    toggling ``github-watch`` on a project doc takes effect within a
    poll interval at worst.
    """
    _cursors: dict[str, datetime] = field(default_factory=dict)
    """``{owner/name: updated_at}`` — most-recent issue updated-at seen
    for each repo. Used as the GitHub ``since=`` param for incremental
    polls."""
    _coord_doc_id: str | None = None
    _coord_doc_version: int | None = None
    _coord_doc_subscription: Subscription | None = None

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
        """One-shot startup: load watch list + open the bus subscription."""
        await self._refresh_watch_list()
        await self._load_cursors_from_coord_doc()
        # Subscribe BEFORE the first poll so any project-doc changes
        # during the first cycle don't get missed.
        self._coord_doc_subscription = self.bus.subscribe(
            event_types=("lithos.note.created", "lithos.note.updated"),
            name="github-watcher-refresh",
            queue_size=_BUS_QUEUE_SIZE,
        )

    # ── Watch-list management ─────────────────────────────────────────

    async def _refresh_watch_list(self) -> None:
        """Query Lithos for project docs tagged ``github-watch``.

        Each match's tags carry a ``github-repo:<owner>/<name>`` entry
        — extract it and stash in :attr:`_watch_list`. Docs that have
        the watch tag but no repo tag are skipped (the CLI prevents
        this, but operator drift could).
        """
        try:
            summaries = await self.lithos.note_list(
                path_prefix="projects/",
                tags=[GITHUB_WATCH_TAG],
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

        new_list: dict[str, str] = {}
        for summary in summaries:
            slug = summary.slug
            if not slug:
                continue
            repo = extract_github_repo(summary.tags)
            if repo is None:
                logger.info(
                    "github-watcher: project %s has %s but no github-repo tag "
                    "— skipping until set",
                    slug,
                    GITHUB_WATCH_TAG,
                )
                continue
            new_list[slug] = repo
        added = set(new_list) - set(self._watch_list)
        removed = set(self._watch_list) - set(new_list)
        if added or removed:
            logger.info(
                "github-watcher: watch list refresh — added=%s removed=%s",
                sorted(added),
                sorted(removed),
            )
        self._watch_list = new_list

    # ── Coord doc cursors ─────────────────────────────────────────────

    async def _load_cursors_from_coord_doc(self) -> None:
        """Read the coord doc; populate :attr:`_cursors` from its body.

        A missing doc is the bootstrap signal — leave cursors empty so
        the first poll walks every open issue (US-56).
        """
        try:
            note = await self.lithos.note_read(path=self.coord_doc_path)
        except (OSError, LithosClientError) as exc:
            logger.warning(
                "github-watcher: failed to read coord doc %s (%s); "
                "treating as first-run",
                self.coord_doc_path,
                exc,
            )
            return
        if note is None:
            logger.info(
                "github-watcher: coord doc %s not yet present; first-run mode",
                self.coord_doc_path,
            )
            return
        self._coord_doc_id = note.id
        self._coord_doc_version = note.version
        self._cursors = parse_cursors(note.body)
        logger.info(
            "github-watcher: loaded %d cursor(s) from coord doc v%d",
            len(self._cursors),
            note.version,
        )

    async def _persist_cursors(self) -> None:
        """CAS-write the coord doc with the current cursor map.

        On version_conflict, merge our pending advances with the
        remote's cursors per-repo (latest timestamp wins) and retry
        with the fresh version. A handful of retries are allowed before
        giving up so a noisy concurrent writer doesn't block forever —
        the poll loop will retry whole-pass next interval anyway.
        """
        attempts = 0
        while True:
            attempts += 1
            body = format_cursors(self._cursors)
            try:
                result = await self.lithos.note_write(
                    agent=self.agent_id,
                    id=self._coord_doc_id,
                    path=self.coord_doc_path if self._coord_doc_id is None else None,
                    title=_COORD_DOC_TITLE,
                    content=body,
                    expected_version=self._coord_doc_version,
                    note_type="concept",
                    tags=["lithos-loom-internal", "github-watcher-state"],
                )
            except (OSError, LithosClientError) as exc:
                logger.warning(
                    "github-watcher: coord doc write failed (%s: %s); "
                    "cursors will retry next poll",
                    type(exc).__name__,
                    exc,
                )
                return
            if result.status in ("created", "updated") and result.note is not None:
                self._coord_doc_id = result.note.id
                self._coord_doc_version = result.note.version
                return
            if result.status == "version_conflict":
                if attempts >= _MAX_COORD_DOC_CAS_ATTEMPTS:
                    logger.warning(
                        "github-watcher: coord doc CAS exhausted after %d "
                        "version_conflicts; pending cursor advances will "
                        "retry next poll",
                        attempts,
                    )
                    return
                logger.info(
                    "github-watcher: coord doc version_conflict; merging + retry "
                    "(attempt %d/%d)",
                    attempts,
                    _MAX_COORD_DOC_CAS_ATTEMPTS,
                )
                # Hold our pending advances; the load step will replace
                # ``_cursors`` with the remote view, then we re-merge so
                # the just-observed advances aren't lost.
                pending = dict(self._cursors)
                await self._load_cursors_from_coord_doc()
                for repo, ts in pending.items():
                    remote_ts = self._cursors.get(repo)
                    if remote_ts is None or ts > remote_ts:
                        self._cursors[repo] = ts
                continue
            logger.warning(
                "github-watcher: unexpected coord doc write status %r: %s",
                result.status,
                result.message,
            )
            return

    # ── Refresh loop (bus subscriber) ─────────────────────────────────

    async def _refresh_loop(self) -> None:
        """Drain the bus subscription and refresh the watch list on relevance.

        Reacts to ``lithos.note.{created,updated}`` events whose
        ``path`` starts with ``projects/``. The lookup is path-prefix
        based because the event payload (per
        :class:`LithosNoteStream._publish`) carries ``{id, title, path}``
        — no tags — so we can't filter by ``github-watch`` directly and
        have to refresh on any project-doc change. Refreshes are cheap.
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
                # trip per poll, not one per repo.
                if self._cursors:
                    await self._persist_cursors()
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
        for slug, repo in list(self._watch_list.items()):
            await self._poll_one_repo(slug=slug, repo=repo)

    async def _poll_one_repo(self, *, slug: str, repo: str) -> None:
        """Fetch issues for one repo, emit events, advance the cursor.

        Errors are absorbed: a 404 drops the repo from the watch list
        (the project doc still owns the mapping; next refresh will
        re-add it if the operator fixes the typo). Auth/rate-limit
        errors are logged but don't propagate — the next pass retries.
        """
        since = self._cursors.get(repo)
        try:
            issues = await self.github.list_issues_since(repo, since=since)
        except GitHubRepoNotFoundError:
            logger.warning(
                "[Friction] github-watcher: repo %s not found; "
                "drop from watch list (slug=%s)",
                repo,
                slug,
            )
            self._watch_list.pop(slug, None)
            self._cursors.pop(repo, None)
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

        for issue in issues:
            await self._publish_issue(slug=slug, issue=issue)

        if issues:
            self._cursors[repo] = max(iss.updated_at for iss in issues)

    async def _publish_issue(self, *, slug: str, issue: Issue) -> None:
        await self.bus.publish(
            Event(
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
                },
            )
        )
