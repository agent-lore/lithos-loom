"""Unit tests for the PR-gate domain helpers (Epic H).

Exercised against the shared :class:`FakeLithosClient`, whose gate validation
mirrors the live server (PR #261): a gate needs a valid ``metadata.gate_type``,
and a ``waits_on_gate`` edge is rejected unless its ``from_task`` is a gate.
"""

from __future__ import annotations

import pytest

from lithos_loom.errors import LithosClientError
from lithos_loom.gates import (
    ESCALATION_SUMMARY_MAX_CHARS,
    GATE_TYPE_PR,
    NEEDS_HUMAN_TAG,
    WAITS_ON_GATE,
    HumanGateSpec,
    PrGateSpec,
    create_human_gate,
    create_human_gate_best_effort,
    create_pr_gate,
    create_pr_gate_best_effort,
    is_human_gate,
    is_loom_human_gate,
    is_pr_gate,
    parse_human_gate,
    parse_pr_gate,
    waiter_of,
)
from tests.support import FakeLithosClient, make_task

_PR_URL = "https://github.com/agent-lore/lithos-loom/pull/42"


async def _story(client: FakeLithosClient) -> str:
    return await client.task_create(title="US42", metadata={"project": "loom"})


# ── create_pr_gate ──────────────────────────────────────────────────────


async def test_create_pr_gate_creates_a_gate_with_pr_metadata() -> None:
    client = FakeLithosClient(agent_id="a1")
    story = await _story(client)

    gate_id = await create_pr_gate(
        client,
        story_id=story,
        story_title="US42",
        pr_url=_PR_URL,
        project="loom",
        agent="a1",
    )

    gate = await client.task_get(task_id=gate_id)
    assert gate is not None
    assert gate.task_type == "gate"
    assert gate.metadata == {
        "gate_type": "pr",
        "repo": "agent-lore/lithos-loom",
        "pr_number": 42,
        "required_state": "merged",
        "pr_url": _PR_URL,
        "project": "loom",
    }
    assert gate.title == "Awaiting merge: US42"


async def test_create_pr_gate_links_the_story_as_waiter() -> None:
    """The whole point: the story is blocked until the gate resolves."""
    client = FakeLithosClient(agent_id="a1")
    story = await _story(client)

    gate_id = await create_pr_gate(
        client,
        story_id=story,
        story_title="US42",
        pr_url=_PR_URL,
        project="loom",
        agent="a1",
    )

    # Story is absent from the ready frontier and named as gate-blocked.
    assert story not in [t.id for t in await client.task_ready(project="loom")]
    blocked = await client.task_blocked(project="loom")
    assert [bt.task.id for bt in blocked] == [story]
    assert blocked[0].blockers[0].kind == "gate"
    assert await waiter_of(client, gate_id) == story


async def test_create_pr_gate_omits_project_when_absent() -> None:
    client = FakeLithosClient(agent_id="a1")
    story = await _story(client)
    gate_id = await create_pr_gate(
        client,
        story_id=story,
        story_title="US42",
        pr_url=_PR_URL,
        project=None,
        agent="a1",
    )
    gate = await client.task_get(task_id=gate_id)
    assert gate is not None and "project" not in gate.metadata


async def test_create_pr_gate_rejects_a_non_pr_url() -> None:
    client = FakeLithosClient(agent_id="a1")
    story = await _story(client)
    for bad in (
        "https://github.com/agent-lore/lithos-loom/issues/42",  # issue, not pull
        "not a url",
        "",
    ):
        with pytest.raises(ValueError):
            await create_pr_gate(
                client,
                story_id=story,
                story_title="US42",
                pr_url=bad,
                project="loom",
                agent="a1",
            )


