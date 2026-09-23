"""The approval vocabulary: "is this row a verdict, and does it ask for anything?"

Two pure classifiers over a review body, and the hand-written vocabulary they
read. They are **policy**, not payload — which is why they live here rather
than beside the dataclasses in :mod:`lithos_loom.github_models` (which
re-exports both names, as :mod:`lithos_loom.github_client` re-exports the
dataclasses, so every existing importer keeps working):

- :func:`is_approval_text` — the STRONG, end-to-end rule: every unit of the
  body is an approval or a courtesy, so the row asks for nothing. The
  watcher's ingestion reads it to classify a row ``approval`` and dispatch
  nothing (827cedf8).
- :func:`carries_approval` — the WEAK floor: SOME unit is an approval. It
  guards S5a's ``NOTHING_TO_REMEDIATE`` verdict, which a model writes about
  third-party prose, so it must be checkable in code
  (``external_triage.approval_eligible_ids``).

The two differ on purpose, and each fails towards the recoverable direction
for its own caller — the long comments below record which, and why each rule
is the shape it is.
"""

from __future__ import annotations

import re

__all__ = [
    "carries_approval",
    "is_approval_text",
]


# ── "an approval is not a finding" (the sibling of 8cfa3184) ───────────
#
# A review whose entire content is an approval — "**No findings.** … Ready to
# merge." — asks for nothing. Remediating it costs a coder turn and a full
# panel pass to learn what the comment already said (remediation run 827cedf8
# on lens #100 spent exactly that, and deferred the PR's merge-gate behind
# it). The classifier below is the cheap half of the answer: it recognises
# approval-only prose BEFORE any paid turn, so the sweep reports the activity
# as an ``approval`` and dispatches nothing. The S5a triage's
# ``NOTHING_TO_REMEDIATE`` verdict is the model-driven backstop for the mixed
# prose this cannot see.
#
# It fails towards ACTIONABLE, the recoverable direction: prose read as a
# finding costs at most one remediation round (the loop's own gate + panel
# still judge the result), while an ask read as an approval is silently
# dropped. Hence a **unit** — one sentence, list item or line — must match a
# known approval phrase END TO END: "LGTM, but rename X" is a single unit
# that matches nothing, so it stays a finding.
#
# The body is author-controlled, so it is CANONICALISED, never deleted (PR
# #425 review, security f-001): the first cut of this dropped every non-ASCII
# codepoint before matching, so "LGTM. 请重命名变量" — an approval plus a
# rename request — read as a bare approval, and so did an approval emoji
# followed by "this leaks credentials" in any non-Latin script. Typographic
# punctuation is folded to its ASCII equivalent and zero-width marks are
# dropped; a unit that still holds a non-ASCII character after that is prose
# this vocabulary cannot read, which is ACTIONABLE by definition.

