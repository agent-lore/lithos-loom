"""The stream registry and the shared decisions over review activity (#355).

Everything that is *specific to one stream* — how to fetch it, which
gate-marker keys hold its cursor, whether one of its rows is actionable (or
a bare approval, which asks for nothing), how a row renders in the
``[ExternalReview]`` finding, and which ``ExternalFinding`` id the reply
epilogue answers it on — lives in exactly one :class:`StreamAdapter` row of
:data:`STREAM_ADAPTERS`. Every consumer looks its policy up through
:func:`adapter_for`, which is **exhaustive over :class:`ReviewStream`** and
fails loudly on an unregistered stream: there is no catch-all branch
anywhere (PR #356 review, finding 1). Adding a stream is one enum member
plus one adapter row, and forgetting a policy is an import-time error, not a
silent mis-classification.

The decisions that are stream-*agnostic* — the authenticated landed-fix
proof, the owning-review suppression scope, trust — live here too, so the
watcher sweep (:mod:`lithos_loom.subscriptions.external_reviews`) and the
converge fetch (:mod:`lithos_loom.plugins.story_develop.external_reviews`)
cannot drift apart on what is still live.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from .github_client import GitHubClient
from .github_models import (
    IssueComment,
    PullRequestReview,
    is_approval_text,
    is_automated_reply,
    is_landed_fix_reply,
    issue_comment_is_actionable,
    review_is_actionable,
)
from .github_review_activity import (
    ActivityKey,
    ExternalReviewActivity,
    ReviewStream,
    from_conversation_comment,
    from_inline_comment,
    from_review,
)

__all__ = [
    "ADAPTERS_BY_STREAM",
    "STREAM_ADAPTERS",
    "TRUSTED_PERMISSIONS",
    "AuthorTrust",
    "ReplyMode",
    "StreamAdapter",
    "actionable",
    "adapter_for",
    "dispositions",
    "excerpt",
    "fetch_activity",
    "handled_review_ids",
    "landed_fix_claims",
    "proven_handled",
    "render_row",
    "reviews_with_comments",
]

# Repo permission levels whose holders may seed a coder and whose landed-fix
# replies count as proof — the ADR 0011 trust line.
TRUSTED_PERMISSIONS = frozenset({"admin", "write"})

# A rendered row is a breadcrumb, not a transcript.
EXCERPT_CHARS = 160


class ReplyMode(StrEnum):
    """How loom answers a row of this stream after acting on it — a reply
    *capability*, chosen per stream in its adapter row (PR #356 re-review).

    A new stream selects an existing capability here; a genuinely new
    transport is a new member plus a transport in the epilogue's table,
    which is exhaustive over this enum.
    """

    NONE = "none"  # nothing to answer on (a summary review has no thread)
    THREAD = "thread"  # an inline review-comment thread reply
    CONVERSATION = "conversation"  # a Conversation-tab comment naming its target


Fetch = Callable[
    [GitHubClient, str, int, datetime | None], Awaitable[list[ExternalReviewActivity]]
]
Actionable = Callable[
    [ExternalReviewActivity, frozenset[ActivityKey], frozenset[int]], bool
]
# (row, proven-handled keys, handled summary-review ids, review ids that own
# inline roots) — the approval rule needs the same suppression inputs as
# ``Actionable`` plus the ownership set, so an APPROVED review whose own
# comments carry the asks is not reported as "nothing to remediate".
Approval = Callable[
    [
        ExternalReviewActivity,
        frozenset[ActivityKey],
        frozenset[int],
        frozenset[int],
    ],
    bool,
]
Render = Callable[[ExternalReviewActivity], str]


@dataclass(frozen=True)
class StreamAdapter:
    """One stream's complete policy, in one row.

    - ``mark_id_key`` / ``mark_at_key``: the gate-marker keys for the stream's
      id high-water mark and ``since`` cursor (``None`` when the endpoint has
      no ``since``). The on-disk contract of ``external_review_seen`` —
      never rename.
    - ``fetch``: list the stream, bounded by the cursor, as normalised rows.
    - ``is_actionable``: the stream's reporting/injection rule, given the
      proven-handled keys and the handled summary-review ids.
    - ``is_approval``: the stream's "this row asks for nothing" rule — the
      row is reported with the ``approval`` disposition and never injected,
      remediated or budgeted against. Checked BEFORE ``is_actionable``.
    - ``render``: the row's line in the ``[ExternalReview]`` finding.
    - ``label``: the finding's noun for a row, for the sweep log.
    - ``reply_mode``: the reply capability the epilogue answers a row with.
    """

    stream: ReviewStream
    mark_id_key: str
    mark_at_key: str | None
    fetch: Fetch
    is_actionable: Actionable
    is_approval: Approval
    render: Render
    label: str
    reply_mode: ReplyMode


def excerpt(body: str) -> str:
    line = " ".join(body.strip().split())
    if len(line) > EXCERPT_CHARS:
        return line[: EXCERPT_CHARS - 1] + "…"
    return line


# ── the REVIEW stream ──────────────────────────────────────────────────


async def _fetch_reviews(
    github: GitHubClient, repo: str, number: int, since: datetime | None
) -> list[ExternalReviewActivity]:
    del since  # /reviews has no `since`; the id mark is the only bound
    rows = await github.list_pull_request_reviews(repo, number)
    return [from_review(r, repo=repo, pr_number=number) for r in rows]


def _review_actionable(
    a: ExternalReviewActivity,
    handled: frozenset[ActivityKey],
    handled_reviews: frozenset[int],
) -> bool:
    del handled  # a review is suppressed via its OWNING roots, never its own key
    review = PullRequestReview(author=a.author, body=a.body, state=a.review_state)
    if not review_is_actionable(review):
        return False
    # CHANGES_REQUESTED is never suppressed — a reply does not prove the
    # requested changes were accepted.
    return a.review_state == "CHANGES_REQUESTED" or a.activity_id not in handled_reviews


def _review_approval(
    a: ExternalReviewActivity,
    handled: frozenset[ActivityKey],
    handled_reviews: frozenset[int],
    owns_comments: frozenset[int],
) -> bool:
    """An APPROVED review with no inline comments of its own, or any summary
    whose whole body is approval prose.

    A review that OWNS inline comments is never the approval: its comments
    carry the asks and speak for it (they are judged on their own stream).
    ``DISMISSED`` is not an approval either — a dismissal has had its say and
    keeps the existing silent policy.
    """
    del handled, handled_reviews
    if a.activity_id in owns_comments or a.review_state == "DISMISSED":
        return False
    if a.review_state == "APPROVED":
        # An APPROVED body that asks for something is not reported as an
        # approval; it keeps the per-state silent policy (PRD S2).
        return not a.body.strip() or is_approval_text(a.body)
    return is_approval_text(a.body)


def _render_review(a: ExternalReviewActivity) -> str:
    state = a.review_state or "COMMENTED"
    at = f", at {a.head_sha[:12]}" if a.head_sha else ""
    text = excerpt(a.body)
    tail = f": {text}" if text else ""
    return f"- review by {a.author} ({state}{at}){tail}"


# ── the INLINE stream ──────────────────────────────────────────────────


async def _fetch_inline(
    github: GitHubClient, repo: str, number: int, since: datetime | None
) -> list[ExternalReviewActivity]:
    rows = await github.list_pull_request_review_comments(repo, number, since=since)
    return [from_inline_comment(c) for c in rows]


def _inline_actionable(
    a: ExternalReviewActivity,
    handled: frozenset[ActivityKey],
    handled_reviews: frozenset[int],
) -> bool:
    del handled_reviews
    if a.is_reply:
        return False  # thread replies ride on their root comment
    return a.key not in handled and not is_automated_reply(a.body)


def _inline_approval(
    a: ExternalReviewActivity,
    handled: frozenset[ActivityKey],
    handled_reviews: frozenset[int],
    owns_comments: frozenset[int],
) -> bool:
    del owns_comments
    return _inline_actionable(a, handled, handled_reviews) and is_approval_text(a.body)


def _render_inline(a: ExternalReviewActivity) -> str:
    loc = f"{a.path}:{a.line}" if a.line else a.path
    url = f" ({a.url})" if a.url else ""
    return f"- comment by {a.author} on {loc}: {excerpt(a.body)}{url}"


# ── the CONVERSATION stream ────────────────────────────────────────────


async def _fetch_conversation(
    github: GitHubClient, repo: str, number: int, since: datetime | None
) -> list[ExternalReviewActivity]:
    rows = await github.list_issue_comments(repo, number, since=since)
    return [from_conversation_comment(c) for c in rows]


def _conversation_actionable(
    a: ExternalReviewActivity,
    handled: frozenset[ActivityKey],
    handled_reviews: frozenset[int],
) -> bool:
    del handled_reviews
    comment = IssueComment(comment_id=a.activity_id, author=a.author, body=a.body)
    return a.key not in handled and issue_comment_is_actionable(comment)


def _conversation_approval(
    a: ExternalReviewActivity,
    handled: frozenset[ActivityKey],
    handled_reviews: frozenset[int],
    owns_comments: frozenset[int],
) -> bool:
    del owns_comments
    return _conversation_actionable(a, handled, handled_reviews) and is_approval_text(
        a.body
    )


def _render_conversation(a: ExternalReviewActivity) -> str:
    url = f" ({a.url})" if a.url else ""
    return f"- comment by {a.author} on the PR conversation: {excerpt(a.body)}{url}"


# ── the registry ───────────────────────────────────────────────────────

# In the order a finding lists them.
STREAM_ADAPTERS: tuple[StreamAdapter, ...] = (
    StreamAdapter(
        stream=ReviewStream.REVIEW,
        mark_id_key="last_review_id",
        mark_at_key=None,
        fetch=_fetch_reviews,
        is_actionable=_review_actionable,
        is_approval=_review_approval,
        render=_render_review,
        label="review",
        reply_mode=ReplyMode.NONE,
    ),
    StreamAdapter(
        stream=ReviewStream.INLINE,
        mark_id_key="last_comment_id",
        mark_at_key="last_comment_at",
        fetch=_fetch_inline,
        is_actionable=_inline_actionable,
        is_approval=_inline_approval,
        render=_render_inline,
        label="inline comment",
        reply_mode=ReplyMode.THREAD,
    ),
    StreamAdapter(
        stream=ReviewStream.CONVERSATION,
        mark_id_key="last_issue_comment_id",
        mark_at_key="last_issue_comment_at",
        fetch=_fetch_conversation,
        is_actionable=_conversation_actionable,
        is_approval=_conversation_approval,
        render=_render_conversation,
        label="conversation comment",
        reply_mode=ReplyMode.CONVERSATION,
    ),
)

# Read-only view of the registry by stream (public so tests can stage an
# unregistered stream without a private reference).
ADAPTERS_BY_STREAM: dict[ReviewStream, StreamAdapter] = {
    a.stream: a for a in STREAM_ADAPTERS
}

# Exhaustive by construction: a ReviewStream member without an adapter row
# is an import-time error, never a row silently handled under some other
# stream's policy.
_missing = set(ReviewStream) - set(ADAPTERS_BY_STREAM)
if _missing or len(ADAPTERS_BY_STREAM) != len(STREAM_ADAPTERS):
    raise RuntimeError(
        f"STREAM_ADAPTERS must register every ReviewStream exactly once; "
        f"missing {sorted(_missing)}"
    )


def adapter_for(stream: ReviewStream) -> StreamAdapter:
    """The registered policy for *stream*; ``LookupError`` if unregistered."""
    try:
        return ADAPTERS_BY_STREAM[stream]
    except KeyError:
        raise LookupError(f"no StreamAdapter registered for {stream!r}") from None


def render_row(a: ExternalReviewActivity) -> str:
    return adapter_for(a.stream).render(a)


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


# ── stream-agnostic decisions ──────────────────────────────────────────


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


def reviews_with_comments(
    activities: Sequence[ExternalReviewActivity],
) -> frozenset[int]:
    """Summary reviews that own at least one inline ROOT comment.

    The approval rule's scope twin of :func:`handled_review_ids`: such a
    review is not "nothing to remediate" whatever its state says — its own
    comments carry the asks.
    """
    return frozenset(
        a.owning_review_id
        for a in activities
        if a.owning_review_id is not None and not a.is_reply
    )


def handled_review_ids(
    activities: Sequence[ExternalReviewActivity], handled: frozenset[ActivityKey]
) -> frozenset[int]:
    """Summary reviews ALL of whose own inline roots are handled.

    Bound to the review that OWNS the roots (PR #345 review F3) — an
    author-wide rule would hide a later summary re-review behind an ancient
    fixed root. A review with no inline roots is never in this set. This is
    the one cross-stream relation in the model (an inline root belongs to a
    summary review), so it is stated once here rather than in an adapter.
    """
    roots: dict[int, list[ActivityKey]] = {}
    for a in activities:
        if a.owning_review_id is not None and not a.is_reply:
            roots.setdefault(a.owning_review_id, []).append(a.key)
    return frozenset(
        rid for rid, keys in roots.items() if keys and all(k in handled for k in keys)
    )


def dispositions(
    candidates: Sequence[ExternalReviewActivity],
    handled: frozenset[ActivityKey],
    *,
    context: Sequence[ExternalReviewActivity] | None = None,
) -> tuple[list[ExternalReviewActivity], list[ExternalReviewActivity]]:
    """``(actionable, approvals)`` for *candidates*, in input order.

    The second list is the rows that ask for nothing — an APPROVED review, a
    "No findings / LGTM" comment. They are reported (the operator wants to
    know the PR was approved) and never acted on: no coder, no panel, no
    remediation round. Each row is classified by its own stream's registered
    rules, approval first (an approval is not a finding, and an APPROVED
    review is not "actionable" at all). ``context`` is the full fetched
    history the cross-row suppression scopes are computed over.
    """
    rows = candidates if context is None else context
    handled_reviews = handled_review_ids(rows, handled)
    owns_comments = reviews_with_comments(rows)
    act: list[ExternalReviewActivity] = []
    appr: list[ExternalReviewActivity] = []
    for a in candidates:
        adapter = adapter_for(a.stream)
        if adapter.is_approval(a, handled, handled_reviews, owns_comments):
            appr.append(a)
        elif adapter.is_actionable(a, handled, handled_reviews):
            act.append(a)
    return act, appr


def actionable(
    candidates: Sequence[ExternalReviewActivity],
    handled: frozenset[ActivityKey],
    *,
    context: Sequence[ExternalReviewActivity] | None = None,
) -> list[ExternalReviewActivity]:
    """The rows of *candidates* worth reporting / injecting, in input order.

    Each row is judged by its own stream's registered rule (PRD S2 per-state
    policy for reviews; not-a-reply / not-loom's / not-handled for inline
    roots; non-empty / not-loom's / not-handled for conversation comments).
    ``context`` (default: the candidates) is the full fetched history the
    review-ownership suppression is computed over — the sweep passes
    everything it fetched while deciding only on the rows above its marks.

    Approval-only rows are deliberately NOT filtered here: the converge
    intake feeds them to the S5a triage step, whose ``NOTHING_TO_REMEDIATE``
    verdict is the model-driven half of the answer. The watcher sweep, which
    decides whether to spend a round at all, splits them out with
    :func:`dispositions`.
    """
    handled_reviews = handled_review_ids(
        candidates if context is None else context, handled
    )
    return [
        a
        for a in candidates
        if adapter_for(a.stream).is_actionable(a, handled, handled_reviews)
    ]
