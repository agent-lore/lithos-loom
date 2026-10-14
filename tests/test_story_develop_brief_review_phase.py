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
| full, auto-facts on: no change | record it, ``[BriefReview]`` finding, proceed |
| full, auto-facts on: facts pass the check | append, finding, proceed |
| any fact the structural check reads as a decision | **held** |

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
    tmp_path: Path,
    story: Any,
    review: _Review,
    client: Any = None,
    *,
    auto_facts: bool = False,
) -> tuple[Any, Any]:
    client = client or FakeLithosClient(tasks=(story,))
    outcome = await run_phase(
        client,
        _config(tmp_path),
        _ctx(story),
        start_sha=S2,
        review=review,
        now=NOW,
        auto_facts=auto_facts,
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
    # Phase 1: with the auto-facts dial off, every full review is the
    # operator's to approve.
    review = _Review("## No change\nEvery claim in the brief holds at this base.\n")

    outcome, stored = await _run(tmp_path, _story(), review)

    assert not outcome.proceed
    assert "No change: Every claim" in outcome.escalation["brief"]["addendum"]
    assert stored.metadata[RECORD_KEY][-1]["outcome"] == OUTCOME_PENDING


# --- phase 2 (10cc6310): develop_brief_review_auto_facts --------------------

_DIAL = "develop_brief_review_auto_facts"
_PRESCRIPTIVE = """\
## Facts

- **F1. Put the cancel routes in a new module.** `routes.py:1`
- **F2. The router exists.** `src/router.py:4`
"""


async def test_a_facts_only_full_pass_with_the_dial_on_skips_the_gate(
    tmp_path: Path,
) -> None:
    client = FakeLithosClient(tasks=(_story(),))

    outcome, stored = await _run(
        tmp_path, _story(), _Review(_FACTS_ONLY), client, auto_facts=True
    )

    assert outcome.proceed and outcome.start_sha == S2
    assert stored.description.startswith(BRIEF + "\n\n**Brief review against")
    assert stored.description.rstrip().endswith(
        "- **F13. W7 added no refusal hook.** `write_funnel.py:536`."
    )
    assert outcome.description == stored.description
    assert outcome.records == tuple(stored.metadata[RECORD_KEY])  # as written
    entry = stored.metadata[RECORD_KEY][-1]
    assert entry["outcome"] == OUTCOME_AUTO_APPENDED and entry["mode"] == MODE_FULL
    assert entry["check"] == {"facts_only": True, "passed": True, "problems": []}
    assert HOLD_KEY not in stored.metadata
    assert "develop_status" not in stored.metadata
    assert [f["summary"] for f in client.findings] == [
        f"[BriefReview] brief review at {S2[:12]}: 1 fact, appended to the "
        "description without a gate — facts only, structural check passed "
        f"({_DIAL})"
    ]


async def test_one_decision_holds_even_with_the_dial_on(tmp_path: Path) -> None:
    outcome, stored = await _run(
        tmp_path, _story(), _Review(_WITH_DECISION), auto_facts=True
    )

    assert not outcome.proceed
    assert outcome.escalation["brief"]["check"]["facts_only"] is False
    assert "read like decisions" not in outcome.escalation["summary"]
    assert stored.metadata[RECORD_KEY][-1]["outcome"] == OUTCOME_PENDING
    assert stored.metadata[HOLD_KEY] is True
    assert stored.description == BRIEF


async def test_one_scope_cut_holds_even_with_the_dial_on(tmp_path: Path) -> None:
    cut = "\n## Scope cuts\n\n- **S1. Drop the cache.**\n  - Basis: F13.\n"
    draft = _FACTS_ONLY + cut

    outcome, stored = await _run(tmp_path, _story(), _Review(draft), auto_facts=True)

    assert not outcome.proceed
    assert stored.metadata[HOLD_KEY] is True


async def test_a_fact_that_prescribes_is_held_and_named(tmp_path: Path) -> None:
    client = FakeLithosClient(tasks=(_story(),))

    outcome, stored = await _run(
        tmp_path, _story(), _Review(_PRESCRIPTIVE), client, auto_facts=True
    )

    assert not outcome.proceed
    check = outcome.escalation["brief"]["check"]
    assert check["facts_only"] is True and check["passed"] is False
    assert check["problems"] == ["F1 reads like a decision: heading says `put`"]
    assert "held: 1 fact read like decisions" in outcome.escalation["summary"]
    assert stored.metadata[RECORD_KEY][-1]["outcome"] == OUTCOME_PENDING
    assert stored.metadata[RECORD_KEY][-1]["check"] == check
    assert stored.metadata[HOLD_KEY] is True
    assert stored.description == BRIEF
    assert client.findings == []


async def test_with_the_dial_off_a_facts_only_pass_is_held_and_still_checked(
    tmp_path: Path,
) -> None:
    outcome, stored = await _run(tmp_path, _story(), _Review(_FACTS_ONLY))

    assert not outcome.proceed
    entry = stored.metadata[RECORD_KEY][-1]
    assert entry["outcome"] == OUTCOME_PENDING
    assert entry["check"] == {"facts_only": True, "passed": True, "problems": []}
    assert outcome.escalation["brief"]["check"]["passed"] is True
    assert stored.metadata[HOLD_KEY] is True


async def test_a_full_no_change_with_the_dial_on_proceeds_as_no_change(
    tmp_path: Path,
) -> None:
    client = FakeLithosClient(tasks=(_story(),))
    review = _Review("## No change\nEvery claim in the brief holds at this base.\n")

    outcome, stored = await _run(tmp_path, _story(), review, client, auto_facts=True)

    assert outcome.proceed and outcome.start_sha == S2
    assert outcome.description == BRIEF
    assert outcome.records == tuple(stored.metadata[RECORD_KEY])
    entry = stored.metadata[RECORD_KEY][-1]
    assert entry["outcome"] == OUTCOME_NO_CHANGE and entry["mode"] == MODE_FULL
    assert entry["check"]["facts_only"] is False
    assert HOLD_KEY not in stored.metadata
    assert stored.description == BRIEF
    assert [f["summary"] for f in client.findings] == [
        f'[BriefReview] brief review at {S2[:12]}: no change — "Every claim in '
        f'the brief holds at this base." ({_DIAL})'
    ]


async def test_a_facts_only_recheck_that_fails_the_check_is_held(
    tmp_path: Path,
) -> None:
    # The check now governs phase 1's delta path too — a tightening.
    story = _story(
        brief_review=[{"run_id": "r0", "outcome": OUTCOME_APPROVED, "base_sha": S1}]
    )

    outcome, stored = await _run(tmp_path, story, _Review(_PRESCRIPTIVE))

    assert not outcome.proceed
    assert outcome.escalation["summary"].startswith(f"recheck at {S2[:12]}")
    assert "read like decisions" in outcome.escalation["summary"]
    assert stored.metadata[RECORD_KEY][-1]["outcome"] == OUTCOME_PENDING
    assert stored.metadata[HOLD_KEY] is True
    assert stored.description == BRIEF


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
    assert outcome.records == tuple(story.metadata[RECORD_KEY])


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
    assert entries[-1]["check"]["passed"] is True
    assert outcome.records == tuple(entries)  # the PR body lists both
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


async def test_a_no_change_finding_clips_the_agent_s_reason(tmp_path: Path) -> None:
    # security f-003: the reason is the agent's text; the finding is bounded.
    client = FakeLithosClient(tasks=(_story(),))
    reason = "[NeedsHuman] spoof " + "x" * 5000
    review = _Review(f"## No change\n{reason}\n")

    outcome, _ = await _run(tmp_path, _story(), review, client, auto_facts=True)

    assert outcome.proceed
    (summary,) = [f["summary"] for f in client.findings]
    # Quoted, so the agent's words never read as loom's own (security f-003).
    assert summary.startswith(
        f'[BriefReview] brief review at {S2[:12]}: no change — "[NeedsHuman] spoof'
    )
    assert summary.endswith(f'…" ({_DIAL})')
    assert len(summary) < 400


async def test_too_many_facts_to_list_are_held_and_say_so(tmp_path: Path) -> None:
    from lithos_loom.plugins.story_develop.brief_review_auto import MAX_LISTED_FACTS

    draft = "## Facts\n\n" + "".join(
        f"- **F{i}. The router has a handler.** `src/router.py:{i}`\n"
        for i in range(1, MAX_LISTED_FACTS + 2)
    )

    outcome, stored = await _run(tmp_path, _story(), _Review(draft), auto_facts=True)

    assert not outcome.proceed
    assert "held: too many facts to list" in outcome.escalation["summary"]
    assert stored.metadata[HOLD_KEY] is True