# Emoji / shortcodes that ARE the approval ("👍" alone is a verdict).
_APPROVAL_EMOJI_RE = re.compile(
    r"👍|✅|🚀|🎉|:\+1:|:shipit:|:rocket:|:tada:|:white_check_mark:"
)
# Typographic punctuation folded to ASCII before matching — an author writing
# "I’m happy with this" or Dave's "**No findings.** … Ready to merge." must
# read the same as the ASCII spelling (the ellipsis folds to a full stop, so
# it still ends a unit).
_CANONICAL = str.maketrans(
    {
        "‘": "'",
        "’": "'",
        "“": '"',
        "”": '"',
        "…": ".",
        " ": " ",
        " ": " ",
        " ": " ",
        "​": "",
        "‌": "",
        "‍": "",
        "﻿": "",
    }
)
# Sentence / clause boundaries. Splitting hard is safe: an ask can only end up
# in MORE units, and every unit must match on its own.
#
# ``?`` is deliberately NOT a boundary (PR #425 re-review, correctness
# f-002): splitting on it threw the question mark away and "Ready to merge?"
# — a reviewer ASKING whether to merge — matched the approval phrase behind
# it. Left inside its unit, the anchored match fails and the whole body is
# actionable, which is what a question is.
_UNIT_SPLIT_RE = re.compile(r"[\n.!;,:]|—|–|\s-\s")
# The WEAK scan's splitter (``carries_approval``): the same boundaries MINUS
# ``:``. The divergence is principled, not drift — the two rules want opposite
# granularity (PR #426 round-4 review, security f-004). A finer split is
# CONSERVATIVE end to end, where every unit must pass, and PERMISSIVE under an
# any-unit floor, where one unit passing is enough: splitting on ``:`` runs
# before ``_DECORATION_RE`` strips shortcodes, so "Field: `approved`. It is
# never validated" and ":lgtm: the token is logged" each handed the floor a
# bare approval word their authors never wrote as a verdict. Left unsplit, a
# non-allowlisted shortcode falls to the decoration strip as intended and a
# label line stays one unit that matches nothing.
#
# For the same reason a ``.`` INSIDE an identifier is not a boundary here (PR
# #426 round-5 review, security f-002): a dotted identifier written in ordinary
# prose without backticks — "The flag task.approved, so nothing validates it" —
# split into the bare unit ``approved`` and handed the floor an approval word
# its author never wrote as a verdict. That is the un-backticked spelling of
# the label line above. The identifier domain is ``[A-Za-z0-9_]``, the
# underscore included: it is a ``_DECORATION_RE`` character, so with a
# letters-and-digits-only rule ``task._approved`` split at the dot and the
# strip then deleted the ``_``, manufacturing the very bare ``approved`` this
# rule exists to prevent (round-5 panel correctness f-002). A sentence-ending
# ``.`` (followed by a space, a ``*``, a newline or nothing) still splits, so
# "**No findings.** … Ready to merge." reads as before.
_WEAK_UNIT_SPLIT_RE = re.compile(
    r"(?:(?<![A-Za-z0-9_])\.|\.(?![A-Za-z0-9_]))|[\n!;,]|—|–|\s-\s"
)
# Markdown decoration and emoji shortcodes are dropped before matching
# ("**No findings**" → "no findings"). ASCII only — see the note above.
_DECORATION_RE = re.compile(r"""[*_`~#>\[\]()"'|]|:[a-z0-9_+-]+:""")

# A qualifier / trailer an approval may carry without becoming an ask
# ("LGTM overall", "no findings from me"). Factored out of the phrases so
# every phrase accepts them uniformly.
_QUALIFIER = r"(?:overall|otherwise|generally|in\ general|so\ far)"
_TRAILER = (
    r"(?:overall|here|now|again|found|to\ me|for\ me|from\ me|by\ me"
    r"|from\ my\ side|on\ my\ end|in\ general|so\ far)"
)
_APPROVAL_PHRASE = r"""(?:
        lgtm
      | (?:this\ )?looks?\ good
      | (?:this\ )?(?:is\ )?(?:all\ )?good(?:\ to\ (?:go|merge))?
      | (?:it\ |this\ )?all\ looks\ good
      | approved?|approving|approval
      | ship\ it
      | \+1
      | all\ clear
      | no\ (?:findings?|issues?|concerns?|comments?|blockers?|objections?
            |problems?|notes?|nits?)
      | no\ (?:further|other|more)\ (?:findings?|issues?|concerns?|comments?
            |questions?|notes?)
      | nothing\ (?:to\ (?:flag|add|fix|change|remediate|do|address|report)
            |further|blocking|else)
      | (?:ready|good|ok|okay|fine|safe)\ to\ merge
      | (?:merge|merging)\ (?:away|it|this)
      | (?:im\ |i\ am\ )?happy\ (?:with\ (?:this|it)|to\ merge)
      | (?:this\ )?(?:is\ )?fine(?:\ (?:by|with)\ me)?
    )"""
