"""Tests for the PR-comment vocabulary in ``lithos_loom.github_models``.

The conversation-comment stream (#353) needs three things the inline stream
already has: a way to recognise loom's own comments (never re-ingested), a
reply shape that names the comment it answers (so a landed fix can be proven
handled), and the actionability rule.
"""

from __future__ import annotations

from lithos_loom.github_models import (
    AUTOMATED_REPLY_MARKER,
    LOOM_NOTICE_MARKER,
    IssueComment,
    is_landed_fix_reply,
    is_loom_pr_comment,
    issue_comment_is_actionable,
    issue_comment_reply_body,
    issue_comment_reply_target,
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
