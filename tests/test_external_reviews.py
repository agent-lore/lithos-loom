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
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

from lithos_loom.gates import create_pr_gate, parse_pr_gate
from lithos_loom.github_client import (
    GitHubError,
    IssueComment,
    PullRequestReview,
    PullRequestReviewComment,
)
from lithos_loom.github_models import (
    AUTOMATED_REPLY_MARKER,
    LOOM_NOTICE_MARKER,
    issue_comment_reply_body,
)
from lithos_loom.github_review_activity import ReviewStream
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
    issue_comments: list[IssueComment] | None = None,
) -> AsyncMock:
    github = AsyncMock()
    github.list_pull_request_reviews.return_value = reviews or []
    github.list_pull_request_review_comments.return_value = comments or []
    github.list_issue_comments.return_value = issue_comments or []
    # Default: reply authors are collaborators, so landed-fix replies count
    # as proof. Forgery tests override this per author.
    github.get_collaborator_permission.return_value = "write"
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
    pull_request_review_id: int | None = None,
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
        pull_request_review_id=pull_request_review_id,
    )


def _issue_comment(
    comment_id: int,
    *,
    author: str = "davesnowdon",
    body: str = "Verdict: not ready — two P1 gaps",
    updated_at: datetime | None = datetime(2026, 9, 5, 10, 25, 24, tzinfo=UTC),
) -> IssueComment:
    return IssueComment(
        comment_id=comment_id,
        author=author,
        body=body,
        html_url=f"https://github.com/{_REPO}/pull/62#issuecomment-{comment_id}",
        created_at=updated_at,
        updated_at=updated_at,
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


# ── PR #344 review round 1 ─────────────────────────────────────────────


async def test_comment_since_cursor_overlaps_one_second() -> None:
    """GitHub's `since` is strictly-after with second precision: a comment
    landing in the same second as the stored boundary would be excluded
    forever (the id mark can't save a row that is never returned). The sweep
    queries one second BEFORE the boundary; the id high-water removes the
    repeats that overlap re-fetches."""
    client = FakeLithosClient(agent_id="a")
    story, gate = await _gate_with_story(client)
    stamp = datetime(2026, 8, 30, 12, 0, 7, tzinfo=UTC)
    github = _github(comments=[_comment(111)])
    github.list_pull_request_review_comments.return_value = [
        PullRequestReviewComment(
            comment_id=111,
            author="reviewer-human",
            path="src/x.py",
            line=12,
            body="b",
            in_reply_to_id=None,
            updated_at=stamp,
        )
    ]
    await _run(client, gate, story, github)

    gate = await _refresh(client, gate)
    github2 = _github()
    await _run(client, gate, story, github2)

    _, kwargs = github2.list_pull_request_review_comments.call_args
    assert kwargs["since"] == stamp - timedelta(seconds=1)


async def test_first_sweep_skips_roots_already_handled_by_the_inline_round() -> None:
    """Backfill guard: until the inline Copilot round is retired, delivery
    remediates root comments and replies with the AUTOMATED_MARKER before the
    gate exists. A markerless gate's first sweep must not re-report those
    handled roots — nor the handled author's summary review — while still
    posting anything the round did NOT get to (the settle-starvation case)."""
    client = FakeLithosClient(agent_id="a")
    story, gate = await _gate_with_story(client)
    github = _github(
        reviews=[
            _review(
                500,
                author="copilot-pull-request-reviewer[bot]",
                state="COMMENTED",
                body="generated 2 comments",
            )
        ],
        comments=[
            _comment(
                111,
                author="copilot-pull-request-reviewer[bot]",
                body="handled",
                pull_request_review_id=500,
            ),
            _comment(
                112,
                author="operator",
                body="Fixed in abc123.\n\n_(automated reply by story-develop)_",
                in_reply_to_id=111,
            ),
            _comment(
                113, author="copilot-pull-request-reviewer[bot]", body="starved out"
            ),
        ],
    )

    await _run(client, gate, story, github)

    posted = _findings(client)
    assert len(posted) == 1
    assert "starved out" in posted[0]  # the unhandled root IS reported
    assert "handled" not in posted[0]  # the remediated root is not
    assert "generated 2 comments" not in posted[0]  # nor the summary review
    marker = await _marker(client, gate.id)
    assert marker["last_comment_id"] == 113 and marker["last_review_id"] == 500


async def test_changes_requested_review_survives_handled_comment_suppression() -> None:
    """The handled-author suppression must stay narrow: a CHANGES_REQUESTED
    review still posts even when some of its author's roots carry automated
    replies — the requested changes are not proven addressed by a reply."""
    client = FakeLithosClient(agent_id="a")
    story, gate = await _gate_with_story(client)
    github = _github(
        reviews=[_review(500, author="reviewer-human", state="CHANGES_REQUESTED")],
        comments=[
            _comment(111, author="reviewer-human", body="handled"),
            _comment(
                112,
                author="operator",
                body="Fixed in abc123.\n\n_(automated reply by story-develop)_",
                in_reply_to_id=111,
            ),
        ],
    )

    await _run(client, gate, story, github)

    posted = _findings(client)
    assert len(posted) == 1
    assert "CHANGES_REQUESTED" in posted[0]


# ── PR #344 re-review: a marker reply is only proof when the fix LANDED ─


def _real_reply(
    *, fixed: bool, sha: str | None, held_back_verdict: str | None = None
) -> str:
    """Build the reply through the real pr_delivery.reply_body so the
    suppression prefix stays in lockstep with the producer."""
    from lithos_loom.plugins.story_develop.pr_delivery import reply_body

    return reply_body(
        fixed=fixed,
        sha=sha,
        coder_response="details",
        held_back_verdict=held_back_verdict,
    )


async def test_held_back_red_reply_does_not_suppress_its_root() -> None:
    """PR #344 re-review: the inline round replies with the AUTOMATED_MARKER
    even when the prepared fix was NOT pushed (red regression gate). That
    root is still unresolved — suppressing it would advance the id mark and
    lose it forever."""
    client = FakeLithosClient(agent_id="a")
    story, gate = await _gate_with_story(client)
    github = _github(
        comments=[
            _comment(111, body="real defect"),
            _comment(
                112,
                author="operator",
                body=_real_reply(fixed=True, sha=None, held_back_verdict="RED"),
                in_reply_to_id=111,
            ),
        ]
    )

    await _run(client, gate, story, github)

    posted = _findings(client)
    assert len(posted) == 1 and "real defect" in posted[0]


async def test_not_changed_reply_does_not_suppress_its_root() -> None:
    """A coder pushback ("Not changed — ...") carries the marker but fixed
    nothing; the operator still gets to see the root."""
    client = FakeLithosClient(agent_id="a")
    story, gate = await _gate_with_story(client)
    github = _github(
        comments=[
            _comment(111, body="real defect"),
            _comment(
                112,
                author="operator",
                body=_real_reply(fixed=False, sha=None),
                in_reply_to_id=111,
            ),
        ]
    )

    await _run(client, gate, story, github)

    posted = _findings(client)
    assert len(posted) == 1 and "real defect" in posted[0]


async def test_fixed_in_sha_reply_built_by_the_real_producer_suppresses() -> None:
    """Lockstep for the landed-fix shape: a reply built by reply_body with a
    sha ("Fixed in <sha10> — ...") is the one and only proof of remediation."""
    client = FakeLithosClient(agent_id="a")
    story, gate = await _gate_with_story(client)
    github = _github(
        comments=[
            _comment(111, body="handled defect"),
            _comment(
                112,
                author="operator",
                body=_real_reply(fixed=True, sha="abc123def4567890"),
                in_reply_to_id=111,
            ),
        ]
    )

    await _run(client, gate, story, github)

    assert _findings(client) == []
    marker = await _marker(client, gate.id)
    assert marker["last_comment_id"] == 112


# ── PR #344 re-review 2: the landed-fix reply must be AUTHENTICATED ─────


async def test_forged_fixed_reply_by_an_outsider_does_not_suppress() -> None:
    """PR #344 re-review 2: both suppression tokens are public body strings —
    any commenter can copy them. A "Fixed in <sha>" + marker reply from an
    author WITHOUT write/admin on the repo must not suppress its root; the
    root still posts and is not lost behind the advanced id mark."""
    client = FakeLithosClient(agent_id="a")
    story, gate = await _gate_with_story(client)
    github = _github(
        comments=[
            _comment(111, author="trusted-reviewer", body="real defect"),
            _comment(
                112,
                author="outside-user",
                body=_real_reply(fixed=True, sha="deadbeefca11ab1e"),
                in_reply_to_id=111,
            ),
        ]
    )
    github.get_collaborator_permission.return_value = "read"

    await _run(client, gate, story, github)

    posted = _findings(client)
    assert len(posted) == 1 and "real defect" in posted[0]
    github.get_collaborator_permission.assert_awaited_once_with(_REPO, "outside-user")


async def test_permission_check_failure_reports_rather_than_hides() -> None:
    """Fail closed on suppression: if the permission probe errors, the reply
    proves nothing and the root posts — a duplicate report is recoverable, a
    hidden root is not."""
    client = FakeLithosClient(agent_id="a")
    story, gate = await _gate_with_story(client)
    github = _github(
        comments=[
            _comment(111, body="real defect"),
            _comment(
                112,
                author="operator",
                body=_real_reply(fixed=True, sha="abc123def4567890"),
                in_reply_to_id=111,
            ),
        ]
    )
    github.get_collaborator_permission.side_effect = GitHubError("boom")

    await _run(client, gate, story, github)

    posted = _findings(client)
    assert len(posted) == 1 and "real defect" in posted[0]


async def test_no_candidate_replies_means_no_permission_calls() -> None:
    """The permission probe is per unseen candidate author only — an ordinary
    batch with no landed-fix replies must not spend API calls on it."""
    client = FakeLithosClient(agent_id="a")
    story, gate = await _gate_with_story(client)
    github = _github(reviews=[_review(500)], comments=[_comment(111)])

    await _run(client, gate, story, github)

    github.get_collaborator_permission.assert_not_called()


async def test_permission_is_checked_once_per_author() -> None:
    client = FakeLithosClient(agent_id="a")
    story, gate = await _gate_with_story(client)
    github = _github(
        comments=[
            _comment(111, body="one"),
            _comment(
                112,
                author="operator",
                body=_real_reply(fixed=True, sha="abc123def4567890"),
                in_reply_to_id=111,
            ),
            _comment(113, body="two"),
            _comment(
                114,
                author="operator",
                body=_real_reply(fixed=True, sha="cafebabe12345678"),
                in_reply_to_id=113,
            ),
        ]
    )

    await _run(client, gate, story, github)

    assert _findings(client) == []  # both roots proven handled
    assert github.get_collaborator_permission.await_count == 1


async def test_later_summary_review_not_hidden_by_old_handled_roots() -> None:
    """PR #345 review F3 (sweep parity): suppression binds a handled root to
    its OWNING review — a bot's later COMMENTED summary (no inline comments
    yet) still posts even though an older root of the same bot was fixed and
    replied to by the inline round."""
    client = FakeLithosClient(agent_id="a")
    story, gate = await _gate_with_story(client)
    github = _github(
        reviews=[
            _review(
                500,
                author="copilot-pull-request-reviewer[bot]",
                state="COMMENTED",
                body="generated 1 comment",
            ),
            _review(
                510,
                author="copilot-pull-request-reviewer[bot]",
                state="COMMENTED",
                body="two new problems",
            ),
        ],
        comments=[
            _comment(
                111,
                author="copilot-pull-request-reviewer[bot]",
                body="old handled",
                pull_request_review_id=500,
            ),
            _comment(
                112,
                author="operator",
                body=_real_reply(fixed=True, sha="abc123def4567890"),
                in_reply_to_id=111,
            ),
        ],
    )

    await _run(client, gate, story, github)

    posted = _findings(client)
    assert len(posted) == 1
    assert "two new problems" in posted[0]
    assert "generated 1 comment" not in posted[0]


# ── slice C seams: the returned batch + the exhaustion note ────────────


async def test_ingest_returns_the_actionable_batch() -> None:
    """Remediation (slice C) dispatches off what ingestion just posted — the
    result carries the actionable material so the dispatch decision (trust,
    own-sha) never re-fetches or re-filters."""
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    github = _github(reviews=[_review(500)], comments=[_comment(1)])
    spec = parse_pr_gate(gate)
    assert spec is not None
    result = await ingest_external_reviews(gate, spec, story, github, _ctx(client))
    assert result.posted
    assert [(a.stream, a.activity_id) for a in result.actionable] == [
        (ReviewStream.REVIEW, 500),
        (ReviewStream.INLINE, 1),
    ]


async def test_ingest_returns_empty_batch_when_nothing_posts() -> None:
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    github = _github(reviews=[_review(500, state="APPROVED", body="")])
    spec = parse_pr_gate(gate)
    assert spec is not None
    result = await ingest_external_reviews(gate, spec, story, github, _ctx(client))
    assert not result.posted
    assert result.actionable == []


async def test_extra_note_lands_in_the_finding_body() -> None:
    """S5b: budget exhaustion is stated in the [ExternalReview] body itself —
    going over budget must not blind the operator to new activity."""
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    github = _github(reviews=[_review(500)])
    spec = parse_pr_gate(gate)
    assert spec is not None
    await ingest_external_reviews(
        gate,
        spec,
        story,
        github,
        _ctx(client),
        extra_note="remediation budget exhausted — findings will be reported "
        "but not auto-fixed until a human pushes or merges",
    )
    (finding,) = _findings(client)
    assert "remediation budget exhausted" in finding


async def test_failed_finding_post_reports_not_posted() -> None:
    """PR #346 review F4: `posted` must be the truth — a batch whose finding
    (or de-dup marker) never landed must not claim to have posted, or
    remediation dispatches without the promised operator breadcrumb."""
    from lithos_loom.errors import LithosClientError

    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)

    async def failing_post(**kwargs: Any) -> Any:
        raise LithosClientError("server_error", "boom")

    client.finding_post = failing_post  # type: ignore[method-assign]
    github = _github(reviews=[_review(500)])
    spec = parse_pr_gate(gate)
    assert spec is not None

    result = await ingest_external_reviews(gate, spec, story, github, _ctx(client))

    assert not result.posted


