"""Brief review at dispatch (604fb936), PR 2: the approval on the dispatch path.

The operator approves a held story's addendum by completing its brief-review
gate, after editing the draft between the gate description's two fences if
they want to. Readiness flips the moment the gate completes, so the approval
is applied where every dispatch passes — after the claim, before
``task.json`` is written: the fenced text is appended to the story verbatim
in ONE optimistic-lock write that also settles the record (per-item
outcomes, the operator's completion note) and drops the slot reservation.
A gate whose fences were edited away fails closed: a fresh brief-review gate,
never a guess at which part of the description is the addendum.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from typing import Any

import pytest

from lithos_loom.errors import LithosClientError
from lithos_loom.gates import BRIEF_REVIEW_REASON, create_human_gate
from lithos_loom.lithos_client import Task
from lithos_loom.plugins.story_develop.brief_review import (
    HOLD_KEY,
    ITEM_ADDED,
    ITEM_CUT,
    ITEM_EDITED,
    ITEM_UNCHANGED,
    MODE_FULL,
    OUTCOME_APPROVED,
    OUTCOME_FAILED,
    OUTCOME_PENDING,
    RECORD_KEY,
    draft_entry,
    parse_addendum,
)
from lithos_loom.subscriptions.brief_review_approval import apply_brief_approval
from tests.support import FakeLithosClient, make_task

STORY = "story-1"
RUN = "held0001"
BRIEF = "Slice W8. Build the relation sentences."
NOW = datetime(2026, 10, 5, 21, 0, tzinfo=UTC)
_DRAFT = """\
**Brief review against `aaaaaaaaaaaa` (2026-10-05)** — checked.

**Facts** (the code at `aaaaaaaaaaaa` — no judgment involved)

- **F1. One.** `a.py:1`.
- **F2. Two.** `b.py:2`.

**Decisions**

