"""Unit tests for the shared round primitives in ``rounds.py``.

The full ``develop()`` round pipeline (``CycleExit`` / ``RoundContext`` / the phase
functions / ``run_round``) is characterised end-to-end by
``test_story_develop_core.py``; this file pins the small shared primitives other
modules drive directly — today ``commit_round`` (ARCH-1.S7), which both
``develop()``'s ``commit_phase`` and ``pr_delivery``'s Copilot fix round call so
the handoff-dir exclusion is single-sourced on ``HANDOFF_DIRNAME``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from lithos_loom.plugins.story_develop import engines as _engines
from lithos_loom.plugins.story_develop import rounds as rounds_mod
from lithos_loom.plugins.story_develop.agent_session import PauseBudget
from lithos_loom.plugins.story_develop.check_set import Check, CheckSetResult
from lithos_loom.plugins.story_develop.config import (
    HANDOFF_DIRNAME,
    DevelopConfig,
    ReviewerSpec,
)
from lithos_loom.plugins.story_develop.gate_findings import GateLedger
from lithos_loom.plugins.story_develop.panel import (
    PanelRoundResult,
    ReviewerState,
    ReviewOutcome,
)
from lithos_loom.plugins.story_develop.rounds import commit_round


def _init_repo(path: Path) -> None:
    def g(*args: str) -> None:
        subprocess.run(["git", *args], cwd=path, check=True, capture_output=True)

    path.mkdir(parents=True, exist_ok=True)
    g("init", "-q")
    g("config", "user.email", "t@t")
    g("config", "user.name", "t")
    g("commit", "--allow-empty", "-q", "-m", "root")


def _tracked_at_head(repo: Path) -> list[str]:
    out = subprocess.run(
        ["git", "show", "--name-only", "--format=", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return out.stdout.split()


def test_commit_round_commits_work_but_excludes_the_handoff_dir(tmp_path: Path) -> None:
    repo = tmp_path / "wt"
    _init_repo(repo)
    (repo / "src.py").write_text("print('work')\n", encoding="utf-8")
    handoff_dir = repo / HANDOFF_DIRNAME
    handoff_dir.mkdir()
    (handoff_dir / "round_01_coder_done.md").write_text(
        "## Status: LGTM\n", encoding="utf-8"
    )

    sha = commit_round(repo, "story-develop r1: do the thing")

    assert sha is not None
    tracked = _tracked_at_head(repo)
    assert "src.py" in tracked
    # the handoff scaffolding must never reach the deliverable commit
    assert not any(HANDOFF_DIRNAME in t for t in tracked)


def test_commit_round_returns_none_when_only_excluded_work_is_present(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "wt"
    _init_repo(repo)
    # only a handoff dir present -> excluded -> nothing staged -> no commit
    handoff_dir = repo / HANDOFF_DIRNAME
    handoff_dir.mkdir()
    (handoff_dir / "x.md").write_text("x\n", encoding="utf-8")

    assert commit_round(repo, "empty round") is None


def test_handoff_dirname_matches_the_legacy_delivery_literal() -> None:
    # ARCH-1.S7: pr_delivery's Copilot-round commit hardcoded exclude=[".handoff"]
    # while develop used HANDOFF_DIRNAME (accidental drift). Both now route through
    # commit_round(exclude=[HANDOFF_DIRNAME]); pin the constant to the value the
    # literal carried so the drift-fix stays behaviour-preserving.
    assert HANDOFF_DIRNAME == ".handoff"


# --- artifact-review pass holds approval (#283 / PR #291 review) --------------


def _passed(reviewer: str = "correctness") -> ReviewOutcome:
    return ReviewOutcome(
        reviewer=reviewer, status="LGTM", passed=True, max_severity=None
    )


def _failed_outcome(reviewer: str = "correctness") -> ReviewOutcome:
    from lithos_loom.plugins.story_develop.handoff import Finding

    return ReviewOutcome(
        reviewer=reviewer,
        status="FINDINGS",
        passed=False,
        max_severity="major",
        findings=[
            Finding(
                finding_id="f-101",
                severity="major",
                status="open",
                files=["note-320.png"],
                rationale="overflow",
            )
        ],
    )


def _artifact_ctx(tmp_path: Path, *, collects: bool, panel_passes: bool) -> tuple:
    """A minimal RoundContext aimed at approval_phase: reviews already passed,
    one candidate check whose (stubbed) run publishes an artifact when
    *collects*, and a run_panel_round stub scripted by *panel_passes*."""
    config = DevelopConfig(
        repo=tmp_path / "repo",
        description="x",
        work_dir=tmp_path / "run",
        artifacts_path="e2e/artifacts",
    )
    panel_calls: list[dict] = []

    def fake_run_check_set(cfg, wt, sha, round_no, checks, ledger):
        if collects:
            shots = cfg.artifacts_dir / f"round_{round_no:02d}" / "repo-parity"
            shots.mkdir(parents=True, exist_ok=True)
            (shots / "note-320.png").write_text("png")
        return CheckSetResult(())

    def fake_run_panel_round(cfg, reviewers, **kw):
        panel_calls.append(kw)
        outcome = _passed() if panel_passes else _failed_outcome()
        return PanelRoundResult(
            round_reviews=[outcome],
            cost=0.02,
            interrupted=False,
            resume_after=None,
            invalid_reviewer=None,
        )

    services = rounds_mod.Services(
        run_turn=lambda **kw: (_ for _ in ()).throw(AssertionError("no turns")),
        sleep=lambda s: None,
        start_container=lambda cmd: "cid",
        stop_container=lambda cid: None,
        run_check_set=fake_run_check_set,
    )
    spec = ReviewerSpec(name="correctness", tool="claude")
    rstate = ReviewerState(spec, "container", ["cmd"], tmp_path)
    ctx = rounds_mod.RoundContext(
        config=config,
        wt=tmp_path / "repo",
        base="0" * 40,
        names=["correctness"],
        services=services,
        reviewers=[rstate],
        coder_container="coder",
        coder_engine=_engines.get_engine("claude"),
        coder_timeout=60,
        reviewer_timeout=60,
        fast_checks=(),
        candidate_checks=(
            Check(
                name="repo-parity",
                command="make e2e",
                state="required",
                stage="candidate",
                raw_exit=True,
            ),
        ),
        formatters=[],
        gate_ledger=GateLedger(),
        budget=PauseBudget(0),
        coder_session="s",
        turn_with_limit_pauses=lambda **kw: None,  # type: ignore[arg-type]
        run_panel_round=fake_run_panel_round,
        resume_after_from=lambda t: None,  # type: ignore[arg-type]
        render_panel_findings=lambda r: "",
        coder_summary=lambda c, r: "",
        record_coder_disputes=lambda c, r, n: None,
        coder_handoff_nudge=lambda r: "",
    )
    ctx.final_reviews = [_passed()]
    ctx.gated_sha = "a" * 40
    return ctx, panel_calls


def test_approval_held_for_artifact_pass_then_sealed(tmp_path: Path) -> None:
    # PR #291 review (High): candidate checks collect screenshots AFTER the
    # panel; sealing without a reviewer seeing them defeats #283. The pass runs
    # (artifact_pass=True), LGTMs, and only then does approval seal.
    ctx, panel_calls = _artifact_ctx(tmp_path, collects=True, panel_passes=True)

    exit_ = rounds_mod.approval_phase(ctx, 1)

    assert [c.get("artifact_pass") for c in panel_calls] == [True]
    assert exit_ is not None and exit_.status == "approved"
    assert ctx.review_cost == pytest.approx(0.02)


def test_artifact_pass_findings_hold_approval_and_continue(tmp_path: Path) -> None:
    ctx, panel_calls = _artifact_ctx(tmp_path, collects=True, panel_passes=False)

    exit_ = rounds_mod.approval_phase(ctx, 1)

    assert len(panel_calls) == 1
    assert exit_ is None  # loop continues; the coder answers the findings
    assert ctx.final_reviews and ctx.final_reviews[0].passed is False


def test_no_new_artifacts_seals_without_extra_pass(tmp_path: Path) -> None:
    ctx, panel_calls = _artifact_ctx(tmp_path, collects=False, panel_passes=True)

    exit_ = rounds_mod.approval_phase(ctx, 1)

    assert panel_calls == []
    assert exit_ is not None and exit_.status == "approved"


def test_unchanged_artifacts_do_not_retrigger_the_pass(tmp_path: Path) -> None:
    # The no-loop property: after a findings pass, the NEXT approval attempt on
    # the SAME sha (candidate dedup skips the re-run, artifacts unchanged)
    # seals without a second artifact pass.
    ctx, panel_calls = _artifact_ctx(tmp_path, collects=True, panel_passes=True)
    first = rounds_mod.approval_phase(ctx, 1)
    assert first is not None and first.status == "approved"
    assert len(panel_calls) == 1

    # next round: panel passed again (its prompt now includes the artifacts),
    # same gated sha -> candidate skipped -> artifacts view unchanged
    ctx.final_reviews = [_passed()]
    exit_ = rounds_mod.approval_phase(ctx, 2)

    assert len(panel_calls) == 1  # no second pass
    assert exit_ is not None and exit_.status == "approved"


def test_combined_outcome_keeps_regular_findings_through_artifact_lgtm(
    tmp_path: Path,
) -> None:
    """#291 round 4: replacing final_reviews with the artifact pass's outcomes
    made the regular review's surviving non-blocking findings vanish from the
    structured result (DevelopResult / state.json metadata) even though the
    ledger kept them. The combined outcome must retain them."""
    from lithos_loom.plugins.story_develop.handoff import Finding

    ctx, panel_calls = _artifact_ctx(tmp_path, collects=True, panel_passes=True)
    minor = Finding(
        finding_id="f-001",
        severity="minor",
        status="open",
        files=["style.css:1"],
        rationale="nit",
    )
    ctx.final_reviews = [
        ReviewOutcome(
            reviewer="correctness",
            status="FINDINGS",
            passed=True,  # minor is below the major threshold
            max_severity="minor",
            findings=[minor],
        )
    ]

    exit_ = rounds_mod.approval_phase(ctx, 1)

    assert exit_ is not None and exit_.status == "approved"
    assert len(panel_calls) == 1
    out = ctx.final_reviews[0]
    assert out.passed is True
    assert [f.finding_id for f in out.findings] == ["f-001"]  # minor SURVIVES
    assert out.max_severity == "minor"
    assert out.status == "FINDINGS"  # findings exist, even though approved


def test_combined_outcome_appends_visual_findings_and_blocks(
    tmp_path: Path,
) -> None:
    from lithos_loom.plugins.story_develop.handoff import Finding

    ctx, panel_calls = _artifact_ctx(tmp_path, collects=True, panel_passes=False)
    minor = Finding(
        finding_id="f-001",
        severity="minor",
        status="open",
        files=["style.css:1"],
        rationale="nit",
    )
    ctx.final_reviews = [
        ReviewOutcome(
            reviewer="correctness",
            status="FINDINGS",
            passed=True,
            max_severity="minor",
            findings=[minor],
        )
    ]

    exit_ = rounds_mod.approval_phase(ctx, 1)

    assert exit_ is None  # visual findings hold approval
    out = ctx.final_reviews[0]
    assert out.passed is False  # conjunction of verdicts
    assert len(out.findings) == 2  # minor retained + visual appended
    assert out.max_severity == "major"
