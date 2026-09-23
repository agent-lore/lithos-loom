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
from pathlib import Path
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
from lithos_loom.github_review_activity import ExternalReviewActivity, ReviewStream
from lithos_loom.github_review_streams import ReplyMode
from lithos_loom.plugins.story_develop import external_reviews as ext_mod
from lithos_loom.plugins.story_develop import external_triage as triage_mod
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
            _review(501, author="dave", state="APPROVED", body="LGTM"),
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
    # human's APPROVED and CHANGES_REQUESTED reviews both survive. The
    # approval reaches the intake on purpose (security f-004): this fetch is
    # what feeds the S5a backstop, and only the WATCHER's `dispositions()`
    # decides that a bare approval is not worth dispatching for.
    assert [(f.author, f.activity_id) for f in trusted] == [
        ("dave", 501),
        ("dave", 502),
    ]
    assert trusted[1].reply_mode is ReplyMode.NONE
    assert "pullrequestreview-502" in trusted[1].thread_url


def test_an_approved_review_that_asks_is_still_injected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PR #425 review, correctness f-001: `APPROVED` used to be silent
    whatever the body said, so the acceptance guard's own "LGTM, but rename
    X" was dropped on both the watcher and the converge side. Only a state
    with nothing written, or a dismissal, is silent now."""
    _install_github(
        monkeypatch,
        reviews=[
            _review(500, author="dave", state="APPROVED", body=""),
            _review(
                501, author="dave", state="APPROVED", body="LGTM, but rename `foo`"
            ),
            _review(502, author="dave", state="DISMISSED", body="rename `foo`"),
        ],
        permissions={"dave": "write"},
    )

    trusted, _untrusted = fetch_external_findings(_REPO, 62, trusted_bots=(_BOT,))

    assert [(f.activity_id, f.body) for f in trusted] == [
        (501, "LGTM, but rename `foo`")
    ]


# ── handoff rendering + injection ──────────────────────────────────────


def _finding(
    *,
    author: str = "dave",
    body: str = "leaks a handle",
    path: str = "src/x.py",
    line: int | None = 12,
    activity_id: int = 7,
    reply_mode: ReplyMode = ReplyMode.THREAD,
    head_sha: str = _HEAD,
) -> ExternalFinding:
    return ExternalFinding(
        author=author,
        source="human",
        trusted=True,
        stream=ReviewStream.INLINE,
        activity_id=activity_id,
        reply_mode=reply_mode,
        thread_url="https://example/thread",
        head_sha=head_sha,
        path=path,
        line=line,
        body=body,
    )


def test_handoff_text_parses_and_attributes_the_author() -> None:
    text = findings_to_handoff_text(
        [
            _finding(),
            _finding(
                body="second",
                path="",
                line=None,
                activity_id=500,
                reply_mode=ReplyMode.NONE,
            ),
        ],
        current_head_sha=_HEAD,
    )
    parsed = parse_review_handoff(text)
    assert parsed.status == "FINDINGS"
    assert len(parsed.findings) == 2
    assert parsed.findings[0].files == ["src/x.py:12"]
    assert "[dave]" in parsed.findings[0].rationale
    assert parsed.findings[1].files == []


def test_a_blocking_review_state_survives_the_fetch_and_reaches_triage() -> None:
    """PR #426 re-review, correctness f-001: the watcher refuses to call a
    ``CHANGES_REQUESTED`` review an approval, but the subprocess re-fetches
    the row — and the state was dropped on the way in, so triage saw a bare
    ``[dave] LGTM`` and could answer it ``NOTHING_TO_REMEDIATE``."""
    row = ExternalReviewActivity(
        stream=ReviewStream.REVIEW,
        activity_id=500,
        author="dave",
        body="LGTM",
        url="https://example/review",
        review_state="CHANGES_REQUESTED",
    )
    finding = ext_mod.finding_from_activity(row, source="human", trusted=True)
    assert finding.review_state == "CHANGES_REQUESTED"

    text = findings_to_handoff_text([finding], current_head_sha=_HEAD)
    rationale = parse_review_handoff(text).findings[0].rationale
    assert "[dave, CHANGES_REQUESTED review]" in rationale
    assert "LGTM" in rationale  # the body reaches the batch intact

    # ...and it is ineligible for the third verdict however it reads.
    _, id_map = ext_mod.external_intake_reviews([finding], current_head_sha=_HEAD)
    assert triage_mod.approval_eligible_ids(id_map) == frozenset()


def test_approval_eligibility_is_read_from_the_row_not_the_verdict() -> None:
    """Security f-001: a ``NOTHING_TO_REMEDIATE`` verdict may only drop a row
    whose own body carries approving words — a real claim never becomes an
    approval because a line of triage prose says so."""
    approval = _finding(body="**No findings.** Ready to merge.", activity_id=1)
    mixed = _finding(body="LGTM, but rename `foo`", activity_id=2)
    claim = _finding(body="this leaks the token", activity_id=3)
    _, id_map = ext_mod.external_intake_reviews(
        [approval, mixed, claim], current_head_sha=_HEAD
    )
    ids = {ext: fid for fid, ext in id_map.items()}
    eligible = triage_mod.approval_eligible_ids(id_map)
    assert ids[approval] in eligible
    assert ids[mixed] in eligible  # carries an approval AND an ask — triage's call
    assert ids[claim] not in eligible


# Bodies whose only approving word is NOT the author's verdict — each a
# measured bypass of the eligibility floor (round-5 review f-001; the round-5
# panel's correctness f-002 for the fence and the colon-less lead-in, security
# f-001 for the invisible comment, security f-002 for the dotted identifier).
_NOT_THE_AUTHORS_VERDICT = [
    "> LGTM\nNo: the token is logged at src/api.py:88.",
    "````\n```\nLGTM\n```\n````\nThe token is logged at src/api.py:88.",
    "Allowed status\n- approved\n\nThe endpoint never validates it at src/api.py:12.",
    "<!-- LGTM -->\nThe admin token is logged at src/api.py:88 — redact it.",
    "The flag task.approved, so nothing validates it. The token is logged at "
    "src/api.py:88.",
    "The flag task._approved, so nothing validates it. The token is logged at "
    "src/api.py:88.",
    "````\n````` example\nLGTM\n````\nThe token is logged at src/api.py:88.",
    "The session is reused even when the user is not\napproved. The token is "
    "logged at src/api.py:88.",
]


@pytest.mark.parametrize("body", _NOT_THE_AUTHORS_VERDICT)
def test_an_approval_that_is_not_the_authors_verdict_cannot_be_dropped(
    body: str,
) -> None:
    """The floor and the parser driven together: an approval word the author
    only quoted, fenced, listed as a value, hid in an invisible comment or
    wrote as part of an identifier is not their verdict, so a
    ``NOTHING_TO_REMEDIATE`` line on that row falls through to PROCEED and the
    defect beside it reaches the coder instead of being consumed at round 0
    with its high-water mark already advanced."""
    quoted = _finding(body=body, activity_id=1)
    verdict = _finding(body="**No findings.** Ready to merge.", activity_id=2)
    _, id_map = ext_mod.external_intake_reviews(
        [quoted, verdict], current_head_sha=_HEAD
    )
    ids = {ext: fid for fid, ext in id_map.items()}
    eligible = triage_mod.approval_eligible_ids(id_map)
    assert ids[quoted] not in eligible
    assert ids[verdict] in eligible

    finding_ids = sorted(id_map)
    text = "".join(
        f"- {fid}: NOTHING_TO_REMEDIATE — the comment only approves\n"
        for fid in finding_ids
    )
    verdicts = triage_mod.parse_triage_verdicts(
        text, finding_ids, approval_eligible=eligible
    )
    assert verdicts.proceed == (ids[quoted],)
    assert set(verdicts.nothing_to_remediate) == {ids[verdict]}


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
    findings = [_finding(), _finding(body="second", activity_id=8)]
    outcomes, id_map = external_intake_reviews(findings, current_head_sha=_HEAD)

    (outcome,) = outcomes
    assert outcome.reviewer == "external"
    assert outcome.status == "FINDINGS" and outcome.passed is False
    assert [f.finding_id for f in outcome.findings] == ["f-001", "f-002"]
    assert id_map["f-001"].activity_id == 7
    assert id_map["f-002"].activity_id == 8
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
    assert [(f.activity_id, f.body) for f in trusted] == [(510, "two new problems")]


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
    id_map = {"f-001": _finding(activity_id=1), "f-002": _finding(activity_id=2)}
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
    # #387: the contract holds in EVERY round and knows a reverted fix
    assert "REVERTED" in text
    assert "NO CHANGE NEEDED" in text  # #380: and a finding that is not a defect
    assert "every round" in text.lower()


# --- #387: a "fixed" claim must survive the final tree -----------------------


def test_final_round_outcomes_reads_the_findings_block_in_round_one_only(
    tmp_path: Path,
) -> None:
    """Round 1's `## Findings` block can only name injected ids, so a
    dispute there counts; a later round's block is the panel's dispute
    contract (ids minted independently) and never speaks for an external
    id — the ack section is the sole channel then (opus round 1)."""
    from lithos_loom.plugins.story_develop.external_reviews import (
        final_round_outcomes,
    )
    from lithos_loom.plugins.story_develop.handoff import coder_handoff_name

    handoff_dir = tmp_path / "handoff"
    handoff_dir.mkdir()
    block = (
        "## Findings\n"
        "- finding_id: f-001\n  severity: minor\n  status: disputed\n"
        "  rationale: r\n  coder_response: deliberate decision\n"
    )
    (handoff_dir / coder_handoff_name(1)).write_text(
        "## Status: LGTM\n## Summary\nf-001 disputed.\n" + block, encoding="utf-8"
    )
    (handoff_dir / coder_handoff_name(2)).write_text(
        "## Status: LGTM\n## Summary\npanel f-001 disputed; external fixed.\n"
        + block
        + "## External findings\n- f-001: FIXED — guarded it\n",
        encoding="utf-8",
    )
    id_map = {"f-001": _finding()}

    def outcomes(rounds: int):
        return final_round_outcomes(
            handoff_dir=handoff_dir,
            run_id="r",
            rounds=rounds,
            loop_approved=True,
            worktree=tmp_path,  # not a repo: the tree read is unknown (None)
            head_sha="h" * 40,
            generated_paths=(),
            id_map=id_map,
            rejections={},
            surviving_ids=["f-001"],
        )

    (r1,) = outcomes(1)
    assert r1.disposition == "disputed" and r1.detail == "deliberate decision"
    (r2,) = outcomes(2)
    assert r2.disposition == "fixed" and r2.detail == "guarded it"


def test_parse_coder_acks_reads_a_no_change_needed_verdict() -> None:
    """#380 (lens #83): an approval verdict injected as a finding is
    dispositioned by the coder as NOT a defect — a fourth word, so the
    outcome parser can read what the coder already writes."""
    text = (
        "## Status: LGTM\n## Summary\nnothing to do\n"
        "## External findings\n"
        "- f-001: NO CHANGE NEEDED — an approval verdict, not a defect\n"
        "- f-002: no_change_needed — the claim describes the intended behaviour\n"
    )
    acks = parse_coder_acks(text, ["f-001", "f-002"])
    assert acks["f-001"].verdict == "no_change_needed"
    assert acks["f-001"].detail == "an approval verdict, not a defect"
    assert acks["f-002"].verdict == "no_change_needed"


def test_outcomes_no_change_needed_is_its_own_disposition() -> None:
    # not a fix (nothing landed) and not a dispute (the coder agrees with the
    # reviewer that there is nothing to change) — but, like `fixed`, a claim
    # the LOOP must have approved (PR #396 review: the coder alone never
    # disposes an external finding; the gate + panel judge the unchanged head)
    id_map = {"f-001": _finding()}
    acks = {"f-001": CoderAck(verdict="no_change_needed", detail="an approval")}
    (o,) = outcomes_after_loop(id_map, {}, {}, acks, loop_approved=True)
    assert o.disposition == "no_change_needed" and o.detail == "an approval"


def test_outcomes_no_change_needed_needs_the_loops_approval() -> None:
    # the panel rejected the claim (or the loop died before judging it): the
    # thread gets no "no change needed" answer — unaddressed, naming the
    # unvalidated claim
    id_map = {"f-001": _finding()}
    acks = {"f-001": CoderAck(verdict="no_change_needed", detail="an approval")}
    (o,) = outcomes_after_loop(id_map, {}, {}, acks, loop_approved=False)
    assert o.disposition == "unaddressed"
    assert "NO CHANGE NEEDED" in o.detail and "an approval" in o.detail
    assert "not approve" in o.detail


def test_parse_coder_acks_reads_a_reverted_verdict() -> None:
    text = (
        "## Status: LGTM\n## Summary\nreverted at the reviewer's insistence\n"
        "## External findings\n"
        "- f-002: REVERTED — the panel holds it contradicts the acceptance "
        "criteria; operator decision needed\n"
    )
    acks = parse_coder_acks(text, ["f-002"])
    assert acks["f-002"].verdict == "reverted"
    assert acks["f-002"].detail.startswith("the panel holds")


def test_outcomes_reverted_ack_is_reported_reverted_never_fixed() -> None:
    """lens #84 (#387): the round-1 ack said FIXED, the final round undid it —
    the final ack is what the thread is answered from."""
    id_map = {"f-002": _finding()}
    acks = {"f-002": CoderAck(verdict="reverted", detail="contradicts the AC")}
    (o,) = outcomes_after_loop(id_map, {}, {}, acks, loop_approved=True)
    assert o.disposition == "reverted"
    assert o.detail == "contradicts the AC"


def test_outcomes_fixed_needs_a_tree_that_moved() -> None:
    """The objective backstop: an approved run whose final tree equals the
    PR head cannot have fixed anything, whatever the handoff claims — and
    since round 1 must commit, it undid what it did: reverted."""
    id_map = {"f-001": _finding()}
    acks = {"f-001": CoderAck(verdict="fixed", detail="guarded it")}
    (o,) = outcomes_after_loop(
        id_map, {}, {}, acks, loop_approved=True, tree_changed=False
    )
    # an APPROVED loop that ends at the PR head is fix-then-revert by
    # construction (round 1 must commit) — stronger than the coder's word,
    # so it is the decision shape, not a mere silence (opus round 1)
    assert o.disposition == "reverted"
    assert "identical to the PR head" in o.detail
    # unknown (None) keeps today's behaviour; True is the normal case
    for known in (None, True):
        (o,) = outcomes_after_loop(
            id_map, {}, {}, acks, loop_approved=True, tree_changed=known
        )
        assert o.disposition == "fixed"


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
        stream=ReviewStream.CONVERSATION,
        activity_id=5551158842,
        reply_mode=ReplyMode.CONVERSATION,
        thread_url=f"https://github.com/{_REPO}/pull/62#issuecomment-5551158842",
        head_sha="",
        body="Verdict: two P1 gaps",
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

    assert [f.activity_id for f in trusted] == [22]
    assert untrusted == []


def test_finding_carries_identity_and_the_adapters_reply_capability() -> None:
    """PR #356 re-review: the finding routes on ``reply_mode``, never on the
    stream — every adapter picks a capability, and the intake copies it."""
    from lithos_loom.github_review_streams import STREAM_ADAPTERS

    assert {a.reply_mode for a in STREAM_ADAPTERS} <= set(ReplyMode)
    for adapter in STREAM_ADAPTERS:
        row = ExternalReviewActivity(
            stream=adapter.stream, activity_id=99, author="x", body="b", url="u"
        )
        finding = ext_mod.finding_from_activity(row, source="human", trusted=True)
        assert (finding.stream, finding.activity_id) == (adapter.stream, 99)
        assert finding.reply_mode is adapter.reply_mode


# ── PR #396 review: a no-change claim is reviewed, not believed ─────────────


def test_claims_nothing_to_change_reads_the_rounds_handoff(tmp_path: Path) -> None:
    """The predicate that admits an empty round 1 for review — true only when
    the round's handoff acknowledges EVERY injected id NO CHANGE NEEDED; a
    FIXED (a contradiction over an unchanged tree), a DISPUTED, an omitted id
    or a missing handoff is not a claim the loop reviews — exit C stands."""
    from lithos_loom.plugins.story_develop import handoff as handoff_mod
    from lithos_loom.plugins.story_develop.external_reviews import (
        claims_nothing_to_change,
    )

    hd = tmp_path / "handoff"
    hd.mkdir()
    ids = ["f-001", "f-002"]
    assert claims_nothing_to_change(hd, 1, ids) is False  # no handoff yet
    path = hd / handoff_mod.coder_handoff_name(1)
    head = "## Status: LGTM\n## Summary\nx\n## External findings\n"
    path.write_text(
        head + "- f-001: NO CHANGE NEEDED — a\n- f-002: no change needed — b\n"
    )
    assert claims_nothing_to_change(hd, 1, ids) is True
    path.write_text(head + "- f-001: FIXED — a\n- f-002: NO CHANGE NEEDED — b\n")
    assert claims_nothing_to_change(hd, 1, ids) is False
    path.write_text(head + "- f-001: DISPUTED — a\n- f-002: NO CHANGE NEEDED — b\n")
    assert claims_nothing_to_change(hd, 1, ids) is False
    path.write_text(head + "- f-001: NO CHANGE NEEDED — a\n")  # f-002 omitted
    assert claims_nothing_to_change(hd, 1, ids) is False
    path.write_text("## Status: LGTM\n## Summary\nnothing\n")  # no section
    assert claims_nothing_to_change(hd, 1, ids) is False
    # the round matters: round 2's handoff is not round 1's claim
    assert claims_nothing_to_change(hd, 2, ids) is False
    assert claims_nothing_to_change(hd, 1, []) is False  # nothing injected


def test_render_external_context_names_every_finding_for_the_panel() -> None:
    """The panel judging a no-change claim must know WHAT was claimed: the
    context names each injected finding (author, location, body — fenced so
    reviewer prose cannot become prompt prose) and tells the reviewers that a
    NO CHANGE NEEDED they disagree with is a finding of theirs."""
    from lithos_loom.plugins.story_develop.external_reviews import (
        render_external_context,
    )

    findings = {
        "f-001": _finding(),
        "f-002": ExternalFinding(
            author="reviewer-bot",
            source="bot",
            trusted=True,
            stream=ReviewStream.CONVERSATION,
            activity_id=9,
            reply_mode=ReplyMode.CONVERSATION,
            thread_url="https://github.com/o/r/pull/1#issuecomment-9",
            head_sha="",
            body="good to merge — the ``` guard ``` exists",
        ),
    }

    ctx = render_external_context(findings)

    assert ctx.startswith("## External review findings under remediation")
    assert "f-001" in ctx and "f-002" in ctx
    assert "reviewer-bot" in ctx and "good to merge" in ctx
    assert "src/x.py:12" in ctx  # the inline finding's location
    assert "NO CHANGE NEEDED" in ctx and "finding of yours" in ctx
    assert "````" in ctx  # the fence outgrows the body's own backticks
    assert render_external_context({}) == ""


def test_ack_section_returns_the_coders_acknowledgement_block() -> None:
    """opus round 2 (High): the round-1 panel must see the claim it judges —
    the coder summary the reviewer prompt renders is the `## Summary`
    paragraph only, so the acknowledgement section is appended to it."""
    from lithos_loom.plugins.story_develop.external_reviews import ack_section

    text = (
        "## Status: LGTM\n## Summary\nnothing to change\n"
        "## External findings\n"
        "- f-001: NO CHANGE NEEDED — an approval verdict\n"
        "- f-002: FIXED — guarded it\n"
        "## Findings\n- finding_id: f-009\n"
    )
    section = ack_section(text)
    assert section.startswith("## External findings")
    assert "f-001: NO CHANGE NEEDED — an approval verdict" in section
    assert "f-002: FIXED" in section
    assert "f-009" not in section  # the next section is not swept in
    assert ack_section("## Status: LGTM\n## Summary\nx\n") == ""


def test_outcomes_the_ack_section_outranks_the_findings_block() -> None:
    """opus round 2 (Medium): a coder that writes NO CHANGE NEEDED on the
    mandated channel AND disputes the same id formally in round 1's
    `## Findings` block is admitted for review on the ack; the epilogue must
    read the same channel first, or an approved no-change run ends `failed`
    over a "contradiction" that is not one. The ack decides; the block only
    speaks for an id with no ack."""
    from lithos_loom.plugins.story_develop.handoff import Finding

    id_map = {"f-001": _finding(), "f-002": _finding(activity_id=8)}
    claims = {
        fid: Finding(
            finding_id=fid,
            severity="minor",
            status="disputed",
            files=[],
            rationale="r",
            coder_response="deliberate decision",
        )
        for fid in id_map
    }
    acks = {"f-001": CoderAck(verdict="no_change_needed", detail="an approval")}
    o1, o2 = outcomes_after_loop(id_map, {}, claims, acks, loop_approved=True)
    assert o1.disposition == "no_change_needed" and o1.detail == "an approval"
    assert o2.disposition == "disputed" and o2.detail == "deliberate decision"
    acks = {"f-001": CoderAck(verdict="fixed", detail="guarded it")}
    (o1, _) = outcomes_after_loop(id_map, {}, claims, acks, loop_approved=True)
    assert o1.disposition == "fixed"


# --- #399: the acks are read across EVERY round, verdicts defined by the tree


_LENS87_FIXTURES = Path(__file__).parent / "fixtures" / "converge_505ad2f8"


def _history_outcomes(handoff_dir: Path, tmp_path: Path, rounds: int, ids: list[str]):
    from lithos_loom.plugins.story_develop.external_reviews import (
        final_round_outcomes,
    )

    return final_round_outcomes(
        handoff_dir=handoff_dir,
        run_id="r",
        rounds=rounds,
        loop_approved=True,
        worktree=tmp_path,  # not a repo: the tree read is unknown (None)
        head_sha="h" * 40,
        generated_paths=(),
        id_map={fid: _finding() for fid in ids},
        rejections={},
        surviving_ids=ids,
    )


def _write_rounds(handoff_dir: Path, sections: dict[int, str]) -> None:
    from lithos_loom.plugins.story_develop.handoff import coder_handoff_name

    handoff_dir.mkdir(exist_ok=True)
    for round_no, acks in sections.items():
        (handoff_dir / coder_handoff_name(round_no)).write_text(
            "## Status: LGTM\n## Summary\nround.\n"
            + ("## External findings\n" + acks if acks else ""),
            encoding="utf-8",
        )


def test_ack_instruction_defines_the_verdicts_by_the_tree_not_the_round() -> None:
    """#399: lens #87's coder read "as of this handoff" as "what I did this
    round" and wrote NO CHANGE NEEDED over a fix it made in round 1. The
    contract names the tree as the referent, says a fixed id stays FIXED in
    every later handoff, defines NO CHANGE NEEDED as "never a defect", and
    keeps the panel's same-looking ids out of the section."""
    text = " ".join(ack_instruction(["f-001"]).split())  # wrapping-agnostic
    assert "whichever round made it" in text
    assert "stays FIXED in every later handoff" in text
    assert "nothing was ever changed for it" in text
    assert 'does NOT mean "nothing further this round"' in text
    assert "its f-001 is not this f-001" in text  # the panel's ids never here


