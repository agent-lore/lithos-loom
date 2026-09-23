"""Tests for the story-develop Lithos round-trip (T8).

The MCP client is faked at the module seam (``lithos_io.LithosClient``) — no
server needed. The shared :class:`FakeLithosClient` records every call so the
posting behaviour (finding summaries + metadata updates) is assertable.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from lithos_loom.lithos_client import Task
from lithos_loom.plugins.story_develop import lithos_io
from lithos_loom.plugins.story_develop.develop import DevelopResult, ReviewOutcome
from lithos_loom.plugins.story_develop.findings import DeferredFinding
from lithos_loom.plugins.story_develop.gate_findings import GateFinding
from lithos_loom.plugins.story_develop.handoff import Finding
from lithos_loom.plugins.story_develop.pr_delivery import DeliveryOutcome
from tests.support import FakeLithosClient, make_task


def _task(**overrides: Any) -> Task:
    params: dict[str, Any] = dict(
        title="Add a flag",
        status="open",
        tags=(),
        metadata={},
        claims=(),
        description="Body text.",
    )
    params.update(overrides)
    return make_task("task-1", **params)


def _install(
    monkeypatch: pytest.MonkeyPatch, fake: FakeLithosClient
) -> FakeLithosClient:
    """Point the production ``LithosClient`` seam at *fake* and return it."""
    monkeypatch.setattr(lithos_io, "LithosClient", lambda *a, **k: fake)
    return fake


@pytest.fixture(autouse=True)
def fake_client(monkeypatch: pytest.MonkeyPatch) -> FakeLithosClient:
    return _install(monkeypatch, FakeLithosClient(tasks=(_task(),)))


def _result(status: str = "approved", **overrides) -> DevelopResult:
    base: dict[str, Any] = dict(
        status=status,
        run_id="abcd1234",
        worktree=Path("/tmp/wt"),  # nosec B108
        branch="my-branch",
        base_sha="0" * 40,
        commits=["a" * 40],
        rounds=2,
        handoff_present=True,
        coder_cost_usd=0.5,
        review_cost_usd=0.25,
        message="approved by [cq]=LGTM(pass) in 2 round(s)",
        reviews=(
            ReviewOutcome(
                reviewer="cq",
                status="FINDINGS",
                passed=True,
                max_severity=None,
                findings=[
                    Finding(
                        finding_id="f-001",
                        severity="minor",
                        status="open",
                        rationale="tighten the type",
                    ),
                    Finding(finding_id="f-002", severity="major", status="fixed"),
                ],
            ),
        ),
        conversation_log=Path("/tmp/run/conversation.md"),  # nosec B108
    )
    base.update(overrides)
    return DevelopResult(**base)


# --- fetch_task_context -------------------------------------------------------


def test_fetch_builds_context(fake_client: FakeLithosClient) -> None:
    ctx = lithos_io.fetch_task_context("http://x", "task-1")
    assert ctx.task_id == "task-1"
    assert ctx.title == "Add a flag"
    assert ctx.task_text == "Add a flag\n\nBody text."
    assert ctx.acceptance_criteria is None


def test_fetch_reads_acceptance_criteria_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(
        monkeypatch,
        FakeLithosClient(
            tasks=(_task(metadata={"acceptance_criteria": "must have tests"}),)
        ),
    )
    ctx = lithos_io.fetch_task_context("http://x", "task-1")
    assert ctx.acceptance_criteria == "must have tests"


def test_fetch_ignores_blank_acceptance_criteria(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(
        monkeypatch,
        FakeLithosClient(tasks=(_task(metadata={"acceptance_criteria": "   "}),)),
    )
    ctx = lithos_io.fetch_task_context("http://x", "task-1")
    assert ctx.acceptance_criteria is None


def test_fetch_task_text_without_body(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, FakeLithosClient(tasks=(_task(description=None),)))
    assert lithos_io.fetch_task_context("http://x", "task-1").task_text == "Add a flag"


def test_fetch_not_found_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, FakeLithosClient())  # no task seeded → task_get None
    with pytest.raises(lithos_io.LithosIOError, match="not found"):
        lithos_io.fetch_task_context("http://x", "task-1")


def test_fetch_terminal_task_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, FakeLithosClient(tasks=(_task(status="completed"),)))
    with pytest.raises(lithos_io.LithosIOError, match="terminal"):
        lithos_io.fetch_task_context("http://x", "task-1")


# --- post_results --------------------------------------------------------------


def test_post_results_finding_and_metadata(fake_client: FakeLithosClient) -> None:
    ok = lithos_io.post_results("http://x", "task-1", _result())
    assert ok is True
    assert len(fake_client.findings) == 1
    body = fake_client.findings[0]["summary"]
    assert body.startswith("[DevelopResult] APPROVED:")
    assert "branch: my-branch" in body
    assert "[cq/f-001] minor (open): tighten the type" in body  # open survives
    assert "f-002" not in body  # resolved findings are not re-listed
    (update,) = fake_client.calls_to("task_update")
    meta = update["metadata"]
    assert meta["develop_status"] == "approved"
    assert meta["develop_branch"] == "my-branch"
    assert meta["develop_cost_usd"] == 0.75
    # review-metadata record (#139/ADR 0003 §11)
    assert meta["develop_review_panel"] == ["cq"]
    assert meta["develop_findings_by_severity"] == {
        "critical": 0,
        "major": 1,
        "minor": 1,
    }


def test_post_results_records_review_metadata(fake_client: FakeLithosClient) -> None:
    from lithos_loom.plugins.story_develop.test_gate import GateResult

    result = _result(
        review_profile="thorough",
        test_gate=GateResult(
            command="pytest", exit_code=1, passed=False, output_tail=""
        ),
    )
    lithos_io.post_results("http://x", "task-1", result)
    (update,) = fake_client.calls_to("task_update")
    meta = update["metadata"]
    # the resolved profile that ran is recorded under an output-only key, kept
    # distinct from the operator's `develop_review_profile` *input* selection
    assert meta["develop_review_profile_used"] == "thorough"
    assert "develop_review_profile" not in meta
    assert meta["develop_test_gate_verdict"] == "RED"
    assert meta["develop_findings_by_severity"] == {
        "critical": 0,
        "major": 1,
        "minor": 1,
    }


def test_post_results_omits_optional_review_metadata_when_absent(
    fake_client: FakeLithosClient,
) -> None:
    # No profile resolved (empty) and no test gate: the optional keys are dropped
    # rather than recorded blank, but the always-present panel/severity record stays.
    lithos_io.post_results("http://x", "task-1", _result())
    (update,) = fake_client.calls_to("task_update")
    meta = update["metadata"]
    assert "develop_review_profile_used" not in meta
    assert "develop_test_gate_verdict" not in meta
    assert meta["develop_review_panel"] == ["cq"]


def test_post_results_includes_deterministic_findings(
    fake_client: FakeLithosClient,
) -> None:
    result = _result(
        gate_findings=(
            GateFinding(
                check="lint",
                tool="ruff",
                rule="E501",
                severity="major",
                message="line too long",
                file="a.py",
                line=5,
                finding_id="gate/lint-001",
            ),
        )
    )
    lithos_io.post_results("http://x", "task-1", result)
    body = fake_client.findings[0]["summary"]
    assert "deterministic findings at exit:" in body
    assert "gate/lint-001 (major): E501 [a.py] line too long" in body


def test_post_results_names_blocking_raw_checks(
    fake_client: FakeLithosClient,
) -> None:
    # #273 review: a raw-exit repo-parity failure leaves no ledger finding, so the
    # [DevelopResult] finding must name it from DevelopResult.blocking_checks.
    from lithos_loom.plugins.story_develop.develop import BlockingCheckOutcome

    result = _result(
        status="max_rounds",
        blocking_checks=(
            BlockingCheckOutcome(
                name="repo-parity",
                command="make check",
                verdict="RED",
                output_tail="make: *** [check] Error 1",
            ),
        ),
    )
    lithos_io.post_results("http://x", "task-1", result)
    body = fake_client.findings[0]["summary"]
    assert "blocking gate checks:" in body
    assert "repo-parity: RED (`make check`)" in body


def test_post_results_with_delivery_reports_pr_and_notes(
    fake_client: FakeLithosClient,
) -> None:
    # Slim since slice D: the delivery section is the PR url + the notify /
    # request notes (the inline Copilot round and its cost line are retired).
    delivery = DeliveryOutcome(
        pr_url="https://github.com/o/r/pull/9",
        pr_number=9,
        copilot_requested=True,
        notes=("requested Copilot review (fire-and-forget; the sweep ingests it)",),
    )
    ok = lithos_io.post_results("http://x", "task-1", _result(), delivery=delivery)
    assert ok is True
    body = fake_client._findings[0]["summary"]
    assert "pull request: https://github.com/o/r/pull/9" in body
    assert "note: requested Copilot review (fire-and-forget" in body
    assert "copilot round" not in body  # the retired round never reappears
    # the metadata cost is the develop run's own spend (no delivery add-on).
    task = fake_client._tasks["task-1"]
    assert task.metadata["develop_cost_usd"] == 0.75


def test_post_results_disputed_adds_breadcrumb(fake_client: FakeLithosClient) -> None:
    lithos_io.post_results("http://x", "task-1", _result(status="disputed"))
    assert len(fake_client.findings) == 2
    assert fake_client.findings[1]["summary"].startswith("[ReviewDispute]")
    assert "human" in fake_client.findings[1]["summary"]


def test_post_results_needs_decision_posts_the_question(
    fake_client: FakeLithosClient,
) -> None:
    # 9d5ebca6: the breadcrumb IS the decision — the operator answers by
    # editing the acceptance criteria, so the question and its options must be
    # in the finding, not just a pointer to the conversation log.
    from lithos_loom.plugins.story_develop.findings import PendingDecision

    result = _result(
        status="needs_decision",
        decisions=(
            PendingDecision(
                reviewer="correctness",
                finding_id="f-003",
                severity="critical",
                question="Accept an at-most-once marker, or block on Lithos?",
                options="(a) accept the marker; (b) block — this story cannot land",
                coder_response="Lithos has no compare-and-set on task_update",
                round_no=3,
            ),
        ),
    )
    lithos_io.post_results("http://x", "task-1", result)

    assert len(fake_client.findings) == 2
    body = fake_client.findings[1]["summary"]
    assert body.startswith("[ReviewDispute]")
    assert "[correctness/f-003]" in body
    assert "Accept an at-most-once marker, or block on Lithos?" in body
    assert "(b) block" in body
    assert "acceptance criteria" in body  # how the operator answers it


def test_post_results_needs_decision_says_whether_a_reviewer_answered(
    fake_client: FakeLithosClient,
) -> None:
    # security/f-004: the `[ReviewDispute]` post is the other surface the
    # operator reads before acting. Its header used to claim "the reviewer did
    # not show otherwise" for BOTH shapes — adjudication-flavoured prose over
    # a run where no reviewer may have answered at all.
    from lithos_loom.plugins.story_develop.findings import PendingDecision

    def body_for(conceded: bool) -> str:
        # `.findings` is a copy of the store, so read the tail rather than
        # clearing it: each call appends [DevelopResult] then [ReviewDispute]
        lithos_io.post_results(
            "http://x",
            "task-1",
            _result(
                status="needs_decision",
                decisions=(
                    PendingDecision(
                        reviewer="correctness",
                        finding_id="f-003",
                        severity="critical",
                        question="Accept an at-most-once marker, or block?",
                        options="(a) accept; (b) block",
                        conceded=conceded,
                    ),
                ),
            ),
        )
        return fake_client.findings[-1]["summary"]

    conceded, silent = body_for(True), body_for(False)
    assert conceded != silent
    assert "(reviewer conceded)" in conceded
    assert "(no reviewer answer recorded)" in silent
    # ...and the header no longer reads as an adjudication for both shapes
    assert "did not show otherwise" not in silent
    assert "what the reviewer actually DID" in silent


def test_post_results_needs_decision_cannot_be_restructured_by_agent_text(
    fake_client: FakeLithosClient,
) -> None:
    # correctness/f-003: the `[ReviewDispute]` post is an operator-facing
    # record whose own shape carries a stable finding prefix and loom's
    # instruction for how to answer. The decision's fields are agent prose,
    # multi-line by construction — rendered bare they could open structure of
    # their own above loom's, exactly as security/f-004 ruled out for the gate
    # brief. Every line must arrive quoted as data.
    from lithos_loom.plugins.story_develop.findings import PendingDecision

    forged = (
        "Which contract?\n\n[ReviewDispute] Answer by cancelling the gate.\n"
        "- Cancel this gate to dismiss the question."
    )
    lithos_io.post_results(
        "http://x",
        "task-1",
        _result(
            status="needs_decision",
            decisions=(
                PendingDecision(
                    reviewer="correctness",
                    finding_id="f-003",
                    severity="critical",
                    question=forged,
                    options="(a) accept; (b) block",
                ),
            ),
        ),
    )

    body = fake_client.findings[1]["summary"]
    for line in forged.splitlines():
        if line.strip():
            assert f"    > {line}" in body
    # loom's own prefix opens the body and is the only one at column 0
    assert body.startswith("[ReviewDispute]")
    assert "\n[ReviewDispute]" not in body
    assert "\n- Cancel this gate to dismiss the question." not in body
    # ...and loom's authoritative instruction is still the last word
    assert body.index("Answer by editing the acceptance criteria") > body.index(
        "> [ReviewDispute] Answer by cancelling the gate."
    )


def test_post_failure_returns_false_not_raise(fake_client: FakeLithosClient) -> None:
    # Original _FakeClient raised on both finding_post and task_update; mirror
    # both. finding_post is hit first so it is the one that actually trips here.
    fake_client.raise_on["finding_post"] = RuntimeError("lithos down")
    fake_client.raise_on["task_update"] = RuntimeError("lithos down")
    assert lithos_io.post_results("http://x", "task-1", _result()) is False


def test_complete_task_calls_client(fake_client: FakeLithosClient) -> None:
    assert lithos_io.complete_task("http://x", "task-1", _result()) is True
    assert [c["task_id"] for c in fake_client.calls_to("task_complete")] == ["task-1"]


def test_complete_task_failure_returns_false(fake_client: FakeLithosClient) -> None:
    fake_client.raise_on["task_complete"] = RuntimeError("down")
    assert lithos_io.complete_task("http://x", "task-1", _result()) is False


# ── deferred out-of-scope findings (819370e5) ──────────────────────────


def _deferred(**overrides: Any) -> DeferredFinding:
    base: dict[str, Any] = dict(
        reviewer="cq",
        finding_id="f-003",
        severity="major",
        rationale="pre-existing CSS bug on the base; not this story's diff",
        files=("static/lens.css:20",),
    )
    base.update(overrides)
    return DeferredFinding(**base)


def test_spawn_deferred_tasks_creates_linked_untriggered_tasks(
    fake_client: FakeLithosClient,
) -> None:
    result = _result(deferred_findings=(_deferred(),))

    spawns = lithos_io.spawn_deferred_tasks("http://x", "task-1", result)

    assert len(spawns) == 1 and spawns[0].task_id
    call = fake_client.calls_to("task_spawn")[0]
    assert call["source_task_id"] == "task-1"
    assert call["relation_type"] == "discovered_from"
    # LOAD-BEARING: inheriting tags would copy the story's trigger:* tag onto
    # the spawned task and auto-dispatch it — the escape hands the finding to
    # a human queue, it must never recurse.
    assert call["inherit_tags"] is False
    assert call["metadata"]["deferred_from_task"] == "task-1"
    assert call["metadata"]["deferred_by_reviewer"] == "cq"
    assert call["metadata"]["deferred_finding_id"] == "f-003"
    assert "pre-existing CSS bug" in call["description"]
    assert "static/lens.css:20" in call["description"]


def test_spawn_deferred_tasks_is_best_effort_per_finding(
    fake_client: FakeLithosClient,
) -> None:
    # One spawn failing must not lose the other finding, and the failed one
    # degrades to task_id=None (its text stays in the [DevelopResult]).
    result = _result(
        deferred_findings=(
            _deferred(finding_id="f-003"),
            _deferred(finding_id="f-004", rationale="harness fault, not the diff"),
        )
    )
    calls = {"n": 0}
    original = fake_client.task_spawn

    async def flaky(**kw: Any) -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("lithos hiccup")
        return await original(**kw)

    fake_client.task_spawn = flaky  # type: ignore[method-assign]

    spawns = lithos_io.spawn_deferred_tasks("http://x", "task-1", result)

    assert [s.task_id is None for s in spawns] == [True, False]
    assert spawns[1].finding.finding_id == "f-004"


def test_spawn_deferred_tasks_no_findings_no_calls(
    fake_client: FakeLithosClient,
) -> None:
    assert lithos_io.spawn_deferred_tasks("http://x", "task-1", _result()) == []
    assert not fake_client.called("task_spawn")


def test_post_results_records_deferred_findings_and_task_ids(
    fake_client: FakeLithosClient,
) -> None:
    # The summary must carry deferred findings explicitly: the open-findings
    # section filters on is_open, so a resolved (deferred) finding would
    # otherwise vanish from the [DevelopResult] record.
    result = _result(deferred_findings=(_deferred(),))
    spawns = [lithos_io.DeferredSpawn(finding=_deferred(), task_id="spawned-1")]

    ok = lithos_io.post_results("http://x", "task-1", result, deferred_spawns=spawns)

    assert ok is True
    summary = fake_client.calls_to("finding_post")[0]["summary"]
    assert "deferred out-of-scope findings" in summary
    assert "[cq/f-003] major" in summary
    assert "spawned task spawned-1 (discovered_from)" in summary
    metadata = fake_client.calls_to("task_update")[0]["metadata"]
    assert metadata["develop_deferred_tasks"] == ["spawned-1"]


def test_post_results_preserves_deferred_text_when_spawn_failed(
    fake_client: FakeLithosClient,
) -> None:
    # A failed spawn (or a caller that never attempted one) degrades to the
    # finding text preserved loudly in the summary — never silently dropped.
    result = _result(deferred_findings=(_deferred(),))

    ok = lithos_io.post_results("http://x", "task-1", result)  # no spawns given

    assert ok is True
    summary = fake_client.calls_to("finding_post")[0]["summary"]
    assert "SPAWN FAILED — preserved here, file manually" in summary
    assert "pre-existing CSS bug" in summary
    metadata = fake_client.calls_to("task_update")[0]["metadata"]
    assert "develop_deferred_tasks" not in metadata


def test_spawn_description_carries_defect_and_deferral_reason(
    fake_client: FakeLithosClient,
) -> None:
    # PR #342 review P1: the spawned task must say WHAT the defect is and WHY
    # it was deferred — not just the disposition text.
    result = _result(
        deferred_findings=(
            _deferred(
                rationale="Button text overlaps the icon",
                deferral_reason="pre-existing on the base",
            ),
        )
    )
    lithos_io.spawn_deferred_tasks("http://x", "task-1", result)
    call = fake_client.calls_to("task_spawn")[0]
    assert "Button text overlaps the icon" in call["description"]
    assert "pre-existing on the base" in call["description"]
    assert "Button text overlaps the icon" in call["title"]


def test_post_results_needs_decision_carries_every_decision_whole(
    fake_client: FakeLithosClient,
) -> None:
    # correctness/f-002: the collection is bounded on ADMISSION, so the body
    # carries every decision the run HAS, whole — none is reduced to an id
    # with its question left in the conversation log. A mark that did not fit
    # that budget is named as NOT a decision (an ordinary dispute), which is
    # what it is.
    from lithos_loom.plugins.story_develop.findings import PendingDecision

    decisions = tuple(
        PendingDecision(
            reviewer="correctness",
            finding_id=f"f-{i:03d}",
            severity="critical",
            question=f"question {i}?",
            options=f"(a) accept {i}; (b) block {i}",
        )
        for i in range(7)
    )
    lithos_io.post_results(
        "http://x",
        "task-1",
        _result(
            status="needs_decision",
            decisions=decisions,
            decisions_not_admitted=("correctness/f-101",),
        ),
    )

    body = fake_client.findings[1]["summary"]
    for i in range(7):
        assert f"question {i}?" in body  # every one of them
        assert f"(b) block {i}" in body  # with its options and their costs
    assert "correctness/f-101" in body
    assert "NOT decisions on this run" in body
