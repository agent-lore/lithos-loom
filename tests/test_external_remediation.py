"""Tests for ``lithos_loom.subscriptions.external_remediation`` (PRD S2
slice C: autonomous dispatch + the S5b budget).

The github-watcher sweep, having posted an ``[ExternalReview]`` batch, may
dispatch ``develop converge --from-github`` as a subprocess — bounded by the
S5b budget on the gate. The load-bearing properties, pinned hardest here:

- **The budget never resets on a loom-authored push** (the two-bot ping-pong
  S5b exists to bound) and **resets on a human push** (head moved to a sha
  loom didn't push — the operator took ownership).
- **One in-flight remediation globally**; detection is never paused, dispatch
  is deferred to a later sweep.
- **Exhaustion stops dispatch, never detection**, and is stated in the
  finding body (rendered by the ingestion module; the note text is minted
  here).
- Only **trusted** authors' material dispatches, and material at loom's own
  pushed sha is reported-not-remediated (own-sha skip).
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from lithos_loom.gates import create_pr_gate, parse_pr_gate
from lithos_loom.github_client import (
    IssueComment,
    PullRequestReview,
    PullRequestReviewComment,
)
from lithos_loom.github_review_activity import (
    ExternalReviewActivity,
    from_conversation_comment,
    from_inline_comment,
    from_review,
)
from lithos_loom.subscriptions import SubscriptionContext
from lithos_loom.subscriptions.external_remediation import (
    PENDING_KEY,
    REMEDIATION_KEY,
    ExternalRemediation,
    OriginRead,
    RemediationBudget,
    RemediationSettings,
    read_budget,
)
from lithos_loom.subscriptions.external_reviews import IngestResult
from tests.support import FakeLithosClient

_PR_URL = "https://github.com/agent-lore/lithos-lens/pull/62"
_HEAD = "h" * 40
_LOOM_SHA = "a1" * 20
_BOT = "copilot-pull-request-reviewer[bot]"


@pytest.fixture(autouse=True)
def _resolvable_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    """The mapped checkout resolves to the gate's repo unless a test says
    otherwise (PR #362 re-review 3: an unresolvable origin fails closed)."""
    from lithos_loom.subscriptions import external_remediation as mod

    async def resolvable(path: Path) -> OriginRead:
        return OriginRead("agent-lore/lithos-lens", "ok")

    monkeypatch.setattr(mod, "origin_read", resolvable)


def _ctx(lithos: Any) -> SubscriptionContext:
    return SubscriptionContext(
        lithos=lithos,
        logger=logging.getLogger("test-external-remediation"),
        agent_id="lithos-loom-agent",
    )


async def _gate_with_story(
    client: FakeLithosClient, *, project: str | None = "p"
) -> tuple[str, Any]:
    story = await client.task_create(title="US7", metadata={"project": "p"})
    gate_id = await create_pr_gate(
        client,
        story_id=story,
        story_title="US7",
        pr_url=_PR_URL,
        project=project,
        agent="a",
    )
    gate = await client.task_get(task_id=gate_id)
    assert gate is not None
    return story, gate


def _settings(tmp_path: Path, **overrides: Any) -> RemediationSettings:
    defaults: dict[str, Any] = {
        "trusted_bots": (_BOT,),
        "budget": 2,
        "projects": {"p": tmp_path / "repo"},
        "work_dir": tmp_path / "work",
    }
    defaults.update(overrides)
    return RemediationSettings(**defaults)


def _review(
    review_id: int = 500,
    *,
    author: str = _BOT,
    commit_id: str = _HEAD,
) -> PullRequestReview:
    return PullRequestReview(
        author=author,
        body="two problems",
        review_id=review_id,
        state="CHANGES_REQUESTED",
        commit_id=commit_id,
    )


def _comment(
    comment_id: int = 7,
    *,
    author: str = "reviewer-human",
    commit_id: str = _HEAD,
) -> PullRequestReviewComment:
    return PullRequestReviewComment(
        comment_id=comment_id,
        author=author,
        path="src/x.py",
        line=12,
        body="leaks a handle",
        in_reply_to_id=None,
        commit_id=commit_id,
    )


def _act(*rows: Any) -> list[ExternalReviewActivity]:
    """Normalise raw GitHub rows the way the sweep does (#355)."""
    out: list[ExternalReviewActivity] = []
    for row in rows:
        if isinstance(row, PullRequestReview):
            out.append(from_review(row, repo="agent-lore/lithos-lens", pr_number=62))
        elif isinstance(row, PullRequestReviewComment):
            out.append(from_inline_comment(row))
        else:
            out.append(from_conversation_comment(row))
    return out


def _ingest(*rows: Any, posted: bool = True) -> IngestResult:
    """An ingest result over *rows* (default: one trusted-bot review)."""
    return IngestResult(posted=posted, actionable=_act(*(rows or (_review(),))))


def _github(permission: str = "write") -> AsyncMock:
    github = AsyncMock()
    github.get_collaborator_permission.return_value = permission
    return github


def _spawner(
    payload: dict | None, rc: int = 0, output: str = "converge output"
) -> tuple[Any, list[list[str]]]:
    """A fake spawn: records the argv, optionally writes the --json payload."""
    calls: list[list[str]] = []

    async def spawn(cmd: list[str]) -> tuple[int, str]:
        calls.append(cmd)
        if payload is not None:
            path = Path(cmd[cmd.index("--json") + 1])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload), encoding="utf-8")
        return rc, output

    return spawn, calls


def _pr(head_sha: str = _HEAD) -> SimpleNamespace:
    return SimpleNamespace(head_sha=head_sha)


async def _marker(client: FakeLithosClient, gate_id: str) -> Any:
    gate = await client.task_get(task_id=gate_id)
    assert gate is not None
    return gate.metadata.get(REMEDIATION_KEY)


def _findings(client: FakeLithosClient) -> list[str]:
    return [f["summary"] for f in client._findings]


# ── the budget marker ──────────────────────────────────────────────────


def test_read_budget_fresh_and_url_scoped() -> None:
    gate = SimpleNamespace(
        metadata={
            REMEDIATION_KEY: {
                "pr_url": "https://example/other/1",
                "rounds_used": 2,
                "last_loom_pushed_sha": "x",
                "last_seen_head_sha": "y",
            }
        }
    )
    # Foreign url → fresh budget (a replacement PR re-evaluates from scratch).
    fresh = read_budget(gate, _PR_URL)
    assert fresh == RemediationBudget(pr_url=_PR_URL)
    same = read_budget(gate, "https://example/other/1")
    assert same.rounds_used == 2
    assert same.last_loom_pushed_sha == "x"


async def test_observe_head_records_first_sighting() -> None:
    client = FakeLithosClient()
    _story, gate = await _gate_with_story(client)
    rem = ExternalRemediation(_settings(Path("/tmp/x")), spawn=_spawner(None)[0])
    spec = parse_pr_gate(gate)
    assert spec is not None

    budget = await rem.observe_head(gate, spec, _pr(), _ctx(client))

    assert budget.last_seen_head_sha == _HEAD
    marker = await _marker(client, gate.id)
    assert marker["last_seen_head_sha"] == _HEAD


async def test_human_push_resets_rounds_but_loom_push_does_not() -> None:
    """THE S5b property: rounds never reset on loom's own push (the ping-pong
    bound) and do reset when a human pushes (operator took ownership)."""
    client = FakeLithosClient()
    _story, gate = await _gate_with_story(client)
    await client.task_update(
        task_id=gate.id,
        metadata={
            REMEDIATION_KEY: {
                "pr_url": _PR_URL,
                "rounds_used": 2,
                "last_loom_pushed_sha": _LOOM_SHA,
                "last_seen_head_sha": "old" + "0" * 37,
            }
        },
    )
    gate = await client.task_get(task_id=gate.id)
    assert gate is not None
    rem = ExternalRemediation(_settings(Path("/tmp/x")), spawn=_spawner(None)[0])
    spec = parse_pr_gate(gate)
    assert spec is not None

    # Head moved to loom's own pushed sha: NOT a reset.
    budget = await rem.observe_head(gate, spec, _pr(_LOOM_SHA), _ctx(client))
    assert budget.rounds_used == 2
    assert budget.last_seen_head_sha == _LOOM_SHA

    # Head moved to a sha loom did not push: the operator took over — reset.
    gate = await client.task_get(task_id=gate.id)
    assert gate is not None
    human_sha = "b2" * 20
    budget = await rem.observe_head(gate, spec, _pr(human_sha), _ctx(client))
    assert budget.rounds_used == 0
    assert budget.last_seen_head_sha == human_sha


async def test_observe_head_is_inert_while_a_run_is_in_flight() -> None:
    """While loom's own converge may push at any moment, head attribution is
    ambiguous — the observer must neither reset nor write."""
    client = FakeLithosClient()
    _story, gate = await _gate_with_story(client)
    await client.task_update(
        task_id=gate.id,
        metadata={
            REMEDIATION_KEY: {
                "pr_url": _PR_URL,
                "rounds_used": 1,
                "last_loom_pushed_sha": "",
                "last_seen_head_sha": _HEAD,
            }
        },
    )
    gate = await client.task_get(task_id=gate.id)
    assert gate is not None
    rem = ExternalRemediation(_settings(Path("/tmp/x")), spawn=_spawner(None)[0])
    rem._task = asyncio.create_task(asyncio.sleep(30))  # a run in flight
    spec = parse_pr_gate(gate)
    assert spec is not None
    try:
        budget = await rem.observe_head(gate, spec, _pr("c3" * 20), _ctx(client))
        assert budget.rounds_used == 1  # no reset
        marker = await _marker(client, gate.id)
        assert marker["last_seen_head_sha"] == _HEAD  # no write
    finally:
        rem._task.cancel()


def test_exhaustion_note_only_at_or_over_budget() -> None:
    rem = ExternalRemediation(_settings(Path("/tmp/x")), spawn=_spawner(None)[0])
    under = RemediationBudget(pr_url=_PR_URL, rounds_used=1)
    at = RemediationBudget(pr_url=_PR_URL, rounds_used=2)
    assert rem.exhaustion_note(under) is None
    note = rem.exhaustion_note(at)
    assert note is not None and "budget exhausted" in note
    # budget == 0 disables dispatch deliberately — no exhaustion noise.
    disabled = ExternalRemediation(
        _settings(Path("/tmp/x"), budget=0), spawn=_spawner(None)[0]
    )
    assert disabled.exhaustion_note(under) is None


# ── the dispatch decision ──────────────────────────────────────────────


async def _consider(
    client: FakeLithosClient,
    gate: Any,
    story: str | None,
    rem: ExternalRemediation,
    *,
    ingest: IngestResult | None = None,
    github: AsyncMock | None = None,
    rounds_used: int = 0,
    budget: RemediationBudget | None = None,
) -> str:
    spec = parse_pr_gate(gate)
    assert spec is not None
    if budget is None:
        budget = RemediationBudget(pr_url=_PR_URL, rounds_used=rounds_used)
    return await rem.consider(
        gate,
        spec,
        story,
        budget,
        ingest if ingest is not None else _ingest(),
        github if github is not None else _github(),
        _ctx(client),
    )


async def test_dispatch_happy_path_runs_converge_and_records_outcome(
    tmp_path: Path,
) -> None:
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    pushed = "e5" * 20
    spawn, calls = _spawner(
        {
            "status": "converged",
            "pushed": True,
            "pushed_sha": pushed,
            "rounds": 2,
            "total_cost_usd": 3.5,
            "external_outcomes": [
                {
                    "finding_id": "f-001",
                    "author": _BOT,
                    "disposition": "fixed",
                    "detail": "guarded it",
                }
            ],
            "message": "converged and pushed",
        }
    )
    rem = ExternalRemediation(_settings(tmp_path), spawn=spawn)

    label = await _consider(client, gate, story, rem)
    assert label == "dispatched"
    assert rem._task is not None
    await rem._task

    # The subprocess argv: converge on this PR, external mode, right repo.
    (cmd,) = calls
    assert cmd[:2] == [sys.executable, "-m"]
    assert "lithos_loom" in cmd
    assert "converge" in cmd and "62" in cmd and "--from-github" in cmd
    # PR #362 review F2: the checkout is pinned to the gate's repo
    assert cmd[cmd.index("--expect-repo") + 1] == "agent-lore/lithos-lens"
    assert str(tmp_path / "repo") in cmd

    # Budget: incremented at dispatch; the push recorded as loom's own sha.
    marker = await _marker(client, gate.id)
    assert marker["rounds_used"] == 1
    assert marker["last_loom_pushed_sha"] == pushed
    assert marker["last_seen_head_sha"] == pushed

    # Outcome finding on the story.
    outcome = next(f for f in _findings(client) if "remediation" in f)
    assert "[ExternalReview]" in outcome
    assert "converged" in outcome
    assert "f-001" in outcome and "fixed" in outcome


async def test_outcome_finding_carries_an_outcomes_note(tmp_path: Path) -> None:
    """#399: the epilogue's note (a final NO CHANGE NEEDED read over an
    earlier FIXED) is shown on the story's outcome finding, so the prompt
    drift is visible where the operator reads the run — never on the
    reviewer's thread, which gets the plain disposition."""
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    spawn, _calls = _spawner(
        {
            "status": "converged",
            "pushed": True,
            "pushed_sha": "e5" * 20,
            "rounds": 4,
            "total_cost_usd": 3.5,
            "external_outcomes": [
                {
                    "finding_id": "f-001",
                    "author": _BOT,
                    "disposition": "fixed",
                    "detail": "guarded it",
                    "note": (
                        "fixed in round 1; the round 4 handoff said NO CHANGE NEEDED"
                    ),
                }
            ],
            "message": "converged and pushed",
        }
    )
    rem = ExternalRemediation(_settings(tmp_path), spawn=spawn)
    assert await _consider(client, gate, story, rem) == "dispatched"
    assert rem._task is not None
    await rem._task

    outcome = next(f for f in _findings(client) if "remediation outcome" in f)
    assert f"f-001 by {_BOT}: fixed — guarded it" in outcome
    assert (
        "[note: fixed in round 1; the round 4 handoff said NO CHANGE NEEDED]" in outcome
    )


async def test_second_dispatch_defers_while_one_is_in_flight(
    tmp_path: Path,
) -> None:
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_spawn(cmd: list[str]) -> tuple[int, str]:
        started.set()
        await release.wait()
        return 0, ""

    rem = ExternalRemediation(_settings(tmp_path), spawn=slow_spawn)
    assert await _consider(client, gate, story, rem) == "dispatched"
    await started.wait()
    # Detection has posted another batch; dispatch defers, never queues.
    assert await _consider(client, gate, story, rem) == "deferred_busy"
    release.set()
    assert rem._task is not None
    await rem._task


async def test_untrusted_only_material_never_dispatches(tmp_path: Path) -> None:
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    spawn, calls = _spawner(None)
    rem = ExternalRemediation(_settings(tmp_path), spawn=spawn)

    label = await _consider(
        client,
        gate,
        story,
        rem,
        ingest=_ingest(_review(author="drive-by"), _comment(author="drive-by")),
        github=_github(permission="read"),
    )

    assert label == "no_trusted"
    assert calls == []
    assert await _marker(client, gate.id) is None  # nothing incremented


async def test_own_sha_material_is_reported_not_remediated(tmp_path: Path) -> None:
    """A re-review of loom's own in-flight fix must not trigger another fix."""
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    spawn, calls = _spawner(None)
    rem = ExternalRemediation(_settings(tmp_path), spawn=spawn)
    spec = parse_pr_gate(gate)
    assert spec is not None
    budget = RemediationBudget(
        pr_url=_PR_URL, rounds_used=1, last_loom_pushed_sha=_LOOM_SHA
    )

    label = await rem.consider(
        gate,
        spec,
        story,
        budget,
        _ingest(_review(commit_id=_LOOM_SHA)),
        _github(),
        _ctx(client),
    )

    assert label == "own_sha_only"
    assert calls == []


async def test_exhausted_budget_stops_dispatch(tmp_path: Path) -> None:
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    spawn, calls = _spawner(None)
    rem = ExternalRemediation(_settings(tmp_path), spawn=spawn)

    label = await _consider(client, gate, story, rem, rounds_used=2)

    assert label == "exhausted"
    assert calls == []


async def test_budget_zero_disables_dispatch(tmp_path: Path) -> None:
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    spawn, calls = _spawner(None)
    rem = ExternalRemediation(_settings(tmp_path, budget=0), spawn=spawn)

    assert await _consider(client, gate, story, rem) == "disabled"
    assert calls == []