async def test_create_pr_gate_cancels_the_orphan_gate_when_the_edge_fails() -> None:
    """If the edge write fails after the gate task is created, the gate is
    cancelled so the open-gate set never holds a gate with no waiter."""

    class _EdgeFails(FakeLithosClient):
        async def task_edge_upsert(self, **kwargs: object) -> None:  # type: ignore[override]
            raise LithosClientError("boom", "edge write failed")

    client = _EdgeFails(agent_id="a1")
    story = await _story(client)
    with pytest.raises(LithosClientError):
        await create_pr_gate(
            client,
            story_id=story,
            story_title="US42",
            pr_url=_PR_URL,
            project="loom",
            agent="a1",
        )
    # No open gate lingers.
    assert [t.id for t in await client.task_ready(project="loom")] == [story]
    assert await client.task_blocked(project="loom") == []


# ── create_pr_gate_best_effort ──────────────────────────────────────────


async def test_best_effort_returns_gate_id_and_no_problem_on_success() -> None:
    client = FakeLithosClient(agent_id="a1")
    story = await _story(client)

    gate_id, problem = await create_pr_gate_best_effort(
        client,
        story_id=story,
        story_title="US42",
        pr_url=_PR_URL,
        project="loom",
        agent="a1",
    )

    assert problem is None
    assert gate_id is not None
    assert await waiter_of(client, gate_id) == story


@pytest.mark.parametrize("pr_url", [None, "", 42, "not a pr url"])
async def test_best_effort_degrades_on_missing_or_bad_pr_url(pr_url: object) -> None:
    """No usable pr_url (absent, non-string, or unparseable) → no gate, but a
    problem string the caller folds into a [Friction] rather than raising."""
    client = FakeLithosClient(agent_id="a1")
    story = await _story(client)

    gate_id, problem = await create_pr_gate_best_effort(
        client,
        story_id=story,
        story_title="US42",
        pr_url=pr_url,
        project="loom",
        agent="a1",
    )

    assert gate_id is None
    assert problem is not None
    # The story stays workable — no half-formed gate blocks it.
    assert [t.id for t in await client.task_ready(project="loom")] == [story]


async def test_best_effort_degrades_when_the_write_fails() -> None:
    class _CreateFails(FakeLithosClient):
        async def task_create(self, **kwargs: object) -> str:  # type: ignore[override]
            raise LithosClientError("boom", "gate create failed")

    client = _CreateFails(agent_id="a1")

    gate_id, problem = await create_pr_gate_best_effort(
        client,
        story_id="s1",
        story_title="US42",
        pr_url=_PR_URL,
        project="loom",
        agent="a1",
    )

    assert gate_id is None
    assert problem is not None and "could not create the pr gate" in problem


# ── is_pr_gate / parse_pr_gate ──────────────────────────────────────────


def test_is_pr_gate_true_only_for_a_pr_gate() -> None:
    assert is_pr_gate(make_task("g", task_type="gate", metadata={"gate_type": "pr"}))
    assert not is_pr_gate(
        make_task("h", task_type="gate", metadata={"gate_type": "human"})
    )
    assert not is_pr_gate(make_task("t", metadata={"gate_type": "pr"}))  # not a gate


def test_parse_pr_gate_reads_the_watched_pr() -> None:
    gate = make_task(
        "g",
        task_type="gate",
        metadata={
            "gate_type": "pr",
            "repo": "o/r",
            "pr_number": 7,
            "pr_url": "https://github.com/o/r/pull/7",
        },
    )
    assert parse_pr_gate(gate) == PrGateSpec(
        repo="o/r", pr_number=7, pr_url="https://github.com/o/r/pull/7"
    )


@pytest.mark.parametrize(
    "metadata",
    [
        {"gate_type": "pr", "repo": "o/r", "pr_url": "u"},  # no pr_number
        {"gate_type": "pr", "pr_number": 7, "pr_url": "u"},  # no repo
        {"gate_type": "pr", "repo": "o/r", "pr_number": 7},  # no pr_url
        {"gate_type": "pr", "repo": "o/r", "pr_number": True, "pr_url": "u"},  # bool
        {"gate_type": "pr", "repo": "", "pr_number": 7, "pr_url": "u"},  # empty repo
    ],
)
def test_parse_pr_gate_returns_none_for_malformed_metadata(
    metadata: dict[str, object],
) -> None:
    assert parse_pr_gate(make_task("g", task_type="gate", metadata=metadata)) is None


