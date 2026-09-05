"""Tests for ``lithos_loom.subscriptions.pr_landability`` (PRD S1).

Each sweep of a still-open ``pr`` gate classifies the PR's landability from
the ``GET /pulls/{n}`` fields GitHub already returns and posts a one-shot
``[PRConflicted]`` finding on the story when the PR cannot merge, de-duped by
a ``(pr_url, base_sha, head_sha)`` marker on the GATE — a conflict that is
resolved by a push moves the head, so the marker must re-evaluate on either
sha moving. ``mergeable == null`` is GitHub's "still computing" and is never
read as clean.
"""

from __future__ import annotations

import logging
from typing import Any

from lithos_loom.gates import create_pr_gate, parse_pr_gate
from lithos_loom.github_client import PullRequest
from lithos_loom.subscriptions import SubscriptionContext
from lithos_loom.subscriptions.pr_landability import (
    LANDABILITY_KEY,
    PR_CONFLICTED,
    check_landability,
    classify_landability,
)
from tests.support import FakeLithosClient

_PR_URL = "https://github.com/agent-lore/lithos-loom/pull/352"
_BASE = "b" * 40
_HEAD = "h" * 40


def _ctx(lithos: Any) -> SubscriptionContext:
    return SubscriptionContext(
        lithos=lithos, logger=logging.getLogger("test-landability"), agent_id="a"
    )


def _pr(
    *,
    mergeable: bool | None,
    mergeable_state: str = "",
    base_sha: str = _BASE,
    head_sha: str = _HEAD,
) -> PullRequest:
    return PullRequest(
        repo="agent-lore/lithos-loom",
        number=352,
        state="open",
        merged=False,
        merged_at=None,
        merge_commit_sha=None,
        head_sha=head_sha,
        base_ref="main",
        head_ref="watcher-driven",
        base_sha=base_sha,
        mergeable=mergeable,
        mergeable_state=mergeable_state,
    )


async def _gate_with_story(client: FakeLithosClient) -> tuple[str, Any]:
    story = await client.task_create(title="US7", metadata={"project": "p"})
    gate_id = await create_pr_gate(
        client,
        story_id=story,
        story_title="US7",
        pr_url=_PR_URL,
        project="p",
        agent="a",
    )
    gate = await client.task_get(task_id=gate_id)
    assert gate is not None
    return story, gate


async def _run(
    client: FakeLithosClient, gate: Any, story: str | None, pr: PullRequest
) -> str:
    spec = parse_pr_gate(gate)
    assert spec is not None
    return await check_landability(gate, spec, story, pr, _ctx(client))


async def _refresh(client: FakeLithosClient, gate: Any) -> Any:
    fresh = await client.task_get(task_id=gate.id)
    assert fresh is not None
    return fresh


async def _marker(client: FakeLithosClient, gate_id: str) -> Any:
    gate = await client.task_get(task_id=gate_id)
    assert gate is not None
    return gate.metadata.get(LANDABILITY_KEY)


def _findings(client: FakeLithosClient) -> list[str]:
    return [f["summary"] for f in client._findings]


# ── classification: a pure function of the fetched PR ─────────────────


def test_classification_table() -> None:
    # null is "ask again" — never clean (PRD S1 caveat 1).
    assert classify_landability(_pr(mergeable=None)) == "unknown"
    assert (
        classify_landability(_pr(mergeable=None, mergeable_state="unknown"))
        == "unknown"
    )
    # GitHub's verdict wins whichever field carries it.
    assert (
        classify_landability(_pr(mergeable=False, mergeable_state="dirty")) == "dirty"
    )
    assert classify_landability(_pr(mergeable=True, mergeable_state="dirty")) == "dirty"
    assert classify_landability(_pr(mergeable=False)) == "dirty"
    # Anything GitHub can merge — clean, behind, blocked, unstable, has_hooks —
    # is landable as far as conflicts go.
    for state in ("clean", "behind", "blocked", "unstable", "has_hooks", ""):
        assert (
            classify_landability(_pr(mergeable=True, mergeable_state=state))
            == "mergeable"
        )