async def test_unmapped_project_skips_with_friction(tmp_path: Path) -> None:
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    spawn, calls = _spawner(None)
    rem = ExternalRemediation(_settings(tmp_path, projects={}), spawn=spawn)

    assert await _consider(client, gate, story, rem) == "no_project"
    assert calls == []


async def test_gate_without_project_falls_back_to_story_metadata(
    tmp_path: Path,
) -> None:
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client, project=None)
    spawn, _calls = _spawner({"status": "triage_rejected", "pushed": False})
    rem = ExternalRemediation(_settings(tmp_path), spawn=spawn)

    label = await _consider(client, gate, story, rem)

    assert label == "dispatched"  # story metadata carries project="p"
    assert rem._task is not None
    await rem._task


async def test_project_can_disable_converge_via_context_doc(
    tmp_path: Path,
) -> None:
    """Per-project ``develop_external_review_converge = false`` (default on,
    ADR 0011 decision 6) stops dispatch; detection is untouched."""
    client = FakeLithosClient()
    await client.note_write(
        title="p project context",
        content="ctx",
        path="projects/p/p-project-context.md",
        metadata={"develop_external_review_converge": False},
    )
    story, gate = await _gate_with_story(client)
    spawn, calls = _spawner(None)
    rem = ExternalRemediation(_settings(tmp_path), spawn=spawn)

    assert await _consider(client, gate, story, rem) == "project_disabled"
    assert calls == []


# ── run completion ─────────────────────────────────────────────────────


async def test_nothing_to_ingest_gives_the_round_back(tmp_path: Path) -> None:
    """converge exiting 0 without a JSON result means it found nothing live
    to ingest (suppression drift between sweep and CLI) — no agent time was
    spent, so the round is returned to the budget."""
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    spawn, _calls = _spawner(None, rc=0)  # exit 0, no json written
    rem = ExternalRemediation(_settings(tmp_path), spawn=spawn)

    assert await _consider(client, gate, story, rem) == "dispatched"
    assert rem._task is not None
    await rem._task

    marker = await _marker(client, gate.id)
    assert marker["rounds_used"] == 0  # incremented at dispatch, given back


async def test_failed_run_keeps_the_round_and_posts_friction(
    tmp_path: Path,
) -> None:
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    spawn, _calls = _spawner(None, rc=1)
    rem = ExternalRemediation(_settings(tmp_path), spawn=spawn)

    assert await _consider(client, gate, story, rem) == "dispatched"
    assert rem._task is not None
    await rem._task

    marker = await _marker(client, gate.id)
    assert marker["rounds_used"] == 1  # the round is spent
    friction = next(f for f in _findings(client) if "[Friction]" in f)
    assert "converge" in friction


async def test_unpushed_result_does_not_record_a_loom_sha(tmp_path: Path) -> None:
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    spawn, _calls = _spawner(
        {"status": "not_converged", "pushed": False, "message": "stalled"}
    )
    rem = ExternalRemediation(_settings(tmp_path), spawn=spawn)

    assert await _consider(client, gate, story, rem) == "dispatched"
    assert rem._task is not None
    await rem._task

    marker = await _marker(client, gate.id)
    assert marker["rounds_used"] == 1
    assert marker["last_loom_pushed_sha"] == ""
    outcome = next(f for f in _findings(client) if "remediation" in f)
    assert "not_converged" in outcome


# ── PR #346 review round 1 (five blocking findings) ────────────────────


async def test_human_push_resets_even_before_any_loom_push() -> None:
    """PR #346 review F2: a PR whose spending rounds never pushed
    (triage_rejected / not_converged) has last_loom_pushed_sha == "" — a
    human push must still reset; only the FIRST sighting is initialization."""
    client = FakeLithosClient()
    _story, gate = await _gate_with_story(client)
    await client.task_update(
        task_id=gate.id,
        metadata={
            REMEDIATION_KEY: {
                "pr_url": _PR_URL,
                "rounds_used": 2,  # exhausted without ever pushing
                "last_loom_pushed_sha": "",
                "last_seen_head_sha": _HEAD,
            }
        },
    )
    gate = await client.task_get(task_id=gate.id)
    assert gate is not None
    rem = ExternalRemediation(_settings(Path("/tmp/x")), spawn=_spawner(None)[0])
    spec = parse_pr_gate(gate)
    assert spec is not None

    human_sha = "c4" * 20
    budget = await rem.observe_head(gate, spec, _pr(human_sha), _ctx(client))

    assert budget.rounds_used == 0  # the operator took ownership
    assert budget.last_seen_head_sha == human_sha


async def test_failed_budget_reservation_blocks_dispatch(tmp_path: Path) -> None:
    """PR #346 review F3: the increment-before-run rule only bounds anything
    if the increment actually LANDED — a failed reservation must not spawn."""
    from lithos_loom.errors import LithosClientError

    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    spawn, calls = _spawner(None)
    rem = ExternalRemediation(_settings(tmp_path), spawn=spawn)

    original = client.task_update

    async def failing_update(**kwargs: Any) -> Any:
        if REMEDIATION_KEY in (kwargs.get("metadata") or {}):
            raise LithosClientError("server_error", "boom")
        return await original(**kwargs)

    client.task_update = failing_update  # type: ignore[method-assign]

    label = await _consider(client, gate, story, rem)

    assert label == "reservation_failed"
    assert calls == []
    assert rem._task is None


async def test_parked_trigger_survives_busy_and_resumes(tmp_path: Path) -> None:
    """PR #346 review F1 + re-review 1: the trigger is parked by INGESTION
    (atomically with the seen marks — see test_external_reviews); consider's
    busy path merely leaves it in place, and a later quiet sweep resumes it,
    consuming the trigger with the budget reservation."""
    from lithos_loom.subscriptions.external_remediation import PENDING_KEY

    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    # The trigger, as ingestion's atomic marker write parks it.
    await client.task_update(
        task_id=gate.id, metadata={PENDING_KEY: {"pr_url": _PR_URL}}
    )
    gate = await client.task_get(task_id=gate.id)
    assert gate is not None
    resumed: list[list[str]] = []

    async def resume_spawn(cmd: list[str]) -> tuple[int, str]:
        resumed.append(cmd)
        return 0, ""

    rem = ExternalRemediation(_settings(tmp_path), spawn=resume_spawn)
    rem._task = asyncio.create_task(asyncio.sleep(30))  # a run in flight
    try:
        # While busy: consider defers and the trigger stays parked.
        assert await _consider(client, gate, story, rem) == "deferred_busy"
        parked = await client.task_get(task_id=gate.id)
        assert parked is not None
        assert parked.metadata.get(PENDING_KEY) == {"pr_url": _PR_URL}
    finally:
        rem._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await rem._task

    # A later quiet sweep (no new material — even a restarted daemon, since
    # the trigger is durable) resumes off the trigger.
    resumer = ExternalRemediation(_settings(tmp_path), spawn=resume_spawn)
    spec = parse_pr_gate(gate)
    assert spec is not None
    budget = RemediationBudget(pr_url=_PR_URL, rounds_used=1)
    label = await resumer.resume_pending(
        gate, spec, story, budget, _github(), _ctx(client)
    )
    assert label == "dispatched"
    run = resumer._task
    assert run is not None
    await run
    assert len(resumed) == 1
    # The trigger is consumed atomically with the reservation.
    refreshed = await client.task_get(task_id=gate.id)
    assert refreshed is not None
    assert PENDING_KEY not in refreshed.metadata


async def test_pending_marker_minted_only_for_a_dispatchable_batch(
    tmp_path: Path,
) -> None:
    """PR #346 re-review 3: dispatchability (trust + own-sha) is evaluated
    BEFORE the atomic parking write, so the trigger only ever exists for a
    batch that would genuinely dispatch — an undispatchable batch neither
    parks nor (later) clears, closing both the older-debt-erasure and the
    own-sha crash-window holes at the root."""
    from lithos_loom.gates import PrGateSpec
    from lithos_loom.subscriptions.external_remediation import PENDING_KEY

    client = FakeLithosClient()
    spec = PrGateSpec(repo="agent-lore/lithos-lens", pr_number=62, pr_url=_PR_URL)
    rem = ExternalRemediation(_settings(tmp_path), spawn=_spawner(None)[0])
    budget = RemediationBudget(pr_url=_PR_URL, last_loom_pushed_sha=_LOOM_SHA)
    provider = rem.pending_marker_provider(
        spec, "story-1", budget, _github(), _ctx(client)
    )

    # Trusted material at a fresh sha: parked.
    assert await provider(_act(_review())) == {PENDING_KEY: {"pr_url": _PR_URL}}
    # Untrusted-only material: never parked.
    untrusted = rem.pending_marker_provider(
        spec, "story-1", budget, _github(permission="read"), _ctx(client)
    )
    assert await untrusted(_act(_review(author="drive-by"))) is None
    # Own-sha-only material (a re-review of loom's own fix): never parked —
    # so no crash between park and clear can ever hand resume_pending a
    # trigger that bypasses the own-sha loop guard.
    assert await provider(_act(_review(commit_id=_LOOM_SHA))) is None

    # Budget off / no story: no parking either.
    no_story = rem.pending_marker_provider(spec, None, budget, _github(), _ctx(client))
    assert await no_story(_act(_review())) is None
    disabled = ExternalRemediation(
        _settings(tmp_path, budget=0), spawn=_spawner(None)[0]
    )
    off = disabled.pending_marker_provider(
        spec, "story-1", budget, _github(), _ctx(client)
    )
    assert await off(_act(_review())) is None


async def test_resume_pending_is_a_noop_without_a_trigger(tmp_path: Path) -> None:
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    rem = ExternalRemediation(_settings(tmp_path), spawn=_spawner(None)[0])
    spec = parse_pr_gate(gate)
    assert spec is not None
    budget = RemediationBudget(pr_url=_PR_URL)

    label = await rem.resume_pending(gate, spec, story, budget, _github(), _ctx(client))

    assert label is None
    assert rem._task is None


async def test_resume_pending_respects_the_budget_and_keeps_the_trigger(
    tmp_path: Path,
) -> None:
    """An exhausted budget stops a pending resume too — but keeps the trigger,
    so a human push (which resets the budget) lets it fire later."""
    from lithos_loom.subscriptions.external_remediation import PENDING_KEY

    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    await client.task_update(
        task_id=gate.id, metadata={PENDING_KEY: {"pr_url": _PR_URL}}
    )
    gate = await client.task_get(task_id=gate.id)
    assert gate is not None
    spawn, calls = _spawner(None)
    rem = ExternalRemediation(_settings(tmp_path), spawn=spawn)
    spec = parse_pr_gate(gate)
    assert spec is not None
    budget = RemediationBudget(pr_url=_PR_URL, rounds_used=2)

    label = await rem.resume_pending(gate, spec, story, budget, _github(), _ctx(client))

    assert label == "exhausted"
    assert calls == []
    refreshed = await client.task_get(task_id=gate.id)
    assert refreshed is not None
    assert PENDING_KEY in refreshed.metadata  # kept for after a reset


async def test_resume_pending_holds_on_a_decision_gate_and_keeps_the_trigger(
    tmp_path: Path,
) -> None:
    """#387 (opus round 1): a parked trigger must not fire past an OPEN
    decision gate on the budget — rounds remaining or not. Once the
    operator has completed the gate, the decision is made: the hold lifts,
    the marker forgets the gate, and the trigger fires."""
    from lithos_loom.subscriptions.external_remediation import PENDING_KEY

    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    decision = await client.task_create(
        title="Needs human", task_type="gate", metadata={"gate_type": "human"}
    )
    await client.task_update(
        task_id=gate.id, metadata={PENDING_KEY: {"pr_url": _PR_URL}}
    )
    gate = await client.task_get(task_id=gate.id)
    assert gate is not None
    spawn, calls = _spawner(None)
    rem = ExternalRemediation(_settings(tmp_path), spawn=spawn)
    spec = parse_pr_gate(gate)
    assert spec is not None
    budget = RemediationBudget(
        pr_url=_PR_URL,
        rounds_used=1,
        needs_human_gate_id=decision,
        needs_human_reason="disputed",
    )

    label = await rem.resume_pending(gate, spec, story, budget, _github(), _ctx(client))

    assert label == "escalated" and calls == []
    refreshed = await client.task_get(task_id=gate.id)
    assert refreshed is not None and PENDING_KEY in refreshed.metadata

    # the operator decides (completes the gate) — no push involved
    await client.task_complete(task_id=decision, agent="dave")
    label = await rem.resume_pending(gate, spec, story, budget, _github(), _ctx(client))
    assert label == "dispatched"
    assert rem._task is not None
    await rem._task
    marker = await _marker(client, gate.id)
    assert marker["needs_human_gate_id"] == "" and marker["needs_human_reason"] == ""


async def test_completing_the_decision_gate_at_the_budget_limit_re_arms_and_dispatches(
    tmp_path: Path,
) -> None:
    """PR #389 review (High): the motivating run was round 2/2 — with the
    exhausted check first, completing the gate (the promised no-push
    decision) could never release anything. The gate is checked FIRST, and
    lifting it is the operator's consent to continue: the budget re-arms
    (rounds reset, the push attribution kept) and the next batch — or the
    parked trigger — dispatches. Both entry points, at the limit."""
    from lithos_loom.subscriptions.external_remediation import PENDING_KEY

    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    decision = await client.task_create(
        title="Needs human", task_type="gate", metadata={"gate_type": "human"}
    )
    await client.task_complete(task_id=decision, agent="dave")
    budget = RemediationBudget(
        pr_url=_PR_URL,
        rounds_used=2,
        last_loom_pushed_sha="8d" * 20,
        last_seen_head_sha="8d" * 20,
        needs_human_gate_id=decision,
        needs_human_reason="disputed",
        no_change_refunded=True,  # this budget's refund was used before the gate
    )
    await client.task_update(
        task_id=gate.id, agent="a", metadata={REMEDIATION_KEY: budget.as_marker()}
    )
    gate = await client.task_get(task_id=gate.id)
    assert gate is not None
    spawn, calls = _spawner(
        {"status": "converged", "succeeded": True, "pushed": False, "rounds": 1}
    )
    rem = ExternalRemediation(_settings(tmp_path, budget=2), spawn=spawn)

    label = await _consider(client, gate, story, rem, budget=budget)

    assert label == "dispatched"
    assert rem._task is not None
    await rem._task
    assert len(calls) == 1
    marker = await _marker(client, gate.id)
    assert marker["needs_human_gate_id"] == "" and marker["needs_human_reason"] == ""
    assert marker["rounds_used"] == 1  # re-armed, then this dispatch's reservation
    assert marker["last_loom_pushed_sha"] == "8d" * 20  # attribution survives
    # PR #396 review (Medium): a FRESH budget — the no-change refund is
    # re-granted with the rounds, as a human push's new budget grants it
    assert marker["no_change_refunded"] is False

    # ...and the parked-trigger path at the limit, same rule
    client2 = FakeLithosClient()
    story2, gate2 = await _gate_with_story(client2)
    decision2 = await client2.task_create(
        title="Needs human", task_type="gate", metadata={"gate_type": "human"}
    )
    await client2.task_complete(task_id=decision2, agent="dave")
    await client2.task_update(
        task_id=gate2.id, agent="a", metadata={PENDING_KEY: {"pr_url": _PR_URL}}
    )
    gate2 = await client2.task_get(task_id=gate2.id)
    assert gate2 is not None
    spawn2, calls2 = _spawner(None)
    rem2 = ExternalRemediation(_settings(tmp_path, budget=2), spawn=spawn2)
    spec2 = parse_pr_gate(gate2)
    assert spec2 is not None
    budget2 = dataclasses.replace(budget, needs_human_gate_id=decision2)
    label = await rem2.resume_pending(
        gate2, spec2, story2, budget2, _github(), _ctx(client2)
    )
    assert label == "dispatched"
    assert rem2._task is not None
    await rem2._task
    assert len(calls2) == 1


async def test_an_open_decision_gate_at_the_limit_still_holds(tmp_path: Path) -> None:
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    decision = await client.task_create(
        title="Needs human", task_type="gate", metadata={"gate_type": "human"}
    )
    budget = RemediationBudget(
        pr_url=_PR_URL, rounds_used=2, needs_human_gate_id=decision
    )
    spawn, calls = _spawner(None)
    rem = ExternalRemediation(_settings(tmp_path, budget=2), spawn=spawn)
    assert await _consider(client, gate, story, rem, budget=budget) == "escalated"
    assert calls == []