def test_final_round_outcomes_a_round_one_fix_survives_a_final_no_change_needed(
    tmp_path: Path,
) -> None:
    """The lens #87 run itself (505ad2f8, #399): f-001 FIXED in round 1, the
    panel's unrelated f-001 attached to the id in rounds 2-3, round 4 says
    NO CHANGE NEEDED — the fix is in the tree, so the disposition is
    `fixed` with round 1's detail, and the drift is named in the note."""
    (o,) = _history_outcomes(_LENS87_FIXTURES, tmp_path, 4, ["f-001"])
    assert o.disposition == "fixed"
    assert o.detail.startswith("the chain now carries its own")
    assert o.note.startswith("FIXED in round 1 and every round since; the round 4 ")
    assert "NO CHANGE NEEDED" in o.note and o.note.endswith("read as round 1's FIXED")


def test_final_round_outcomes_only_round_ones_fixed_carries_forward(
    tmp_path: Path,
) -> None:
    """Round 1 predates the panel, so its section can only speak of the
    external ids; a later round's FIXED line may describe the panel's own
    same-looking f-001 (lens #87 rounds 2-3). Any disagreement whose
    trusted reading is not "round 1 said FIXED and every round since" is
    unaddressed — no thread answered, the note says what the handoffs said
    — a false "Fixed in" being the one unacceptable outcome (opus review)."""
    cases = {
        # fixed, reverted, "no change": the revert is never carried forward
        "revert": {
            1: "- f-001: FIXED — guarded it\n",
            2: "- f-001: REVERTED — contradicts the acceptance criteria\n",
            3: "- f-001: NO CHANGE NEEDED — nothing further this round\n",
        },
        # re-fixed in round 3: a later-round origin is not trusted
        "refix": {
            1: "- f-001: FIXED — guarded it\n",
            2: "- f-001: REVERTED — the panel objected\n",
            3: "- f-001: FIXED — guarded it the other way\n",
            4: "- f-001: NO CHANGE NEEDED — unchanged since round 3\n",
        },
        # disputed in round 1, FIXED lines later (the contaminated shape)
        "contaminated": {
            1: "- f-001: DISPUTED — not a defect\n",
            2: "- f-001: FIXED — correctness: the panel's own f-001\n",
            3: "- f-001: NO CHANGE NEEDED — the reviewer closed it with LGTM\n",
        },
        # the streak from round 1 is broken by an omission
        "gap": {
            1: "- f-001: FIXED — guarded it\n",
            2: "",
            3: "- f-001: NO CHANGE NEEDED — nothing further\n",
        },
    }
    for name, rounds in cases.items():
        _write_rounds(tmp_path / name, rounds)
        (o,) = _history_outcomes(tmp_path / name, tmp_path, len(rounds), ["f-001"])
        assert o.disposition == "unaddressed", name
        assert "NO CHANGE NEEDED" in o.note and "not answered" in o.note, name
    (o,) = _history_outcomes(tmp_path / "revert", tmp_path, 3, ["f-001"])
    assert "FIXED in round 1, REVERTED in round 2" in o.note
    (o,) = _history_outcomes(tmp_path / "contaminated", tmp_path, 3, ["f-001"])
    assert "FIXED in round 2" in o.note and "round 1" not in o.note


