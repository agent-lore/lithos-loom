"""Unit tests for the shared round primitives in ``rounds.py``.

The full ``develop()`` round pipeline (``CycleExit`` / ``RoundContext`` / the phase
functions / ``run_round``) is characterised end-to-end by
``test_story_develop_core.py``; this file pins the small shared primitives other
modules drive directly — today ``commit_round`` (ARCH-1.S7), which both
``develop()``'s ``commit_phase`` and ``pr_delivery``'s Copilot fix round call so
the handoff-dir exclusion is single-sourced on ``HANDOFF_DIRNAME``.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from lithos_loom.plugins.story_develop import check_artifacts
from lithos_loom.plugins.story_develop import engines as _engines
from lithos_loom.plugins.story_develop import rounds as rounds_mod
from lithos_loom.plugins.story_develop.agent_session import PauseBudget, TurnAttempt
from lithos_loom.plugins.story_develop.check_set import (
    Check,
    CheckResult,
    CheckSetResult,
    render_check_summary,
)
from lithos_loom.plugins.story_develop.config import (
    HANDOFF_DIRNAME,
    DevelopConfig,
    ReviewerSpec,
)
from lithos_loom.plugins.story_develop.gate_findings import GateLedger
from lithos_loom.plugins.story_develop.handoff import Finding, ReviewHandoff
from lithos_loom.plugins.story_develop.loop_entry import PostCommitOutcome
from lithos_loom.plugins.story_develop.panel import (
    PanelRoundResult,
    ReviewerState,
    ReviewOutcome,
)
from lithos_loom.plugins.story_develop.rounds import commit_round
from lithos_loom.plugins.story_develop.test_gate import GateResult


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
            # 793edc9f: the real collector stamps every snapshot with the sha
            # it captured; the freshness guard reads it, so the fixture must
            # write it too or every approval holds as "stale".
            (shots / check_artifacts.CAPTURE_MANIFEST).write_text(
                json.dumps({"sha": sha, "round": round_no, "check": "repo-parity"})
            )
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
        base=rounds_mod.git.RangeBase("0" * 40),
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
        turn_with_reactions=lambda **kw: None,  # type: ignore[arg-type]
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


def test_the_artifact_pass_joins_the_rounds_review_provenance(
    tmp_path: Path,
) -> None:
    """5dbeb0c8 slice C: a visual finding must survive a later infra death.

    ``panel_phase`` vouches for the regular reviewer handoffs, but the artifact
    pass writes its approval-controlling verdicts to their own ``_artifacts``
    files. Left out of ``reviewed_digests``, a round that ended in a held
    artifact finding and then died resumed on the regular pass's LGTM alone —
    the one finding actually holding the run silently dropped from the intake.
    """
    from lithos_loom.plugins.story_develop import handoff as handoff_mod

    ctx, _ = _artifact_ctx(tmp_path, collects=True, panel_passes=False)
    handoff_dir = ctx.config.handoff_dir
    handoff_dir.mkdir(parents=True, exist_ok=True)
    regular = handoff_dir / handoff_mod.reviewer_handoff_name(1, "correctness")
    regular.write_text("## Status: LGTM\n## Summary\ncode reads fine\n")
    artifacts = handoff_dir / handoff_mod.reviewer_handoff_name(
        1, handoff_mod.artifact_reviewer_token("correctness")
    )
    artifacts.write_text("## Status: FINDINGS\n## Summary\nnote-320 overflows\n")
    # what `panel_phase` already recorded for the regular pass this round
    ctx.reviewed_round = 1
    ctx.reviewed_digests[1] = {
        "correctness": handoff_mod.file_fingerprint(regular) or ""
    }

    assert rounds_mod.approval_phase(ctx, 1) is None  # the pass filed findings

    # ADDED to the round, never replacing it: both handoffs are what a resume
    # reads, and each under the token its filename is built from.
    assert ctx.reviewed_round == 1
    assert set(ctx.reviewed_digests[1]) == {"correctness", "correctness_artifacts"}
    assert ctx.reviewed_digests[1]["correctness_artifacts"] == (
        handoff_mod.file_fingerprint(artifacts)
    )


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


def test_combined_max_severity_ignores_resolved_findings(tmp_path: Path) -> None:
    """#291 round 5 (low): a FIXED major from the regular review + visual LGTM
    must not headline max_severity=major on an approved outcome."""
    from lithos_loom.plugins.story_develop.handoff import Finding

    ctx, panel_calls = _artifact_ctx(tmp_path, collects=True, panel_passes=True)
    fixed_major = Finding(
        finding_id="f-001",
        severity="major",
        status="fixed",
        files=["a.py:1"],
        rationale="was fixed in round 2",
    )
    ctx.final_reviews = [
        ReviewOutcome(
            reviewer="correctness",
            status="FINDINGS",
            passed=True,
            max_severity=None,
            findings=[fixed_major],
        )
    ]

    exit_ = rounds_mod.approval_phase(ctx, 1)

    assert exit_ is not None and exit_.status == "approved"
    out = ctx.final_reviews[0]
    assert out.max_severity is None  # no OPEN findings
    assert [f.finding_id for f in out.findings] == ["f-001"]  # still recorded


# ── Capture-freshness approval hold (793edc9f) ─────────────────────────


def _stale_snapshot(ctx, *, sha: str | None) -> None:
    """A pre-existing round_01 snapshot that does NOT describe ctx.gated_sha."""
    d = ctx.config.artifacts_dir / "round_01" / "repo-parity"
    d.mkdir(parents=True, exist_ok=True)
    (d / "note-320.png").write_text("old png")
    if sha is not None:
        (d / check_artifacts.CAPTURE_MANIFEST).write_text(
            json.dumps({"sha": sha, "round": 1, "check": "repo-parity"})
        )


def test_stale_captures_hold_approval_with_notice(tmp_path: Path) -> None:
    # 793edc9f (T1-S11): snapshots exist but none was rendered from the tree
    # under review — the candidate re-capture was skipped or produced nothing.
    # Sealing (or running the artifact pass on those pixels) would mark
    # rendered-output findings "fixed" that nobody has seen. Approval is held,
    # NO artifact pass runs, and the notice is queued for the next coder round.
    ctx, panel_calls = _artifact_ctx(tmp_path, collects=False, panel_passes=True)
    _stale_snapshot(ctx, sha="0" * 40)  # captured from an OLDER commit

    exit_ = rounds_mod.approval_phase(ctx, 2)

    assert exit_ is None  # held, not approved
    assert panel_calls == []  # no artifact pass over stale pixels
    assert ctx.artifact_capture_notice is not None
    assert "approval is held" in ctx.artifact_capture_notice
    assert ctx.gated_sha[:12] in ctx.artifact_capture_notice


def test_manifest_less_captures_hold_approval(tmp_path: Path) -> None:
    # A snapshot with no provenance manifest (pre-upgrade run resumed across
    # versions, operator-seeded dir) is UNKNOWN — treated as stale, fail
    # closed, never "assume current".
    ctx, panel_calls = _artifact_ctx(tmp_path, collects=False, panel_passes=True)
    _stale_snapshot(ctx, sha=None)

    exit_ = rounds_mod.approval_phase(ctx, 2)

    assert exit_ is None
    assert panel_calls == []
    assert ctx.artifact_capture_notice is not None


def _errored_candidate_services(ctx, check_name: str):
    """Services whose candidate run reports one ERRORED candidate check."""

    def errored_run_check_set(cfg, wt, sha, round_no, checks, ledger):
        errored_check = Check(
            name=check_name,
            command="x",
            state="required",
            stage="candidate",
            raw_exit=True,
        )
        return CheckSetResult(
            (
                CheckResult(
                    check=errored_check,
                    execution_outcome="errored",
                    gate=None,
                ),
            )
        )

    return rounds_mod.Services(
        run_turn=ctx.services.run_turn,
        sleep=ctx.services.sleep,
        start_container=ctx.services.start_container,
        stop_container=ctx.services.stop_container,
        run_check_set=errored_run_check_set,
    )


def test_errored_capture_check_holds_via_freshness(tmp_path: Path) -> None:
    # S4, the T1-S11 shape: the artifact-producing candidate check errors, so
    # nothing re-captures and the previous round's snapshot stays newest. The
    # freshness classification alone holds (an errored check publishes no
    # snapshot, so the sealing sha can have no CURRENT capture) — no separate
    # errored-check trigger needed. The notice names the errored check as the
    # likely cause.
    ctx, panel_calls = _artifact_ctx(tmp_path, collects=False, panel_passes=True)
    _stale_snapshot(ctx, sha="0" * 40)  # the previous round's capture
    ctx.services = _errored_candidate_services(ctx, "repo-parity")

    exit_ = rounds_mod.approval_phase(ctx, 2)

    assert exit_ is None
    assert panel_calls == []
    assert ctx.artifact_capture_notice is not None
    assert "repo-parity" in ctx.artifact_capture_notice


def test_errored_nonartifact_candidate_does_not_hold_current_capture(
    tmp_path: Path,
) -> None:
    # PR #340 review (P1): the thorough profile candidate-stages dep-audit /
    # coverage / semgrep, which produce no artifacts. An unrelated errored
    # candidate check must NOT hold approval when the actual capture is
    # CURRENT — that would stall the loop on infrastructure noise.
    ctx, panel_calls = _artifact_ctx(tmp_path, collects=False, panel_passes=True)
    _stale_snapshot(ctx, sha=ctx.gated_sha)  # capture IS current
    ctx.services = _errored_candidate_services(ctx, "dep-audit")

    exit_ = rounds_mod.approval_phase(ctx, 2)

    assert exit_ is not None and exit_.status == "approved"
    assert ctx.artifact_capture_notice is None


def test_errored_candidate_with_no_captures_seals(tmp_path: Path) -> None:
    # Documented residual (spec §5.5): a first-round errored capture check
    # with NO snapshots at all classifies no_artifacts and seals — there are
    # no stale pixels to falsely verify, and holding would livelock repos
    # whose artifacts path legitimately produces nothing. The floor's
    # errored-passes semantics are out of scope for this guard.
    ctx, panel_calls = _artifact_ctx(tmp_path, collects=False, panel_passes=True)
    ctx.services = _errored_candidate_services(ctx, "repo-parity")

    exit_ = rounds_mod.approval_phase(ctx, 2)

    assert exit_ is not None and exit_.status == "approved"
    assert ctx.artifact_capture_notice is None


def test_current_captures_still_seal_through_artifact_pass(tmp_path: Path) -> None:
    # The happy path is unchanged: a capture from the tree under review runs
    # the artifact pass and seals on its LGTM. (Same behaviour the pre-guard
    # tests pin; re-asserted here against a freshness-guard regression.)
    ctx, panel_calls = _artifact_ctx(tmp_path, collects=True, panel_passes=True)

    exit_ = rounds_mod.approval_phase(ctx, 1)

    assert exit_ is not None and exit_.status == "approved"
    assert [kw.get("artifact_pass") for kw in panel_calls] == [True]
    assert ctx.artifact_capture_notice is None


def test_capture_notice_reaches_next_coder_prompt(tmp_path: Path) -> None:
    # The hold must not be a silent stall: the next coder round's gate slot
    # carries the notice so the loop can FIX the capture. The notice is
    # consumed (cleared) once delivered.
    ctx, _ = _artifact_ctx(tmp_path, collects=False, panel_passes=True)
    ctx.artifact_capture_notice = (
        "## Artifact capture is stale — approval is held\n\ndetails here"
    )
    prompts: list[str] = []

    def recording_turn(
        config,
        budget,
        *,
        services,
        agent,
        container,
        config_dir,
        prompt,
        session_id,
        resume,
        round_no,
        timeout,
        engine,
    ):
        prompts.append(prompt)
        # write the handoff so coder_phase's gate passes
        handoff_file = config.handoff_dir / f"round_{round_no:02d}_coder_done.md"
        handoff_file.parent.mkdir(parents=True, exist_ok=True)
        handoff_file.write_text("done")
        turn = _engines.TurnResult(
            exit_code=0,
            succeeded=True,
            completed=True,
            session_id="s",
            result_text="",
            cost_usd=0.0,
            raw=None,
            stderr="",
        )
        return TurnAttempt(turn, False, 0.0)

    ctx.turn_with_reactions = recording_turn
    ctx.final_reviews = [_failed_outcome()]
    (ctx.wt).mkdir(parents=True, exist_ok=True)

    exit_ = rounds_mod.coder_phase(ctx, 2)

    assert exit_ is None
    assert len(prompts) == 1
    assert "Artifact capture is stale" in prompts[0]
    assert ctx.artifact_capture_notice is None  # consumed


def test_resumed_run_reads_provenance_from_disk(tmp_path: Path) -> None:
    # Resume shape: a daemon re-dispatch reuses the same artifacts_dir with a
    # BRAND-NEW RoundContext (in-memory state gone). Freshness comes from the
    # on-disk manifest, so a snapshot captured from the current tree before
    # the interruption still counts as current — the run seals instead of
    # holding on state it lost.
    ctx, panel_calls = _artifact_ctx(tmp_path, collects=False, panel_passes=True)
    _stale_snapshot(ctx, sha=ctx.gated_sha)  # written "before the restart"

    exit_ = rounds_mod.approval_phase(ctx, 2)

    # The panel of this (fresh) round already saw the snapshot in its prompt
    # and the candidate run changed nothing, so no extra artifact pass is due.
    assert exit_ is not None and exit_.status == "approved"
    assert ctx.artifact_capture_notice is None


def test_commit_phase_runs_the_post_commit_pass_after_formatting(
    tmp_path: Path, tmp_git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PRD S4: the entry's post-commit pass (resolve mode's regenerate) runs
    after the round commit AND the format pass, and its commit is the one the
    gate sees; a pass that changes nothing leaves the format commit in place."""
    ctx, _calls = _artifact_ctx(tmp_path, collects=False, panel_passes=True)
    ctx.wt = tmp_git_repo
    (tmp_git_repo / "src.py").write_text("x = 1\n")
    order: list[str] = []

    def fake_format(config, wt, round_no, formatters):
        order.append("format")
        return None

    def regen(wt: Path, round_no: int) -> PostCommitOutcome:
        order.append(f"regen r{round_no}")
        (wt / "gen.json").write_text("{}\n")
        subprocess.run(["git", "add", "-A"], cwd=wt, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "regenerate"], cwd=wt, check=True)
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=wt, capture_output=True, text=True
        ).stdout.strip()
        return PostCommitOutcome(sha=sha)

    monkeypatch.setattr(rounds_mod.autoformat, "run_format_pass", fake_format)
    ctx.post_commit_pass = regen

    exit_ = rounds_mod.commit_phase(ctx, 3)

    assert exit_ is None
    assert order == ["format", "regen r3"]
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_git_repo, capture_output=True, text=True
    ).stdout.strip()
    assert ctx.new_commit == head and ctx.gated_sha == head
    assert (tmp_git_repo / "gen.json").exists()

    # a no-op pass keeps the round commit as the gated tree
    ctx.post_commit_pass = lambda wt, n: PostCommitOutcome()
    (tmp_git_repo / "src.py").write_text("x = 2\n")
    assert rounds_mod.commit_phase(ctx, 4) is None
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_git_repo, capture_output=True, text=True
    ).stdout.strip()
    assert ctx.new_commit == head


