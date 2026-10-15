"""A story whose approved brief-review addendum overflows ``## What``.

The pilot's largest addendum (``lens-t3-w7.md``, 35 items in 13374 bytes)
rendered as the approval appends it, with the mark it records — so delivery
tests on every path (a run, a resumed run, a hand delivery) share one story
and one expectation.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from lithos_loom.plugins.story_develop.brief_review import (
    MODE_FULL,
    OUTCOME_APPROVED,
    draft_entry,
    item_outcomes,
    parse_addendum,
    render_addendum,
)
from lithos_loom.plugins.story_develop.brief_review_auto import item_heading
from lithos_loom.plugins.story_develop.brief_review_blocks import appended_mark

APPROVED_BASE = "c0ffee" + "0" * 34
_PILOT = Path(__file__).parent.parent / "fixtures" / "brief_review" / "lens-t3-w7.md"


def approved_addendum() -> tuple[str, dict[str, Any], tuple[str, ...]]:
    """``(text, entry, listed)``: the addendum as appended to the story's
    description, its ``approved`` record entry, and every item's
    ``"<id>. <heading>"`` line the PR body must list."""
    addendum = parse_addendum(_PILOT.read_text(encoding="utf-8"))
    text = render_addendum(
        addendum, base_sha=APPROVED_BASE, on=date(2026, 10, 14), mode=MODE_FULL
    ).strip()
    # as the pass drafts it and the approval settles it (nothing edited)
    entry = draft_entry(
        run_id="r0",
        mode=MODE_FULL,
        base_sha=APPROVED_BASE,
        addendum=addendum,
        drafted_at=datetime(2026, 10, 14, tzinfo=UTC),
        outcome=OUTCOME_APPROVED,
    )
    entry["gate_id"] = "g-1"
    entry["outcomes"] = item_outcomes(entry, addendum)
    entry["appended"] = appended_mark(text)
    listed = tuple(f"{item.id}. {item_heading(item)}" for item in addendum.items)
    return text, entry, listed
