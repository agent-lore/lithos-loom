"""The Obsidian task-line grammar — the marker vocabulary shared by the
projection writer, the fs-watcher reader, and the import parser.

Historically each of those three sites re-encoded the same grammar: two
mutually-inverse priority-emoji tables kept honest by a cross-module
drift test, the ``🆔 lithos:<id>`` regex spelled out twice byte-for-byte,
and the priority / due-date marker shapes split across writer and reader.
This module owns that grammar in one place — the priority-enum ↔ emoji
bijection plus the render/parse inverse pairs for the markers all three
sites need: the 🆔 stable-id marker, the priority emoji, and the 📅
due-date marker.

Model: :mod:`lithos_loom.render_project_context` (render + parse as
explicit inverses in one module; a round-trip property test replaces the
per-table drift test).

Consumers:

* :mod:`lithos_loom.render` — projection writer. Composes a full task
  line from :func:`render_task_id`, the :data:`PRIORITY_EMOJI` table, and
  the ``📅 <date>`` format.
* :mod:`lithos_loom.sources.obsidian_fs_watcher` — reader of the
  projected file. Recovers ``(task_id, priority, due_date)`` from a line
  via :data:`TASK_ID_RE` + :func:`parse_priority` + :func:`parse_due_date`.
* :mod:`lithos_loom.task_line_parser` — import-doc parser. Reads
  operator-authored task lines; consumes :data:`EMOJI_TO_PRIORITY` for
  its precedence-ordered priority extraction.

Leaf module: stdlib only. No config / client / ``Task`` imports — the
grammar operates on strings, enums, and the trailing-metadata zone of a
line, not on domain objects. That keeps it importable from Foundation
(``task_line_parser``), Core (``render``), and a source
(``obsidian_fs_watcher``) alike without a cycle.

The priority tie-break differs by input domain and is deliberately NOT
unified here: the reader parses the projection's own output (at most one
priority emoji per line) with :func:`parse_priority`'s positional
first-match; the importer parses arbitrary operator markdown (possibly
several emoji) with a precedence-ordered first-match that strips all of
them. Both draw the same bijection from this module.
"""

from __future__ import annotations

import re

__all__ = [
    "EMOJI_TO_PRIORITY",
    "PRIORITY_EMOJI",
    "TASK_ID_RE",
    "extract_task_ids",
    "parse_due_date",
    "parse_priority",
    "render_task_id",
]


PRIORITY_EMOJI: dict[str, str] = {
    "highest": "🔺",
    "high": "⏫",
    "medium": "🔼",
    "low": "🔽",
    "lowest": "⏬",
}
"""Priority enum → Tasks-plugin emoji — the single source of truth.

Values: ``highest`` / ``high`` / ``medium`` / ``low`` / ``lowest``.
Declared highest → lowest; the order is load-bearing for the importer's
precedence tie-break (see :data:`EMOJI_TO_PRIORITY`). Strict
case-sensitive match: the Lithos surface owns this enum, and a
non-canonical value simply drops the marker rather than guessing."""


EMOJI_TO_PRIORITY: dict[str, str] = {
    emoji: enum for enum, emoji in PRIORITY_EMOJI.items()
}
"""Emoji → priority enum — the computed inverse of :data:`PRIORITY_EMOJI`.

Derived, not hand-maintained: the two tables can never disagree, so there
is no cross-module drift test any more — a single-table typo is caught by
the round-trip property test in ``tests/test_task_line.py``. Iteration
order mirrors :data:`PRIORITY_EMOJI` (highest → lowest), so a caller
iterating ``.items()`` for a first hit wins the highest priority — the
importer relies on this."""


# 🆔 lithos:<id> — the stable-identifier marker the projection writes
# immediately after the title so other lines (and re-writes) can recover
# which Lithos task a projected/archived line belongs to. Lithos ids are
# ``[A-Za-z0-9_-]``; the charset stops the match at a '.' / '/' / space.
TASK_ID_RE = re.compile(r"🆔 lithos:(?P<task_id>[A-Za-z0-9_-]+)")

# Any one of the five priority emoji. Derived from the table's values so
# it can never list an emoji the table doesn't map. Used to locate the
# priority marker in a line's trailing-metadata zone. (``re.escape`` is a
# no-op on the emoji — none are regex metacharacters — so this compiles
# to the literal ``(🔺|⏫|🔼|🔽|⏬)`` alternation.)
_PRIORITY_EMOJI_RE = re.compile(
    "(" + "|".join(re.escape(emoji) for emoji in PRIORITY_EMOJI.values()) + ")"
)

# 📅 YYYY-MM-DD — the Tasks-plugin due-date marker. Captures a
# ``YYYY-MM-DD`` substring after the emoji: a marker with no date-shaped
# text (``📅 next Friday``) reads as "no date", while a datetime-like
# edit (``📅 2026-06-15T09:00Z``) is normalized to its date *prefix*
# (``2026-06-15``) — the trailing time is left unmatched. That
# normalization is deliberate: the projection only ever writes the
# canonical ``YYYY-MM-DD`` (``render`` emits ``date.isoformat()``), so
# the prefix/reject distinction only bites a hand-edit, and recovering
# the date beats dropping it (dropping would push a due-date *removal*
# back to Lithos, losing the operator's date). The round-trip stays
# closed under the writer's own output.
_DUE_DATE_RE = re.compile(r"📅 (\d{4}-\d{2}-\d{2})")


def render_task_id(task_id: str) -> str:
    """Render the ``🆔 lithos:<id>`` stable-identifier marker.

    The inverse of :func:`extract_task_ids` / :data:`TASK_ID_RE`: for any
    id matching the Lithos charset,
    ``extract_task_ids(render_task_id(x)) == {x}``.
    """
    return f"🆔 lithos:{task_id}"


def extract_task_ids(text: str) -> set[str]:
    """Return every Lithos task id referenced by a ``🆔 lithos:<id>``
    marker anywhere in ``text``.

    Used to recover task identity from already-written vault content: the
    projection's restart seed (parse ``tasks.md``) and the task-archive
    dedup-cache load (parse ``<slug>-done.md``). Lines without the marker
    contribute nothing, so prose / headers / blank lines are inert.
    """
    return {m.group("task_id") for m in TASK_ID_RE.finditer(text)}


def parse_priority(zone: str) -> str | None:
    """Return the priority enum for the first priority emoji in ``zone``,
    or ``None`` when none is present.

    ``zone`` is a line's trailing-metadata region (the text after the
    ``🆔`` marker) — the caller scopes it so a priority emoji inside the
    title text can't be misread as the task's priority. This is the
    reader's positional first-match, valid because the projection emits at
    most one priority emoji per line (contrast the importer's
    precedence-ordered extraction that iterates :data:`EMOJI_TO_PRIORITY`).
    """
    m = _PRIORITY_EMOJI_RE.search(zone)
    return EMOJI_TO_PRIORITY[m.group(1)] if m else None


def parse_due_date(zone: str) -> str | None:
    """Return the ``YYYY-MM-DD`` string of the first ``📅`` marker in
    ``zone``, or ``None`` when no date-shaped substring follows a ``📅``.

    A datetime-like value is normalized to its date prefix
    (``📅 2026-06-15T09:00Z`` → ``"2026-06-15"``) rather than rejected —
    see :data:`_DUE_DATE_RE`. Yielded verbatim as a string; no further
    date parsing or validation here (that is the handler's job). Same
    trailing-metadata-zone scoping as :func:`parse_priority`.
    """
    m = _DUE_DATE_RE.search(zone)
    return m.group(1) if m else None