_APPROVAL_UNIT_RE = re.compile(
    rf"^(?:{_QUALIFIER}\ )?{_APPROVAL_PHRASE}(?:\ {_TRAILER})*$",
    re.IGNORECASE | re.VERBOSE,
)
# Neutral courtesy: allowed ALONGSIDE an approval, never an approval by itself
# ("Thanks!" on its own is not a verdict).
_COURTESY_UNIT_RE = re.compile(
    r"""^(?:
        thanks?(?:\ (?:again|all|both))?
      | thank\ you
      | cheers
      | (?:very\ )?nice(?:\ (?:work|catch|one))?
      | (?:great|good)\ (?:work|stuff|job)
      | great|awesome|perfect|excellent
    )$""",
    re.IGNORECASE | re.VERBOSE,
)


def _units(body: str, split: re.Pattern[str] = _UNIT_SPLIT_RE) -> list[str]:
    """*body* as canonicalised units (sentence / list item / line), blanks
    dropped — the one splitting rule both classifiers below read, at the
    granularity *split* asks for.

    Splitting only: the approval-emoji rewrite is NOT done here. It belongs to
    the whole-body reading (:func:`is_approval_text`), where every other unit
    must still be approval or courtesy, so a decorative emoji cannot carry a
    body on its own (PR #426 re-review, security f-003).
    """
    text = body.translate(_CANONICAL)
    units = []
    for raw in split.split(text):
        unit = " ".join(_DECORATION_RE.sub("", raw).split()).strip("- ")
        if unit:
            units.append(unit)
    return units


