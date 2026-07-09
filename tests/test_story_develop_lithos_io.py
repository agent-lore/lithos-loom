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
from lithos_loom.plugins.story_develop.gate_findings import GateFinding
from lithos_loom.plugins.story_develop.handoff import Finding
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


def test_post_results_with_delivery_corrects_cost_and_reports_round(
    fake_client: FakeLithosClient,
) -> None:
    from lithos_loom.plugins.story_develop.pr_delivery import DeliveryOutcome

    delivery = DeliveryOutcome(
        pr_url="https://github.com/o/r/pull/12",
        pr_number=12,
        copilot_requested=True,
        copilot_reviewed=True,
        comments_count=2,
        fix_committed=True,
        fix_pushed=True,
        fix_sha="abc123",
        replies_posted=2,
        extra_cost_usd=0.5,
    )
    ok = lithos_io.post_results("http://x", "task-1", _result(), delivery=delivery)
    assert ok is True
    body = fake_client.findings[0]["summary"]
    assert "pull request: https://github.com/o/r/pull/12" in body
    assert "copilot round: 2 comment(s); fix pushed (abc123); 2 repl(ies)" in body
    assert "total cost incl. copilot round: $1.2500" in body  # 0.75 + 0.5
    (update,) = fake_client.calls_to("task_update")
    meta = update["metadata"]
    assert meta["develop_cost_usd"] == 1.25  # NOT the stale 0.75
    assert meta["develop_pr_url"] == "https://github.com/o/r/pull/12"


def test_post_results_with_held_back_delivery(fake_client: FakeLithosClient) -> None:
    from lithos_loom.plugins.story_develop.pr_delivery import DeliveryOutcome

    delivery = DeliveryOutcome(
        pr_url="https://github.com/o/r/pull/12",
        pr_number=12,
        copilot_requested=True,
        copilot_reviewed=True,
        comments_count=1,
        fix_committed=True,
        fix_pushed=False,
        fix_gate_verdict="RED",
        replies_posted=1,
        extra_cost_usd=0.2,
    )
    lithos_io.post_results("http://x", "task-1", _result(), delivery=delivery)
    body = fake_client.findings[0]["summary"]
    assert "HELD BACK (gate RED)" in body


def test_post_results_disputed_adds_breadcrumb(fake_client: FakeLithosClient) -> None:
    lithos_io.post_results("http://x", "task-1", _result(status="disputed"))
    assert len(fake_client.findings) == 2
    assert fake_client.findings[1]["summary"].startswith("[ReviewDispute]")
    assert "human" in fake_client.findings[1]["summary"]


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
