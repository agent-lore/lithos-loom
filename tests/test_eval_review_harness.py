"""Tests for the eval harness aggregation (#183).

``review_change`` (the live, host-only run) is injected as a callable returning
scripted report JSON, so the harness's rate aggregation is fully hermetic — no
agents, no docker.
"""

from __future__ import annotations

import pytest

from lithos_loom.evals.review import harness as harness_mod
from lithos_loom.evals.review.case import Case, Expected
from lithos_loom.evals.review.harness import (
    _base_for,
    aggregate_case,
    live_review,
    run_case,
)
from lithos_loom.evals.review.match import JudgeVerdict, RunScore, score_run
from lithos_loom.plugins.story_develop.review_report import ReviewReport

_EXPECTED = Expected(
    file="cli/develop.py",
    keywords=("delivery",),
    min_severity="critical",
    mechanism="exits before delivery",
)


def _case(known_good: bool = True) -> Case:
    return Case(
        id="180-attach-delivery",
        description="",
        repo=".",
        base="base",
        head="buggy",
        acceptance_criteria="attach must wait for delivery",
        personas=("correctness",),
        profile="standard",
        expected=(_EXPECTED,),
        known_good_head="fixed" if known_good else None,
    )


def _caught(severity: str = "critical") -> dict:
    return {
        "reviewers": [
            {
                "name": "correctness",
                "status": "FINDINGS",
                "passed": False,
                "findings": [
                    {
                        "reviewer": "correctness",
                        "severity": severity,
                        "files": ["cli/develop.py"],
                        "rationale": "exits on approved before delivery",
                        "finding_id": "f-001",
                    }
                ],
            }
        ]
    }


def _clean() -> dict:
    return {
        "reviewers": [
            {"name": "correctness", "status": "LGTM", "passed": True, "findings": []}
        ]
    }


def _errored(status: str = "invalid") -> dict:
    # a crashed/short-circuited reviewer turn — always findings=[] (#182 A3)
    return {
        "reviewers": [
            {"name": "correctness", "status": status, "passed": False, "findings": []}
        ]
    }


def _report_for(marker: str) -> dict:
    return {"catch": _caught, "clean": _clean, "error": _errored}[marker]()


def _review_fn(buggy_pattern: list[bool], *, good_caught: bool = False):
    counters = {"buggy": 0, "good": 0}

    def review_fn(case: Case, head: str) -> dict:
        if head == case.head:
            i = counters["buggy"]
            counters["buggy"] += 1
            return (
                _caught() if buggy_pattern[min(i, len(buggy_pattern) - 1)] else _clean()
            )
        counters["good"] += 1
        return _caught() if good_caught else _clean()

    return review_fn


def _seq_review_fn(buggy: list[str], good: list[str] | None = None):
    """Map an explicit marker sequence ('catch'/'clean'/'error') to reports."""
    good = good or []
    counters = {"buggy": 0, "good": 0}

    def review_fn(case: Case, head: str) -> dict:
        if head == case.head:
            m = buggy[counters["buggy"]]
            counters["buggy"] += 1
            return _report_for(m)
        m = good[counters["good"]]
        counters["good"] += 1
        return _report_for(m)

    return review_fn


def test_catch_rate_over_k_runs() -> None:
    # 4 of 5 buggy runs surface the defect
    fn = _review_fn([True, True, True, True, False])
    result = run_case(_case(), k=5, bar=0.8, review_fn=fn)
    assert result.n == 5
    assert result.catch_rate == 0.8
    assert result.passed is True  # 0.8 >= 0.8 bar


def test_below_bar_does_not_pass() -> None:
    fn = _review_fn([True, False, False, False, False])
    result = run_case(_case(), k=5, bar=0.8, review_fn=fn)
    assert result.catch_rate == 0.2
    assert result.passed is False


