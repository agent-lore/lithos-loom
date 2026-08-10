"""Tests for expected->produced matching + run scoring (#183).

Pure, deterministic logic — the structured matcher is hermetic; the optional
LLM-judge fallback is injected as a callable so these tests never call an agent.
"""

from __future__ import annotations

from pathlib import Path

from lithos_loom.evals.review.case import Case, Expected, load_case
from lithos_loom.evals.review.match import (
    _structured_match,
    match_expected,
    review_incomplete,
    score_run,
)


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


def test_judge_confirms_and_returns_the_matched_finding() -> None:
    produced = [
        _finding("critical", ["cli/develop.py"], "exits before delivery", fid="f-007")
    ]
    seen = {}

    def judge(mechanism: str, findings: list[dict]) -> list[str]:
        seen["mechanism"] = mechanism
        seen["ids"] = [f["finding_id"] for f in findings]
        return ["f-007"]

    m = match_expected(_EXPECTED, produced, judge=judge)
    assert m.caught is True
    assert m.method == "judge"
    assert m.finding_id == "f-007"
    assert m.severity_correct is True
    # the judge saw the mechanism + every produced finding
    assert seen["mechanism"] == _EXPECTED.mechanism
    assert seen["ids"] == ["f-007"]


def test_judge_vetoes_a_false_structural_hit() -> None:
    # this finding structurally matches (file + keyword) but is a DIFFERENT
    # defect — the judge keyed on the mechanism rejects it. This is the FP fix.
    produced = [
        _finding("critical", ["cli/develop.py"], "the approved-state delivery summary")
    ]
    assert _structured_match(_EXPECTED, produced[0]) is True  # would falsely match

    m = match_expected(_EXPECTED, produced, judge=lambda mech, fs: [])
    assert m.caught is False
    assert m.method == "judge"


def test_judge_rescues_a_keyword_less_finding() -> None:
    # no keyword overlap (structural miss), but the judge affirms the mechanism
    produced = [_finding("critical", ["cli/develop.py"], "premature exit", fid="f-009")]
    assert _structured_match(_EXPECTED, produced[0]) is False

    m = match_expected(_EXPECTED, produced, judge=lambda mech, fs: ["f-009"])
    assert m.caught is True
    assert m.finding_id == "f-009"


def test_judge_match_below_min_severity_is_not_severity_correct() -> None:
    produced = [
        _finding("minor", ["cli/develop.py"], "exits before delivery", fid="f-1")
    ]
    m = match_expected(_EXPECTED, produced, judge=lambda mech, fs: ["f-1"])
    assert m.caught is True
    assert m.severity_correct is False


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


# ── incomplete-report detection (#182 A3) ─────────────────────────────────────


def _report_status(status: str, findings: list[dict] | None = None) -> dict:
    return {
        "reviewers": [
            {"name": "correctness", "status": status, "findings": findings or []}
        ]
    }


def test_review_incomplete_flags_a_crashed_reviewer() -> None:
    assert review_incomplete(_report_status("invalid")) is True


def test_review_incomplete_flags_a_not_run_reviewer() -> None:
    assert review_incomplete(_report_status("not-run")) is True


def test_review_incomplete_false_for_completed_verdicts() -> None:
    assert review_incomplete(_report_status("LGTM")) is False
    assert review_incomplete(_report_status("FINDINGS")) is False


def test_review_incomplete_false_for_statusless_reviewer() -> None:
    # robustness: a reviewer dict without a status (only test stubs) is treated
    # as complete — we key on the presence of an error status, not its absence.
    assert (
        review_incomplete({"reviewers": [{"name": "correctness", "findings": []}]})
        is False
    )


def test_review_incomplete_true_if_any_reviewer_invalid() -> None:
    report = {
        "reviewers": [
            {"name": "correctness", "status": "FINDINGS", "findings": []},
            {"name": "security", "status": "invalid", "findings": []},
        ]
    }
    assert review_incomplete(report) is True


def test_score_run_sets_incomplete() -> None:
    assert score_run(_case(), _report_status("invalid")).incomplete is True
    assert (
        score_run(
            _case(),
            _report_status(
                "FINDINGS",
                [_finding("critical", ["cli/develop.py"], "approved before delivery")],
            ),
        ).incomplete
        is False
    )


