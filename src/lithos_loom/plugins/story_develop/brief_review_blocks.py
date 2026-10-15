"""Where each brief-review addendum is in a story's description.

A record entry (``metadata.brief_review``) keeps its items' ids and digests,
not their text (``draft_entry``), and the appenders add the text to the
description with no fence of their own. So the PR body's ``## Brief
review`` (``brief_review_auto.brief_review_section``) reads each entry's
items from the description: the block under the header ``render_addendum``
wrote, whose items are the ones the record vouches for (:func:`find_blocks`).

A header line is only a CANDIDATE boundary — an item may quote one, and the
description's head may say anything — so every candidate is considered, and
the record decides. The work stays linear in the description per candidate
header: an item opens and closes at the same lines whichever block it is
parsed in, so the description is parsed once and only the block that
matches is parsed again.
"""

from __future__ import annotations

import bisect
import hashlib
import heapq
import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .brief_review import (
    ITEM_CUT,
    ITEM_UNCHANGED,
    MODE_DELTA,
    MODE_FULL,
    OUTCOME_AUTO_APPENDED,
    Addendum,
    BriefItem,
    item_digest,
    parse_addendum,
)

__all__ = [
    "ADDENDUM_HEADER_RE",
    "MODE_TITLE",
    "entry_header",
    "find_blocks",
    "matches_record",
]

# How ``render_addendum``'s header names each mode — what finds an entry's
# block in the description, with the base's first 12 hex digits.
MODE_TITLE: dict[str, str] = {MODE_FULL: "Brief review", MODE_DELTA: "Recheck"}
# A rendered addendum's header: at column 0, opening a paragraph. Linear: a
# fixed prefix, then a bounded hex run. An operator-edited block keeps it
# unless they edited it away. Such a line inside an addendum (an item quoting
# one) or before it (anything the description says) is only a CANDIDATE
# boundary: the record check decides which text is the addendum.
ADDENDUM_HEADER_RE = re.compile(
    r"(?:\*\*)?(?P<title>brief review|recheck) against `(?P<base>[0-9a-f]{12})`",
    re.IGNORECASE,
)
_BASE_SHA_RE = re.compile(r"[0-9a-f]{12,64}")


def entry_header(entry: Mapping[str, Any]) -> tuple[str | None, str | None]:
    """The mode title and ``base_sha[:12]`` *entry*'s block's header carries,
    each only when the record's value is well formed (``None`` otherwise)."""
    mode = entry.get("mode")
    title = MODE_TITLE.get(mode) if isinstance(mode, str) else None
    raw_base = entry.get("base_sha")
    base = (
        raw_base[:12].lower()
        if isinstance(raw_base, str) and _BASE_SHA_RE.fullmatch(raw_base.lower())
        else None
    )
    return title, base


@dataclass(frozen=True)
class _Span:
    """One item of the description, by line — where :func:`parse_addendum`
    opens it, whatever text it is parsed in. *end*: the line after its text
    (trailing blank lines aside); *digest*: its text's :func:`item_digest`;
    *cut*: the digest of its text cut at each candidate boundary inside it."""

    line: int
    id: str
    end: int
    digest: str
    cut: dict[int, str]


def find_blocks(
    entries: Sequence[Mapping[str, Any]], description: str
) -> list[Addendum | None]:
    """Each entry's addendum in *description*, or ``None`` when no text there
    is the one its record vouches for (:func:`matches_record`).

    A candidate block opens at a header line under the entry's ``(title,
    base)`` and ends at a later candidate header or the description's end —
    every one is considered, so a header an item quotes, in a fence or not,
    ends nothing. The appenders only append, so the newest entry pairs with
    the LAST matching candidate, and the next-newest with one before it —
    pairing in order of appearance, with text earlier in the description
    (anything the story said before its addenda) tried last, and the
    longest matching block under a header first; found blocks do not
    overlap. A header that reads as a quote (:func:`_quoted`) is tried only
    after every other, so it cannot split the block that quotes it. A text no record vouches for can stand in for no addendum.

    An item opens and closes at the same lines whichever block it is parsed
    in, so the description is parsed once (:func:`_spans`) and each candidate
    is checked against the record from those spans; only the block that
    passes is parsed. The work is linear per candidate header, whatever the
    description holds."""
    lines = description.splitlines()
    offsets = [0]
    for line in description.splitlines(keepends=True):
        offsets.append(offsets[-1] + len(line))
    heads: list[tuple[int, tuple[str, str]]] = []
    for index, line in enumerate(lines):
        header = ADDENDUM_HEADER_RE.match(line)
        if header and (index == 0 or not lines[index - 1].strip()):
            key = (header.group("title").lower(), header.group("base").lower())
            heads.append((index, key))
    bounds = [index for index, _ in heads] + [len(lines)]
    spans = _spans(lines, parse_addendum(description).items, set(bounds))
    quoted = _quoted(lines, spans, [index for index, _ in heads])
    # newest first; a header that reads as a quote only after every other
    order = [h for h in reversed(heads) if h[0] not in quoted] + [
        h for h in reversed(heads) if h[0] in quoted
    ]
    taken: list[tuple[int, int]] = []
    found: list[Addendum | None] = [None] * len(entries)
    for index in reversed(range(len(entries))):
        title, base = entry_header(entries[index])
        key = (title.lower(), base) if title and base else None
        chosen = _search(entries[index], key, order, bounds, spans, taken)
        for start, end in chosen:
            addendum = parse_addendum(description[offsets[start] : offsets[end]])
            if matches_record(entries[index], addendum.items):
                found[index] = addendum
                taken.append((start, end))
                break
    return found