def test_final_round_outcomes_a_loop_that_died_before_its_final_handoff(
    tmp_path: Path,
) -> None:
    """`rounds` past the handoffs on disk (the loop died mid-round): no
    final ack, nothing carried forward, the note names the missing handoff
    rather than an omission the coder chose."""
    _write_rounds(tmp_path / "h", {1: "- f-001: FIXED — guarded it\n"})
    (o,) = _history_outcomes(tmp_path / "h", tmp_path, 2, ["f-001"])
    assert o.disposition == "unaddressed" and o.detail == ""
    assert o.note == "FIXED in round 1; no acknowledgement in the round 2 handoff"


def test_outcomes_the_note_survives_the_tree_backstop_without_contradiction() -> None:
    """F N over an unmoved tree: the backstop makes it `reverted` (#387);
    the note describes the handoffs, never asserts what the tree carries."""
    id_map = {"f-001": _finding()}
    acks = {"f-001": CoderAck(verdict="fixed", detail="guarded it")}
    notes = {
        "f-001": "FIXED in round 1 and every round since; ... read as round 1's FIXED"
    }
    (o,) = outcomes_after_loop(
        id_map, {}, {}, acks, loop_approved=True, tree_changed=False, notes=notes
    )
    assert o.disposition == "reverted" and "identical to the PR head" in o.detail
    assert o.note == notes["f-001"] and "tree carries" not in o.note