async def test_completing_the_exhaustion_gate_re_arms_the_budget_too(
    tmp_path: Path,
) -> None:
    """One rule for every loom remediation gate: it IS the budget's stop, and
    the operator lifting it is consent to continue — an exhaustion gate
    completed without a push re-arms exactly like a disputed one (the
    actions say so)."""
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    decision = await client.task_create(
        title="Needs human", task_type="gate", metadata={"gate_type": "human"}
    )
    await client.task_complete(task_id=decision, agent="dave")
    budget = RemediationBudget(
        pr_url=_PR_URL,
        rounds_used=2,
        needs_human_gate_id=decision,
        needs_human_reason="remediation_exhausted",
    )
    spawn, calls = _spawner(None)
    rem = ExternalRemediation(_settings(tmp_path, budget=2), spawn=spawn)
    assert await _consider(client, gate, story, rem, budget=budget) == "dispatched"
    assert rem._task is not None
    await rem._task
    assert len(calls) == 1


async def test_consider_releases_the_hold_once_the_decision_gate_is_terminal(
    tmp_path: Path,
) -> None:
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    decision = await client.task_create(
        title="Needs human", task_type="gate", metadata={"gate_type": "human"}
    )
    await client.task_cancel(task_id=decision, agent="dave", reason="moot")
    spawn, calls = _spawner(None)
    rem = ExternalRemediation(_settings(tmp_path), spawn=spawn)
    budget = RemediationBudget(
        pr_url=_PR_URL, rounds_used=1, needs_human_gate_id=decision
    )
    label = await _consider(client, gate, story, rem, budget=budget)
    assert label == "dispatched"
    assert rem._task is not None
    await rem._task
    assert len(calls) == 1


async def test_undispatchable_batch_never_erases_older_parked_debt(
    tmp_path: Path,
) -> None:
    """PR #346 re-review 3, finding 1 (the reviewer's exact probe): a trusted
    batch's trigger is parked while the slot is busy; a LATER untrusted-only
    (or own-sha-only) batch must not clear that PR-wide bit — the older
    batch's marks are consumed, so its debt would be lost permanently."""
    from lithos_loom.subscriptions.external_remediation import PENDING_KEY

    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    await client.task_update(
        task_id=gate.id, metadata={PENDING_KEY: {"pr_url": _PR_URL}}
    )
    gate = await client.task_get(task_id=gate.id)
    assert gate is not None
    rem = ExternalRemediation(_settings(tmp_path), spawn=_spawner(None)[0])

    label = await _consider(
        client,
        gate,
        story,
        rem,
        ingest=_ingest(_review(author="drive-by")),
        github=_github(permission="read"),
    )
    assert label == "no_trusted"
    refreshed = await client.task_get(task_id=gate.id)
    assert refreshed is not None
    assert PENDING_KEY in refreshed.metadata  # older debt preserved

    # Same for an own-sha-only batch.
    budget = RemediationBudget(
        pr_url=_PR_URL, rounds_used=1, last_loom_pushed_sha=_LOOM_SHA
    )
    spec = parse_pr_gate(gate)
    assert spec is not None
    label = await rem.consider(
        gate,
        spec,
        story,
        budget,
        _ingest(_review(commit_id=_LOOM_SHA)),
        _github(),
        _ctx(client),
    )
    assert label == "own_sha_only"
    refreshed = await client.task_get(task_id=gate.id)
    assert refreshed is not None
    assert PENDING_KEY in refreshed.metadata


async def test_project_settings_read_failure_fails_closed(tmp_path: Path) -> None:
    """PR #346 re-review 2: an unreadable context doc must NOT authorize an
    autonomous code-pushing run — a project's explicit opt-out could be
    sitting in it. Fail closed, keep the parked trigger for a later retry,
    and spend nothing."""
    from lithos_loom.errors import LithosClientError
    from lithos_loom.subscriptions.external_remediation import (
        PENDING_KEY,
        REMEDIATION_KEY,
    )

    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    await client.task_update(
        task_id=gate.id, metadata={PENDING_KEY: {"pr_url": _PR_URL}}
    )
    gate = await client.task_get(task_id=gate.id)
    assert gate is not None

    async def failing_note_read(**kwargs: Any) -> Any:
        raise LithosClientError("server_error", "lithos down")

    client.note_read = failing_note_read  # type: ignore[method-assign]
    spawn, calls = _spawner(None)
    rem = ExternalRemediation(_settings(tmp_path), spawn=spawn)

    label = await _consider(client, gate, story, rem)

    assert label == "project_settings_unavailable"
    assert calls == []
    assert rem._task is None
    refreshed = await client.task_get(task_id=gate.id)
    assert refreshed is not None
    assert PENDING_KEY in refreshed.metadata  # retained → retried later
    marker = refreshed.metadata.get(REMEDIATION_KEY)
    assert marker is None or marker.get("rounds_used", 0) == 0  # nothing spent


async def test_shutdown_cancels_the_inflight_run(tmp_path: Path) -> None:
    """PR #346 review F5: watcher shutdown must own the in-flight run."""
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    started = asyncio.Event()

    async def hanging_spawn(cmd: list[str]) -> tuple[int, str]:
        started.set()
        await asyncio.sleep(3600)
        return 0, ""

    rem = ExternalRemediation(_settings(tmp_path), spawn=hanging_spawn)
    assert await _consider(client, gate, story, rem) == "dispatched"
    await started.wait()

    await rem.shutdown()

    assert rem._task is not None and rem._task.done()
    assert not rem.busy


async def test_default_spawn_terminates_the_child_on_cancel(
    tmp_path: Path,
) -> None:
    """PR #346 review F5: cancelling the spawn must terminate the subprocess,
    not orphan it to keep running after loom stopped."""
    import os

    from lithos_loom.subscriptions.external_remediation import spawn_converge

    pid_file = tmp_path / "child.pid"
    child_src = (
        "import os, sys, time; "
        "open(sys.argv[1], 'w').write(str(os.getpid())); "
        "time.sleep(60)"
    )
    task = asyncio.create_task(
        spawn_converge([sys.executable, "-c", child_src, str(pid_file)])
    )
    for _ in range(100):  # wait for the child to record its pid
        if pid_file.exists() and pid_file.read_text():
            break
        await asyncio.sleep(0.05)
    pid = int(pid_file.read_text())

    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    for _ in range(100):  # the child must die promptly
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.05)
    else:
        raise AssertionError(f"child {pid} still alive after cancellation")


# ── conversation comments (#353) ──────────────────────────────────────


def _issue_comment(
    comment_id: int = 40, *, author: str = "davesnowdon"
) -> IssueComment:
    return IssueComment(
        comment_id=comment_id,
        author=author,
        body="Verdict: not ready — two P1 gaps",
        html_url=f"{_PR_URL}#issuecomment-{comment_id}",
    )


async def test_trusted_conversation_comment_dispatches_even_after_a_loom_push(
    tmp_path: Path,
) -> None:
    """A conversation comment reviews no particular sha, so the own-sha guard
    (a bot re-reviewing loom's in-flight fix) never applies to it — a human
    verdict after loom's push is exactly the material that must dispatch."""
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    (tmp_path / "repo").mkdir()
    spawn, calls = _spawner({"status": "converged", "pushed": False})
    rem = ExternalRemediation(_settings(tmp_path), spawn=spawn)
    spec = parse_pr_gate(gate)
    assert spec is not None
    budget = RemediationBudget(
        pr_url=_PR_URL, rounds_used=0, last_loom_pushed_sha=_LOOM_SHA
    )

    label = await rem.consider(
        gate,
        spec,
        story,
        budget,
        _ingest(_issue_comment()),
        _github("admin"),
        _ctx(client),
    )
    assert label == "dispatched"
    assert rem._task is not None
    await rem._task
    assert calls and "--from-github" in calls[0]


async def test_untrusted_conversation_comment_never_dispatches(tmp_path: Path) -> None:
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    spawn, calls = _spawner(None)
    rem = ExternalRemediation(_settings(tmp_path), spawn=spawn)
    spec = parse_pr_gate(gate)
    assert spec is not None

    label = await rem.consider(
        gate,
        spec,
        story,
        RemediationBudget(pr_url=_PR_URL),
        _ingest(_issue_comment(author="stranger")),
        _github("none"),
        _ctx(client),
    )

    assert label == "no_trusted"
    assert calls == []


async def test_pending_provider_parks_for_a_trusted_conversation_batch(
    tmp_path: Path,
) -> None:
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    rem = ExternalRemediation(_settings(tmp_path))
    spec = parse_pr_gate(gate)
    assert spec is not None
    provider = rem.pending_marker_provider(
        spec, story, RemediationBudget(pr_url=_PR_URL), _github("write"), _ctx(client)
    )

    assert await provider(_act(_issue_comment())) == {
        "external_remediation_pending": {"pr_url": _PR_URL}
    }


# ── the argv must be accepted by the REAL converge parser ────────────────────
#
# Every spawn above is faked, so the argv the dispatcher builds was never run
# through Typer. In production it was, and the first live dispatch (lens#78,
# 2026-09-05) died on `No such option: -c` — the config flag the watcher child
# understands is not the one `develop converge` exposes — spending a budget
# round on a usage error. This test invokes the actual CLI with the actual
# argv (the converge body stopped at its first host-config read).


def test_dispatch_argv_is_accepted_by_the_real_converge_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    from lithos_loom.cli import converge as converge_cli
    from lithos_loom.gates import PrGateSpec
    from lithos_loom.main import app

    class _Stop(Exception):
        pass

    seen: list[Path | None] = []

    def fake_load_config(path: Path | None) -> None:
        seen.append(path)
        raise _Stop

    monkeypatch.setattr(converge_cli, "load_config", fake_load_config)
    host_cfg = tmp_path / "host.toml"
    rem = ExternalRemediation(
        _settings(tmp_path, config_path=host_cfg), spawn=_spawner(None)[0]
    )
    spec = PrGateSpec(repo="agent-lore/lithos-lens", pr_number=78, pr_url=_PR_URL)
    cmd = rem._command(spec, tmp_path / "repo", tmp_path / "out.json", "story-1")
    assert cmd[:3] == [sys.executable, "-m", "lithos_loom"]

    result = CliRunner().invoke(app, cmd[3:])

    # Typer's usage error is exit 2 — the class of failure this pins.
    assert result.exit_code != 2, result.output
    assert "No such option" not in result.output
    assert isinstance(result.exception, _Stop), result.output
    # ...and the host config the child was booted with reached the run.
    assert seen == [host_cfg]


# ── the argv carries the story so converge resolves ITS develop settings ────


def test_dispatch_argv_names_the_story(tmp_path: Path) -> None:
    # lens#78 (2026-09-07): the dispatched converge ran at the CLI default of 5
    # rounds while the project doc said 8 — converge resolved nothing from the
    # project. The dispatcher hands it the story so it resolves the same
    # settings the daemon path would.
    from lithos_loom.gates import PrGateSpec

    rem = ExternalRemediation(_settings(tmp_path), spawn=_spawner(None)[0])
    spec = PrGateSpec(repo="agent-lore/lithos-lens", pr_number=78, pr_url=_PR_URL)
    cmd = rem._command(spec, tmp_path / "repo", tmp_path / "out.json", "story-9")
    assert cmd[cmd.index("--story") + 1] == "story-9"


# ── S5b: exhaustion is an escalation, not a finding ─────────────────────────
#
# PRD S5b: "On exhaustion → S7 human gate." Until now the exhausted round's
# outcome was a finding on the story and nothing else — the August failure
# mode (a stop nobody is told about). lens#78's round 2/2 ended not_converged
# at 07:24 and was found twelve hours later by looking.


class _RecordingNotifier:
    def __init__(self) -> None:
        self.notices: list[Any] = []

    async def needs_human(self, notice: Any) -> list[str]:
        self.notices.append(notice)
        return []


async def _human_gates(client: FakeLithosClient) -> list[Any]:
    tasks = await client.task_list(status="open")
    return [
        t
        for t in tasks
        if getattr(t, "task_type", "") == "gate"
        and t.metadata.get("gate_type") == "human"
    ]


def _not_converged_payload() -> dict:
    return {
        "status": "not_converged",
        "pushed": False,
        "pushed_sha": "",
        "rounds": 5,
        "develop_status": "max_rounds",
        "total_cost_usd": 67.95,
        "message": "NOT approved after 5 round(s) (max_rounds)",
    }


async def test_exhausted_budget_after_an_unconverged_run_raises_a_needs_human_gate(
    tmp_path: Path,
) -> None:
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    notifier = _RecordingNotifier()
    spawn, _calls = _spawner(_not_converged_payload())
    rem = ExternalRemediation(
        _settings(tmp_path, budget=1, notifier=notifier), spawn=spawn
    )

    assert await _consider(client, gate, story, rem) == "dispatched"
    assert rem._task is not None
    await rem._task

    gates = await _human_gates(client)
    assert len(gates) == 1
    human = gates[0]
    assert human.metadata["raised_by"] == "loom"
    assert human.metadata["escalation_reason"] == "remediation_exhausted"
    assert human.metadata["route"] == "external-remediation"
    assert human.metadata["story_id"] == story
    assert _PR_URL in human.metadata["escalation_summary"]
    brief = human.metadata["run_brief"]
    assert brief["pr_url"] == _PR_URL
    assert brief["rounds_used"] == 1 and brief["budget"] == 1
    assert brief["last_status"] == "not_converged"
    assert brief["cost_usd"] == 67.95
    # the gate blocks the story (waits_on_gate edge) and is recorded on it
    fresh = await client.task_get(task_id=story)
    assert fresh is not None
    assert fresh.metadata["needs_human_gate_id"] == human.id
    # ...and on the PR gate's budget marker, so it is raised once
    marker = await _marker(client, gate.id)
    assert marker["needs_human_gate_id"] == human.id
    # the operator is told: [NeedsHuman] on the story + the push sinks
    needs = [f for f in _findings(client) if f.startswith("[NeedsHuman]")]
    assert len(needs) == 1
    assert "remediation_exhausted" in needs[0] and human.id in needs[0]
    assert "push the fix branch" in needs[0]  # remediation's actions, not re-dispatch
    assert [n.reason for n in notifier.notices] == ["remediation_exhausted"]
    assert notifier.notices[0].gate_id == human.id
    assert notifier.notices[0].route == "external-remediation"


async def test_an_already_clean_run_refunds_the_round_and_never_escalates(
    tmp_path: Path,
) -> None:
    """#380 (lens #83): a run that changed nothing because every external
    finding needed no change is reported, not remediated — the reserved
    round comes back (the own-sha re-review precedent), the outcome finding
    names the dispositions, and the last budgeted round raises no gate."""
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    notifier = _RecordingNotifier()
    spawn, _calls = _spawner(_already_clean_payload())
    rem = ExternalRemediation(
        _settings(tmp_path, budget=1, notifier=notifier), spawn=spawn
    )

    assert await _consider(client, gate, story, rem) == "dispatched"
    assert rem._task is not None
    await rem._task

    marker = await _marker(client, gate.id)
    assert marker["rounds_used"] == 0  # reserved at dispatch, refunded on the result
    assert await _human_gates(client) == []
    assert notifier.notices == []
    outcome = next(f for f in _findings(client) if "remediation outcome" in f)
    assert "already_clean" in outcome
    assert "f-001 by davesnowdon: no_change_needed" in outcome
    assert "refunded" in outcome


def _already_clean_payload() -> dict:
    return {
        "status": "already_clean",
        "succeeded": True,
        "pushed": False,
        "pushed_sha": "",
        "rounds": 1,
        "develop_status": "failed",
        "total_cost_usd": 1.04,
        "message": "every external finding needed no change (f-001)",
        "external_outcomes": [
            {
                "finding_id": "f-001",
                "author": "davesnowdon",
                "source": "conversation",
                "stream": "issue_comment",
                "activity_id": 9,
                "reply_mode": "conversation",
                "thread_url": "https://github.com/o/r/pull/142#issuecomment-9",
                "disposition": "no_change_needed",
                "detail": "an approval verdict, not a defect",
            }
        ],
    }


