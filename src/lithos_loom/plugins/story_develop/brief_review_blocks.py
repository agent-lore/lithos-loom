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
    "Found",
    "appended_mark",
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
_SHA_RE = re.compile(r"[0-9a-f]{16}")
# How much text one marked entry's search may hash, in multiples of the
# description's length. The search tries the recorded offset first, then
# only windows opening with the text's own first line (its recorded digest)
# — in practice its header, so a handful. Only many copies of that line
# could reach the bound; past it the entry is not found — the outcome a
# later edit of its text has.
_HASH_BUDGET = 8


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


@dataclass(frozen=True)
class Found:
    """An entry's block in the description. *exact*: found by the text the
    approval recorded appending (:func:`appended_mark`), so every item in
    it is what the operator approved; otherwise located by header, ids and
    digests (an entry from before approvals recorded it, or an
    auto-appended pass, whose ids and digests are exact anyway)."""

    addendum: Addendum
    exact: bool = False


def appended_mark(text: str, *, at: int | None = None) -> dict[str, object]:
    """What an approval records of the *text* it appends — its length and a
    digest of it exactly, a digest of its first line, and *at*, the offset
    it was appended at — so delivery finds that text, and only it, without
    hashing every window of the description."""
    mark: dict[str, object] = {
        "chars": len(text),
        "sha": _text_sha(text),
        "head": _text_sha(text.splitlines()[0] if text else ""),
    }
    if at is not None:
        mark["at"] = at
    return mark


def _text_sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class _Mark:
    chars: int
    sha: str
    head: str
    at: int | None


def _mark(entry: Mapping[str, Any]) -> _Mark | None:
    raw = entry.get("appended")
    if not isinstance(raw, Mapping):
        return None
    chars, sha, head, at = (raw.get(k) for k in ("chars", "sha", "head", "at"))
    if type(chars) is not int or chars <= 0:
        return None
    if not (isinstance(sha, str) and _SHA_RE.fullmatch(sha)):
        return None
    if not (isinstance(head, str) and _SHA_RE.fullmatch(head)):
        return None
    return _Mark(chars, sha, head, at if type(at) is int and at >= 0 else None)


def find_blocks(
    entries: Sequence[Mapping[str, Any]], description: str
) -> list[Found | None]:
    """Each entry's block in *description*, or ``None`` when no text there
    is the one its record vouches for.

    An approval records the text it appended (:func:`appended_mark`), so an
    entry carrying that mark is found by it — the newest such entry first,
    where it was appended or else the last place that text stands
    (:func:`_marked`) — and nothing else can stand in for
    it or cut it short: when that text is not there (a later edit), the
    entry is not found, never located by its header. The other entries (an
    auto-appended pass, whose ids and digests are exact; an approval from
    before the mark) are located by header, ids and digests
    (:func:`matches_record`):

    A candidate block opens at a header line under the entry's ``(title,
    base)`` and ends at a later candidate header or the description's end —
    every one is considered, so a header an item quotes, in a fence or not,
    ends nothing. The appenders only append, so the blocks stand in record
    order: oldest first, each entry pairs with the EARLIEST block under its
    header past the block the entry before it found (and outside any found
    by its mark), ending at the first end its record vouches for — so a
    later entry is not crowded out, and a header an item quotes lies inside
    the block that quotes it. Each such block then runs on to the last end
    its record vouches for before the next found block (or the
    description's end), so none is cut short. Text before the addenda under
    an entry's header can open such a block; ``brief_review_auto`` flags
    what that could hide.

    An item opens and closes at the same lines whichever block it is parsed
    in, so the description is parsed once (:func:`_spans`) and each candidate
    is checked against the record from those spans (:class:`_Fit`); only a
    block that passes is parsed. The work is linear per candidate header,
    whatever the description holds."""
    lines = description.splitlines()
    offsets = [0]
    for line in description.splitlines(keepends=True):
        offsets.append(offsets[-1] + len(line))
    found: list[Found | None] = [None] * len(entries)
    blocks = _marked(entries, description, lines, offsets)
    for index, (start, end) in blocks.items():
        text = description[offsets[start] : offsets[end]]
        found[index] = Found(parse_addendum(text), exact=True)
    marked = list(blocks.values())
    heads: list[tuple[int, tuple[str, str]]] = []
    for index, line in enumerate(lines):
        header = ADDENDUM_HEADER_RE.match(line)
        if header and (index == 0 or not lines[index - 1].strip()):
            key = (header.group("title").lower(), header.group("base").lower())
            heads.append((index, key))
    bounds = [index for index, _ in heads] + [len(lines)]
    spans = _spans(lines, parse_addendum(description).items, set(bounds))

    def parsed(index: int, start: int, end: int) -> Found | None:
        addendum = parse_addendum(description[offsets[start] : offsets[end]])
        ok = matches_record(entries[index], addendum.items)
        return Found(addendum) if ok else None

    fits = [_Fit(entry, spans, bounds) for entry in entries]
    floor = 0
    for index, fit in enumerate(fits):
        if index in blocks:
            floor = max(floor, blocks[index][1])
            continue
        if _mark(entries[index]) is not None:
            continue  # its recorded text is gone: nothing else stands in
        for start, head_key in heads:
            if start < floor or head_key != fit.key:
                continue
            if any(a <= start < b for a, b in marked):
                continue
            limit = min((a for a, _ in marked if a > start), default=len(lines))
            for end in fit.ends(start, limit, longest=False):
                if (block := parsed(index, start, end)) is not None:
                    found[index], blocks[index], floor = block, (start, end), end
                    break
            if index in blocks:
                break
    order = sorted(blocks, key=lambda index: blocks[index])
    for at, index in enumerate(order):
        start, end = blocks[index]
        if (block := found[index]) is None or block.exact:
            continue
        limit = blocks[order[at + 1]][0] if at + 1 < len(order) else len(lines)
        for longer in fits[index].ends(start, limit, longest=True):
            if longer <= end:
                break
            if (block := parsed(index, start, longer)) is not None:
                found[index] = block
                break
    return found


