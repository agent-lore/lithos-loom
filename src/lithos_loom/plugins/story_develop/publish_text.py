"""Rendering text loom did not author fit to publish in a PR description.

A PR description is **live markup** opened under the operator's `gh` identity:
GitHub honours closing keywords anywhere in it, notifies every ``@name``,
renders inline HTML and follows markdown links (inline *and* reference forms).
Several strings that reach one were written by a weaker party than loom — an
agent's handoff (a read-write bind mount is the agent's to fill), and, for a
story the github-issue watcher materialised, the **story's own description and
acceptance criteria**: those are the external issue body, mirrored into Lithos
by :mod:`~lithos_loom.subscriptions._github_issue_sync` and re-read live at
delivery time.

Two tools, used together where the text is a whole document:

* :func:`fence_untrusted` — bound the text and put it in a fenced code block
  **its own content cannot terminate** (the fence is measured against the
  longest backtick run inside it). Inside that block no GFM construct renders,
  which is the only defence that does not have to enumerate GFM.
* :func:`defang_markup` — rewrite the individual live constructs so the text
  still reads the same and binds nothing. The sole defence where a fence will
  not do (the one-line provenance bullet), and belt-and-braces inside every
  fence, since a rule that never stands alone is a rule nobody maintains.

So the neutralising lives here rather than beside any one caller: the plugin's
PR-body builder (:func:`~.pr_delivery.build_pr_body`) and the hand-delivery
facts (:mod:`lithos_loom.cli._deliver_facts`) share one rule, and a new
publisher gets it by importing it.
"""

from __future__ import annotations

import re

__all__ = [
    "CONTROL_CHARS_RE",
    "MAX_SECTION_CHARS",
    "defang_markup",
    "fence_untrusted",
]

# C0 / C1 *and* every Unicode character that reorders text or renders as
# nothing: bidi overrides + isolates (trojan source — GitHub warns about it in
# diffs), the zero-width / invisible formatters, the invisible fillers
# (U+00AD soft hyphen, the hangul fillers), the interlinear annotators, and the
# **tag block** (U+E0000–U+E007F — wholly invisible, the standard
# text-smuggling channel). A line must not be able to render differently from
# the text it carries — on a PR strangers read, on the `--dry-run` screen the
# operator decides on, and in the bytes a later LLM reviewer re-ingests: text
# the operator cannot see is an instruction channel only its author knows is
# there.
CONTROL_CHARS_RE = re.compile(
    "[\x00-\x08\x0b-\x1f\x7f-\x9f\u00ad\u034f\u115f\u1160\u17b4\u17b5"
    "\u180b-\u180f\u200b-\u200f\u2028\u2029\u202a-\u202e\u2060-\u2064"
    "\u2066-\u2069\u3164\ufe00-\ufe0f\ufeff\uffa0\ufff9-\ufffb"
    "\U000e0000-\U000e007f]"
)
# The closing keyword and the issue ref it binds to, with everything GitHub
# tolerates between them. `GH-123` is a closing ref exactly like `#123`
# (GitHub's documented `KEYWORD GH-ISSUE-NUMBER` form) and so is the issue's
# full url — `parse_issue_ref` in this package reads that shape as an issue
# reference, so it is not one to leave live here. Emphasis around the keyword
# (`**Closes** #1`), an entity-encoded non-breaking space, and a leading `[`
# (the reference-link shape `Closes [#1][r]`) are each a one-character bypass
# of a narrower pattern, so each is admitted.
_CLOSES_KEYWORD = r"close[sd]?|fix(?:e[sd])?|resolve[sd]?"
_CLOSES_SEPARATOR = r"\s*:\s*|(?:[\s\u00a0]|&nbsp;|&#0*160;|&#[xX]0*a0;)+"
_CLOSES_REF = r"#\d|GH-\d|[\w.-]+/[\w.-]+#\d|https?://\S+?/(?:issues|pull)/\d"
# Not `\b`: `_fixes_ #1` has no word boundary before the keyword (`_` is a word
# character), and emphasis must not be the bypass.
_CLOSES_RE = re.compile(
    rf"(?<![A-Za-z0-9])(?:{_CLOSES_KEYWORD})[*_~]*(?:{_CLOSES_SEPARATOR})"
    rf"(?=\[?(?:{_CLOSES_REF}))",
    re.IGNORECASE,
)
# No `\`` in the lookbehind: a preceding backtick does NOT make a mention
# inert (a single unmatched backtick opens no code span in GFM), so exempting
# one would hand the author a one-character bypass.
_MENTION_RE = re.compile(r"(?<!\w)@([A-Za-z0-9][-A-Za-z0-9/]*)")
# Backtick **and tilde** runs: `~~~` opens a GFM fence exactly like ```` ``` ````
# does, and an unclosed one swallows every section composed after it (every
# verdict the operator reads, and loom's own `Closes #N`, which then binds
# nothing). Defused to a run too short to open or close either fence.
_FENCE_RE = re.compile(r"`{3,}|~{3,}")
# GitHub renders inline HTML in a PR description, so `<img src=…>` is a live
# off-site request (a tracking beacon on every viewer) and `<!-- … -->` hides
# text from the reader while leaving it in the body. Only a `<` that starts a
# tag is escaped — `a < b` stays readable.
_HTML_OPEN_RE = re.compile(r"<(?=[A-Za-z/!?])")
# `[text](target)` is a live link, and so is `![alt](target)` — an off-site
# request needing no `<`. The lookahead crosses `]` (a NESTED label,
# `[outer [inner]](url)`, is the inline form too) but never `(`, so ordinary
# bracketed prose (`[Friction]`) is left alone; `&#91;` renders as `[`, so
# escaping one bracket too many costs nothing on screen.
_LINK_RE = re.compile(r"\[(?=[^(]*\]\()")
# The other half: `[label][ref]`, `[ref][]` and the bare `[ref]` shortcut are
# equally live, and their target lives in a **link-reference definition** on
# its own line — which renders as nothing at all, so the payload's plumbing is
# invisible to a reader skimming the rendered body. Break the definitions and
# every reference to them degrades to the literal text of its label, without
# touching a single bracket of ordinary prose.
_LINK_DEF_RE = re.compile(r"(?m)^([ \t]{0,3})\[(?=[^\]\n]*\]:)")
# Backtick runs decide how long a fence must be to be unterminable.
_BACKTICK_RUN_RE = re.compile(r"`+")

