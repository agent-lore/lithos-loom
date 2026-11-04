"""``DevelopResult`` is the one outcome value: its message and its ``state.json``
record are rendered from it, so the three surfaces agree on every exit.

Lithos task ``f0f4b46e``. The develop loop's epilogue used to assemble the
operator message, the durable run state and the returned result from the same
locals three times over; a fact added to one could miss the others. These
tests pin the agreement per terminal status without running the loop.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from lithos_loom.plugins.story_develop.develop import (
    BlockingCheckOutcome,
    DevelopResult,
    ReviewerSession,
)
from lithos_loom.plugins.story_develop.handoff import Finding
from lithos_loom.plugins.story_develop.panel import ReviewOutcome, findings_by_severity
from lithos_loom.plugins.story_develop.test_gate import GateResult

REASON_BEARING = (
    "failed",
    "interrupted",
    "stalled",
    "disputed",
    "cost_exceeded",
    "infra_failed",
    "needs_decision",
)
SELF_DESCRIBING = ("approved", "max_rounds")


def _reviews() -> tuple[ReviewOutcome, ...]:
    return (
        ReviewOutcome(
            reviewer="code-quality",
            status="FINDINGS",
            passed=False,
            max_severity="major",
            findings=[
                Finding(
                    finding_id="f-001",
                    severity="major",
                    status="open",
                    rationale="Leaks the handle\nmore detail",
                ),
                Finding(finding_id="f-002", severity="minor", status="fixed"),
            ],
        ),
        ReviewOutcome(
            reviewer="security", status="LGTM", passed=True, max_severity=None
        ),
    )


def _result(status: str, tmp_path: Path, **overrides: object) -> DevelopResult:
    fields: dict[str, object] = dict(
        status=status,
        run_id="run-1",
        worktree=tmp_path / "wt",
        branch="loom/run-1",
        base_sha="a" * 40,
        commits=["c1", "c2"],
        rounds=3,
        handoff_present=True,
        coder_cost_usd=0.123456,
        review_cost_usd=0.2,
        message="",
        reviews=_reviews(),
        coder_session="coder-sess",
        test_gate=GateResult(
            command="make check", exit_code=1, passed=False, output_tail=""
        ),
        blocking_checks=(
            BlockingCheckOutcome(
                name="repo-parity", command="make parity", verdict="RED"
            ),
        ),
        review_profile="standard",
        resume_after=datetime(2026, 11, 4, 12, 30, 15, tzinfo=UTC),
        failure_reason="why it stopped" if status in REASON_BEARING else "",
        host_action="rotate the token" if status == "infra_failed" else "",
        run_dir=tmp_path / "run",
        reviewer_sessions=(
            ReviewerSession(name="code-quality", session="s-cq", tool="claude"),
            ReviewerSession(name="security", session="s-sec", tool="codex"),
        ),
        pause_budget_remaining_s=61.26,
        blocking_check_records=(
            {
                "name": "lint",
                "command": "ruff",
                "verdict": "RED",
                "findings": ["gate/lint-001"],
            },
        ),
    )
    fields.update(overrides)
    result = DevelopResult(**fields)  # type: ignore[arg-type]
    return result if result.message else _with_message(result)


def _with_message(result: DevelopResult) -> DevelopResult:
    from dataclasses import replace

    return replace(result, message=result.render_message())


@pytest.mark.parametrize("status", REASON_BEARING + SELF_DESCRIBING)
def test_state_record_is_the_result(status: str, tmp_path: Path) -> None:
    result = _result(status, tmp_path)
    state = result.state_record()

    assert state["status"] == result.status
    assert state["run_id"] == result.run_id
    assert state["branch"] == result.branch
    assert state["worktree"] == str(result.worktree)
    assert state["base_sha"] == result.base_sha
    assert state["rounds"] == result.rounds
    assert state["cost_usd"] == round(result.total_cost_usd, 4)
    assert state["review_profile"] == result.review_profile
    assert state["review_panel"] == [r.reviewer for r in result.reviews]
    assert state["findings_by_severity"] == findings_by_severity(result.reviews)
    assert state["test_gate"] == {"verdict": "RED", "command": "make check"}
    assert state["blocking_checks"] == [dict(c) for c in result.blocking_check_records]
    assert (
        state["open_findings"]
        == result.open_findings
        == [
            {
                "reviewer": "code-quality",
                "finding_id": "f-001",
                "severity": "major",
                "title": "Leaks the handle",
            }
        ]
    )
    assert state["coder_session"] == result.coder_session
    assert state["reviewers"] == {
        "code-quality": {"session": "s-cq", "tool": "claude"},
        "security": {"session": "s-sec", "tool": "codex"},
    }
    assert state["pause_budget_remaining_s"] == 61.3
    assert state["resume_after"] == "2026-11-04T12:30:15+00:00"


@pytest.mark.parametrize("status", REASON_BEARING)
def test_reason_bearing_exits_carry_their_reason_everywhere(
    status: str, tmp_path: Path
) -> None:
    result = _result(status, tmp_path)
    assert result.reason_bearing
    assert result.state_record()["failure_reason"] == "why it stopped"
    assert "why it stopped" in result.message


@pytest.mark.parametrize("status", SELF_DESCRIBING)
def test_self_describing_exits_record_no_reason(status: str, tmp_path: Path) -> None:
    """``approved`` / ``max_rounds`` describe themselves; a sentinel reason must
    not leak into ``state.json`` (#188) nor the message."""
    result = _result(status, tmp_path, failure_reason="no rounds ran")
    assert not result.reason_bearing
    assert result.state_record()["failure_reason"] is None
    assert "no rounds ran" not in result.message
    assert f"{result.rounds} round(s)" in result.message


def test_message_facts_match_the_result(tmp_path: Path) -> None:
    result = _result("max_rounds", tmp_path)
    message = result.message
    assert message.startswith("NOT approved after 3 round(s) (max_rounds)")
    assert "[code-quality]=FINDINGS(blocks max major) [security]=LGTM(pass)" in message
    assert "test gate RED (`make check`)" in message
    assert "blocking gate checks: repo-parity RED (`make parity`)" in message
    assert "2 commit(s) on loom/run-1" in message
    assert f"cost ${result.total_cost_usd:.4f}" in message


def test_interrupted_and_infra_failed_name_the_run_dir(tmp_path: Path) -> None:
    interrupted = _result("interrupted", tmp_path)
    assert interrupted.message.startswith("INTERRUPTED: why it stopped")
    assert f"sessions + handoffs preserved in {tmp_path / 'run'} (re-run to retry)" in (
        interrupted.message
    )

    infra = _result("infra_failed", tmp_path)
    assert infra.message.startswith("INFRA FAILURE: why it stopped — rotate the token")
    assert f"preserved in {tmp_path / 'run'}" in infra.message
    assert infra.state_record()["failure_reason"] == "why it stopped"


def test_stopped_statuses_share_one_shape(tmp_path: Path) -> None:
    for status in ("stalled", "disputed", "cost_exceeded", "needs_decision"):
        message = _result(status, tmp_path).message
        assert message.startswith(f"STOPPED ({status}): why it stopped; last reviews:")


def test_failed_message_is_the_reason_plus_the_gate(tmp_path: Path) -> None:
    result = _result("failed", tmp_path)
    assert result.message == (
        "why it stopped; test gate RED (`make check`); blocking gate checks: "
        "repo-parity RED (`make parity`); 2 commit(s) on loom/run-1"
    )


def test_approved_message_without_gate_or_checks(tmp_path: Path) -> None:
    result = _result("approved", tmp_path, test_gate=None, blocking_checks=())
    assert result.message == (
        "approved by [code-quality]=FINDINGS(blocks max major) [security]=LGTM(pass) "
        f"in 3 round(s); 2 commit(s) on loom/run-1; cost ${result.total_cost_usd:.4f}"
    )
    assert result.state_record()["test_gate"] is None


def test_defaults_keep_the_record_shape_for_a_minimal_result(tmp_path: Path) -> None:
    """A result built with only the required fields (the test doubles' shape)
    still renders a complete, JSON-ready record."""
    result = DevelopResult(
        status="approved",
        run_id="r1",
        worktree=tmp_path,
        branch="b",
        base_sha="0" * 40,
        commits=["c"],
        rounds=1,
        handoff_present=True,
        coder_cost_usd=0.0,
        review_cost_usd=0.0,
        message="m",
    )
    state = result.state_record()
    assert state["reviewers"] == {}
    assert state["blocking_checks"] == []
    assert state["open_findings"] == []
    assert state["resume_after"] is None
    assert state["pause_budget_remaining_s"] == 0.0
    assert (
        result.render_message()
        == "approved by  in 1 round(s); 1 commit(s) on b; cost $0.0000"
    )
