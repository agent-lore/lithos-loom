"""Tests for ``lithos_loom.subscriptions.pr_gate_stranding`` (04c2448b / #268).

A delivered PR that closed unmerged or disappeared used to leave its ``pr``
gate open with a finding nobody saw (gate e8126732 sat nineteen days while its
work had merged via another PR). The resolver now converts that stranding into
a loom ``human`` gate on the story and completes the ``pr`` gate — see
``test_develop_pr_merge.py`` for the conversion itself. This module owns the
pieces around it: reading the PR a stranding gate was raised for, and the
waiter-resolved hygiene that keeps every loom human gate honest.
"""

from __future__ import annotations

import logging
from typing import Any

from lithos_loom.errors import LithosClientError
from lithos_loom.gates import create_human_gate
from lithos_loom.subscriptions import SubscriptionContext
from lithos_loom.subscriptions.pr_gate_stranding import (
    complete_if_waiter_resolved,
    human_gate_pr,
)
from tests.support import FakeLithosClient

_PR_URL = "https://github.com/agent-lore/lithos-lens/pull/37"


def _ctx(lithos: Any) -> SubscriptionContext:
    return SubscriptionContext(
        lithos=lithos,
        logger=logging.getLogger("test-pr-gate-stranding"),
        agent_id="lithos-loom-agent",
    )


async def _human_gate(
    client: FakeLithosClient,
    *,
    reason: str = "pr_closed_unmerged",
    brief: dict[str, Any] | None = None,
) -> tuple[str, Any]:
    story = await client.task_create(title="US7", metadata={"project": "p"})
    gate_id = await create_human_gate(
        client,
        story_id=story,
        story_title="US7",
        project="p",
        agent="a",
        route="pr-gate",
        reason=reason,
        summary="delivered PR closed unmerged",
        brief={"pr_url": _PR_URL} if brief is None else brief,
    )
    return story, await client.task_get(task_id=gate_id)


# ── human_gate_pr: the PR a stranding gate watches ─────────────────────


async def test_human_gate_pr_reads_the_brief_url() -> None:
    client = FakeLithosClient(agent_id="a")
    _, gate = await _human_gate(client)

    spec = human_gate_pr(gate)

    assert spec is not None
    assert (spec.repo, spec.pr_number, spec.pr_url) == (
        "agent-lore/lithos-lens",
        37,
        _PR_URL,
    )


async def test_human_gate_pr_reads_a_gone_pr_too() -> None:
    client = FakeLithosClient(agent_id="a")
    _, gate = await _human_gate(client, reason="pr_gone")

    assert human_gate_pr(gate) is not None


async def test_human_gate_pr_ignores_other_escalation_reasons() -> None:
    """A conflict / remediation gate also carries a pr_url in its brief; it is
    not a stranding and must not be merge-polled."""
    client = FakeLithosClient(agent_id="a")
    _, gate = await _human_gate(client, reason="conflict_unresolved")

    assert human_gate_pr(gate) is None


async def test_human_gate_pr_ignores_a_missing_or_unparseable_url() -> None:
    client = FakeLithosClient(agent_id="a")
    _, no_url = await _human_gate(client, brief={"branch": "x"})
    _, bad_url = await _human_gate(client, brief={"pr_url": "not a url"})
    _, an_issue = await _human_gate(
        client, brief={"pr_url": "https://github.com/agent-lore/lithos-lens/issues/3"}
    )

    assert human_gate_pr(no_url) is None
    assert human_gate_pr(bad_url) is None
    assert human_gate_pr(an_issue) is None


# ── waiter-resolved hygiene ────────────────────────────────────────────


async def test_a_gate_whose_waiter_completed_is_completed() -> None:
    """The operator completed the story by hand (the work merged elsewhere):
    the gate that blocked it is now noise on every board — close it."""
    client = FakeLithosClient(agent_id="a")
    story, gate = await _human_gate(client)
    await client.task_complete(task_id=story)

    label = await complete_if_waiter_resolved(gate, _ctx(client))

    assert label == "completed"
    stored = await client.task_get(task_id=gate.id)
    assert stored is not None and stored.status == "completed"


async def test_a_gate_whose_waiter_was_cancelled_is_completed() -> None:
    client = FakeLithosClient(agent_id="a")
    story, gate = await _human_gate(client)
    await client.task_cancel(task_id=story)

    assert await complete_if_waiter_resolved(gate, _ctx(client)) == "completed"
    stored = await client.task_get(task_id=gate.id)
    assert stored is not None and stored.status == "completed"


async def test_an_open_waiter_leaves_the_gate_alone() -> None:
    client = FakeLithosClient(agent_id="a")
    _, gate = await _human_gate(client)

    assert await complete_if_waiter_resolved(gate, _ctx(client)) == "open"
    stored = await client.task_get(task_id=gate.id)
    assert stored is not None and stored.status == "open"
    assert not client.called("task_complete")


async def test_an_orphan_gate_is_left_for_the_operator() -> None:
    """No waiter edge → nothing says whether the gate is done; the `gates`
    CLI flags it as `orphan` and a human decides."""
    client = FakeLithosClient(agent_id="a")
    gate_id = await client.task_create(
        title="Needs human: orphan",
        task_type="gate",
        metadata={"gate_type": "human", "raised_by": "loom"},
    )
    gate = await client.task_get(task_id=gate_id)

    assert await complete_if_waiter_resolved(gate, _ctx(client)) == "orphan"
    assert not client.called("task_complete")


async def test_hygiene_never_nudges() -> None:
    """Completing the gate is a tidy-up, not a release: a dependent that was
    behind the story is not written to (the story's own completion already
    released it, or a sibling blocker still holds it)."""
    client = FakeLithosClient(agent_id="a")
    story, gate = await _human_gate(client)
    dependent = await client.task_create(title="next", metadata={"project": "p"})
    await client.task_edge_upsert(
        from_task_id=story, to_task_id=dependent, type="blocks", agent="a"
    )
    await client.task_complete(task_id=story)
    writes_before = len(client.calls_to("task_update"))

    await complete_if_waiter_resolved(gate, _ctx(client))

    assert len(client.calls_to("task_update")) == writes_before


async def test_a_transient_completion_failure_is_reported_not_raised() -> None:
    client = FakeLithosClient(agent_id="a")
    story, gate = await _human_gate(client)
    await client.task_complete(task_id=story)

    async def _fail(**kwargs: Any) -> Any:
        raise LithosClientError("internal", "boom")

    client.task_complete = _fail  # type: ignore[method-assign]

    assert await complete_if_waiter_resolved(gate, _ctx(client)) == "error"


async def test_a_waiter_that_no_longer_exists_is_left_alone() -> None:
    """The edge dangles (waiter deleted): the CLI reports `waiter-gone`; the
    sweep must not guess that "gone" means "done"."""
    client = FakeLithosClient(agent_id="a")
    story, gate = await _human_gate(client)
    client._tasks.pop(story)

    assert await complete_if_waiter_resolved(gate, _ctx(client)) == "waiter-gone"
    assert not client.called("task_complete")