- **D1. Do it.** this way.
  - Basis: F1."""


async def _held(
    client: FakeLithosClient, *, run_id: str = RUN, draft: str = _DRAFT
) -> str:
    """A story held for review: its pending entry + hold flag, and its gate."""
    entry = draft_entry(
        run_id=run_id,
        mode=MODE_FULL,
        base_sha="a" * 40,
        addendum=parse_addendum(draft),
        drafted_at=datetime(2026, 10, 5, 20, 0, tzinfo=UTC),
        outcome=OUTCOME_PENDING,
    )
    await client.task_update(
        task_id=STORY,
        metadata={RECORD_KEY: [entry], HOLD_KEY: True},
    )
    return await create_human_gate(
        client,
        story_id=STORY,
        story_title="T3-W8",
        project="lithos-lens",
        agent="loom",
        route="story-develop",
        reason=BRIEF_REVIEW_REASON,
        summary="brief review at aaaaaaaaaaaa: 2 facts, 1 decision",
        run_id=run_id,
        brief={"base_sha": "a" * 40, "mode": MODE_FULL, "addendum": draft},
    )


async def _get(client: Any, task_id: str) -> Task:
    task = await client.task_get(task_id=task_id)
    assert task is not None
    return task


def _client() -> FakeLithosClient:
    story = make_task(
        STORY,
        title="T3-W8",
        description=BRIEF,
        metadata={"project": "lithos-lens", "prd": "docs/prd/t3.md"},
        tags=("trigger:story-develop",),
    )
    return FakeLithosClient(tasks=(story,), agent_id="loom")


async def _edit_gate(
    client: FakeLithosClient, gate_id: str, old: str, new: str
) -> None:
    gate = await _get(client, gate_id)
    assert gate is not None and gate.description is not None and old in gate.description
    await client.task_update(
        task_id=gate_id, description=gate.description.replace(old, new)
    )


async def _apply(client: Any) -> Any:
    story = await _get(client, STORY)
    payload = {"id": STORY, "metadata": dict(story.metadata)}
    return await apply_brief_approval(
        client,
        task_id=STORY,
        route="story-develop",
        agent="loom",
        payload=payload,
        now=NOW,
    )


async def test_an_approved_draft_is_appended_verbatim_and_settled() -> None:
    client = _client()
    gate_id = await _held(client)
    await client.task_complete(task_id=gate_id, agent="dave")

    outcome = await _apply(client)

    assert outcome.applied and outcome.refused is None
    story = await _get(client, STORY)
    assert story.description == f"{BRIEF}\n\n{_DRAFT}\n"
    # task.json is written from this payload: the coder gets the addendum
    assert outcome.payload["description"] == story.description
    entry = story.metadata[RECORD_KEY][-1]
    assert entry["outcome"] == OUTCOME_APPROVED
    assert entry["approved_at"] == NOW.isoformat()
    assert entry["gate_id"] == gate_id
    assert entry["outcomes"] == {
        "F1": ITEM_UNCHANGED,
        "F2": ITEM_UNCHANGED,
        "D1": ITEM_UNCHANGED,
    }
    # what and where the text is (security/f-003, converge round 3)
    block = _DRAFT.strip()
    at = (story.description or "").index(block)
    assert entry["appended"]["at"] == at == len(BRIEF) + 2
    assert entry["appended"]["chars"] == len(block)
    assert HOLD_KEY not in story.metadata  # the slot is the run's now


async def test_the_operator_s_edits_are_what_is_appended_and_recorded() -> None:
    client = _client()
    gate_id = await _held(client)
    await _edit_gate(client, gate_id, "- **F2. Two.** `b.py:2`.\n", "")  # cut
    await _edit_gate(client, gate_id, "this way.", "that way.")  # edit
    await _edit_gate(
        client,
        gate_id,
        "  - Basis: F1.",
        "  - Basis: F1.\n- **D2. Added.** by the operator.\n  - Basis: F1.",
    )
    await client.task_complete(task_id=gate_id, agent="dave")
    # Lens's optional completion note travels on the gate's outcome
    client._tasks[gate_id] = dataclasses.replace(
        client._tasks[gate_id], outcome="cut F2; D1 the other way"
    )

    await _apply(client)

    story = await _get(client, STORY)
    text = story.description or ""
    assert "that way." in text and "this way." not in text
    assert "**F2. Two.**" not in text
    assert "**D2. Added.**" in text
    entry = story.metadata[RECORD_KEY][-1]
    assert entry["outcomes"] == {
        "F1": ITEM_UNCHANGED,
        "F2": ITEM_CUT,
        "D1": ITEM_EDITED,
        "D2": ITEM_ADDED,
    }
    assert entry["operator_note"] == "cut F2; D1 the other way"


async def test_an_applied_approval_is_what_the_pr_body_lists() -> None:
    # The PR body's `## Brief review` verifies each block against its record:
    # the operator's edited approval, as appended, must still match it.
    from lithos_loom.plugins.story_develop.brief_review import records_of
    from lithos_loom.plugins.story_develop.brief_review_auto import (
        brief_review_section,
    )

    client = _client()
    gate_id = await _held(client)
    await _edit_gate(client, gate_id, "- **F2. Two.** `b.py:2`.\n", "")
    await _edit_gate(client, gate_id, "this way.", "that way.")
    await client.task_complete(task_id=gate_id, agent="dave")

    outcome = await _apply(client)

    story = await _get(client, STORY)
    section = brief_review_section(
        records_of(story.metadata), outcome.payload["description"]
    )
    assert "\nF1. One.\nD1. Do it.\n" in section
    assert "F2." not in section and "not found" not in section


async def test_an_approval_repeating_an_id_reaches_the_pr_body_whole() -> None:
    # correctness/f-002 (round 3): the approval appends an operator's
    # repeated id unvalidated, recording it `edited`; the PR body still lists
    # the whole addendum — both copies and the unchanged items.
    from lithos_loom.plugins.story_develop.brief_review import records_of
    from lithos_loom.plugins.story_develop.brief_review_auto import (
        brief_review_section,
    )

    client = _client()
    gate_id = await _held(client)
    await _edit_gate(
        client,
        gate_id,
        "  - Basis: F1.",
        "  - Basis: F1.\n- **D1. Also expose the new endpoint.** too.\n  - Basis: F1.",
    )
    await client.task_complete(task_id=gate_id, agent="dave")

    outcome = await _apply(client)

    assert outcome.applied
    story = await _get(client, STORY)
    assert story.metadata[RECORD_KEY][-1]["outcomes"]["D1"] == ITEM_EDITED
    section = brief_review_section(
        records_of(story.metadata), "x" * 8000 + outcome.payload["description"]
    )
    assert "\nF1. One.\nF2. Two.\nD1. Do it.\nD1. Also expose the new endpoint.\n" in (
        section
    )
    assert "not found" not in section


async def test_a_repeated_id_past_a_quoted_header_reaches_the_pr_body() -> None:
    # correctness/f-001 (round 2): the shorter block ending at the quoted
    # header already holds every kept id — the whole approved block, with
    # the repeated D1 after the header, must still be the one listed.
    from lithos_loom.plugins.story_develop.brief_review import records_of
    from lithos_loom.plugins.story_develop.brief_review_auto import (
        brief_review_section,
    )

    client = _client()
    gate_id = await _held(client)
    await _edit_gate(
        client,
        gate_id,
        "  - Basis: F1.",
        "  - Basis: F1.\n\n**Recheck against `bbbbbbbbbbbb` (2026-10-14)** — quoted"
        " example.\n\n- **D1. Also expose the new endpoint.** Too.\n  - Basis: F1.",
    )
    await client.task_complete(task_id=gate_id, agent="dave")

    outcome = await _apply(client)

    story = await _get(client, STORY)
    assert story.metadata[RECORD_KEY][-1]["outcomes"]["D1"] == ITEM_EDITED
    section = brief_review_section(
        records_of(story.metadata), "x" * 8000 + outcome.payload["description"]
    )
    assert "\nF1. One.\nF2. Two.\nD1. Do it.\nD1. Also expose the new endpoint.\n" in (
        section
    )
    assert "not found" not in section


@pytest.mark.parametrize("heading", ["", "**Decisions**\n\n"])
async def test_a_quote_of_the_block_s_own_header_stands_in_for_no_part_of_it(
    heading: str,
) -> None:
    # correctness/f-001 (rounds 3, 4): a quoted copy of the block's OWN
    # header (same mode and base) between two copies of a repeated kept id,
    # with or without a section heading after it: the suffix from the quote
    # holds every kept id too, but the whole block — both D1s — is listed.
    from lithos_loom.plugins.story_develop.brief_review import records_of
    from lithos_loom.plugins.story_develop.brief_review_auto import (
        brief_review_section,
    )

    client = _client()
    gate_id = await _held(client)
    await _edit_gate(client, gate_id, "- **F1. One.** `a.py:1`.\n", "")
    await _edit_gate(client, gate_id, "- **F2. Two.** `b.py:2`.\n", "")
    await _edit_gate(
        client,
        gate_id,
        "  - Basis: F1.",
        "  - Basis: F1.\n\n**Brief review against `aaaaaaaaaaaa` (2026-10-14)** —"
        f" quoted example.\n\n{heading}- **D1. Also expose the new endpoint.** Too.\n"
        "  - Basis: F1.",
    )
    await client.task_complete(task_id=gate_id, agent="dave")

    outcome = await _apply(client)

    story = await _get(client, STORY)
    outcomes = story.metadata[RECORD_KEY][-1]["outcomes"]
    assert outcomes == {"F1": ITEM_CUT, "F2": ITEM_CUT, "D1": ITEM_EDITED}
    section = brief_review_section(
        records_of(story.metadata), "x" * 8000 + outcome.payload["description"]
    )
    assert "\nD1. Do it.\nD1. Also expose the new endpoint.\n" in section
    assert "not found" not in section


_HEADER = "**Brief review against `aaaaaaaaaaaa` (2026-10-14)**"


def _decisions(*titles: str) -> str:
    items = "\n".join(
        f"- **D{i}. {title}** x\n  - Basis: F1." for i, title in enumerate(titles, 1)
    )
    return f"{_HEADER} — checked.\n\n**Decisions**\n\n{items}"


async def _approve_next(client: FakeLithosClient, run_id: str, draft: str, edited: str):
    """Hold the story again under *draft* (its records kept), approve the
    gate with the operator's *edited* text, and apply it."""
    story = await _get(client, STORY)
    entry = draft_entry(
        run_id=run_id,
        mode=MODE_FULL,
        base_sha="a" * 40,
        addendum=parse_addendum(draft),
        drafted_at=datetime(2026, 10, 5, 20, 0, tzinfo=UTC),
        outcome=OUTCOME_PENDING,
    )
    await client.task_update(
        task_id=STORY,
        metadata={
            RECORD_KEY: [*story.metadata.get(RECORD_KEY, []), entry],
            HOLD_KEY: True,
        },
    )
    gate_id = await create_human_gate(
        client,
        story_id=STORY,
        story_title="T3-W8",
        project="lithos-lens",
        agent="loom",
        route="story-develop",
        reason=BRIEF_REVIEW_REASON,
        summary="brief review",
        run_id=run_id,
        brief={"base_sha": "a" * 40, "mode": MODE_FULL, "addendum": draft},
    )
    await _edit_gate(client, gate_id, draft, edited)
    await client.task_complete(task_id=gate_id, agent="dave")
    return await _apply(client)