async def test_failed_marker_write_reports_not_posted() -> None:
    from lithos_loom.errors import LithosClientError

    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    original = client.task_update

    async def failing_update(**kwargs: Any) -> Any:
        if REVIEW_SEEN_KEY in (kwargs.get("metadata") or {}):
            raise LithosClientError("server_error", "boom")
        return await original(**kwargs)

    client.task_update = failing_update  # type: ignore[method-assign]
    github = _github(reviews=[_review(500)])
    spec = parse_pr_gate(gate)
    assert spec is not None

    result = await ingest_external_reviews(gate, spec, story, github, _ctx(client))

    # The finding itself posted (breadcrumb exists) but the de-dup mark did
    # not land — the batch will re-post next sweep, so it must not dispatch.
    assert not result.posted


async def test_pending_marker_rides_atomically_with_the_seen_marks() -> None:
    """PR #346 re-review 1: the remediation pending trigger must land in the
    SAME task_update as the seen high-water marks — deferral may never be
    acknowledged before the trigger is durable. On a failed write, neither
    lands and the whole batch (finding already posted or not) retries."""
    from lithos_loom.errors import LithosClientError

    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    github = _github(reviews=[_review(500)])
    spec = parse_pr_gate(gate)
    assert spec is not None
    seen_batches: list[list[Any]] = []

    async def provider(activities: Any) -> dict[str, Any]:
        # The dispatcher's provider sees the actionable batch (it evaluates
        # trust + own-sha BEFORE the durable write — re-review 3).
        seen_batches.append([(a.stream, a.activity_id) for a in activities])
        return {"external_remediation_pending": {"pr_url": spec.pr_url}}

    result = await ingest_external_reviews(
        gate, spec, story, github, _ctx(client), pending_marker_for=provider
    )

    assert result.posted
    assert seen_batches == [[(ReviewStream.REVIEW, 500)]]
    refreshed = await client.task_get(task_id=gate.id)
    assert refreshed is not None
    assert REVIEW_SEEN_KEY in refreshed.metadata
    assert refreshed.metadata["external_remediation_pending"] == {"pr_url": spec.pr_url}

    # Failure injection: the one combined write fails → NEITHER key lands and
    # posted is False (the batch retries next sweep, trigger included).
    client2 = FakeLithosClient()
    story2, gate2 = await _gate_with_story(client2)
    original = client2.task_update

    async def failing_update(**kwargs: Any) -> Any:
        if REVIEW_SEEN_KEY in (kwargs.get("metadata") or {}):
            raise LithosClientError("server_error", "boom")
        return await original(**kwargs)

    client2.task_update = failing_update  # type: ignore[method-assign]
    spec2 = parse_pr_gate(gate2)
    assert spec2 is not None

    async def provider2(activities: Any) -> dict[str, Any]:
        return {"external_remediation_pending": {"pr_url": spec2.pr_url}}

    result = await ingest_external_reviews(
        gate2,
        spec2,
        story2,
        _github(reviews=[_review(500)]),
        _ctx(client2),
        pending_marker_for=provider2,
    )

    assert not result.posted
    refreshed2 = await client2.task_get(task_id=gate2.id)
    assert refreshed2 is not None
    assert REVIEW_SEEN_KEY not in refreshed2.metadata
    assert "external_remediation_pending" not in refreshed2.metadata


