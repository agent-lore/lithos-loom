"""Tests for the PR-comment vocabulary in ``lithos_loom.github_models``.

The conversation-comment stream (#353) needs three things the inline stream
already has: a way to recognise loom's own comments (never re-ingested), a
reply shape that names the comment it answers (so a landed fix can be proven
handled), and the actionability rule.
"""

from __future__ import annotations

import pytest

from lithos_loom.github_models import (
    AUTOMATED_REPLY_MARKER,
    LOOM_NOTICE_MARKER,
    IssueComment,
    PullRequestReview,
    is_approval_text,
    is_automated_reply,
    is_landed_fix_reply,
    is_loom_pr_comment,
    issue_comment_is_actionable,
    issue_comment_reply_body,
    issue_comment_reply_target,
    review_is_actionable,
)

_URL = "https://github.com/o/r/pull/78#issuecomment-5551158842"


def _c(body: str, author: str = "dave") -> IssueComment:
    return IssueComment(comment_id=1, author=author, body=body, html_url=_URL)


def test_loom_pr_comment_recognises_every_loom_authored_shape() -> None:
    assert is_loom_pr_comment(f"Fixed in abc — done\n\n{AUTOMATED_REPLY_MARKER}")
    assert is_loom_pr_comment(
        f"@dave [NeedsHuman] loom stopped on x\n\n{LOOM_NOTICE_MARKER}"
    )
    # Notices posted before the marker existed carry the fixed head only.
    assert is_loom_pr_comment(
        "@dave [NeedsHuman] loom stopped on **x** (`max_rounds`): y"
    )
    assert not is_loom_pr_comment("Verdict: not ready to merge yet")
    assert not is_loom_pr_comment("a human mentioning [NeedsHuman] in passing")


def test_issue_comment_actionable_needs_a_body_from_a_non_loom_author() -> None:
    assert issue_comment_is_actionable(_c("Verdict: two P1 gaps"))
    assert not issue_comment_is_actionable(_c("   \n"))
    assert not issue_comment_is_actionable(
        _c(f"Not changed — x\n\n{AUTOMATED_REPLY_MARKER}")
    )


def test_reply_body_names_its_target_and_keeps_the_landed_fix_shape() -> None:
    reply = f"Fixed in abc123def4 — guarded it\n\n{AUTOMATED_REPLY_MARKER}"
    body = issue_comment_reply_body(reply, _URL)
    assert body.startswith("Fixed in abc123def4")  # the proof head survives
    assert is_landed_fix_reply(body)
    assert issue_comment_reply_target(body) == 5551158842


def test_reply_target_reads_only_the_reply_line_never_the_prose() -> None:
    # A coder detail quoting another comment's url must not be mistaken for
    # the reply target; only the marker line counts.
    prose = f"Not changed — see {_URL} for context\n\n{AUTOMATED_REPLY_MARKER}"
    assert issue_comment_reply_target(prose) is None
    assert issue_comment_reply_target("plain human text") is None


# ── PR #354 review, finding 1: a QUOTED loom marker is not loom's ─────
#
# GitHub's Quote-reply carries the quoted comment into the new one as
# `> ...` lines. A human verdict that quotes a loom notice or reply must
# never be discarded as automation — seeing the operator's Conversation
# comment is the whole point of the stream.


def test_quoted_loom_comment_followed_by_a_human_verdict_is_human() -> None:
    quoted_notice = (
        "> _(automated notice by lithos-loom)_\n\n"
        "Still not ready: the retry path drops state."
    )
    assert not is_loom_pr_comment(quoted_notice)
    quoted_reply = (
        f"> Fixed in abc123 — guarded it\n> \n> {AUTOMATED_REPLY_MARKER}\n\n"
        "No it isn't — the guard is on the wrong branch."
    )
    assert not is_loom_pr_comment(quoted_reply)
    assert not is_automated_reply(quoted_reply)
    quoted_legacy = (
        "> @dave [NeedsHuman] loom stopped on **x** (`max_rounds`): y\n\n"
        "Resolved this by hand; re-dispatch."
    )
    assert not is_loom_pr_comment(quoted_legacy)
    # …and mentioning the phrase mid-sentence is discussion, not a notice.
    assert not is_loom_pr_comment(
        "the '[NeedsHuman] loom stopped on' notice fired twice here"
    )
    assert issue_comment_is_actionable(_c(quoted_notice))


def test_genuine_loom_shapes_are_still_recognised_structurally() -> None:
    # A marker on its own line (what loom writes), with surrounding blank
    # lines, trailing whitespace, or CRLF endings.
    assert is_loom_pr_comment(f"Not changed — x\n\n{AUTOMATED_REPLY_MARKER}")
    assert is_loom_pr_comment(f"Not changed — x\r\n\r\n{AUTOMATED_REPLY_MARKER}  \r\n")
    assert is_loom_pr_comment(
        f"@dave [NeedsHuman] loom stopped on x\n\n{LOOM_NOTICE_MARKER}"
    )
    assert is_loom_pr_comment(
        f"@dave [NeedsHuman] loom stopped on x\n\n{LOOM_NOTICE_MARKER}\n"
    )
    # The legacy notice: the fixed head at the very start of the body.
    assert is_loom_pr_comment(
        "@dave [NeedsHuman] loom stopped on **x** (`max_rounds`): y"
    )
    assert is_automated_reply(f"Fixed in abc — done\n\n{AUTOMATED_REPLY_MARKER}")
    # The reply line loom appends AFTER the marker on conversation replies
    # keeps the marker structural.
    body = issue_comment_reply_body(
        f"Fixed in abc — done\n\n{AUTOMATED_REPLY_MARKER}", _URL
    )
    assert is_loom_pr_comment(body) and is_landed_fix_reply(body)


