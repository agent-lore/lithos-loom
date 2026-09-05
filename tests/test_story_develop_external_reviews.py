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
    IssueComment,
    PullRequestReview,
    PullRequestReviewComment,
)
from lithos_loom.github_models import AUTOMATED_REPLY_MARKER, issue_comment_reply_body
from lithos_loom.plugins.story_develop import external_reviews as ext_mod
from lithos_loom.plugins.story_develop.external_reviews import (
    CoderAck,
    ExternalFinding,
    ack_instruction,
    external_intake_reviews,
    fetch_external_findings,
    findings_to_handoff_text,
    outcomes_after_loop,
    parse_coder_acks,
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
        commit_id=commit_id,
        pull_request_review_id=pull_request_review_id,
    )


def _install_github(
    monkeypatch: pytest.MonkeyPatch,
    *,
    reviews: list[PullRequestReview] | None = None,
    comments: list[PullRequestReviewComment] | None = None,
    permissions: dict[str, Any] | None = None,
    issue_comments: list[IssueComment] | None = None,
) -> AsyncMock:
    """Route the module's ``github_call`` bridge onto a fake async client."""
    client = AsyncMock()
    client.list_pull_request_reviews.return_value = reviews or []
    client.list_pull_request_review_comments.return_value = comments or []
    client.list_issue_comments.return_value = issue_comments or []
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
            _comment(1, author=_BOT, body="handled", pull_request_review_id=500),
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