async def test_silent_material_marker_failure_reports_failed() -> None:
    """PR #348 re-review round 3: for APPROVED/DISMISSED/reply-only activity
    the marker IS the durable record — a failed write must surface as
    ``failed`` so the merged-path caller defers resolution instead of losing
    the observation forever."""
    from lithos_loom.errors import LithosClientError

    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    original = client.task_update

    async def failing_update(**kwargs: Any) -> Any:
        if REVIEW_SEEN_KEY in (kwargs.get("metadata") or {}):
            raise LithosClientError("server_error", "boom")
        return await original(**kwargs)

    client.task_update = failing_update  # type: ignore[method-assign]
    github = _github(reviews=[_review(500, state="APPROVED", body="")])
    spec = parse_pr_gate(gate)
    assert spec is not None

    result = await ingest_external_reviews(gate, spec, story, github, _ctx(client))

    assert result.failed is True
    assert not result.posted


async def test_orphan_gate_marker_failure_reports_failed() -> None:
    from lithos_loom.errors import LithosClientError

    client = FakeLithosClient()
    _story, gate = await _gate_with_story(client)

    async def failing_update(**kwargs: Any) -> Any:
        raise LithosClientError("server_error", "boom")

    client.task_update = failing_update  # type: ignore[method-assign]
    github = _github(reviews=[_review(500)])
    spec = parse_pr_gate(gate)
    assert spec is not None

    result = await ingest_external_reviews(gate, spec, None, github, _ctx(client))

    assert result.failed is True


