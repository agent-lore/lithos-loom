"""Brief review at dispatch (604fb936), phase 2: the structural check.

With ``develop_brief_review_auto_facts`` on, an addendum that holds **only
facts** may be appended to the story without its gate. The pass's own labels
are not enough for that: the pilot showed the fact/decision split is exactly
where the agent errs (W6's "the cancel routes go in a new module" sat under
Facts). So every fact must also pass a second check that is structural, pure
and deterministic — no judgment, no I/O:

1. **heading** — its bold title carries no prescriptive marker
   (:data:`HEADING_MARKERS`, word-bounded, case-insensitive);
2. **citation** — at least one backtick span is, in its entirety, a
   repository path with a known extension and an optional ``:N`` / ``:N-M``;
3. **marker** — no ``Basis:`` or ``Proposed:`` (a decision's lines);
4. **structure** — nothing in its body that would read as another item or
   section once appended: an emphasised or tagged item id (``**D1.`` behind
   any prefix), a bare decision or scope-cut id, a section word in emphasis
   or a tag, a table row or HTML line, a Markdown heading or setext
   underline. The parse folds every such line into the open item, so
   without this rule one "fact" could carry a rendered block of decisions or
   scope cuts past the gate. An id longer than eight characters fails too:
   the PR listing could not show it whole.

And the PR body must be able to list every appended fact
(:data:`MAX_LISTED_FACTS`), so a larger addendum is held as well.

Every failure direction is safe: a fact the check cannot read is treated as
a decision, and the story is held for the operator. Every pattern here is
linear in its input — the input is an agent's handoff, up to 1 MiB.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .brief_review import (
    ITEM_CUT,
    ITEM_UNCHANGED,
    KIND_FACT,
    MODE_DELTA,
    MODE_FULL,
    OUTCOME_APPROVED,
    OUTCOME_AUTO_APPENDED,
    Addendum,
    BriefItem,
    item_digest,
    parse_addendum,
)
from .publish_text import fence_untrusted, publish_line

__all__ = [
    "CITATION_EXTENSIONS",
    "HEADING_MARKERS",
    "MAX_LISTED_FACTS",
    "AutoFactsVerdict",
    "brief_review_section",
    "check_auto_facts",
    "describe_verdict",
    "fact_headings",
    "fact_problems",
    "item_heading",
    "pr_section",
]

# The words that make a heading say what to build rather than what is there.
# The prompt (``prompts/brief_review.md``) names exactly these.
HEADING_MARKERS: tuple[str, ...] = (
    "must",
    "should",
    "shall",
    "needs to",
    "has to",
    "goes in",
    "go in",
    "put",
    "use",
    "add",
    "create",
    "move",
    "rename",
    "split",
    "extract",
    "make",
)
CITATION_EXTENSIONS: frozenset[str] = frozenset(
    {
        "py",
        "pyi",
        "md",
        "toml",
        "json",
        "yaml",
        "yml",
        "txt",
        "cfg",
        "ini",
        "sh",
        "js",
        "ts",
        "tsx",
        "css",
        "html",
        "sql",
    }
)

# The PR body's ``## Brief review`` lists every fact appended without a gate,
# one line each, capped at _LISTED_LINE_CHARS. An addendum with more facts
# than this is held: a list that dropped some would hide them at the PR gate.
MAX_LISTED_FACTS = 40
_LISTED_LINE_CHARS = 128
# defang_markup grows text at most 4x (`<` → `&lt;`), so the listing's fence
# can never cut a line within the two caps above.
_LISTING_LIMIT = MAX_LISTED_FACTS * (_LISTED_LINE_CHARS + 1) * 5
# The record outcomes whose text was appended to the description: the ones
# the PR body's ``## Brief review`` lists (pending, failed and no-change
# entries appended nothing).
_APPENDED = (OUTCOME_APPROVED, OUTCOME_AUTO_APPENDED)
# How ``render_addendum``'s header names each mode — what finds an entry's
# block in the description, with the base's first 12 hex digits.
_MODE_TITLE: dict[str, str] = {MODE_FULL: "Brief review", MODE_DELTA: "Recheck"}
# A rendered addendum's header: at column 0, opening a paragraph, outside
# any fenced block (an item may quote one in an example). Linear: a fixed
# prefix, then a bounded hex run. An operator-edited block keeps it unless
# they edited it away.
_ADDENDUM_HEADER_RE = re.compile(
    r"(?:\*\*)?(?P<title>brief review|recheck) against `(?P<base>[0-9a-f]{12})`",
    re.IGNORECASE,
)
# A fence line, behind any indent or blockquote: the run that opens or
# closes it (a close is the same character, at least as long).
_FENCE_LINE_RE = re.compile(r"[ \t>]*(?P<run>`{3,}|~{3,})")
# The whole ``## Brief review`` section's size: one worst-case listing
# always fits, and with ``## What``, ``## Acceptance criteria`` and a
# provenance quote at MAX_SECTION_CHARS each the body stays well under
# GitHub's 65536-character limit. Older addenda past it are counted, not
# listed — a body over the limit would fail every delivery of the story.
_SECTION_BUDGET = _LISTING_LIMIT + 2_000
_BASE_SHA_RE = re.compile(r"[0-9a-f]{12,64}")
_AUTO_NOTE = "without a gate (facts only, structural check passed)"
# A citation longer than this is not a path anyone writes; refusing it before
# the match keeps _PATH_RE's backtracking bounded.
_MAX_PATH_CHARS = 512

# Linear: the heading class excludes ``*``, so it cannot overlap the close.
_HEADING_RE = re.compile(r"^\*\*[A-Z]\d+\.(?P<heading>[^*\n]*)\*\*")
_MARKER_RES = tuple(
    (marker, re.compile(rf"\b{marker.replace(' ', r'\s+')}\b", re.IGNORECASE))
    for marker in HEADING_MARKERS
)
_SPAN_RE = re.compile(r"`([^`\n]+)`")
_PATH_RE = re.compile(
    r"(?:[\w.-]+/)*\.*[\w-][\w.-]*\.(?P<ext>[A-Za-z]+)(?::\d+(?:-\d+)?)?"
)
_DECISION_LINE_RE = re.compile(r"\b(?P<marker>Basis|Proposed):", re.IGNORECASE)
# What in a fact's body (everything after its own ``**F1.``) would read as
# structure of its own once appended. Enumerating the prefixes a bullet can
# hide behind (a blockquote, a checkbox, a second bullet, a Unicode hyphen)
# cannot win, so the patterns key on the TOKENS, anywhere in a line:
# an emphasised or tagged item id (``**D1.``, ``__D2.``, ``<b>D1.``); a bare
# decision or scope-cut id — a facts-only addendum has none to refer to; a
# section word inside emphasis or a tag (``*Decisions*``, ``**Binding
# decisions:**``, ``<h3>Decisions</h3>``); a table row or HTML line (``|``,
# ``<!--``); one of the sentences loom itself renders around an addendum
# (``render_addendum``'s headers, ``_section_heading``'s texts). Plus, at a
# line's start, an ATX heading or a setext underline. The emphasised id
# takes ANY letter (``**Ꭰ1.``, ``**X1.``), and the body is searched NFKC-
# normalised with combining marks removed, so a look-alike (``Ｄ1``, ``𝐃1``,
# ``D́1``) reads as the ``D1`` it imitates. Every repetition after a ``<`` is
# bounded, so each search stays linear.
_TAG = r"<[A-Za-z!/][^>\n]{0,200}>"
_SECTION_WORD = r"(?:scope\s+(?:cuts?|changes?)|decisions?|no\s+change)\b"
_STRUCTURE_SEARCHES = (
    re.compile(rf"(?:\*\*|__|{_TAG})\W{{0,3}}[^\W\d_]\d+[.:)]"),
    re.compile(r"\b[SD]\d+\b"),
    re.compile(
        rf"(?:\*|(?<!\w)_|{_TAG})[^*_<\n]{{0,40}}?\b{_SECTION_WORD}", re.IGNORECASE
    ),
    re.compile(
        r"they\s+replace\s+the\s+matching\s+parts|no\s+judgment\s+involved"
        r"|brief\s+review\s+against|recheck\s+against|the\s+item\s+wins",
        re.IGNORECASE,
    ),
)
_STRUCTURE_LINES = (
    re.compile(r"^[ \t>]*[|<]"),
    re.compile(r"^[ \t]*#{1,6}(?:[ \t]|$)"),
    re.compile(r"^[ \t]*(?:=+|-+)[ \t]*$"),
)
_OWN_ID_RE = re.compile(r"^\*\*[A-Z]\d+\.")
# A longer id is not one anybody numbers to; the PR listing could not show
# it whole, so it would no longer identify its fact.
_MAX_ID_CHARS = 8
# A word mixing ASCII letters with other letters — a look-alike spelling
# (Cyrillic ``у`` in ``Pуt``) would otherwise slip a marker past rule 1.
_WORD_RE = re.compile(r"[^\W\d_]+")


@dataclass(frozen=True)
class AutoFactsVerdict:
    """The check's answer for one addendum. *facts_only*: the pass labelled
    every item a fact (and there are some, with no stray line and no
    ``No change``). *problems*: one line per failed rule per fact."""

    facts_only: bool
    problems: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return self.facts_only and not self.problems

    def as_record(self) -> dict[str, object]:
        """The ``check`` sub-record every drafted ``brief_review`` entry carries."""
        return {
            "facts_only": self.facts_only,
            "passed": self.passed,
            "problems": list(self.problems),
        }


def item_heading(item: BriefItem) -> str | None:
    """The bold title after *item*'s id, up to the closing ``**`` — ``None``
    when the first line is not ``**<id>. <title>**``."""
    match = _HEADING_RE.match(item.text)
    if match is None or not match.group("heading").strip():
        return None
    return match.group("heading").strip()


def fact_problems(item: BriefItem) -> list[str]:
    """Why *item* reads like a decision, one line per failed rule (empty: it
    reads as a fact)."""
    problems: list[str] = []
    label = item.id if len(item.id) <= _MAX_ID_CHARS else f"{item.id[:6]}…"
    if label != item.id:
        problems.append(
            f"{label} reads like a decision: its id is longer than "
            f"{_MAX_ID_CHARS} characters"
        )
    heading = item_heading(item)
    if heading is None:
        problems.append(
            f"{label} reads like a decision: its heading could not be read — "
            f"start it `- **{label}. <title>.**`"
        )
    else:
        said = [m for m, pattern in _MARKER_RES if pattern.search(heading)]
        if said:
            words = ", ".join(f"`{m}`" for m in said)
            problems.append(f"{label} reads like a decision: heading says {words}")
        if any(_mixed_script(word) for word in _WORD_RE.findall(heading)):
            problems.append(
                f"{label} reads like a decision: its heading mixes alphabets "
                "within a word (a look-alike letter hides what it says)"
            )
    if not any(_is_path(span) for span in _SPAN_RE.findall(item.text)):
        problems.append(
            f"{label} reads like a decision: it cites no repository path "
            "(a backticked `path/file.py:123`)"
        )
    marked = sorted(
        {m.group("marker").capitalize() for m in _DECISION_LINE_RE.finditer(item.text)}
    )
    if marked:
        lines = ", ".join(f"`{m}:`" for m in marked)
        problems.append(f"{label} reads like a decision: it carries {lines}")
    body = _normalised(_OWN_ID_RE.sub("", item.text, count=1))
    body_lines = body.splitlines()
    if any(p.search(body) for p in _STRUCTURE_SEARCHES) or any(
        p.match(line) for line in body_lines[1:] for p in _STRUCTURE_LINES
    ):
        problems.append(
            f"{label} reads like a decision: its body holds another item or "
            "its id, a heading, a table, HTML or a section name — write each "
            "item as its own top-level bullet under its section"
        )
    return problems


def _is_path(span: str) -> bool:
    if len(span) > _MAX_PATH_CHARS:
        return False
    match = _PATH_RE.fullmatch(span)
    return match is not None and match.group("ext") in CITATION_EXTENSIONS


def _normalised(text: str) -> str:
    """*text* NFKC-normalised, combining marks removed: compatibility and
    decorated forms of a letter read as the letter they imitate."""
    folded = unicodedata.normalize("NFKC", text)
    return "".join(ch for ch in folded if unicodedata.category(ch) != "Mn")


def _mixed_script(word: str) -> bool:
    ascii_letters = sum(1 for ch in word if ch.isascii())
    return 0 < ascii_letters < len(word)


def check_auto_facts(addendum: Addendum) -> AutoFactsVerdict:
    """The structural check over *addendum* — run on every pass, so every
    drafted record carries it, whether or not the dial lets it skip a gate."""
    facts_only = (
        bool(addendum.items)
        and all(item.kind == KIND_FACT for item in addendum.items)
        and not addendum.stray
        and addendum.no_change is None
    )
    problems = [p for item in addendum.facts for p in fact_problems(item)]
    if len(addendum.facts) > MAX_LISTED_FACTS:
        problems.append(
            f"{len(addendum.facts)} facts are more than the {MAX_LISTED_FACTS} "
            "a PR body lists — merge or cut facts to send them without a gate"
        )
    return AutoFactsVerdict(facts_only=facts_only, problems=tuple(problems))


def fact_headings(addendum: Addendum) -> tuple[str, ...]:
    """Each fact as ``"F13. W7 added no refusal hook."`` — its id and heading
    (the first line, for a fact whose heading cannot be read)."""
    return tuple(_listed(item) for item in addendum.facts)


def _listed(item: BriefItem) -> str:
    return f"{item.id}. {item_heading(item) or item.text.splitlines()[0]}"


def pr_section(headings: tuple[str, ...]) -> str:
    """The PR body's ``## Brief review`` section for facts appended without a
    gate: loom's own heading and line, then *headings* fenced (the agent wrote
    them). ``""`` when there are none. ``## What`` is capped, and appended
    facts are its tail — the first text cut — so they are listed here too.

    Every fact gets its own line, each capped, under a fence limit those caps
    cannot reach — so the list is never cut short. The check holds an
    addendum with more than :data:`MAX_LISTED_FACTS` facts; past that, a
    caller's extra headings are counted on loom's own line, never dropped
    silently."""
    if not headings:
        return ""
    parts = [
        "## Brief review",
        "",
        f"These facts were appended to the story's description at dispatch "
        f"{_AUTO_NOTE}. Their full text ends *What*, unless that section was "
        "truncated, and is always on the Lithos story.",
        *_listing(headings, noun="facts"),
    ]
    return "\n".join(parts)


def brief_review_section(records: Sequence[Mapping[str, Any]], description: str) -> str:
    """The PR body's ``## Brief review`` section for every addendum appended
    to the story — each *records* entry (``metadata.brief_review``, oldest
    first) that is ``approved`` or ``auto_appended`` — or ``""`` for none.
    One renderer for both deliveries: a run (and a resumed one) and
    ``lithos-loom develop deliver``.

    A record keeps item digests, not text, so each entry's items are read
    from its block in *description* — the text ``## What`` renders: the
    block opens at the header ``render_addendum`` wrote (the mode's title and
    ``base_sha[:12]``; entries sharing one pair with headers in order) and
    runs to the next such header or the end, and its items must be the ones
    the record vouches for. Every item, of every kind, is listed as
    ``<id>. <heading>``. An entry whose block is not there (its header edited
    away, an emptied approval, a later edit) is named on loom's line as not
    found — never filled in from the record's draft.

    Loom's own lines (mode, base, outcome) are never cut; each listing is
    capped as :func:`pr_section`'s is, its overflow counted, not dropped. The
    section keeps within :data:`_SECTION_BUDGET`: the newest addenda that
    fit are listed, and the earlier ones counted on loom's line."""
    entries = [e for e in records if e.get("outcome") in _APPENDED]
    if not entries:
        return ""
    blocks = _addendum_blocks(description)
    # Paired in record order (blocks in order of appearance), then the
    # newest that fit the budget are listed, the rest counted.
    chunks = [_entry_chunk(entry, blocks) for entry in entries]
    listed: list[list[str]] = []
    spent = 0
    for chunk in reversed(chunks):
        size = sum(len(line) + 1 for line in chunk)
        if listed and spent + size > _SECTION_BUDGET:
            break
        listed.append(chunk)
        spent += size
    parts = [
        "## Brief review",
        "",
        "The brief review appended these addenda to the story's description, "
        "oldest first; each is listed by item id and heading. Their full text "
        "ends *What*, unless that section was truncated, and is always on the "
        "Lithos story.",
    ]
    if len(chunks) > len(listed):
        earlier = len(chunks) - len(listed)
        parts += [
            "",
            f"…and {earlier} earlier {'addendum' if earlier == 1 else 'addenda'} "
            "not listed here, to keep this PR body within GitHub's size limit; "
            "see the Lithos story.",
        ]
    for chunk in reversed(listed):
        parts += chunk
    return "\n".join(parts)


