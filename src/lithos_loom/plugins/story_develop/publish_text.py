"""Rendering text loom did not author fit to publish in a PR description.

A PR description is **live markup** opened under the operator's `gh` identity:
GitHub honours closing keywords anywhere in it, notifies every ``@name``,
renders inline HTML and follows markdown links. Several strings that reach one
were written by a weaker party than loom — an agent's handoff (a read-write
bind mount is the agent's to fill), and, for a story the github-issue watcher
materialised, the **story's own description and acceptance criteria**: those
are the external issue body, mirrored into Lithos by
:mod:`~lithos_loom.subscriptions._github_issue_sync` and re-read live at
delivery time.

So the neutralising lives here rather than beside any one caller: the plugin's
PR-body builder (:func:`~.pr_delivery.build_pr_body`) and the hand-delivery
facts (:mod:`lithos_loom.cli._deliver_facts`) share one rule, and a new
publisher gets it by importing it.
"""

from __future__ import annotations

import re

__all__ = ["CONTROL_CHARS_RE", "defang_markup"]

# C0 / C1 *and* the Unicode characters that reorder or hide text without
# being control codes: bidi overrides + isolates (trojan source — GitHub warns
# about it in diffs) and the zero-width / invisible formatters. A line must not
# be able to render differently from the text it carries — on a PR strangers
# read, and on the `--dry-run` screen the operator decides on.
CONTROL_CHARS_RE = re.compile(
    "[\x00-\x08\x0b-\x1f\x7f-\x9f"
    "\u200b-\u200f\u2028\u2029\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff]"
)
# `GH-123` is a closing ref exactly like `#123` (GitHub's documented
# `KEYWORD GH-ISSUE-NUMBER` form), so the lookahead admits it too.
_CLOSES_RE = re.compile(
    r"\b(close[sd]?|fix(e[sd])?|resolve[sd]?)(\s+|\s*:\s*)"
    r"(?=#\d|GH-\d|[\w.-]+/[\w.-]+#\d)",
    re.IGNORECASE,
)
# No `\`` in the lookbehind: a preceding backtick does NOT make a mention
# inert (a single unmatched backtick opens no code span in GFM), so exempting
# one would hand the author a one-character bypass.
_MENTION_RE = re.compile(r"(?<!\w)@([A-Za-z0-9][-A-Za-z0-9/]*)")
# Backtick runs would break out of a fence that quotes this text.
_FENCE_RE = re.compile(r"`{3,}")
# GitHub renders inline HTML in a PR description, so `<img src=…>` is a live
# off-site request (a tracking beacon on every viewer) and `<!-- … -->` hides
# text from the reader while leaving it in the body. Only a `<` that starts a
# tag is escaped — `a < b` stays readable.
_HTML_OPEN_RE = re.compile(r"<(?=[A-Za-z/!?])")
# `[text](target)` is a live link. Break the syntax at the bracket.
_LINK_RE = re.compile(r"\[(?=[^\]]*\]\()")


def defang_markup(text: str) -> str:
    """Neutralise the markup GitHub treats as *live* in a PR description.

    This is the SOLE defence on every unfenced section of a PR body loom did
    not author — the redacted stop reason, and the story's description +
    acceptance criteria (a mirrored story's are an outside issue reporter's
    text) — and belt-and-braces behind the fence that quotes the coder's
    handoff, so it must stand alone: GitHub closes issues named by a closing
    keyword anywhere in a description, notifies every ``@name``, renders inline
    HTML, and follows markdown links — a line saying
    ``Closes #1337 cc @org/sec <img src=//evil.example/p.png>`` would close an
    unrelated issue on merge, ping strangers and fire an off-site request for
    every viewer, all under the operator's identity.

    Each construct is rewritten so it still READS the same and binds nothing:
    the keyword keeps its word, the mention's ``@`` becomes the entity that
    renders as one and notifies nobody, a tag-opening ``<`` and a link's ``[``
    are HTML-escaped, and backtick runs that would escape a fence are defused.
    **Nothing here leans on code spans**: quoting a mention in backticks only
    works while the backticks pair up, and the author of this text chooses how
    many of those it contains.

    Control bytes go first — C0/C1 *and* the bidi / zero-width formatters
    (:data:`CONTROL_CHARS_RE`) — because a construct that renders in a
    different order than it was written defeats every rewrite below it.
    """
    out = CONTROL_CHARS_RE.sub("", text)
    out = _CLOSES_RE.sub(lambda m: f"{m.group(1)} → ", out)
    out = _MENTION_RE.sub(r"&#64;\1", out)
    out = _HTML_OPEN_RE.sub("&lt;", out)
    out = _LINK_RE.sub("&#91;", out)
    return _FENCE_RE.sub("``", out)
