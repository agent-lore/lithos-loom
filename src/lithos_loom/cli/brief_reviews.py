"""``lithos-loom brief-reviews`` — the brief-review record, read back.

Every dispatch-time brief review leaves an entry on its story's
``metadata.brief_review`` (:mod:`lithos_loom.plugins.story_develop.brief_review`):
what the pass drafted, how it settled, the structural ``check`` phase 2 ran
on the draft and, once approved, what the operator did to each item. This is
the read side, modelled on ``gates --shadow``: entries by outcome per project,
the approval edits by kind (a FACT edited or cut at a gate is the
``develop_brief_review_auto_facts`` tally's criterion 1), the entries that
skipped their gate under the dial, and the check's false-hold rate.

**Selection.** Stories are listed by ``metadata.project`` (every creation
path sets it; none tags every story), in every status, and each story's
entries are read with :func:`records_of`. ``--since`` keeps an entry by its
own ``drafted_at`` — never the story's ``updated_at``, which any later write
bumps. Read-only: one ``task_list`` call.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from lithos_loom.lithos_client import TaskClient
from lithos_loom.plugins.story_develop.brief_review import (
    ITEM_ADDED,
    ITEM_CUT,
    ITEM_EDITED,
    ITEM_UNCHANGED,
    KIND_DECISION,
    KIND_FACT,
    KIND_SCOPE_CUT,
    MODE_DELTA,
    MODE_FULL,
    OUTCOME_APPROVED,
    OUTCOME_AUTO_APPENDED,
    OUTCOME_FAILED,
    OUTCOME_NO_CHANGE,
    OUTCOME_PENDING,
    records_of,
)
from lithos_loom.subscriptions.dispatch_guards import project_of

__all__ = [
    "brief_reviews_output",
    "collect_brief_reviews",
    "false_holds",
    "render_brief_reviews",
]

_NO_VALUE = "—"
_OUTCOMES = (
    OUTCOME_PENDING,
    OUTCOME_APPROVED,
    OUTCOME_AUTO_APPENDED,
    OUTCOME_NO_CHANGE,
    OUTCOME_FAILED,
)
_ITEM_OUTCOMES = (ITEM_UNCHANGED, ITEM_EDITED, ITEM_CUT, ITEM_ADDED)
_KIND_LETTERS = {KIND_SCOPE_CUT: "S", KIND_FACT: "F", KIND_DECISION: "D"}
_LETTERS = ("S", "F", "D")
_SKIPPED_GATE = frozenset({OUTCOME_AUTO_APPENDED, OUTCOME_NO_CHANGE})


def _str(value: object) -> str:
    return value if isinstance(value, str) else ""


def _drafted(row: Mapping[str, Any]) -> datetime | None:
    """The entry's ``drafted_at``; a naive time is read as UTC, an unreadable
    one is ``None`` (outside any window)."""
    try:
        when = datetime.fromisoformat(_str(row.get("drafted_at")))
    except ValueError:
        return None
    return when.replace(tzinfo=UTC) if when.tzinfo is None else when


async def collect_brief_reviews(
    client: TaskClient,
    *,
    project: str | None = None,
    since: datetime | None = None,
) -> list[dict[str, Any]]:
    """One row per recorded entry: the entry as :func:`records_of` returns
    it, every key passed through, plus ``story_id`` and ``project`` (``—``
    for a story with none). Entries drafted before *since* (or with no
    readable ``drafted_at`` when *since* is given) are dropped. Ordered by
    story id, then record order. Read-only."""
    stories = await client.task_list(
        task_type="task",
        metadata_match={"project": project} if project is not None else None,
    )
    rows: list[dict[str, Any]] = []
    for story in sorted(stories, key=lambda s: s.id):
        meta: Mapping[str, Any] = story.metadata or {}
        slug = project_of(meta) or _NO_VALUE
        for entry in records_of(meta):
            row = {**entry, "story_id": story.id, "project": slug}
            if since is not None:
                drafted = _drafted(row)
                if drafted is None or drafted < since:
                    continue
            rows.append(row)
    return rows


def _check(row: Mapping[str, Any]) -> Mapping[str, Any] | None:
    check = row.get("check")
    return check if isinstance(check, Mapping) else None


def _held_facts_only(row: Mapping[str, Any]) -> bool:
    """A facts-only draft the check held at its gate."""
    check = _check(row)
    return (
        check is not None
        and check.get("facts_only") is True
        and check.get("passed") is False
    )


def _outcomes(row: Mapping[str, Any]) -> Mapping[str, Any]:
    outcomes = row.get("outcomes")
    return outcomes if isinstance(outcomes, Mapping) else {}


def false_holds(
    rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """``(held, false)``: the approved entries whose facts-only draft the
    check held, and those of them the operator approved with every item
    ``unchanged`` — the check held facts the operator agreed were facts. An
    entry with no ``check`` is in neither (pure)."""
    held = [
        row
        for row in rows
        if row.get("outcome") == OUTCOME_APPROVED and _held_facts_only(row)
    ]
    false = [
        row
        for row in held
        if _outcomes(row)
        and all(value == ITEM_UNCHANGED for value in _outcomes(row).values())
    ]
    return held, false


def _kinds(row: Mapping[str, Any]) -> dict[str, str]:
    """Each drafted item's kind letter by id, from its ``items[].kind``."""
    kinds: dict[str, str] = {}
    for item in row.get("items") or ():
        if isinstance(item, Mapping) and isinstance(item.get("id"), str):
            letter = _KIND_LETTERS.get(_str(item.get("kind")))
            if letter:
                kinds[item["id"]] = letter
    return kinds