# ── waiter_of ───────────────────────────────────────────────────────────


async def test_waiter_of_returns_none_for_an_orphan_gate() -> None:
    client = FakeLithosClient(agent_id="a1")
    gate = await client.task_create(
        title="orphan", task_type="gate", metadata={"gate_type": "pr"}
    )
    assert await waiter_of(client, gate) is None


async def test_module_constants() -> None:
    assert GATE_TYPE_PR == "pr"
    assert WAITS_ON_GATE == "waits_on_gate"


# ── create_human_gate (b91177d2 — the needs-human escalation primitive) ───


async def _raise(client: FakeLithosClient, story: str, **overrides: object) -> str:
    kwargs: dict[str, object] = dict(
        story_id=story,
        story_title="US42",
        project="loom",
        agent="a1",
        route="story-develop",
        reason="max_rounds",
        summary="round 5: NOT approved (max_rounds)",
        run_id="abcd1234",
        brief={"branch": "loom/us42", "rounds": 5, "cost_usd": 48.53},
    )
    kwargs.update(overrides)
    return await create_human_gate(client, **kwargs)  # type: ignore[arg-type]


async def test_create_human_gate_shape_is_triageable_from_the_list() -> None:
    """The gate carries everything the list views need as FLAT keys, and the
    run brief nested — see the design note on lens's 3-chip advisory row."""
    client = FakeLithosClient(agent_id="a1")
    story = await _story(client)

    gate_id = await _raise(client, story)

    gate = await client.task_get(task_id=gate_id)
    assert gate is not None
    assert gate.task_type == "gate"
    assert gate.title == "Needs human: US42"
    assert set(gate.tags) == {"project:loom", NEEDS_HUMAN_TAG}
    assert gate.metadata == {
        "gate_type": "human",
        "raised_by": "loom",
        "project": "loom",
        "route": "story-develop",
        "story_id": story,
        "run_id": "abcd1234",
        "escalation_reason": "max_rounds",
        "escalation_summary": "round 5: NOT approved (max_rounds)",
        "run_brief": {"branch": "loom/us42", "rounds": 5, "cost_usd": 48.53},
    }
    # The description is the operator's brief: why, what, and what to do.
    assert gate.description is not None
    assert "max_rounds" in gate.description
    assert "loom/us42" in gate.description
    assert "Complete this gate" in gate.description
    assert "Cancel the *story*" in gate.description


async def test_create_human_gate_blocks_the_story() -> None:
    """The whole point: the story leaves the ready frontier until a human
    completes the gate, and returns to it when they do."""
    client = FakeLithosClient(agent_id="a1")
    story = await _story(client)

    gate_id = await _raise(client, story)

    assert story not in [t.id for t in await client.task_ready(project="loom")]
    blocked = await client.task_blocked(project="loom")
    assert [bt.task.id for bt in blocked] == [story]
    assert blocked[0].blockers[0].kind == "gate"
    assert await waiter_of(client, gate_id) == story

    await client.task_complete(task_id=gate_id, agent="dave")
    assert [t.id for t in await client.task_ready(project="loom")] == [story]


async def test_create_human_gate_omits_optional_fields() -> None:
    client = FakeLithosClient(agent_id="a1")
    story = await _story(client)
    gate_id = await _raise(client, story, project=None, run_id=None, brief=None)
    gate = await client.task_get(task_id=gate_id)
    assert gate is not None
    assert "project" not in gate.metadata
    assert "run_id" not in gate.metadata
    assert "run_brief" not in gate.metadata
    assert gate.tags == (NEEDS_HUMAN_TAG,)