def _regen_row(*, passed: bool, exit_code: int = 0, tail: str = "gen") -> CheckResult:
    return CheckResult(
        check=Check(
            name="regenerate", command="make diagrams", state="required", raw_exit=True
        ),
        execution_outcome="timed_out" if exit_code == 124 else "ran",
        gate=GateResult(
            command="make diagrams",
            exit_code=exit_code,
            passed=passed,
            output_tail=tail,
        ),
    )


def _head(repo: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True
    ).stdout.strip()


def test_a_red_post_commit_pass_holds_approval_until_a_later_commit_passes(
    tmp_path: Path, tmp_git_repo: Path
) -> None:
    """PR #388 review (High): a project need not have a parity / drift check,
    so approving reviewers alone must never seal a tree whose generated paths
    the generator could not rebuild. The pass's verdict is a REQUIRED check
    row riding with the round's commit: the floor holds approval, the next
    coder prompt carries the output, the epilogue's raw-check list names it —
    and a round with no new commit keeps it (the tree is unchanged)."""
    ctx, _calls = _artifact_ctx(tmp_path, collects=False, panel_passes=True)
    ctx.wt = tmp_git_repo
    ctx.candidate_checks = ()  # no parity check anywhere
    outcomes = {
        1: PostCommitOutcome(
            row=_regen_row(passed=False, exit_code=2, tail="guardrail: orphan module")
        ),
        3: PostCommitOutcome(row=_regen_row(passed=True)),
    }
    ctx.post_commit_pass = lambda wt, n: outcomes[n]

    (tmp_git_repo / "src.py").write_text("x = 1\n")
    assert rounds_mod.commit_phase(ctx, 1) is None
    assert rounds_mod.fast_gate_phase(ctx, 1) is None
    assert rounds_mod.approval_phase(ctx, 1) is None  # held, reviewers passing
    assert ctx.check_set is not None
    assert [r.check.name for r in ctx.check_set.results] == ["regenerate"]
    assert not ctx.check_set.blocking_passed
    assert [r.check.name for r in ctx.check_set.failing_raw_checks] == ["regenerate"]
    brief = render_check_summary(ctx.check_set, for_coder=True)
    assert "regenerate gate (FAILED)" in brief and "guardrail: orphan module" in brief

    # no new commit: the pass does not run and the red row still describes HEAD
    assert rounds_mod.commit_phase(ctx, 2) is None
    assert ctx.new_commit is None
    assert rounds_mod.fast_gate_phase(ctx, 2) is None
    assert rounds_mod.approval_phase(ctx, 2) is None
    assert ctx.check_set is not None and not ctx.check_set.blocking_passed

    # a commit the generator accepts: ONE row, green, and approval seals
    (tmp_git_repo / "src.py").write_text("x = 2\n")
    assert rounds_mod.commit_phase(ctx, 3) is None
    assert rounds_mod.fast_gate_phase(ctx, 3) is None
    exit_ = rounds_mod.approval_phase(ctx, 3)
    assert exit_ is not None and exit_.status == "approved"
    assert ctx.check_set is not None
    rows = [r for r in ctx.check_set.results if r.check.name == "regenerate"]
    assert len(rows) == 1 and rows[0].passed


