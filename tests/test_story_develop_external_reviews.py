"""Tests for ``plugins.story_develop.external_reviews`` (PRD S2, slice B).

The converge-side fetch + injection seam: pull a PR's external review
material, split it by the ADR 0011 trust line (allowlisted bots + write/admin
humans seed the coder; everyone else is reported only), skip roots already
proven handled by an authenticated landed-fix reply, and render the trusted
findings as a synthetic ``ReviewOutcome`` that seeds converge's fix loop via
``LoopEntry.intake_reviews``.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest

from lithos_loom.github_client import (
    GitHubError,
    PullRequestReview,
    PullRequestReviewComment,
)
from lithos_loom.plugins.story_develop import external_reviews as ext_mod
from lithos_loom.plugins.story_develop.external_reviews import (
    ExternalFinding,
    external_intake_reviews,
    fetch_external_findings,
    findings_to_handoff_text,
)
from lithos_loom.plugins.story_develop.handoff import parse_review_handoff
from lithos_loom.plugins.story_develop.pr_delivery import reply_body

_REPO = "agent-lore/lithos-lens"
_BOT = "copilot-pull-request-reviewer[bot]"
_HEAD = "e" * 40


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
        commit_id=_HEAD,
    )


def _comment(
    comment_id: int,
    *,
    author: str = "reviewer-human",
    body: str = "this leaks a handle",
    in_reply_to_id: int | None = None,
    path: str = "src/x.py",
    line: int | None = 12,
    commit_id: str = _HEAD,
) -> PullRequestReviewComment:
    return PullRequestReviewComment(
        comment_id=comment_id,
        author=author,
        path=path,
        line=line,
        body=body,
        in_reply_to_id=in_reply_to_id,
        html_url=f"https://github.com/{_REPO}/pull/62#discussion_r{comment_id}",
        commit_id=commit_id,
    )


def _install_github(
    monkeypatch: pytest.MonkeyPatch,
    *,
    reviews: list[PullRequestReview] | None = None,
    comments: list[PullRequestReviewComment] | None = None,
    permissions: dict[str, Any] | None = None,
) -> AsyncMock:
    """Route the module's ``github_call`` bridge onto a fake async client."""
    client = AsyncMock()
    client.list_pull_request_reviews.return_value = reviews or []
    client.list_pull_request_review_comments.return_value = comments or []
    perms = permissions or {}

    async def _perm(repo: str, username: str) -> str:
        value = perms.get(username, "none")
        if isinstance(value, Exception):
            raise value
        return value

    client.get_collaborator_permission.side_effect = _perm

    def fake_github_call(op):
        return asyncio.run(op(client))

    monkeypatch.setattr(ext_mod, "github_call", fake_github_call)
    return client


# ── fetch: trust split ─────────────────────────────────────────────────


def test_bot_and_write_human_are_trusted_others_are_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_github(
        monkeypatch,
        comments=[
            _comment(1, author=_BOT, body="bot finding"),
            _comment(2, author="dave", body="maintainer finding"),
            _comment(3, author="stranger", body="outside finding"),
        ],
        permissions={"dave": "admin", "stranger": "read"},
    )

    trusted, untrusted = fetch_external_findings(_REPO, 62, trusted_bots=(_BOT,))

    assert [(f.author, f.source) for f in trusted] == [
        (_BOT, "bot"),
        ("dave", "human"),
    ]
    assert [f.author for f in untrusted] == ["stranger"]
    assert all(f.trusted for f in trusted)
    assert not any(f.trusted for f in untrusted)


def test_permission_probe_error_lands_the_author_in_untrusted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail closed for the PROMPT path: an unverifiable author is reported,
    never fed to an agent."""
    _install_github(
        monkeypatch,
        comments=[_comment(1, author="dave", body="finding")],
        permissions={"dave": GitHubError("boom")},
    )

    trusted, untrusted = fetch_external_findings(_REPO, 62, trusted_bots=())

    assert trusted == [] and [f.author for f in untrusted] == ["dave"]


def test_fetch_raises_on_github_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unlike the old fetch_copilot_comments, a listing failure RAISES — the
    caller must distinguish 'no findings' from 'could not look'."""
    client = _install_github(monkeypatch)
    client.list_pull_request_reviews.side_effect = GitHubError("boom")

    with pytest.raises(GitHubError):
        fetch_external_findings(_REPO, 62, trusted_bots=())


# ── fetch: filtering ───────────────────────────────────────────────────


