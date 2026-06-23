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
    return {"reviewers": [{"name": "correctness", "findings": []}]}


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