def test_per_sample_booleans_and_catch_ci() -> None:
    # 3 of 5 buggy runs surface the defect -> the per-sample pattern is retained
    # and the Wilson CI brackets the 0.6 point estimate (not a bare percentage).
    fn = _review_fn([True, True, True, False, False])
    result = run_case(_case(known_good=False), k=5, review_fn=fn)
    assert result.caught_per_sample == (True, True, True, False, False)
    assert result.severity_per_sample == (True, True, True, False, False)
    assert result.catch_rate == 0.6
    lo, hi = result.catch_rate_ci
    assert 0.0 < lo < 0.6 < hi < 1.0


def test_fp_per_sample_and_ci() -> None:
    # the known-good head trips the matcher every time -> fp 100% with a CI whose
    # lower bound is < 1.0 even at 3/3 (small-n uncertainty is surfaced).
    fn = _review_fn([True, True, True], good_caught=True)
    result = run_case(_case(), k=3, review_fn=fn, known_good_runs=3)
    assert result.false_positive_per_sample == (True, True, True)
    assert result.false_positive_rate == 1.0
    flo, fhi = result.false_positive_rate_ci
    assert flo < 1.0 and fhi == 1.0


def test_no_known_good_means_empty_fp_samples() -> None:
    fn = _review_fn([True, True])
    result = run_case(_case(known_good=False), k=2, review_fn=fn)
    assert result.false_positive_per_sample == ()
    assert result.false_positive_rate_ci == (0.0, 0.0)


# ── errored-sample tracking (#182 A3): a crashed reviewer turn is not data ─────


def test_errored_samples_excluded_from_catch_denominator() -> None:
    # 3 catches + 2 crashed reviewer turns: the crashes are EXCLUDED, not counted
    # as misses, so catch-rate is 3/3 over valid samples (not a deflated 3/5).
    fn = _seq_review_fn(["catch", "catch", "catch", "error", "error"])
    result = run_case(_case(known_good=False), k=5, review_fn=fn)
    assert result.errored_per_sample == (False, False, False, True, True)
    assert result.caught_per_sample == (True, True, True, False, False)
    assert result.n - sum(result.errored_per_sample) == 3  # valid samples
    assert result.catch_rate == 1.0  # 3/3 valid, not 3/5
    _lo, hi = result.catch_rate_ci
    assert hi == 1.0  # CI computed over the 3 valid samples
    assert result.passed is True


def test_known_good_crashes_are_errored_not_clean_passes() -> None:
    # the 180 incident: 4 real LGTMs + 16 crashed reviewer turns on known-good ->
    # FP is 0/4 over valid (with 16 errored), NOT a fake "0/20".
    fn = _seq_review_fn(buggy=["catch"] * 20, good=["clean"] * 4 + ["error"] * 16)
    result = run_case(_case(), k=20, review_fn=fn, known_good_runs=20)
    assert result.catch_rate == 1.0
    assert sum(result.errored_per_sample) == 0  # buggy side healthy
    assert sum(result.false_positive_errored_per_sample) == 16
    assert 20 - sum(result.false_positive_errored_per_sample) == 4  # valid good
    assert result.false_positive_rate == 0.0  # 0 flagged / 4 valid


def test_all_errored_means_no_valid_samples_and_not_passed() -> None:
    fn = _seq_review_fn(["error"] * 5)
    result = run_case(_case(known_good=False), k=5, review_fn=fn)
    assert result.errored_per_sample == (True,) * 5
    assert result.catch_rate == 0.0  # no valid samples -> 0.0, not a divide-by-zero
    assert result.passed is False  # cannot pass with zero valid reviews


def test_caught_sample_with_a_crashed_second_reviewer_still_counts() -> None:
    # a real catch by one reviewer is trusted even if a panel peer crashed.
    def fn(case: Case, head: str) -> dict:
        return {
            "reviewers": [
                {
                    "name": "correctness",
                    "status": "FINDINGS",
                    "passed": False,
                    "findings": [
                        {
                            "reviewer": "correctness",
                            "severity": "critical",
                            "files": ["cli/develop.py"],
                            "rationale": "exits on approved before delivery",
                            "finding_id": "f-001",
                        }
                    ],
                },
                {
                    "name": "security",
                    "status": "invalid",
                    "passed": False,
                    "findings": [],
                },
            ]
        }

    result = run_case(_case(known_good=False), k=1, review_fn=fn)
    assert result.caught_per_sample == (True,)
    assert result.errored_per_sample == (False,)  # caught -> trusted, not errored
    assert result.catch_rate == 1.0