# ── conversation comments (#353) ──────────────────────────────────────
#
# A PR's author cannot leave a review on it, so the operator's only channel
# on a loom-delivered PR is the Conversation tab — an ISSUE comment, a third
# stream with its own id space and high-water mark.


async def test_conversation_comment_posts_a_finding_and_marks_its_own_stream() -> None:
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    github = _github(issue_comments=[_issue_comment(5551158842)])

    await _run(client, gate, story, github)

    findings = _findings(client)
    assert len(findings) == 1
    body = findings[0]
    assert body.startswith(EXTERNAL_REVIEW)
    assert "comment by davesnowdon on the PR conversation: Verdict: not ready" in body
    assert f"{_PR_URL}#issuecomment-5551158842" in body
    seen = (await _refresh(client, gate)).metadata[REVIEW_SEEN_KEY]
    assert seen["last_issue_comment_id"] == 5551158842
    assert seen["last_issue_comment_at"] == "2026-09-05T10:25:24+00:00"
    assert seen["last_review_id"] == 0 and seen["last_comment_id"] == 0

    # Second sweep: the same comment is below the mark → nothing new.
    await _run(client, await _refresh(client, gate), story, github)
    assert len(_findings(client)) == 1


async def test_conversation_fetch_is_bounded_by_its_own_cursor() -> None:
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    await client.task_update(
        task_id=gate.id,
        metadata={
            REVIEW_SEEN_KEY: {
                "pr_url": _PR_URL,
                "last_review_id": 0,
                "last_comment_id": 0,
                "last_issue_comment_id": 5,
                "last_issue_comment_at": "2026-09-05T10:25:24+00:00",
            }
        },
    )
    github = _github()

    await _run(client, await _refresh(client, gate), story, github)

    # One-second overlap, same as the inline stream (PR #344 review, finding 1).
    github.list_issue_comments.assert_awaited_once_with(
        _REPO, 62, since=datetime(2026, 9, 5, 10, 25, 23, tzinfo=UTC)
    )
    github.list_pull_request_review_comments.assert_awaited_once_with(
        _REPO, 62, since=None
    )


