"""One representation for a PR's external review material (#355): the model.

Three GitHub streams carry review activity on a delivered PR — summary
**reviews**, **inline** review comments, and **Conversation**-tab comments —
each with its own endpoint, id space and cursor. This module is the
normalised row (:class:`ExternalReviewActivity`), its stream enum, and the
converters from the raw GitHub types. Everything stream-*specific* beyond
the conversion — fetch, cursor keys, actionability, rendering, the reply-id
projection — lives in the adapter registry in
:mod:`lithos_loom.github_review_streams`, which is infrastructure, not a
domain entity, and is kept out of the domain diagram on purpose.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from .github_models import (
    IssueComment,
    PullRequestReview,
    PullRequestReviewComment,
    issue_comment_reply_target,
)

__all__ = [
    "ActivityKey",
    "ExternalReviewActivity",
    "ReviewStream",
    "from_conversation_comment",
    "from_inline_comment",
    "from_review",
]


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


# ── converters from the raw GitHub types ───────────────────────────────


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
