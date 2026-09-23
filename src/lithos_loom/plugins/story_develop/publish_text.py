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
    "MIN_SECTION_CHARS",
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
# Both link families are broken at the ONE literal sequence each of them
# cannot be written without, rather than at the bracket that opens a label
# whose contents the author chooses. Enumerating labels is what let the nested
# `[outer [inner]](…)`, the backslash-escaped `[a\]b]:` and the
# container-prefixed `> [a]:` through in earlier rounds, and the widest label
# pattern was quadratic as well (security/f-007) — a fixed two-character match
# is linear and has nothing left to enumerate.
#
# `[text](target)` and `![alt](target)` (an off-site request needing no `<`)
# both need `](`: CommonMark admits no space there. Escaping the `(` breaks
# the link and leaves the label's brackets readable.
_INLINE_LINK_RE = re.compile(r"\]\(")
# `[label][ref]`, `[ref][]` and the bare `[ref]` shortcut all resolve through a
# **link-reference definition**, which renders as nothing at all — so the
# payload's plumbing is invisible to a reader skimming the rendered body. A
# definition needs `]:` after its label, whatever that label holds and whatever
# container block it sits in (definitions are collected document-globally), and
# the colon is read at block level before any entity is decoded. Break it and
# every reference to it degrades to the literal text of its label.
_LINK_DEF_RE = re.compile(r"\]:")
# Backtick runs decide how long a fence must be to be unterminable.
_BACKTICK_RUN_RE = re.compile(r"`+")

MAX_SECTION_CHARS = 8000
"""Cap on one untrusted PR-body section (CWE-770).

Counts the **quoted content**: the text plus the truncation marker, never more
(the fence itself is loom's own two lines, and is not the author's to spend).

A GitHub issue body may be 65536 characters and GitHub rejects a PR body over
the same limit — so an unbounded section hands its author a repeatable way to
make `gh pr create` fail for every delivery of that story (a burned run and an
operator interrupt each time). Generous enough that a real story description
travels whole; the Lithos story always carries the untruncated text."""

_TRUNCATION_NOTE = "… (truncated — the whole text is on the Lithos story)"

MIN_SECTION_CHARS = len(_TRUNCATION_NOTE) + 2
"""The smallest cap :func:`fence_untrusted` can honour.

A truncated section is the marker, a newline and whatever text fits before
them, so a cap under this leaves the marker nowhere to go — and quietly
publishing the marker ALONE (or, for a non-positive cap, letting Python's
negative slicing publish nearly the whole input) would break the promise the
cap is made of. Out-of-domain is a caller's bug, so it raises."""

# How much of the input the rewrites are allowed to see, as a multiple of the
# published bound. The rewrites only ever GROW text, so text past this slice
# could not have fitted under the bound anyway — and `redact_for_publication`
# already makes the same trade for the same reason: bound what the patterns
# ever see, rather than hand an external author the size of the host's regex
# work (security/f-007).
_DEFANG_INPUT_SLACK = 2

# The budget is spent on what the published section can actually carry, so
# everything this pipeline was going to drop or collapse ANYWAY is dropped or
# collapsed BEFORE the slice is taken — otherwise the cheapest way to suppress
# a section is to pad it with something that never reaches the page: the
# invisibles (security/f-009), a fence run `_FENCE_RE` shortens to two
# characters, or runaway whitespace (security/f-010 — a run of spaces, a line
# that is only spaces, a wall of blank lines; markdown collapses them where the
# reporter's own issue is rendered, and inside the fence they are blank).
# The caps are far wider than any layout a story description carries: deeper
# than any indentation, and more blank lines than a paragraph break.
_MAX_SPACE_RUN = 40
_MAX_NEWLINE_RUN = 3
_SPACE_RUN_RE = re.compile(rf"[^\S\n]{{{_MAX_SPACE_RUN},}}")
# AFTER the run collapse, never before. A whitespace run that never reaches a
# line end (`" " * n + "x"`) makes this pattern quadratic — it rescans the rest
# of the run from every position in it, 0.25 s at 10 KiB and 4× per doubling —
# and the author of this text chooses n. With runs already capped at
# `_MAX_SPACE_RUN` it scans at most that many characters per position.
_LINE_TRAILING_RE = re.compile(r"(?m)[^\S\n]+$")
_NEWLINE_RUN_RE = re.compile(rf"\n{{{_MAX_NEWLINE_RUN},}}")


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
    a tag-opening ``<`` is HTML-escaped, a link's ``](`` and a reference
    definition's ``]:`` — the one sequence each family cannot be written
    without, so neither a nested label, a backslash escape inside one nor a
    blockquote / list prefix is a way past — lose their punctuation to the
    entity that renders as it, and backtick / tilde runs that would open or
    escape a fence are defused.
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
    out = _INLINE_LINK_RE.sub("]&#40;", out)
    out = _LINK_DEF_RE.sub("]&#58;", out)
    return _FENCE_RE.sub(lambda m: m.group(0)[0] * 2, out)