def test_replies_automated_replies_and_handled_roots_are_excluded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handled_reply = reply_body(
        fixed=True, sha="abc123def4567890", coder_response="done"
    )
    _install_github(
        monkeypatch,
        comments=[
            _comment(1, author=_BOT, body="already handled"),
            _comment(2, author="dave", body=handled_reply, in_reply_to_id=1),
            _comment(3, author=_BOT, body="still live"),
            _comment(4, author="dave", body="plain reply", in_reply_to_id=3),
        ],
        permissions={"dave": "write"},
    )

    trusted, untrusted = fetch_external_findings(_REPO, 62, trusted_bots=(_BOT,))

    assert [f.body for f in trusted] == ["still live"]
    assert untrusted == []


def test_forged_landed_fix_reply_does_not_suppress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same authentication rule as the sweep (PR #344 re-review 2): a
    landed-fix reply from a non-collaborator proves nothing."""
    forged = reply_body(fixed=True, sha="deadbeefca11ab1e", coder_response="x")
    _install_github(
        monkeypatch,
        comments=[
            _comment(1, author=_BOT, body="real defect"),
            _comment(2, author="stranger", body=forged, in_reply_to_id=1),
        ],
        permissions={"stranger": "read"},
    )

    trusted, _untrusted = fetch_external_findings(_REPO, 62, trusted_bots=(_BOT,))

    assert [f.body for f in trusted] == ["real defect"]


def test_review_policy_and_handled_author_suppression(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handled_reply = reply_body(
        fixed=True, sha="abc123def4567890", coder_response="done"
    )
    _install_github(
        monkeypatch,
        reviews=[
            _review(500, author=_BOT, state="COMMENTED", body="generated 1 comment"),
            _review(501, author="dave", state="APPROVED"),
            _review(502, author="dave", state="CHANGES_REQUESTED", body="blockers"),
        ],
        comments=[
            _comment(1, author=_BOT, body="handled"),
            _comment(2, author="operator", body=handled_reply, in_reply_to_id=1),
        ],
        permissions={"dave": "write", "operator": "admin"},
    )

    trusted, _untrusted = fetch_external_findings(_REPO, 62, trusted_bots=(_BOT,))

    # The bot's COMMENTED summary is suppressed (its roots were handled); the
    # APPROVED review is silent; the human CHANGES_REQUESTED survives.
    assert [(f.author, f.review_id) for f in trusted] == [("dave", 502)]
    assert trusted[0].comment_id is None
    assert "pullrequestreview-502" in trusted[0].thread_url


# ── handoff rendering + injection ──────────────────────────────────────


def _finding(
    *,
    author: str = "dave",
    body: str = "leaks a handle",
    path: str = "src/x.py",
    line: int | None = 12,
    comment_id: int | None = 7,
    head_sha: str = _HEAD,
) -> ExternalFinding:
    return ExternalFinding(
        author=author,
        source="human",
        trusted=True,
        review_id=None,
        comment_id=comment_id,
        thread_url="https://example/thread",
        head_sha=head_sha,
        path=path,
        line=line,
        body=body,
    )


def test_handoff_text_parses_and_attributes_the_author() -> None:
    text = findings_to_handoff_text(
        [_finding(), _finding(body="second", path="", line=None, comment_id=None)],
        current_head_sha=_HEAD,
    )
    parsed = parse_review_handoff(text)
    assert parsed.status == "FINDINGS"
    assert len(parsed.findings) == 2
    assert parsed.findings[0].files == ["src/x.py:12"]
    assert "[dave]" in parsed.findings[0].rationale
    assert parsed.findings[1].files == []


def test_stale_head_sha_gets_a_reanchor_note() -> None:
    """A finding written against an older sha may already be fixed — the coder
    is told to verify before changing anything, never to re-fix blindly."""
    text = findings_to_handoff_text(
        [_finding(head_sha="a" * 40)], current_head_sha=_HEAD
    )
    parsed = parse_review_handoff(text)
    assert "older" in parsed.findings[0].rationale
    assert ("a" * 12) in parsed.findings[0].rationale

    fresh = findings_to_handoff_text([_finding()], current_head_sha=_HEAD)
    assert "older" not in parse_review_handoff(fresh).findings[0].rationale


def test_external_intake_reviews_builds_outcome_and_id_map() -> None:
    findings = [_finding(), _finding(body="second", comment_id=8)]
    outcomes, id_map = external_intake_reviews(findings, current_head_sha=_HEAD)

    (outcome,) = outcomes
    assert outcome.reviewer == "external"
    assert outcome.status == "FINDINGS" and outcome.passed is False
    assert [f.finding_id for f in outcome.findings] == ["f-001", "f-002"]
    assert id_map["f-001"].comment_id == 7
    assert id_map["f-002"].comment_id == 8
    assert outcome.cost_usd == 0.0
