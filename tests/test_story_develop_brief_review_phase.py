"""Brief review at dispatch (604fb936), PR 2: the plugin's half.

With ``develop_brief_review`` on, a story carrying ``metadata.prd`` is checked
against the exact base its coder would start from before the coder starts.
What the phase does is decided by the story's records (``plan_review``):

| State | Action |
|---|---|
| never reviewed | full pass → **held** |
| approved at this base | proceed |
| approved at another base | delta pass |
| delta: no change | record it, proceed |
| delta: facts only | append (CAS), ``[BriefReview]`` finding, proceed |
| delta: any decision | **held** |

Held means the run ends with a ``brief_review`` escalation, the story keeps
its admission slot (``brief_review_hold``), and the runner raises the gate.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from lithos_loom.plugins.story_develop import brief_review_phase as phase_mod
from lithos_loom.plugins.story_develop.brief_review import (
    HOLD_KEY,
    MODE_DELTA,
    MODE_FULL,
    OUTCOME_APPROVED,
    OUTCOME_AUTO_APPENDED,
    OUTCOME_FAILED,
    OUTCOME_NO_CHANGE,
    OUTCOME_PENDING,
    RECORD_KEY,
    BriefInputs,
    BriefReviewResult,
    parse_addendum,
)
from lithos_loom.plugins.story_develop.brief_review_phase import run_phase
from lithos_loom.plugins.story_develop.config import DevelopConfig
from lithos_loom.plugins.story_develop.lithos_io import TaskContext
from tests.support import FakeLithosClient, make_task

S1 = "a" * 40
S2 = "b" * 40
STORY = "story-1"
CREATED = datetime(2026, 10, 1, 8, 29, 44, tzinfo=UTC)
NOW = datetime(2026, 10, 5, 20, 0, tzinfo=UTC)
BRIEF = "Slice W8. Build the relation sentences."

_FACTS_ONLY = """\
## Facts

- **F13. W7 added no refusal hook.** `write_funnel.py:536`.
"""
_WITH_DECISION = (
    _FACTS_ONLY
    + """
## Decisions

- **D11. Share the candidate macro.** Move it to a shared partial.
  - Basis: F13.