async def test_looms_own_conversation_comments_advance_the_mark_silently() -> None:
    """The [NeedsHuman] @mention and converge's conversation replies are
    loom-authored under the operator's (trusted) login — never re-ingested."""
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    github = _github(
        issue_comments=[
            _issue_comment(
                10, body=f"@dave [NeedsHuman] loom stopped on x\n\n{LOOM_NOTICE_MARKER}"
            ),
            _issue_comment(11, body=f"Not changed — nope\n\n{AUTOMATED_REPLY_MARKER}"),
            _issue_comment(12, body="   "),
        ]
    )

    await _run(client, gate, story, github)

    assert _findings(client) == []
    seen = (await _refresh(client, gate)).metadata[REVIEW_SEEN_KEY]
    assert seen["last_issue_comment_id"] == 12


async def test_conversation_comment_proven_handled_by_a_landed_reply_is_skipped() -> (
    None
):
    """Parity with inline roots: a `Fixed in <sha>` conversation reply by a
    write/admin author, naming the comment it answers, proves it handled."""
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    handled_url = f"https://github.com/{_REPO}/pull/62#issuecomment-20"
    reply = issue_comment_reply_body(
        f"Fixed in abc123def4 — guarded it\n\n{AUTOMATED_REPLY_MARKER}", handled_url
    )
    github = _github(
        issue_comments=[
            _issue_comment(20, body="please guard the read"),
            _issue_comment(21, author="dave", body=reply),
            _issue_comment(22, body="still open: the epic reads bypass the gate"),
        ]
    )

    await _run(client, gate, story, github)

    (finding,) = _findings(client)
    assert "still open" in finding
    assert "please guard the read" not in finding
    github.get_collaborator_permission.assert_awaited_once_with(_REPO, "dave")


