"""Tests for ``lithos_loom.subscriptions.external_reviews`` (PRD S2, detection).

The reconcile sweep's still-open ``pr``-gate branch ingests external review
activity: new PR reviews and inline review comments post a one-shot
``[ExternalReview]`` finding on the blocked story, de-duped by high-water
marks (``last_review_id`` / ``last_comment_id``) in a url-scoped
``external_review_seen`` marker on the GATE. Per-state policy:
``CHANGES_REQUESTED`` always posts; ``COMMENTED`` only with content;
``APPROVED`` / ``DISMISSED`` advance the marker silently. GitHub + Lithos are
stubbed exactly as in ``test_develop_pr_merge``.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

from lithos_loom.gates import create_pr_gate, parse_pr_gate
from lithos_loom.github_client import (
    GitHubError,
    PullRequestReview,
    PullRequestReviewComment,
)
from lithos_loom.subscriptions import SubscriptionContext
from lithos_loom.subscriptions.external_reviews import (
    EXTERNAL_REVIEW,
    REVIEW_SEEN_KEY,
    ingest_external_reviews,
)
from tests.support import FakeLithosClient

_PR_URL = "https://github.com/agent-lore/lithos-lens/pull/62"
_REPO = "agent-lore/lithos-lens"


def _ctx(lithos: Any) -> SubscriptionContext:
    return SubscriptionContext(
        lithos=lithos,
        logger=logging.getLogger("test-external-reviews"),
        agent_id="lithos-loom-agent",
    )


async def _gate_with_story(
    client: FakeLithosClient, *, pr_url: str = _PR_URL
) -> tuple[str, Any]:
    story = await client.task_create(title="US7", metadata={"project": "p"})
    gate_id = await create_pr_gate(
        client,
        story_id=story,
        story_title="US7",
        pr_url=pr_url,
        project="p",
        agent="a",
    )
    gate = await client.task_get(task_id=gate_id)
    return story, gate


def _github(
    *,
    reviews: list[PullRequestReview] | None = None,
    comments: list[PullRequestReviewComment] | None = None,
) -> AsyncMock:
    github = AsyncMock()
    github.list_pull_request_reviews.return_value = reviews or []
    github.list_pull_request_review_comments.return_value = comments or []
    return github


def _review(
    review_id: int,
    *,
    state: str = "CHANGES_REQUESTED",
    author: str = "reviewer-human",
    body: str = "two problems here",
) -> PullRequestReview:
    return PullRequestReview(
        author=author,
        body=body,
        review_id=review_id,
        state=state,
        submitted_at=datetime(2026, 8, 30, tzinfo=UTC),
    )


def _comment(
    comment_id: int,
    *,
    author: str = "reviewer-human",
    body: str = "this leaks a handle",
    in_reply_to_id: int | None = None,
    path: str = "src/x.py",
    line: int | None = 12,
) -> PullRequestReviewComment:
    return PullRequestReviewComment(
        comment_id=comment_id,
        author=author,
        path=path,
        line=line,
        body=body,
        in_reply_to_id=in_reply_to_id,
        html_url=f"https://github.com/{_REPO}/pull/62#discussion_r{comment_id}",
        commit_id="d" * 40,
        updated_at=datetime(2026, 8, 30, 12, 0, tzinfo=UTC),
    )


async def _run(
    client: FakeLithosClient, gate: Any, story_id: str | None, github: AsyncMock
) -> None:
    spec = parse_pr_gate(gate)
    assert spec is not None
    await ingest_external_reviews(gate, spec, story_id, github, _ctx(client))


async def _refresh(client: FakeLithosClient, gate: Any) -> Any:
    """Re-read the gate so the just-written marker is visible (asserts alive)."""
    fresh = await client.task_get(task_id=gate.id)
    assert fresh is not None
    return fresh


def _findings(client: FakeLithosClient) -> list[str]:
    return [f["summary"] for f in client._findings]


async def _marker(client: FakeLithosClient, gate_id: str) -> Any:
    gate = await client.task_get(task_id=gate_id)
    assert gate is not None
    return gate.metadata.get(REVIEW_SEEN_KEY)


# ── posting policy ─────────────────────────────────────────────────────


async def test_changes_requested_review_posts_finding_and_marks_gate() -> None:
    client = FakeLithosClient(agent_id="a")
    story, gate = await _gate_with_story(client)
    github = _github(reviews=[_review(500)])

    await _run(client, gate, story, github)

    posted = _findings(client)
    assert len(posted) == 1
    assert posted[0].startswith(EXTERNAL_REVIEW)
    assert "reviewer-human" in posted[0] and "CHANGES_REQUESTED" in posted[0]
    marker = await _marker(client, gate.id)
    assert marker["pr_url"] == _PR_URL and marker["last_review_id"] == 500


async def test_summary_only_changes_requested_posts_exactly_once() -> None:
    """The case comment-id de-dup cannot represent: a review with zero inline
    comments must post on first sight and never again."""
    client = FakeLithosClient(agent_id="a")
    story, gate = await _gate_with_story(client)
    github = _github(reviews=[_review(500)])

    await _run(client, gate, story, github)
    gate = await _refresh(client, gate)
    await _run(client, gate, story, github)

    assert len(_findings(client)) == 1


async def test_approved_and_dismissed_advance_marker_without_posting() -> None:
    client = FakeLithosClient(agent_id="a")
    story, gate = await _gate_with_story(client)
    github = _github(
        reviews=[_review(500, state="APPROVED"), _review(501, state="DISMISSED")]
    )

    await _run(client, gate, story, github)

    assert _findings(client) == []
    marker = await _marker(client, gate.id)
    assert marker["last_review_id"] == 501  # recorded, silent


async def test_commented_review_posts_only_with_content() -> None:
    client = FakeLithosClient(agent_id="a")
    story, gate = await _gate_with_story(client)
    github = _github(reviews=[_review(500, state="COMMENTED", body="  ")])
    await _run(client, gate, story, github)
    assert _findings(client) == []

    gate = await _refresh(client, gate)
    github = _github(reviews=[_review(501, state="COMMENTED", body="worth a look")])
    await _run(client, gate, story, github)
    assert len(_findings(client)) == 1


async def test_new_inline_comment_posts_with_location_and_thread_url() -> None:
    client = FakeLithosClient(agent_id="a")
    story, gate = await _gate_with_story(client)
    github = _github(comments=[_comment(111)])

    await _run(client, gate, story, github)

    posted = _findings(client)
    assert len(posted) == 1
    assert "src/x.py:12" in posted[0]
    assert "discussion_r111" in posted[0]
    marker = await _marker(client, gate.id)
    assert marker["last_comment_id"] == 111


async def test_looms_automated_reply_marker_is_in_lockstep() -> None:
    """The sweep skips loom's own PR replies by their AUTOMATED_MARKER, held as
    a duplicated literal (subscriptions must not import plugin internals). A
    drifting copy would make loom re-ingest its own replies as external
    findings — pin the two strings byte-for-byte via behaviour."""
    from lithos_loom.plugins.story_develop.pr_delivery import AUTOMATED_MARKER

    client = FakeLithosClient(agent_id="a")
    story, gate = await _gate_with_story(client)
    github = _github(
        comments=[_comment(111, body=f"Fixed in abc123.\n\n{AUTOMATED_MARKER}")]
    )

    await _run(client, gate, story, github)

    assert _findings(client) == []


async def test_replies_and_looms_own_automated_replies_are_skipped() -> None:
    client = FakeLithosClient(agent_id="a")
    story, gate = await _gate_with_story(client)
    github = _github(
        comments=[
            _comment(112, in_reply_to_id=111),
            _comment(
                113,
                author="operator",
                body="Fixed in abc123.\n\n_(automated reply by story-develop)_",
            ),
        ]
    )

    await _run(client, gate, story, github)

    assert _findings(client) == []
    # The marker still advances past the skipped ids so they are never re-read.
    marker = await _marker(client, gate.id)
    assert marker["last_comment_id"] == 113


# ── de-dup semantics ───────────────────────────────────────────────────


async def test_same_material_across_two_sweeps_posts_once() -> None:
    client = FakeLithosClient(agent_id="a")
    story, gate = await _gate_with_story(client)
    github = _github(reviews=[_review(500)], comments=[_comment(111)])

    await _run(client, gate, story, github)
    gate = await _refresh(client, gate)
    await _run(client, gate, story, github)

    assert len(_findings(client)) == 1


async def test_new_comment_on_already_reported_pr_posts_again() -> None:
    client = FakeLithosClient(agent_id="a")
    story, gate = await _gate_with_story(client)
    await _run(client, gate, story, _github(comments=[_comment(111)]))

    gate = await _refresh(client, gate)
    await _run(client, gate, story, _github(comments=[_comment(111), _comment(112)]))

    posted = _findings(client)
    assert len(posted) == 2
    assert "discussion_r112" in posted[1]


async def test_marker_for_a_different_pr_url_is_ignored() -> None:
    """Url scoping: a replacement PR re-evaluates from scratch (the recorded
    high-water marks belong to the dead PR's id space)."""
    client = FakeLithosClient(agent_id="a")
    story, gate = await _gate_with_story(client)
    await client.task_update(
        task_id=gate.id,
        metadata={
            REVIEW_SEEN_KEY: {
                "pr_url": "https://github.com/agent-lore/lithos-lens/pull/60",
                "last_review_id": 9999,
                "last_comment_id": 9999,
            }
        },
    )
    gate = await _refresh(client, gate)
    github = _github(reviews=[_review(500)])

    await _run(client, gate, story, github)

    assert len(_findings(client)) == 1
    marker = await _marker(client, gate.id)
    assert marker["pr_url"] == _PR_URL and marker["last_review_id"] == 500


async def test_comment_since_cursor_is_passed_from_the_marker() -> None:
    client = FakeLithosClient(agent_id="a")
    story, gate = await _gate_with_story(client)
    github = _github(comments=[_comment(111)])
    await _run(client, gate, story, github)

    gate = await _refresh(client, gate)
    github2 = _github()
    await _run(client, gate, story, github2)

    _, kwargs = github2.list_pull_request_review_comments.call_args
    assert kwargs["since"] is not None  # bounded pagination, not a full re-walk


# ── degraded paths ─────────────────────────────────────────────────────


async def test_github_error_is_swallowed_and_nothing_is_marked() -> None:
    client = FakeLithosClient(agent_id="a")
    story, gate = await _gate_with_story(client)
    github = _github()
    github.list_pull_request_reviews.side_effect = GitHubError("boom")

    await _run(client, gate, story, github)  # must not raise

    assert _findings(client) == []
    assert await _marker(client, gate.id) is None  # retried next sweep


async def test_orphan_gate_marks_without_posting() -> None:
    client = FakeLithosClient(agent_id="a")
    _story, gate = await _gate_with_story(client)
    github = _github(reviews=[_review(500)])

    await _run(client, gate, None, github)

    assert _findings(client) == []
    marker = await _marker(client, gate.id)
    assert marker["last_review_id"] == 500


async def test_nothing_new_writes_no_marker() -> None:
    """An idle PR must not generate a task_update every sweep."""
    client = FakeLithosClient(agent_id="a")
    story, gate = await _gate_with_story(client)
    github = _github(reviews=[_review(500)])
    await _run(client, gate, story, github)

    gate = await _refresh(client, gate)
    spy = AsyncMock(wraps=client.task_update)
    client.task_update = spy  # type: ignore[method-assign]
    await _run(client, gate, story, github)
    assert spy.await_count == 0