async def test_an_older_block_quoting_its_header_keeps_the_newer_one_whole() -> None:
    # correctness/f-001 (round 5): an older approval quotes its own header
    # (with a section heading) between two copies of its edited D1; a newer
    # approval under the same header edits all of D1..D40. The approval
    # records where each block's text is, so each lists exactly its own.
    from lithos_loom.plugins.story_develop.brief_review import records_of
    from lithos_loom.plugins.story_develop.brief_review_auto import (
        brief_review_section,
    )

    client = _client()
    older = _decisions("Older first.").replace(
        "  - Basis: F1.",
        f"  - Basis: F1.\n\n{_HEADER} — quoted.\n\n**Decisions**\n\n"
        "- **D1. Older repeated.** y\n  - Basis: F1.",
    )
    await _approve_next(client, "r-old", _decisions("Drafted."), older)
    drafts = [f"Drafted {i}." for i in range(1, 41)]
    newer = [f"Newer decision {i}." for i in range(1, 41)]
    outcome = await _approve_next(
        client, "r-new", _decisions(*drafts), _decisions(*newer)
    )

    story = await _get(client, STORY)
    records = records_of(story.metadata)
    assert set(records[-1]["outcomes"].values()) == {ITEM_EDITED}
    section = brief_review_section(records, "x" * 8000 + outcome.payload["description"])

    first, second = section.split("(mode `full`, outcome `approved`)")[1:]
    assert "\nD1. Older first.\nD1. Older repeated.\n" in first
    assert "Newer" not in first and "Older" not in second
    assert "\nD40. Newer decision 40.\n" in second
    assert "more items" not in section and "not found" not in section


