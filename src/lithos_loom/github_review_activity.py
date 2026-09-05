"""One representation for a PR's external review material (#355).

Three GitHub streams carry review activity on a delivered PR — summary
**reviews**, **inline** review comments, and **Conversation**-tab comments —
each with its own endpoint, id space and cursor. Once a row is normalised,
everything that decides what to *do* with it is stream-agnostic: which rows
are actionable, which roots an authenticated landed-fix reply proves handled,
and who is trusted (ADR 0011 decision 8). This module owns that normalisation
and those pure decisions. The watcher sweep
(:mod:`lithos_loom.subscriptions.external_reviews`) and the converge fetch
(:mod:`lithos_loom.plugins.story_develop.external_reviews`) both consume it,
so the two paths cannot drift apart on what is still live.

Stream-specific knowledge lives in exactly one place — :data:`STREAM_ADAPTERS`
— so adding a fourth source is one :class:`ReviewStream` member plus one
:class:`StreamAdapter` entry (fetch + cursor keys + rendering label), not a
parallel-list expansion across fetch, cursoring, suppression, rendering, the
pending trigger, trust, intake and replies.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from .github_client import (
    GitHubClient,
    IssueComment,
    PullRequestReview,
    PullRequestReviewComment,
)
from .github_models import (
    is_automated_reply,
    is_landed_fix_reply,
    issue_comment_is_actionable,
    issue_comment_reply_target,
    review_is_actionable,
)

__all__ = [
    "STREAM_ADAPTERS",
    "TRUSTED_PERMISSIONS",
    "ActivityKey",
    "AuthorTrust",
    "ExternalReviewActivity",
    "ReviewStream",
    "StreamAdapter",
    "actionable",
    "fetch_activity",
    "from_conversation_comment",
    "from_inline_comment",
    "from_review",
    "handled_review_ids",
    "landed_fix_claims",
    "proven_handled",
]

# Repo permission levels whose holders may seed a coder and whose landed-fix
# replies count as proof — the ADR 0011 trust line.
TRUSTED_PERMISSIONS = frozenset({"admin", "write"})


class ReviewStream(StrEnum):
    """The GitHub stream a row came from — also its id space."""

    REVIEW = "review"
    INLINE = "inline"
    CONVERSATION = "conversation"


# A row's identity: ids are only unique WITHIN a stream (an inline comment and
# a conversation comment can share an int), so every key carries its stream.
ActivityKey = tuple[ReviewStream, int]


@dataclass(frozen=True)
class ExternalReviewActivity:
    """One normalised row of review activity.

    ``head_sha`` is the commit the reviewer actually read (empty for a
    conversation comment — it reviews the PR, not a commit). ``reply_to``
    names the root this row answers: ``in_reply_to_id`` for an inline
    thread reply, the ``_(replying to …)_`` target for a loom conversation
    reply. ``owning_review_id`` binds an inline root to the summary review it
    belongs to (the PR #345 F3 suppression scope).
    """

    stream: ReviewStream
    activity_id: int
    author: str
    body: str
    url: str
    head_sha: str = ""
    path: str = ""
    line: int | None = None
    review_state: str = ""
    owning_review_id: int | None = None
    reply_to: int | None = None
    updated_at: datetime | None = None

    @property
    def key(self) -> ActivityKey:
        return (self.stream, self.activity_id)

    @property
    def is_reply(self) -> bool:
        return self.reply_to is not None

    @property
    def root_key(self) -> ActivityKey | None:
        """The key of the row this one replies to (same stream), if any."""
        return None if self.reply_to is None else (self.stream, self.reply_to)


# ── adapters: the only stream-specific code ────────────────────────────


def from_review(
    review: PullRequestReview, *, repo: str, pr_number: int
) -> ExternalReviewActivity:
    return ExternalReviewActivity(
        stream=ReviewStream.REVIEW,
        activity_id=review.review_id,
        author=review.author,
        body=review.body,
        url=f"https://github.com/{repo}/pull/{pr_number}#pullrequestreview-{review.review_id}",
        head_sha=review.commit_id,
        review_state=review.state,
        updated_at=review.submitted_at,
    )


def from_inline_comment(comment: PullRequestReviewComment) -> ExternalReviewActivity:
    return ExternalReviewActivity(
        stream=ReviewStream.INLINE,
        activity_id=comment.comment_id,
        author=comment.author,
        body=comment.body,
        url=comment.html_url,
        head_sha=comment.commit_id or comment.original_commit_id,
        path=comment.path,
        line=comment.line,
        owning_review_id=comment.pull_request_review_id,
        reply_to=comment.in_reply_to_id,
        updated_at=comment.updated_at,
    )


def from_conversation_comment(comment: IssueComment) -> ExternalReviewActivity:
    # No thread structure on the conversation: a loom reply names its target
    # in its reply line, and that is the only reply relation there is.
    return ExternalReviewActivity(
        stream=ReviewStream.CONVERSATION,
        activity_id=comment.comment_id,
        author=comment.author,
        body=comment.body,
        url=comment.html_url,
        reply_to=issue_comment_reply_target(comment.body),
        updated_at=comment.updated_at,
    )


Fetch = Callable[
    [GitHubClient, str, int, datetime | None], Awaitable[list[ExternalReviewActivity]]
]


@dataclass(frozen=True)
class StreamAdapter:
    """Everything the sweep needs to know about one stream, in one row.

    ``mark_id_key`` / ``mark_at_key`` are the gate-marker keys for the
    stream's id high-water mark and ``since`` cursor (``None`` when the
    endpoint has no ``since``). ``fetch`` lists the stream, bounded by the
    cursor, as normalised rows. ``label`` is the finding's noun for a row.
    """

    stream: ReviewStream
    mark_id_key: str
    mark_at_key: str | None
    fetch: Fetch
    label: str


async def _fetch_reviews(
    github: GitHubClient, repo: str, number: int, since: datetime | None
) -> list[ExternalReviewActivity]:
    del since  # /reviews has no `since`; the id mark is the only bound
    rows = await github.list_pull_request_reviews(repo, number)
    return [from_review(r, repo=repo, pr_number=number) for r in rows]


async def _fetch_inline(
    github: GitHubClient, repo: str, number: int, since: datetime | None
) -> list[ExternalReviewActivity]:
    rows = await github.list_pull_request_review_comments(repo, number, since=since)
    return [from_inline_comment(c) for c in rows]


async def _fetch_conversation(
    github: GitHubClient, repo: str, number: int, since: datetime | None
) -> list[ExternalReviewActivity]:
    rows = await github.list_issue_comments(repo, number, since=since)
    return [from_conversation_comment(c) for c in rows]


# In the order a finding lists them. The marker keys are the on-disk contract
# of `metadata.external_review_seen` on every gate in the wild — never rename.
STREAM_ADAPTERS: tuple[StreamAdapter, ...] = (
    StreamAdapter(
        ReviewStream.REVIEW, "last_review_id", None, _fetch_reviews, "review"
    ),
    StreamAdapter(
        ReviewStream.INLINE,
        "last_comment_id",
        "last_comment_at",
        _fetch_inline,
        "inline comment",
    ),
    StreamAdapter(
        ReviewStream.CONVERSATION,
        "last_issue_comment_id",
        "last_issue_comment_at",
        _fetch_conversation,
        "conversation comment",
    ),
)


async def fetch_activity(
    github: GitHubClient,
    repo: str,
    pr_number: int,
    *,
    since: Mapping[ReviewStream, datetime | None] | None = None,
) -> list[ExternalReviewActivity]:
    """Every stream's rows, concatenated in stream order. Raises ``GitHubError``
    on any listing failure (the caller decides whether that is retryable)."""
    cursors = since or {}
    rows: list[ExternalReviewActivity] = []
    for adapter in STREAM_ADAPTERS:
        rows.extend(
            await adapter.fetch(github, repo, pr_number, cursors.get(adapter.stream))
        )
    return rows


# ── trust ──────────────────────────────────────────────────────────────

PermissionOf = Callable[[str], Awaitable[str]]
ProbeError = Callable[[str, Exception], None]


class AuthorTrust:
    """Per-batch answer to "may this author's material act?" (ADR 0011 d8).

    Allowlisted bot logins are trusted without a probe; a human is trusted
    with repo write/admin. One permission probe per unseen author, cached
    for the batch. A probe failure is **untrusted** — fail closed for both
    uses (seeding a coder, and proving a root handled) — and reported to
    ``on_error`` so the caller can log it its own way.
    """

    def __init__(
        self,
        permission_of: PermissionOf,
        *,
        bots: Iterable[str] = (),
        on_error: ProbeError | None = None,
    ) -> None:
        self._permission_of = permission_of
        self._bots = frozenset(bots)
        self._on_error = on_error
        self._cache: dict[str, str] = {}

    async def permission(self, author: str) -> str:
        cached = self._cache.get(author)
        if cached is not None:
            return cached
        try:
            value = await self._permission_of(author)
        except Exception as exc:  # noqa: BLE001 — any probe failure = unverified
            if self._on_error is not None:
                self._on_error(author, exc)
            value = "none"
        self._cache[author] = value
        return value

    async def is_trusted(self, author: str) -> bool:
        if author in self._bots:
            return True
        return await self.permission(author) in TRUSTED_PERMISSIONS

    async def source(self, author: str) -> tuple[str, bool]:
        """``("bot" | "human", trusted)`` — the intake's provenance pair."""
        if author in self._bots:
            return "bot", True
        return "human", await self.is_trusted(author)


