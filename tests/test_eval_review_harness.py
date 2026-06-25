"""Tests for the eval harness aggregation (#183).

``review_change`` (the live, host-only run) is injected as a callable returning
scripted report JSON, so the harness's rate aggregation is fully hermetic — no
agents, no docker.
"""

from __future__ import annotations

import pytest

from lithos_loom.evals.review import harness as harness_mod
from lithos_loom.evals.review.case import Case, Expected
from lithos_loom.evals.review.harness import _base_for, live_review, run_case
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
