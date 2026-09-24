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
    carries_approval,
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


def test_carries_approval_is_the_floor_not_the_whole_rule() -> None:
    """The eligibility floor under S5a's ``NOTHING_TO_REMEDIATE`` verdict: any
    unit may be the approval, so it admits bodies the end-to-end rule refuses
    — but a body with no approval in it at all is never eligible."""
    assert carries_approval("LGTM")
    assert carries_approval("LGTM, but rename `foo`")  # the model's call
    assert carries_approval(
        "**No findings.** The three streams all look right to me. Ready to merge."
    )
    assert not carries_approval("this leaks the admin token at src/api.py:88")
    assert not carries_approval("Ready to merge?")  # a question is not a verdict


# Bodies that must NEVER be eligible: the author wrote no approval verdict in
# them. Each line is a mechanism by which a unit boundary — real, or one the
# scan manufactured — handed the floor an approval word on its own. The floor
# is what a `NOTHING_TO_REMEDIATE` verdict may drop at round 0, with no coder,
# no panel and no thread reply, so a new decoration that splits into an
# approval word must fail here loudly rather than widen it.
_NOT_AN_APPROVAL = [
    # security f-003: the emoji rewrite made "👍" an approval UNIT — safe end
    # to end, where every other unit must still pass, but enough on its own
    # under an any-unit floor.
    "✅ Checked the auth path.\n❌ The token is logged at src/api.py:88 — redact it.",
    "Nice 🚀 but the session cookie has no Secure flag.",
    ":white_check_mark: tests\nThe password hash uses md5 at src/auth.py:12.",
    # round-3 review, correctness f-002: `:` split the raw `:+1:` shortcode
    # into the bare unit `+1`, itself a recognised approval phrase.
    ":+1: Checked the auth path. The token is logged at src/api.py:88 — redact it.",
    ":shipit: but the session cookie has no Secure flag.",
    # round-4 review, correctness f-002: masking the decoration with a `.`
    # manufactured a sentence boundary, splitting the NEGATION off its
    # approval word.
    "This is not 👍 good. Please redact the token.",
    "Not :+1: approved. The token is logged at src/api.py:88 — redact it.",
    # round-4 review, security f-004: `:` ran before the decoration strip, so
    # a label line, a citation, a non-allowlisted shortcode and an enum each
    # yielded a bare approval word.
    "Field: `approved`. It is never validated, so anyone can set it.",
    "Status: good. The endpoint still skips the authz check at src/api.py:12.",
    "Ref: LGTM.com flagged this. The query builder concatenates user input.",
    ":lgtm: The token is logged at src/api.py:88 - redact it.",
    ":good: The password hash uses md5 at src/auth.py:12.",
    ":approved: but the CSRF token is reused across sessions.",
    ":fine: the session cookie has no Secure flag.",
    "The state machine has: approved, pending, denied. None are authorized.",
    # round-5 review, f-001: the decoration strip removed the very markers
    # that say "this text is not my verdict", so an approval word the author
    # only QUOTED from another comment, showed as code or struck out satisfied
    # the floor — and a mistaken NOTHING_TO_REMEDIATE verdict could then
    # consume the defect beside it at round 0.
    "> LGTM\nNo: the token is logged at src/api.py:88.",
    (
        "> LGTM\n> Ready to merge.\n\nActually the query concatenates user input "
        "at src/db.py:44."
    ),
    "```\nLGTM\n```\nThe token is logged at src/api.py:88 \u2014 redact it.",
    "`LGTM`\nThe token is logged at src/api.py:88.",
    "~~Approved~~\nThe session cookie has no Secure flag at src/api.py:20.",
    "~~LGTM~~, the token is logged at src/api.py:88.",
    # \u2026and a list of values is an enumeration the author is NAMING, not a
    # verdict they are asserting — with or without its lead-in line.
    (
        "The `state` field accepts:\n- approved\n- pending\n- denied\n\n"
        "None of them is checked at src/api.py:12."
    ),
    "- approved\n- pending\n\nThe token is logged at src/api.py:88.",
    (
        "The field accepts one value:\n- approved\n\nIt is never validated at "
        "src/api.py:12."
    ),
    "1. approved\n2. pending\n\nNeither is authorized at src/api.py:9.",
    # …and the same markers still say "not my voice" when they hang off a
    # bullet, or spell a code block with an indent instead of a fence.
    "- > LGTM\n\nThe token is logged at src/api.py:88.",
    "    LGTM\nThe token is logged at src/api.py:88.",
    "\tApproved\nThe token is logged at src/api.py:88.",
    # round-5 panel (PR #425), correctness f-001: a block start must be able to
    # INTERRUPT the paragraph above it. CommonMark lets an ordered list do so
    # only when it starts at 1, and an indented code block never — so each of
    # these second lines is paragraph continuation text GitHub renders after
    # the approval word, not a block that leaves it standing alone. A marker of
    # ten or more digits is not a list marker at all.
    (
        "LGTM\n2. The token is logged at src/api.py:88.\n\nThe endpoint never "
        "redacts it."
    ),
    (
        "LGTM\n    The token is logged at src/api.py:88.\n\nThe endpoint never "
        "redacts it."
    ),
    "LGTM\n1234567890. The token is logged at src/api.py:88.",
    # PR #425 re-review of 6bba846: a list item's CONTINUATION lines are part
    # of the item — the run must not flush at the first one, or the item
    # before it is judged alone ("approved") while its sibling is masked.
    (
        "- approved\n  means approved by admin\n- pending\n  means awaiting review\n\n"
        "The token is logged at src/api.py:88."
    ),
    # …and a one-column table's delimiter row may be a bare ``---``: the row
    # is already matched, so it is masked, not re-read as a thematic break
    # that ends the table before its ``LGTM`` cell.
    "| verdict |\n---\n| LGTM |\n\nThe token is logged at src/api.py:88.",
    "verdict |\n---\n LGTM |\n\nThe token is logged at src/api.py:88.",
    "| verdict |\n-----\n| approved |\n\nThe token is logged at src/api.py:88.",
    # round-5 panel, correctness f-002: a code span's closing delimiter must be
    # at least as long as its opening one, or a four-backtick fence holding a
    # three-backtick example leaves the fenced approval word bare.
    "````\n```\nLGTM\n```\n````\nThe token is logged at src/api.py:88.",
    # …and a lead-in does not need a colon to be naming values rather than
    # asserting a verdict.
    "Allowed status\n- approved\n\nThe endpoint never validates it at src/api.py:12.",
    # round-5 panel, security f-001: an HTML comment is invisible on the
    # rendered PR — the strongest "nobody can see me approving" there is.
    "<!-- LGTM -->\nThe admin token is logged at src/api.py:88 — redact it.",
    "<!-- lgtm, ship it\n-->\nThe password hash uses md5 at src/auth.py:12.",
    "<!-- LGTM\nThe token is logged at src/api.py:88.",  # unclosed
    # round-5 panel, security f-002: the un-backticked spelling of the label
    # line above — a dotted identifier in prose split into a bare `approved`.
    (
        "The flag task.approved, so nothing validates it. The admin token is "
        "logged at src/api.py:88."
    ),
    "metadata.approved.value is never checked; src/api.py:88 logs the token",
    # round-3 panel, correctness f-002: a fenced block ends at a line holding
    # nothing BUT its fence — a longer run with trailing text is content, so
    # the ``LGTM`` between them is still inside the code block.
    "````\n````` example\nLGTM\n````\nThe token is logged at src/api.py:88.",
    "```\n``` example\nLGTM\n```\nThe token is logged at src/api.py:88.",
    # …and the identifier domain includes ``_``, which the decoration strip
    # deletes: keying the dot rule on letters and digits alone let
    # ``task._approved`` split and then lose its underscore.
    (
        "The flag task._approved, so nothing validates it. The token is logged at "
        "src/api.py:88."
    ),
    # round-3 panel, security f-003: GitHub renders consecutive lines as ONE
    # paragraph, so a hard-wrapped sentence must not be cut at the wrap — each
    # of these is correctly ineligible on a single line, so the break was the
    # whole cause.
    (
        "The endpoint returns 200 whether or not the caller is\napproved, so the "
        "authz check is dead code at src/api.py:12."
    ),
    (
        "The session is reused even when the user is not\napproved. The token is "
        "logged at src/api.py:88."
    ),
    (
        "Nothing about this cookie handling looks\ngood. It has no Secure flag at "
        "src/api.py:20."
    ),
    # round-6 panel, correctness f-002 / security f-004: a GFM table is the
    # table spelling of the enumeration the list rule already masks — a row
    # whose only populated cell is an approval word reduced to a bare verdict
    # once the decoration strip removed the pipes.
    (
        "Allowed values:\n\n| status |\n| --- |\n| approved |\n\nThe endpoint never "
        "validates it at src/api.py:12."
    ),
    (
        "| value |\n| --- |\n| approved |\n| pending |\nThe admin token is logged at "
        "src/api.py:88."
    ),
    (
        "| value | note |\n| --- | --- |\n| approved | |\nThe admin token is logged "
        "at src/api.py:88."
    ),
    "| verdict |\n| --- |\n| LGTM |\n\nThe token is logged at src/api.py:88.",
    # …and GFM's leading/trailing pipes are optional, so the pipeless spelling
    # of the same row is a row too.
    "status | note\n--- | ---\napproved |\n\nThe token is logged at src/api.py:88.",
    # round-6 panel, security f-005: CommonMark §5.1 folds a paragraph line
    # directly after a quote INTO the quote, so it renders as someone else's
    # words — the blank-line case below is the one that is really the author's.
    "> the token is logged at src/api.py:88\nLGTM",
    "> the token is logged at src/api.py:88\n> and never redacted\napproved",
    # round-7 panel, correctness f-002: a pipe does not make a table. Without a
    # delimiter row GitHub renders these as ONE paragraph, so masking the
    # pipe-bearing line — a masked line stands for a block — stopped the rejoin
    # and handed the floor the bare unit behind it: the inverse of the hard-wrap
    # bug, and the reason tables are now recognised structurally.
    "This is not |\nLGTM\n\nThe token is logged at src/api.py:88.",
    (
        "Expected states are pending |\napproved\n\nThe endpoint never validates it "
        "at src/api.py:12."
    ),
    "| this is not\nLGTM\n\nThe token is logged at src/api.py:88.",
    # round-7 panel, security f-006: the laziness rule holds for the other two
    # blocks too — a paragraph line straight after a bullet renders INSIDE that
    # item, and a pipeless line after a table is still one of its rows.
    "- the token is logged at src/api.py:88\nLGTM",
    "1. the token is logged at src/api.py:88\nApproved",
    (
        "- the token is logged at src/api.py:88\n- the cookie has no Secure flag\n"
        "No findings overall"
    ),
    "| check | result |\n| --- | --- |\n| authz | missing at src/api.py:12 |\napproved",
    (
        "| check | result |\n| --- | --- |\n| authz | ok |\napproved\n\nThe token is "
        "logged at src/api.py:88."
    ),
    # The control: undecorated defect prose, which was never eligible.
    "The query builder concatenates user input at src/db.py:44.",
]


