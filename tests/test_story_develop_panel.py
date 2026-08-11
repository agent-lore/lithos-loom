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
        config,
        budget,
        rstate,
        *,
        services,
        round_no,
        resume,
        prompt,
        timeout,
        base,
        review_file=None,
        reseed_prompt_override=None,
        skip_lifecycle_validation=False,
    ):
        name = rstate.spec.name
        calls.append(
            {
                "name": name,
                "round_no": round_no,
                "resume": resume,
                "prompt": prompt,
                "review_file": review_file,
            }
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


def test_collected_artifacts_reach_the_reviewer_prompt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # #283 slice 2: when the gate collected rendered-page artifacts, both the
    # round-1 and re-review prompts must tell the reviewer where to look —
    # using the in-container mount path.
    config = _config(tmp_path)
    shots = config.artifacts_dir / "round_01" / "repo-parity"
    shots.mkdir(parents=True)
    (shots / "note-320.png").write_text("png")
    reviewers = [_reviewer("correctness", tmp_path)]
    calls = _install_reviewer_stub(monkeypatch)

    _run(config, reviewers, round_no=1)
    _run(config, reviewers, round_no=2)

    for call in calls:
        assert "/workspace/.handoff/artifacts/round_01/repo-parity" in call["prompt"]
        assert "note-320.png" in call["prompt"]


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


# --- artifact pass: the artifact handoff controls the verdict (#291 re-review)


_ART_LGTM = "## Status: LGTM\n## Summary\nCode looked fine earlier.\n"
_ART_FINDINGS = (
    "## Status: FINDINGS\n## Summary\nVisual breakage.\n## Findings\n"
    "- finding_id:\n  severity: major\n  status: open\n"
    '  files: ["note-320.png"]\n  rationale: layout overflows at 320\n'
)


def _live_services(run_turn) -> Services:
    return Services(
        run_turn=run_turn,
        sleep=lambda s: None,
        start_container=lambda cmd: "cid",
        stop_container=lambda cid: None,
        run_check_set=lambda *a, **k: None,
    )


def _ok_turn(session_id: str) -> TurnResult:
    return TurnResult(
        exit_code=0,
        succeeded=True,
        session_id=session_id,
        result_text="done",
        cost_usd=0.01,
        raw={},
        stderr="",
    )


def _limited_turn() -> TurnResult:
    return TurnResult(
        exit_code=1,
        succeeded=False,
        session_id="",
        result_text="Claude AI usage limit reached|1750000000",
        cost_usd=0.001,
        raw={"is_error": True},
        stderr="",
    )


def test_artifact_pass_verdict_comes_from_the_artifact_handoff(
    tmp_path: Path,
) -> None:
    """#291 re-review (High): the round's regular handoff already holds a valid
    LGTM; the artifact pass writes FINDINGS to ITS OWN file. The outcome must
    carry the artifact verdict — reading the stale LGTM silently approved
    unreviewed screenshots. Runs the REAL reviewer-turn machinery (only
    ``Services.run_turn`` is faked)."""
    from lithos_loom.plugins.story_develop import handoff as handoff_mod

    config = _config(tmp_path)
    config.handoff_dir.mkdir(parents=True, exist_ok=True)
    (
        config.handoff_dir / handoff_mod.reviewer_handoff_name(1, "correctness")
    ).write_text(_ART_LGTM)
    shots = config.artifacts_dir / "round_01" / "repo-parity"
    shots.mkdir(parents=True)
    (shots / "note-320.png").write_text("png")

    def run_turn(*, container, prompt, session_id, resume, timeout, engine, **kw):
        (
            config.handoff_dir
            / handoff_mod.reviewer_handoff_name(1, "correctness_artifacts")
        ).write_text(_ART_FINDINGS)
        return _ok_turn(session_id)

    result = panel_mod.run_panel_round(
        config,
        [_reviewer("correctness", tmp_path)],
        wt=config.repo,
        base="0" * 40,
        round_no=1,
        check_set=None,
        gate_ledger=GateLedger(),
        budget=panel_mod.PauseBudget(0),
        reviewer_timeout=60,
        coder_summary="",
        services=_live_services(run_turn),
        artifact_pass=True,
    )

    out = result.round_reviews[0]
    assert out.status == "FINDINGS"
    assert out.passed is False
    assert out.findings and "320" in out.findings[0].rationale


def test_artifact_pass_survives_usage_limit_tool_switch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """#291 re-review (Medium): a usage-limited artifact reviewer switching
    engines must still be told to inspect the rendered pages — and its artifact
    handoff must control the result."""
    from lithos_loom.plugins.story_develop import handoff as handoff_mod

    config = _config(tmp_path)
    config.handoff_dir.mkdir(parents=True, exist_ok=True)
    (
        config.handoff_dir / handoff_mod.reviewer_handoff_name(1, "correctness")
    ).write_text(_ART_LGTM)
    shots = config.artifacts_dir / "round_01" / "repo-parity"
    shots.mkdir(parents=True)
    (shots / "note-320.png").write_text("png")

    rstate = _ReviewerState(
        ReviewerSpec(name="correctness", fallback_chain=("codex",)),
        "cid-correctness",
        [],
        tmp_path,
    )
    monkeypatch.setattr(panel_mod.containers, "stop_container", lambda c: None)
    monkeypatch.setattr(panel_mod.containers, "start_container", lambda cmd: "cid2")
    monkeypatch.setattr(panel_mod, "build_run_cmd", lambda *a, **k: ("cid2", ["cmd"]))

    prompts: list[str] = []

    def run_turn(*, container, prompt, session_id, resume, timeout, engine, **kw):
        prompts.append(prompt)
        if len(prompts) == 1:
            return _limited_turn()
        (
            config.handoff_dir
            / handoff_mod.reviewer_handoff_name(1, "correctness_artifacts")
        ).write_text(_ART_FINDINGS)
        return _ok_turn(session_id)

    result = panel_mod.run_panel_round(
        config,
        [rstate],
        wt=config.repo,
        base="0" * 40,
        round_no=1,
        check_set=None,
        gate_ledger=GateLedger(),
        budget=panel_mod.PauseBudget(0),
        reviewer_timeout=60,
        coder_summary="",
        services=_live_services(run_turn),
        artifact_pass=True,
    )

    # the replacement's prompt still carries the artifact listing + visual brief
    assert "/workspace/.handoff/artifacts/round_01/repo-parity" in prompts[1]
    assert "note-320.png" in prompts[1]
    out = result.round_reviews[0]
    assert out.status == "FINDINGS" and out.passed is False


_ART_NEW_MAJOR = (
    "## Status: FINDINGS\n## Summary\nVisual breakage.\n## Findings\n"
    "- finding_id:\n  severity: major\n  status: open\n"
    '  files: ["note-320.png"]\n  rationale: overflow at 320\n'
)


def _seed_minor_finding(rstate) -> str:
    """Populate the reviewer ledger like a passing-with-minor code review."""
    from lithos_loom.plugins.story_develop.handoff import ReviewHandoff

    applied = rstate.ledger.apply_review(
        ReviewHandoff(
            status="FINDINGS",
            summary="",
            findings=[
                Finding(
                    finding_id="",
                    severity="minor",
                    status="open",
                    files=["style.css:1"],
                    rationale="nit",
                )
            ],
        ),
        1,
    )
    return applied[0].finding_id


def test_artifact_lgtm_preserves_unrelated_code_findings(tmp_path: Path) -> None:
    """#291 round 3 (Medium): the artifact pass shares the reviewer's ledger;
    full-review LGTM semantics silently ACCEPTED the code review's open minor
    finding. Additive semantics: the visual LGTM approves, the code finding
    stays recorded and open."""
    from lithos_loom.plugins.story_develop import handoff as handoff_mod

    config = _config(tmp_path)
    config.handoff_dir.mkdir(parents=True, exist_ok=True)
    shots = config.artifacts_dir / "round_01" / "repo-parity"
    shots.mkdir(parents=True)
    (shots / "note-320.png").write_text("png")
    rstate = _reviewer("correctness", tmp_path)
    fid = _seed_minor_finding(rstate)

    def run_turn(*, container, prompt, session_id, resume, timeout, engine, **kw):
        (
            config.handoff_dir
            / handoff_mod.reviewer_handoff_name(1, "correctness_artifacts")
        ).write_text("## Status: LGTM\n## Summary\nPages look right.\n")
        return _ok_turn(session_id)

    result = panel_mod.run_panel_round(
        config,
        [rstate],
        wt=config.repo,
        base="0" * 40,
        round_no=1,
        check_set=None,
        gate_ledger=GateLedger(),
        budget=panel_mod.PauseBudget(0),
        reviewer_timeout=60,
        coder_summary="",
        services=_live_services(run_turn),
        artifact_pass=True,
    )

    assert result.round_reviews[0].passed is True  # visual verdict approves
    entry = rstate.ledger.entries[fid]
    assert entry.status == "open"  # the code finding SURVIVES, still recorded


def test_artifact_findings_append_without_invalid_handoff_retry(
    tmp_path: Path,
) -> None:
    """#291 round 3: a new visual finding while an earlier minor stays open
    must neither trip the open-id accounting validation (the artifact prompt
    never listed those ids) nor mutate the existing entry."""
    from lithos_loom.plugins.story_develop import handoff as handoff_mod

    config = _config(tmp_path)
    config.handoff_dir.mkdir(parents=True, exist_ok=True)
    shots = config.artifacts_dir / "round_01" / "repo-parity"
    shots.mkdir(parents=True)
    (shots / "note-320.png").write_text("png")
    rstate = _reviewer("correctness", tmp_path)
    fid = _seed_minor_finding(rstate)

    calls = {"n": 0}

    def run_turn(*, container, prompt, session_id, resume, timeout, engine, **kw):
        calls["n"] += 1
        (
            config.handoff_dir
            / handoff_mod.reviewer_handoff_name(1, "correctness_artifacts")
        ).write_text(_ART_NEW_MAJOR)
        return _ok_turn(session_id)

    result = panel_mod.run_panel_round(
        config,
        [rstate],
        wt=config.repo,
        base="0" * 40,
        round_no=1,
        check_set=None,
        gate_ledger=GateLedger(),
        budget=panel_mod.PauseBudget(0),
        reviewer_timeout=60,
        coder_summary="",
        services=_live_services(run_turn),
        artifact_pass=True,
    )

    assert calls["n"] == 1  # no invalid-handoff correction retry
    out = result.round_reviews[0]
    assert out.status == "FINDINGS" and out.passed is False  # approval held
    assert rstate.ledger.entries[fid].status == "open"  # untouched
    assert rstate.ledger.entries[fid].severity == "minor"
    new_ids = [k for k in rstate.ledger.entries if k != fid]
    assert len(new_ids) == 1  # the visual finding appended as NEW
    assert rstate.ledger.entries[new_ids[0]].severity == "major"


# --- #298 artifact-first salvage: a failed turn's valid handoff is trusted ---


def _infra_failed_turn() -> TurnResult:
    # A transport/infra death (classified agent_error, NOT usage-limited) —
    # the observed #298 class: auth 401 / stream disconnect after the review
    # work was already done.
    return TurnResult(
        exit_code=1,
        succeeded=False,
        session_id="",
        result_text="API Error: 401 OAuth access token has been revoked",
        cost_usd=0.005,
        raw={"is_error": True},
        stderr="",
    )


def _run_live_round(config, reviewers, run_turn, *, budget_seconds: int = 0):
    return panel_mod.run_panel_round(
        config,
        reviewers,
        wt=config.repo,
        base="0" * 40,
        round_no=1,
        check_set=None,
        gate_ledger=GateLedger(),
        budget=panel_mod.PauseBudget(budget_seconds),
        reviewer_timeout=60,
        coder_summary="",
        services=_live_services(run_turn),
    )


def test_failed_turn_with_valid_handoff_salvages_the_artifact(
    tmp_path: Path,
) -> None:
    """#298: the reviewer finished the review (valid handoff on disk) but the
    turn died on engine infra — the artifact is authoritative, not the exit
    code. Run 2bf85bc2's shape: green work discarded over a transport blip."""
    from lithos_loom.plugins.story_develop import handoff as handoff_mod

    config = _config(tmp_path)
    config.handoff_dir.mkdir(parents=True, exist_ok=True)

    def run_turn(*, container, prompt, session_id, resume, timeout, engine, **kw):
        (
            config.handoff_dir / handoff_mod.reviewer_handoff_name(1, "correctness")
        ).write_text(_ART_LGTM)
        return _infra_failed_turn()

    result = _run_live_round(config, [_reviewer("correctness", tmp_path)], run_turn)

    out = result.round_reviews[0]
    assert out.status == "LGTM" and out.passed is True
    assert result.invalid_reviewer is None
    assert result.interrupted is False
    # The failure evidence trail is still recorded for classification capture.
    assert list(config.failures_dir.glob("round_01_review-correctness*.json"))


def test_salvaged_findings_flow_into_the_ledger(tmp_path: Path) -> None:
    from lithos_loom.plugins.story_develop import handoff as handoff_mod

    config = _config(tmp_path)
    config.handoff_dir.mkdir(parents=True, exist_ok=True)
    rstate = _reviewer("correctness", tmp_path)

    def run_turn(*, container, prompt, session_id, resume, timeout, engine, **kw):
        (
            config.handoff_dir / handoff_mod.reviewer_handoff_name(1, "correctness")
        ).write_text(_ART_FINDINGS)
        return _infra_failed_turn()

    result = _run_live_round(config, [rstate], run_turn)

    out = result.round_reviews[0]
    assert out.status == "FINDINGS" and out.passed is False
    assert out.findings and "320" in out.findings[0].rationale
    assert len(rstate.ledger.entries) == 1  # salvaged findings hit the ledger


def test_failed_correction_retry_salvages_the_rewritten_handoff(
    tmp_path: Path,
) -> None:
    """The correction retry may rewrite a valid handoff and THEN die — the
    rewritten artifact must be re-read, not discarded with the retry."""
    from lithos_loom.plugins.story_develop import handoff as handoff_mod

    config = _config(tmp_path)
    config.handoff_dir.mkdir(parents=True, exist_ok=True)
    review_path = config.handoff_dir / handoff_mod.reviewer_handoff_name(
        1, "correctness"
    )
    calls = {"n": 0}

    def run_turn(*, container, prompt, session_id, resume, timeout, engine, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            review_path.write_text("not a handoff")
            return _ok_turn(session_id)
        review_path.write_text(_ART_LGTM)
        return _infra_failed_turn()

    result = _run_live_round(config, [_reviewer("correctness", tmp_path)], run_turn)

    assert calls["n"] == 2  # initial + correction retry, no more
    out = result.round_reviews[0]
    assert out.status == "LGTM" and out.passed is True
    assert result.invalid_reviewer is None


def test_failed_turn_without_valid_handoff_stays_invalid(tmp_path: Path) -> None:
    """Salvage never invents a verdict: a failed turn that left an unparseable
    file is still the terminal invalid outcome."""
    from lithos_loom.plugins.story_develop import handoff as handoff_mod

    config = _config(tmp_path)
    config.handoff_dir.mkdir(parents=True, exist_ok=True)

    def run_turn(*, container, prompt, session_id, resume, timeout, engine, **kw):
        (
            config.handoff_dir / handoff_mod.reviewer_handoff_name(1, "correctness")
        ).write_text("half-written garb")
        return _infra_failed_turn()

    result = _run_live_round(config, [_reviewer("correctness", tmp_path)], run_turn)

    assert result.round_reviews[0].status == "invalid"
    assert result.invalid_reviewer == "correctness"


def test_usage_limited_turn_is_not_salvaged(tmp_path: Path) -> None:
    """A usage-limited turn belongs to the T5 reaction (switch/pause), even if
    a valid handoff exists — salvaging would bypass the budgeted pause."""
    from lithos_loom.plugins.story_develop import handoff as handoff_mod

    config = _config(tmp_path)
    config.handoff_dir.mkdir(parents=True, exist_ok=True)

    def run_turn(*, container, prompt, session_id, resume, timeout, engine, **kw):
        (
            config.handoff_dir / handoff_mod.reviewer_handoff_name(1, "correctness")
        ).write_text(_ART_LGTM)
        return _limited_turn()

    # No fallback chain + zero pause budget: the reaction gives up immediately.
    result = _run_live_round(config, [_reviewer("correctness", tmp_path)], run_turn)

    assert result.interrupted is True
    assert result.round_reviews[0].status == "invalid"