# ── pure decisions ─────────────────────────────────────────────────────


def landed_fix_claims(
    activities: Sequence[ExternalReviewActivity],
) -> list[tuple[ActivityKey, str]]:
    """``(root_key, reply_author)`` pairs whose reply *claims* a landed fix.

    A claim, not proof: the marker and the ``Fixed in`` head are public body
    strings any commenter can copy. :func:`proven_handled` authenticates the
    author before the claim may suppress anything. Only the ``Fixed in
    <sha>`` shape counts — a held-back (red gate) or "Not changed" reply
    carries the same marker while the root is still unresolved (PR #344
    re-review 1).
    """
    return [
        (a.root_key, a.author)
        for a in activities
        if a.root_key is not None and is_landed_fix_reply(a.body)
    ]


async def proven_handled(
    activities: Sequence[ExternalReviewActivity], trust: AuthorTrust
) -> frozenset[ActivityKey]:
    """Root keys proven handled by an **authenticated** landed-fix reply.

    Backfill guard (PR #344 review, finding 2): pre-S2 delivery remediated
    inline roots and replied before the ``pr`` gate existed, and converge's
    own replies keep producing landed-fix replies; without this a markerless
    gate's first sweep — or every converge fetch — would re-report handled
    history. Authenticated (PR #344 re-review 2): the reply author must be
    trusted, or any outside commenter could hide a real root forever by
    copying two body strings. A probe failure fails closed (a duplicate
    report is recoverable, a hidden root is not).
    """
    handled: set[ActivityKey] = set()
    for root_key, author in landed_fix_claims(activities):
        if await trust.is_trusted(author):
            handled.add(root_key)
    return frozenset(handled)