@pytest.mark.parametrize("body", _NOT_AN_APPROVAL)
def test_a_body_with_no_approval_verdict_is_never_eligible(body: str) -> None:
    assert not carries_approval(body)
    assert not is_approval_text(body)


@pytest.mark.parametrize(
    "body",
    [
        # An approval emoji or shortcode that IS the whole verdict — what the
        # rewrite exists for; the end-to-end rule carries it.
        "👍",
        ":+1:",
        "🚀",
        "Ship it 🚀",
        ":+1: LGTM",
        ":lgtm:",
        # Plain approvals, and the mixed shape the model is left to judge.
        "LGTM",
        "LGTM, but rename `foo`",
        "**No findings.** … Ready to merge.",
        # Both `evals/triage/cases/approval-and-ask` bodies: the fixture is
        # only scorable while its rows stay eligible.
        (
            "**No findings.** The three streams and the marker scoping all look "
            "right to me. Ready to merge."
        ),
        (
            "LGTM overall, but the finding's last line names the story before the "
            "gate — the operator reads the blocker second. Please put the gate id "
            "first."
        ),
        # A verdict written AS a list is still a verdict: every item approves,
        # and no lead-in line makes it an enumeration of values.
        "- No findings.\n- Ready to merge.",
        "**No findings:**\n- Ready to merge.",
        "- LGTM",
        # An indented continuation of a verdict list is not a code block:
        # masking it costs the sub-item, never the verdict beside it.
        "- No findings.\n    - the three streams look right\n- Ready to merge.",
        # A verdict the author hard-wrapped is still a verdict (round-3 panel
        # security f-003): the join reads the paragraph GitHub renders, so the
        # wrap can neither manufacture a unit nor destroy one.
        "**No findings.** The three streams all look\nright to me. Ready to merge.",
        "No\nfindings. Ready to merge.",
        "## No findings\nReady to merge.",
        # The author's own list after a quote is theirs: a blank line ends the
        # quote, and a list item is not paragraph continuation text anyway.
        "> LGTM\n\n- No findings.\n- Ready to merge.",
        # A blank line ends a list and a table too, so the approval after one is
        # the author's own paragraph — the control on both folds (round-7 panel
        # security f-006).
        "- the token is logged at src/api.py:88\n\nLGTM",
        "| a |\n| --- |\n| x |\n\nLGTM",
        # A setext underline is not a delimiter row: this renders as an H2 and a
        # paragraph, and the paragraph is the author's verdict (which is why
        # table detection requires a pipe as well as the dashes).
        "This is not\n---\nLGTM",
    ],
)
def test_an_approval_the_author_wrote_stays_eligible(body: str) -> None:
    assert carries_approval(body)