def test_the_post_commit_row_joins_a_fresh_fast_check_set(
    tmp_path: Path, tmp_git_repo: Path
) -> None:
    """The production shape (a Python repo: lint / typecheck fast checks):
    fast_gate_phase OVERWRITES the check-set with this commit's fresh run and
    only then joins the pass's row — the row must survive that overwrite
    (and an errored run that yields no set at all)."""
    ctx, _calls = _artifact_ctx(tmp_path, collects=False, panel_passes=True)
    ctx.wt = tmp_git_repo
    ctx.candidate_checks = ()
    lint = Check(name="lint", command="ruff check", state="required")
    ctx.fast_checks = (lint,)
    fresh: dict[str, CheckSetResult | None] = {}

    def fake_run_check_set(cfg, wt, sha, round_no, checks, ledger):
        assert checks == (lint,)
        return fresh[sha]

    ctx.services = rounds_mod.Services(
        run_turn=ctx.services.run_turn,
        sleep=ctx.services.sleep,
        start_container=ctx.services.start_container,
        stop_container=ctx.services.stop_container,
        run_check_set=fake_run_check_set,
    )
    ctx.post_commit_pass = lambda wt, n: PostCommitOutcome(
        row=_regen_row(passed=False, exit_code=2)
    )

    (tmp_git_repo / "src.py").write_text("x = 1\n")
    assert rounds_mod.commit_phase(ctx, 1) is None
    green_lint = CheckResult(
        check=lint,
        execution_outcome="ran",
        gate=GateResult(command="ruff check", exit_code=0, passed=True, output_tail=""),
    )
    fresh[_head(tmp_git_repo)] = CheckSetResult((green_lint,))
    assert rounds_mod.fast_gate_phase(ctx, 1) is None
    assert ctx.check_set is not None
    assert [r.check.name for r in ctx.check_set.results] == ["lint", "regenerate"]
    assert rounds_mod.approval_phase(ctx, 1) is None  # lint green, regenerate holds

    # a check-set run that errored out entirely (None) still carries the row
    (tmp_git_repo / "src.py").write_text("x = 2\n")
    assert rounds_mod.commit_phase(ctx, 2) is None
    fresh[_head(tmp_git_repo)] = None
    assert rounds_mod.fast_gate_phase(ctx, 2) is None
    assert ctx.check_set is not None
    assert [r.check.name for r in ctx.check_set.results] == ["regenerate"]
    assert rounds_mod.approval_phase(ctx, 2) is None


