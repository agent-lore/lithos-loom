"""Where each brief-review addendum is in a story's description.

A record entry (``metadata.brief_review``) keeps its items' ids and digests,
not their text (``draft_entry``). So each appender — the approval
(``brief_review_approval``) and the phase's auto path
(``brief_review_phase``) — records on the entry the exact text it appended
(:func:`appended_mark`), and the PR body's ``## Brief review``
(``brief_review_auto.brief_review_section``) reads each entry's items from
that text and nothing else (:func:`find_blocks`).

There is no search by header: a header line, an item id or a digest can be
quoted, copied or forged anywhere in a description, and no rule over them
tells an addendum from a copy of one. An entry without the mark (recorded
before the appenders kept it) is listed by its record's ids.
"""

from __future__ import annotations

import bisect
import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .brief_review import MODE_DELTA, MODE_FULL, Addendum, parse_addendum

__all__ = [
    "MODE_TITLE",
    "appended_mark",
    "entry_header",
    "find_blocks",
]

# How ``render_addendum``'s header names each mode — loom's line for an
# entry in the PR body names it the same way, with the base's first 12 hex
# digits.
MODE_TITLE: dict[str, str] = {MODE_FULL: "Brief review", MODE_DELTA: "Recheck"}
_BASE_SHA_RE = re.compile(r"[0-9a-f]{12,64}")
_SHA_RE = re.compile(r"[0-9a-f]{16}")

# How much text one marked entry's search may hash, in multiples of the
# description's length. The search tries the recorded offset first, then
# only windows the text's recorded shape fits exactly — its length, its line
# count, its first and its last line — so ordinary text, quoting the
# header however often, offers none but the text itself. Only text crafted
# to that shape could reach the bound; past it the entry is not found — the
# outcome a later edit of its text has.
_HASH_BUDGET = 8


def entry_header(entry: Mapping[str, Any]) -> tuple[str | None, str | None]:
    """The mode title and ``base_sha[:12]`` loom's line names *entry* by,
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


def appended_mark(text: str, *, at: int | None = None) -> dict[str, object]:
    """What an appender records of the *text* it appends — its length and a
    digest of it exactly; its line count and a digest of its first and of
    its last line; and *at*, its offset in the description as stripped (the
    body ``task_text`` carries after the title) — so delivery finds that
    text, and only it, without hashing every window of the description."""
    lines = text.splitlines() or [""]
    mark: dict[str, object] = {
        "chars": len(text),
        "sha": _text_sha(text),
        "lines": len(lines),
        "head": _text_sha(lines[0]),
        "tail": _text_sha(lines[-1]),
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
    lines: int
    head: str
    tail: str
    at: int | None


def _mark(entry: Mapping[str, Any]) -> _Mark | None:
    raw = entry.get("appended")
    if not isinstance(raw, Mapping):
        return None
    chars, sha, lines, head, tail, at = (
        raw.get(k) for k in ("chars", "sha", "lines", "head", "tail", "at")
    )
    if type(chars) is not int or chars <= 0 or type(lines) is not int or lines <= 0:
        return None
    digests = (sha, head, tail)
    if not all(isinstance(d, str) and _SHA_RE.fullmatch(d) for d in digests):
        return None
    return _Mark(
        chars,
        str(sha),
        lines,
        str(head),
        str(tail),
        at if type(at) is int and at >= 0 else None,
    )


def find_blocks(
    entries: Sequence[Mapping[str, Any]], description: str, *, body_at: int = 0
) -> list[Addendum | None]:
    """Each entry's addendum in *description*: the text its appender
    recorded appending (:func:`appended_mark`), parsed — or ``None`` when
    the entry has no mark (recorded before the appenders kept one) or that
    text is no longer there whole (edited since). *body_at*: where the
    story's description, stripped, starts in *description* — the origin of
    the recorded offset.

    The newest entry is placed first, where it was appended or else at the
    last place its text stands, outside the blocks already found
    (:func:`_marked`); nothing else in the description can stand in for an
    entry's text or cut it short."""
    lines = description.splitlines()
    offsets = [0]
    for line in description.splitlines(keepends=True):
        offsets.append(offsets[-1] + len(line))
    found: list[Addendum | None] = [None] * len(entries)
    for index, (start, end) in _marked(
        entries, description, lines, offsets, body_at
    ).items():
        found[index] = parse_addendum(description[offsets[start] : offsets[end]])
    return found


def _marked(
    entries: Sequence[Mapping[str, Any]],
    description: str,
    lines: Sequence[str],
    offsets: Sequence[int],
    body_at: int,
) -> dict[int, tuple[int, int]]:
    """The ``(start, end)`` lines of each marked entry's appended text,
    newest entry first, outside every block already found: where it was
    appended (its recorded offset from *body_at*, where the stripped
    description starts), when it still stands there whole; else at the last
    line start where it stands whole. Only a window the recorded shape fits
    — the text's length, line count, first and last line — is hashed whole,
    within :data:`_HASH_BUDGET`; each line's digest is taken once, and an
    entry visits only the lines its first line's digest names, so the work
    stays linear in the description however it is shaped."""
    marks = {index: mark for index, e in enumerate(entries) if (mark := _mark(e))}
    if not marks:
        return {}
    digest_of: dict[str, str] = {}
    starts_of: dict[str, list[int]] = {}
    for number, line in enumerate(lines):
        if (digest := digest_of.get(line)) is None:
            digest = digest_of[line] = _text_sha(line)
        starts_of.setdefault(digest, []).append(number)
    blocks: dict[int, tuple[int, int]] = {}

    def fits(mark: _Mark, start: int) -> int | None:
        """The line after the text, when *mark*'s shape fits from *start*."""
        end = start + mark.lines
        if end > len(lines) or digest_of[lines[end - 1]] != mark.tail:
            return None
        if offsets[end - 1] + len(lines[end - 1]) != offsets[start] + mark.chars:
            return None
        if any(start < b and end > a for a, b in blocks.values()):
            return None
        return end

    for index in sorted(marks, reverse=True):
        mark = marks[index]
        budget = _HASH_BUDGET * len(description)
        candidates = starts_of.get(mark.head, [])
        at = None if mark.at is None else body_at + mark.at
        recorded = bisect.bisect_left(candidates, bisect.bisect_left(offsets, at or 0))
        first = (
            [candidates[recorded]]
            if at is not None
            and recorded < len(candidates)
            and offsets[candidates[recorded]] == at
            else []
        )
        for start in [*first, *reversed(candidates)]:
            if (end := fits(mark, start)) is None:
                continue
            budget -= mark.chars
            if budget < 0:
                break  # not found: see _HASH_BUDGET
            if _text_sha(description[offsets[start] : offsets[start] + mark.chars]) == (
                mark.sha
            ):
                blocks[index] = (start, end)
                break
    return blocks
