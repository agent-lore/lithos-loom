"""Tests for the triage-eval harness: scoring a batch verdict, aggregating K samples.

Hermetic: the triage function is injected. The live one (a read-only
container turn) is host-only and never runs here.
"""

from __future__ import annotations

from pathlib import Path

from lithos_loom.evals.triage.case import TriageCase, TriageFinding
from lithos_loom.evals.triage.harness import (
    TriageCaseResult,
    aggregate_triage,
    run_triage_case,
    score_sample,
)
from lithos_loom.plugins.story_develop.external_triage import TriageVerdicts


def _finding(
    fid: str, expected: str, refutation: tuple[str, ...] = ()
) -> TriageFinding:
    return TriageFinding(
        finding_id=fid,
        severity="major",
        files=("src/a.py:1",),
        rationale=f"claim {fid}",
        expected=expected,
        refutation_files=refutation,
        provenance="synthetic",
    )


def _case() -> TriageCase:
    return TriageCase(
        id="t",
        description="d",
        repo=".",
        sha="a" * 40,
        acceptance_criteria="ac",
        findings=(
            _finding("f-001", "proceed"),
            _finding("f-002", "proceed"),
            _finding("f-003", "reject", ("src/a.py",)),
            _finding("f-004", "reject", ("src/b.py", "docs/R.md")),
        ),
    )


def _verdicts(
    proceed: tuple[str, ...], rejections: dict[str, str], note: str = ""
) -> TriageVerdicts:
    return TriageVerdicts(
        proceed=proceed, rejections=rejections, cost_usd=0.1, note=note
    )


def test_perfect_sample_scores_every_finding_correct() -> None:
    score = score_sample(
        _case(),
        _verdicts(
            ("f-001", "f-002"),
            {"f-003": "src/a.py:12 guards it", "f-004": "docs/R.md:3 says otherwise"},
        ),
    )
    assert score.correct == {"f-001": True, "f-002": True, "f-003": True, "f-004": True}
    assert score.rejected_known_false == 2
    assert score.suppressed_known_true == 0
    assert score.errored is False


def test_reject_citing_a_file_outside_the_refutation_set_is_not_a_correct_reject() -> (
    None
):
    # The parser already requires a resolving file:line; the eval asks for
    # MORE — the rejection must cite the code that actually refutes it.
    score = score_sample(
        _case(),
        _verdicts(("f-001", "f-002", "f-004"), {"f-003": "src/zzz.py:1 unrelated"}),
    )
    assert score.correct["f-003"] is False
    assert score.rejected_known_false == 0


def test_rejecting_a_known_true_finding_is_over_suppression() -> None:
    score = score_sample(
        _case(),
        _verdicts(
            ("f-002",),
            {
                "f-001": "src/a.py:5 nope",
                "f-003": "src/a.py:12 x",
                "f-004": "src/b.py:1 y",
            },
        ),
    )
    assert score.correct["f-001"] is False
    assert score.suppressed_known_true == 1
    assert score.rejected_known_false == 2


def test_degraded_turn_is_errored_not_a_miss() -> None:
    score = score_sample(
        _case(),
        _verdicts(("f-001", "f-002", "f-003", "f-004"), {}, note="triage turn failed"),
    )
    assert score.errored is True
    assert score.note == "triage turn failed"


def test_aggregate_rates_over_valid_samples_only() -> None:
    case = _case()
    good = score_sample(
        case,
        _verdicts(
            ("f-001", "f-002"), {"f-003": "src/a.py:1 a", "f-004": "src/b.py:1 b"}
        ),
    )
    half = score_sample(
        case, _verdicts(("f-001", "f-002", "f-004"), {"f-003": "src/a.py:1 a"})
    )
    bad = score_sample(
        case, _verdicts(("f-002", "f-003", "f-004"), {"f-001": "src/a.py:1 wrong"})
    )
    errored = score_sample(
        case, _verdicts(("f-001", "f-002", "f-003", "f-004"), {}, note="x")
    )

    r = aggregate_triage(
        "t",
        [good, half, bad, errored],
        case=case,
        k=4,
        bar=0.8,
        max_over_suppression=0.0,
    )
    assert isinstance(r, TriageCaseResult)
    assert r.n == 4
    assert r.errored_per_sample == (False, False, False, True)
    # known-false rejected: 2 + 1 + 0 over 3 valid samples × 2 known-false
    assert (r.rejected_known_false, r.known_false_opportunities) == (3, 6)
    assert r.reject_rate == 0.5
    # known-true suppressed: 0 + 0 + 1 over 3 valid × 2 known-true
    assert (r.suppressed_known_true, r.known_true_opportunities) == (1, 6)
    assert abs(r.over_suppression_rate - 1 / 6) < 1e-9
    assert r.passed is False
    assert r.per_finding_correct == {"f-001": 2, "f-002": 3, "f-003": 2, "f-004": 1}
    lo, hi = r.reject_rate_ci
    assert 0.0 <= lo < 0.5 < hi <= 1.0