# ── Non-authorial text: what the WEAK scan must not read ───────────────
#
# The floor below asks one question — "did the author write an approving
# word?" — and ANY unit passing answers it, so it must read only text the
# author wrote AS A VERDICT, IN THEIR OWN VOICE. Markdown has contexts that
# are visibly not that (PR #426 round-5 review, f-001): a block quote is
# someone else's comment, a code span is data (a field value, a log line, a
# bot's output), a strike-through is retracted, an HTML comment is invisible on
# the rendered PR, and a list under a lead-in is an enumeration.
# ``_DECORATION_RE`` stripped exactly those markers before matching, so
# "> LGTM" quoted above a defect, a fence holding only ``LGTM``,
# "~~Approved~~" and "The `state` field accepts:\n- approved" each handed the
# floor an approval word nobody asserted — and a mistaken (or talked-into)
# ``NOTHING_TO_REMEDIATE`` verdict could then consume the real finding at
# round 0, after its high-water mark had advanced and with no thread answered.
#
# The same question decides how the remaining text is SPLIT: GitHub renders
# consecutive lines as one paragraph, so a hard wrap is not a sentence end
# (round-5 panel security f-003) — :func:`_join_soft_wraps` reads the paragraph
# the author wrote and a human sees.
#
# Each such span is MASKED to a bare ``x``: a word that matches no phrase and
# introduces no unit boundary. Deleting could fuse the two sides into a word
# ("LG`x`TM"), and masking with punctuation would manufacture a boundary that
# splits a negation off its approval word — the two failure modes the emoji
# mask already learned (see :func:`carries_approval`). An unclosed span masks
# the rest of the body, which is the conservative direction here: under an
# any-unit floor, masking can only make a row LESS eligible.
_MASK = " x "
# An HTML comment is not merely "not my verdict": it is INVISIBLE in GitHub's
# rendered view (PR #426 round-5 review, security f-001), and templates and bot
# metadata routinely carry one. `<!-- LGTM -->` used to leave a bare approval
# unit behind (``!`` is a boundary, ``>`` is decoration, ``--`` is stripped),
# so nothing a human can see on the PR approved anything.
_HTML_COMMENT_RE = re.compile(r"<!--[\s\S]*?(?:-->|\Z)")
_BACKTICK_RUN_RE = re.compile(r"`+")  # inline code spans
_TILDE_RUN_RE = re.compile(r"~~?")  # GFM strike-through
# A fenced code block is a LINE structure, not a delimited span (round-5 panel
# correctness f-002): its closing fence must be a line holding nothing but a
# run of the same character, at least as long as the opener. Matching "the next
# run at least as long" anywhere let a longer run WITH trailing text close the
# block early — "```` / ````` example / LGTM / ````" is one code block whose
# ``LGTM`` the early close handed to the floor. The info string of a backtick
# fence may not itself contain a backtick, which is what keeps a one-line
# "```LGTM``` the token is logged" an inline span rather than a fence.
_FENCE_OPEN_RE = re.compile(r"^ {0,3}(?P<fence>`{3,}|~{3,})(?P<info>[^\n]*)$")
_LIST_MARKER = r"(?:[-*+]|\d+[.)])"
# A quoted line, with or without the bullet it hangs off ("- > LGTM"); an
# indented code block — the fence's other spelling — is below.
_QUOTE_LINE_RE = re.compile(rf"^[ \t]*(?:{_LIST_MARKER}[ \t]+)?>[^\n]*")
_LIST_ITEM_RE = re.compile(rf"^[ \t]*{_LIST_MARKER}[ \t]+(?P<text>\S.*)$")
# A table row: the TABLE spelling of the enumeration ``_mask_data_lists`` masks
# for bullets (round-6 panel correctness f-002 / security f-004). A row whose
# only populated cell holds an approval word reduced to a bare verdict once
# ``_DECORATION_RE`` stripped the pipes — "| verdict |\n| --- |\n| LGTM |" above
# a defect satisfied the floor. Any line carrying a ``|`` is masked, not just
# one that starts with it: GFM's leading and trailing pipes are optional
# ("status | note" / "approved |" are rows too), and a table is an enumeration
# by construction — nobody writes a verdict as a table, so there is nothing to
# lose in the masking direction. Runs after the code-span masks, so a pipe
# inside `code` is already gone.
_TABLE_ROW_RE = re.compile(r"^[^\n]*\|[^\n]*$", re.MULTILINE)
_INDENTED_LINE_RE = re.compile(r"^(?: {4,}|\t)[^\n]*", re.MULTILINE)
# Lines that start a block of their own, so the line break before/after them is
# a real boundary rather than a soft wrap (see :func:`_join_soft_wraps`) and
# never a block quote's lazy continuation (see :func:`_mask_quotes`): headings,
# block quotes, table rows and thematic breaks.
_BLOCK_START_RE = re.compile(r"^[ \t]*(?:#{1,6}[ \t]|>|\||-{3,}$|\*{3,}$|_{3,}$)")


def _starts_a_block(line: str) -> bool:
    """True when *line* opens a Markdown block of its own rather than
    continuing the paragraph above it."""
    return bool(
        _LIST_ITEM_RE.match(line)
        or _BLOCK_START_RE.match(line)
        or _FENCE_OPEN_RE.match(line)
    )


def _mask_quotes(text: str) -> str:
    """*text* with every block-quote line — and its **lazy continuation** —
    masked.

    CommonMark §5.1: a paragraph line directly after a quoted paragraph, with
    no blank line and no ``>`` of its own, is folded INTO the quote, so GitHub
    renders it as someone else's words (round-6 panel security f-005). Reading
    it as the author's verdict let "> the token is logged at src/api.py:88"
    followed immediately by "LGTM" satisfy the floor. The fold stops at the
    first blank line — which really does end the quote, so the author's own
    approval after one stays their verdict — and at any line that starts a
    block of its own, which is not paragraph continuation text.
    """
    out: list[str] = []
    quoted = False
    for line in text.split("\n"):
        if _QUOTE_LINE_RE.match(line):
            quoted = True
            out.append(_MASK)
        elif quoted and line.strip() and not _starts_a_block(line):
            out.append(_MASK)  # lazy continuation: still inside the quote
        else:
            quoted = False
            out.append(line)
    return "\n".join(out)