# ── shipped 289-symlink case: two directions, partial catches must not score ──
# as full catches (#292 review finding 2). Uses the REAL shipped fixture so the
# keyword sets stay honest: a representative single-direction finding matches
# exactly its own [[expected]] under the structured matcher.

_SHIPPED_CASES_DIR = Path(__file__).resolve().parents[1] / "evals" / "review" / "cases"

_READ_ONLY_RATIONALE = (
    "shutil.copytree in check_runner.py follows symlinks, so a symlink planted "
    "in the artifacts dir makes the host collector read files from outside the "
    "export into the handoff"
)
_WRITE_ONLY_RATIONALE = (
    "the destination in check_runner.py is agent-writable: an agent can "
    "pre-create the predictable round/check path as a symlink and the host "
    "writes through it onto arbitrary host files"
)
_SYMLINK_FILE = "src/lithos_loom/plugins/story_develop/check_runner.py"


def _symlink_case() -> Case:
    return load_case(_SHIPPED_CASES_DIR / "289-symlink-artifacts")


def _symlink_report(*rationales: str) -> dict:
    return {
        "reviewers": [
            {
                "name": "security",
                "status": "FINDINGS",
                "findings": [
                    _finding("critical", [_SYMLINK_FILE], r, fid=f"f-{i:03d}")
                    for i, r in enumerate(rationales, start=1)
                ],
            }
        ]
    }


def test_symlink_case_has_two_expected_defects() -> None:
    assert len(_symlink_case().expected) == 2


def test_symlink_read_only_finding_is_a_partial_miss() -> None:
    score = score_run(_symlink_case(), _symlink_report(_READ_ONLY_RATIONALE))
    assert score.caught is False
    assert [m.caught for m in score.matches] == [True, False]


def test_symlink_write_only_finding_is_a_partial_miss() -> None:
    score = score_run(_symlink_case(), _symlink_report(_WRITE_ONLY_RATIONALE))
    assert score.caught is False
    assert [m.caught for m in score.matches] == [False, True]


def test_symlink_both_directions_as_separate_findings_is_caught() -> None:
    score = score_run(
        _symlink_case(), _symlink_report(_READ_ONLY_RATIONALE, _WRITE_ONLY_RATIONALE)
    )
    assert score.caught is True


def test_symlink_one_comprehensive_finding_catches_both() -> None:
    score = score_run(
        _symlink_case(),
        _symlink_report(_READ_ONLY_RATIONALE + "; also " + _WRITE_ONLY_RATIONALE),
    )
    assert score.caught is True


# ── shipped lens33 case: the two confidence failure modes score separately ──
# (#293 review finding 1 — same partial-catch semantics as 289-symlink). The
# negative test also pins finding 2: an unrelated confidence-adjacent finding
# on the same file must not structurally match either expected.

_CONFIDENCE_FILE = "src/lithos_lens/knowledge_metadata.py"

_RANGE_ONLY_RATIONALE = (
    "_format_confidence formats any finite numeric, so confidence values "
    "outside the documented 0..1 range render misleading chips like 200%"
)
_CRASH_ONLY_RATIONALE = (
    "confidence: .nan passes the isinstance guard and round() blows up with "
    "ValueError inside template rendering, crashing the whole note page"
)
_UNRELATED_RATIONALE = (
    "the confidence chip has insufficient colour contrast against the "
    "surface background and should meet WCAG AA"
)


def _confidence_case() -> Case:
    return load_case(_SHIPPED_CASES_DIR / "lens33-confidence-crash")


def _confidence_report(*rationales: str) -> dict:
    return {
        "reviewers": [
            {
                "name": "correctness",
                "status": "FINDINGS",
                "findings": [
                    _finding("major", [_CONFIDENCE_FILE], r, fid=f"f-{i:03d}")
                    for i, r in enumerate(rationales, start=1)
                ],
            }
        ]
    }


def test_confidence_case_has_two_expected_defects() -> None:
    assert len(_confidence_case().expected) == 2