async def test_the_no_change_refund_is_granted_once_per_budget(tmp_path: Path) -> None:
    """opus round 1 (Medium): an already_clean run is a PAID run (triage +
    a coder turn), so an unbounded refund removes the S5b spend bound —
    five "thanks" comments would be five paid runs at 0/2. One refund per
    budget: the common case (one approval comment) stays free, the second
    keeps its round, and a human push (a fresh budget) re-grants it."""
    from lithos_loom.subscriptions.remediation_outcome import record_result

    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    spec = parse_pr_gate(gate)
    assert spec is not None
    first = RemediationBudget(pr_url=_PR_URL, rounds_used=1)
    await record_result(
        _ctx(client),
        gate_id=gate.id,
        story_id=story,
        spec=spec,
        budget=first,
        budget_limit=2,
        notifier=None,
        data=_already_clean_payload(),
    )
    marker = await _marker(client, gate.id)
    assert marker["rounds_used"] == 0 and marker["no_change_refunded"] is True

    refreshed = await client.task_get(task_id=gate.id)
    assert refreshed is not None
    second = dataclasses.replace(read_budget(refreshed, _PR_URL), rounds_used=1)
    # the reservation a dispatch would have written before the second run
    await client.task_update(
        task_id=gate.id, agent="a", metadata={REMEDIATION_KEY: second.as_marker()}
    )
    await record_result(
        _ctx(client),
        gate_id=gate.id,
        story_id=story,
        spec=spec,
        budget=second,
        budget_limit=2,
        notifier=None,
        data=_already_clean_payload(),
    )
    marker = await _marker(client, gate.id)
    assert marker["rounds_used"] == 1  # kept
    outcomes = [f for f in _findings(client) if "remediation outcome" in f]
    assert "refunded" in outcomes[0] and "already used" in outcomes[1]
    assert await _human_gates(client) == []


async def test_a_no_change_refund_that_cannot_land_never_raises_a_false_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """opus round 1 (High): the refund write must not escape into the crash
    handler, which re-reads the reserved budget and would raise the very
    `remediation_exhausted` gate #380 exists to prevent. A transport error
    (not a LithosClientError) during the refund: the write is retried like
    the other refunds, the outcome finding is still posted, the round is
    reported as kept, no gate."""
    from lithos_loom.subscriptions import remediation_outcome as ro

    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    spec = parse_pr_gate(gate)
    assert spec is not None
    real_update = client.task_update
    attempts: list[int] = []

    async def failing_update(**kw):
        meta = kw.get("metadata") or {}
        if kw.get("task_id") == gate.id and REMEDIATION_KEY in meta:
            attempts.append(1)
            raise RuntimeError("mcp session closed")  # a raw transport error
        return await real_update(**kw)

    monkeypatch.setattr(client, "task_update", failing_update)
    monkeypatch.setattr(ro, "REFUND_RETRY_DELAYS", ())  # no sleeping in the test
    await ro.record_result(
        _ctx(client),
        gate_id=gate.id,
        story_id=story,
        spec=spec,
        budget=RemediationBudget(pr_url=_PR_URL, rounds_used=2),
        budget_limit=2,
        notifier=None,
        data=_already_clean_payload(),
    )
    assert attempts  # it tried
    outcome = next(f for f in _findings(client) if "remediation outcome" in f)
    assert "did not land" in outcome and "stays spent" in outcome
    assert await _human_gates(client) == []  # succeeded: never the exhaustion gate


async def test_rounds_remaining_after_an_unconverged_run_does_not_escalate(
    tmp_path: Path,
) -> None:
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    notifier = _RecordingNotifier()
    spawn, _calls = _spawner(_not_converged_payload())
    rem = ExternalRemediation(
        _settings(tmp_path, budget=2, notifier=notifier), spawn=spawn
    )
    await _consider(client, gate, story, rem)
    assert rem._task is not None
    await rem._task
    assert await _human_gates(client) == []
    assert not [f for f in _findings(client) if f.startswith("[NeedsHuman]")]
    assert notifier.notices == []
    # the outcome finding still says a round remains, as before
    outcome = next(f for f in _findings(client) if "remediation outcome" in f)
    assert "round 1/2" in outcome


async def test_converged_on_the_last_round_does_not_escalate(tmp_path: Path) -> None:
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    spawn, _calls = _spawner(
        {"status": "converged", "pushed": True, "pushed_sha": "ab" * 20, "rounds": 1}
    )
    rem = ExternalRemediation(_settings(tmp_path, budget=1), spawn=spawn)
    await _consider(client, gate, story, rem)
    assert rem._task is not None
    await rem._task
    assert await _human_gates(client) == []


async def test_a_raised_decision_gate_holds_dispatch_until_a_human_push(
    tmp_path: Path,
) -> None:
    # A gate already raised for this budget (marker carries its id) means a
    # decision is outstanding: nothing dispatches on it — rounds remaining or
    # not — until a human push resets the budget (#387: the disputed gate is
    # raised with rounds to spare, so "exhausted" alone no longer covers it).
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    decision = await client.task_create(
        title="Needs human", task_type="gate", metadata={"gate_type": "human"}
    )
    await client.task_update(
        task_id=gate.id,
        agent="a",
        metadata={
            REMEDIATION_KEY: {
                "pr_url": _PR_URL,
                "rounds_used": 1,
                "last_loom_pushed_sha": "",
                "last_seen_head_sha": _HEAD,
                "needs_human_gate_id": decision,
                "needs_human_reason": "disputed",
            }
        },
    )
    gate = await client.task_get(task_id=gate.id)
    assert gate is not None
    spawn, calls = _spawner(_not_converged_payload())
    rem = ExternalRemediation(_settings(tmp_path, budget=2), spawn=spawn)
    label = await _consider(client, gate, story, rem, budget=read_budget(gate, _PR_URL))
    assert label == "escalated"
    assert rem._task is None and calls == []
    assert len(await _human_gates(client)) == 1  # the one that already stands
    marker = await _marker(client, gate.id)
    assert marker["rounds_used"] == 1
    assert marker["needs_human_gate_id"] == decision


async def test_exhaustion_escalates_once_per_budget(tmp_path: Path) -> None:
    # The recorder's own guard: a result landing at exhaustion while the
    # marker already names a gate raises no second one.
    from lithos_loom.subscriptions.remediation_outcome import record_result

    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    spec = parse_pr_gate(gate)
    assert spec is not None
    budget = RemediationBudget(
        pr_url=_PR_URL, rounds_used=2, needs_human_gate_id="gate-already-raised"
    )
    await record_result(
        _ctx(client),
        gate_id=gate.id,
        story_id=story,
        spec=spec,
        budget=budget,
        budget_limit=2,
        notifier=None,
        data=_not_converged_payload(),
    )
    assert await _human_gates(client) == []


async def test_failed_run_without_a_result_at_exhaustion_escalates(
    tmp_path: Path,
) -> None:
    # The `-c` bug shape: the subprocess died before producing a result. The
    # round is spent, and if that spent the budget the operator must hear.
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    notifier = _RecordingNotifier()
    spawn, _calls = _spawner(None, rc=2)
    rem = ExternalRemediation(
        _settings(tmp_path, budget=1, notifier=notifier), spawn=spawn
    )
    await _consider(client, gate, story, rem)
    assert rem._task is not None
    await rem._task
    gates = await _human_gates(client)
    assert len(gates) == 1
    assert gates[0].metadata["run_brief"]["last_status"] == "failed"
    assert [f for f in _findings(client) if f.startswith("[Friction]")]
    assert len(notifier.notices) == 1


def _reverted_payload() -> dict:
    # lens #84 (#387): the loop converged and pushed a tree that UNDID the
    # external fix; the CLI reports the finding reverted and the run not
    # succeeded.
    return {
        "status": "converged",
        "succeeded": False,
        "pushed": True,
        "pushed_sha": "8d" * 20,
        "rounds": 3,
        "develop_status": "approved",
        "total_cost_usd": 12.5,
        "message": "converged; external f-001 REVERTED — operator decision needed",
        "external_outcomes": [
            {
                "finding_id": "f-001",
                "author": "davesnowdon",
                "source": "review",
                "stream": "review_comment",
                "activity_id": 7,
                "reply_mode": "thread",
                "thread_url": "https://github.com/o/r/pull/142#discussion_r7",
                "disposition": "reverted",
                "detail": "the correctness reviewer holds it contradicts the "
                "acceptance criteria",
            }
        ],
    }


async def test_a_reverted_external_fix_raises_a_disputed_gate_with_rounds_to_spare(
    tmp_path: Path,
) -> None:
    """#387: the external reviewer and the story's acceptance criteria
    disagree — a decision, not a re-run. The gate is raised NOW (budget 1/2),
    names both sides, and stops dispatch until a human push."""
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    notifier = _RecordingNotifier()
    spawn, _calls = _spawner(_reverted_payload())
    rem = ExternalRemediation(
        _settings(tmp_path, budget=2, notifier=notifier), spawn=spawn
    )

    assert await _consider(client, gate, story, rem) == "dispatched"
    assert rem._task is not None
    await rem._task

    gates = await _human_gates(client)
    assert len(gates) == 1
    human = gates[0]
    assert human.metadata["escalation_reason"] == "disputed"
    assert human.metadata["route"] == "external-remediation"
    summary = human.metadata["escalation_summary"]
    assert "f-001" in summary and "acceptance criteria" in summary
    brief = human.metadata["run_brief"]
    assert brief["pr_url"] == _PR_URL
    assert brief["finding_id"] == "f-001"
    assert brief["thread_url"].endswith("#discussion_r7")
    assert brief["author"] == "davesnowdon"
    assert "contradicts" in brief["coder_reason"]
    assert brief["pushed_sha"] == "8d" * 20
    # recorded on the story and, once per budget, on the marker — with WHY
    fresh = await client.task_get(task_id=story)
    assert fresh is not None and fresh.metadata["needs_human_gate_id"] == human.id
    marker = await _marker(client, gate.id)
    assert marker["needs_human_gate_id"] == human.id
    assert marker["needs_human_reason"] == "disputed"
    assert marker["rounds_used"] == 1  # the round was spent, not refunded
    # ...and loom's own push stays attributed (opus round 1: the escalation
    # write must not clobber it, or the next sweep reads the revert push as
    # a human push, resets the budget and lifts the hold)
    assert marker["last_loom_pushed_sha"] == "8d" * 20
    assert marker["last_seen_head_sha"] == "8d" * 20
    needs = [f for f in _findings(client) if f.startswith("[NeedsHuman]")]
    assert len(needs) == 1
    assert "disputed" in needs[0] and human.id in needs[0]
    assert "acceptance criteria" in needs[0]  # the actions name the decision
    assert [n.reason for n in notifier.notices] == ["disputed"]
    # the outcome finding still records the run as before
    outcome = next(f for f in _findings(client) if "remediation outcome" in f)
    assert "f-001 by davesnowdon: reverted" in outcome


async def test_a_disputed_gate_is_raised_once_per_budget(tmp_path: Path) -> None:
    from lithos_loom.subscriptions.remediation_outcome import record_result

    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    spec = parse_pr_gate(gate)
    assert spec is not None
    budget = RemediationBudget(
        pr_url=_PR_URL,
        rounds_used=1,
        needs_human_gate_id="gate-already-raised",
        needs_human_reason="disputed",
    )
    await record_result(
        _ctx(client),
        gate_id=gate.id,
        story_id=story,
        spec=spec,
        budget=budget,
        budget_limit=2,
        notifier=None,
        data=_reverted_payload(),
    )
    assert await _human_gates(client) == []


def test_budget_marker_round_trips_the_escalation_reason() -> None:
    budget = RemediationBudget(
        pr_url=_PR_URL, needs_human_gate_id="g1", needs_human_reason="disputed"
    )
    marker = budget.as_marker()
    assert marker["needs_human_reason"] == "disputed"
    gate = SimpleNamespace(metadata={REMEDIATION_KEY: marker})
    assert read_budget(gate, _PR_URL) == budget
    # an older record without the field reads as the exhaustion it was
    del marker["needs_human_reason"]
    assert read_budget(gate, _PR_URL).needs_human_reason == ""


async def test_human_push_reset_also_clears_the_escalation(tmp_path: Path) -> None:
    # A human push resets the budget (S5b) — and with it the raised-gate
    # record, so the NEXT exhaustion escalates again.
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    await client.task_update(
        task_id=gate.id,
        agent="a",
        metadata={
            REMEDIATION_KEY: {
                "pr_url": _PR_URL,
                "rounds_used": 2,
                "last_loom_pushed_sha": "",
                "last_seen_head_sha": _HEAD,
                "needs_human_gate_id": "gate-1",
            }
        },
    )
    gate = await client.task_get(task_id=gate.id)
    assert gate is not None
    assert read_budget(gate, _PR_URL).needs_human_gate_id == "gate-1"
    rem = ExternalRemediation(_settings(tmp_path), spawn=_spawner(None)[0])
    pr = SimpleNamespace(head_sha="9f" * 20)
    spec = parse_pr_gate(gate)
    assert spec is not None
    budget = await rem.observe_head(gate, spec, pr, _ctx(client))
    assert budget.rounds_used == 0
    assert budget.needs_human_gate_id == ""


async def test_run_completion_is_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # The dispatcher logged the dispatch and nothing after it — a run's end
    # was invisible in the daemon log (lens#78, 2026-09-07).
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    spawn, _calls = _spawner(_not_converged_payload())
    rem = ExternalRemediation(_settings(tmp_path, budget=2), spawn=spawn)
    with caplog.at_level(logging.INFO, logger="test-external-remediation"):
        await _consider(client, gate, story, rem)
        assert rem._task is not None
        await rem._task
    done = [r.message for r in caplog.records if "finished" in r.message]
    assert len(done) == 1
    assert _PR_URL in done[0] and "not_converged" in done[0] and "1/2" in done[0]


# ── PR #361 review ───────────────────────────────────────────────────────────


async def test_a_successful_triage_rejected_last_round_does_not_escalate(
    tmp_path: Path,
) -> None:
    # Review finding 1: `triage_rejected` is a SUCCESS (every external claim
    # refuted with evidence — nothing left for the operator), and the CLI's
    # own `succeeded` flag is the authority, not `status == "converged"`.
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    notifier = _RecordingNotifier()
    spawn, _calls = _spawner(
        {
            "status": "triage_rejected",
            "succeeded": True,
            "pushed": False,
            "pushed_sha": "",
            "message": "every external finding was rejected with evidence",
        }
    )
    rem = ExternalRemediation(
        _settings(tmp_path, budget=1, notifier=notifier), spawn=spawn
    )
    await _consider(client, gate, story, rem)
    assert rem._task is not None
    await rem._task
    assert await _human_gates(client) == []
    assert notifier.notices == []
    # PR #396 review: reported-not-remediated — the round comes back
    assert (await _marker(client, gate.id))["rounds_used"] == 0


async def test_a_triage_rejected_run_refunds_the_round_once_per_budget(
    tmp_path: Path,
) -> None:
    """PR #396 review (High): the lens #84 route on #380 — every external
    claim refuted by triage as already addressed at this head — is reported,
    not remediated, exactly like `already_clean`: the reserved round comes
    back, bounded by the same once-per-budget allowance (a second reported
    run keeps its round), and the last budgeted round raises no gate."""
    from lithos_loom.subscriptions.remediation_outcome import record_result

    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    spec = parse_pr_gate(gate)
    assert spec is not None
    payload = {
        "status": "triage_rejected",
        "succeeded": True,
        "pushed": False,
        "pushed_sha": "",
        "total_cost_usd": 0.61,
        "message": "triage rejected every external finding with cited evidence",
        "external_outcomes": [
            {
                "finding_id": "f-001",
                "author": "davesnowdon",
                "disposition": "rejected",
                "detail": "src/x.py:12 — the banner no longer exists at this head",
            }
        ],
    }
    await record_result(
        _ctx(client),
        gate_id=gate.id,
        story_id=story,
        spec=spec,
        budget=RemediationBudget(pr_url=_PR_URL, rounds_used=1),
        budget_limit=1,
        notifier=None,
        data=payload,
    )
    marker = await _marker(client, gate.id)
    assert marker["rounds_used"] == 0 and marker["no_change_refunded"] is True
    assert await _human_gates(client) == []  # the last round, refunded: no gate
    outcome = next(f for f in _findings(client) if "remediation outcome" in f)
    assert "triage_rejected" in outcome and "refunded" in outcome

    # the allowance is ONE per budget, shared with already_clean
    await client.task_update(
        task_id=gate.id,
        agent="a",
        metadata={
            REMEDIATION_KEY: dataclasses.replace(
                read_budget(await client.task_get(task_id=gate.id), _PR_URL),
                rounds_used=1,
            ).as_marker()
        },
    )
    await record_result(
        _ctx(client),
        gate_id=gate.id,
        story_id=story,
        spec=spec,
        budget=RemediationBudget(
            pr_url=_PR_URL, rounds_used=1, no_change_refunded=True
        ),
        budget_limit=1,
        notifier=None,
        data=_already_clean_payload(),
    )
    marker = await _marker(client, gate.id)
    assert marker["rounds_used"] == 1  # kept
    outcomes = [f for f in _findings(client) if "remediation outcome" in f]
    assert "already used" in outcomes[1]