def _mask_fenced_blocks(text: str) -> str:
    """*text* with every fenced code block (opener line .. closing line)
    replaced by one masked line.

    CommonMark's structural rule, not a delimiter search (round-5 panel
    correctness f-002): the block ends at the first line that holds nothing but
    a run of the fence character at least as long as the opener; a longer run
    followed by other text is CONTENT. An unclosed fence runs to the end of the
    body, as CommonMark also says.
    """
    lines = text.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        opener = _FENCE_OPEN_RE.match(lines[i])
        if opener is None or (opener["fence"][0] == "`" and "`" in opener["info"]):
            out.append(lines[i])
            i += 1
            continue
        fence = opener["fence"]
        close = re.compile(rf"^ {{0,3}}{fence[0]}{{{len(fence)},}}[ \t]*$")
        out.append(_MASK)
        i += 1
        while i < len(lines) and close.match(lines[i]) is None:
            i += 1
        i += 1  # the closing fence line itself, or past the end
    return "\n".join(out)


def _mask_inline_runs(text: str, run: re.Pattern[str]) -> str:
    """*text* with every ``run``-delimited inline span masked.

    The closing run must be **exactly** as long as the opening one — the
    CommonMark code-span rule and the GFM strike-through rule (round-5 panel
    correctness f-002). An unclosed run masks the rest of the body: CommonMark
    would read it as literal text, but under an any-unit floor masking can only
    make a row LESS eligible, and reading it as a span is the direction that
    cannot silently consume a finding.
    """
    out: list[str] = []
    pos = 0
    while (opener := run.search(text, pos)) is not None:
        out.append(text[pos : opener.start()])
        out.append(_MASK)
        pos = opener.end()
        while (closer := run.search(text, pos)) is not None:
            pos = closer.end()
            if len(closer.group()) == len(opener.group()):
                break
        else:
            return "".join(out)  # unclosed: the remainder is inside the span
    out.append(text[pos:])
    return "".join(out)


def _join_soft_wraps(text: str) -> str:
    """Consecutive prose lines joined into the one paragraph GitHub renders.

    A single ``\n`` inside a paragraph is a SOFT WRAP, not a sentence boundary
    (round-5 panel security f-003). Hard-wrapping is the default in most
    editors and in much bot output, and every ``\n`` being a unit boundary cut
    those sentences mid-clause: "…whether or not the caller is\napproved, so
    the authz check is dead code" handed the floor the bare unit ``approved``
    its author never wrote as a verdict. Joining reads the sentence the author
    wrote, which is also the one a human sees on the PR.

    Only prose joins: a blank line (the paragraph break), a list item, a
    block-start line (heading, quote, table row, thematic break) and a line the
    masks above reduced to the bare mask — it STANDS FOR a block (a fence, a
    quote, an indented block, a data list), so it keeps that block's boundaries
    — all keep their break. Runs LAST in :func:`_authorial_text`: every
    line-structured mask above needs the real line breaks.
    """

    def prose(line: str) -> bool:
        stripped = line.strip()
        return (
            bool(stripped) and stripped != _MASK.strip() and not _starts_a_block(line)
        )

    out: list[str] = []
    for line in text.split("\n"):
        if out and prose(line) and prose(out[-1]):
            out[-1] = f"{out[-1]} {line.strip()}"
        else:
            out.append(line)
    return "\n".join(out)


def _reads_as_verdict(text: str) -> bool:
    """True when *text* reads END TO END as approval / courtesy.

    :func:`is_approval_text`'s rule applied to a fragment — used by the list
    rule below to tell a verdict list ("- No findings.") from an enumeration
    of values ("- approved").
    """
    units = _units(text, _WEAK_UNIT_SPLIT_RE)
    return bool(units) and all(
        unit.isascii()
        and (_APPROVAL_UNIT_RE.match(unit) or _COURTESY_UNIT_RE.match(unit))
        for unit in units
    )