def _spans(
    lines: Sequence[str], items: Sequence[BriefItem], bounds: set[int]
) -> list[_Span]:
    """Where each of *items* (the whole description's parse, in order) sits
    in *lines*, with its digest cut at each of *bounds* inside it — hashed a
    line at a time, so reading is linear however many bounds an item holds.
    An item's first line is its own ``- **<id>.`` line, which nothing before
    it can repeat without opening an item there."""
    spans: list[_Span] = []
    at = 0
    for item in items:
        own = item.text.split("\n")
        opening = f"- {own[0]}"
        while at < len(lines) and lines[at] != opening:
            at += 1
        if at == len(lines):
            break
        hasher = hashlib.sha256()
        cut: dict[int, str] = {}
        started = False
        for index, line in enumerate(own, start=at):
            if index in bounds and index > at:
                cut[index] = hasher.copy().hexdigest()[:16]
            # item_digest's whitespace-blind text, fed a line at a time
            if words := " ".join(line.split()):
                hasher.update(f"{' ' if started else ''}{words}".encode())
                started = True
        spans.append(_Span(at, item.id, at + len(own), hasher.hexdigest()[:16], cut))
        at += len(own)
    return spans


def _quoted(
    lines: Sequence[str], spans: Sequence[_Span], heads: Sequence[int]
) -> set[int]:
    """The *heads* that read as a quote: inside an item's text that runs on
    to the next item (or the end) with no section heading between — an
    addendum's header is followed by a section heading before its items, as
    ``render_addendum`` writes it. An operator may quote the block's own
    header in an item; a block from that quote on can hold every kept id
    too, so it is tried only after every header that opens a section."""
    span_lines = [span.line for span in spans]
    quoted: set[int] = set()
    runs_on: dict[int, bool] = {}
    for head in heads:
        index = bisect.bisect_right(span_lines, head) - 1
        if index < 0 or head >= spans[index].end:
            continue
        if index not in runs_on:
            stop = span_lines[index + 1] if index + 1 < len(spans) else len(lines)
            gap = lines[spans[index].end : stop]
            runs_on[index] = not any(line.strip() for line in gap)
        if runs_on[index]:
            quoted.add(head)
    return quoted


def _search(
    entry: Mapping[str, Any],
    key: tuple[str, str] | None,
    heads: Sequence[tuple[int, tuple[str, str]]],
    bounds: Sequence[int],
    spans: Sequence[_Span],
    taken: Sequence[tuple[int, int]],
) -> Iterator[tuple[int, int]]:
    """The ``(start, end)`` lines of each block *entry*'s record could be,
    most likely first: candidate *heads* in the order given, its longest block
    first, ending where its last item can — checked from *spans* the way
    :func:`matches_record` checks a parse (ids, and digests where the record
    has them).

    Whether a block whose last item is ``spans[p]`` holds the recorded text
    does not depend on where it starts, past its ids all being seen
    (:func:`_scan`): so each candidate header is a search, not a walk."""
    expected = _expected(entry)
    if key is None or expected is None:
        return
    allowed, need, unique = expected
    span_lines = [span.line for span in spans]
    beyond = [len(spans)] * (len(spans) + 1)  # the next id the record lacks
    for index in reversed(range(len(spans))):
        beyond[index] = index if spans[index].id not in allowed else beyond[index + 1]
    ok, least = _scan(spans, allowed, need)
    ends: dict[int, tuple[list[int], list[int]]] = {}  # by limit
    for start, head_key in heads:
        if head_key != key or any(a <= start < b for a, b in taken):
            continue
        limit = min((a for a, _ in taken if a > start), default=bounds[-1])
        first = bisect.bisect_left(span_lines, start)
        if not allowed:  # an approved No change: a block with no items
            stop = span_lines[first] if first < len(spans) else bounds[-1]
            if (end := _last_bound(bounds, start, min(stop, limit))) is not None:
                yield start, end
            continue
        if limit not in ends:
            ends[limit] = _ends(spans, bounds, ok, need, limit)
        last_items, last_ends = ends[limit]
        stop = min(beyond[first], bisect.bisect_left(span_lines, limit))
        # the block's ids all seen by its last item; exactly once, if unique
        low = bisect.bisect_left(least, first)
        if unique:
            low = max(low, first + len(allowed) - 1)
            stop = min(stop, first + len(allowed))
        # the longest block first: an approval's repeated `edited` id may sit
        # past a header its items quote, and a shorter block already holds
        # every kept id — only the whole block lists every copy
        at = bisect.bisect_left(last_items, stop) - 1
        while at >= 0 and last_items[at] >= low:
            yield start, last_ends[at]
            at -= 1