def _entry_chunk(
    entry: Mapping[str, Any], blocks: dict[tuple[str, str], list[str]]
) -> list[str]:
    """*entry*'s lines in the section: loom's line, then its items fenced.
    Its block is the first unpaired one under its header whose items are the
    ones the record vouches for (:func:`_matches_record`) — a block that is
    not (a later edit, or a header someone else wrote into the description)
    is skipped, so it can neither stand in for the addendum nor hide it."""
    line, key = _entry_line(entry)
    candidates = blocks.get(key, []) if key else []
    for index, block in enumerate(candidates):
        items = parse_addendum(block).items
        if items and _matches_record(entry, items):
            del candidates[index]
            headings = tuple(_listed(item) for item in items)
            return ["", f"{line}.", *_listing(headings, noun="items")]
    return [
        "",
        f"{line}. Its text was not found in the description as its record "
        "describes it (its header was edited, the approval appended nothing, "
        "or the description changed since), so its items are not listed.",
    ]


def _matches_record(entry: Mapping[str, Any], items: tuple[BriefItem, ...]) -> bool:
    """Whether *items* are the addendum *entry* records: an auto-appended
    pass's ids and digests exactly; an approval's kept ids (every id its
    ``outcomes`` does not mark cut), with each ``unchanged`` one's digest."""
    ids = [item.id for item in items]
    if len(set(ids)) != len(ids):
        return False
    recorded: dict[str, object] = {
        item["id"]: item.get("sha")
        for item in entry.get("items") or ()
        if isinstance(item, Mapping) and isinstance(item.get("id"), str)
    }
    if entry.get("outcome") == OUTCOME_AUTO_APPENDED:
        return set(ids) == set(recorded) and all(
            item_digest(item.text) == recorded[item.id] for item in items
        )
    outcomes = entry.get("outcomes")
    if not isinstance(outcomes, Mapping):
        return False
    kept = {item_id for item_id, how in outcomes.items() if how != ITEM_CUT}
    return set(ids) == kept and all(
        item_digest(item.text) == recorded.get(item.id)
        for item in items
        if outcomes.get(item.id) == ITEM_UNCHANGED
    )