def test_resolve_ack_history_table() -> None:
    """The resolver alone, over ack sequences (F/R/D/N/- per round): the
    final ack stands when decisive; a final N carries round 1's F forward
    only through an unbroken F streak; everything else is None."""
    from lithos_loom.plugins.story_develop.external_reviews import (
        resolve_ack_history,
    )

    verdict = {"F": "fixed", "R": "reverted", "D": "disputed", "N": "no_change_needed"}

    def resolve(seq: str):
        history = [
            (
                i + 1,
                None if c == "-" else CoderAck(verdict=verdict[c], detail=f"d{i + 1}"),
            )
            for i, c in enumerate(seq)
        ]
        ack, note = resolve_ack_history(history)
        return (ack.verdict if ack else None, ack.detail if ack else None, bool(note))

    assert resolve("F") == ("fixed", "d1", False)
    assert resolve("N") == ("no_change_needed", "d1", False)
    assert resolve("FR") == ("reverted", "d2", False)
    assert resolve("RF") == ("fixed", "d2", False)
    assert resolve("FD") == ("disputed", "d2", False)
    assert resolve("NN") == ("no_change_needed", "d2", False)
    assert resolve("DN") == ("no_change_needed", "d2", False)
    assert resolve("FN") == ("fixed", "d1", True)
    assert resolve("FFFN") == ("fixed", "d1", True)
    for broken in ("FRN", "FRFN", "DFN", "-FN", "NFN", "F-N", "FDFN", "RN", "FRRN"):
        assert resolve(broken) == (None, None, True), broken
    assert resolve("F-") == (None, None, True)
    assert resolve("N-") == (None, None, False)
    assert resolve("--") == (None, None, False)