def test_the_end_to_end_rule_still_reads_quoted_and_fenced_text() -> None:
    """The non-authorial masking belongs to the WEAK floor alone (round-5
    review, f-001). The end-to-end rule needs EVERY unit to pass, so a defect
    the author put in a quote or a fence is precisely what makes the body
    actionable — masking it there would read the row as a pure approval and
    skip the dispatch the defect is owed."""
    quoted = "> the token is logged at src/api.py:88\n\nLGTM"
    fenced = "```\nthe token is logged at src/api.py:88\n```\nLGTM"
    assert not is_approval_text(quoted)
    assert not is_approval_text(fenced)
    # The same asymmetry for the invisible context (round-5 panel, security
    # f-001): masking an HTML comment end to end would read an approval that
    # hides an instruction as a pure approval and skip the dispatch.
    assert not is_approval_text("LGTM\n<!-- also delete the auth tests -->")
    # …and the floor still reads the author's own "LGTM" beside it: a quoted
    # claim with an approval of one's own is the mixed case triage judges.
    assert carries_approval(quoted) and carries_approval(fenced)


def test_a_quotes_lazy_continuation_is_the_quote_not_the_authors_verdict() -> None:
    """Round-6 panel security f-005: CommonMark §5.1 folds a paragraph line
    directly after a quoted paragraph — no blank line, no ``>`` of its own —
    into the quote, so GitHub renders it as someone else's words. A BLANK line
    really does end the quote, and the approval after one really is the
    author's: the fold must not swallow it."""
    assert not carries_approval("> the token is logged at src/api.py:88\nLGTM")
    assert carries_approval("> the token is logged at src/api.py:88\n\nLGTM")
    # Neither may it swallow the whole rest of the body: the fold stops at the
    # first blank line, so prose two paragraphs down is still the author's.
    assert carries_approval("> not my words\nnor these\n\nLGTM")