def test_a_post_commit_pass_that_cannot_run_ends_the_round_infra_failed(
    tmp_path: Path, tmp_git_repo: Path
) -> None:
    """PR #388 review (High), the other half: a pass that could not run at all
    (export / container / copy-back) has no verdict for the coder to act on —
    the round is terminal ``infra_failed`` with the host action, never a
    silent continue into the gate and the panel."""
    ctx, _calls = _artifact_ctx(tmp_path, collects=False, panel_passes=True)
    ctx.wt = tmp_git_repo
    dead = PostCommitOutcome(
        infra_error=(
            "regenerate pass could not run (`make diagrams`): docker: not found"
        ),
        host_action="check docker on the host, then complete the gate to re-dispatch",
    )
    ctx.post_commit_pass = lambda wt, n: dead
    (tmp_git_repo / "src.py").write_text("x = 1\n")

    exit_ = rounds_mod.commit_phase(ctx, 1)

    assert exit_ is not None and exit_.status == "infra_failed"
    assert exit_.failure_reason == (
        "round 1: regenerate pass could not run (`make diagrams`): docker: not found"
    )
    assert exit_.host_action.startswith("check docker on the host")
    # the round's commit exists and is recorded; nothing was gated or reviewed
    assert ctx.new_commit == _head(tmp_git_repo) and ctx.gated_sha == ctx.new_commit
    assert ctx.post_commit_row is None


def test_commit_phase_honours_the_pre_commit_guard(
    tmp_path: Path, tmp_git_repo: Path
) -> None:
    """S5: a conflict-resolution round must not commit a tree that still carries
    conflict markers — the guard's reason is the round's failure, and nothing
    is committed."""
    ctx, _calls = _artifact_ctx(tmp_path, collects=False, panel_passes=True)
    ctx.wt = tmp_git_repo
    before = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_git_repo, capture_output=True, text=True
    ).stdout.strip()
    (tmp_git_repo / "shared.txt").write_text("<<<<<<< HEAD\n")
    ctx.pre_commit_guard = lambda wt: "conflict markers remain in: shared.txt"

    exit_ = rounds_mod.commit_phase(ctx, 1)

    assert exit_ is not None and exit_.status == "failed"
    assert "conflict markers remain in: shared.txt" in (exit_.failure_reason or "")
    after = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_git_repo, capture_output=True, text=True
    ).stdout.strip()
    assert after == before and ctx.new_commit is None