def _introduced_as_data(lines: list[str], first: int) -> bool:
    """True when the list block starting at *first* hangs off a lead-in that is
    not itself a verdict — "The `state` field accepts:", but also the
    colon-less "Allowed status" (PR #426 round-5 review, correctness f-002).

    Keying on the colon alone left every other enumeration eligible, so the
    test is inverted: a list under prose this vocabulary cannot read as an
    approval is DATA, which is the only way to tell a one-item enum
    (``- approved``) from a one-line verdict. A trailing colon is the lead-in's
    own punctuation, not part of the verdict — "**No findings:**" over its
    bullets still reads as one.
    """
    for line in reversed(lines[:first]):
        if not line.strip():
            continue
        lead = " ".join(_DECORATION_RE.sub("", line.translate(_CANONICAL)).split())
        return not _reads_as_verdict(lead.rstrip(":"))
    return False


def _mask_data_lists(text: str) -> str:
    """*text* with every list block that reads as DATA masked out.

    A block is a verdict only when EVERY item reads as approval / courtesy and
    it carries no data lead-in; anything else is an enumeration whose values
    the author is naming, not asserting. The cost is borne in the safe
    direction: a pure-ish approval with a non-approval bullet ("- Checked the
    three streams.\n- No findings.") loses its eligibility and pays a
    remediation round, while an enum listing ``approved`` beside a real defect
    can no longer be dropped at round 0.
    """
    lines = text.split("\n")
    out = list(lines)
    run: list[int] = []

    def flush() -> None:
        if not run:
            return
        items = [_LIST_ITEM_RE.match(lines[i]) for i in run]
        if _introduced_as_data(lines, run[0]) or not all(
            m is not None and _reads_as_verdict(m["text"]) for m in items
        ):
            for i in run:
                out[i] = _MASK
        run.clear()

    for idx, line in enumerate(lines):
        if _LIST_ITEM_RE.match(line):
            run.append(idx)
        elif run and not line.strip():
            continue  # a blank line inside a loose list does not end it
        else:
            flush()
    flush()
    return "\n".join(out)


def _authorial_text(body: str) -> str:
    """*body* with every non-authorial span masked, then hard-wrapped prose
    rejoined — the text the weak floor reads.

    Fences first, then inline spans: a code block may hold a ``>``, a ``~~``,
    an ``<!-- -->`` or a shorter fence of its own, and inside one they are
    literal text. The line-structured masks all run before
    :func:`_join_soft_wraps`, which destroys the line breaks they read.
    """
    text = _mask_fenced_blocks(body)
    text = _mask_inline_runs(text, _BACKTICK_RUN_RE)
    text = _HTML_COMMENT_RE.sub(_MASK, text)
    text = _mask_inline_runs(text, _TILDE_RUN_RE)
    text = _mask_quotes(text)
    text = _TABLE_ROW_RE.sub(_MASK, text)
    text = _INDENTED_LINE_RE.sub(_MASK, text)
    return _join_soft_wraps(_mask_data_lists(text))