async def test_a_result_without_the_succeeded_flag_falls_back_to_status(
    tmp_path: Path,
) -> None:
    # An older CLI's JSON (no `succeeded`) is judged by status alone.
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    spawn, _calls = _spawner({"status": "not_converged", "pushed": False})
    rem = ExternalRemediation(_settings(tmp_path, budget=1), spawn=spawn)
    await _consider(client, gate, story, rem)
    assert rem._task is not None
    await rem._task
    assert len(await _human_gates(client)) == 1


async def test_an_exception_in_the_run_at_exhaustion_still_escalates(
    tmp_path: Path,
) -> None:
    # Review finding 2: the run task's catch-all logged and dropped — an
    # OSError spawning (or reading the result) left the reserved final round
    # spent with no finding, no notice, no gate: the silent exhaustion this
    # PR exists to close, reborn one layer up.
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    notifier = _RecordingNotifier()

    async def boom(cmd: list[str]) -> tuple[int, str]:
        raise OSError("spawn failed: ENOENT")

    rem = ExternalRemediation(
        _settings(tmp_path, budget=1, notifier=notifier), spawn=boom
    )
    await _consider(client, gate, story, rem)
    assert rem._task is not None
    await rem._task
    frictions = [f for f in _findings(client) if f.startswith("[Friction]")]
    assert any("ENOENT" in f for f in frictions)
    gates = await _human_gates(client)
    assert len(gates) == 1
    assert gates[0].metadata["run_brief"]["last_status"] == "failed"
    assert len(notifier.notices) == 1


async def test_an_exception_with_rounds_remaining_posts_friction_only(
    tmp_path: Path,
) -> None:
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)

    async def boom(cmd: list[str]) -> tuple[int, str]:
        raise OSError("spawn failed")

    rem = ExternalRemediation(_settings(tmp_path, budget=2), spawn=boom)
    await _consider(client, gate, story, rem)
    assert rem._task is not None
    await rem._task
    assert [f for f in _findings(client) if f.startswith("[Friction]")]
    assert await _human_gates(client) == []


# ── PRD S3 watcher half: merge-gate and remediation hold each other ─────────


async def test_dispatch_is_held_while_a_merge_gate_runs_on_the_pr(
    tmp_path: Path,
) -> None:
    # A merge-gate run may push a merge commit at any moment; a converge
    # dispatched beside it would lose its leased push (a wasted round). The
    # pending trigger stays parked, so a later sweep resumes it.
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    spawn, calls = _spawner(None)
    held = {"on": True}
    rem = ExternalRemediation(
        _settings(tmp_path), spawn=spawn, hold=lambda pr_url: held["on"]
    )

    label = await _consider(client, gate, story, rem)
    assert label == "deferred_merge_gate"
    assert calls == []
    assert (await _marker(client, gate.id)) is None  # no reservation spent

    held["on"] = False
    label = await _consider(client, gate, story, rem)
    assert label == "dispatched"
    assert rem._task is not None
    await rem._task


async def test_busy_on_names_the_in_flight_pr(tmp_path: Path) -> None:
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    started = asyncio.Event()
    release = asyncio.Event()

    async def spawn(cmd: list[str]) -> tuple[int, str]:
        started.set()
        await release.wait()
        return 0, ""

    rem = ExternalRemediation(_settings(tmp_path), spawn=spawn)
    assert rem.busy_on(_PR_URL) is False
    assert await _consider(client, gate, story, rem) == "dispatched"
    await started.wait()
    assert rem.busy_on(_PR_URL) is True
    assert rem.busy_on("https://github.com/agent-lore/lithos-lens/pull/999") is False
    release.set()
    assert rem._task is not None
    await rem._task
    assert rem.busy_on(_PR_URL) is False


async def test_observe_head_is_inert_while_a_merge_gate_runs_on_the_pr() -> None:
    # PRD S3 review: a merge-gate run may push its merge commit at any moment
    # and records it as loom's own only when it finishes; a head observed in
    # that window cannot be attributed, so it must not reset the budget.
    client = FakeLithosClient()
    _story, gate = await _gate_with_story(client)
    await client.task_update(
        task_id=gate.id,
        metadata={
            REMEDIATION_KEY: RemediationBudget(
                pr_url=_PR_URL, rounds_used=2, last_seen_head_sha=_HEAD
            ).as_marker()
        },
    )
    gate = await client.task_get(task_id=gate.id)
    assert gate is not None
    rem = ExternalRemediation(
        _settings(Path("/tmp/x")), spawn=_spawner(None)[0], hold=lambda url: True
    )
    spec = parse_pr_gate(gate)
    assert spec is not None

    budget = await rem.observe_head(gate, spec, _pr("m" * 40), _ctx(client))

    assert budget.rounds_used == 2  # no reset
    assert budget.last_seen_head_sha == _HEAD  # and no observation recorded
    assert (await _marker(client, gate.id))["rounds_used"] == 2


async def test_busy_on_holds_from_the_moment_dispatch_commits(tmp_path: Path) -> None:
    # self-review: the hold was checked BEFORE the budget-reservation write
    # and the in-flight url set only AFTER it — a merge-gate probe finishing
    # during that await saw no run on the PR and started its own. The claim
    # must be visible for the whole dispatch, not just once the task exists.
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    original = client.task_update
    entered = asyncio.Event()
    release = asyncio.Event()

    async def slow_update(**kw: Any) -> Any:
        entered.set()
        await release.wait()
        return await original(**kw)

    client.task_update = slow_update  # type: ignore[method-assign]
    rem = ExternalRemediation(_settings(tmp_path), spawn=_spawner(None)[0])
    pending = asyncio.create_task(_consider(client, gate, story, rem))
    await entered.wait()
    assert rem.busy_on(_PR_URL) is True  # claimed while the reservation is out
    release.set()
    assert await pending == "dispatched"
    assert rem._task is not None
    await rem._task
    assert rem.busy_on(_PR_URL) is False


async def test_a_failed_reservation_releases_the_claim(tmp_path: Path) -> None:
    from lithos_loom.errors import LithosClientError

    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    client.raise_on["task_update"] = LithosClientError("internal", "down")
    rem = ExternalRemediation(_settings(tmp_path), spawn=_spawner(None)[0])
    assert await _consider(client, gate, story, rem) == "reservation_failed"
    assert rem.busy_on(_PR_URL) is False


# ── PR #362 re-review 2 F2: a repo mismatch must not spend a round ─────────


async def test_a_mismatched_checkout_is_refused_before_any_spend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The cheap origin check runs in the sweep, BEFORE the reservation: no
    # round spent, the pending trigger stays parked, one [Friction] on the
    # story — and fixing the mapping resumes the review debt.
    from lithos_loom.subscriptions import external_remediation as mod
    from lithos_loom.subscriptions.external_remediation import PENDING_KEY

    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    await client.task_update(
        task_id=gate.id, metadata={PENDING_KEY: {"pr_url": _PR_URL}}
    )
    gate = await client.task_get(task_id=gate.id)
    assert gate is not None
    origin = {"repo": "agent-lore/other"}

    async def fake_origin(path: Path) -> OriginRead:
        return OriginRead(origin["repo"], "ok")

    monkeypatch.setattr(mod, "origin_read", fake_origin)
    spawn, calls = _spawner(None)
    rem = ExternalRemediation(_settings(tmp_path), spawn=spawn)

    assert await _consider(client, gate, story, rem) == "repo_mismatch"
    assert calls == []
    assert rem.busy_on(_PR_URL) is False
    refreshed = await client.task_get(task_id=gate.id)
    assert refreshed is not None
    assert refreshed.metadata.get(PENDING_KEY) == {"pr_url": _PR_URL}  # debt kept
    assert (await _marker(client, gate.id)) is None  # nothing reserved
    (finding,) = _findings(client)
    assert finding.startswith("[Friction] external-remediation")
    assert "agent-lore/other" in finding and "agent-lore/lithos-lens" in finding
    assert "[projects." in finding

    # the next sweep (a fresh read of the gate): same mismatch, nothing re-posted
    gate = await client.task_get(task_id=gate.id)
    assert gate is not None
    assert await _consider(client, gate, story, rem) == "repo_mismatch"
    assert len(_findings(client)) == 1

    # the mapping is fixed: the parked debt dispatches
    origin["repo"] = "Agent-Lore/Lithos-Lens"  # case-insensitive
    assert await _consider(client, gate, story, rem) == "dispatched"
    assert rem._task is not None
    await rem._task
    assert len(calls) == 1


async def test_a_cli_repo_mismatch_refunds_the_round_and_re_parks(
    tmp_path: Path,
) -> None:
    # The CLI's authoritative check (gh, redirect-aware) may still refuse
    # where the cheap origin read passed: a structured refusal, not a
    # failed run — the round is refunded, the trigger re-parked, no
    # exhaustion escalation even on the last round.
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    spawn, _calls = _spawner(
        {
            "status": "repo_mismatch",
            "expected_repo": "agent-lore/lithos-lens",
            "actual_repo": "agent-lore/other",
            "message": "checkout origin is agent-lore/other",
        },
        rc=2,
    )
    rem = ExternalRemediation(
        _settings(tmp_path, notifier=_RecordingNotifier()), spawn=spawn
    )
    label = await _consider(client, gate, story, rem, rounds_used=1)  # last round
    assert label == "dispatched"
    assert rem._task is not None
    await rem._task

    from lithos_loom.subscriptions.external_remediation import PENDING_KEY

    marker = await _marker(client, gate.id)
    assert marker["rounds_used"] == 1  # refunded
    assert marker["needs_human_gate_id"] == ""
    assert await _human_gates(client) == []
    refreshed = await client.task_get(task_id=gate.id)
    assert refreshed is not None
    assert refreshed.metadata.get(PENDING_KEY) == {"pr_url": _PR_URL}  # re-parked
    (finding,) = _findings(client)
    assert finding.startswith("[Friction] external-remediation")
    assert "agent-lore/other" in finding and "round" in finding


async def test_a_cli_refusal_settles_until_the_mapping_or_origin_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # self-review: with the sweep's origin read matching and the CLI still
    # refusing, every sweep spawned converge, refunded and re-posted. The
    # refusal settles on what the sweep observes (path + its origin read)
    # and re-arms only when one of those changes.
    from lithos_loom.subscriptions import external_remediation as mod
    from lithos_loom.subscriptions.external_remediation import PENDING_KEY

    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    origin = {"repo": "agent-lore/lithos-lens"}

    async def fake_origin(path: Path) -> OriginRead:
        return OriginRead(origin["repo"], "ok")

    monkeypatch.setattr(mod, "origin_read", fake_origin)
    spawn, calls = _spawner(
        {
            "status": "repo_mismatch",
            "expected_repo": "agent-lore/lithos-lens",
            "actual_repo": "agent-lore/renamed",
            "message": "gh says renamed",
        },
        rc=2,
    )
    rem = ExternalRemediation(_settings(tmp_path), spawn=spawn)
    assert await _consider(client, gate, story, rem) == "dispatched"
    assert rem._task is not None
    await rem._task
    assert len(calls) == 1
    assert (await _marker(client, gate.id))["rounds_used"] == 0  # refunded
    gate = await client.task_get(task_id=gate.id)
    assert gate is not None
    assert gate.metadata.get(PENDING_KEY) == {"pr_url": _PR_URL}  # re-parked

    # later sweeps: settled — no spawn, no new finding, the debt still parked
    for _ in range(3):
        assert await _consider(client, gate, story, rem) == "repo_mismatch"
    assert len(calls) == 1 and len(_findings(client)) == 1

    # the remote url changes: one fresh attempt
    origin["repo"] = "agent-lore/renamed"
    gate = await client.task_get(task_id=gate.id)
    assert gate is not None
    assert await _consider(client, gate, story, rem) == "repo_mismatch"  # pre-check
    assert len(calls) == 1
    origin["repo"] = "agent-lore/lithos-lens"
    gate = await client.task_get(task_id=gate.id)
    assert gate is not None
    assert await _consider(client, gate, story, rem) == "dispatched"
    assert rem._task is not None
    await rem._task
    assert len(calls) == 2


# ── PR #362 re-review 3: unresolvable origins fail closed; refunds are strict ──


@pytest.mark.parametrize("reason", ["missing", "no_origin", "unparseable"])
async def test_an_unresolvable_checkout_is_refused_before_any_spend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reason: str
) -> None:
    from lithos_loom.subscriptions import external_remediation as mod
    from lithos_loom.subscriptions.external_remediation import PENDING_KEY

    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    await client.task_update(
        task_id=gate.id, metadata={PENDING_KEY: {"pr_url": _PR_URL}}
    )
    gate = await client.task_get(task_id=gate.id)
    assert gate is not None
    read = {"value": OriginRead(None, reason)}

    async def fake_read(path: Path) -> OriginRead:
        return read["value"]

    monkeypatch.setattr(mod, "origin_read", fake_read)
    spawn, calls = _spawner(None)
    rem = ExternalRemediation(_settings(tmp_path), spawn=spawn)

    assert await _consider(client, gate, story, rem) == "checkout_unresolved"
    assert calls == [] and rem.busy_on(_PR_URL) is False
    assert (await _marker(client, gate.id)) is None  # nothing reserved
    refreshed = await client.task_get(task_id=gate.id)
    assert refreshed is not None
    assert refreshed.metadata.get(PENDING_KEY) == {"pr_url": _PR_URL}
    (finding,) = _findings(client)
    assert finding.startswith("[Friction] external-remediation")
    assert reason in finding and str(tmp_path / "repo") in finding

    gate = refreshed
    assert await _consider(client, gate, story, rem) == "checkout_unresolved"
    assert len(_findings(client)) == 1

    read["value"] = OriginRead("agent-lore/lithos-lens", "ok")
    gate = await client.task_get(task_id=gate.id)
    assert gate is not None
    assert await _consider(client, gate, story, rem) == "dispatched"
    assert rem._task is not None
    await rem._task
    assert len(calls) == 1


def _refusal_payload() -> dict[str, Any]:
    return {
        "status": "repo_mismatch",
        "expected_repo": "agent-lore/lithos-lens",
        "actual_repo": "agent-lore/renamed",
        "message": "gh says renamed",
    }


async def test_a_refund_that_fails_transiently_still_lands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # PR #362 re-review 3 F2: the refund's state write is what keeps the
    # round and the review debt; a transient Lithos failure must be retried,
    # not swallowed behind a success breadcrumb.
    from lithos_loom.errors import LithosClientError
    from lithos_loom.subscriptions import remediation_outcome
    from lithos_loom.subscriptions.external_remediation import PENDING_KEY

    monkeypatch.setattr(remediation_outcome, "REFUND_RETRY_DELAYS", (0, 0, 0))
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    original = client.task_update
    failures = {"left": 2}

    async def flaky(**kw: Any) -> Any:
        if REMEDIATION_KEY in (kw.get("metadata") or {}) and failures["left"] > 0:
            failures["left"] -= 1
            raise LithosClientError("internal", "blip")
        return await original(**kw)

    rem = ExternalRemediation(
        _settings(tmp_path), spawn=_spawner(_refusal_payload(), rc=2)[0]
    )
    assert await _consider(client, gate, story, rem, rounds_used=1) == "dispatched"
    client.task_update = flaky  # type: ignore[method-assign]
    assert rem._task is not None
    await rem._task

    marker = await _marker(client, gate.id)
    assert marker["rounds_used"] == 1  # refunded after the retries
    refreshed = await client.task_get(task_id=gate.id)
    assert refreshed is not None
    assert refreshed.metadata.get(PENDING_KEY) == {"pr_url": _PR_URL}
    (finding,) = _findings(client)
    assert "refunded" in finding and "re-parked" in finding