def test_severity_correctness_among_caught() -> None:
    # both runs catch, but report only `major` -> below the critical bar
    def fn(case, head):
        return _caught(severity="major") if head == case.head else _clean()

    result = run_case(_case(known_good=False), k=2, review_fn=fn)
    assert result.catch_rate == 1.0
    assert result.severity_correctness == 0.0  # caught, but never at critical


def test_false_positive_rate_from_known_good() -> None:
    # the known-good (fixed) head wrongly trips the matcher every time
    fn = _review_fn([True, True], good_caught=True)
    result = run_case(_case(), k=2, review_fn=fn, known_good_runs=2)
    assert result.false_positive_rate == 1.0


def test_no_known_good_means_zero_fp() -> None:
    fn = _review_fn([True, True])
    result = run_case(_case(known_good=False), k=2, review_fn=fn)
    assert result.false_positive_rate == 0.0


def test_report_sink_receives_every_run() -> None:
    fn = _review_fn([True, True])
    calls: list = []

    def sink(case_id: str, variant: str, i: int, report: dict) -> None:
        calls.append((case_id, variant, i))

    run_case(_case(), k=2, review_fn=fn, known_good_runs=2, report_sink=sink)
    assert sorted(calls) == [
        ("180-attach-delivery", "buggy", 0),
        ("180-attach-delivery", "buggy", 1),
        ("180-attach-delivery", "known-good", 0),
        ("180-attach-delivery", "known-good", 1),
    ]


def test_base_for_selects_defect_vs_known_good_base() -> None:
    case = Case(
        id="c",
        description="",
        repo=".",
        base="fix",
        head="buggy",
        acceptance_criteria="ac",
        personas=("correctness",),
        profile="standard",
        expected=(_EXPECTED,),
        known_good_head="fix",
        known_good_base="prefix",
    )
    # the buggy head diffs against the defect base; the known-good head against
    # its own (independent) base
    assert _base_for(case, "buggy") == "fix"
    assert _base_for(case, "fix") == "prefix"


def test_base_for_falls_back_to_base_without_known_good_base() -> None:
    case = _case()  # known_good_head="fixed", no known_good_base
    assert case.known_good_head is not None
    assert _base_for(case, case.known_good_head) == case.base


def _live_case() -> Case:
    return Case(
        id="c",
        description="",
        repo=".",
        base="b",
        head="h",
        acceptance_criteria="ac",
        personas=("correctness",),  # a real canonical persona
        profile="standard",
        expected=(_EXPECTED,),
    )


def _fake_report() -> ReviewReport:
    return ReviewReport(head_ref="x", base_sha="b", head_sha="h", profile="standard")


def test_live_review_cleans_up_its_work_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def fake_review_change(config, change, **kw):
        captured["work_dir"] = config.work_dir
        return _fake_report()

    monkeypatch.setattr(harness_mod, "review_change", fake_review_change)
    live_review(_live_case(), "h")
    # the per-sample temp work dir (run state, handoffs, transcripts) is removed
    assert not captured["work_dir"].exists()


def test_live_review_defaults_to_case_panel_and_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from lithos_loom.plugins.story_develop.personas import canonical_personas

    captured: dict = {}

    def fake_review_change(config, change, **kw):
        captured["config"] = config
        return _fake_report()

    monkeypatch.setattr(harness_mod, "review_change", fake_review_change)
    live_review(_live_case(), "h")
    assert captured["config"].reviewers == (canonical_personas()["correctness"],)
    assert captured["config"].review_profile == "standard"