def test_final_round_outcomes_no_change_needed_throughout_carries_no_note(
    tmp_path: Path,
) -> None:
    """The ordinary #380 shape is untouched: a finding never changed for is
    `no_change_needed` on the final ack, nothing to remark on."""
    _write_rounds(
        tmp_path / "h",
        {
            1: "- f-001: NO CHANGE NEEDED — an approval verdict\n",
            2: "- f-001: NO CHANGE NEEDED — an approval verdict\n",
        },
    )
    (o,) = _history_outcomes(tmp_path / "h", tmp_path, 2, ["f-001"])
    assert o.disposition == "no_change_needed"
    assert o.detail == "an approval verdict"
    assert o.note == ""


def test_final_round_outcomes_a_final_fixed_or_reverted_stands_on_its_own(
    tmp_path: Path,
) -> None:
    """A decisive final ack is the answer, whatever came before (#387's
    rule): FIXED after a REVERTED is fixed; REVERTED after a FIXED is
    reverted; neither carries a note."""
    _write_rounds(
        tmp_path / "h",
        {
            1: "- f-001: FIXED — guarded it\n- f-002: REVERTED — objected\n",
            2: "- f-001: REVERTED — contradicts the AC\n- f-002: FIXED — redone\n",
        },
    )
    a, b = _history_outcomes(tmp_path / "h", tmp_path, 2, ["f-001", "f-002"])
    assert (a.disposition, a.detail, a.note) == ("reverted", "contradicts the AC", "")
    assert (b.disposition, b.detail, b.note) == ("fixed", "redone", "")


def test_final_round_outcomes_an_omitted_final_ack_names_the_earlier_claim(
    tmp_path: Path,
) -> None:
    """Omission keeps the safe direction (#387: no "Fixed in" on a stale
    claim) but the operator is told what was dropped: the note names the
    round-1 FIXED the final section left out."""
    _write_rounds(
        tmp_path / "h",
        {
            1: "- f-001: FIXED — guarded it\n- f-002: FIXED — also guarded\n",
            2: "- f-002: FIXED — also guarded\n",
        },
    )
    a, b = _history_outcomes(tmp_path / "h", tmp_path, 2, ["f-001", "f-002"])
    assert a.disposition == "unaddressed"
    assert "round 2" in a.detail and "acknowledg" in a.detail
    assert a.note == "FIXED in round 1; no acknowledgement in the round 2 handoff"
    assert b.disposition == "fixed" and b.note == ""
