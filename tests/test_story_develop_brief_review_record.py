"""Brief review at dispatch (604fb936), PR 2: the records a review leaves on
its story, and what the next dispatch decides from them.

``metadata.brief_review`` is a list of entries, newest last — one per pass —
and phase 2 (10cc6310) decides from them whether a facts-only addendum may
skip the gate. ``metadata.brief_review_hold`` is the flag that reserves the
story's admission slot while it waits at its gate.
"""

from __future__ import annotations

from datetime import UTC, datetime

from lithos_loom.plugins.story_develop.brief_review import (
    ITEM_ADDED,
    ITEM_CUT,
    ITEM_EDITED,
    ITEM_UNCHANGED,
    MODE_DELTA,
    MODE_FULL,
    OUTCOME_APPROVED,
    OUTCOME_AUTO_APPENDED,
    OUTCOME_FAILED,
    OUTCOME_NO_CHANGE,
    OUTCOME_PENDING,
    PLAN_DELTA,
    PLAN_FULL,
    PLAN_PROCEED,
    draft_entry,
    item_digest,
    item_outcomes,
    latest_entry,
    parse_addendum,
    plan_review,
    records_of,
)

S1 = "a" * 40
S2 = "b" * 40
WHEN = datetime(2026, 10, 5, 20, 0, tzinfo=UTC)

_DRAFT = """\
**Facts**

- **F1. One.** `a.py:1`.
- **F2. Two.** `b.py:2`.

**Decisions**

- **D1. Do it.** this way.
  - Basis: F1.
"""


def test_a_digest_ignores_whitespace_only_changes() -> None:
    assert item_digest("**F1. One.**  `a.py:1`.") == item_digest(
        "**F1. One.**\n  `a.py:1`."
    )
    assert item_digest("**F1. One.** `a.py:1`.") != item_digest(
        "**F1. One.** `a.py:2`."
    )


def test_a_draft_entry_records_each_item_by_id_kind_and_digest() -> None:
    entry = draft_entry(
        run_id="r1",
        mode=MODE_FULL,
        base_sha=S1,
        addendum=parse_addendum(_DRAFT),
        drafted_at=WHEN,
        outcome=OUTCOME_PENDING,
    )

    assert entry["run_id"] == "r1" and entry["mode"] == MODE_FULL
    assert entry["base_sha"] == S1 and entry["outcome"] == OUTCOME_PENDING
    assert entry["drafted_at"] == "2026-10-05T20:00:00+00:00"
    assert [(i["id"], i["kind"]) for i in entry["items"]] == [
        ("F1", "fact"),
        ("F2", "fact"),
        ("D1", "decision"),
    ]
    assert all(len(i["sha"]) == 16 for i in entry["items"])


def test_item_outcomes_compare_the_approved_text_with_the_draft() -> None:
    entry = draft_entry(
        run_id="r1",
        mode=MODE_FULL,
        base_sha=S1,
        addendum=parse_addendum(_DRAFT),
        drafted_at=WHEN,
        outcome=OUTCOME_PENDING,
    )
    approved = parse_addendum(
        "**Facts**\n\n- **F1. One.**  `a.py:1`.\n\n"  # whitespace only: unchanged
        "**Decisions**\n\n- **D1. Do it.** another way.\n  - Basis: F1.\n"  # edited
        "- **D2. New.** added by the operator.\n  - Basis: F1.\n"  # added
    )  # F2 deleted: cut

    assert item_outcomes(entry, approved) == {
        "F1": ITEM_UNCHANGED,
        "F2": ITEM_CUT,
        "D1": ITEM_EDITED,
        "D2": ITEM_ADDED,
    }


def test_records_of_reads_only_well_formed_entries() -> None:
    good = {"run_id": "r1", "outcome": OUTCOME_APPROVED, "base_sha": S1}
    assert records_of({"brief_review": [good, "junk", {"no": "outcome"}]}) == [good]
    assert records_of({"brief_review": "not a list"}) == []
    assert records_of({}) == []
    assert latest_entry({"brief_review": [good]}) == good
    assert latest_entry({}) is None


def _meta(*entries: dict) -> dict:
    return {"brief_review": list(entries)}


def test_plan_a_never_reviewed_story_gets_a_full_review() -> None:
    assert plan_review({}, S1) == (PLAN_FULL, None)


def test_plan_an_approved_review_at_the_same_base_proceeds() -> None:
    meta = _meta({"run_id": "r1", "outcome": OUTCOME_APPROVED, "base_sha": S1})
    assert plan_review(meta, S1) == (PLAN_PROCEED, None)


def test_plan_a_moved_base_rechecks_against_the_last_settled_base() -> None:
    for settled in (OUTCOME_APPROVED, OUTCOME_AUTO_APPENDED, OUTCOME_NO_CHANGE):
        meta = _meta({"run_id": "r1", "outcome": settled, "base_sha": S1})
        assert plan_review(meta, S2) == (PLAN_DELTA, S1), settled


def test_plan_a_pending_or_failed_latest_entry_reviews_from_scratch() -> None:
    # pending at dispatch: the approval was never applied (a cancelled gate);
    # failed: the pass produced no draft. Neither is a review to build on.
    for unsettled in (OUTCOME_PENDING, OUTCOME_FAILED):
        meta = _meta({"run_id": "r1", "outcome": unsettled, "base_sha": S1})
        assert plan_review(meta, S1) == (PLAN_FULL, None), unsettled


def test_plan_a_delta_after_an_earlier_approval_uses_the_newest_settled_base() -> None:
    meta = _meta(
        {
            "run_id": "r1",
            "outcome": OUTCOME_APPROVED,
            "base_sha": S1,
            "mode": MODE_FULL,
        },
        {
            "run_id": "r2",
            "outcome": OUTCOME_AUTO_APPENDED,
            "base_sha": S2,
            "mode": MODE_DELTA,
        },
    )
    assert plan_review(meta, S2) == (PLAN_PROCEED, None)
    assert plan_review(meta, "c" * 40) == (PLAN_DELTA, S2)