def _marked(
    entries: Sequence[Mapping[str, Any]],
    description: str,
    lines: Sequence[str],
    offsets: Sequence[int],
) -> dict[int, tuple[int, int]]:
    """The ``(start, end)`` lines of each marked entry's appended text,
    newest entry first, outside every block already found: where it was
    appended, when it still stands there whole; else at the last line start
    where it stands whole, ending a line. Only a window opening with the
    text's own first line is hashed whole, within :data:`_HASH_BUDGET`, so
    the work stays linear in the description however text appended after it
    is shaped."""
    blocks: dict[int, tuple[int, int]] = {}
    for index in reversed(range(len(entries))):
        if (mark := _mark(entries[index])) is None:
            continue
        budget = _HASH_BUDGET * len(description)
        recorded = bisect.bisect_left(offsets, mark.at) if mark.at is not None else -1
        if 0 <= recorded < len(lines) and offsets[recorded] == mark.at:
            starts = [recorded]
        else:
            starts = []
        for start in [*starts, *reversed(range(len(lines)))]:
            stop = offsets[start] + mark.chars
            if stop > len(description) or description[stop : stop + 1] not in (
                "",
                "\n",
                "\r",
            ):
                continue
            end = min(bisect.bisect_right(offsets, stop), len(offsets) - 1)
            if any(start < b and end > a for a, b in blocks.values()):
                continue
            if _text_sha(lines[start]) != mark.head:
                continue
            budget -= mark.chars
            if budget < 0:
                break  # not found: see _HASH_BUDGET
            if _text_sha(description[offsets[start] : stop]) == mark.sha:
                blocks[index] = (start, end)
                break
    return blocks


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


class _Fit:
    """Which blocks *entry*'s record vouches for, checked from *spans* the
    way :func:`matches_record` checks a parse (ids, and digests where the
    record has them).

    Whether a block whose last item is ``spans[p]`` holds the recorded text
    does not depend on where it starts, past its ids all being seen
    (:func:`_scan`): so each candidate start is a search, not a walk."""

    def __init__(
        self, entry: Mapping[str, Any], spans: Sequence[_Span], bounds: Sequence[int]
    ) -> None:
        title, base = entry_header(entry)
        expected = _expected(entry)
        self.key = (title.lower(), base) if title and base and expected else None
        self.allowed, self.need, self.unique = expected or (frozenset(), {}, False)
        self.spans, self.bounds = spans, bounds
        self.span_lines = [span.line for span in spans]
        # the next item whose id the record lacks
        self.beyond = [len(spans)] * (len(spans) + 1)
        for index in reversed(range(len(spans))):
            lacks = spans[index].id not in self.allowed
            self.beyond[index] = index if lacks else self.beyond[index + 1]
        self.ok, self.least = _scan(spans, self.allowed, self.need)
        self.tables: dict[tuple[int, bool], tuple[list[int], list[int]]] = {}

    def ends(self, start: int, limit: int, *, longest: bool) -> Iterator[int]:
        """The ends, at most *limit*, of the blocks from *start* the record
        can vouch for — the first one first, or the *longest*."""
        first = bisect.bisect_left(self.span_lines, start)
        if not self.allowed:  # an approved No change: a block with no items
            stop = self.span_lines[first] if first < len(self.spans) else limit
            window = self.bounds[
                bisect.bisect_right(self.bounds, start) : bisect.bisect_right(
                    self.bounds, min(stop, limit)
                )
            ]
            yield from reversed(window) if longest else window
            return
        if (limit, longest) not in self.tables:
            self.tables[limit, longest] = _ends(
                self.spans, self.bounds, self.ok, self.need, limit, longest
            )
        last_items, last_ends = self.tables[limit, longest]
        stop = min(self.beyond[first], bisect.bisect_left(self.span_lines, limit))
        # the block's ids all seen by its last item; exactly once, if unique
        low = bisect.bisect_left(self.least, first)
        if self.unique:
            low = max(low, first + len(self.allowed) - 1)
            stop = min(stop, first + len(self.allowed))
        if longest:
            at = bisect.bisect_left(last_items, stop) - 1
            while at >= 0 and last_items[at] >= low:
                yield last_ends[at]
                at -= 1
        else:
            at = bisect.bisect_left(last_items, low)
            while at < len(last_items) and last_items[at] < stop:
                yield last_ends[at]
                at += 1


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
    longest: bool,
) -> tuple[list[int], list[int]]:
    """Each item a block ending before *limit* can end with, and where (its
    first end, or *longest*) — linear: the items' end windows do not
    overlap."""
    items: list[int] = []
    ends: list[int] = []
    for index, span in enumerate(spans):
        if span.line >= limit:
            break
        if ok[index]:
            end = _span_end(span, index, spans, bounds, limit, need, longest)
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
    longest: bool,
) -> int | None:
    """The first (or *longest*: the last) boundary a block whose last item is
    *span* can end at — before the next item line and *limit* — where that
    item's text is the recorded one (any, for an item with no recorded
    digest)."""
    stop = min(spans[index + 1].line if index + 1 < len(spans) else bounds[-1], limit)
    low = bisect.bisect_right(bounds, span.line)
    high = bisect.bisect_right(bounds, stop)
    window = bounds[low:high]
    for end in reversed(window) if longest else window:
        if span.id not in need:
            return end
        digest = span.cut[end] if end < span.end else span.digest
        if digest == need[span.id]:
            return end
    return None


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