def test_commit_phase_admits_a_round_one_no_change_claim_for_review(
    tmp_path: Path, tmp_git_repo: Path
) -> None:
    """PR #396 review (High): a round-1 coder that committed nothing because
    every injected finding needs no change has made a CLAIM about the PR
    head, and the loop's own gate + panel judge it there — never the coder
    alone. The entry's predicate admits the empty round: no exit, nothing
    committed, the unchanged head is the gated tree."""
    ctx, _calls = _artifact_ctx(tmp_path, collects=False, panel_passes=True)
    ctx.wt = tmp_git_repo
    ctx.gated_sha = None
    head = _head(tmp_git_repo)
    asked: list[int] = []

    def claim(round_no: int) -> bool:
        asked.append(round_no)
        return True

    ctx.no_change_claim = claim

    exit_ = rounds_mod.commit_phase(ctx, 1)

    assert exit_ is None
    assert asked == [1]
    assert ctx.new_commit is None and ctx.gated_sha == head
    assert _head(tmp_git_repo) == head  # nothing committed


def test_commit_phase_keeps_exit_c_when_no_change_is_not_claimed(
    tmp_path: Path, tmp_git_repo: Path
) -> None:
    # the predicate says the handoff does NOT claim no-change for every id
    # (a FIXED over an unchanged tree, an omitted id): exit C as before
    ctx, _calls = _artifact_ctx(tmp_path, collects=False, panel_passes=True)
    ctx.wt = tmp_git_repo
    ctx.gated_sha = None
    ctx.no_change_claim = lambda round_no: False

    exit_ = rounds_mod.commit_phase(ctx, 1)

    assert exit_ is not None and exit_.status == "failed"
    assert exit_.failure_reason == "round 1: coder produced no commit"
    assert ctx.gated_sha is None


def test_fast_gate_phase_runs_the_checks_on_an_admitted_unchanged_head(
    tmp_path: Path, tmp_git_repo: Path
) -> None:
    """The admitted no-change round has no commit and — in external mode — no
    intake check-set describing the head, so the fast checks run on the
    gated head itself: a required red check then holds the claim's approval
    through the floor like any round's."""
    ctx, _calls = _artifact_ctx(tmp_path, collects=False, panel_passes=True)
    ctx.wt = tmp_git_repo
    ctx.gated_sha = None
    ran: list[str] = []
    ctx.fast_checks = (
        Check(name="tests", command="make test", state="required", stage="fast"),
    )

    def fake_run_check_set(cfg, wt, sha, round_no, checks, ledger):
        ran.append(sha)
        return CheckSetResult(())

    ctx.services = rounds_mod.Services(
        run_turn=ctx.services.run_turn,
        sleep=ctx.services.sleep,
        start_container=ctx.services.start_container,
        stop_container=ctx.services.stop_container,
        run_check_set=fake_run_check_set,
    )
    ctx.no_change_claim = lambda round_no: True
    assert rounds_mod.commit_phase(ctx, 1) is None

    assert rounds_mod.fast_gate_phase(ctx, 1) is None

    assert ran == [_head(tmp_git_repo)]
    assert ctx.check_set is not None


def test_no_change_verdict_phase_ends_an_unapproved_validation_pass(
    tmp_path: Path, tmp_git_repo: Path
) -> None:
    """opus round 2 (Medium): the admitted no-change round is a VALIDATION
    pass — approval seals it above; a panel that rejects the claim (or a
    red required check the floor holds on) ends the run here with the
    rationale, never an entry to the fix loop over the whole PR."""
    ctx, _calls = _artifact_ctx(tmp_path, collects=False, panel_passes=True)
    ctx.wt = tmp_git_repo
    ctx.gated_sha = None
    ctx.no_change_claim = lambda round_no: True
    assert rounds_mod.commit_phase(ctx, 1) is None
    ctx.final_reviews = [_failed_outcome()]  # the panel filed f-101

    exit_ = rounds_mod.no_change_verdict_phase(ctx, 1)

    assert exit_ is not None and exit_.status == "failed"
    assert "rejected the coder's no-change claim" in exit_.failure_reason
    assert "correctness" in exit_.failure_reason and "overflow" in exit_.failure_reason

    # reviews passed but approval did not seal (a required check is red):
    # the floor's reason, not a phantom panel rejection
    ctx.final_reviews = [_passed()]
    exit_ = rounds_mod.no_change_verdict_phase(ctx, 1)
    assert exit_ is not None and "required check" in exit_.failure_reason

    # an ordinary round (a commit, or not the admitted round) is untouched
    ctx.no_change_round = False
    assert rounds_mod.no_change_verdict_phase(ctx, 1) is None
    assert rounds_mod.no_change_verdict_phase(ctx, 2) is None


def test_round1_coder_prompt_uses_the_entry_template_and_extra_slots(
    tmp_path: Path, tmp_git_repo: Path
) -> None:
    ctx, _calls = _artifact_ctx(tmp_path, collects=False, panel_passes=True)
    ctx.intake_reviews = []
    ctx.coder_init_template = "resolve_coder_init.md"
    ctx.coder_init_extra = {"conflict_brief": "CONFLICT-BRIEF-MARKER"}
    ctx.wt = tmp_git_repo
    ctx.base = rounds_mod.git.RangeBase(rounds_mod.git.base_sha(tmp_git_repo))

    prompt = rounds_mod.round1_coder_prompt(ctx)

    assert "CONFLICT-BRIEF-MARKER" in prompt
    assert "merge" in prompt.lower() and "conflict" in prompt.lower()


# --- slice B: infra escalation exits + coder-side salvage -------------------


def _coder_ctx(tmp_path: Path, wt: Path):
    ctx, _ = _artifact_ctx(tmp_path, collects=False, panel_passes=True)
    ctx.wt = wt
    ctx.final_reviews = [_failed_outcome()]
    return ctx


def _dead_turn(text: str) -> _engines.TurnResult:
    return _engines.TurnResult(
        exit_code=1,
        succeeded=False,
        completed=False,
        session_id="s",
        result_text=text,
        cost_usd=0.0,
        raw={"is_error": True},
        stderr="",
    )