def _scan(
    spans: Sequence[_Span], allowed: frozenset[str], need: Mapping[str, object]
) -> tuple[list[bool], list[int]]:
    """Per item ``p``, start-independent: whether every checked id but its
    own has its recorded text at its last occurrence up to ``p``; and the
    earliest of the allowed ids' last occurrences (``-1`` until all are
    seen) — a block starting at or before it holds them all."""
    last: dict[str, int] = {}
    heap: list[tuple[int, str]] = []
    wrong = set(need)  # checked ids whose last text so far is not recorded
    ok: list[bool] = []
    least: list[int] = []
    for index, span in enumerate(spans):
        ok.append(not wrong or wrong == {span.id})
        if span.id in allowed:
            last[span.id] = index
            heapq.heappush(heap, (index, span.id))
        while heap and last[heap[0][1]] != heap[0][0]:
            heapq.heappop(heap)
        least.append(heap[0][0] if heap and len(last) == len(allowed) else -1)
        if span.id in need:
            if span.digest == need[span.id]:
                wrong.discard(span.id)
            else:
                wrong.add(span.id)
    return ok, least


def _ends(
    spans: Sequence[_Span],
    bounds: Sequence[int],
    ok: Sequence[bool],
    need: Mapping[str, object],
    limit: int,
) -> tuple[list[int], list[int]]:
    """Each item a block ending before *limit* can end with, and where —
    linear: the items' end windows do not overlap."""
    items: list[int] = []
    ends: list[int] = []
    for index, span in enumerate(spans):
        if span.line >= limit:
            break
        if ok[index]:
            end = _span_end(span, index, spans, bounds, limit, need)
            if end is not None:
                items.append(index)
                ends.append(end)
    return items, ends


def _span_end(
    span: _Span,
    index: int,
    spans: Sequence[_Span],
    bounds: Sequence[int],
    limit: int,
    need: Mapping[str, object],
) -> int | None:
    """The last boundary a block whose last item is *span* can end at —
    before the next item line and *limit* — where that item's text is the
    recorded one (any, for an item with no recorded digest)."""
    stop = min(spans[index + 1].line if index + 1 < len(spans) else bounds[-1], limit)
    low = bisect.bisect_right(bounds, span.line)
    high = bisect.bisect_right(bounds, stop)
    for end in reversed(bounds[low:high]):
        if span.id not in need:
            return end
        digest = span.cut[end] if end < span.end else span.digest
        if digest == need[span.id]:
            return end
    return None


def _last_bound(bounds: Sequence[int], start: int, stop: int) -> int | None:
    at = bisect.bisect_right(bounds, stop) - 1
    return bounds[at] if at >= 0 and bounds[at] > start else None


def _expected(
    entry: Mapping[str, Any],
) -> tuple[frozenset[str], dict[str, object], bool] | None:
    """What *entry*'s block must hold, as :func:`matches_record` reads it:
    the ids, the digest each checked id's (last) text has, and whether ids
    must be unique. ``None``: no block can be its."""
    items = entry.get("items")
    recorded: dict[str, object] = {
        item["id"]: item.get("sha")
        for item in (items if isinstance(items, list) else ())
        if isinstance(item, Mapping) and isinstance(item.get("id"), str)
    }
    if entry.get("outcome") == OUTCOME_AUTO_APPENDED:
        return (frozenset(recorded), recorded, True) if recorded else None
    outcomes = entry.get("outcomes")
    if not isinstance(outcomes, Mapping):
        return None
    kept = frozenset(item_id for item_id, how in outcomes.items() if how != ITEM_CUT)
    need = {
        item_id: recorded.get(item_id)
        for item_id, how in outcomes.items()
        if how == ITEM_UNCHANGED
    }
    return kept, need, False


def matches_record(entry: Mapping[str, Any], items: tuple[BriefItem, ...]) -> bool:
    """Whether *items* are the addendum *entry* records: an auto-appended
    pass's ids and digests exactly; an approval's kept ids (every id its
    ``outcomes`` does not mark cut — none, for an approved *No change*), with
    each ``unchanged`` one's digest. An ``edited`` or ``added`` item's text
    has no recorded digest to check. The approval accepts an id the operator
    repeated and keys its outcome on the id's last text
    (:func:`item_outcomes`), so a repeated id is checked the same way — and
    every copy is listed."""
    expected = _expected(entry)
    if expected is None:
        return False
    allowed, need, unique = expected
    last = {item.id: item for item in items}
    if unique and len(last) != len(items):
        return False
    return set(last) == allowed and all(
        item_digest(item.text) == need[item_id]
        for item_id, item in last.items()
        if item_id in need
    )