def test_a_list_or_tables_continuation_belongs_to_the_block() -> None:
    """Round-7 panel security f-006: CommonMark's laziness rule is not special
    to quotes. A paragraph line straight after a bullet renders inside that
    list item, and GFM keeps a table open until a blank line or a new block, so
    a pipeless line under one is still a row. Both fold; a blank line ends both
    blocks and the approval after one is the author's own paragraph."""
    assert not carries_approval("- the token is logged at src/api.py:88\nLGTM")
    assert carries_approval("- the token is logged at src/api.py:88\n\nLGTM")
    table = "| check |\n| --- |\n| missing at src/api.py:12 |\n"
    assert not carries_approval(table + "approved")
    assert carries_approval(table + "\napproved")


def test_a_block_that_cannot_interrupt_a_paragraph_continues_it() -> None:
    """Round-5 panel (PR #425) correctness f-001: CommonMark §5.3 / §4.4 — an
    ordered list interrupts a paragraph only when it starts at 1, and an
    indented code block cannot interrupt one at all. GitHub therefore renders
    "LGTM\\n2. the token…" and "LGTM\\n    the token…" as ONE paragraph, so the
    line after the approval word joins it rather than being masked as a block
    that leaves ``LGTM`` bare. After a blank line the same lines really do
    start blocks, and an ordered item that continues an open ordered list is
    an item whatever its number."""
    assert not carries_approval("LGTM\n2. The token is logged at src/api.py:88.")
    assert not carries_approval("LGTM\n    The token is logged at src/api.py:88.")
    assert not carries_approval("LGTM\n\tThe token is logged at src/api.py:88.")
    # A ten-digit "marker" is prose under CommonMark's nine-digit limit.
    assert not carries_approval(
        "LGTM\n1234567890. The token is logged at src/api.py:88."
    )
    # The paragraph break makes them blocks again: the list is an enumeration
    # (masked as data) and the code block is code, so the verdict above each
    # stands as the author's own.
    assert carries_approval("LGTM\n\n2. The token is logged at src/api.py:88.")
    assert carries_approval("LGTM\n\n    The token is logged at src/api.py:88.")
    # `1.` may interrupt a paragraph — and item 2 of an open ordered list is an
    # item, so the existing enumeration corpus still masks as data.
    assert carries_approval("LGTM\n1. The token is logged at src/api.py:88.")
    assert not carries_approval("1. approved\n2. pending\n\nNeither is checked.")