def test_live_review_honours_explicit_panel_and_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The RH-7 seam: an already-resolved panel + profile override land on the
    # DevelopConfig verbatim (nothing re-resolves personas downstream).
    from dataclasses import replace

    from lithos_loom.plugins.story_develop.personas import canonical_personas

    captured: dict = {}

    def fake_review_change(config, change, **kw):
        captured["config"] = config
        return _fake_report()

    monkeypatch.setattr(harness_mod, "review_change", fake_review_change)
    spec = replace(canonical_personas()["security"], model="some-model")
    live_review(_live_case(), "h", reviewers=(spec,), profile="thorough")
    assert captured["config"].reviewers == (spec,)
    assert captured["config"].review_profile == "thorough"


def _artifact_case(tmp_path) -> Case:
    case_dir = tmp_path / "case"
    art = case_dir / "artifacts"
    art.mkdir(parents=True)
    (art / "page-800.png").write_bytes(b"\x89PNG...")
    return Case(
        id="c",
        description="",
        repo=".",
        base="b",
        head="h",
        acceptance_criteria="ac",
        personas=("correctness",),
        profile="standard",
        expected=(_EXPECTED,),
        case_dir=case_dir,
        artifacts_dir="artifacts",
        artifact_provenance="captured",
    )


def test_live_review_seeds_artifacts_and_runs_artifact_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    # RH-3: an artifact case measures the approval-hold artifact-review pass —
    # live_review seeds the checked-in captures into the run's artifacts dir
    # (where render_artifacts_note walks) and asks review-only for the
    # artifact pass INSTEAD of the diff panel.
    captured: dict = {}

    def fake_review_change(config, change, **kw):
        captured["artifact_only"] = kw.get("artifact_only")
        captured["seeded"] = sorted(
            p.name for p in config.artifacts_dir.rglob("*") if p.is_file()
        )
        return _fake_report()

    monkeypatch.setattr(harness_mod, "review_change", fake_review_change)
    live_review(_artifact_case(tmp_path), "h")
    assert captured["artifact_only"] is True
    # the seeder also stamps the provenance manifest (793edc9f) so the
    # artifact pass labels the seeded captures CURRENT for the head under
    # review rather than UNKNOWN
    assert captured["seeded"] == [".capture.json", "page-800.png"]


