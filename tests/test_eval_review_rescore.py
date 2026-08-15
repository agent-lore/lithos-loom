"""Tests for offline re-scoring of retained report dirs (#307).

The judge is always a scripted in-process callable, so nothing here ever reaches
a subprocess or an agent. That is the whole point of the module under test:
scoring is pure over `(case.expected, stored report JSON, judge)`, so it can be
exercised — and paid for — without running a reviewer.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lithos_loom.evals.review.case import Case, Expected, expected_fingerprint
from lithos_loom.evals.review.match import JudgeVerdict
from lithos_loom.evals.review.rescore import (
    RescoreError,
    drift_vs_summary,
    identity_of,
    judge_call_count,
    load_report_dir,
    rescore_case,
)

_EXPECTED = Expected(
    file="cli/develop.py",
    keywords=("delivery",),
    min_severity="critical",
    mechanism="exits before delivery",
)


def _case(expected: tuple[Expected, ...] = (_EXPECTED,)) -> Case:
    return Case(
        id="180-attach-delivery",
        description="",
        repo=".",
        base="base",
        head="buggy",
        acceptance_criteria="ac",
        personas=("correctness",),
        profile="standard",
        expected=expected,
    )


def _report(findings: list[dict] | None = None, status: str = "FINDINGS") -> dict:
    return {
        "blocking": bool(findings),
        "reviewers": [
            {
                "name": "correctness",
                "status": status,
                "passed": not findings,
                "findings": findings or [],
            }
        ],
    }


def _finding(fid: str = "f-001") -> dict:
    return {
        "reviewer": "correctness",
        "severity": "critical",
        "files": ["cli/develop.py"],
        "rationale": "exits on approved before delivery",
        "finding_id": fid,
    }


def _dir(
    tmp_path: Path,
    buggy: list[dict],
    known_good: list[dict] | None = None,
    summary: dict | None = None,
    case_id: str = "180-attach-delivery",
) -> Path:
    root = tmp_path / "reports"
    case_dir = root / case_id
    case_dir.mkdir(parents=True)
    for i, rep in enumerate(buggy):
        (case_dir / f"buggy-{i}.json").write_text(json.dumps(rep))
    for i, rep in enumerate(known_good or []):
        (case_dir / f"known-good-{i}.json").write_text(json.dumps(rep))
    if summary is not None:
        (case_dir / "summary.json").write_text(json.dumps(summary))
    return root


def _confirming(_mech: str, findings: list[dict]) -> JudgeVerdict:
    return JudgeVerdict(matched_ids=tuple(f["finding_id"] for f in findings))


def _vetoing(_mech: str, _findings: list[dict]) -> JudgeVerdict:
    return JudgeVerdict()


def _flipping(pattern: list[bool]):
    """A judge whose Nth call confirms or vetoes per *pattern* — a scripted flip."""
    calls = {"n": 0}

    def judge(_mech: str, findings: list[dict]) -> JudgeVerdict:
        i = calls["n"]
        calls["n"] += 1
        if pattern[min(i, len(pattern) - 1)]:
            return JudgeVerdict(matched_ids=tuple(f["finding_id"] for f in findings))
        return JudgeVerdict()

    return judge


# ── loading a report dir: fail loudly, never silently skip ───────────────────


def test_load_reads_every_sample_and_the_summary(tmp_path: Path) -> None:
    root = _dir(
        tmp_path,
        [_report([_finding()]), _report()],
        [_report()],
        summary={"case": "180-attach-delivery"},
    )
    (case,) = load_report_dir(root)

    assert case.case_id == "180-attach-delivery"
    assert [s.index for s in case.buggy] == [0, 1]
    assert [s.index for s in case.known_good] == [0]
    assert case.summary == {"case": "180-attach-delivery"}


def test_load_ignores_the_judge_sidecar_directory(tmp_path: Path) -> None:
    root = _dir(tmp_path, [_report([_finding()])])
    judge_dir = root / "180-attach-delivery" / "judge"
    judge_dir.mkdir()
    (judge_dir / "buggy-0.json").write_text("{}")

    (case,) = load_report_dir(root)
    assert len(case.buggy) == 1  # the sidecar is not a scoring input


def test_load_tolerates_its_own_rescore_json(tmp_path: Path) -> None:
    """Otherwise the command fails on its own output the second time it runs."""
    root = _dir(tmp_path, [_report([_finding()])])
    (root / "180-attach-delivery" / "rescore.json").write_text("{}")
    assert load_report_dir(root)


def test_load_rejects_an_unrecognised_filename(tmp_path: Path) -> None:
    root = _dir(tmp_path, [_report([_finding()])])
    (root / "180-attach-delivery" / "buggy-1.json.tmp").write_text("{}")
    with pytest.raises(RescoreError, match="unexpected file"):
        load_report_dir(root)


def test_load_rejects_malformed_json_and_names_the_file(tmp_path: Path) -> None:
    root = _dir(tmp_path, [_report([_finding()])])
    (root / "180-attach-delivery" / "buggy-1.json").write_text("{not json")
    with pytest.raises(RescoreError, match="buggy-1.json: not valid JSON"):
        load_report_dir(root)


def test_load_rejects_a_report_that_is_not_a_review_report(tmp_path: Path) -> None:
    root = _dir(tmp_path, [_report([_finding()])])
    (root / "180-attach-delivery" / "buggy-1.json").write_text('{"nope": 1}')
    with pytest.raises(RescoreError, match="not a ReviewReport"):
        load_report_dir(root)


def test_load_rejects_a_sample_index_gap(tmp_path: Path) -> None:
    """Recorded per-sample tuples are positional, so a gap misaligns drift."""
    root = _dir(tmp_path, [_report([_finding()])])
    (root / "180-attach-delivery" / "buggy-2.json").write_text(
        json.dumps(_report([_finding()]))
    )
    with pytest.raises(RescoreError, match="not contiguous"):
        load_report_dir(root)


def test_load_rejects_a_case_with_no_defect_arm(tmp_path: Path) -> None:
    root = tmp_path / "reports" / "180-attach-delivery"
    root.mkdir(parents=True)
    (root / "summary.json").write_text("{}")
    with pytest.raises(RescoreError, match="no buggy-N.json"):
        load_report_dir(tmp_path / "reports")


def test_load_rejects_a_missing_dir(tmp_path: Path) -> None:
    with pytest.raises(RescoreError, match="no report directory"):
        load_report_dir(tmp_path / "nope")


def test_load_rejects_an_empty_dir(tmp_path: Path) -> None:
    (tmp_path / "reports").mkdir()
    with pytest.raises(RescoreError, match="no eval reports"):
        load_report_dir(tmp_path / "reports")


def test_load_rejects_an_unknown_case_filter(tmp_path: Path) -> None:
    root = _dir(tmp_path, [_report([_finding()])])
    with pytest.raises(RescoreError, match="no case 'nope'"):
        load_report_dir(root, case_id="nope")


# ── cost is known before paying ──────────────────────────────────────────────


def test_call_count_skips_samples_with_no_findings(tmp_path: Path) -> None:
    """The judge short-circuits an empty findings list, so those cost nothing."""
    root = _dir(tmp_path, [_report([_finding()]), _report(), _report([_finding()])])
    reports = load_report_dir(root)

    assert judge_call_count(reports, {"180-attach-delivery": _case()}, 1) == 2


def test_call_count_scales_with_expecteds_and_repeats(tmp_path: Path) -> None:
    root = _dir(tmp_path, [_report([_finding()]), _report([_finding()])])
    reports = load_report_dir(root)
    two = _case((_EXPECTED, _EXPECTED))

    assert judge_call_count(reports, {"180-attach-delivery": two}, 3) == 12


# ── scoring ──────────────────────────────────────────────────────────────────


def test_rescore_reproduces_the_structured_universe(tmp_path: Path) -> None:
    root = _dir(tmp_path, [_report([_finding()]), _report()])
    (reports,) = load_report_dir(root)

    scored = rescore_case(_case(), reports, bar=0.5, judge=None, repeats=1)
    assert scored.judged.caught_per_sample == (True, False)
    assert scored.structured == scored.judged  # no judge: they are the same answer


def test_rescore_records_the_structured_counterfactual_beside_the_judged(
    tmp_path: Path,
) -> None:
    root = _dir(tmp_path, [_report([_finding()])] * 3)
    (reports,) = load_report_dir(root)

    scored = rescore_case(_case(), reports, bar=0.5, judge=_vetoing, repeats=1)
    assert scored.judged.catch_rate == 0.0  # the judge vetoed every sample
    assert scored.structured.caught_per_sample == (True, True, True)


def test_repeat_zero_is_authoritative(tmp_path: Path) -> None:
    """Measure variance; do not silently re-estimate while measuring it."""
    root = _dir(tmp_path, [_report([_finding()])])
    (reports,) = load_report_dir(root)
    # one site, three repeats: confirm, veto, veto
    scored = rescore_case(
        _case(), reports, bar=0.5, judge=_flipping([True, False, False]), repeats=3
    )

    assert scored.judged.caught_per_sample == (True,)  # repeat 0 wins
    assert scored.catch_per_repeat == (1, 0, 0)


def test_a_flipped_site_is_counted(tmp_path: Path) -> None:
    root = _dir(tmp_path, [_report([_finding()])])
    (reports,) = load_report_dir(root)
    scored = rescore_case(
        _case(), reports, bar=0.5, judge=_flipping([True, False]), repeats=2
    )

    assert scored.judged_sites == 1
    assert scored.flipped_sites == 1
    assert scored.sites[0].stable is False


def test_a_stable_site_is_not_counted_as_flipped(tmp_path: Path) -> None:
    root = _dir(tmp_path, [_report([_finding()])])
    (reports,) = load_report_dir(root)
    scored = rescore_case(_case(), reports, bar=0.5, judge=_confirming, repeats=3)

    assert scored.flipped_sites == 0
    assert scored.sites[0].stable is True


def test_sites_with_no_findings_are_excluded_from_the_stability_denominator(
    tmp_path: Path,
) -> None:
    """Counting free unanimity as stability would flatter a quiet arm."""
    root = _dir(tmp_path, [_report([_finding()]), _report(), _report()])
    (reports,) = load_report_dir(root)
    scored = rescore_case(_case(), reports, bar=0.5, judge=_confirming, repeats=2)

    assert len(scored.sites) == 3
    assert scored.judged_sites == 1


def test_a_veto_is_recorded_even_at_one_repeat(tmp_path: Path) -> None:
    """The site list IS the audit trail: the finding, and that nothing matched."""
    root = _dir(tmp_path, [_report([_finding()])])
    (reports,) = load_report_dir(root)
    scored = rescore_case(_case(), reports, bar=0.5, judge=_vetoing, repeats=1)

    (site,) = scored.sites
    assert site.produced_ids == ("f-001",)
    assert site.verdicts == (frozenset(),)


# ── drift, and the identity guard ────────────────────────────────────────────


def test_drift_names_the_flipped_sample_indices(tmp_path: Path) -> None:
    root = _dir(tmp_path, [_report([_finding()]), _report()])
    (reports,) = load_report_dir(root)
    scored = rescore_case(_case(), reports, bar=0.5, judge=None, repeats=1)

    drift = drift_vs_summary(scored.judged, {"caught_per_sample": [True, True]})
    assert drift["caught_per_sample"]["flipped"] == [1]


def test_drift_is_empty_when_the_answers_agree(tmp_path: Path) -> None:
    root = _dir(tmp_path, [_report([_finding()])])
    (reports,) = load_report_dir(root)
    scored = rescore_case(_case(), reports, bar=0.5, judge=None, repeats=1)

    drift = drift_vs_summary(scored.judged, {"caught_per_sample": [True]})
    assert drift["caught_per_sample"]["flipped"] == []


def test_drift_is_null_for_keys_the_recorded_summary_never_had(tmp_path: Path) -> None:
    """Most retained dirs predate most fields — absent must not read as agreed."""
    root = _dir(tmp_path, [_report([_finding()])])
    (reports,) = load_report_dir(root)
    scored = rescore_case(_case(), reports, bar=0.5, judge=None, repeats=1)

    drift = drift_vs_summary(scored.judged, {"caught_per_sample": [True]})
    assert drift["errored_per_sample"] is None


def test_no_summary_means_no_drift_block(tmp_path: Path) -> None:
    root = _dir(tmp_path, [_report([_finding()])])
    (reports,) = load_report_dir(root)
    scored = rescore_case(_case(), reports, bar=0.5, judge=None, repeats=1)
    assert drift_vs_summary(scored.judged, None) == {}


def test_identity_is_verified_when_the_fingerprint_matches() -> None:
    case = _case()
    summary = {"expected_fingerprint": expected_fingerprint(case)}
    assert identity_of(case, summary) == "verified"


def test_identity_is_changed_when_the_mechanism_changed() -> None:
    summary = {"expected_fingerprint": expected_fingerprint(_case())}
    other = _case(
        (
            Expected(
                file="cli/develop.py",
                keywords=("delivery",),
                min_severity="critical",
                mechanism="a DIFFERENT mechanism entirely",
            ),
        )
    )
    assert identity_of(other, summary) == "changed"


def test_identity_is_unverifiable_without_a_recorded_fingerprint() -> None:
    assert identity_of(_case(), {"case": "x"}) == "unverifiable"
    assert identity_of(_case(), None) == "unverifiable"


# ── the fingerprint covers the scorer's inputs and nothing else ──────────────


def test_fingerprint_is_stable_under_keyword_reorder() -> None:
    a = _case((Expected("f.py", ("x", "y"), "major", "m"),))
    b = _case((Expected("f.py", ("y", "x"), "major", "m"),))
    assert expected_fingerprint(a) == expected_fingerprint(b)


def test_fingerprint_is_stable_under_expected_reorder() -> None:
    one = Expected("a.py", ("x",), "major", "m1")
    two = Expected("b.py", ("y",), "major", "m2")
    assert expected_fingerprint(_case((one, two))) == expected_fingerprint(
        _case((two, one))
    )


def test_fingerprint_changes_when_a_mechanism_changes() -> None:
    a = _case((Expected("f.py", ("x",), "major", "m"),))
    b = _case((Expected("f.py", ("x",), "major", "m reworded"),))
    assert expected_fingerprint(a) != expected_fingerprint(b)


def test_fingerprint_ignores_what_the_scorer_never_reads() -> None:
    """ac.md / personas / profile change what the REVIEWER saw, which a rescore
    never revisits — that is #309's question, deliberately not this one's."""
    from dataclasses import replace

    base = _case()
    assert expected_fingerprint(base) == expected_fingerprint(
        replace(base, acceptance_criteria="totally different", personas=("security",))
    )