def handled_review_ids(
    activities: Sequence[ExternalReviewActivity], handled: frozenset[ActivityKey]
) -> frozenset[int]:
    """Summary reviews ALL of whose own inline roots are handled.

    Bound to the review that OWNS the roots (PR #345 review F3) — an
    author-wide rule would hide a later summary re-review behind an ancient
    fixed root. A review with no inline roots is never in this set.
    """
    roots: dict[int, list[ActivityKey]] = {}
    for a in activities:
        if (
            a.stream is ReviewStream.INLINE
            and not a.is_reply
            and a.owning_review_id is not None
        ):
            roots.setdefault(a.owning_review_id, []).append(a.key)
    return frozenset(
        rid for rid, keys in roots.items() if keys and all(k in handled for k in keys)
    )


def _is_actionable(
    a: ExternalReviewActivity,
    handled: frozenset[ActivityKey],
    handled_reviews: frozenset[int],
) -> bool:
    if a.stream is ReviewStream.REVIEW:
        review = PullRequestReview(author=a.author, body=a.body, state=a.review_state)
        if not review_is_actionable(review):
            return False
        # CHANGES_REQUESTED is never suppressed — a reply does not prove the
        # requested changes were accepted.
        return (
            a.review_state == "CHANGES_REQUESTED"
            or a.activity_id not in handled_reviews
        )
    if a.stream is ReviewStream.INLINE:
        if a.is_reply:
            return False  # thread replies ride on their root comment
        return a.key not in handled and not is_automated_reply(a.body)
    comment = IssueComment(comment_id=a.activity_id, author=a.author, body=a.body)
    return a.key not in handled and issue_comment_is_actionable(comment)


def actionable(
    candidates: Sequence[ExternalReviewActivity],
    handled: frozenset[ActivityKey],
    *,
    context: Sequence[ExternalReviewActivity] | None = None,
) -> list[ExternalReviewActivity]:
    """The rows of *candidates* worth reporting / injecting, in input order.

    Per stream: a summary review by the per-state policy (PRD S2) minus the
    handled-review suppression; an inline root that is not a reply, not
    loom's own, and not proven handled; a conversation comment that is
    non-empty, not loom's own, and not proven handled. ``context`` (default:
    the candidates) is the full fetched history the review-ownership
    suppression is computed over — the sweep passes everything it fetched
    while deciding only on the rows above its marks.
    """
    handled_reviews = handled_review_ids(
        candidates if context is None else context, handled
    )
    return [a for a in candidates if _is_actionable(a, handled, handled_reviews)]