async def test_an_example_under_the_header_in_the_brief_is_not_listed() -> None:
    # correctness/f-003 (round 5): the brief itself holds an example under
    # the addendum's exact header with 40 D1 items; the approved one-item
    # addendum's own heading is what the PR lists, not the example's.
    from lithos_loom.plugins.story_develop.brief_review import records_of
    from lithos_loom.plugins.story_develop.brief_review_auto import (
        brief_review_section,
    )

    example = _decisions(*(f"Example heading {i}." for i in range(40))).replace(
        "- **D2.", "- **D1."
    )
    example = "\n".join(
        line.replace(line[: line.index(".")], "- **D1")
        if line.startswith("- **D")
        else line
        for line in example.splitlines()
    )
    story = make_task(
        STORY,
        title="T3-W8",
        description=f"{'x' * 8000}\n\n{example}\n\nThe brief continues.",
        metadata={"project": "lithos-lens", "prd": "docs/prd/t3.md"},
        tags=("trigger:story-develop",),
    )
    client = FakeLithosClient(tasks=(story,), agent_id="loom")
    genuine = _decisions("Genuine approved heading.")
    outcome = await _approve_next(client, "r-1", genuine, genuine)

    fresh = await _get(client, STORY)
    assert fresh.metadata[RECORD_KEY][-1]["outcomes"] == {"D1": ITEM_UNCHANGED}
    section = brief_review_section(
        records_of(fresh.metadata), outcome.payload["description"]
    )
    assert "\nD1. Genuine approved heading.\n" in section
    assert "Example heading" not in section and "more items" not in section