def test_a_list_items_continuation_lines_stay_in_its_block() -> None:
    """PR #425 re-review of 6bba846, Medium: CommonMark keeps a continuation
    line inside the current item and the next marker a sibling in the SAME
    list, so the whole container is classified at once — an item's text is
    read with its continuations folded in. A non-indented line after a blank
    ends the list; an indented one after a blank is still the item's."""
    body = (
        "- approved\n  means approved by admin\n- pending\n  means awaiting review"
        "\n\nThe token is logged at src/api.py:88."
    )
    assert not carries_approval(body)
    assert not carries_approval(
        "- approved\n\n  means approved by admin\n- pending\n\n"
        "The token is logged at src/api.py:88."
    )
    # The paragraph after the (data) list is the author's own.
    assert carries_approval("- approved\n- pending\n\nLGTM")


def test_a_tables_bare_delimiter_row_is_part_of_the_table() -> None:
    """PR #425 re-review of 6bba846, Low: ``| verdict |\\n---\\n| LGTM |`` is a
    valid one-column GFM table. The delimiter is what recognised the table, so
    it is masked with the header rather than re-read as a thematic break that
    ends the table in front of its cell. A pipeless ``---`` under prose is still
    a setext underline, and the paragraph after one is still the author's."""
    assert not carries_approval("| verdict |\n---\n| LGTM |\n\nThe token is logged.")
    assert not carries_approval(
        "| verdict |\n-----\n| approved |\n\nThe token is logged."
    )
    assert carries_approval("approved\n---")
    assert carries_approval("This is not\n---\nLGTM")


def test_review_state_policy_follows_the_body_not_just_the_state() -> None:
    def review(state: str, body: str = "") -> PullRequestReview:
        return PullRequestReview(author="dave", body=body, state=state)

    # PR #425 review, correctness f-001: `APPROVED` was unconditionally
    # silent, so an approval that also ASKED was dropped by every consumer.
    assert review_is_actionable(review("APPROVED", "LGTM, but rename X"))
    assert not review_is_actionable(review("APPROVED"))
    # A dismissal has had its say, whatever it says.
    assert not review_is_actionable(review("DISMISSED", "rename X"))
    # Unchanged: CHANGES_REQUESTED always, every other state on content —
    # including a body that reads like an approval, which the watcher's
    # approval rule refuses for the same reason (PR #426 review, f-001).
    assert review_is_actionable(review("CHANGES_REQUESTED"))
    assert review_is_actionable(review("CHANGES_REQUESTED", "LGTM"))
    assert review_is_actionable(review("COMMENTED", "rename X"))
    assert not review_is_actionable(review("COMMENTED", "   "))
    assert review_is_actionable(review("QUEUED", "rename X"))
    # This rule does NOT read the body for approval prose (security f-004):
    # that is the watcher's dispatch question, decided once in
    # `dispositions()`, so the converge intake keeps feeding approval rows to
    # the S5a backstop on every stream alike.
    assert review_is_actionable(review("APPROVED", "LGTM"))
    assert review_is_actionable(review("COMMENTED", "LGTM"))