def test_live_review_seeds_the_variant_matching_the_head(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    # RH-1: the known-good run must review the FIXED render, not the buggy one —
    # otherwise the false-positive rate measures nothing.
    from dataclasses import replace

    case_dir = tmp_path / "case"
    art = case_dir / "artifacts"
    art.mkdir(parents=True)
    (art / "page-800.png").write_bytes(b"\x89PNG-buggy")
    kg = case_dir / "known-good-artifacts"
    kg.mkdir(parents=True)
    (kg / "page-800.png").write_bytes(b"\x89PNG-fixed")
    case = replace(
        _artifact_case(tmp_path / "unused"),
        case_dir=case_dir,
        known_good_head="kg",
        known_good_artifacts_dir="known-good-artifacts",
    )
    seeded: dict = {}

    def fake_review_change(config, change, **kw):
        dest = config.artifacts_dir / "round_01" / "seeded" / "page-800.png"
        seeded[change.head_sha] = dest.read_bytes()
        return _fake_report()

    monkeypatch.setattr(harness_mod, "review_change", fake_review_change)
    live_review(case, "h")
    live_review(case, "kg")
    assert seeded == {"h": b"\x89PNG-buggy", "kg": b"\x89PNG-fixed"}


def test_run_case_scores_a_paired_artifact_case_end_to_end(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    # PR #306 review: the per-head seeding is unit-tested, but nothing proved
    # the whole paired-artifact flow through run_case's aggregation — that the
    # catch arm and the FP arm review DIFFERENT captures and score separately.
    # The stub reports the defect only when it is shown the defect renders, so
    # a regression that seeds one variant for both arms shows up as fp == 1.0.
    from dataclasses import replace

    from lithos_loom.plugins.story_develop.review_report import (
        ReviewerReport,
        ReviewFinding,
    )

    case_dir = tmp_path / "case"
    (case_dir / "artifacts").mkdir(parents=True)
    (case_dir / "artifacts" / "page.png").write_bytes(b"\x89PNG-buggy")
    (case_dir / "known-good-artifacts").mkdir(parents=True)
    (case_dir / "known-good-artifacts" / "page.png").write_bytes(b"\x89PNG-fixed")
    case = replace(
        _artifact_case(tmp_path / "unused"),
        case_dir=case_dir,
        known_good_head="kg",
        known_good_artifacts_dir="known-good-artifacts",
        expected=(_EXPECTED,),
    )

    def fake_review_change(config, change, **kw):
        seeded = (
            config.artifacts_dir / "round_01" / "seeded" / "page.png"
        ).read_bytes()
        if seeded == b"\x89PNG-buggy":
            reviewers = [
                ReviewerReport(
                    name="correctness",
                    status="FINDINGS",
                    passed=False,
                    findings=[
                        ReviewFinding(
                            reviewer="correctness",
                            severity="critical",
                            files=["cli/develop.py"],
                            rationale="exits before delivery",
                        )
                    ],
                )
            ]
        else:
            reviewers = [ReviewerReport(name="correctness", status="LGTM", passed=True)]
        return ReviewReport(
            head_ref="x",
            base_sha="b",
            head_sha=change.head_sha,
            profile="standard",
            reviewers=reviewers,
        )

    monkeypatch.setattr(harness_mod, "review_change", fake_review_change)
    result = run_case(case, k=2, judge=None)

    assert result.catch_rate == 1.0
    assert result.false_positive_rate == 0.0
    assert result.caught_per_sample == (True, True)
    assert result.false_positive_per_sample == (False, False)


def test_live_review_without_artifacts_stays_on_the_diff_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    def fake_review_change(config, change, **kw):
        captured["kw"] = kw
        captured["artifacts_exists"] = config.artifacts_dir.exists()
        return _fake_report()

    monkeypatch.setattr(harness_mod, "review_change", fake_review_change)
    live_review(_live_case(), "h")
    # no artifact_only kwarg passed and nothing seeded — today's path exactly
    assert "artifact_only" not in captured["kw"]
    assert captured["artifacts_exists"] is False


def test_live_review_cleans_up_on_error(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def boom(config, change, **kw):
        captured["work_dir"] = config.work_dir
        raise RuntimeError("review failed")

    monkeypatch.setattr(harness_mod, "review_change", boom)
    with pytest.raises(RuntimeError):
        live_review(_live_case(), "h")
    assert not captured["work_dir"].exists()


# ── patch-based cases (#193): run_case materialises an ephemeral head ──────────


def _patch_case(tmp_git_repo, tmp_path):
    """A real patch-`Case` whose head is `mod.py: ok=False  # BUG` on a fresh base."""
    import subprocess

    from lithos_loom.runner import git as _git

    (tmp_git_repo / "mod.py").write_text("ok = True\n")
    subprocess.run(
        ["git", "add", "-A"], cwd=tmp_git_repo, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "-m", "seed"],
        cwd=tmp_git_repo,
        check=True,
        capture_output=True,
    )
    base = _git.base_sha(tmp_git_repo)
    (tmp_git_repo / "mod.py").write_text("ok = False  # BUG\n")
    diff = subprocess.run(
        ["git", "diff"], cwd=tmp_git_repo, capture_output=True, text=True
    ).stdout
    subprocess.run(
        ["git", "checkout", "--", "."],
        cwd=tmp_git_repo,
        check=True,
        capture_output=True,
    )
    case_dir = tmp_path / "case"
    case_dir.mkdir()
    (case_dir / "head.patch").write_text(diff)
    case = Case(
        id="194-x",
        description="",
        repo=str(tmp_git_repo),
        base=base,
        head="",
        acceptance_criteria="ac",
        personas=("correctness",),
        profile="standard",
        expected=(_EXPECTED,),
        head_patch="head.patch",
        case_dir=case_dir,
    )
    return case, base


def _worktree_list(repo):
    import subprocess

    return subprocess.run(
        ["git", "worktree", "list"], cwd=repo, capture_output=True, text=True
    ).stdout


def test_run_case_materialises_a_patch_head_end_to_end(tmp_git_repo, tmp_path) -> None:
    import subprocess

    case, base = _patch_case(tmp_git_repo, tmp_path)
    seen: dict = {}

    def capturing(c: Case, head: str) -> dict:
        seen["head"] = head
        seen["diff"] = subprocess.run(
            ["git", "diff", f"{base}..{head}"],
            cwd=tmp_git_repo,
            capture_output=True,
            text=True,
        ).stdout
        return _caught()

    result = run_case(case, k=1, review_fn=capturing)

    assert seen["head"] and seen["head"] != base  # the reviewer got an ephemeral sha
    assert "BUG" in seen["diff"]  # which resolves to base + the patch
    assert result.catch_rate == 1.0
    assert "eval-patch" not in _worktree_list(tmp_git_repo)  # build worktree cleaned up


def test_run_case_cleans_up_patch_head_even_on_review_error(
    tmp_git_repo, tmp_path
) -> None:
    case, _base = _patch_case(tmp_git_repo, tmp_path)

    def boom(c: Case, head: str) -> dict:
        raise RuntimeError("review blew up")

    with pytest.raises(RuntimeError):
        run_case(case, k=1, review_fn=boom)
    assert "eval-patch" not in _worktree_list(
        tmp_git_repo
    )  # cleaned up despite the error


# ── noise instrumentation (#310) ──────────────────────────────────────────────
#
# The false-positive rate asks one question of a known-good run: did it report
# the case's *expected defect*? Everything else it reported is invisible to that
# number — so a panel that files four unrelated findings and BLOCKS still scores
# a clean `fp 0/3`. These cover the instrumentation that makes the difference
# between two arms visible.


def _noisy(n: int = 3, *, blocking: bool = True) -> dict:
    """A run that reports *n* findings, none of them the expected defect."""
    return {
        "blocking": blocking,
        "reviewers": [
            {
                "name": "correctness",
                "status": "FINDINGS",
                "passed": False,
                "findings": [
                    {
                        "reviewer": "correctness",
                        "severity": "minor",
                        "files": ["style.css"],
                        "rationale": f"unrelated nit {i}",
                        "finding_id": f"n-{i}",
                    }
                    for i in range(n)
                ],
            }
        ],
    }


def _quiet() -> dict:
    return {
        "blocking": False,
        "reviewers": [
            {"name": "correctness", "status": "LGTM", "passed": True, "findings": []}
        ],
    }


def _variant_review_fn(buggy: list[dict], good: list[dict]):
    """Serve an explicit report per sample, per variant."""
    counters = {"buggy": 0, "good": 0}

    def review_fn(case: Case, head: str) -> dict:
        if head == case.head:
            r = buggy[counters["buggy"]]
            counters["buggy"] += 1
            return r
        r = good[counters["good"]]
        counters["good"] += 1
        return r

    return review_fn


def test_noise_is_invisible_to_the_false_positive_rate() -> None:
    # The measured #310 shape: every known-good run files unrelated findings and
    # blocks. The defect-specific FP rate is a clean 0/3 — and must stay that
    # way; the noise rate is what carries the signal.
    fn = _variant_review_fn([_caught()] * 3, [_noisy(1), _noisy(4), _noisy(1)])
    result = run_case(_case(), k=3, review_fn=fn, known_good_runs=3)
    assert result.false_positive_rate == 0.0
    assert result.noise_rate == 1.0
    assert result.known_good_findings_per_sample == (1, 4, 1)
    assert result.known_good_blocked_per_sample == (True, True, True)


def test_a_quiet_known_good_arm_scores_zero_noise() -> None:
    fn = _variant_review_fn([_caught()] * 3, [_quiet()] * 3)
    result = run_case(_case(), k=3, review_fn=fn, known_good_runs=3)
    assert result.noise_rate == 0.0
    assert result.known_good_findings_per_sample == (0, 0, 0)
    assert result.known_good_blocked_per_sample == (False, False, False)


def test_noise_rate_is_the_any_finding_fraction() -> None:
    fn = _variant_review_fn([_caught()] * 4, [_noisy(2), _quiet(), _quiet(), _noisy(1)])
    result = run_case(_case(), k=4, review_fn=fn, known_good_runs=4)
    assert result.noise_rate == 0.5
    lo, hi = result.noise_rate_ci
    assert 0.0 < lo < 0.5 < hi < 1.0


def test_findings_without_blocking_are_noise_but_not_a_block() -> None:
    # A minor finding under the reviewer's block threshold: noise, no held
    # approval. The two are recorded separately because they cost differently.
    fn = _variant_review_fn([_caught()] * 2, [_noisy(2, blocking=False)] * 2)
    result = run_case(_case(), k=2, review_fn=fn, known_good_runs=2)
    assert result.noise_rate == 1.0
    assert result.known_good_blocked_per_sample == (False, False)


def test_errored_known_good_samples_are_excluded_from_the_noise_denominator() -> None:
    # An incomplete panel produces no findings — counting it as "quiet" would
    # flatter the arm. Same denominator as the FP rate, so the two are directly
    # comparable side by side.
    fn = _variant_review_fn([_caught()] * 3, [_noisy(2), _errored(), _quiet()])
    result = run_case(_case(), k=3, review_fn=fn, known_good_runs=3)
    assert result.false_positive_errored_per_sample == (False, True, False)
    assert result.noise_rate == 0.5  # 1 noisy of 2 VALID, not of 3


def test_buggy_arm_carries_the_same_instrumentation() -> None:
    # A run that catches the defect AND invents three others is not the same
    # quality of catch — record it, but do not rate it (an extra finding on a
    # buggy head may be a real second defect).
    fn = _variant_review_fn([_caught(), _noisy(3)], [_quiet()] * 2)
    result = run_case(_case(), k=2, review_fn=fn, known_good_runs=2)
    assert result.findings_per_sample == (1, 3)
    assert result.blocked_per_sample == (False, True)


def test_no_known_good_means_no_noise_measurement() -> None:
    fn = _variant_review_fn([_caught()] * 2, [])
    result = run_case(_case(known_good=False), k=2, review_fn=fn)
    assert result.known_good_findings_per_sample == ()
    assert result.known_good_blocked_per_sample == ()
    assert result.noise_rate == 0.0
    assert result.noise_rate_ci == (0.0, 0.0)


# ── judge failure is its own signal (#307) ───────────────────────────────────


def _scripted_judge(statuses: list[str]):
    """A judge whose Nth call returns the Nth scripted status.

    Markers: ``ok`` confirms every finding (so a `_caught()` report scores as a
    catch), ``veto`` answers "none match", and ``failed`` / ``unparsed`` answer
    nothing at all — which is the point: those must not read as a review miss.
    """
    calls = {"n": 0}

    def judge(mechanism: str, findings: list[dict]) -> JudgeVerdict:
        i = calls["n"]
        calls["n"] += 1
        marker = statuses[min(i, len(statuses) - 1)]
        if marker == "veto":
            return JudgeVerdict()
        if marker != "ok":
            return JudgeVerdict(status=marker)  # type: ignore[arg-type]
        ids = tuple(str(f.get("finding_id", "")) for f in findings)
        return JudgeVerdict(matched_ids=ids)

    return judge


def test_judge_errored_samples_are_excluded_from_the_catch_denominator() -> None:
    """#307 defect 1: a judge timeout used to silently depress catch-rate.

    Three runs judged cleanly and caught; two could not be judged at all. The
    honest answer is 3/3, not 3/5.
    """
    fn = _review_fn([True] * 5)
    result = run_case(
        _case(known_good=False),
        k=5,
        review_fn=fn,
        judge=_scripted_judge(["ok", "ok", "ok", "failed", "failed"]),
    )

    assert result.catch_rate == 1.0
    assert result.judge_errored_per_sample == (False, False, False, True, True)
    assert result.errored_per_sample == (False,) * 5  # the REVIEWERS were fine
    assert result.excluded_per_sample == (False, False, False, True, True)


def test_judge_errors_are_reported_separately_from_reviewer_errors() -> None:
    fn = _seq_review_fn(["catch", "error", "catch"])
    result = run_case(
        _case(known_good=False),
        k=3,
        review_fn=fn,
        judge=_scripted_judge(["ok", "ok", "unparsed"]),
    )

    # sample 1 crashed the reviewer; sample 2 crashed the judge — different halves
    assert result.errored_per_sample == (False, True, False)
    assert result.judge_errored_per_sample == (False, False, True)
    assert result.excluded_per_sample == (False, True, True)
    assert result.catch_rate == 1.0  # the one valid sample caught it


def test_all_judge_errored_means_no_valid_samples_and_not_passed() -> None:
    fn = _review_fn([True] * 3)
    result = run_case(
        _case(known_good=False), k=3, review_fn=fn, judge=_scripted_judge(["failed"])
    )

    assert result.catch_rate == 0.0
    assert result.passed is False  # cannot pass on zero evidence
    assert all(result.judge_errored_per_sample)


def test_known_good_judge_errors_are_excluded_from_fp_and_noise() -> None:
    # 2 buggy (judged ok), then 2 known-good whose judge fails on the second
    fn = _review_fn([True, True], good_caught=False)
    result = run_case(
        _case(),
        k=2,
        review_fn=fn,
        judge=_scripted_judge(["ok", "ok", "ok", "failed"]),
    )

    assert result.false_positive_judge_errored_per_sample == (False, True)
    # fp and noise share one denominator, so they still describe the same runs
    assert result.false_positive_rate == 0.0
    assert result.noise_rate == 0.0


def test_structured_caught_per_sample_records_the_judge_free_answer() -> None:
    """The judge vetoes every sample; the structured matcher accepts every one."""
    fn = _review_fn([True] * 4)
    result = run_case(
        _case(known_good=False), k=4, review_fn=fn, judge=_scripted_judge(["veto"])
    )

    assert result.catch_rate == 0.0
    assert result.structured_caught_per_sample == (True,) * 4


def test_judge_sink_receives_every_scored_sample() -> None:
    seen: list[tuple[str, str, int, str]] = []

    def sink(case, variant, i, score):
        seen.append((case.id, variant, i, score.judge_status))

    run_case(
        _case(),
        k=2,
        review_fn=_review_fn([True, True]),
        judge=_scripted_judge(["ok"]),
        judge_sink=sink,
    )

    assert [(v, i) for _, v, i, _ in seen] == [
        ("buggy", 0),
        ("buggy", 1),
        ("known-good", 0),
        ("known-good", 1),
    ]
    assert all(case_id == "180-attach-delivery" for case_id, _, _, _ in seen)
    assert all(status == "ok" for *_, status in seen)


# ── aggregate_case: the arithmetic has one home (#307 slice 2) ───────────────


def test_aggregate_case_needs_no_case_no_git_and_no_review_fn() -> None:
    """It is a pure function of the per-sample scores.

    This is what lets `eval rescore` reuse it against stored reports: no head to
    materialise, no worktree, no container — just the scores.
    """
    buggy = [
        RunScore(caught=True, severity_correct=True),
        RunScore(caught=False, severity_correct=False),
    ]
    result = aggregate_case("c", buggy, k=2, bar=0.5)

    assert result.case_id == "c"
    assert result.catch_rate == 0.5
    assert result.passed is True
    assert result.caught_per_sample == (True, False)


def test_aggregate_case_reproduces_run_case_exactly() -> None:
    """The anti-drift guard: one implementation of the denominators and CIs.

    If rescore ever grew its own copy of the errored/valid rule, the Wilson
    intervals or the shared fp/noise denominator, a re-score would stop being
    comparable to the run it re-scores — silently.
    """
    fn = _variant_review_fn([_caught(), _clean(), _caught()], [_clean(), _noisy(2)])
    via_run = run_case(_case(), k=3, review_fn=fn, known_good_runs=2, bar=0.6)

    scored = [
        score_run(_case(), _caught()),
        score_run(_case(), _clean()),
        score_run(_case(), _caught()),
    ]
    kg = [score_run(_case(), _clean()), score_run(_case(), _noisy(2))]
    via_aggregate = aggregate_case(_case().id, scored, kg, k=3, bar=0.6)

    assert via_aggregate == via_run
