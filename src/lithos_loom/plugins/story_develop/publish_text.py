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

Three tools. The first two are used together where the text is a whole
document:

* :func:`fence_untrusted` — bound the text and put it in a fenced code block
  **its own content cannot terminate** (the fence is measured against the
  longest backtick run inside it). Inside that block no GFM construct renders,
  which is the only defence that does not have to enumerate GFM.
* :func:`defang_markup` — rewrite the individual live constructs so the text
  still reads the same and binds nothing. The sole defence where a fence will
  not do (the one-line provenance bullet), and belt-and-braces inside every
  fence, since a rule that never stands alone is a rule nobody maintains.

…the third is for the one string that is neither:

* :func:`publish_title` — the PR **title**. Not markdown (GitHub renders it as
  plain text, so the body's entity rewrites would show up literally) and not
  inert either: with the repo default squash-merge message the title becomes
  the **commit subject**, and GitHub honours closing keywords and ``@name`` in
  commit messages on the default branch. A commit message is raw text, so no
  fence reaches it.

…and the fourth is for text loom merely QUOTES, in a sentence of its own:

* :func:`publish_line` — one bounded, invisible-free line: git's stderr on the
  operator's terminal, a crashed child's last output line inside a
  ``[Friction]`` finding. Same hazard, shortest shape.

So the neutralising lives here rather than beside any one caller: the plugin's
PR-body builder (:func:`~.pr_delivery.build_pr_body`) and the hand-delivery
facts (:mod:`lithos_loom.cli._deliver_facts`) share one rule, and a new
publisher gets it by importing it.
"""

from __future__ import annotations

import re

__all__ = [
    "CONTROL_CHARS_RE",
    "MAX_EXCERPT_CHARS",
    "MAX_SECTION_CHARS",
    "MAX_TITLE_CHARS",
    "MIN_SECTION_CHARS",
    "defang_markup",
    "fence_untrusted",
    "flatten_line",
    "publish_line",
    "publish_title",
]

# The characters a published line must not be able to carry: the C0 / C1
# controls (tab and newline excepted — the only two a PR body renders), the
# Unicode line / paragraph separators, every Default_Ignorable_Code_Point
# (the bidi formatters — trojan source, which GitHub warns about in diffs —
# the zero-width and invisible formatters, the invisible fillers, the
# variation selectors and the **tag block**, the standard text-smuggling
# channel), and the interlinear annotators. A line must not be able to render
# differently from the text it carries — on a PR strangers read, on the
# `--dry-run` screen the operator decides on, and in the bytes a later LLM
# reviewer re-ingests: text the operator cannot see is an instruction channel
# only its author knows is there.
#
# A table of named code-point blocks rather than a handful of wide literal
# ranges, for the two things a wide range cost this class. It hid what it
# took: U+000B-U+001F swept the whitespace controls (VT, FF, CR) in with the
# rest without ever saying so — CodeQL's "overly permissive range"
# (code-scanning/10) is exactly that complaint. And it could not be checked
# against the property it tracks, so it had drifted from it: U+061C, U+2065,
# the deprecated U+206A-U+206F, U+FFF0-U+FFF8 and everything past U+E007F were
# all missing, each of them surviving into a published body — and since the
# budget below is counted AFTER the strip, each was also a character an
# external author could spend a whole section's allowance on.
_STRIPPED_RANGES: tuple[tuple[int, int], ...] = (
    (0x0000, 0x0008),  # NUL..BS — the C0 controls before TAB
    (0x000B, 0x000C),  # VT, FF
    (0x000D, 0x000D),  # CR
    (0x000E, 0x001F),  # SO..US — the C0 controls after LF
    (0x007F, 0x009F),  # DEL and the C1 controls
    (0x2028, 0x2029),  # LINE / PARAGRAPH SEPARATOR (Zl / Zp)
    # Default_Ignorable_Code_Point (DerivedCoreProperties.txt), in full.
    (0x00AD, 0x00AD),  # SOFT HYPHEN
    (0x034F, 0x034F),  # COMBINING GRAPHEME JOINER
    (0x061C, 0x061C),  # ARABIC LETTER MARK — a bidi formatter
    (0x115F, 0x1160),  # HANGUL CHOSEONG / JUNGSEONG FILLER
    (0x17B4, 0x17B5),  # KHMER VOWEL INHERENT AQ / AA
    (0x180B, 0x180F),  # MONGOLIAN free variation selectors + vowel separator
    (0x200B, 0x200F),  # ZWSP..RLM
    (0x202A, 0x202E),  # the bidi embeddings and overrides
    (0x2060, 0x206F),  # word joiner, the invisible operators, U+2065, the
    #                    bidi isolates, the deprecated format characters
    (0x3164, 0x3164),  # HANGUL FILLER
    (0xFE00, 0xFE0F),  # VARIATION SELECTOR-1..16
    (0xFEFF, 0xFEFF),  # ZERO WIDTH NO-BREAK SPACE (the BOM)
    (0xFFA0, 0xFFA0),  # HALFWIDTH HANGUL FILLER
    (0xFFF0, 0xFFF8),  # unassigned, reserved default-ignorable
    (0x1BCA0, 0x1BCA3),  # SHORTHAND FORMAT controls
    (0x1D173, 0x1D17A),  # MUSICAL SYMBOL BEGIN/END formatters
    (0xE0000, 0xE0FFF),  # the tag block + VARIATION SELECTOR-17..256
    # Not Default_Ignorable (Unicode excludes these three), but they hide the
    # text they annotate from the reader while leaving it in the body.
    (0xFFF9, 0xFFFB),  # INTERLINEAR ANNOTATION ANCHOR / SEPARATOR / TERMINATOR
)
CONTROL_CHARS_RE = re.compile(
    "["
    + "".join(
        re.escape(chr(first))
        if first == last
        else f"{re.escape(chr(first))}-{re.escape(chr(last))}"
        for first, last in _STRIPPED_RANGES
    )
    + "]"
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

MAX_TITLE_CHARS = 90
"""Cap on a published PR title — the length both delivery paths already used."""

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
# what this pipeline drops or collapses ANYWAY comes out BEFORE the slice is
# taken (:func:`_squeeze_for_budget`) — otherwise the cheapest way to suppress
# a section is to pad it with something that never reaches the page: the
# invisibles above (security/f-009), or a fence run `_FENCE_RE` shortens to
# two characters.
#
# Whitespace is NOT such a thing, and nothing here touches it. The section is
# published inside a code block, which preserves every space, tab and blank
# line in it — so a deep indent, an exact-output fixture, an ASCII diagram or
# a whitespace-sensitive example is the author's CONTENT, and capping runs of
# it rewrote technical requirements while still publishing them as whole.
# Padding with whitespace therefore buys its author nothing but a section that
# is honestly truncated, with the `(truncated …)` marker on it to say so.


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


def publish_title(text: str, *, limit: int = MAX_TITLE_CHARS) -> str:
    """*text*'s first line as a PR title that reads the same and binds nothing.

    The story's title is the external issue's title for a story the
    github-issue watcher materialised (the mirror creates the task with
    ``title=issue.title``), so it is the same weaker party's text as the
    description — and it travels a channel the body's fence cannot reach.
    With the repo default squash-merge message (``COMMIT_OR_PR_TITLE``) a
    multi-commit PR takes its commit **subject** from the PR title, and loom's
    PRs are deliberately multi-commit (:func:`~.pr_delivery.build_pr_body`
    says so in the body it writes). GitHub honours closing keywords in commit
    messages on the default branch, so an issue titled
    ``Closes #1 parser crashes on empty input`` would close issue #1 under the
    operator's account on merge, with nothing in the PR body to show why —
    the same exploit as the unfenced description, on the other half of the
    same boundary.

    Neutralised with **separators, not entities**: a title is plain text, not
    markdown, so `defang_markup`'s ``&#64;`` / ``&lt;`` would be published
    literally instead of rendering as the character they stand for. The
    keyword keeps its word and gains the same arrow the body gives it, a
    mention keeps its name behind a space, and the invisibles come out
    (:data:`CONTROL_CHARS_RE`) — they would otherwise reorder the PR list, the
    notification email and the commit subject, none of which the
    ``--dry-run`` screen's own sanitising covers.

    Unlike a fenced section, a title has no whitespace to preserve: it is one
    plain-text line that GitHub trims itself, and the first line is all of it.

    **The first line, and never a later one.** *text* is usually the whole
    task text — ``f"{title}\n\n{body}"`` — so the line is chosen before the
    text is trimmed, not after. Trimming first let a story whose issue title
    is empty (or nothing but default-ignorable code points, which come out
    here) fall through to the first line of the issue **body**, publishing as
    the PR title a line the reporter wrote as prose and nobody chose as a
    subject.

    ``""`` when *text* holds no first line, which is the caller's cue to fall
    back — **both** delivery paths take it, to the branch name.

    *limit* is a character cap and must be at least one: a non-positive value
    is a caller's bug, not a cap (Python's negative slicing would read it as
    "keep nearly everything", which cannot satisfy a cap at all), so it
    raises — the same contract :func:`fence_untrusted` holds its own limit to.
    """
    if limit < 1:
        raise ValueError(
            f"publish_title limit must be at least 1 character, got {limit}"
        )
    # Split before trimming, and on `\n` alone: `CONTROL_CHARS_RE` has already
    # taken every other character `str.splitlines` would break on (VT, FF, CR,
    # U+0085, U+2028, U+2029), so this is exactly the line the author wrote.
    first = CONTROL_CHARS_RE.sub("", text).split("\n", 1)[0].strip()
    defanged = _CLOSES_RE.sub(lambda m: f"{m.group(0)}\u2192 ", first)
    defanged = _MENTION_RE.sub(r"@ \1", defanged)
    # Cut last: the rewrites only ever grow the line, and dropping a suffix
    # cannot rejoin a keyword with the ref the arrow was put between.
    return defanged[:limit].strip()


# A published excerpt of text loom did not author: one line, bounded. 300 is the
# cap `remediation_refunds.refund_infra_failed` already applies to a child's
# `message` — the same sink (a Lithos finding) and the same reason.
MAX_EXCERPT_CHARS = 300
_ELLIPSIS = "…"


def publish_line(text: str, *, limit: int = MAX_EXCERPT_CHARS) -> str:
    """*text* as ONE bounded line an operator can be shown safely.

    The fourth tool, for the shortest shape of all: a line loom quotes from
    somewhere else — git's stderr (the origin host's and the local ssh
    client's text: an ssh banner, a ``remote:`` line), a crashed child's last
    output line — published into an operator's terminal and into a Lithos
    finding they read to DECIDE. Neither sink bounds or neutralises what it is
    given, so this does both: the invisibles come out
    (:data:`CONTROL_CHARS_RE` — an ANSI escape can forge or erase a line on
    the operator's own tty, a bidi override can reorder it; CWE-117 /
    CWE-150), every remaining whitespace run collapses to a single space so
    the excerpt cannot break out of the sentence it sits in, and the result is
    capped at *limit* characters (CWE-770).

    The cut keeps the **head**: a quoted failure identifies itself first
    (``fatal: …``, ``RuntimeError: …``) and an operator who reads only the
    start still knows what happened.
    """
    if limit < 1:
        raise ValueError(
            f"publish_line limit must be at least 1 character, got {limit}"
        )
    flat = flatten_line(text)
    if len(flat) <= limit:
        return flat
    return flat[: limit - len(_ELLIPSIS)].rstrip() + _ELLIPSIS


def flatten_line(text: str) -> str:
    """*text* as one line with the invisibles out — :func:`publish_line`
    without the cut.

    For the caller that must MEASURE before it chooses what to keep: the
    watcher composes a finding's excerpt out of several physical lines and can
    only decide how many fit once they are flattened (#431 review f-007).
    """
    return " ".join(CONTROL_CHARS_RE.sub("", text).split())


def _squeeze_for_budget(text: str) -> str:
    """*text* with everything the published section cannot carry taken out.

    Not a defence in itself — both rules here are ones the pipeline applies
    anyway: the invisibles are stripped by :func:`defang_markup`, and the
    fence runs are collapsed by it, so neither reaches a reader whatever the
    budget does. Running them FIRST is what stops a weaker party from spending
    a section's whole budget on characters that reach no reader — the
    difference between a `## What` that carries the story and one that is only
    a truncation marker.

    Nothing else is normalised. Whitespace survives a fenced block, so it is
    the author's content and not this function's to collapse: text that
    exceeds the budget is cut and SAYS so instead of being quietly squeezed
    into it.
    """
    out = CONTROL_CHARS_RE.sub("", text)
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
    tag characters (security/f-009), or a fence run — and what gets published
    is nothing but the truncation marker. Both passes are fixed-width
    character classes, linear in the input, which is what makes them safe to
    run before the bound rather than behind it.

    Whitespace is published exactly as written — **at the section's edges as
    well as between its words**. A code block carries every space, tab and
    blank line, so the story's indentation is its content: acceptance criteria
    that OPEN on an indented exact-output line lose their meaning if the first
    line is un-indented, and a final line's trailing spaces are as significant
    as any other line's. Trimming them was the last place this function still
    normalised silently. A reporter padding with whitespace spends a real
    budget and gets a visibly truncated section, which is the honest outcome,
    not a silently reflowed one. A section that is **nothing but** whitespace
    carries nothing, and is the empty section it already was.
    """
    if limit < MIN_SECTION_CHARS:
        raise ValueError(
            f"fence_untrusted limit must be at least {MIN_SECTION_CHARS} "
            f"characters (the truncation marker's room), got {limit}"
        )
    carried = _squeeze_for_budget(text)
    clipped = carried[: limit * _DEFANG_INPUT_SLACK]
    body = defang_markup(clipped)
    if len(clipped) < len(carried) or len(body) > limit:
        # The marker fits INSIDE the cap, and lands before the emptiness check
        # below: a section whose text all sat past the input slice would read
        # as "the story said nothing" rather than "there was more of this".
        # `_squeeze_for_budget` is what keeps that from being reachable by
        # padding; this ordering is the backstop if it ever stops being.
        # The cut itself trims nothing either — it is announced, so it has no
        # need to also tidy the edge it left behind.
        keep = limit - len(_TRUNCATION_NOTE) - 1
        body = f"{body[:keep]}\n{_TRUNCATION_NOTE}"
    # Emptiness is a question about the CONTENT, not a reason to rewrite it:
    # `body.strip()` decides whether there is a section at all, and `body`
    # itself is what gets published if there is.
    if not body.strip():
        return ""
    longest = max(
        (len(run.group(0)) for run in _BACKTICK_RUN_RE.finditer(body)), default=0
    )
    fence = "`" * max(3, longest + 1)
    return f"{fence}text\n{body}\n{fence}"
