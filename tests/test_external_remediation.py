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
    REMEDIATION_KEY,
    ExternalRemediation,
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


def _spawner(payload: dict | None, rc: int = 0) -> tuple[Any, list[list[str]]]:
    """A fake spawn: records the argv, optionally writes the --json payload."""
    calls: list[list[str]] = []

    async def spawn(cmd: list[str]) -> tuple[int, str]:
        calls.append(cmd)
        if payload is not None:
            path = Path(cmd[cmd.index("--json") + 1])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload), encoding="utf-8")
        return rc, "converge output"

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


async def test_exhaustion_escalates_once_per_budget(tmp_path: Path) -> None:
    # A gate already raised for this exhaustion (marker carries its id) is not
    # raised again by a later exhausted run — one decision, one gate.
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    await client.task_update(
        task_id=gate.id,
        agent="a",
        metadata={
            REMEDIATION_KEY: {
                "pr_url": _PR_URL,
                "rounds_used": 1,
                "last_loom_pushed_sha": "",
                "last_seen_head_sha": _HEAD,
                "needs_human_gate_id": "gate-already-raised",
            }
        },
    )
    gate = await client.task_get(task_id=gate.id)
    assert gate is not None
    spawn, _calls = _spawner(_not_converged_payload())
    rem = ExternalRemediation(_settings(tmp_path, budget=2), spawn=spawn)
    label = await _consider(client, gate, story, rem, budget=read_budget(gate, _PR_URL))
    assert label == "dispatched"
    assert rem._task is not None
    await rem._task
    assert await _human_gates(client) == []
    marker = await _marker(client, gate.id)
    assert marker["rounds_used"] == 2
    assert marker["needs_human_gate_id"] == "gate-already-raised"


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