async def test_a_refund_that_never_lands_is_reported_honestly_and_escalates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lithos_loom.errors import LithosClientError
    from lithos_loom.subscriptions import remediation_outcome
    from lithos_loom.subscriptions.external_remediation import PENDING_KEY

    monkeypatch.setattr(remediation_outcome, "REFUND_RETRY_DELAYS", (0, 0, 0))
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    original = client.task_update

    async def failing(**kw: Any) -> Any:
        if REMEDIATION_KEY in (kw.get("metadata") or {}):
            raise LithosClientError("internal", "down")
        return await original(**kw)

    notifier = _RecordingNotifier()
    rem = ExternalRemediation(
        _settings(tmp_path, notifier=notifier),
        spawn=_spawner(_refusal_payload(), rc=2)[0],
    )
    assert await _consider(client, gate, story, rem, rounds_used=1) == "dispatched"
    client.task_update = failing  # type: ignore[method-assign]
    assert rem._task is not None
    await rem._task

    marker = await _marker(client, gate.id)
    assert marker["rounds_used"] == 2  # the reservation stands
    refreshed = await client.task_get(task_id=gate.id)
    assert refreshed is not None
    assert PENDING_KEY not in refreshed.metadata  # the debt is gone, and we say so
    findings = _findings(client)
    assert not any("is refunded" in f for f in findings)
    assert any("did not land" in f and "not re-parked" in f for f in findings)
    # the last round is spent with nothing done: a human decides
    assert len(await _human_gates(client)) == 1


# ── #377: an infra failure is not a spent round ──────────────────────────


def _infra_failed_payload() -> dict:
    return {
        "status": "infra_failed",
        "succeeded": False,
        "pushed": False,
        "pushed_sha": "",
        "rounds": 1,
        "develop_status": "infra_failed",
        "total_cost_usd": 0.0,
        "message": (
            "INFRA FAILURE: round 1: coder auth_failed persisted after 2 attempts: "
            "Failed to authenticate — re-authenticate the agent CLI on the host"
        ),
        "host_action": "re-authenticate the agent CLI on the host, then restart loom",
    }


async def test_an_infra_failed_run_refunds_re_parks_and_holds_until_a_restart(
    tmp_path: Path,
) -> None:
    # The host, not the change, is broken: the round is refunded, the review
    # trigger re-parked, no exhaustion escalation even on the last round, and
    # this boot does not spawn for the PR again (an outage would otherwise
    # burn the budget one sweep at a time). A restart — the operator's fix
    # attempt — retries once.
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    spawn, calls = _spawner(_infra_failed_payload(), rc=1)
    rem = ExternalRemediation(
        _settings(tmp_path, notifier=_RecordingNotifier()), spawn=spawn
    )
    assert await _consider(client, gate, story, rem, rounds_used=1) == "dispatched"
    assert rem._task is not None
    await rem._task

    from lithos_loom.subscriptions.external_remediation import PENDING_KEY

    marker = await _marker(client, gate.id)
    assert marker["rounds_used"] == 1  # refunded
    assert marker["needs_human_gate_id"] == ""
    assert await _human_gates(client) == []
    refreshed = await client.task_get(task_id=gate.id)
    assert refreshed is not None
    assert refreshed.metadata.get(PENDING_KEY) == {"pr_url": _PR_URL}  # re-parked
    (finding,) = _findings(client)
    assert finding.startswith("[Friction] external-remediation")
    assert "infrastructure" in finding and "re-authenticate the agent CLI" in finding
    assert "refunded" in finding and "restart" in finding
    # held for the rest of this boot: the parked trigger does not fire again
    assert len(calls) == 1
    spec = parse_pr_gate(refreshed)
    assert spec is not None
    label = await rem.resume_pending(
        refreshed, spec, story, read_budget(refreshed, _PR_URL), _github(), _ctx(client)
    )
    assert label == "held_infra" and len(calls) == 1
    # a restart is the operator's fix attempt: the new boot fires the trigger
    restarted = ExternalRemediation(_settings(tmp_path), spawn=spawn)
    label = await restarted.resume_pending(
        refreshed, spec, story, read_budget(refreshed, _PR_URL), _github(), _ctx(client)
    )
    assert label == "dispatched"
    assert restarted._task is not None
    await restarted._task
    assert len(calls) == 2


async def test_the_infra_hold_is_decided_at_consider(tmp_path: Path) -> None:
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    spawn, calls = _spawner(_infra_failed_payload(), rc=1)
    rem = ExternalRemediation(_settings(tmp_path), spawn=spawn)
    assert await _consider(client, gate, story, rem) == "dispatched"
    assert rem._task is not None
    await rem._task
    assert await _consider(client, gate, story, rem) == "held_infra"
    assert len(calls) == 1


async def test_an_infra_refund_that_never_lands_still_decides_the_last_round(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Mirror of the repo-mismatch case: on this path the round IS spent, so a
    # last round escalates like any other — and a raw transport error (not a
    # LithosClientError) must land in the retry loop, never the crash handler.
    from lithos_loom.subscriptions import remediation_outcome
    from lithos_loom.subscriptions.external_remediation import PENDING_KEY

    monkeypatch.setattr(remediation_outcome, "REFUND_RETRY_DELAYS", (0, 0, 0))
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    original = client.task_update

    async def failing(**kw: Any) -> Any:
        if REMEDIATION_KEY in (kw.get("metadata") or {}):
            raise ConnectionResetError("sse stream closed")  # a raw transport error
        return await original(**kw)

    notifier = _RecordingNotifier()
    rem = ExternalRemediation(
        _settings(tmp_path, notifier=notifier),
        spawn=_spawner(_infra_failed_payload(), rc=1)[0],
    )
    assert await _consider(client, gate, story, rem, rounds_used=1) == "dispatched"
    client.task_update = failing  # type: ignore[method-assign]
    assert rem._task is not None
    await rem._task

    marker = await _marker(client, gate.id)
    assert marker["rounds_used"] == 2  # the reservation stands
    refreshed = await client.task_get(task_id=gate.id)
    assert refreshed is not None
    assert PENDING_KEY not in refreshed.metadata
    findings = _findings(client)
    assert not any("crashed before recording" in f for f in findings)
    assert any("did not land" in f and "not re-parked" in f for f in findings)
    assert len(await _human_gates(client)) == 1  # the last round decides
    assert [n.reason for n in notifier.notices] == ["remediation_exhausted"]


async def test_a_breadcrumb_that_fails_after_the_refund_landed_never_spends_the_round(
    tmp_path: Path,
) -> None:
    # PR #379 review (High): the refund write landed, then the [Friction]
    # post hit a raw transport error. That must not escape to the crash
    # handler, which would re-read the ORIGINAL reserved budget and raise a
    # false remediation_exhausted gate on the last round — the gate already
    # holds the refund and the re-parked trigger.
    from lithos_loom.subscriptions.external_remediation import PENDING_KEY

    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)

    async def failing_post(**kw: Any) -> Any:
        raise ConnectionResetError("sse stream closed")

    notifier = _RecordingNotifier()
    rem = ExternalRemediation(
        _settings(tmp_path, notifier=notifier),
        spawn=_spawner(_infra_failed_payload(), rc=1)[0],
    )
    assert await _consider(client, gate, story, rem, rounds_used=1) == "dispatched"
    client.finding_post = failing_post  # type: ignore[method-assign]
    assert rem._task is not None
    await rem._task  # never raises

    marker = await _marker(client, gate.id)
    assert marker["rounds_used"] == 1  # the refund stands
    refreshed = await client.task_get(task_id=gate.id)
    assert refreshed is not None
    assert refreshed.metadata.get(PENDING_KEY) == {"pr_url": _PR_URL}
    assert await _human_gates(client) == []
    assert notifier.notices == []


# ── #408: the budget records how its last round ended ────────────────────


def test_budget_marker_round_trips_the_last_outcome() -> None:
    budget = RemediationBudget(
        pr_url=_PR_URL, rounds_used=2, last_status="converged", last_settled=True
    )
    marker = budget.as_marker()
    assert marker["last_status"] == "converged" and marker["last_settled"] is True
    gate = SimpleNamespace(metadata={REMEDIATION_KEY: marker})
    assert read_budget(gate, _PR_URL) == budget
    # an older record without the fields, or a malformed one, reads as no
    # outcome — unsettled (fail-closed)
    del marker["last_status"], marker["last_settled"]
    assert read_budget(gate, _PR_URL) == RemediationBudget(
        pr_url=_PR_URL, rounds_used=2
    )
    marker["last_status"] = 7
    marker["last_settled"] = "true"
    parsed = read_budget(gate, _PR_URL)
    assert parsed.last_status == "" and parsed.last_settled is False


async def _dispatched_marker(
    client: FakeLithosClient, gate: Any, story: str, rem: ExternalRemediation
) -> Any:
    """Run one dispatch to completion and return the budget marker."""
    await _consider(client, gate, story, rem)
    assert rem._task is not None
    await rem._task
    return await _marker(client, gate.id)


async def test_a_converged_run_records_a_settled_outcome_on_the_budget(
    tmp_path: Path,
) -> None:
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    spawn, _calls = _spawner(
        {"status": "converged", "pushed": True, "pushed_sha": "ab" * 20, "rounds": 1}
    )
    rem = ExternalRemediation(_settings(tmp_path, budget=2), spawn=spawn)
    marker = await _dispatched_marker(client, gate, story, rem)
    assert marker["last_status"] == "converged" and marker["last_settled"] is True
    assert marker["last_loom_pushed_sha"] == "ab" * 20  # the push write is kept


async def test_a_no_change_run_records_a_settled_outcome_on_the_budget(
    tmp_path: Path,
) -> None:
    # Nothing pushed, so the verdict is the only thing that can vouch for
    # the round when the budget later reads as spent (#408).
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    spawn, _calls = _spawner(_already_clean_payload())
    rem = ExternalRemediation(_settings(tmp_path, budget=2), spawn=spawn)
    marker = await _dispatched_marker(client, gate, story, rem)
    assert marker["last_status"] == "already_clean" and marker["last_settled"] is True
    assert marker["rounds_used"] == 0 and marker["no_change_refunded"] is True


async def test_an_unconverged_run_records_an_unsettled_outcome(tmp_path: Path) -> None:
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    spawn, _calls = _spawner(_not_converged_payload())
    rem = ExternalRemediation(_settings(tmp_path, budget=2), spawn=spawn)
    marker = await _dispatched_marker(client, gate, story, rem)
    assert marker["last_status"] == "not_converged" and marker["last_settled"] is False
    assert marker["rounds_used"] == 1


async def test_a_reverted_run_records_an_unsettled_outcome(tmp_path: Path) -> None:
    # #387: `converged` by status, NOT succeeded — the verdict, not the
    # status, is what the state reads (opus review of #408).
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    spawn, _calls = _spawner(_reverted_payload())
    rem = ExternalRemediation(_settings(tmp_path, budget=2), spawn=spawn)
    marker = await _dispatched_marker(client, gate, story, rem)
    assert marker["last_status"] == "reverted" and marker["last_settled"] is False
    assert marker["last_loom_pushed_sha"] == "8d" * 20


async def test_a_run_that_dies_without_a_result_records_an_unsettled_outcome(
    tmp_path: Path,
) -> None:
    # Rounds remain, so no gate is raised — but the record must still say
    # the round did not settle anything.
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    spawn, _calls = _spawner(None, rc=2)
    rem = ExternalRemediation(_settings(tmp_path, budget=2), spawn=spawn)
    marker = await _dispatched_marker(client, gate, story, rem)
    assert marker["last_status"] == "failed" and marker["last_settled"] is False
    assert marker["rounds_used"] == 1


async def test_a_run_that_crashes_records_an_unsettled_outcome(tmp_path: Path) -> None:
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)

    async def boom(cmd: list[str]) -> tuple[int, str]:
        raise OSError("spawn failed: ENOENT")

    rem = ExternalRemediation(_settings(tmp_path, budget=2), spawn=boom)
    marker = await _dispatched_marker(client, gate, story, rem)
    assert marker["last_status"] == "failed" and marker["last_settled"] is False
    assert marker["rounds_used"] == 1


async def test_the_reservation_clears_the_previous_rounds_outcome(
    tmp_path: Path,
) -> None:
    # Fail-closed (opus review of #408, the #407 shape): a round orphaned
    # mid-run — a daemon restart, a host death — never records an outcome,
    # so the reservation itself must leave the spent round unsettled or the
    # board would read the round BEFORE ("converged") over unremediated
    # material.
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    started = asyncio.Event()
    release = asyncio.Event()

    async def blocking(cmd: list[str]) -> tuple[int, str]:
        started.set()
        await release.wait()
        return 2, "killed"

    rem = ExternalRemediation(_settings(tmp_path, budget=2), spawn=blocking)
    prior = RemediationBudget(
        pr_url=_PR_URL,
        rounds_used=1,
        last_loom_pushed_sha="c1" * 20,  # round 1's push; the material is newer
        last_seen_head_sha=_HEAD,
        last_status="converged",
        last_settled=True,
    )
    assert await _consider(client, gate, story, rem, budget=prior) == "dispatched"
    await started.wait()
    marker = await _marker(client, gate.id)
    assert marker["rounds_used"] == 2
    assert marker["last_status"] == "" and marker["last_settled"] is False
    assert marker["last_loom_pushed_sha"] == "c1" * 20  # the attribution is kept
    release.set()
    assert rem._task is not None
    await rem._task


async def test_a_crash_after_the_result_landed_keeps_the_push_attribution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # opus review of #408 (Medium): the crash handler must compose its
    # "failed" record on a re-read of the gate, not the dispatched copy —
    # or a crash after `record_result` landed loom's push would revert the
    # attribution and the next sweep would read that push as a human's
    # (the budget reset the S5b bound exists to prevent).
    from lithos_loom.subscriptions import external_remediation as er

    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    spawn, _calls = _spawner(
        {"status": "converged", "pushed": True, "pushed_sha": "ab" * 20, "rounds": 1}
    )
    real = er.record_result

    async def record_then_raise(*args: Any, **kwargs: Any) -> None:
        await real(*args, **kwargs)
        raise RuntimeError("logging blew up after the record landed")

    monkeypatch.setattr(er, "record_result", record_then_raise)
    rem = ExternalRemediation(_settings(tmp_path, budget=2), spawn=spawn)
    marker = await _dispatched_marker(client, gate, story, rem)
    assert marker["last_loom_pushed_sha"] == "ab" * 20
    assert marker["last_seen_head_sha"] == "ab" * 20
    assert marker["last_status"] == "failed" and marker["last_settled"] is False


async def test_the_infra_friction_names_the_run_worktree_that_holds_the_coders_work(
    tmp_path: Path,
) -> None:
    # #412 (lens #89): the host died under the reviewer AFTER the coder had
    # committed a fix. Converge commits on the RUN's own worktree branch
    # (opus round 1 High) — never on the PR's branch, which an infra_failed
    # run never pushes — so that is what the breadcrumb must name.
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    payload = {
        **_infra_failed_payload(),
        "head_branch": "t2-a5-detail-mini-graph-b0552ed3",
        "branch": "t2-a5-detail-mini-graph-884dc481",
        "worktree": "/tmp/lithos-loom/work/converge/86b39768/worktree/t2-a5-884dc481",
        "host_action": "check docker (`docker ps -a`, memory limits, a daemon restart)",
    }
    spawn, _calls = _spawner(payload, rc=1)
    rem = ExternalRemediation(_settings(tmp_path, budget=2), spawn=spawn)
    await _consider(client, gate, story, rem, rounds_used=1)
    assert rem._task is not None
    await rem._task
    friction = next(f for f in _findings(client) if f.startswith("[Friction]"))
    assert "/tmp/lithos-loom/work/converge/86b39768/worktree/t2-a5-884dc481" in friction
    assert "t2-a5-detail-mini-graph-884dc481" in friction
    assert "b0552ed3 holds" not in friction  # the PR branch holds nothing new
    assert "check docker" in friction


async def test_the_infra_friction_says_nothing_about_work_when_no_coder_ran(
    tmp_path: Path,
) -> None:
    # the intake-infra shape: no develop result, no worktree — no false pointer
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    payload = {**_infra_failed_payload(), "head_branch": "feature"}
    spawn, _calls = _spawner(payload, rc=1)
    rem = ExternalRemediation(_settings(tmp_path, budget=2), spawn=spawn)
    await _consider(client, gate, story, rem, rounds_used=1)
    assert rem._task is not None
    await rem._task
    friction = next(f for f in _findings(client) if f.startswith("[Friction]"))
    assert "holds any fix" not in friction


# ── #407 slice 2a: the daemon refunds what it kills ──────────────────────────