_AUTH = "Failed to authenticate: OAuth session expired and could not be refreshed"
_ESCALATION = f"coder auth_failed persisted after 2 attempts: {_AUTH}"
_HOST_ACTION = "re-authenticate the agent CLI on the host, then complete the gate"


def test_panel_phase_infra_failure_ends_the_run_infra_failed(tmp_path: Path) -> None:
    ctx, _ = _artifact_ctx(tmp_path, collects=False, panel_passes=True)

    def escalating_panel(cfg, reviewers, **kw):
        return PanelRoundResult(
            round_reviews=[],
            cost=0.0,
            interrupted=False,
            resume_after=None,
            invalid_reviewer="correctness",  # additive with the escalation
            infra_failure=(
                "reviewer [correctness] auth_failed persisted after 2 attempts"
            ),
            infra_host_action=_HOST_ACTION,
        )

    ctx.run_panel_round = escalating_panel
    exit_ = rounds_mod.panel_phase(ctx, 3)
    assert exit_ is not None and exit_.status == "infra_failed"  # outranks failed
    assert exit_.failure_reason.startswith(
        "round 3: reviewer [correctness] auth_failed"
    )
    assert exit_.host_action == _HOST_ACTION


def test_artifact_pass_infra_failure_ends_the_run_infra_failed(tmp_path: Path) -> None:
    # The pass's consumer must not treat an escalating reviewer as "proceed":
    # on main the same turn ended the run `failed`; now it ends `infra_failed`.
    ctx, panel_calls = _artifact_ctx(tmp_path, collects=True, panel_passes=True)

    def escalating_panel(cfg, reviewers, **kw):
        panel_calls.append(kw)
        return PanelRoundResult(
            round_reviews=[_failed_outcome()],
            cost=0.0,
            interrupted=False,
            resume_after=None,
            invalid_reviewer="correctness",
            infra_failure=(
                "reviewer [correctness] transient_infra persisted after 3 attempts"
            ),
            infra_host_action=_HOST_ACTION,
        )

    ctx.run_panel_round = escalating_panel
    exit_ = rounds_mod.approval_phase(ctx, 1)

    assert [c.get("artifact_pass") for c in panel_calls] == [True]
    assert exit_ is not None and exit_.status == "infra_failed"
    assert "during the artifact-review pass" in exit_.failure_reason
    assert exit_.host_action == _HOST_ACTION


def test_coder_phase_escalation_ends_the_run_infra_failed(tmp_path: Path) -> None:
    ctx = _coder_ctx(tmp_path, tmp_path / "wt")
    ctx.wt.mkdir()
    ctx.turn_with_reactions = lambda *a, **kw: TurnAttempt(
        _dead_turn(_AUTH), False, 0.02, _ESCALATION, host_action=_HOST_ACTION
    )

    exit_ = rounds_mod.coder_phase(ctx, 2)

    assert exit_ is not None and exit_.status == "infra_failed"
    assert exit_.failure_reason == f"round 2: {_ESCALATION}"
    assert exit_.host_action == _HOST_ACTION
    assert ctx.coder_cost == pytest.approx(0.02)


def test_coder_phase_salvages_the_handoff_the_dying_attempt_wrote(
    tmp_path: Path,
) -> None:
    # The coder finished (wrote its handoff) and THEN the engine died on infra:
    # the work product is authoritative, as for reviewers (#298) — the round
    # proceeds to commit instead of ending infra_failed.
    ctx = _coder_ctx(tmp_path, tmp_path / "wt")
    ctx.wt.mkdir()
    ctx.config.handoff_dir.mkdir(parents=True, exist_ok=True)

    def dying_turn(*a, **kw):
        (ctx.config.handoff_dir / "round_02_coder_done.md").write_text("done")
        return TurnAttempt(_dead_turn(_AUTH), False, 0.02, _ESCALATION)

    ctx.turn_with_reactions = dying_turn
    assert rounds_mod.coder_phase(ctx, 2) is None


def test_coder_phase_does_not_salvage_a_preexisting_handoff(tmp_path: Path) -> None:
    ctx = _coder_ctx(tmp_path, tmp_path / "wt")
    ctx.wt.mkdir()
    ctx.config.handoff_dir.mkdir(parents=True, exist_ok=True)
    (ctx.config.handoff_dir / "round_02_coder_done.md").write_text("stale")
    ctx.turn_with_reactions = lambda *a, **kw: TurnAttempt(
        _dead_turn(_AUTH), False, 0.0, _ESCALATION
    )

    exit_ = rounds_mod.coder_phase(ctx, 2)
    assert exit_ is not None and exit_.status == "infra_failed"


def test_coder_phase_does_not_salvage_on_a_plain_agent_error(tmp_path: Path) -> None:
    # A crashed coder that still wrote a handoff is the existing failure path:
    # only an INFRA death (a retry class) qualifies for salvage.
    ctx = _coder_ctx(tmp_path, tmp_path / "wt")
    ctx.wt.mkdir()
    ctx.config.handoff_dir.mkdir(parents=True, exist_ok=True)

    def crashing_turn(*a, **kw):
        (ctx.config.handoff_dir / "round_02_coder_done.md").write_text("done")
        return TurnAttempt(_dead_turn("AssertionError: nope"), False, 0.0)

    ctx.turn_with_reactions = crashing_turn
    exit_ = rounds_mod.coder_phase(ctx, 2)
    assert exit_ is not None and exit_.status == "failed"
    assert "coder turn failed" in exit_.failure_reason