def _kind_of(item_id: str, kinds: Mapping[str, str]) -> str:
    """The item's recorded kind, else (an ``added`` id has none) its letter."""
    return kinds.get(item_id) or (item_id[:1] if item_id[:1] in _LETTERS else "?")


def brief_reviews_output(rows: list[dict[str, Any]], *, as_json: bool) -> list[str]:
    """What the command prints: the rows as one JSON array (one object per
    entry), or the text report (pure)."""
    if as_json:
        return [json.dumps(rows, indent=2)]
    return render_brief_reviews(rows)


def render_brief_reviews(rows: list[dict[str, Any]]) -> list[str]:
    """The text report: entries by outcome, approval edits, auto-applied
    entries, check false holds (pure)."""
    if not rows:
        return ["no brief-review entries in the window"]
    stories = len({row["story_id"] for row in rows})
    lines = [f"{len(rows)} brief-review entries on {stories} stories", ""]
    lines += _outcome_table(rows)
    lines += ["", *_edits(rows)]
    lines += ["", *_auto_applied(rows)]
    lines += ["", *_false_hold_section(rows)]
    return lines


def _table(headers: tuple[str, ...], cells: list[tuple[str, ...]]) -> list[str]:
    widths = [
        max(len(headers[col]), *(len(cell[col]) for cell in cells))
        for col in range(len(headers))
    ]

    def _fmt(values: tuple[str, ...]) -> str:
        return "  ".join(v.ljust(widths[col]) for col, v in enumerate(values)).rstrip()

    return [_fmt(headers), *(_fmt(cell) for cell in cells)]


def _outcome_table(rows: list[dict[str, Any]]) -> list[str]:
    counts = Counter(
        (row["project"], _str(row.get("mode")) or _NO_VALUE, _str(row["outcome"]))
        for row in rows
    )
    keys = sorted({(project, mode) for project, mode, _ in counts})
    cells = []
    for project, mode in keys:
        known = [counts[(project, mode, outcome)] for outcome in _OUTCOMES]
        total = sum(n for (p, m, _), n in counts.items() if (p, m) == (project, mode))
        other = total - sum(known)
        cells.append(
            (
                project,
                mode,
                *(str(n) for n in known),
                str(other) if other else _NO_VALUE,
                str(total),
            )
        )
    headers = ("PROJECT", "MODE", *(o.upper() for o in _OUTCOMES), "OTHER", "TOTAL")
    return ["entries by outcome", *_table(headers, cells)]