# ── an approval is not a finding (827cedf8 / lens #100) ────────────────


@pytest.mark.parametrize(
    "body",
    [
        "LGTM",
        "lgtm!",
        "**No findings.** Ready to merge.",  # Dave's comment on lens #100
        "No findings. Ready to merge.",
        "Looks good to me. Thanks!",
        "Approved; nothing to flag here.",
        "- LGTM\n- no issues found",
        "👍",
        "Ship it 🚀",
        "All good",
        "LGTM overall",  # correctness f-002: ordinary qualifiers cost a round
        "overall LGTM",
        "no findings from me",
        "No further comments",
        "Approving — all good",
        "I'm happy with this, ready to merge",
        "No findings.\n\nThanks again — great work!",
    ],
)
def test_pure_approvals_are_recognised(body: str) -> None:
    assert is_approval_text(body)


@pytest.mark.parametrize(
    "body",
    [
        # The guard: an approval that ALSO asks is a finding, not an approval.
        "LGTM, but rename X",
        "LGTM — but please rename `foo`",
        "👍 but rename X",
        "Approving, but note the TODO on line 40",
        "Ready to merge once CI is green",
        # Not approvals at all.
        "this leaks a handle",
        "nit: rename this",
        "Two problems here: the guard is wrong",
        "Thanks!",  # courtesy alone is not a verdict
        "Nice work",
        "",
        "```python\nx = 1\n```",
    ],
)
def test_anything_carrying_an_ask_is_not_an_approval(body: str) -> None:
    assert not is_approval_text(body)


@pytest.mark.parametrize(
    "body",
    [
        # Security f-003's measured bypasses: the first cut deleted every
        # non-ASCII codepoint before matching, so an ASCII approval token (or
        # an approval emoji) beside an ask in any other script read as a bare
        # approval — and was posted as "no actionable finding".
        "✅ Нужно исправить",
        "👍\nこの変更は脆弱です",
        "LGTM 修复这个漏洞",
        "LGTM. 请重命名变量",
        "LGTM — 이 코드는 비밀번호를 로그에 남깁니다",
        "LGTM Ｆｉｘ　ｔｈｅ　ａｕｔｈ　ｂｙｐａｓｓ",  # fullwidth ASCII is non-ASCII
    ],
)
def test_an_ask_in_another_script_is_never_an_approval(body: str) -> None:
    assert not is_approval_text(body)


@pytest.mark.parametrize(
    "body", ["Ready to merge?", "LGTM?", "No findings?", "Approved?"]
)
def test_a_question_is_never_an_approval(body: str) -> None:
    """Correctness f-002: `?` used to be a unit boundary, so it was thrown
    away and "Ready to merge?" — a reviewer ASKING — matched the phrase
    behind it."""
    assert not is_approval_text(body)


def test_typographic_punctuation_is_folded_not_deleted() -> None:
    # Canonicalised, so the curly-quote and ellipsis spellings read like their
    # ASCII twins…
    assert is_approval_text("I’m happy with this")
    assert is_approval_text("**No findings.** … Ready to merge.")
    # …while a zero-width character cannot smuggle an ask past the matcher.
    assert not is_approval_text("LGTM​ rename the handle")


def test_review_state_policy_follows_the_body_not_just_the_state() -> None:
    def review(state: str, body: str = "") -> PullRequestReview:
        return PullRequestReview(author="dave", body=body, state=state)

    # PR #425 review, correctness f-001: `APPROVED` was unconditionally
    # silent, so an approval that also ASKED was dropped by every consumer.
    assert review_is_actionable(review("APPROVED", "LGTM, but rename X"))
    assert not review_is_actionable(review("APPROVED"))
    # A dismissal has had its say, whatever it says.
    assert not review_is_actionable(review("DISMISSED", "rename X"))
    # Unchanged: CHANGES_REQUESTED always, every other state on content.
    assert review_is_actionable(review("CHANGES_REQUESTED"))
    assert review_is_actionable(review("COMMENTED", "rename X"))
    assert not review_is_actionable(review("COMMENTED", "   "))
    assert review_is_actionable(review("QUEUED", "rename X"))
    # This rule does NOT read the body for approval prose (security f-004):
    # that is the watcher's dispatch question, decided once in
    # `dispositions()`, so the converge intake keeps feeding approval rows to
    # the S5a backstop on every stream alike.
    assert review_is_actionable(review("APPROVED", "LGTM"))
    assert review_is_actionable(review("COMMENTED", "LGTM"))