def test_handoff_nudge_runs_through_the_reaction_wrapper(
    tmp_path: Path, tmp_git_repo: Path
) -> None:
    # #114 nudge, slice B: the re-prompt is a turn like any other, so an infra
    # death during it gets the same retry/escalate treatment — it must go
    # through turn_with_reactions, never straight to services.run_turn.
    ctx = _coder_ctx(tmp_path, tmp_git_repo)
    ctx.coder_handoff_nudge = lambda r: f"you never wrote your handoff for round {r}"
    (tmp_git_repo / "work.txt").write_text("uncommitted work")
    prompts: list[str] = []

    def turn(*a, **kw):
        prompts.append(kw["prompt"])
        ok = _engines.TurnResult(
            exit_code=0,
            succeeded=True,
            completed=True,
            session_id="s",
            result_text="",
            cost_usd=0.01,
            raw={},
            stderr="",
        )
        if len(prompts) == 2:
            assert kw["resume"] is True
            (ctx.config.handoff_dir / "round_02_coder_done.md").write_text("done")
        return TurnAttempt(ok, False, 0.01)

    ctx.turn_with_reactions = turn
    ctx.services = rounds_mod.Services(
        run_turn=lambda **kw: (_ for _ in ()).throw(AssertionError("bypassed")),
        sleep=lambda s: None,
        start_container=lambda cmd: "cid",
        stop_container=lambda cid: None,
        run_check_set=lambda *a, **k: None,
    )
    ctx.config.handoff_dir.mkdir(parents=True, exist_ok=True)

    assert rounds_mod.coder_phase(ctx, 2) is None
    assert len(prompts) == 2 and "never wrote your handoff" in prompts[1]
    assert ctx.coder_cost == pytest.approx(0.02)


def test_nudge_escalation_ends_the_run_infra_failed(
    tmp_path: Path, tmp_git_repo: Path
) -> None:
    ctx = _coder_ctx(tmp_path, tmp_git_repo)
    (tmp_git_repo / "work.txt").write_text("uncommitted work")
    ctx.config.handoff_dir.mkdir(parents=True, exist_ok=True)
    n = {"calls": 0}

    def turn(*a, **kw):
        n["calls"] += 1
        if n["calls"] == 1:
            ok = _engines.TurnResult(
                exit_code=0,
                succeeded=True,
                completed=True,
                session_id="s",
                result_text="",
                cost_usd=0.0,
                raw={},
                stderr="",
            )
            return TurnAttempt(ok, False, 0.0)
        return TurnAttempt(
            _dead_turn(_AUTH), False, 0.0, _ESCALATION, host_action=_HOST_ACTION
        )

    ctx.turn_with_reactions = turn
    exit_ = rounds_mod.coder_phase(ctx, 2)
    assert exit_ is not None and exit_.status == "infra_failed"
    assert exit_.host_action == _HOST_ACTION
    assert n["calls"] == 2


def test_coder_phase_persists_the_wrappers_rebound_session(tmp_path: Path) -> None:
    ctx = _coder_ctx(tmp_path, tmp_path / "wt")
    ctx.wt.mkdir()
    ctx.config.handoff_dir.mkdir(parents=True, exist_ok=True)

    def turn(*a, **kw):
        (ctx.config.handoff_dir / "round_02_coder_done.md").write_text("done")
        ok = _engines.TurnResult(
            exit_code=0,
            succeeded=True,
            completed=True,
            session_id="",  # a codex resume may not re-announce the thread
            result_text="",
            cost_usd=0.0,
            raw={},
            stderr="",
        )
        return TurnAttempt(ok, False, 0.0, session_id="thread-minted")

    ctx.turn_with_reactions = turn
    assert rounds_mod.coder_phase(ctx, 2) is None
    assert ctx.coder_session == "thread-minted"


# ── decision_phase: the cheap escalation (9d5ebca6) ────────────────────


def _decision_ctx(tmp_path: Path):
    """A ctx whose sole reviewer's ledger holds one blocking finding — the
    coder's mark and the reviewer's answer are applied per test."""
    ctx, _ = _artifact_ctx(tmp_path, collects=False, panel_passes=True)
    ledger = ctx.reviewers[0].ledger
    ledger.apply_review(
        ReviewHandoff(
            status="FINDINGS",
            summary="",
            findings=[
                Finding(
                    finding_id="",
                    severity="critical",
                    status="open",
                    rationale="the finding post is not idempotent",
                )
            ],
        ),
        1,
    )
    return ctx, ledger


def _coder_decision(**kw) -> Finding:
    base: dict = dict(
        finding_id="f-001",
        severity="critical",
        status="needs-decision",
        coder_response="Lithos has no compare-and-set on task_update",
        decision_question="Accept an at-most-once marker, or block on Lithos?",
        decision_options="(a) accept the marker; (b) block — this story cannot land",
    )
    base.update(kw)
    return Finding(**base)


def _reviewer_keeps_open(**kw) -> ReviewHandoff:
    return ReviewHandoff(
        status="FINDINGS",
        summary="",
        findings=[
            Finding(finding_id="f-001", severity="critical", status="open", **kw)
        ],
    )


def test_decision_phase_stops_the_run_on_an_uncontested_decision(
    tmp_path: Path,
) -> None:
    ctx, ledger = _decision_ctx(tmp_path)
    ledger.record_coder_updates([_coder_decision()], 2)
    # the reviewer's one turn: it answers, and cannot show the finding is in
    # scope (an unanswered review is re-prompted — security/f-003)
    ledger.apply_review(_reviewer_keeps_open(decision_verdict="concede"), 2)

    exit_ = rounds_mod.decision_phase(ctx, 2)

    assert exit_ is not None
    assert exit_.status == "needs_decision"
    assert "correctness/f-001" in exit_.failure_reason
    # the reason line is loom-authored — it names the finding and where the
    # question is, never the coder's prose (security/f-002: this string is
    # published as an `@operator` GitHub comment by the needs-human notifier)
    assert "Accept an at-most-once marker" not in exit_.failure_reason
    assert "[ReviewDispute]" in exit_.failure_reason
    # security/f-004: it also says WHICH uncontested shape this was. This line
    # is `escalation_summary` — the `gates` CLI's `↳` and the GitHub
    # `@mention` — so it is the cheapest place to keep an unanswered decision
    # from reading like an adjudicated one, and the token stays loom-authored.
    assert "correctness/f-001 (reviewer: conceded)" in exit_.failure_reason
    # ... and it stops BEFORE the dispute guard would have paid two more rounds
    assert rounds_mod.deadlock_phase(ctx, 2) is None


