"""Tests for expected->produced matching + run scoring (#183).

Pure, deterministic logic — the structured matcher is hermetic; the optional
LLM-judge fallback is injected as a callable so these tests never call an agent.
"""

from __future__ import annotations

from lithos_loom.evals.review.case import Case, Expected
from lithos_loom.evals.review.match import match_expected, score_run


def _finding(
    severity: str, files, rationale, reviewer="correctness", fid="f-001"
) -> dict:
    return {
        "reviewer": reviewer,
        "severity": severity,
        "files": list(files),
        "rationale": rationale,
        "finding_id": fid,
    }


_EXPECTED = Expected(
    file="cli/develop.py",
    keywords=("delivery", "approved"),
    min_severity="critical",
    mechanism="attach exits on approved before delivery",
)


def test_file_and_keyword_hit_is_caught() -> None:
    produced = [
        _finding(
            "critical",
            ["src/lithos_loom/cli/develop.py:1790"],
            "attach exits on the approved verdict before delivery completes",
        )
    ]
    m = match_expected(_EXPECTED, produced)
    assert m.caught is True
    assert m.severity_correct is True
    assert m.method == "structured"
    assert m.finding_id == "f-001"


def test_wrong_file_is_a_miss() -> None:
    produced = [_finding("critical", ["src/other.py"], "approved delivery ordering")]
    m = match_expected(_EXPECTED, produced)
    assert m.caught is False


def test_keyword_miss_is_a_miss() -> None:
    produced = [_finding("critical", ["cli/develop.py"], "some unrelated nitpick")]
    m = match_expected(_EXPECTED, produced)
    assert m.caught is False


def test_below_min_severity_is_caught_but_not_severity_correct() -> None:
    produced = [
        _finding("minor", ["cli/develop.py"], "approved before delivery, minor nit")
    ]
    m = match_expected(_EXPECTED, produced)
    assert m.caught is True
    assert m.severity_correct is False


def test_keyword_match_in_rationale_without_file_in_files_list() -> None:
    # the file can be named in the rationale rather than the files list
    produced = [
        _finding("critical", [], "in cli/develop.py the approved delivery races")
    ]
    m = match_expected(_EXPECTED, produced)
    assert m.caught is True


def test_judge_fallback_catches_a_structural_miss() -> None:
    produced = [_finding("critical", ["cli/develop.py"], "totally different wording")]
    # structured miss (no keyword), but the judge affirms the mechanism
    calls = []

    def judge(mechanism: str, findings: list[dict]) -> bool:
        calls.append(mechanism)
        return True

    m = match_expected(_EXPECTED, produced, judge=judge)
    assert m.caught is True
    assert m.method == "judge"
    assert calls  # the judge was actually consulted


def test_no_judge_means_structural_only() -> None:
    produced = [_finding("critical", ["cli/develop.py"], "different wording")]
    m = match_expected(_EXPECTED, produced, judge=None)
    assert m.caught is False
    assert m.method == "none"


def _case() -> Case:
    return Case(
        id="c",
        description="",
        repo=".",
        base="a",
        head="b",
        acceptance_criteria="ac",
        personas=("correctness",),
        profile="standard",
        expected=(_EXPECTED,),
    )


def _report(findings: list[dict]) -> dict:
    return {"reviewers": [{"name": "correctness", "findings": findings}]}


def test_score_run_caught_when_every_expected_matches() -> None:
    report = _report(
        [_finding("critical", ["cli/develop.py"], "approved before delivery")]
    )
    score = score_run(_case(), report)
    assert score.caught is True
    assert score.severity_correct is True


def test_score_run_miss_when_an_expected_is_unmatched() -> None:
    report = _report([_finding("minor", ["other.py"], "nit")])
    score = score_run(_case(), report)
    assert score.caught is False