def test_later_summary_review_is_not_hidden_by_old_handled_roots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PR #345 review F3: suppression must bind a handled root to its OWNING
    review, never to the author across the whole PR — a bot's new COMMENTED
    summary (a later re-review, no inline comments yet) must still ingest even
    though an older root of the same bot was fixed and replied to."""
    handled_reply = reply_body(
        fixed=True, sha="abc123def4567890", coder_response="done"
    )
    _install_github(
        monkeypatch,
        reviews=[
            _review(500, author=_BOT, state="COMMENTED", body="generated 1 comment"),
            _review(510, author=_BOT, state="COMMENTED", body="two new problems"),
        ],
        comments=[
            _comment(1, author=_BOT, body="old handled", pull_request_review_id=500),
            _comment(2, author="operator", body=handled_reply, in_reply_to_id=1),
        ],
        permissions={"operator": "admin"},
    )

    trusted, _untrusted = fetch_external_findings(_REPO, 62, trusted_bots=(_BOT,))

    # Review 500 (all of its roots handled) is suppressed; review 510 is new
    # material and survives.
    assert [(f.review_id, f.body) for f in trusted] == [(510, "two new problems")]


# --- per-id coder acknowledgements (PR #345 re-review 1) ---------------------


_ACK_HANDOFF = (
    "## Status: LGTM\n"
    "## Summary\n"
    "- f-001: fixed the guard properly.\n"  # summary prose must NOT count
    "## External findings\n"
    "- f-001: FIXED — added the None guard in src/x.py\n"
    "- f-002: DISPUTED — deliberate: the handle closes in __exit__\n"
    "- f-099: FIXED — invented id\n"
)


def test_parse_coder_acks_reads_only_the_ack_section() -> None:
    acks = parse_coder_acks(_ACK_HANDOFF, ["f-001", "f-002", "f-003"])
    assert acks["f-001"].verdict == "fixed"
    assert "None guard" in acks["f-001"].detail
    assert acks["f-002"].verdict == "disputed"
    assert "f-003" not in acks  # omitted id: no ack, never invented
    assert "f-099" not in acks  # invented id: ignored


def test_prose_outside_the_ack_section_is_never_an_ack() -> None:
    # The Summary is REQUIRED to address each finding by id, so a bare
    # "- f-001: fixed ..." line exists in every conforming handoff; only the
    # dedicated section is authoritative.
    text = "## Status: LGTM\n## Summary\n- f-001: FIXED — did the thing\n"
    assert parse_coder_acks(text, ["f-001"]) == {}


def test_outcomes_approval_alone_is_never_fixed() -> None:
    # The reviewer's direct probe (PR #345 re-review 1): two ids, no per-id
    # claims, loop approved. Approval is evidence the TREE passed the loop,
    # not evidence of each external disposition — a silent partial fix must
    # not earn a per-thread "Fixed in" claim.
    id_map = {"f-001": _finding(comment_id=1), "f-002": _finding(comment_id=2)}
    out = outcomes_after_loop(id_map, {}, {}, {}, loop_approved=True)
    assert [o.disposition for o in out] == ["unaddressed", "unaddressed"]


def test_outcomes_fixed_needs_ack_and_approval() -> None:
    id_map = {"f-001": _finding()}
    acks = {"f-001": CoderAck(verdict="fixed", detail="guarded it")}
    (approved,) = outcomes_after_loop(id_map, {}, {}, acks, loop_approved=True)
    assert approved.disposition == "fixed"
    assert approved.detail == "guarded it"
    # The other half: an acknowledged fix in an UNAPPROVED loop is a claim the
    # panel + gate never validated — stays unaddressed.
    (unapproved,) = outcomes_after_loop(id_map, {}, {}, acks, loop_approved=False)
    assert unapproved.disposition == "unaddressed"


def test_outcomes_ack_dispute_counts_without_findings_block() -> None:
    id_map = {"f-001": _finding()}
    acks = {"f-001": CoderAck(verdict="disputed", detail="deliberate design")}
    (o,) = outcomes_after_loop(id_map, {}, {}, acks, loop_approved=True)
    assert o.disposition == "disputed"
    assert o.detail == "deliberate design"


def test_ack_instruction_names_every_id_and_the_section() -> None:
    text = ack_instruction(["f-001", "f-002"])
    assert "## External findings" in text
    assert "f-001" in text and "f-002" in text
    assert "omit" in text.lower()  # the never-omit-silently steering


# ── conversation comments (#353) ──────────────────────────────────────


def _issue_comment(
    comment_id: int, *, author: str = "davesnowdon", body: str = "Verdict: two P1 gaps"
) -> IssueComment:
    return IssueComment(
        comment_id=comment_id,
        author=author,
        body=body,
        html_url=f"https://github.com/{_REPO}/pull/62#issuecomment-{comment_id}",
    )


def test_fetch_turns_conversation_comments_into_findings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_github(
        monkeypatch,
        issue_comments=[
            _issue_comment(5551158842),
            _issue_comment(5551158900, author="stranger", body="drive-by"),
            _issue_comment(
                5551158901, body=f"Not changed — x\n\n{AUTOMATED_REPLY_MARKER}"
            ),
        ],
        permissions={"davesnowdon": "admin"},
    )

    trusted, untrusted = fetch_external_findings(_REPO, 62, trusted_bots=(_BOT,))

    (finding,) = trusted
    assert finding == ExternalFinding(
        author="davesnowdon",
        source="human",
        trusted=True,
        review_id=None,
        comment_id=None,
        thread_url=f"https://github.com/{_REPO}/pull/62#issuecomment-5551158842",
        head_sha="",
        body="Verdict: two P1 gaps",
        issue_comment_id=5551158842,
    )
    assert [f.author for f in untrusted] == ["stranger"]
    # No sha → no re-anchor note (the comment reviews the PR, not a commit).
    text = findings_to_handoff_text(trusted, current_head_sha=_HEAD)
    assert "written against" not in text
    assert "[davesnowdon] Verdict: two P1 gaps" in text


def test_fetch_skips_conversation_comments_proven_handled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handled_url = f"https://github.com/{_REPO}/pull/62#issuecomment-20"
    landed = issue_comment_reply_body(
        reply_body(fixed=True, sha="abc123def4567890", coder_response="done"),
        handled_url,
    )
    forged_url = f"https://github.com/{_REPO}/pull/62#issuecomment-22"
    forged = issue_comment_reply_body(
        reply_body(fixed=True, sha="abc123def4567890", coder_response="done"),
        forged_url,
    )
    _install_github(
        monkeypatch,
        issue_comments=[
            _issue_comment(20, body="handled"),
            _issue_comment(21, author="dave", body=landed),
            _issue_comment(22, body="still live"),
            _issue_comment(23, author="stranger", body=forged),
        ],
        permissions={"davesnowdon": "admin", "dave": "write"},
    )

    trusted, untrusted = fetch_external_findings(_REPO, 62, trusted_bots=())

    assert [f.issue_comment_id for f in trusted] == [22]
    assert untrusted == []


def test_every_adapters_finding_id_field_is_a_real_finding_field() -> None:
    """The reply epilogue answers on the id the adapter projects the row onto
    (PR #356 review, finding 1): a registry row naming a field ExternalFinding
    does not have would be a silent loss of reply identity."""
    from dataclasses import fields

    from lithos_loom.github_review_activity import ExternalReviewActivity, ReviewStream
    from lithos_loom.github_review_streams import STREAM_ADAPTERS

    names = {f.name for f in fields(ExternalFinding)}
    assert {a.finding_id_field for a in STREAM_ADAPTERS} <= names
    assert len({a.finding_id_field for a in STREAM_ADAPTERS}) == len(STREAM_ADAPTERS)
    for adapter in STREAM_ADAPTERS:
        row = ExternalReviewActivity(
            stream=adapter.stream, activity_id=99, author="x", body="b", url="u"
        )
        finding = ext_mod.finding_from_activity(row, source="human", trusted=True)
        assert getattr(finding, adapter.finding_id_field) == 99
        others = {f for f in ("review_id", "comment_id", "issue_comment_id")} - {
            adapter.finding_id_field
        }
        assert all(getattr(finding, f) is None for f in others)
    assert list(ReviewStream)  # the enum drives the registry, not the reverse