async def test_shutdown_refunds_the_round_it_kills_and_re_parks_the_trigger(
    tmp_path: Path,
) -> None:
    """#407 (lens #88 r2, 2026-09-15): the operator restarted the daemon
    18 s after a dispatch; the reservation stayed spent and the trigger
    consumed, and nothing ever re-dispatched. The daemon KNOWS the moment
    it kills a run — shutdown is where the refund belongs."""
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    started = asyncio.Event()

    async def hanging_spawn(cmd: list[str]) -> tuple[int, str]:
        started.set()
        await asyncio.sleep(3600)
        return 0, ""

    from lithos_loom.subscriptions.external_remediation import PENDING_KEY

    rem = ExternalRemediation(_settings(tmp_path, budget=2), spawn=hanging_spawn)
    assert await _consider(client, gate, story, rem, rounds_used=1) == "dispatched"
    await started.wait()
    assert (await _marker(client, gate.id))["rounds_used"] == 2  # reserved

    await rem.shutdown()

    marker = await _marker(client, gate.id)
    assert marker["rounds_used"] == 1  # refunded
    assert marker["last_status"] == "" and marker["last_settled"] is False
    refreshed = await client.task_get(task_id=gate.id)
    assert refreshed is not None
    assert refreshed.metadata.get(PENDING_KEY) == {"pr_url": _PR_URL}  # re-parked
    friction = next(f for f in _findings(client) if f.startswith("[Friction]"))
    assert "lost to a loom shutdown" in friction
    assert "round 2/2" in friction and "refunded" in friction
    assert "re-dispatches" in friction
    assert not rem.busy


async def test_shutdown_never_refunds_a_run_that_recorded_its_outcome(
    tmp_path: Path,
) -> None:
    # The cancel can land after the run's outcome write (#410's last_status)
    # — then the round is spent by a recorded outcome, and a refund on top
    # would be a second refund.
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    started = asyncio.Event()

    async def hanging_spawn(cmd: list[str]) -> tuple[int, str]:
        started.set()
        await asyncio.sleep(3600)
        return 0, ""

    rem = ExternalRemediation(_settings(tmp_path, budget=2), spawn=hanging_spawn)
    assert await _consider(client, gate, story, rem, rounds_used=1) == "dispatched"
    await started.wait()
    recorded = RemediationBudget(
        pr_url=_PR_URL, rounds_used=2, last_status="converged", last_settled=True
    )
    await client.task_update(
        task_id=gate.id, agent="a", metadata={REMEDIATION_KEY: recorded.as_marker()}
    )

    await rem.shutdown()

    marker = await _marker(client, gate.id)
    assert marker["rounds_used"] == 2 and marker["last_status"] == "converged"
    assert [f for f in _findings(client) if "lost to a loom shutdown" in f] == []


async def test_shutdown_with_nothing_in_flight_writes_nothing(tmp_path: Path) -> None:
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    rem = ExternalRemediation(_settings(tmp_path, budget=2), spawn=_spawner(None)[0])
    await rem.shutdown()
    assert await _marker(client, gate.id) is None
    assert _findings(client) == []


async def test_shutdown_never_double_refunds_a_run_whose_own_refund_landed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """opus round 1 (High, reproduced): the infra / repo-mismatch / no-result
    paths REFUND the round and used to leave `last_status` empty, so a cancel
    that landed during their finding post read as "never recorded" and the
    shutdown refunded a paid round a second time. Every refund path now
    stamps its outcome, and the shutdown guard sees it."""
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    spawn, _calls = _spawner(_infra_failed_payload(), rc=1)
    blocked = asyncio.Event()
    release = asyncio.Event()
    real_post = client.finding_post

    async def slow_post(**kw):
        blocked.set()
        await release.wait()
        return await real_post(**kw)

    monkeypatch.setattr(client, "finding_post", slow_post)
    rem = ExternalRemediation(_settings(tmp_path, budget=2), spawn=spawn)
    assert await _consider(client, gate, story, rem, rounds_used=1) == "dispatched"
    await blocked.wait()  # the infra refund landed; its friction post is in flight

    await rem.shutdown()

    marker = await _marker(client, gate.id)
    assert marker["rounds_used"] == 1  # the ONE refund, not two
    assert marker["last_status"] == "infra_failed" and marker["last_settled"] is False
    assert [f for f in _findings(client) if "lost to a loom shutdown" in f] == []


async def test_a_no_result_refund_stamps_its_outcome(tmp_path: Path) -> None:
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    spawn, _calls = _spawner(None, rc=0)  # exit 0, nothing to ingest
    rem = ExternalRemediation(_settings(tmp_path, budget=2), spawn=spawn)
    await _consider(client, gate, story, rem, rounds_used=1)
    assert rem._task is not None
    await rem._task
    marker = await _marker(client, gate.id)
    assert marker["rounds_used"] == 1 and marker["last_status"] == "no_result"


async def test_the_shutdown_friction_says_where_the_killed_run_left_its_work(
    tmp_path: Path,
) -> None:
    # opus round 1 (Medium): the killed run may have committed, even pushed;
    # the operator is told where to look and that a push it made reads as a
    # human push on the next sweep (re-arming the budget) rather than paying
    # for the fix again
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    started = asyncio.Event()

    async def hanging_spawn(cmd: list[str]) -> tuple[int, str]:
        started.set()
        await asyncio.sleep(3600)
        return 0, ""

    rem = ExternalRemediation(_settings(tmp_path, budget=2), spawn=hanging_spawn)
    assert await _consider(client, gate, story, rem, rounds_used=0) == "dispatched"
    await started.wait()
    await rem.shutdown()
    friction = next(f for f in _findings(client) if "lost to a loom shutdown" in f)
    assert str(tmp_path) in friction and "converge" in friction
    assert "re-arms the budget" in friction


# ── #407 slice 2b: the reservation names its boot; a later boot reconciles ────


def test_budget_marker_round_trips_the_in_flight_boot_id() -> None:
    budget = RemediationBudget(pr_url=_PR_URL, rounds_used=1, in_flight_boot_id="b1")
    marker = budget.as_marker()
    assert marker["in_flight_boot_id"] == "b1"
    gate = SimpleNamespace(metadata={REMEDIATION_KEY: marker})
    assert read_budget(gate, _PR_URL) == budget
    del marker["in_flight_boot_id"]  # a record from before the field: no stamp
    assert read_budget(gate, _PR_URL).in_flight_boot_id == ""


async def test_the_reservation_carries_the_dispatching_boots_id(tmp_path: Path) -> None:
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    started = asyncio.Event()
    release = asyncio.Event()

    async def blocking(cmd: list[str]) -> tuple[int, str]:
        started.set()
        await release.wait()
        path = Path(cmd[cmd.index("--json") + 1])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(_not_converged_payload()), encoding="utf-8")
        return 1, ""

    rem = ExternalRemediation(
        _settings(tmp_path, budget=2), spawn=blocking, boot_id="boot-a"
    )
    assert await _consider(client, gate, story, rem) == "dispatched"
    await started.wait()
    assert (await _marker(client, gate.id))["in_flight_boot_id"] == "boot-a"
    release.set()
    assert rem._task is not None
    await rem._task
    # every outcome write clears the stamp — the round is decided
    assert (await _marker(client, gate.id))["in_flight_boot_id"] == ""


@pytest.mark.parametrize(
    "payload, rc",
    [
        (
            {
                "status": "converged",
                "pushed": True,
                "pushed_sha": "ab" * 20,
                "rounds": 1,
            },
            0,
        ),
        (None, 0),  # no result, exit 0: the no-ingest refund
        (None, 2),  # no result, failed
        ("infra", 1),
    ],
)
async def test_every_outcome_clears_the_boot_stamp(tmp_path: Path, payload, rc) -> None:
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    spawn, _calls = _spawner(
        _infra_failed_payload() if payload == "infra" else payload, rc=rc
    )
    rem = ExternalRemediation(
        _settings(tmp_path, budget=3), spawn=spawn, boot_id="boot-a"
    )
    await _consider(client, gate, story, rem)
    assert rem._task is not None
    await rem._task
    assert (await _marker(client, gate.id))["in_flight_boot_id"] == ""


async def test_a_crash_clears_the_boot_stamp(tmp_path: Path) -> None:
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)

    async def boom(cmd: list[str]) -> tuple[int, str]:
        raise OSError("spawn failed")

    rem = ExternalRemediation(
        _settings(tmp_path, budget=3), spawn=boom, boot_id="boot-a"
    )
    await _consider(client, gate, story, rem)
    assert rem._task is not None
    await rem._task
    assert (await _marker(client, gate.id))["in_flight_boot_id"] == ""


async def _stale_stamped_gate(
    client: FakeLithosClient, *, boot: str, last_status: str = ""
):
    import os

    from lithos_loom.runner.orphans import process_identity

    story, gate = await _gate_with_story(client)
    me = process_identity(os.getpid())
    assert me is not None
    stale = RemediationBudget(
        pr_url=_PR_URL,
        rounds_used=2,
        last_seen_head_sha=_HEAD,
        last_status=last_status,
        in_flight_boot_id=boot,
        # A positive dead identity, not the pid-0 "unknown" state. The live
        # pid's different start marker proves this old incarnation is gone.
        in_flight_pid=me.pid,
        in_flight_pid_start=me.start_ticks + 1,
        in_flight_host_boot=me.host_boot,
    )
    await client.task_update(
        task_id=gate.id, agent="a", metadata={REMEDIATION_KEY: stale.as_marker()}
    )
    gate = await client.task_get(task_id=gate.id)
    assert gate is not None
    return story, gate


async def test_a_new_boot_refunds_a_reservation_stamped_by_a_dead_boot(
    tmp_path: Path,
) -> None:
    """#407 slice 2b (the ungraceful death — SIGKILL, a host crash — where the
    shutdown refund of slice 2a never runs): the reservation still names the
    boot that made it; the first sweep of a later boot sees a stamp that is
    not its own, no run in flight, and no recorded outcome — a lost run —
    and refunds it, re-parks the trigger, and says so on the story."""
    from lithos_loom.subscriptions.external_remediation import PENDING_KEY

    client = FakeLithosClient()
    story, gate = await _stale_stamped_gate(client, boot="dead-boot")
    rem = ExternalRemediation(
        _settings(tmp_path, budget=2), spawn=_spawner(None)[0], boot_id="new-boot"
    )
    spec = parse_pr_gate(gate)
    assert spec is not None

    budget = await rem.observe_head(gate, spec, _pr(), _ctx(client))

    assert budget.rounds_used == 1 and budget.in_flight_boot_id == ""
    marker = await _marker(client, gate.id)
    assert marker["rounds_used"] == 1 and marker["in_flight_boot_id"] == ""
    refreshed = await client.task_get(task_id=gate.id)
    assert refreshed is not None
    assert refreshed.metadata.get(PENDING_KEY) == {"pr_url": _PR_URL}
    friction = next(f for f in _findings(client) if f.startswith("[Friction]"))
    assert "lost to a daemon restart" in friction and "round 2/2" in friction
    assert "re-dispatches later in this sweep" in friction  # not "after the next boot"
    assert "after the next boot" not in friction


async def test_a_stamp_from_this_boot_is_a_run_not_a_loss(tmp_path: Path) -> None:
    client = FakeLithosClient()
    story, gate = await _stale_stamped_gate(client, boot="this-boot")
    rem = ExternalRemediation(
        _settings(tmp_path, budget=2), spawn=_spawner(None)[0], boot_id="this-boot"
    )
    spec = parse_pr_gate(gate)
    assert spec is not None
    budget = await rem.observe_head(gate, spec, _pr(), _ctx(client))
    assert budget.rounds_used == 2 and budget.in_flight_boot_id == "this-boot"
    assert _findings(client) == []


async def test_a_stale_stamp_beside_a_recorded_outcome_is_not_refunded(
    tmp_path: Path,
) -> None:
    # belt and braces: an outcome write always clears the stamp, but if one
    # ever did not, the recorded outcome wins — the round IS decided
    client = FakeLithosClient()
    story, gate = await _stale_stamped_gate(
        client, boot="dead-boot", last_status="converged"
    )
    rem = ExternalRemediation(
        _settings(tmp_path, budget=2), spawn=_spawner(None)[0], boot_id="new-boot"
    )
    spec = parse_pr_gate(gate)
    assert spec is not None
    budget = await rem.observe_head(gate, spec, _pr(), _ctx(client))
    assert budget.rounds_used == 2
    assert _findings(client) == []