def test_decision_phase_reason_names_a_decision_no_reviewer_answered(
    tmp_path: Path,
) -> None:
    # security/f-004, the other shape: the reviewer was re-prompted and still
    # said nothing, which escalates exactly as a concession does. The summary
    # must not claim an adjudication that never happened.
    ctx, ledger = _decision_ctx(tmp_path)
    ledger.record_coder_updates([_coder_decision()], 2)
    ledger.apply_review(_reviewer_keeps_open(), 2)  # committed with no answer

    exit_ = rounds_mod.decision_phase(ctx, 2)

    assert exit_ is not None and exit_.status == "needs_decision"
    assert "correctness/f-001 (reviewer: unanswered)" in exit_.failure_reason
    assert "conceded" not in exit_.failure_reason


def test_decision_phase_is_silent_when_the_reviewer_contests(tmp_path: Path) -> None:
    ctx, ledger = _decision_ctx(tmp_path)
    ledger.record_coder_updates([_coder_decision()], 2)
    ledger.apply_review(
        _reviewer_keeps_open(
            decision_verdict="contest",
            decision_contest="AC 4: 'exactly once per sweep'",
        ),
        2,
    )

    # a CITED contest is the one answer that stops it (correctness/f-001)
    assert rounds_mod.decision_phase(ctx, 2) is None
    # the ordinary guard takes over unchanged: a second blocked round deadlocks
    ledger.apply_review(_reviewer_keeps_open(), 3)
    assert rounds_mod.decision_phase(ctx, 3) is None
    deadlock = rounds_mod.deadlock_phase(ctx, 3)
    assert deadlock is not None and deadlock.status == "disputed"


def test_decision_phase_is_silent_without_a_decision(tmp_path: Path) -> None:
    ctx, ledger = _decision_ctx(tmp_path)
    ledger.record_coder_updates(
        [Finding(finding_id="f-001", severity="critical", status="disputed")], 2
    )
    ledger.apply_review(_reviewer_keeps_open(), 2)
    assert rounds_mod.decision_phase(ctx, 2) is None


def test_decision_phase_runs_before_the_deadlock_and_stall_guards() -> None:
    # Ordering is the whole point: the decision escalates at once, and a
    # contested one falls through to the guards that were there before.
    import inspect

    src = inspect.getsource(rounds_mod.run_round)
    assert (
        src.index("decision_phase")
        < src.index("deadlock_phase")
        < src.index("stall_phase")
    )


# --- what the checkpoint vouches for (security/f-003, f-005) -----------------


def _vouch_ctx(tmp_path: Path) -> rounds_mod.RoundContext:
    """A RoundContext aimed at `vouch_for_review`: only its config is read."""
    ctx, _calls = _artifact_ctx(tmp_path, collects=False, panel_passes=True)
    ctx.config.handoff_dir.mkdir(parents=True, exist_ok=True)
    return ctx


def _panel(**overrides: object) -> PanelRoundResult:
    fields: dict = {
        "round_reviews": [_passed()],
        "cost": 0.0,
        "interrupted": False,
        "resume_after": None,
        "invalid_reviewer": None,
    }
    fields.update(overrides)
    return PanelRoundResult(**fields)  # type: ignore[arg-type]


def test_a_reviewed_round_is_vouched_for_with_its_content(tmp_path: Path) -> None:
    """The resume reads both halves from here: the round, and what it said."""
    ctx = _vouch_ctx(tmp_path)
    handoff_file = ctx.config.handoff_dir / "round_02_review_correctness.md"
    handoff_file.write_text("## Status: LGTM\n## Summary\nfine\n")

    rounds_mod.vouch_for_review(ctx, 2, _panel())

    assert ctx.reviewed_round == 2
    digest = ctx.reviewed_digests[2]["correctness"]
    assert digest and ":" in digest  # size:sha256, handoff.file_fingerprint
    # …and it is the CONTENT that is vouched for, not just the name
    handoff_file.write_text("## Status: LGTM\n## Summary\nrewritten\n")
    from lithos_loom.plugins.story_develop.handoff import file_fingerprint

    assert file_fingerprint(handoff_file) != digest


@pytest.mark.parametrize(
    "panel_kwargs",
    [
        {"infra_failure": "reviewer auth_failed persisted"},  # died before writing
        {"invalid_reviewer": "correctness"},
        {"round_reviews": []},  # nothing ran
        {"round_reviews": [ReviewOutcome("correctness", "invalid", False, None)]},
    ],
)
def test_a_round_nothing_reviewed_is_not_vouched_for(
    tmp_path: Path, panel_kwargs: dict
) -> None:
    """security/f-005: the field must mean what its name says.

    `panel.round_reviews` carries an entry for every reviewer that RAN —
    including an `invalid` one and one that died of an infra failure — so
    recording the round on that alone made an unreviewed round "reviewed", and
    the resume's discovery fallback (where a coder plant is eligible) reachable
    for it.
    """
    ctx = _vouch_ctx(tmp_path)
    (ctx.config.handoff_dir / "round_02_review_correctness.md").write_text("x")

    rounds_mod.vouch_for_review(ctx, 2, _panel(**panel_kwargs))

    assert ctx.reviewed_round == 0 and ctx.reviewed_digests == {}


def test_a_review_with_no_handoff_on_disk_is_not_vouched_for(tmp_path: Path) -> None:
    # Nothing to fingerprint is nothing to verify later, so the round is not
    # offered to the resume at all.
    ctx = _vouch_ctx(tmp_path)

    rounds_mod.vouch_for_review(ctx, 2, _panel())

    assert ctx.reviewed_round == 0 and ctx.reviewed_digests == {}