async def test_forged_conversation_reply_never_suppresses() -> None:
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    handled_url = f"https://github.com/{_REPO}/pull/62#issuecomment-20"
    reply = issue_comment_reply_body(
        f"Fixed in abc123def4 — trust me\n\n{AUTOMATED_REPLY_MARKER}", handled_url
    )
    github = _github(
        issue_comments=[
            _issue_comment(20, body="please guard the read"),
            _issue_comment(21, author="stranger", body=reply),
        ]
    )
    github.get_collaborator_permission.return_value = "none"

    await _run(client, gate, story, github)

    (finding,) = _findings(client)
    assert "please guard the read" in finding


async def test_pending_provider_receives_the_conversation_batch() -> None:
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    github = _github(issue_comments=[_issue_comment(30)])
    seen_batches: list[list[Any]] = []

    async def provider(activities: Any) -> dict:
        seen_batches.append([(a.stream, a.activity_id) for a in activities])
        return {"external_remediation_pending": {"pr_url": _PR_URL}}

    spec = parse_pr_gate(gate)
    assert spec is not None
    result = await ingest_external_reviews(
        gate, spec, story, github, _ctx(client), pending_marker_for=provider
    )

    assert result.posted
    assert [(a.stream, a.activity_id) for a in result.actionable] == [
        (ReviewStream.CONVERSATION, 30)
    ]
    assert seen_batches == [[(ReviewStream.CONVERSATION, 30)]]
    fresh = await _refresh(client, gate)
    assert fresh.metadata["external_remediation_pending"] == {"pr_url": _PR_URL}