MAX_SECTION_CHARS = 8000
"""Cap on one untrusted PR-body section (CWE-770).

A GitHub issue body may be 65536 characters and GitHub rejects a PR body over
the same limit — so an unbounded section hands its author a repeatable way to
make `gh pr create` fail for every delivery of that story (a burned run and an
operator interrupt each time). Generous enough that a real story description
travels whole; the Lithos story always carries the untruncated text."""

_TRUNCATION_NOTE = "… (truncated — the whole text is on the Lithos story)"


def defang_markup(text: str) -> str:
    """Neutralise the markup GitHub treats as *live* in a PR description.

    This is the SOLE defence on the unfenced provenance bullet (the redacted
    stop reason, a single flattened line), and belt-and-braces inside every
    fenced block, so it must stand alone: GitHub closes issues named by a
    closing keyword anywhere in a description, notifies every ``@name``,
    renders inline HTML, and follows markdown links — a line saying
    ``Closes #1337 cc @org/sec <img src=//evil.example/p.png>`` would close an
    unrelated issue on merge, ping strangers and fire an off-site request for
    every viewer, all under the operator's identity.

    Each construct is rewritten so it still READS the same and binds nothing:
    the keyword keeps its word and gains an arrow between it and the ref, the
    mention's ``@`` becomes the entity that renders as one and notifies nobody,
    a tag-opening ``<`` and a link's ``[`` (inline, nested, and the definitions
    every reference form resolves through) are HTML-escaped, and backtick /
    tilde runs that would open or escape a fence are defused.
    **Nothing here leans on code spans**: quoting a mention in backticks only
    works while the backticks pair up, and the author of this text chooses how
    many of those it contains.

    Control bytes go first — C0/C1, the bidi formatters *and* the invisibles
    (:data:`CONTROL_CHARS_RE`) — because a construct that renders in a
    different order, or that the reader cannot see at all, defeats every
    rewrite below it.

    It does **not** make arbitrary markdown inert: GitHub autolinks a bare url,
    and tomorrow's GFM adds a construct this enumeration has never heard of.
    Text loom did not author goes through :func:`fence_untrusted` as well
    wherever it is a whole document rather than one line.
    """
    out = CONTROL_CHARS_RE.sub("", text)
    out = _CLOSES_RE.sub(lambda m: f"{m.group(0)}→ ", out)
    out = _MENTION_RE.sub(r"&#64;\1", out)
    out = _HTML_OPEN_RE.sub("&lt;", out)
    out = _LINK_DEF_RE.sub(r"\1&#91;", out)
    out = _LINK_RE.sub("&#91;", out)
    return _FENCE_RE.sub(lambda m: m.group(0)[0] * 2, out)


def fence_untrusted(text: str, *, limit: int = MAX_SECTION_CHARS) -> str:
    """*text* as a fenced block it cannot break out of, or ``""`` if empty.

    A regex pass over a whole markdown document can only ever enumerate the
    live constructs it knows (this one missed tilde fences and every reference
    link form on its first outing). A fenced code block needs no enumeration:
    nothing inside one renders, so nothing inside one binds. The fence is
    measured against the text itself — one backtick longer than the longest
    run it contains — so no line of it can close the block early and splice
    live markup back into a body loom composes around it (the sections after
    it carry the panel's verdicts, and loom's own ``Closes #N``).

    The content is still :func:`defang_markup`-ed inside the fence: the body is
    re-ingested by machines as well as read (an LLM reviewer on the PR, the
    external-review sweep), and one of them may not honour the fence.
    """
    body = defang_markup(text).strip()
    if not body:
        return ""
    if len(body) > limit:
        body = body[:limit].rstrip() + "\n" + _TRUNCATION_NOTE
    longest = max(
        (len(run.group(0)) for run in _BACKTICK_RUN_RE.finditer(body)), default=0
    )
    fence = "`" * max(3, longest + 1)
    return f"{fence}text\n{body}\n{fence}"