async def test_an_example_under_the_header_keeps_a_legacy_approval_s_heading() -> None:
    # correctness/f-001 (converge): the same story, approved before approvals
    # recorded the text they append (no `appended`): located by header, the
    # example opens its block, and the genuine heading still has its line.
    from lithos_loom.plugins.story_develop.brief_review import records_of
    from lithos_loom.plugins.story_develop.brief_review_auto import (
        brief_review_section,
    )

    example = "\n".join(
        f"- **D1. Example heading {i}.** x\n  - Basis: F1." for i in range(40)
    )
    story = make_task(
        STORY,
        title="T3-W8",
        description=f"{'x' * 8000}\n\n{_HEADER} — e.\n\n**Decisions**\n\n"
        f"{example}\n\nThe brief continues.",
        metadata={"project": "lithos-lens", "prd": "docs/prd/t3.md"},
        tags=("trigger:story-develop",),
    )
    client = FakeLithosClient(tasks=(story,), agent_id="loom")
    genuine = _decisions("Genuine approved heading.")
    outcome = await _approve_next(client, "r-1", genuine, genuine)

    fresh = await _get(client, STORY)
    records = records_of(fresh.metadata)
    legacy = {k: v for k, v in records[-1].items() if k != "appended"}
    section = brief_review_section([legacy], outcome.payload["description"])
    assert "\nD1. Genuine approved heading.\n" in section
    assert "an earlier copy is listed unchecked" in section
    assert "…and 1 more items." in section


async def test_an_emptied_draft_dispatches_with_nothing_appended() -> None:
    client = _client()
    gate_id = await _held(client)
    await _edit_gate(client, gate_id, _DRAFT, "")
    await client.task_complete(task_id=gate_id, agent="dave")

    outcome = await _apply(client)

    assert outcome.applied
    story = await _get(client, STORY)
    assert story.description == BRIEF
    assert set(story.metadata[RECORD_KEY][-1]["outcomes"].values()) == {ITEM_CUT}


async def test_a_gate_whose_fences_were_edited_away_fails_closed() -> None:
    client = _client()
    gate_id = await _held(client)
    await _edit_gate(client, gate_id, "#### End of addendum", "")
    await client.task_complete(task_id=gate_id, agent="dave")

    outcome = await _apply(client)

    assert not outcome.applied
    refused = outcome.refused
    assert refused is not None and refused.reason == BRIEF_REVIEW_REASON
    assert gate_id in refused.summary
    # the operator gets their text back to trim, inside fresh fences
    assert "**F1. One.**" in refused.brief["addendum"]
    story = await _get(client, STORY)
    assert story.description == BRIEF
    assert story.metadata[RECORD_KEY][-1]["outcome"] == OUTCOME_PENDING


async def test_a_gate_still_open_is_not_an_approval() -> None:
    client = _client()
    await _held(client)

    outcome = await _apply(client)

    assert not outcome.applied and outcome.refused is None
    story = await _get(client, STORY)
    assert story.description == BRIEF  # nothing appended
    assert story.metadata[RECORD_KEY][-1]["outcome"] == OUTCOME_PENDING


async def test_a_dispatch_with_no_approval_to_apply_drops_a_stale_hold() -> None:
    # A failed review the operator skipped: the hold is the run's slot now.
    client = _client()
    await _held(client)
    story = await _get(client, STORY)
    failed = dict(story.metadata[RECORD_KEY][-1], outcome=OUTCOME_FAILED)
    await client.task_update(task_id=STORY, metadata={RECORD_KEY: [failed]})

    outcome = await _apply(client)

    assert not outcome.applied and outcome.refused is None
    assert outcome.payload is not None
    assert HOLD_KEY not in outcome.payload["metadata"]
    assert HOLD_KEY not in (await _get(client, STORY)).metadata


