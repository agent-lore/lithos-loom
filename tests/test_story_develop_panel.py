"""Contract tests for ``run_panel_round`` — the single reviewer-panel primitive.

``develop()`` calls this once per round; review-only mode (#154) calls it once.
The extraction is behaviour-preserving (the full ``develop()`` loop is covered by
``test_story_develop_core.py``); these tests pin the primitive's own contract so
the review-only caller can depend on it directly.

Most tests stub the reviewer turn machinery (``_run_reviewer_with_reaction``) so
they exercise only the panel orchestration: per-reviewer prompt assembly (round-1
vs re-review), ledger application, cost aggregation, and the interrupted / invalid
short-circuits. The last test leaves it live to pin the ARCH-1.S5 contract that
the round routes the reviewer turn through the *injected* :class:`Services`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lithos_loom.plugins.story_develop import panel as panel_mod
from lithos_loom.plugins.story_develop.config import DevelopConfig, ReviewerSpec
from lithos_loom.plugins.story_develop.gate_findings import GateLedger
from lithos_loom.plugins.story_develop.handoff import Finding
from lithos_loom.plugins.story_develop.panel import (
    ReviewerState as _ReviewerState,
)
from lithos_loom.plugins.story_develop.panel import (
    ReviewOutcome,
)
from lithos_loom.plugins.story_develop.rounds import Services
from lithos_loom.plugins.story_develop.turns import TurnResult


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
    # test is the loop + ledger + aggregation, not the rendered text. Patch on
    # the panel module — run_panel_round now reads these off its own globals.
    monkeypatch.setattr(panel_mod.git, "diff_stat", lambda wt, base: "1 file")
    monkeypatch.setattr(panel_mod, "render_check_summary", lambda *a, **k: "GATE: pass")


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

    def fake(
        config, budget, rstate, *, services, round_no, resume, prompt, timeout, base
    ):
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

    monkeypatch.setattr(panel_mod, "_run_reviewer_with_reaction", fake)
    return calls


def _run(config, reviewers, *, round_no):
    return panel_mod.run_panel_round(
        config,
        reviewers,
        wt=config.repo,
        base="0" * 40,
        round_no=round_no,
        check_set=None,
        gate_ledger=GateLedger(),
        budget=panel_mod.PauseBudget(0),
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


def test_run_panel_round_routes_the_reviewer_turn_through_injected_services(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # ARCH-1.S5: the #154 primitive must run the reviewer turn through the
    # INJECTED Services — not a hardcoded Services.live() — so that develop()'s
    # run_turn / _sleep patches take effect. Here _run_reviewer_with_reaction is
    # NOT stubbed, so the real reviewer path drives the fake run_turn.
    monkeypatch.setattr(
        panel_mod.limits, "record_failure_fixture", lambda *a, **k: None
    )
    config = _config(tmp_path)
    reviewers = [_reviewer("correctness", tmp_path)]
    calls: list[dict] = []

    def fake_run_turn(**kw: object) -> TurnResult:
        calls.append(kw)
        # a failed, non-usage-limit turn: no handoff written -> invalid review,
        # and the reaction loop returns immediately (no fallback, no pause)
        return TurnResult(
            exit_code=1,
            succeeded=False,
            session_id="",
            result_text="boom",
            cost_usd=0.03,
            raw=None,
            stderr="",
        )

    services = Services(
        run_turn=fake_run_turn,
        sleep=lambda seconds: None,
        start_container=lambda cmd: "cid",
        stop_container=lambda name: None,
        run_check_set=lambda *a, **k: None,
    )

    result = panel_mod.run_panel_round(
        config,
        reviewers,
        wt=config.repo,
        base="0" * 40,
        round_no=1,
        check_set=None,
        gate_ledger=GateLedger(),
        budget=panel_mod.PauseBudget(600),
        reviewer_timeout=60,
        coder_summary="the coder did the work",
        services=services,
    )

    # the reviewer turn ran through the injected fake (against this reviewer's
    # container), and its cost + invalid outcome flowed back out
    assert [c["container"] for c in calls] == ["cid-correctness"]
    assert result.round_reviews[0].status == "invalid"
    assert result.cost == pytest.approx(0.03)
