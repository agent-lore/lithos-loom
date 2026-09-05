"""Tests for ``lithos_loom.github_review_activity`` (#355).

One representation for a PR's external review material — summary reviews,
inline review comments and Conversation-tab comments — plus the shared pure
decisions (actionability, authenticated handled-proof, trust) that the
watcher sweep and the converge fetch both consume.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest

from lithos_loom.github_client import (
    GitHubError,
    IssueComment,
    PullRequestReview,
    PullRequestReviewComment,
)
from lithos_loom.github_models import AUTOMATED_REPLY_MARKER, issue_comment_reply_body
from lithos_loom.github_review_activity import (
    ExternalReviewActivity,
    ReviewStream,
    from_conversation_comment,
    from_inline_comment,
    from_review,
)
from lithos_loom.github_review_streams import (
    STREAM_ADAPTERS,
    AuthorTrust,
    actionable,
    adapter_for,
    fetch_activity,
    handled_review_ids,
    landed_fix_claims,
    proven_handled,
    render_row,
)

_REPO = "agent-lore/lithos-lens"
_PR = 62
_HEAD = "e" * 40
_FIXED = f"Fixed in abc123def4 — guarded it\n\n{AUTOMATED_REPLY_MARKER}"


def _review(
    review_id: int,
    *,
    state: str = "CHANGES_REQUESTED",
    body: str = "two problems",
    author: str = "reviewer",
) -> PullRequestReview:
    return PullRequestReview(
        author=author, body=body, review_id=review_id, state=state, commit_id=_HEAD
    )


def _inline(
    comment_id: int,
    *,
    author: str = "reviewer",
    body: str = "leaks a handle",
    in_reply_to_id: int | None = None,
    review_id: int | None = None,
) -> PullRequestReviewComment:
    return PullRequestReviewComment(
        comment_id=comment_id,
        author=author,
        path="src/x.py",
        line=12,
        body=body,
        in_reply_to_id=in_reply_to_id,
        html_url=f"https://github.com/{_REPO}/pull/{_PR}#discussion_r{comment_id}",
        commit_id="",
        original_commit_id=_HEAD,
        updated_at=datetime(2026, 9, 5, 10, 0, tzinfo=UTC),
        pull_request_review_id=review_id,
    )


def _conversation(
    comment_id: int, *, author: str = "davesnowdon", body: str = "Verdict: two P1 gaps"
) -> IssueComment:
    return IssueComment(
        comment_id=comment_id,
        author=author,
        body=body,
        html_url=f"https://github.com/{_REPO}/pull/{_PR}#issuecomment-{comment_id}",
        updated_at=datetime(2026, 9, 5, 10, 25, tzinfo=UTC),
    )


def _trust(
    permissions: dict[str, Any] | None = None, *, bots: tuple[str, ...] = ()
) -> AuthorTrust:
    perms = permissions or {}

    async def permission_of(author: str) -> str:
        value = perms.get(author, "none")
        if isinstance(value, Exception):
            raise value
        return value

    return AuthorTrust(permission_of, bots=bots)


# ── adapters ───────────────────────────────────────────────────────────


def test_adapters_normalise_each_stream_with_its_provenance() -> None:
    r = from_review(_review(500), repo=_REPO, pr_number=_PR)
    assert r.stream is ReviewStream.REVIEW and r.activity_id == 500
    assert r.url == f"https://github.com/{_REPO}/pull/{_PR}#pullrequestreview-500"
    assert r.head_sha == _HEAD and r.review_state == "CHANGES_REQUESTED"
    assert r.reply_to is None and r.key == (ReviewStream.REVIEW, 500)

    c = from_inline_comment(_inline(7, in_reply_to_id=3, review_id=500))
    assert c.stream is ReviewStream.INLINE and c.activity_id == 7
    assert c.path == "src/x.py" and c.line == 12
    assert c.head_sha == _HEAD  # commit_id empty → original_commit_id
    assert c.reply_to == 3 and c.is_reply and c.root_key == (ReviewStream.INLINE, 3)
    assert c.owning_review_id == 500

    reply = issue_comment_reply_body(
        _FIXED, f"https://github.com/{_REPO}/pull/{_PR}#issuecomment-20"
    )
    v = from_conversation_comment(_conversation(21, author="dave", body=reply))
    assert v.stream is ReviewStream.CONVERSATION and v.head_sha == ""
    assert v.reply_to == 20 and v.root_key == (ReviewStream.CONVERSATION, 20)
    plain = from_conversation_comment(_conversation(22))
    assert plain.reply_to is None and not plain.is_reply


def test_stream_adapters_cover_every_stream_once_in_finding_order() -> None:
    assert [a.stream for a in STREAM_ADAPTERS] == list(ReviewStream)
    assert [a.mark_id_key for a in STREAM_ADAPTERS] == [
        "last_review_id",
        "last_comment_id",
        "last_issue_comment_id",
    ]
    assert [a.mark_at_key for a in STREAM_ADAPTERS] == [
        None,
        "last_comment_at",
        "last_issue_comment_at",
    ]


@pytest.mark.asyncio
async def test_fetch_activity_concatenates_streams_and_threads_since() -> None:
    github = AsyncMock()
    github.list_pull_request_reviews.return_value = [_review(500)]
    github.list_pull_request_review_comments.return_value = [_inline(7)]
    github.list_issue_comments.return_value = [_conversation(30)]
    since = datetime(2026, 9, 1, tzinfo=UTC)

    rows = await fetch_activity(
        github,
        _REPO,
        _PR,
        since={ReviewStream.INLINE: since, ReviewStream.CONVERSATION: None},
    )

    assert [(a.stream, a.activity_id) for a in rows] == [
        (ReviewStream.REVIEW, 500),
        (ReviewStream.INLINE, 7),
        (ReviewStream.CONVERSATION, 30),
    ]
    github.list_pull_request_review_comments.assert_awaited_once_with(
        _REPO, _PR, since=since
    )
    github.list_issue_comments.assert_awaited_once_with(_REPO, _PR, since=None)


@pytest.mark.asyncio
async def test_fetch_activity_propagates_listing_errors() -> None:
    github = AsyncMock()
    github.list_pull_request_reviews.side_effect = GitHubError("boom")
    with pytest.raises(GitHubError):
        await fetch_activity(github, _REPO, _PR)


# ── handled proof ──────────────────────────────────────────────────────


def _rows(*items: Any) -> list[ExternalReviewActivity]:
    out: list[ExternalReviewActivity] = []
    for it in items:
        if isinstance(it, PullRequestReview):
            out.append(from_review(it, repo=_REPO, pr_number=_PR))
        elif isinstance(it, PullRequestReviewComment):
            out.append(from_inline_comment(it))
        else:
            out.append(from_conversation_comment(it))
    return out


def test_landed_fix_claims_name_the_root_in_the_right_stream() -> None:
    conv_url = f"https://github.com/{_REPO}/pull/{_PR}#issuecomment-20"
    rows = _rows(
        _inline(1),
        _inline(2, author="dave", body=_FIXED, in_reply_to_id=1),
        _inline(
            3,
            author="dave",
            body=f"Not changed — nope\n\n{AUTOMATED_REPLY_MARKER}",
            in_reply_to_id=1,
        ),
        _conversation(20),
        _conversation(
            21, author="dave", body=issue_comment_reply_body(_FIXED, conv_url)
        ),
        _conversation(22, author="dave", body=_FIXED),  # landed, but names no target
    )
    assert landed_fix_claims(rows) == [
        ((ReviewStream.INLINE, 1), "dave"),
        ((ReviewStream.CONVERSATION, 20), "dave"),
    ]


@pytest.mark.asyncio
async def test_proven_handled_authenticates_the_reply_author() -> None:
    rows = _rows(
        _inline(1),
        _inline(2, author="dave", body=_FIXED, in_reply_to_id=1),
        _inline(3),
        _inline(4, author="stranger", body=_FIXED, in_reply_to_id=3),
    )
    handled = await proven_handled(rows, _trust({"dave": "write", "stranger": "read"}))
    assert handled == frozenset({(ReviewStream.INLINE, 1)})


@pytest.mark.asyncio
async def test_proven_handled_fails_closed_on_a_probe_error_and_reports_it() -> None:
    errors: list[tuple[str, Exception]] = []
    rows = _rows(_inline(1), _inline(2, author="dave", body=_FIXED, in_reply_to_id=1))

    async def permission_of(author: str) -> str:
        raise GitHubError("transient")

    trust = AuthorTrust(permission_of, on_error=lambda a, exc: errors.append((a, exc)))
    assert await proven_handled(rows, trust) == frozenset()
    assert [a for a, _ in errors] == ["dave"]


@pytest.mark.asyncio
async def test_author_trust_probes_each_author_once_and_allowlists_bots() -> None:
    calls: list[str] = []

    async def permission_of(author: str) -> str:
        calls.append(author)
        return "admin" if author == "dave" else "none"

    trust = AuthorTrust(permission_of, bots=("copilot[bot]",))
    assert await trust.is_trusted("dave") and await trust.is_trusted("dave")
    assert await trust.source("copilot[bot]") == ("bot", True)
    assert await trust.source("dave") == ("human", True)
    assert await trust.source("stranger") == ("human", False)
    assert calls == ["dave", "stranger"]  # the bot is never probed


# ── actionability ──────────────────────────────────────────────────────


def test_actionable_applies_every_stream_rule() -> None:
    conv_url = f"https://github.com/{_REPO}/pull/{_PR}#issuecomment-20"
    rows = _rows(
        _review(500, state="APPROVED", body=""),  # silent state
        _review(501, state="COMMENTED", body="  "),  # empty summary
        _review(502, state="COMMENTED", body="looks off"),
        _review(503),  # CHANGES_REQUESTED, no body — always posts
        _inline(1),
        _inline(2, author="dave", body=_FIXED, in_reply_to_id=1),  # reply
        _inline(3, body=f"Not changed — x\n\n{AUTOMATED_REPLY_MARKER}"),  # loom's own
        _inline(4),
        _conversation(20),
        _conversation(
            21, author="dave", body=issue_comment_reply_body(_FIXED, conv_url)
        ),
        _conversation(22, body="   "),
        _conversation(23),
    )
    handled = frozenset({(ReviewStream.INLINE, 1), (ReviewStream.CONVERSATION, 20)})
    assert [(a.stream, a.activity_id) for a in actionable(rows, handled)] == [
        (ReviewStream.REVIEW, 502),
        (ReviewStream.REVIEW, 503),
        (ReviewStream.INLINE, 4),
        (ReviewStream.CONVERSATION, 23),
    ]


def test_handled_review_suppression_is_bound_to_the_owning_review() -> None:
    """PR #345 review F3: a non-blocking summary review is suppressed only
    when ALL of its own inline roots are handled — never author-wide, and
    never for CHANGES_REQUESTED."""
    rows = _rows(
        _review(500, state="COMMENTED", body="see inline"),
        _inline(1, review_id=500),
        _inline(2, review_id=500),
        _review(600, state="COMMENTED", body="newer pass"),
        _inline(3, review_id=600),
        _review(700, body="blocking"),  # CHANGES_REQUESTED
        _inline(4, review_id=700),
    )
    handled = frozenset(
        {(ReviewStream.INLINE, 1), (ReviewStream.INLINE, 2), (ReviewStream.INLINE, 4)}
    )
    assert handled_review_ids(rows, handled) == frozenset({500, 700})
    # 500 suppressed (all roots handled); 600 posts (root 3 live); 700 posts
    # despite handled roots (CHANGES_REQUESTED is never suppressed).
    assert [
        a.activity_id
        for a in actionable(rows, handled)
        if a.stream is ReviewStream.REVIEW
    ] == [600, 700]


def test_actionable_candidates_can_be_a_subset_of_the_context() -> None:
    """The sweep decides on NEW rows but proves handled-ness over the whole
    fetched history: a review whose roots sit below the id mark still counts
    as handled by them."""
    rows = _rows(
        _review(500, state="COMMENTED", body="see inline"), _inline(1, review_id=500)
    )
    handled = frozenset({(ReviewStream.INLINE, 1)})
    new = [rows[0]]
    assert actionable(new, handled, context=rows) == []
    assert [a.activity_id for a in actionable(new, handled)] == [
        500
    ]  # no context → no roots known


# ── the registry is the ONLY policy site (PR #356 review, finding 1) ──


def test_every_stream_policy_is_registered_exhaustively() -> None:
    assert {a.stream for a in STREAM_ADAPTERS} == set(ReviewStream)
    for a in STREAM_ADAPTERS:
        assert adapter_for(a.stream) is a
        assert callable(a.fetch) and callable(a.is_actionable) and callable(a.render)
        assert a.label and a.finding_id_field


def test_an_unregistered_stream_fails_loudly_never_as_a_catch_all(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Remove a stream's adapter and every stream-indexed decision must raise
    — not fall through to another stream's policy."""
    from lithos_loom import github_review_streams as streams

    registry = {
        a.stream: a
        for a in STREAM_ADAPTERS
        if a.stream is not ReviewStream.CONVERSATION
    }
    monkeypatch.setattr(streams, "_BY_STREAM", registry)
    row = from_conversation_comment(_conversation(20))
    with pytest.raises(LookupError):
        adapter_for(ReviewStream.CONVERSATION)
    with pytest.raises(LookupError):
        actionable([row], frozenset())
    with pytest.raises(LookupError):
        render_row(row)


def test_render_row_uses_each_streams_own_line_shape() -> None:
    conv = f"https://github.com/{_REPO}/pull/{_PR}#issuecomment-20"
    assert render_row(from_review(_review(500), repo=_REPO, pr_number=_PR)) == (
        f"- review by reviewer (CHANGES_REQUESTED, at {_HEAD[:12]}): two problems"
    )
    assert render_row(from_inline_comment(_inline(7))) == (
        f"- comment by reviewer on src/x.py:12: leaks a handle "
        f"(https://github.com/{_REPO}/pull/{_PR}#discussion_r7)"
    )
    assert render_row(from_conversation_comment(_conversation(20))) == (
        "- comment by davesnowdon on the PR conversation: Verdict: two P1 gaps "
        f"({conv})"
    )