def _addendum_blocks(description: str) -> dict[tuple[str, str], list[str]]:
    """Each rendered addendum in *description*, keyed by its header's
    ``(title, base)`` in order of appearance: from its header to the next
    one, or the end. A header counts only at column 0, opening a paragraph,
    outside a fenced block — so an item's quoted example is not a boundary."""
    starts: list[tuple[int, tuple[str, str]]] = []
    fence: str | None = None
    after_blank = True
    offset = 0
    for line in description.splitlines(keepends=True):
        run = _FENCE_LINE_RE.match(line)
        if fence is not None:
            if run is not None and run.group("run").startswith(fence):
                fence = None
        elif run is not None:
            fence = run.group("run")
        elif after_blank and (header := _ADDENDUM_HEADER_RE.match(line)):
            key = (header.group("title").lower(), header.group("base").lower())
            starts.append((offset, key))
        after_blank = not line.strip()
        offset += len(line)
    blocks: dict[tuple[str, str], list[str]] = {}
    ends = [at for at, _ in starts[1:]] + [len(description)]
    for (at, key), end in zip(starts, ends, strict=False):
        blocks.setdefault(key, []).append(description[at:end])
    return blocks


def _entry_line(entry: Mapping[str, Any]) -> tuple[str, tuple[str, str] | None]:
    """Loom's line for *entry* — its mode, base and outcome, only as far as
    they are well formed — and the ``(title, base)`` its block's header
    carries (``None``: no header could be its)."""
    mode = entry.get("mode")
    title = _MODE_TITLE.get(mode) if isinstance(mode, str) else None
    raw_base = entry.get("base_sha")
    base = (
        raw_base[:12].lower()
        if isinstance(raw_base, str) and _BASE_SHA_RE.fullmatch(raw_base.lower())
        else None
    )
    line = (
        f"**{title or 'Addendum'} against "
        f"{f'`{base}`' if base else 'an unrecorded base'}** "
        f"(mode `{mode if title else 'unknown'}`, outcome `{entry['outcome']}`): "
        + _appended_how(entry)
    )
    return line, ((title.lower(), base) if title and base else None)