async def test_a_story_with_no_hold_and_nothing_to_apply_is_not_written() -> None:
    client = _client()
    writes_before = len(client.calls)

    outcome = await _apply(client)

    assert outcome.payload is None
    assert not [c for c in client.calls[writes_before:] if c.method == "task_update"]


async def test_a_story_with_no_pending_review_is_left_alone() -> None:
    client = _client()

    outcome = await _apply(client)

    assert not outcome.applied and outcome.refused is None
    assert not [c for c in client.calls if c.method == "task_edge_list"]


async def test_a_completed_gate_of_another_run_is_not_this_approval() -> None:
    client = _client()
    gate_id = await _held(client, run_id="older001")
    await client.task_complete(task_id=gate_id, agent="dave")
    # a newer pass re-drafted under another run; its gate is not yet raised
    story = await _get(client, STORY)
    newer = dict(story.metadata[RECORD_KEY][-1], run_id="newer001")
    await client.task_update(task_id=STORY, metadata={RECORD_KEY: [newer]})

    outcome = await _apply(client)

    assert not outcome.applied and outcome.refused is None


async def test_a_concurrent_edit_is_reread_and_the_approval_retried() -> None:
    client = _client()
    gate_id = await _held(client)
    await client.task_complete(task_id=gate_id, agent="dave")

    class Racy:
        raced = False

        def __getattr__(self, name: str) -> Any:
            return getattr(client, name)

        async def task_update(self, **kwargs: Any) -> Any:
            if not Racy.raced and "expected_updated_at" in kwargs:
                Racy.raced = True
                await client.task_update(task_id=STORY, metadata={"priority": "high"})
            return await client.task_update(**kwargs)

    outcome = await _apply(Racy())

    assert outcome.applied
    story = await _get(client, STORY)
    assert story.metadata["priority"] == "high"
    assert story.description == f"{BRIEF}\n\n{_DRAFT}\n"


async def test_a_second_conflict_fails_closed_instead_of_dispatching() -> None:
    client = _client()
    gate_id = await _held(client)
    await client.task_complete(task_id=gate_id, agent="dave")

    class Busy:
        def __getattr__(self, name: str) -> Any:
            return getattr(client, name)

        async def task_update(self, **kwargs: Any) -> Any:
            if "expected_updated_at" in kwargs:
                raise LithosClientError("version_conflict", "someone else again")
            return await client.task_update(**kwargs)

    outcome = await _apply(Busy())

    assert not outcome.applied
    assert outcome.refused is not None
    assert outcome.refused.reason == BRIEF_REVIEW_REASON
    story = await _get(client, STORY)
    assert story.description == BRIEF


async def test_after_a_refusal_the_fresh_gate_is_the_approval_not_the_old_one() -> None:
    # The refused gate stays completed on the story (gates are never
    # cancelled), and names the same run — so it must be recorded as
    # superseded, or every later dispatch would find it again and refuse
    # again, forever.
    client = _client()
    gate_id = await _held(client)
    await _edit_gate(client, gate_id, "#### End of addendum", "")
    await client.task_complete(task_id=gate_id, agent="dave")
    refused = (await _apply(client)).refused
    assert refused is not None
    # the runner raises the fresh gate from the refusal
    fresh_gate = await create_human_gate(
        client,
        story_id=STORY,
        story_title="T3-W8",
        project="lithos-lens",
        agent="loom",
        route="story-develop",
        reason=refused.reason,
        summary=refused.summary,
        run_id=RUN,
        brief=refused.brief,
    )
    # while it is open, nothing is approved
    assert not (await _apply(client)).applied
    await _edit_gate(
        client, fresh_gate, "- **F2. Two.** `b.py:2`.\n", ""
    )  # the operator trims
    await client.task_complete(task_id=fresh_gate, agent="dave")

    outcome = await _apply(client)

    assert outcome.applied
    story = await _get(client, STORY)
    entry = story.metadata[RECORD_KEY][-1]
    assert entry["gate_id"] == fresh_gate
    assert entry["superseded_gates"] == [gate_id]
    assert "**F2. Two.**" not in (story.description or "").split(BRIEF, 1)[1]
