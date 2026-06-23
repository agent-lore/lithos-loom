"""Contract tests for ``run_panel_round`` — the single reviewer-panel primitive.

``develop()`` calls this once per round; review-only mode (#154) calls it once.
The extraction is behaviour-preserving (the full ``develop()`` loop is covered by
``test_story_develop_core.py``); these tests pin the primitive's own contract so
the review-only caller can depend on it directly.

The reviewer turn machinery (``_run_reviewer_with_reaction``) is stubbed so the
test exercises only the panel orchestration: per-reviewer prompt assembly
(round-1 vs re-review), ledger application, cost aggregation, and the
interrupted / invalid short-circuits.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lithos_loom.plugins.story_develop import develop as develop_mod
from lithos_loom.plugins.story_develop.config import DevelopConfig, ReviewerSpec
from lithos_loom.plugins.story_develop.develop import ReviewOutcome, _ReviewerState
from lithos_loom.plugins.story_develop.gate_findings import GateLedger
from lithos_loom.plugins.story_develop.handoff import Finding


def _config(tmp_path: Path) -> DevelopConfig:
    cfg_dir = tmp_path / "fake-claude"
    cfg_dir.mkdir()
    return DevelopConfig(
        repo=tmp_path,
        description="Add a greeting file",
        work_dir=tmp_path / "work",
        claude_config_dir=cfg_dir,
    )


def _reviewer(name: str, tmp_path: Path) -> _ReviewerState:
    return _ReviewerState(ReviewerSpec(name=name), "cid-" + name, [], tmp_path)


@pytest.fixture(autouse=True)
def _stub_render(monkeypatch: pytest.MonkeyPatch) -> None:
    # Keep prompt assembly from touching git / the gate; the panel logic under
    # test is the loop + ledger + aggregation, not the rendered text.
    monkeypatch.setattr(develop_mod.git, "diff_stat", lambda wt, base: "1 file")
    monkeypatch.setattr(
        develop_mod, "render_check_summary", lambda *a, **k: "GATE: pass"
    )


def _install_reviewer_stub(
    monkeypatch: pytest.MonkeyPatch,
    *,
    script: dict[str, dict] | None = None,
) -> list[dict]:
    """Stub ``_run_reviewer_with_reaction``; return a captured-calls list.

    ``script`` maps reviewer name -> the outcome to return:
    ``{status, passed, severity, findings, cost, interrupted}``.
    """
    calls: list[dict] = []
    script = script or {}

    def fake(config, budget, rstate, *, round_no, resume, prompt, timeout, base):
        name = rstate.spec.name
        calls.append(
            {"name": name, "round_no": round_no, "resume": resume, "prompt": prompt}
        )
        spec = script.get(name, {})
        findings = spec.get("findings", [])
        review = ReviewOutcome(
            reviewer=name,
            status=spec.get("status", "LGTM"),
            passed=spec.get("passed", True),
            max_severity=spec.get("severity"),
            findings=findings,
            cost_usd=spec.get("cost", 0.02),
        )
        interrupted = spec.get("interrupted", False)
        resume_after = "RESUME" if interrupted else None
        return review, review.cost_usd, interrupted, resume_after

    monkeypatch.setattr(develop_mod, "_run_reviewer_with_reaction", fake)
    return calls


def _run(config, reviewers, *, round_no):
    return develop_mod.run_panel_round(
        config,
        reviewers,
        wt=config.repo,
        base="0" * 40,
        round_no=round_no,
        check_set=None,
        gate_ledger=GateLedger(),
        budget=develop_mod._PauseBudget(0),
        reviewer_timeout=60,
        coder_summary="the coder did the work",
    )


def test_round_one_runs_each_reviewer_fresh(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = _config(tmp_path)
    reviewers = [_reviewer("correctness", tmp_path), _reviewer("security", tmp_path)]
    calls = _install_reviewer_stub(monkeypatch)

    result = _run(config, reviewers, round_no=1)

    assert [c["name"] for c in calls] == ["correctness", "security"]
    # round 1 is a fresh review for every reviewer (no resume)
    assert all(c["resume"] is False for c in calls)
    # the round-1 prompt targets each reviewer's round_01 handoff file
    assert "round_01_review_correctness" in calls[0]["prompt"]
    assert len(result.round_reviews) == 2
    assert result.interrupted is False
    assert result.invalid_reviewer is None
    assert result.cost == pytest.approx(0.04)


def test_round_two_resumes_with_rereview_prompt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = _config(tmp_path)
    reviewers = [_reviewer("correctness", tmp_path)]
    calls = _install_reviewer_stub(monkeypatch)

    _run(config, reviewers, round_no=2)

    assert calls[0]["resume"] is True
    assert calls[0]["round_no"] == 2
    assert "round_02_review_correctness" in calls[0]["prompt"]


def test_findings_are_applied_to_each_reviewer_ledger(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = _config(tmp_path)
    reviewers = [_reviewer("correctness", tmp_path)]
    finding = Finding(
        finding_id="",  # NEW finding — ledger assigns the canonical id
        severity="major",
        status="open",
        files=["greeting.txt:1"],
        rationale="needs work",
    )
    _install_reviewer_stub(
        monkeypatch,
        script={
            "correctness": {
                "status": "FINDINGS",
                "passed": False,
                "severity": "major",
                "findings": [finding],
            }
        },
    )

    result = _run(config, reviewers, round_no=1)

    # the ledger assigned a canonical id and the outcome reflects the applied set
    applied = reviewers[0].ledger.open_entries()
    assert len(applied) == 1
    assert applied[0].finding_id.startswith("f-")
    assert result.round_reviews[0].findings[0].finding_id.startswith("f-")
    assert reviewers[0].outcome is result.round_reviews[0]


def test_interrupted_reviewer_short_circuits_the_panel(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = _config(tmp_path)
    reviewers = [_reviewer("correctness", tmp_path), _reviewer("security", tmp_path)]
    calls = _install_reviewer_stub(
        monkeypatch, script={"correctness": {"interrupted": True}}
    )

    result = _run(config, reviewers, round_no=1)

    assert result.interrupted is True
    assert result.resume_after == "RESUME"
    # the panel stops at the interrupted reviewer — security never runs
    assert [c["name"] for c in calls] == ["correctness"]


def test_invalid_reviewer_short_circuits_the_panel(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = _config(tmp_path)
    reviewers = [_reviewer("correctness", tmp_path), _reviewer("security", tmp_path)]
    calls = _install_reviewer_stub(
        monkeypatch,
        script={"correctness": {"status": "invalid", "passed": False}},
    )

    result = _run(config, reviewers, round_no=1)

    assert result.invalid_reviewer == "correctness"
    assert [c["name"] for c in calls] == ["correctness"]