def _appended_how(entry: Mapping[str, Any]) -> str:
    """How *entry*'s addendum reached the description, in loom's words."""
    if entry.get("outcome") == OUTCOME_APPROVED:
        return "approved by the operator at its brief-review gate"
    check = entry.get("check")
    if isinstance(check, Mapping) and check.get("passed") is True:
        return f"appended at dispatch {_AUTO_NOTE}"
    return "appended at dispatch without a gate, as a facts-only recheck"


def _listing(headings: tuple[str, ...], *, noun: str) -> list[str]:
    """*headings* fenced, one capped line each, under a fence limit those caps
    cannot reach — so the list is never cut short — with any past
    :data:`MAX_LISTED_FACTS` counted on loom's own line after it."""
    listed = [
        publish_line(heading, limit=_LISTED_LINE_CHARS)
        for heading in headings[:MAX_LISTED_FACTS]
    ]
    parts = ["", fence_untrusted("\n".join(listed), limit=_LISTING_LIMIT)]
    if len(headings) > len(listed):
        parts += ["", f"…and {len(headings) - len(listed)} more {noun}."]
    return parts


def describe_verdict(
    verdict: AutoFactsVerdict, *, counts: Mapping[str, int], mode: str = MODE_FULL
) -> str:
    """One line saying what the dispatch would do with this verdict. *counts*
    is the pass's ``{"scope_cut", "fact", "decision"}`` tally; *mode* full or
    delta (a recheck is governed by the check whatever the dial says)."""
    dial = "with develop_brief_review_auto_facts on"
    if not any(counts.values()):
        tail = (
            "would proceed without a gate"
            if mode == MODE_DELTA
            else f"would skip the gate {dial}"
        )
        return f"auto-facts check: no change — {tail}"
    if verdict.passed:
        if mode == MODE_DELTA:
            return "auto-facts check: would append without a gate"
        return f"auto-facts check: would skip the gate {dial}"
    if verdict.facts_only:
        return f"auto-facts check: would hold: {'; '.join(verdict.problems)}"
    other = [
        f"{n} {noun}{'' if n == 1 else 's'}"
        for n, noun in (
            (counts.get("scope_cut", 0), "scope cut"),
            (counts.get("decision", 0), "decision"),
        )
        if n
    ]
    return (
        f"auto-facts check: not facts only ({', '.join(other) or 'stray lines'}): "
        "would hold"
    )