def test_confidence_range_only_finding_is_a_partial_miss() -> None:
    score = score_run(_confidence_case(), _confidence_report(_RANGE_ONLY_RATIONALE))
    assert score.caught is False
    assert [m.caught for m in score.matches] == [True, False]


def test_confidence_crash_only_finding_is_a_partial_miss() -> None:
    score = score_run(_confidence_case(), _confidence_report(_CRASH_ONLY_RATIONALE))
    assert score.caught is False
    assert [m.caught for m in score.matches] == [False, True]


def test_confidence_both_forms_as_separate_findings_is_caught() -> None:
    score = score_run(
        _confidence_case(),
        _confidence_report(_RANGE_ONLY_RATIONALE, _CRASH_ONLY_RATIONALE),
    )
    assert score.caught is True


def test_confidence_one_comprehensive_finding_catches_both() -> None:
    score = score_run(
        _confidence_case(),
        _confidence_report(_RANGE_ONLY_RATIONALE + "; also " + _CRASH_ONLY_RATIONALE),
    )
    assert score.caught is True


def test_confidence_unrelated_finding_matches_neither_expected() -> None:
    # No generic "confidence" keyword: a same-file finding about something else
    # entirely must not count in --no-judge mode (#293 review finding 2).
    score = score_run(_confidence_case(), _confidence_report(_UNRELATED_RATIONALE))
    assert score.caught is False
    assert [m.caught for m in score.matches] == [False, False]


# ── shipped lens34 case: false-truncation vs overlap-ordering score separately ─
# (same partial-catch semantics as 289-symlink / lens33 — a limit check fixes
# the false banner but not the elif ordering, and vice versa).

_FRONTIER_FILE = "src/lithos_lens/frontier.py"

_TRUNCATION_ONLY_RATIONALE = (
    "truncated is inferred from unclassified rows without ever checking that a "
    "response actually reached frontier_limit, so read-skew between the three "
    "independent reads renders a false truncation warning"
)
_OVERLAP_ONLY_RATIONALE = (
    "a task returned by both frontier responses is silently classified Ready "
    "because the elif chain tests the ready set first — a contested row is "
    "shown as workable with no warning"
)


def _frontier_case() -> Case:
    return load_case(_SHIPPED_CASES_DIR / "lens34-truncation")


def _frontier_report(*rationales: str) -> dict:
    return {
        "reviewers": [
            {
                "name": "correctness",
                "status": "FINDINGS",
                "findings": [
                    _finding("major", [_FRONTIER_FILE], r, fid=f"f-{i:03d}")
                    for i, r in enumerate(rationales, start=1)
                ],
            }
        ]
    }


def test_frontier_case_has_two_expected_defects() -> None:
    assert len(_frontier_case().expected) == 2


def test_frontier_truncation_only_finding_is_a_partial_miss() -> None:
    score = score_run(_frontier_case(), _frontier_report(_TRUNCATION_ONLY_RATIONALE))
    assert score.caught is False
    assert [m.caught for m in score.matches] == [True, False]


def test_frontier_overlap_only_finding_is_a_partial_miss() -> None:
    score = score_run(_frontier_case(), _frontier_report(_OVERLAP_ONLY_RATIONALE))
    assert score.caught is False
    assert [m.caught for m in score.matches] == [False, True]


def test_frontier_both_forms_as_separate_findings_is_caught() -> None:
    score = score_run(
        _frontier_case(),
        _frontier_report(_TRUNCATION_ONLY_RATIONALE, _OVERLAP_ONLY_RATIONALE),
    )
    assert score.caught is True


def test_frontier_one_comprehensive_finding_catches_both() -> None:
    score = score_run(
        _frontier_case(),
        _frontier_report(
            _TRUNCATION_ONLY_RATIONALE + "; worse, " + _OVERLAP_ONLY_RATIONALE
        ),
    )
    assert score.caught is True


def test_frontier_unrelated_finding_matches_neither_expected() -> None:
    score = score_run(
        _frontier_case(),
        _frontier_report(
            "the master-open read in frontier.py should paginate instead of "
            "loading every open task in one call"
        ),
    )
    assert score.caught is False
    assert [m.caught for m in score.matches] == [False, False]
