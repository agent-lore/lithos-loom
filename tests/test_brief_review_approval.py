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