# ── the sweep step ─────────────────────────────────────────────────────


async def test_dirty_pr_posts_conflicted_once_and_marks_the_gate() -> None:
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    pr = _pr(mergeable=False, mergeable_state="dirty")

    assert await _run(client, gate, story, pr) == "dirty"

    (finding,) = _findings(client)
    assert finding.startswith(PR_CONFLICTED)
    assert (
        _PR_URL in finding
        and "main" in finding
        and story in finding
        and gate.id in finding
    )
    assert _HEAD[:12] in finding and _BASE[:12] in finding
    marker = await _marker(client, gate.id)
    assert marker == {
        "pr_url": _PR_URL,
        "base_sha": _BASE,
        "head_sha": _HEAD,
        "state": "dirty",
        "mergeable_state": "dirty",
    }

    # Same shas next sweep: nothing new to say.
    gate = await _refresh(client, gate)
    assert await _run(client, gate, story, pr) == "dirty"
    assert len(_findings(client)) == 1


async def test_conflict_refires_when_either_sha_moves() -> None:
    """The negative test that matters (PRD S1 caveat 2): pushing a resolution
    moves the HEAD and not the base — a base-scoped marker would suppress the
    re-check that proves the fix landed (or did not)."""
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    await _run(client, gate, story, _pr(mergeable=False, mergeable_state="dirty"))

    gate = await _refresh(client, gate)
    still_dirty = _pr(mergeable=False, mergeable_state="dirty", head_sha="2" * 40)
    await _run(client, gate, story, still_dirty)
    assert len(_findings(client)) == 2
    assert (await _marker(client, gate.id))["head_sha"] == "2" * 40

    gate = await _refresh(client, gate)
    base_moved = _pr(
        mergeable=False, mergeable_state="dirty", head_sha="2" * 40, base_sha="3" * 40
    )
    await _run(client, gate, story, base_moved)
    assert len(_findings(client)) == 3


async def test_resolution_updates_the_marker_silently() -> None:
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    await _run(client, gate, story, _pr(mergeable=False, mergeable_state="dirty"))

    gate = await _refresh(client, gate)
    fixed = _pr(mergeable=True, mergeable_state="clean", head_sha="2" * 40)
    assert await _run(client, gate, story, fixed) == "mergeable"

    assert len(_findings(client)) == 1  # no "resolved" chatter on the story
    marker = await _marker(client, gate.id)
    assert marker["state"] == "mergeable" and marker["head_sha"] == "2" * 40


async def test_unknown_writes_nothing_and_re_asks_next_sweep() -> None:
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)

    assert await _run(client, gate, story, _pr(mergeable=None)) == "unknown"

    assert _findings(client) == []
    assert await _marker(client, gate.id) is None
    # …and a later dirty answer is not masked by the earlier null.
    gate = await _refresh(client, gate)
    await _run(client, gate, story, _pr(mergeable=False, mergeable_state="dirty"))
    assert len(_findings(client)) == 1


async def test_orphan_gate_marks_without_a_finding() -> None:
    client = FakeLithosClient()
    _, gate = await _gate_with_story(client)

    assert (
        await _run(client, gate, None, _pr(mergeable=False, mergeable_state="dirty"))
        == "dirty"
    )

    assert _findings(client) == []
    assert (await _marker(client, gate.id))["state"] == "dirty"


async def test_marker_is_scoped_to_the_pr_url() -> None:
    """A replacement PR (fresh url) re-evaluates from scratch."""
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    await client.task_update(
        task_id=gate.id,
        metadata={
            LANDABILITY_KEY: {
                "pr_url": "https://github.com/agent-lore/lithos-loom/pull/1",
                "base_sha": _BASE,
                "head_sha": _HEAD,
                "state": "dirty",
                "mergeable_state": "dirty",
            }
        },
    )
    gate = await _refresh(client, gate)
    await _run(client, gate, story, _pr(mergeable=False, mergeable_state="dirty"))
    assert len(_findings(client)) == 1