def carries_approval(body: str) -> bool:
    """True when ANY unit of *body* is a recognised approval phrase.

    The weak half of :func:`is_approval_text`: necessary for "this asks for
    nothing", never sufficient — "LGTM, but rename `foo`" carries an approval
    AND an ask, so this is True while :func:`is_approval_text` is False.

    It exists as the machine-checkable floor under S5a's
    ``NOTHING_TO_REMEDIATE`` verdict (PR #426 re-review, security f-001): the
    triage agent reads third-party prose, so its "this row is just an
    approval" judgement is honoured only for a row whose body actually
    contains approving words. A body with none — "this leaks the token" —
    can never be dropped as an approval, whatever a model was talked into
    writing about it. The strong half is not usable as that floor: the
    ingestion classifier already keeps every body it recognises end to end
    out of the dispatched batch, so requiring it would leave the verdict
    unreachable in production (and the S8 approval fixture unscorable)
    rather than guarded.

    Being an any-unit rule, it reads only AUTHORIAL text, at the granularity
    GitHub renders: block quotes with their lazy continuations, code spans and
    fences (parsed structurally — a fenced block ends only at a line holding
    nothing but its fence), strike-through, HTML comments, indented code and
    enumerations of values (a list, or a table row) are masked, and
    hard-wrapped prose is rejoined into its paragraph, before the split
    (:func:`_authorial_text`). So an approval word the author merely QUOTED,
    showed as data, struck out, hid in an invisible comment or never wrote at
    all (a line break landing in front of one) cannot satisfy it (PR #426
    round-5 review, f-001 + the panel's security f-001 / f-003 / f-004 / f-005
    and correctness f-002). The end-to-end rule
    deliberately keeps reading those contexts: there EVERY unit must pass, so
    a defect inside a block quote —
    "> the token is logged at src/api.py:88" above an "LGTM" — is precisely
    what makes the body actionable, and masking it would read that row as a
    pure approval.

    An approval EMOJI counts only when it is the whole verdict (the
    ``or is_approval_text`` arm), never as decoration inside prose (PR #426
    re-review, security f-003): the rewrite that turns "👍" into an approval
    unit is safe under the strong classifier, where every other unit must
    still pass, but under an any-unit rule it let a checkmark-bulleted defect
    report — "✅ Checked the auth path. ❌ The token is logged at
    src/api.py:88" — satisfy the floor. Nothing is lost in production: a body
    the strong classifier reads end to end is never in a dispatched batch
    anyway.

    So the weak scan **erases** every approval emoji and shortcode first,
    leaving nothing where it stood. Dropping the rewrite was not enough on its
    own (PR #426 round-3 review, correctness f-002): ``:`` was a unit
    boundary, so the raw ``:+1:`` shortcode split into the bare unit ``+1`` —
    itself a recognised approval phrase — and ":+1: Checked the auth path. The
    token is logged at src/api.py:88" satisfied the floor by the very
    decoration the rule above excludes. Nor was erasing it to a ``.``: that
    manufactured a SENTENCE boundary the author never wrote, so "This is not
    👍 good." and "Not :+1: approved." split their negations off and handed
    the floor the approval word alone (round-4 review, correctness f-002).
    The mask must be invisible to the splitter, which is why it deletes: the
    worst a deletion can do is fuse two fragments into a word, and a fused
    word matches nothing.
    """
    scanned = _units(
        _authorial_text(_APPROVAL_EMOJI_RE.sub("", body)), _WEAK_UNIT_SPLIT_RE
    )
    if any(unit.isascii() and _APPROVAL_UNIT_RE.match(unit) for unit in scanned):
        return True
    return is_approval_text(body)


def is_approval_text(body: str) -> bool:
    """True when *body* carries an approval and **no ask** — nothing to remediate.

    Every unit of the body (sentence / list item / line) must be a known
    approval phrase or a neutral courtesy, and at least one must be an
    approval. Anything else — a request, a question, an observation, code,
    an emoji that is not an approval, prose in a script this vocabulary
    cannot read — makes the whole body a finding.
    """
    approved = False
    # The emoji stands alone as its own unit: "Ship it 🚀" is two approvals,
    # never one unrecognised sentence.
    for unit in _units(_APPROVAL_EMOJI_RE.sub(". lgtm .", body)):
        if not unit.isascii():
            # Not deleted, not skipped: text this vocabulary cannot read is
            # an ask until something says otherwise (security f-001).
            return False
        if _APPROVAL_UNIT_RE.match(unit):
            approved = True
        elif not _COURTESY_UNIT_RE.match(unit):
            return False
    return approved