def _edits(rows: list[dict[str, Any]]) -> list[str]:
    approved = [row for row in rows if row.get("outcome") == OUTCOME_APPROVED]
    counts: Counter[tuple[str, str]] = Counter()
    for row in approved:
        kinds = _kinds(row)
        for item_id, value in _outcomes(row).items():
            if value in _ITEM_OUTCOMES:
                counts[(_kind_of(str(item_id), kinds), value)] += 1
    letters = [*_LETTERS, *sorted({k for k, _ in counts} - set(_LETTERS))]
    cells = [
        (letter, *(str(counts[(letter, value)]) for value in _ITEM_OUTCOMES))
        for letter in letters
    ]
    facts = counts[("F", ITEM_EDITED)] + counts[("F", ITEM_CUT)]
    return [
        f"approval edits ({len(approved)} approved entries)",
        *_table(("KIND", *(v.upper() for v in _ITEM_OUTCOMES)), cells),
        f"facts edited or cut at a gate: {facts}",
    ]


def _check_label(row: Mapping[str, Any]) -> str:
    check = _check(row)
    if check is None:
        return "no check"
    shape = "facts-only" if check.get("facts_only") is True else "not facts-only"
    if check.get("passed") is True:
        return f"{shape}, passed"
    return f"{shape}, held: {_problems(check)}"


def _problems(check: Mapping[str, Any]) -> str:
    problems = check.get("problems")
    if not isinstance(problems, list) or not problems:
        return _NO_VALUE
    return "; ".join(str(problem) for problem in problems)


def _items(row: Mapping[str, Any]) -> str:
    ids = [
        _str(item.get("id"))
        for item in row.get("items") or ()
        if isinstance(item, Mapping)
    ]
    return ",".join(i for i in ids if i) or _NO_VALUE


def _entry_cells(row: Mapping[str, Any]) -> tuple[str, ...]:
    drafted = _drafted(row)
    return (
        _str(row["story_id"]),
        _str(row["project"]),
        _str(row.get("run_id")) or _NO_VALUE,
        drafted.date().isoformat() if drafted else _NO_VALUE,
        _str(row["outcome"]),
        _items(row),
        _check_label(row),
    )


_ENTRY_HEADERS = ("STORY", "PROJECT", "RUN", "DRAFTED", "OUTCOME", "ITEMS", "CHECK")


def _auto_applied(rows: list[dict[str, Any]]) -> list[str]:
    auto = [
        row
        for row in rows
        if row.get("mode") == MODE_FULL and row.get("outcome") in _SKIPPED_GATE
    ]
    delta = sum(
        1
        for row in rows
        if row.get("mode") == MODE_DELTA and row.get("outcome") in _SKIPPED_GATE
    )
    lines = [
        f"auto-applied (full mode, dial on): {len(auto)}"
        f" — delta rechecks that skipped their gate: {delta} (not dial-governed)"
    ]
    if auto:
        lines += _table(_ENTRY_HEADERS, [_entry_cells(row) for row in auto])
    return lines


def _false_hold_section(rows: list[dict[str, Any]]) -> list[str]:
    held, false = false_holds(rows)
    awaiting = [
        row
        for row in rows
        if row.get("outcome") == OUTCOME_PENDING and _held_facts_only(row)
    ]
    unchecked = sum(1 for row in rows if _check(row) is None)
    if held:
        head = (
            f"check false holds: {len(false)}/{len(held)} held facts-only drafts "
            f"approved all-unchanged ({len(false) / len(held):.0%})"
        )
    else:
        head = "check false holds: no held facts-only draft approved yet"
    lines = [head]
    lines += [
        f"  {row['story_id']} {_str(row.get('run_id')) or _NO_VALUE} — problems: "
        f"{_problems(_check(row) or {})}"
        for row in false
    ]
    if awaiting:
        lines.append(f"awaiting approval: {len(awaiting)} held facts-only drafts")
        lines += [
            f"  {row['story_id']} {_str(row.get('run_id')) or _NO_VALUE} — problems: "
            f"{_problems(_check(row) or {})}"
            for row in awaiting
        ]
    lines.append(
        f"no check: {unchecked} entries (drafted before phase 2, or failed) — "
        "outside the false-hold count"
    )
    return lines