"""
)


def _story(**metadata: Any) -> Any:
    meta = {"project": "lithos-lens", "prd": "docs/prd/t3.md", **metadata}
    return dataclasses.replace(
        make_task(STORY, title="T3-W8", description=BRIEF, metadata=meta),
        created_at=CREATED,
        updated_at=datetime(2026, 10, 5, 19, 0, tzinfo=UTC),
    )


def _ctx(story: Any) -> TaskContext:
    return TaskContext(
        task_id=story.id,
        title=story.title,
        description=story.description or "",
        acceptance_criteria=None,
        metadata=dict(story.metadata),
    )


def _config(tmp_path: Path) -> DevelopConfig:
    return DevelopConfig(
        repo=tmp_path / "repo",
        description="T3-W8",
        work_dir=tmp_path / "work",
        run_id="run00001",
    )


class _Review:
    """A fake pass: records what it was asked, answers *draft* (or degrades)."""

    def __init__(self, draft: str | None, note: str = "") -> None:
        self.draft = draft
        self.note = note
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self,
        config: DevelopConfig,
        inputs: BriefInputs,
        *,
        base_sha: str,
        mode: str,
        prior_base: str | None,
        timeout: int,
    ) -> BriefReviewResult:
        self.calls.append(
            {"inputs": inputs, "base_sha": base_sha, "mode": mode, "prior": prior_base}
        )
        return BriefReviewResult(
            addendum=parse_addendum(self.draft) if self.draft is not None else None,
            base_sha=base_sha,
            mode=mode,
            prior_base=prior_base,
            cost_usd=1.25,
            note=self.note,
            raw=self.draft or "",
        )


async def _run(
    tmp_path: Path, story: Any, review: _Review, client: Any = None
) -> tuple[Any, Any]:
    client = client or FakeLithosClient(tasks=(story,))
    outcome = await run_phase(
        client,
        _config(tmp_path),
        _ctx(story),
        start_sha=S2,
        review=review,
        now=NOW,
    )
    return outcome, await client.task_get(task_id=STORY)


async def test_a_never_reviewed_story_is_reviewed_in_full_and_held(
    tmp_path: Path,
) -> None:
    review = _Review(_WITH_DECISION)
    story = _story(acceptance_criteria="- evicts both endpoints")

    outcome, stored = await _run(tmp_path, story, review)

    assert not outcome.proceed
    call = review.calls[0]
    assert call["mode"] == MODE_FULL and call["base_sha"] == S2
    inputs = call["inputs"]
    assert inputs.brief == BRIEF and inputs.prd == "docs/prd/t3.md"
    assert inputs.written_at == CREATED.isoformat()
    assert inputs.acceptance_criteria == "- evicts both endpoints"
    esc = outcome.escalation
    assert esc["reason"] == "brief_review"
    assert len(esc["summary"]) <= 200 and "1 fact, 1 decision" in esc["summary"]
    assert esc["brief"]["addendum"].startswith(
        f"**Brief review against `{S2[:12]}` (2026-10-05)**"
    )
    assert esc["brief"]["base_sha"] == S2 and esc["brief"]["mode"] == MODE_FULL
    entries = stored.metadata[RECORD_KEY]
    assert [e["outcome"] for e in entries] == [OUTCOME_PENDING]
    assert entries[0]["run_id"] == "run00001" and entries[0]["base_sha"] == S2
    assert [i["id"] for i in entries[0]["items"]] == ["F13", "D11"]
    assert stored.metadata[HOLD_KEY] is True
    assert stored.metadata["develop_status"] == "held_for_brief_review"
    assert stored.description == BRIEF  # nothing appended until approval


async def test_a_full_review_that_finds_nothing_is_still_held(tmp_path: Path) -> None:
    # Phase 1: every full review is the operator's to approve.
    review = _Review("## No change\nEvery claim in the brief holds at this base.\n")

    outcome, stored = await _run(tmp_path, _story(), review)

    assert not outcome.proceed
    assert "No change: Every claim" in outcome.escalation["brief"]["addendum"]
    assert stored.metadata[RECORD_KEY][-1]["outcome"] == OUTCOME_PENDING


async def test_an_approved_review_at_this_base_proceeds_without_a_pass(
    tmp_path: Path,
) -> None:
    review = _Review(_FACTS_ONLY)
    story = _story(
        brief_review=[{"run_id": "r0", "outcome": OUTCOME_APPROVED, "base_sha": S2}]
    )

    outcome, stored = await _run(tmp_path, story, review)

    assert outcome.proceed and outcome.start_sha == S2
    assert outcome.description == BRIEF
    assert review.calls == []
    assert stored.metadata[RECORD_KEY] == story.metadata[RECORD_KEY]


async def test_a_moved_base_with_a_facts_only_recheck_appends_and_proceeds(
    tmp_path: Path,
) -> None:
    review = _Review(_FACTS_ONLY)
    story = _story(
        brief_review=[{"run_id": "r0", "outcome": OUTCOME_APPROVED, "base_sha": S1}]
    )
    client = FakeLithosClient(tasks=(story,))

    outcome, stored = await _run(tmp_path, story, review, client)

    assert review.calls[0]["mode"] == MODE_DELTA
    assert review.calls[0]["prior"] == S1
    assert outcome.proceed and outcome.start_sha == S2
    assert stored.description.startswith(BRIEF + "\n\n**Recheck against")
    assert "**F13. W7 added no refusal hook.**" in stored.description
    assert outcome.description == stored.description  # the coder gets it too
    entries = stored.metadata[RECORD_KEY]
    assert [e["outcome"] for e in entries] == [OUTCOME_APPROVED, OUTCOME_AUTO_APPENDED]
    assert HOLD_KEY not in stored.metadata
    assert any(
        f["summary"].startswith("[BriefReview]") and "1 fact" in f["summary"]
        for f in client.findings
    )


async def test_a_moved_base_with_nothing_new_records_it_and_proceeds(
    tmp_path: Path,
) -> None:
    review = _Review("## No change\nNothing merged touches this brief.\n")
    story = _story(
        brief_review=[{"run_id": "r0", "outcome": OUTCOME_APPROVED, "base_sha": S1}]
    )

    outcome, stored = await _run(tmp_path, story, review)

    assert outcome.proceed and outcome.description == BRIEF
    assert stored.description == BRIEF
    assert stored.metadata[RECORD_KEY][-1]["outcome"] == OUTCOME_NO_CHANGE
    assert stored.metadata[RECORD_KEY][-1]["base_sha"] == S2


async def test_a_no_change_recheck_dispatches_the_brief_as_edited_meanwhile(
    tmp_path: Path,
) -> None:
    # The pass takes minutes; an operator editing the brief meanwhile is the
    # realistic source of the record write's version conflict. The record is
    # retried on the fresh read, and the coder must get that read's brief —
    # not the text the phase started from (review #449 F3).
    review = _Review("## No change\nNothing merged touches this brief.\n")
    story = _story(
        brief_review=[{"run_id": "r0", "outcome": OUTCOME_APPROVED, "base_sha": S1}]
    )
    inner = FakeLithosClient(tasks=(story,))
    edited = BRIEF + " Cover the empty state too."

    class Racy:
        raced = False

        def __getattr__(self, name: str) -> Any:
            return getattr(inner, name)

        async def task_update(self, **kwargs: Any) -> Any:
            if not Racy.raced and "expected_updated_at" in kwargs:
                Racy.raced = True
                await inner.task_update(task_id=STORY, description=edited, agent="dave")
            return await inner.task_update(**kwargs)

    outcome, stored = await _run(tmp_path, story, review, Racy())

    assert stored.description == edited
    assert stored.metadata[RECORD_KEY][-1]["outcome"] == OUTCOME_NO_CHANGE
    assert outcome.proceed and outcome.description == edited


async def test_a_moved_base_with_a_decision_is_held_again(tmp_path: Path) -> None:
    review = _Review(_WITH_DECISION)
    story = _story(
        brief_review=[{"run_id": "r0", "outcome": OUTCOME_APPROVED, "base_sha": S1}]
    )

    outcome, stored = await _run(tmp_path, story, review)

    assert not outcome.proceed
    assert outcome.escalation["brief"]["addendum"].startswith("**Recheck against")
    assert outcome.escalation["brief"]["prior_base"] == S1
    assert stored.metadata[RECORD_KEY][-1]["outcome"] == OUTCOME_PENDING
    assert stored.metadata[HOLD_KEY] is True
    assert stored.description == BRIEF


async def test_a_degraded_pass_holds_the_story_with_its_note(tmp_path: Path) -> None:
    # No path dispatches a held story from an unreviewed brief.
    review = _Review(None, note="the brief-review turn failed (attempt 1)")

    outcome, stored = await _run(tmp_path, _story(), review)

    assert not outcome.proceed
    brief = outcome.escalation["brief"]
    assert "addendum" not in brief
    assert brief["note"] == "the brief-review turn failed (attempt 1)"
    entry = stored.metadata[RECORD_KEY][-1]
    assert entry["outcome"] == OUTCOME_FAILED and entry["items"] == []
    assert stored.metadata[HOLD_KEY] is True


async def test_a_github_mirrored_story_is_not_reviewed(tmp_path: Path) -> None:
    # The issue drift sync rewrites the description from the issue body, so
    # an appended addendum would be erased on the next poll.
    review = _Review(_FACTS_ONLY)
    story = _story(github_issue_url="https://github.com/o/r/issues/7")

    outcome, stored = await _run(tmp_path, story, review)

    assert outcome.proceed and outcome.start_sha is None
    assert review.calls == []
    assert any("github" in f.lower() for f in outcome.frictions)
    assert RECORD_KEY not in stored.metadata


async def test_the_record_write_retries_once_on_a_concurrent_edit(
    tmp_path: Path,
) -> None:
    story = _story()
    inner = FakeLithosClient(tasks=(story,))

    class Racy:
        """Another writer lands between the phase's read and its write."""

        raced = False

        def __getattr__(self, name: str) -> Any:
            return getattr(inner, name)

        async def task_update(self, **kwargs: Any) -> Any:
            if not Racy.raced and "expected_updated_at" in kwargs:
                Racy.raced = True
                await inner.task_update(
                    task_id=STORY, metadata={"priority": "high"}, agent="dave"
                )
            return await inner.task_update(**kwargs)

    outcome, stored = await _run(tmp_path, story, _Review(_WITH_DECISION), Racy())

    assert not outcome.proceed
    assert stored.metadata["priority"] == "high"  # the other write survives
    assert stored.metadata[RECORD_KEY][-1]["outcome"] == OUTCOME_PENDING


async def test_a_story_lithos_cannot_find_raises(tmp_path: Path) -> None:
    with pytest.raises(LookupError):
        await run_phase(
            FakeLithosClient(),
            _config(tmp_path),
            _ctx(_story()),
            start_sha=S2,
            review=_Review(_FACTS_ONLY),
            now=NOW,
        )


def test_the_phase_writes_with_the_plugin_agent_id() -> None:
    assert phase_mod.AGENT_ID == "lithos-loom-story-develop"