def test_passed_requires_bar_and_no_over_suppression_by_default() -> None:
    case = _case()
    perfect = score_sample(
        case,
        _verdicts(
            ("f-001", "f-002"), {"f-003": "src/a.py:1 a", "f-004": "docs/R.md:2 b"}
        ),
    )
    r = aggregate_triage(
        "t", [perfect] * 3, case=case, k=3, bar=0.8, max_over_suppression=0.0
    )
    assert r.passed is True

    one_suppressed = score_sample(
        case,
        _verdicts(
            ("f-002",),
            {
                "f-001": "src/a.py:1 no",
                "f-003": "src/a.py:1 a",
                "f-004": "docs/R.md:2 b",
            },
        ),
    )
    r2 = aggregate_triage(
        "t",
        [perfect, perfect, one_suppressed],
        case=case,
        k=3,
        bar=0.8,
        max_over_suppression=0.0,
    )
    assert r2.reject_rate == 1.0
    assert r2.passed is False  # over-suppression is the failure mode to watch
    r3 = aggregate_triage(
        "t",
        [perfect, perfect, one_suppressed],
        case=case,
        k=3,
        bar=0.8,
        max_over_suppression=0.5,
    )
    assert r3.passed is True


def test_all_errored_never_passes() -> None:
    case = _case()
    e = score_sample(
        case, _verdicts(("f-001", "f-002", "f-003", "f-004"), {}, note="x")
    )
    r = aggregate_triage("t", [e, e], case=case, k=2, bar=0.8, max_over_suppression=0.0)
    assert r.passed is False
    assert r.reject_rate == 0.0


def test_proceed_only_case_measures_over_suppression_alone() -> None:
    case = TriageCase(
        id="p",
        description="d",
        repo=".",
        sha="b" * 40,
        acceptance_criteria="ac",
        findings=(_finding("f-001", "proceed"),),
    )
    r = aggregate_triage(
        "p",
        [score_sample(case, _verdicts(("f-001",), {}))],
        case=case,
        k=1,
        bar=0.8,
        max_over_suppression=0.0,
    )
    assert r.known_false_opportunities == 0
    assert r.reject_rate == 0.0
    assert r.passed is True


def test_run_case_calls_triage_k_times_and_feeds_the_sink(tmp_path: Path) -> None:
    case = _case()
    calls: list[TriageCase] = []
    sunk: list[tuple[str, int, dict]] = []

    def fn(c: TriageCase) -> TriageVerdicts:
        calls.append(c)
        return _verdicts(
            ("f-001", "f-002"), {"f-003": "src/a.py:1 a", "f-004": "src/b.py:1 b"}
        )

    r = run_triage_case(
        case,
        k=3,
        bar=0.8,
        max_over_suppression=0.0,
        triage_fn=fn,
        sink=lambda cid, i, payload: sunk.append((cid, i, payload)),
    )
    assert len(calls) == 3 and all(c is case for c in calls)
    assert r.reject_rate == 1.0 and r.passed is True
    assert [(cid, i) for cid, i, _ in sunk] == [("t", 0), ("t", 1), ("t", 2)]
    payload = sunk[0][2]
    assert payload["rejections"] == {"f-003": "src/a.py:1 a", "f-004": "src/b.py:1 b"}
    assert payload["correct"]["f-003"] is True
    assert payload["cost_usd"] == 0.1
