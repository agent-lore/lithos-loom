"""Tests for review-only orchestration ``review_change`` (#154).

Stubs the container / gate / panel boundaries (the same seams
``test_story_develop_core.py`` uses) so the test exercises only the review-only
composition: worktree-at-head, one gate run, ONE panel round (no coder), report
assembly, and worktree cleanup.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lithos_loom.plugins.story_develop import review_only
from lithos_loom.plugins.story_develop.check_set import (
    Check,
    CheckResult,
    CheckSetResult,
)
from lithos_loom.plugins.story_develop.config import DevelopConfig, ReviewerSpec
from lithos_loom.plugins.story_develop.develop import PanelRoundResult, ReviewOutcome
from lithos_loom.plugins.story_develop.handoff import Finding
from lithos_loom.plugins.story_develop.review_resolve import ResolvedChange
from lithos_loom.plugins.story_develop.test_gate import GateResult


def _config(tmp_path: Path, reviewers=("correctness", "security")) -> DevelopConfig:
    cfg_dir = tmp_path / "fake-claude"
    cfg_dir.mkdir()
    return DevelopConfig(
        repo=tmp_path / "repo",
        description="Review this change",
        work_dir=tmp_path / "work",
        claude_config_dir=cfg_dir,
        reviewers=tuple(ReviewerSpec(name=n) for n in reviewers),
    )


_CHANGE = ResolvedChange(
    base_sha="b" * 40, head_sha="h" * 40, head_ref="#142 (feature)", body="do the thing"
)


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict:
    """Install review-only fakes; return captured state."""
    state: dict = {
        "started": [],
        "stopped": [],
        "removed": [],
        "gate_calls": [],
        "panel_calls": [],
        "panel_script": {},  # name -> outcome dict
    }
    wt_path = tmp_path / "wt"
    wt_path.mkdir()

    monkeypatch.setattr(
        review_only.worktree, "create_at", lambda repo, ref, name, parent=None: wt_path
    )
    monkeypatch.setattr(
        review_only.worktree,
        "remove",
        lambda p, force=False: state["removed"].append(p),
    )
    monkeypatch.setattr(
        review_only.containers,
        "start_container",
        lambda cmd: state["started"].append(cmd),
    )
    monkeypatch.setattr(
        review_only.containers,
        "stop_container",
        lambda name: state["stopped"].append(name),
    )
    monkeypatch.setattr(
        review_only, "_build_run_cmd", lambda *a, **k: (k.get("agent", "c"), ["run"])
    )
    monkeypatch.setattr(review_only, "seed_handoff_dir", lambda d: None)

    # one required lint check that passes
    check = Check(name="lint", command="ruff check", state="required", stage="fast")
    gate = GateResult(command="ruff check", exit_code=0, passed=True, output_tail="ok")

    def fake_build_check_set(config, wt):
        return (check,)

    def fake_run_check_set(config, wt, sha, round_no, checks, gate_ledger=None):
        state["gate_calls"].append({"sha": sha, "round_no": round_no})
        return CheckSetResult(
            results=(CheckResult(check=check, execution_outcome="ran", gate=gate),)
        )

    monkeypatch.setattr(review_only, "build_check_set", fake_build_check_set)
    monkeypatch.setattr(review_only, "_run_check_set", fake_run_check_set)

    def fake_panel(config, reviewers, **kwargs):
        state["panel_calls"].append(kwargs)
        round_reviews = []
        for rstate in reviewers:
            spec = state["panel_script"].get(rstate.spec.name, {})
            outcome = ReviewOutcome(
                reviewer=rstate.spec.name,
                status=spec.get("status", "LGTM"),
                passed=spec.get("passed", True),
                max_severity=spec.get("severity"),
                findings=spec.get("findings", []),
                cost_usd=0.02,
            )
            rstate.outcome = outcome
            round_reviews.append(outcome)
        return PanelRoundResult(
            round_reviews=round_reviews,
            cost=0.04,
            interrupted=False,
            resume_after=None,
            invalid_reviewer=None,
        )

    monkeypatch.setattr(review_only, "run_panel_round", fake_panel)
    return state


def test_runs_one_panel_round_no_coder(harness: dict, tmp_path: Path) -> None:
    config = _config(tmp_path)
    report = review_only.review_change(config, _CHANGE)

    # exactly one panel round, at round 1 (review is a one-shot, no fix loop)
    assert len(harness["panel_calls"]) == 1
    assert harness["panel_calls"][0]["round_no"] == 1
    # the coder summary is a neutral external-change stub (there is no coder)
    assert "external" in harness["panel_calls"][0]["coder_summary"].lower()
    # both reviewers reported, all LGTM -> not blocking
    assert {r.name for r in report.reviewers} == {"correctness", "security"}
    assert report.blocking is False


def test_gate_runs_once_on_head_sha(harness: dict, tmp_path: Path) -> None:
    config = _config(tmp_path)
    review_only.review_change(config, _CHANGE)
    assert harness["gate_calls"] == [{"sha": "h" * 40, "round_no": 1}]


def test_findings_flow_into_report_and_block(harness: dict, tmp_path: Path) -> None:
    config = _config(tmp_path)
    harness["panel_script"]["correctness"] = {
        "status": "FINDINGS",
        "passed": False,
        "severity": "critical",
        "findings": [
            Finding(
                finding_id="f-001",
                severity="critical",
                status="open",
                files=["cli/develop.py"],
                rationale="exits before delivery",
            )
        ],
    }
    report = review_only.review_change(config, _CHANGE)

    assert report.blocking is True
    corr = next(r for r in report.reviewers if r.name == "correctness")
    assert corr.findings[0].severity == "critical"
    assert corr.findings[0].files == ["cli/develop.py"]
    assert corr.findings[0].reviewer == "correctness"


def test_worktree_removed_by_default(harness: dict, tmp_path: Path) -> None:
    config = _config(tmp_path)
    review_only.review_change(config, _CHANGE)
    assert len(harness["removed"]) == 1
    # both reviewers' containers torn down
    assert len(harness["stopped"]) == 2


def test_keep_worktree_retains_it(harness: dict, tmp_path: Path) -> None:
    config = _config(tmp_path)
    review_only.review_change(config, _CHANGE, keep_worktree=True)
    assert harness["removed"] == []