async def test_create_human_gate_truncates_a_long_summary() -> None:
    client = FakeLithosClient(agent_id="a1")
    story = await _story(client)
    gate_id = await _raise(client, story, summary="x" * 500)
    gate = await client.task_get(task_id=gate_id)
    assert gate is not None
    assert len(gate.metadata["escalation_summary"]) == ESCALATION_SUMMARY_MAX_CHARS
    assert gate.metadata["escalation_summary"].endswith("…")


async def test_create_human_gate_rejects_an_unknown_reason() -> None:
    """The reason is a closed vocabulary — operator queries and lens badges key
    on it, so a typo must fail loudly at the call site, not land on the board."""
    client = FakeLithosClient(agent_id="a1")
    story = await _story(client)
    with pytest.raises(ValueError):
        await _raise(client, story, reason="because")
    assert await client.task_blocked(project="loom") == []


async def test_create_human_gate_cancels_the_orphan_gate_when_the_edge_fails() -> None:
    class _EdgeFails(FakeLithosClient):
        async def task_edge_upsert(self, **kwargs: object) -> None:  # type: ignore[override]
            raise LithosClientError("boom", "edge write failed")

    client = _EdgeFails(agent_id="a1")
    story = await _story(client)
    with pytest.raises(LithosClientError):
        await _raise(client, story)
    assert [t.id for t in await client.task_ready(project="loom")] == [story]
    assert await client.task_blocked(project="loom") == []


async def test_create_human_gate_best_effort_degrades_when_the_write_fails() -> None:
    class _CreateFails(FakeLithosClient):
        async def task_create(self, **kwargs: object) -> str:  # type: ignore[override]
            raise LithosClientError("boom", "gate create failed")

    gate_id, problem = await create_human_gate_best_effort(
        _CreateFails(agent_id="a1"),
        story_id="s1",
        story_title="US42",
        project="loom",
        agent="a1",
        route="story-develop",
        reason="stalled",
        summary="stalled",
    )
    assert gate_id is None
    assert problem is not None and "could not create the needs-human gate" in problem


async def test_create_human_gate_best_effort_degrades_on_a_bad_reason() -> None:
    gate_id, problem = await create_human_gate_best_effort(
        FakeLithosClient(agent_id="a1"),
        story_id="s1",
        story_title="US42",
        project="loom",
        agent="a1",
        route="story-develop",
        reason="nope",
        summary="stalled",
    )
    assert gate_id is None
    assert problem is not None


# ── is_loom_human_gate / parse_human_gate ──────────────────────────────


def test_is_loom_human_gate_requires_type_and_provenance() -> None:
    loom = make_task(
        "g", task_type="gate", metadata={"gate_type": "human", "raised_by": "loom"}
    )
    daves = make_task("h", task_type="gate", metadata={"gate_type": "human"})
    pr = make_task("p", task_type="gate", metadata={"gate_type": "pr"})
    assert is_human_gate(loom) and is_human_gate(daves) and not is_human_gate(pr)
    assert is_loom_human_gate(loom)
    assert not is_loom_human_gate(daves)  # the operator's own gates are theirs
    assert not is_loom_human_gate(pr)


def test_parse_human_gate_reads_the_escalation() -> None:
    gate = make_task(
        "g",
        task_type="gate",
        metadata={
            "gate_type": "human",
            "raised_by": "loom",
            "route": "story-develop",
            "story_id": "s1",
            "run_id": "abcd1234",
            "escalation_reason": "stalled",
            "escalation_summary": "round 4: stalled",
            "run_brief": {"branch": "b"},
        },
    )
    assert parse_human_gate(gate) == HumanGateSpec(
        reason="stalled",
        summary="round 4: stalled",
        route="story-develop",
        story_id="s1",
        run_id="abcd1234",
        brief={"branch": "b"},
    )


def test_parse_human_gate_returns_none_without_a_reason() -> None:
    gate = make_task(
        "g", task_type="gate", metadata={"gate_type": "human", "raised_by": "loom"}
    )
    assert parse_human_gate(gate) is None