def _squeeze_for_budget(text: str) -> str:
    """*text* with everything the published section cannot carry taken out.

    Not a defence in itself — every rule here is one the pipeline applies
    anyway (the invisibles are stripped by :func:`defang_markup`, the fence
    runs are collapsed by it, and whitespace runs are blank wherever the text
    is read). Running them FIRST is what stops a weaker party from spending a
    section's whole budget on characters that reach no reader: the collapse is
    the difference between a `## What` that carries the story and one that is
    only a truncation marker.

    Order matters twice: the run collapse precedes the trailing-whitespace
    strip (which would otherwise rescan a long run from every position), and
    both precede the slice they exist to protect.
    """
    out = CONTROL_CHARS_RE.sub("", text)
    out = _FENCE_RE.sub(lambda m: m.group(0)[0] * 2, out)
    out = _SPACE_RUN_RE.sub(lambda m: m.group(0)[0] * _MAX_SPACE_RUN, out)
    out = _LINE_TRAILING_RE.sub("", out)
    return _NEWLINE_RUN_RE.sub("\n" * _MAX_NEWLINE_RUN, out)


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

    *limit* bounds the **quoted content** — the text plus the truncation marker
    — and is applied **twice**: once to the input, before a single pattern runs
    (the author of this text picks its length, so it also picks how much work
    the host does on it), and once to the result. The marker's room is reserved
    inside the cap rather than added on top of it, so the published section is
    never longer than the caller asked for; the fence's own two lines are
    loom's markup and sit outside the count. Either cut says so in the body — a
    section that lost text never reads as the whole story. A cap too small to
    hold the marker, or a non-positive one (which Python's negative slicing
    would read as "keep nearly everything"), is a caller's bug: ``ValueError``.

    What the published section cannot carry is dropped **before** the input
    slice (:func:`_squeeze_for_budget`), so the budget is spent on what a
    reader will actually see. Otherwise the cheapest way to suppress a section
    is to pad it with something that never reaches the page — zero-widths and
    tag characters (security/f-009), a fence run, sixteen thousand spaces or a
    wall of blank lines (security/f-010) — and what gets published is nothing
    but the truncation marker. Those passes are fixed-width classes and
    capped-run matches, linear in the input, which is what makes them safe to
    run before the bound rather than behind it.
    """
    if limit < MIN_SECTION_CHARS:
        raise ValueError(
            f"fence_untrusted limit must be at least {MIN_SECTION_CHARS} "
            f"characters (the truncation marker's room), got {limit}"
        )
    carried = _squeeze_for_budget(text)
    clipped = carried[: limit * _DEFANG_INPUT_SLACK]
    body = defang_markup(clipped).strip()
    if len(clipped) < len(carried) or len(body) > limit:
        # The marker fits INSIDE the cap, and lands before the emptiness check
        # below: a section whose text all sat past the input slice would read
        # as "the story said nothing" rather than "there was more of this".
        # `_squeeze_for_budget` is what keeps that from being reachable by
        # padding; this ordering is the backstop if it ever stops being.
        keep = limit - len(_TRUNCATION_NOTE) - 1
        body = f"{body[:keep].rstrip()}\n{_TRUNCATION_NOTE}".lstrip()
    if not body:
        return ""
    longest = max(
        (len(run.group(0)) for run in _BACKTICK_RUN_RE.finditer(body)), default=0
    )
    fence = "`" * max(3, longest + 1)
    return f"{fence}text\n{body}\n{fence}"
