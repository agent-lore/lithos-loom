"""GitHubWatchStateStore — the github-watcher's coordination-doc state store.

Extracted from :mod:`github_issue_watcher` (ARCH-8). This module owns the
watcher's persistent coordination state — a second, deeper ``CursorStore``
(cf. :mod:`lithos_loom.cursor_store`) grown for the github-watcher's needs:

- the per-repo poll cursors (``{owner/name: updated_at}``, the GitHub
  ``since=`` param for incremental polls);
- the stuck-issue repair set (``{repo: {number, ...}}`` — issues whose inline
  dispatch failed and must be retried by-number next poll);
- the persisted-vs-in-memory snapshot pairs that drive the unchanged-content
  short-circuit;
- the CAS-with-tombstones write to the Lithos coord doc (version-conflict
  reload-then-merge, replaying cursor + stuck deletions so a locally-dropped
  row is not resurrected from the remote view).

The residual :class:`~github_issue_watcher.GitHubIssueWatcher` is a genuine
source: poll loop + watch-list refresh + dispatch, delegating all cursor /
stuck state to this store through its public interface.

Cursor persistence format:

The coord doc's body is human-readable text — one line per repo, of the form
``<owner>/<name> <iso-timestamp>``, with stuck-issue rows rendered beneath as
``stuck:<owner>/<name>#<number>``. Plain text avoids introducing a YAML/JSON
parser at this layer and the doc stays operator-readable in the vault when the
project-context-projection picks it up. The coord doc is created lazily on
first persist.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Protocol

from lithos_loom.errors import LithosClientError
from lithos_loom.lithos_client import Note, WriteResult

__all__ = [
    "GitHubWatchStateStore",
    "format_cursors",
    "parse_cursors",
    "parse_stuck",
]

logger = logging.getLogger(__name__)

_COORD_DOC_TITLE = "GitHub Watcher State"
_COORD_DOC_BODY_HEADER = (
    "Daemon-owned coordination doc. Do not edit by hand —\n"
    "the github-issue-watcher overwrites this file on every successful poll.\n\n"
    "Format: one line per watched repo, '<owner>/<name> <ISO-8601 cursor>'.\n"
)
_MAX_COORD_DOC_CAS_ATTEMPTS = 3
_STUCK_PREFIX = "stuck:"


# ── Coord doc grammar ──────────────────────────────────────────────────


def parse_cursors(body: str) -> dict[str, datetime]:
    """Parse the coord doc body into a ``{repo: cursor}`` map.

    Tolerates blank lines, comment lines (anything not matching the
    ``owner/name <iso>`` shape is skipped with a debug log) and either
    UTC ``Z`` or explicit ``+00:00`` timezone suffixes.

    Stuck-issue rows (``stuck:owner/name#42``) are recognised but
    skipped here — parse them with :func:`parse_stuck`.

    Returns an empty dict for a fresh / unparseable doc — that's
    indistinguishable from "first poll" and the caller falls through
    to a full re-walk per repo.
    """
    out: dict[str, datetime] = {}
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith("Daemon"):
            continue
        if line.startswith(_STUCK_PREFIX):
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


def parse_stuck(body: str) -> dict[str, set[int]]:
    """Parse ``stuck:owner/name#<number>`` rows out of the coord doc.

    PR-review finding 3 (round 5, 2026-05-30): the stuck-issue set is
    persisted alongside cursors so a daemon restart between an
    incomplete reconciliation (e.g. ``task_create`` succeeded, marker
    PATCH failed) and the next retry doesn't lose the repair record.
    """
    out: dict[str, set[int]] = {}
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line.startswith(_STUCK_PREFIX):
            continue
        rest = line[len(_STUCK_PREFIX) :]
        if "#" not in rest:
            continue
        repo, num_str = rest.rsplit("#", 1)
        if "/" not in repo:
            continue
        try:
            number = int(num_str)
        except ValueError:
            continue
        out.setdefault(repo, set()).add(number)
    return out


def format_cursors(
    cursors: dict[str, datetime],
    stuck: dict[str, set[int]] | None = None,
) -> str:
    """Render the coord doc body.

    Cursors render as ``owner/name <iso>`` rows; stuck-issue entries
    (optional) render as ``stuck:owner/name#<number>`` rows beneath
    them. Sorted output keeps diffs minimal across writes.
    """
    lines = [_COORD_DOC_BODY_HEADER]
    for repo in sorted(cursors):
        lines.append(f"{repo} {_isoformat(cursors[repo])}")
    if stuck:
        for repo in sorted(stuck):
            for number in sorted(stuck[repo]):
                lines.append(f"{_STUCK_PREFIX}{repo}#{number}")
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


def _copy_stuck(stuck: dict[str, set[int]]) -> dict[str, set[int]]:
    """Deep-copy a stuck-issue map so the persisted snapshot doesn't
    alias the live one.

    PR-review finding 3 (round 5, 2026-05-30): the unchanged-content
    short-circuit compares the current map against the persisted
    snapshot. A shallow ``dict(stuck)`` would let live ``stuck[repo]``
    mutations leak into the snapshot via shared ``set`` references —
    next persist would then see "unchanged" and skip the write even
    when an issue was added or drained.
    """
    return {repo: set(numbers) for repo, numbers in stuck.items()}


# ── Coord-doc client surface ───────────────────────────────────────────


class CoordDocClient(Protocol):
    """The minimal Lithos note surface the state store reads/writes.

    Narrower than the watcher's ``WatcherLithosClient`` (which also lists
    project docs for watch-list refresh) — the store only reads and CAS-writes
    the single coord doc, so it depends on just those two methods.
    """

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


# ── State store ────────────────────────────────────────────────────────


class GitHubWatchStateStore:
    """Owns the github-watcher's cursor + stuck-issue coordination state.

    Constructed once per watcher with the Lithos coord-doc client, the
    persisting agent id, and the coord-doc path. All cursor / stuck mutation
    goes through the public interface; :meth:`load` and :meth:`persist` own the
    coord-doc round-trip (including the CAS-with-tombstones conflict merge).
    """

    def __init__(
        self,
        *,
        lithos: CoordDocClient,
        agent_id: str,
        coord_doc_path: str,
    ) -> None:
        self._lithos = lithos
        self._agent_id = agent_id
        self._coord_doc_path = coord_doc_path
        # ``{owner/name: updated_at}`` — most-recent issue updated-at seen for
        # each repo; the GitHub ``since=`` param for incremental polls.
        self._cursors: dict[str, datetime] = {}
        # Snapshot of the cursor map at the last successful coord-doc write (or
        # load). Drives the unchanged-content short-circuit so a poll cycle that
        # advanced nothing doesn't re-write the doc (soak: version creep).
        self._last_persisted_cursors: dict[str, datetime] = {}
        # ``{repo: {issue_number, ...}}`` — issues whose inline dispatch raised;
        # retried by-number next poll (cursor/state-filter independent).
        self._stuck_issues: dict[str, set[int]] = {}
        # Snapshot of ``_stuck_issues`` at the last successful write, paired with
        # ``_last_persisted_cursors`` for the unchanged-content short-circuit.
        self._last_persisted_stuck: dict[str, set[int]] = {}
        self._coord_doc_id: str | None = None
        self._coord_doc_version: int | None = None

    # ── Read-only identity (for logging / tests) ───────────────────────

    @property
    def coord_doc_id(self) -> str | None:
        return self._coord_doc_id

    @property
    def coord_doc_version(self) -> int | None:
        return self._coord_doc_version

    # ── Cursor interface ───────────────────────────────────────────────

    def cursor(self, repo: str) -> datetime | None:
        """The persisted ``updated_at`` cursor for ``repo`` (None = first-run)."""
        return self._cursors.get(repo)

    def set_cursor(self, repo: str, updated_at: datetime) -> None:
        """Advance ``repo``'s cursor to the latest successfully-reconciled issue."""
        self._cursors[repo] = updated_at

    def forget_cursor(self, repo: str) -> bool:
        """Drop ``repo``'s cursor (watch-list reset path).

        Returns whether a cursor was present so the caller can log the reset
        only when it actually did something.
        """
        if repo in self._cursors:
            self._cursors.pop(repo, None)
            return True
        return False

    # ── Stuck-issue interface ──────────────────────────────────────────

    def stuck_numbers(self, repo: str) -> list[int]:
        """A snapshot list of ``repo``'s stuck issue numbers (sorted, safe to
        iterate while :meth:`discard_stuck` mutates the live set)."""
        return sorted(self._stuck_issues.get(repo, set()))

    def mark_stuck(self, repo: str, number: int) -> None:
        """Record that ``repo#number``'s dispatch failed; retry by-number next poll."""
        self._stuck_issues.setdefault(repo, set()).add(number)

    def discard_stuck(self, repo: str, number: int) -> None:
        """Retire a stuck entry (retry succeeded, or issue deleted on GH),
        dropping the repo key once its set drains."""
        numbers = self._stuck_issues.get(repo)
        if numbers is None:
            return
        numbers.discard(number)
        if not numbers:
            self._stuck_issues.pop(repo, None)

    # ── Repo teardown ──────────────────────────────────────────────────

    def drop_repo(self, repo: str) -> None:
        """Clear a repo's cursor + stuck state (e.g. on a 404 drop)."""
        self._cursors.pop(repo, None)
        self._stuck_issues.pop(repo, None)

    # ── Coord-doc persistence ──────────────────────────────────────────

    async def load(self) -> None:
        """Read the coord doc; populate :attr:`_cursors` + :attr:`_stuck_issues`.

        A missing doc is the bootstrap signal — leave state empty so the first
        poll walks every open issue (US-56).
        """
        try:
            note = await self._lithos.note_read(path=self._coord_doc_path)
        except (OSError, LithosClientError) as exc:
            logger.warning(
                "github-watcher: failed to read coord doc %s (%s); "
                "treating as first-run",
                self._coord_doc_path,
                exc,
            )
            return
        if note is None:
            logger.info(
                "github-watcher: coord doc %s not yet present; first-run mode",
                self._coord_doc_path,
            )
            return
        self._coord_doc_id = note.id
        self._coord_doc_version = note.version
        self._cursors = parse_cursors(note.body)
        # PR-review finding 3 (round 5, 2026-05-30): also reload the persisted
        # stuck-issue set so a daemon restart between a partial task_create +
        # marker_write and the next retry pass still surfaces the stuck issue
        # by-number on the first poll after boot. Without this, restart loses
        # the in-memory set and a closed-before-restart issue stays orphaned.
        self._stuck_issues = parse_stuck(note.body)
        # The remote already holds what we just loaded — track it as "already
        # persisted" so the first poll-cycle's write is skipped when no cursor
        # advanced.
        self._last_persisted_cursors = dict(self._cursors)
        self._last_persisted_stuck = _copy_stuck(self._stuck_issues)
        logger.info(
            "github-watcher: loaded %d cursor(s) and %d stuck issue(s) from "
            "coord doc v%d",
            len(self._cursors),
            sum(len(s) for s in self._stuck_issues.values()),
            note.version,
        )

    async def persist(self) -> None:
        """CAS-write the coord doc with the current cursor map.

        Short-circuits when no cursor has advanced since the last successful
        write — otherwise every poll cycle would re-write the same content,
        bumping the Lithos version and firing two SSE note.updated events per
        minute even when GitHub returned nothing new (soak observation: coord
        doc climbed to v60+ in under an hour with no GH activity).

        On version_conflict, merge our pending advances with the remote's
        cursors per-repo (latest timestamp wins) and retry with the fresh
        version. A handful of retries are allowed before giving up so a noisy
        concurrent writer doesn't block forever — the poll loop will retry
        whole-pass next interval anyway.

        The unchanged-cursors check runs at the TOP of every CAS iteration (not
        just at entry) so the conflict-then-merge path also short-circuits when
        the remote already held what we would have written — otherwise
        ``continue`` would bypass the entry guard and pointlessly bump the
        coord-doc version on the retry (PR-review finding round 2).
        """
        # PR-review finding 1 (round 5, 2026-05-30): track which repos we
        # *intend to delete* this persist call so the version_conflict
        # reload-then-merge path can re-apply the deletions. Without
        # tombstones, a refresh that popped a cursor would lose that intent on
        # conflict because reload re-populates ``_cursors`` from the remote
        # (which still contains the row we wanted gone). Snapshot the intended
        # deletions BEFORE the first write so subsequent reload+merge cycles can
        # replay them deterministically.
        deletions = set(self._last_persisted_cursors) - set(self._cursors)
        # PR-review finding 3 (round 6, 2026-05-30): same pattern for stuck-issue
        # rows. A stuck entry drained locally (issue's by-number retry succeeded,
        # or GH returned 404) was getting resurrected when a CAS conflict
        # reloaded the remote stuck set and merged pending entries — the remote
        # row was preserved because we only union, never subtract. Capture
        # per-repo number-level tombstones at entry and apply them after the
        # reload+merge.
        stuck_deletions: dict[str, set[int]] = {}
        for repo, numbers in self._last_persisted_stuck.items():
            current = self._stuck_issues.get(repo, set())
            removed = numbers - current
            if removed:
                stuck_deletions[repo] = removed
        attempts = 0
        while True:
            cursors_unchanged = self._cursors == self._last_persisted_cursors
            stuck_unchanged = self._stuck_issues == self._last_persisted_stuck
            if cursors_unchanged and stuck_unchanged:
                logger.debug(
                    "github-watcher: coord doc write skipped — cursors and "
                    "stuck-set unchanged"
                )
                return
            attempts += 1
            body = format_cursors(self._cursors, self._stuck_issues)
            try:
                result = await self._lithos.note_write(
                    agent=self._agent_id,
                    id=self._coord_doc_id,
                    path=self._coord_doc_path if self._coord_doc_id is None else None,
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
                self._last_persisted_cursors = dict(self._cursors)
                self._last_persisted_stuck = _copy_stuck(self._stuck_issues)
                stuck_count = sum(len(s) for s in self._stuck_issues.values())
                logger.info(
                    "github-watcher: coord doc %s → v%d (%d cursor(s), %d stuck)",
                    result.status,
                    result.note.version,
                    len(self._cursors),
                    stuck_count,
                )
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
                # Hold our pending advances + stuck-issue set; the load step will
                # replace ``_cursors`` and ``_stuck_issues`` with the remote
                # view, then we re-merge so the just-observed advances aren't
                # lost.
                pending = dict(self._cursors)
                pending_stuck = _copy_stuck(self._stuck_issues)
                await self.load()
                for repo, ts in pending.items():
                    remote_ts = self._cursors.get(repo)
                    if remote_ts is None or ts > remote_ts:
                        self._cursors[repo] = ts
                # Merge pending stuck entries: union per repo. Remote may have
                # stuck entries from another writer we want to keep, and we may
                # have new ones from this poll.
                for repo, numbers in pending_stuck.items():
                    self._stuck_issues.setdefault(repo, set()).update(numbers)
                # Re-apply intended deletions captured at function entry
                # (PR-review finding 1, round 5, 2026-05-30). Without this, a
                # cursor we explicitly popped is restored by reload and silently
                # lives on in the next write.
                for repo in deletions:
                    self._cursors.pop(repo, None)
                # Same tombstone re-application for stuck entries — PR-review
                # finding 3, round 6, 2026-05-30. Without this, draining a row
                # locally and then hitting a CAS conflict resurrects the row from
                # the remote view.
                for repo, numbers in stuck_deletions.items():
                    remote_set = self._stuck_issues.get(repo)
                    if remote_set is None:
                        continue
                    remote_set.difference_update(numbers)
                    if not remote_set:
                        self._stuck_issues.pop(repo, None)
                continue
            logger.warning(
                "github-watcher: unexpected coord doc write status %r: %s",
                result.status,
                result.message,
            )
            return