async def test_a_refund_whose_re_read_fails_still_drives_the_sweep_from_the_refund(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lithos_loom.errors import LithosClientError

    # opus round (Medium, reproduced): the refund landed but the re-read
    # blipped, and the sweep went on from the STALE copy — a false
    # exhaustion note, no re-dispatch, needs_human for a sweep
    client = FakeLithosClient()
    story, gate = await _stale_stamped_gate(client, boot="dead-boot")
    real_get = client.task_get
    gets = {"n": 0}

    async def flaky_get(**kw):
        gets["n"] += 1
        if (
            gets["n"] == 2
        ):  # the first is refund_lost_run's own read; the second is the re-read
            raise LithosClientError("server_error", "blip")
        return await real_get(**kw)

    monkeypatch.setattr(client, "task_get", flaky_get)
    rem = ExternalRemediation(
        _settings(tmp_path, budget=2), spawn=_spawner(None)[0], boot_id="new-boot"
    )
    spec = parse_pr_gate(gate)
    assert spec is not None
    budget = await rem.observe_head(gate, spec, _pr(), _ctx(client))
    assert budget.rounds_used == 1 and budget.in_flight_boot_id == ""
    assert rem.exhaustion_note(budget) is None


async def test_a_foreign_stamp_never_breaks_the_sweep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # observe_head promises never to raise; the reconcile's own reads must
    # keep that promise under a raw transport error too
    client = FakeLithosClient()
    story, gate = await _stale_stamped_gate(client, boot="dead-boot")

    async def dead_get(**kw):
        raise RuntimeError("mcp session closed")

    monkeypatch.setattr(client, "task_get", dead_get)
    rem = ExternalRemediation(
        _settings(tmp_path, budget=2), spawn=_spawner(None)[0], boot_id="new-boot"
    )
    spec = parse_pr_gate(gate)
    assert spec is not None
    budget = await rem.observe_head(gate, spec, _pr(), _ctx(client))  # no raise
    assert budget.in_flight_boot_id == ""  # dropped in memory for this sweep


async def test_an_orphan_gate_is_refunded_without_a_finding(tmp_path: Path) -> None:
    # opus round (Medium): the refund needs no story; only the finding does
    import os

    from lithos_loom.runner.orphans import process_identity

    client = FakeLithosClient()
    me = process_identity(os.getpid())
    assert me is not None
    gate_id = await client.task_create(
        title="Awaiting merge: x",
        agent="a",
        task_type="gate",
        metadata={
            "gate_type": "pr",
            "repo": "agent-lore/lithos-loom",
            "pr_number": 62,
            "pr_url": _PR_URL,
            REMEDIATION_KEY: RemediationBudget(
                pr_url=_PR_URL,
                rounds_used=1,
                in_flight_boot_id="dead-boot",
                in_flight_pid=me.pid,
                in_flight_pid_start=me.start_ticks + 1,
                in_flight_host_boot=me.host_boot,
            ).as_marker(),
        },
    )
    gate = await client.task_get(task_id=gate_id)
    assert gate is not None
    rem = ExternalRemediation(
        _settings(tmp_path, budget=2), spawn=_spawner(None)[0], boot_id="new-boot"
    )
    spec = parse_pr_gate(gate)
    assert spec is not None
    budget = await rem.observe_head(gate, spec, _pr(), _ctx(client))
    assert budget.rounds_used == 0 and budget.in_flight_boot_id == ""
    assert (await _marker(client, gate_id))["rounds_used"] == 0
    assert _findings(client) == []


async def test_the_sweeps_story_id_is_preferred_over_the_gates_provenance(
    tmp_path: Path,
) -> None:
    # the waits_on_gate edge is the authoritative link; metadata.story_id is
    # provenance — the friction lands where the sweep's other findings land
    client = FakeLithosClient()
    story, gate = await _stale_stamped_gate(client, boot="dead-boot")
    other = await client.task_create(title="other story", agent="a")
    rem = ExternalRemediation(
        _settings(tmp_path, budget=2), spawn=_spawner(None)[0], boot_id="new-boot"
    )
    spec = parse_pr_gate(gate)
    assert spec is not None
    await rem.observe_head(gate, spec, _pr(), _ctx(client), story_id=other)
    posted = [f for f in client._findings if "lost to a daemon restart" in f["summary"]]
    assert [f["task_id"] for f in posted] == [other]


async def test_a_child_that_outlived_the_daemon_is_not_re_dispatched_beside(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # opus round (Medium): SIGKILL of the daemon does not kill its converge
    # child; the stamp names the dispatcher's pid, and a live one means the
    # run is still going — refunding and re-dispatching would put two agents
    # on one branch
    import os

    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    from lithos_loom.runner.orphans import process_identity

    me = process_identity(os.getpid())
    assert me is not None
    stale = RemediationBudget(
        pr_url=_PR_URL,
        rounds_used=1,
        last_seen_head_sha=_HEAD,
        in_flight_boot_id="dead-boot",
        in_flight_pid=me.pid,  # alive: this very process, by full identity
        in_flight_pid_start=me.start_ticks,
        in_flight_host_boot=me.host_boot,
    )
    await client.task_update(
        task_id=gate.id, agent="a", metadata={REMEDIATION_KEY: stale.as_marker()}
    )
    gate = await client.task_get(task_id=gate.id)
    assert gate is not None
    rem = ExternalRemediation(
        _settings(tmp_path, budget=2), spawn=_spawner(None)[0], boot_id="new-boot"
    )
    spec = parse_pr_gate(gate)
    assert spec is not None
    budget = await rem.observe_head(gate, spec, _pr(), _ctx(client))
    assert budget.rounds_used == 1  # untouched while the child lives
    assert (await _marker(client, gate.id))["in_flight_boot_id"] == "dead-boot"
    assert _findings(client) == []
    # Dave's review of #416 (High): the live foreign run is a REAL hold — on
    # the single-flight slot and on the per-PR holds the peers read — not a
    # memory note; a new batch this sweep must wait behind it
    assert rem.busy is True and rem.busy_on(_PR_URL) is True
    assert await _consider(client, gate, story, rem, budget=budget) == "deferred_busy"


async def test_a_foreign_run_whose_identity_died_releases_the_hold_and_refunds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    from lithos_loom.runner import orphans

    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    stale = RemediationBudget(
        pr_url=_PR_URL,
        rounds_used=1,
        last_seen_head_sha=_HEAD,
        in_flight_boot_id="dead-boot",
        in_flight_pid=os.getpid(),
        in_flight_pid_start=12345,  # a different process wore this pid
        in_flight_host_boot=orphans.host_boot_id(),
    )
    await client.task_update(
        task_id=gate.id, agent="a", metadata={REMEDIATION_KEY: stale.as_marker()}
    )
    gate = await client.task_get(task_id=gate.id)
    assert gate is not None
    rem = ExternalRemediation(
        _settings(tmp_path, budget=2), spawn=_spawner(None)[0], boot_id="new-boot"
    )
    spec = parse_pr_gate(gate)
    assert spec is not None
    budget = await rem.observe_head(gate, spec, _pr(), _ctx(client))
    assert budget.rounds_used == 0  # a reused pid is not the run: refunded
    assert rem.busy is False and rem.busy_on(_PR_URL) is False


async def test_the_reservation_carries_the_dispatchers_pid(tmp_path: Path) -> None:
    import os

    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    started = asyncio.Event()

    async def hanging(cmd: list[str]) -> tuple[int, str]:
        started.set()
        await asyncio.sleep(3600)
        return 0, ""

    rem = ExternalRemediation(_settings(tmp_path, budget=2), spawn=hanging, boot_id="b")
    assert await _consider(client, gate, story, rem) == "dispatched"
    await started.wait()
    from lithos_loom.runner.orphans import process_identity

    me = process_identity(os.getpid())
    assert me is not None
    marker = await _marker(client, gate.id)
    assert marker["in_flight_pid"] == me.pid
    assert marker["in_flight_pid_start"] == me.start_ticks
    assert marker["in_flight_host_boot"] == me.host_boot
    await rem.shutdown()
    marker = await _marker(client, gate.id)
    assert marker["in_flight_pid"] == 0 and marker["in_flight_boot_id"] == ""
    assert marker["in_flight_pid_start"] == 0 and marker["in_flight_host_boot"] == ""


async def test_a_repo_mismatch_refund_clears_the_boot_stamp(tmp_path: Path) -> None:
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    spawn, _calls = _spawner(
        {"status": "repo_mismatch", "actual_repo": "o/other"}, rc=3
    )
    rem = ExternalRemediation(_settings(tmp_path, budget=3), spawn=spawn, boot_id="b")
    await _consider(client, gate, story, rem)
    assert rem._task is not None
    await rem._task
    marker = await _marker(client, gate.id)
    assert (
        marker["in_flight_boot_id"] == "" and marker["last_status"] == "repo_mismatch"
    )


async def test_the_dispatcher_spawns_its_child_bound_to_itself(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Dave's review of #416 (High): the supervisor kills the watcher pid, not
    # its process group; the converge child must die with its parent
    from lithos_loom.runner import signals
    from lithos_loom.subscriptions.external_remediation import spawn_converge

    captured: dict = {}

    class _Proc:
        returncode = 0

        async def communicate(self):
            return b"", b""

        async def wait(self):
            return 0

    async def fake_exec(*argv, **kwargs):
        captured["env"] = kwargs.get("env")
        return _Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    await spawn_converge(["true"])
    import os

    assert captured["env"] is not None and captured["env"][signals.BOUND_ENV] == "1"
    assert captured["env"][signals.PARENT_PID_ENV] == str(os.getpid())


# ── #416 re-review: the cached hold follows the durable stamp ────────────────


async def _foreign_live_gate(client: FakeLithosClient, *, rounds_used: int = 1):
    import os

    from lithos_loom.runner.orphans import process_identity

    story, gate = await _gate_with_story(client)
    me = process_identity(os.getpid())
    assert me is not None
    stale = RemediationBudget(
        pr_url=_PR_URL,
        rounds_used=rounds_used,
        last_seen_head_sha=_HEAD,
        in_flight_boot_id="dead-boot",
        in_flight_pid=me.pid,
        in_flight_pid_start=me.start_ticks,
        in_flight_host_boot=me.host_boot,
    )
    await client.task_update(
        task_id=gate.id, agent="a", metadata={REMEDIATION_KEY: stale.as_marker()}
    )
    gate = await client.task_get(task_id=gate.id)
    assert gate is not None
    return story, gate


async def test_a_foreign_hold_is_released_when_the_survivor_records_its_outcome(
    tmp_path: Path,
) -> None:
    """Dave's re-review of #416 (High, reproduced): the old watcher's run
    finished and recorded — clearing every in-flight field — and the new
    watcher's cached hold stayed forever, blocking every remediation
    globally and both peers on the PR. A cleared or changed stamp is proof
    the run settled; the hold follows the durable record."""
    client = FakeLithosClient()
    story, gate = await _foreign_live_gate(client)
    rem = ExternalRemediation(
        _settings(tmp_path, budget=2), spawn=_spawner(None)[0], boot_id="new-boot"
    )
    spec = parse_pr_gate(gate)
    assert spec is not None
    await rem.observe_head(gate, spec, _pr(), _ctx(client))
    assert rem.busy and rem.busy_on(_PR_URL)  # the survivor is running

    settled = RemediationBudget(
        pr_url=_PR_URL,
        rounds_used=1,
        last_seen_head_sha=_HEAD,
        last_status="converged",
        last_settled=True,
    )
    await client.task_update(
        task_id=gate.id, agent="a", metadata={REMEDIATION_KEY: settled.as_marker()}
    )
    gate = await client.task_get(task_id=gate.id)
    assert gate is not None
    budget = await rem.observe_head(gate, spec, _pr(), _ctx(client))
    assert budget.rounds_used == 1 and budget.last_status == "converged"
    assert not rem.busy and not rem.busy_on(_PR_URL)
    assert await _consider(client, gate, story, rem, budget=budget) != "deferred_busy"


async def test_a_foreign_hold_is_released_when_its_identity_dies_off_sweep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # a gate that stops being swept (merged, closed) must not pin the global
    # slot through a stale cache entry: the slot re-probes what it holds
    from lithos_loom.subscriptions import external_remediation as rem_mod

    client = FakeLithosClient()
    story, gate = await _foreign_live_gate(client)
    rem = ExternalRemediation(
        _settings(tmp_path, budget=2), spawn=_spawner(None)[0], boot_id="new-boot"
    )
    spec = parse_pr_gate(gate)
    assert spec is not None
    await rem.observe_head(gate, spec, _pr(), _ctx(client))
    assert rem.busy
    monkeypatch.setattr(rem_mod, "identity_alive", lambda ident: None)
    assert rem.busy and rem.busy_on(_PR_URL)  # uncertainty fails closed
    monkeypatch.setattr(rem_mod, "identity_alive", lambda ident: False)
    assert not rem.busy and not rem.busy_on(_PR_URL)


async def test_dispatch_waits_when_its_identity_cannot_be_captured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Review of f4bfe47: without a durable identity a later boot cannot prove
    # this dispatcher died. Do not create the ambiguous reservation or start
    # a push-capable child; leave the pending trigger for the next sweep.
    from lithos_loom.subscriptions import external_remediation as rem_mod
    from lithos_loom.subscriptions.external_remediation import PENDING_KEY

    monkeypatch.setattr(rem_mod, "process_identity", lambda pid: None)
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    await client.task_update(
        task_id=gate.id, metadata={PENDING_KEY: {"pr_url": _PR_URL}}
    )
    spawn, calls = _spawner(None)
    rem = ExternalRemediation(_settings(tmp_path, budget=2), spawn=spawn, boot_id="b")
    assert await _consider(client, gate, story, rem) == "identity_unavailable"
    assert calls == [] and not rem.busy and not rem.busy_on(_PR_URL)
    gate = await client.task_get(task_id=gate.id)
    assert gate is not None
    assert gate.metadata.get(PENDING_KEY) == {"pr_url": _PR_URL}
    assert REMEDIATION_KEY not in gate.metadata


async def test_an_unverifiable_foreign_reservation_is_held_not_refunded(
    tmp_path: Path,
) -> None:
    # A legacy/malformed pid-0 stamp cannot prove its run died. The lifetime
    # bind follows the old watcher and is not evidence that watcher is gone.
    client = FakeLithosClient()
    _story, gate = await _gate_with_story(client)
    stale = RemediationBudget(
        pr_url=_PR_URL,
        rounds_used=1,
        last_seen_head_sha=_HEAD,
        in_flight_boot_id="old-boot",
    )
    await client.task_update(
        task_id=gate.id, metadata={REMEDIATION_KEY: stale.as_marker()}
    )
    gate = await client.task_get(task_id=gate.id)
    assert gate is not None
    spec = parse_pr_gate(gate)
    assert spec is not None
    rem = ExternalRemediation(
        _settings(tmp_path, budget=2), spawn=_spawner(None)[0], boot_id="new-boot"
    )
    budget = await rem.observe_head(gate, spec, _pr(), _ctx(client))
    assert budget.rounds_used == 1
    assert rem.busy and rem.busy_on(_PR_URL)
    assert (await _marker(client, gate.id))["in_flight_boot_id"] == "old-boot"


# ── drain (#407 slice 3) ─────────────────────────────────────────────────


def _blocking_spawner(
    payload: dict[str, Any],
) -> tuple[Any, asyncio.Event, asyncio.Event]:
    """A spawn that parks until released — the run "in flight" a drain waits for."""
    started, release = asyncio.Event(), asyncio.Event()

    async def spawn(cmd: list[str]) -> tuple[int, str]:
        started.set()
        await release.wait()
        path = Path(cmd[cmd.index("--json") + 1])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")
        return 0, "converge output"

    return spawn, started, release


async def test_a_draining_dispatcher_refuses_before_any_reservation(
    tmp_path: Path,
) -> None:
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    spawn, calls = _spawner({"status": "converged", "pushed": False})
    rem = ExternalRemediation(_settings(tmp_path), spawn=spawn)
    rem.begin_drain()

    assert await _consider(client, gate, story, rem) == "draining"
    assert calls == []
    assert await _marker(client, gate.id) is None  # nothing reserved


async def test_a_parked_trigger_is_not_resumed_into_a_drain(tmp_path: Path) -> None:
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    await client.task_update(
        task_id=gate.id,
        metadata={PENDING_KEY: {"pr_url": _PR_URL, "batch": "b1"}},
    )
    gate = await client.task_get(task_id=gate.id)
    assert gate is not None
    spawn, calls = _spawner({"status": "converged", "pushed": False})
    rem = ExternalRemediation(_settings(tmp_path), spawn=spawn)
    rem.begin_drain()

    spec = parse_pr_gate(gate)
    assert spec is not None
    label = await rem.resume_pending(
        gate, spec, story, RemediationBudget(pr_url=_PR_URL), _github(), _ctx(client)
    )
    assert label == "draining"
    assert calls == []
    gate = await client.task_get(task_id=gate.id)
    assert gate is not None
    assert gate.metadata.get(PENDING_KEY) is not None  # still parked


async def test_drained_waits_for_the_run_in_flight_and_its_outcome_lands(
    tmp_path: Path,
) -> None:
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    spawn, started, release = _blocking_spawner(
        {"status": "converged", "pushed": True, "pushed_sha": "ab" * 20, "rounds": 1}
    )
    rem = ExternalRemediation(_settings(tmp_path), spawn=spawn)
    assert await _consider(client, gate, story, rem) == "dispatched"
    await asyncio.wait_for(started.wait(), 1.0)

    rem.begin_drain()
    drained = asyncio.create_task(rem.drained())
    await asyncio.sleep(0.05)
    assert not drained.done()
    assert rem.busy  # still the single-flight holder while it finishes
    release.set()
    await asyncio.wait_for(drained, 2.0)
    assert not rem.busy
    marker = await _marker(client, gate.id)
    assert marker["last_status"] == "converged"  # the outcome was recorded
    assert marker["last_loom_pushed_sha"] == "ab" * 20


async def test_drained_returns_at_once_when_idle(tmp_path: Path) -> None:
    rem = ExternalRemediation(_settings(tmp_path), spawn=_spawner(None)[0])
    rem.begin_drain()
    await asyncio.wait_for(rem.drained(), 1.0)


async def test_drained_covers_a_dispatch_between_its_check_and_its_spawn(
    tmp_path: Path,
) -> None:
    """The reservation write is an await a drain can begin under: a
    `drained()` that ran then must not return before the run it did not
    yet see has started AND ended (else the child exits with a stamped
    round that the next boot must refund — sloppy, though self-healing)."""
    write_started, write_release = asyncio.Event(), asyncio.Event()

    class _SlowWrite(FakeLithosClient):
        async def task_update(self, *a: Any, **kw: Any) -> Any:
            if REMEDIATION_KEY in (kw.get("metadata") or {}):
                write_started.set()
                await write_release.wait()
            return await super().task_update(*a, **kw)

    client = _SlowWrite()
    story, gate = await _gate_with_story(client)
    spawn, started, release = _blocking_spawner(
        {"status": "converged", "pushed": False}
    )
    rem = ExternalRemediation(_settings(tmp_path), spawn=spawn)
    considering = asyncio.create_task(_consider(client, gate, story, rem))
    await asyncio.wait_for(write_started.wait(), 1.0)  # inside the reservation

    rem.begin_drain()
    drained = asyncio.create_task(rem.drained())
    await asyncio.sleep(0.05)
    assert not drained.done()  # a dispatch is committing
    write_release.set()
    assert await asyncio.wait_for(considering, 1.0) == "dispatched"
    await asyncio.wait_for(started.wait(), 1.0)
    await asyncio.sleep(0.05)
    assert not drained.done()  # and now its run is in flight
    release.set()
    await asyncio.wait_for(drained, 2.0)


async def test_a_no_result_run_reports_the_message_line_not_a_traceback_frame(
    tmp_path: Path,
) -> None:
    # #431: the remediation dispatcher renders the same no-result tail the
    # conflict resolver does — a rich frame is not a reason an operator can act
    # on, so the finding carries the child's last message line.
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    traceback = (
        "╭──────────── Traceback (most recent call last) ────────────╮\n"
        "│ /workspace/src/lithos_loom/cli/converge.py:308 in x       │\n"
        "│ ❱ 103 │   raise RuntimeError(...)                         │\n"
        "╰───────────────────────────────────────────────────────────╯\n"
        "RuntimeError: git fetch origin pull/62/head failed: fatal: Could not "
        "read from remote repository.\n"
    )
    spawn, _calls = _spawner(None, rc=1, output=traceback)
    rem = ExternalRemediation(_settings(tmp_path), spawn=spawn)

    assert await _consider(client, gate, story, rem) == "dispatched"
    assert rem._task is not None
    await rem._task

    friction = next(f for f in _findings(client) if "[Friction]" in f)
    assert "Could not read from remote repository" in friction
    assert "│" not in friction and "❱" not in friction
