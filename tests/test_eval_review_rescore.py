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
    resolve_bar,
)

_FAILED = JudgeVerdict(status="failed", detail="TimeoutExpired: 300s")
_UNPARSED = JudgeVerdict(status="unparsed", reply="I think f-001 is wrong")

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


def _scripted(verdicts: list[JudgeVerdict]):
    """A judge answering *verdicts* in call order (sample-major, then repeat)."""
    calls = {"n": 0}

    def judge(_mech: str, findings: list[dict]) -> JudgeVerdict:
        i = calls["n"]
        calls["n"] += 1
        v = verdicts[min(i, len(verdicts) - 1)]
        # A confirming verdict names the findings it was actually shown.
        if v.status == "ok" and v.matched_ids == ("*",):
            return JudgeVerdict(matched_ids=tuple(f["finding_id"] for f in findings))
        return v

    return judge


_CONFIRM = JudgeVerdict(matched_ids=("*",))
_VETO = JudgeVerdict()


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


def test_load_rejects_reviewers_that_are_not_a_list(tmp_path: Path) -> None:
    root = _dir(tmp_path, [_report([_finding()])])
    (root / "180-attach-delivery" / "buggy-1.json").write_text('{"reviewers": 3}')
    with pytest.raises(RescoreError, match="'reviewers' is not a list"):
        load_report_dir(root)


def test_load_rejects_a_reviewer_that_is_not_an_object(tmp_path: Path) -> None:
    root = _dir(tmp_path, [_report([_finding()])])
    (root / "180-attach-delivery" / "buggy-1.json").write_text('{"reviewers": ["x"]}')
    with pytest.raises(RescoreError, match=r"reviewers\[0\] is not an object"):
        load_report_dir(root)


def test_load_rejects_findings_that_are_not_a_list(tmp_path: Path) -> None:
    root = _dir(tmp_path, [_report([_finding()])])
    (root / "180-attach-delivery" / "buggy-1.json").write_text(
        json.dumps({"reviewers": [{"status": "LGTM", "findings": "nope"}]})
    )
    with pytest.raises(RescoreError, match=r"findings is not a list"):
        load_report_dir(root)


def test_load_rejects_a_finding_that_is_not_an_object(tmp_path: Path) -> None:
    """The shape the scorer would crash on AFTER paying for earlier samples."""
    root = _dir(tmp_path, [_report([_finding()])])
    (root / "180-attach-delivery" / "buggy-1.json").write_text(
        json.dumps({"reviewers": [{"status": "FINDINGS", "findings": ["nope"]}]})
    )
    with pytest.raises(RescoreError, match=r"findings\[0\] is not an object"):
        load_report_dir(root)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("rationale", 7, "rationale is not a string"),
        ("files", "cli/develop.py", "files is not a list of strings"),
        ("files", [1], "files is not a list of strings"),
        ("finding_id", 1, "finding_id is not a string"),
        ("severity", "blocker", "severity must be one of"),
        ("severity", None, "severity must be one of"),
    ],
)
def test_load_rejects_a_finding_field_the_scorer_would_choke_on(
    tmp_path: Path, field: str, value: object, message: str
) -> None:
    bad = {**_finding(), field: value}
    root = _dir(tmp_path, [_report([bad])])
    with pytest.raises(RescoreError, match=message):
        load_report_dir(root)


@pytest.mark.parametrize("status", [["invalid"], "invald", None])
def test_load_rejects_a_status_that_is_not_a_real_reviewer_status(
    tmp_path: Path, status: object
) -> None:
    """`review_incomplete` keys on the PRESENCE of an error status, so a typo or
    an absent status reads as a clean review and inflates the valid denominator;
    an unhashable one raised TypeError after a judge request was already paid."""
    report = _report([_finding()])
    if status is None:
        report["reviewers"][0].pop("status")
    else:
        report["reviewers"][0]["status"] = status
    root = _dir(tmp_path, [report])
    with pytest.raises(RescoreError, match="status must be one of"):
        load_report_dir(root)


def test_load_accepts_every_canonical_reviewer_status(tmp_path: Path) -> None:
    """All 407 reviewers in the retained corpus carry one of these."""
    for i, status in enumerate(("LGTM", "FINDINGS", "invalid", "not-run")):
        report = _report([_finding()], status=status)
        assert load_report_dir(_dir(tmp_path / str(i), [report]))


def test_load_rejects_a_blocking_flag_that_is_not_a_boolean(tmp_path: Path) -> None:
    """`bool("false")` is True: this one never crashes, it silently inverts the
    noise instrumentation."""
    report = {**_report([_finding()]), "blocking": "false"}
    root = _dir(tmp_path, [report])
    with pytest.raises(RescoreError, match="blocking is not a boolean"):
        load_report_dir(root)


def test_load_accepts_a_report_with_no_blocking_key(tmp_path: Path) -> None:
    """Reports predating #310 have none — refusing them would strand the corpus."""
    report = _report([_finding()])
    report.pop("blocking")
    assert load_report_dir(_dir(tmp_path, [report]))


def test_load_rejects_a_summary_array_of_the_wrong_element_type(
    tmp_path: Path,
) -> None:
    """`[1] == [True]` in Python, so an int array would compare equal to a
    boolean one and report no drift where the corpus actually differs."""
    root = _dir(tmp_path, [_report([_finding()])], summary={"caught_per_sample": [1]})
    with pytest.raises(RescoreError, match="must be a list of bool"):
        load_report_dir(root)


def test_load_rejects_a_non_string_judge_status_array(tmp_path: Path) -> None:
    root = _dir(
        tmp_path, [_report([_finding()])], summary={"judge_status_per_sample": [True]}
    )
    with pytest.raises(RescoreError, match="must be a list of str"):
        load_report_dir(root)


def test_load_rejects_a_malformed_summary_array(tmp_path: Path) -> None:
    """Drift runs after the whole measurement is paid for — so it fails here."""
    root = _dir(
        tmp_path, [_report([_finding()])], summary={"caught_per_sample": "true"}
    )
    with pytest.raises(RescoreError, match="caught_per_sample is not a list"):
        load_report_dir(root)


@pytest.mark.parametrize("bar", [1.5, -0.1, "high", True, float("nan")])
def test_load_rejects_an_unusable_recorded_bar(tmp_path: Path, bar: object) -> None:
    root = _dir(tmp_path, [_report([_finding()])], summary={"bar": bar})
    with pytest.raises(RescoreError, match="bar must be a finite rate"):
        load_report_dir(root)


def test_load_validates_every_case_before_returning_any(tmp_path: Path) -> None:
    """A malformed report in the LAST case must not surface after the first
    case has already been judged and paid for."""
    root = _dir(tmp_path, [_report([_finding()])])
    other = root / "zz-later-case"
    other.mkdir()
    (other / "buggy-0.json").write_text(
        json.dumps({"reviewers": [{"status": "FINDINGS", "findings": [1]}]})
    )

    with pytest.raises(RescoreError, match="zz-later-case"):
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
    assert [(v.status, v.matched_ids) for v in site.verdicts] == [("ok", ())]


# ── a judge error is an ABSENCE of a verdict, never a stable veto ────────────


def test_an_errored_repeat_is_not_a_stable_veto(tmp_path: Path) -> None:
    """A timeout and a veto both match nothing — only the status separates them.

    Collapsing them would let a site the judge never answered be reported as a
    verdict it stuck to, which is the false confidence #307 exists to remove.
    """
    root = _dir(tmp_path, [_report([_finding()])])
    (reports,) = load_report_dir(root)
    scored = rescore_case(
        _case(), reports, bar=0.5, judge=_scripted([_VETO, _FAILED]), repeats=2
    )

    (site,) = scored.sites
    assert site.errored is True
    assert site.stable is False
    assert site.flipped is False  # nothing DISAGREED — there was only one answer
    assert scored.measured_sites == 0
    assert scored.unmeasured_sites == 1


def test_every_repeat_failing_is_unmeasured_not_stable(tmp_path: Path) -> None:
    root = _dir(tmp_path, [_report([_finding()])])
    (reports,) = load_report_dir(root)
    scored = rescore_case(
        _case(), reports, bar=0.5, judge=_scripted([_FAILED]), repeats=3
    )

    assert scored.judged_sites == 1
    assert scored.measured_sites == 0  # a 100%-stable reading here would be a lie
    assert scored.flipped_sites == 0
    assert scored.unmeasured_sites == 1


def test_an_unparsed_reply_counts_as_an_error_too(tmp_path: Path) -> None:
    root = _dir(tmp_path, [_report([_finding()])])
    (reports,) = load_report_dir(root)
    scored = rescore_case(
        _case(), reports, bar=0.5, judge=_scripted([_CONFIRM, _UNPARSED]), repeats=2
    )

    assert scored.sites[0].errored is True
    assert scored.measured_sites == 0


def test_a_flip_between_answering_repeats_survives_an_unrelated_error(
    tmp_path: Path,
) -> None:
    """A disagreement SEEN is a fact about the judge; a timeout must not erase it."""
    root = _dir(tmp_path, [_report([_finding()])])
    (reports,) = load_report_dir(root)
    scored = rescore_case(
        _case(),
        reports,
        bar=0.5,
        judge=_scripted([_CONFIRM, _VETO, _FAILED]),
        repeats=3,
    )

    assert scored.sites[0].flipped is True
    assert scored.flipped_sites == 1
    assert scored.measured_sites == 1
    assert scored.unmeasured_sites == 0


def test_the_spread_denominator_follows_each_repeats_valid_samples(
    tmp_path: Path,
) -> None:
    """A repeat where the judge errored has fewer scorable samples — holding K
    fixed would render that as a drop in catches instead of missing data."""
    root = _dir(tmp_path, [_report([_finding()]), _report([_finding()])])
    (reports,) = load_report_dir(root)
    # sample 0: confirm, then a timeout; sample 1: confirm twice
    scored = rescore_case(
        _case(),
        reports,
        bar=0.5,
        judge=_scripted([_CONFIRM, _FAILED, _CONFIRM, _CONFIRM]),
        repeats=2,
    )

    assert scored.catch_per_repeat == (2, 1)
    assert scored.valid_per_repeat == (2, 1)  # not (2, 2): one sample was excluded


def test_an_errored_repeat_keeps_its_status_and_detail_for_the_audit(
    tmp_path: Path,
) -> None:
    root = _dir(tmp_path, [_report([_finding()])])
    (reports,) = load_report_dir(root)
    scored = rescore_case(
        _case(), reports, bar=0.5, judge=_scripted([_FAILED]), repeats=1
    )

    (verdict,) = scored.sites[0].verdicts
    assert verdict.status == "failed"
    assert "TimeoutExpired" in verdict.detail


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


def test_drift_flags_a_sample_the_report_dir_gained(tmp_path: Path) -> None:
    """A positional zip skips a trailing sample entirely — a different corpus
    would read as an unchanged one."""
    root = _dir(tmp_path, [_report([_finding()]), _report([_finding()])])
    (reports,) = load_report_dir(root)
    scored = rescore_case(_case(), reports, bar=0.5, judge=None, repeats=1)

    drift = drift_vs_summary(scored.judged, {"caught_per_sample": [True]})
    assert drift["caught_per_sample"]["flipped"] == [1]


def test_drift_flags_a_sample_the_report_dir_lost(tmp_path: Path) -> None:
    root = _dir(tmp_path, [_report([_finding()])])
    (reports,) = load_report_dir(root)
    scored = rescore_case(_case(), reports, bar=0.5, judge=None, repeats=1)

    drift = drift_vs_summary(scored.judged, {"caught_per_sample": [True, True]})
    assert drift["caught_per_sample"]["flipped"] == [1]


def test_drift_compares_the_judge_status_arms(tmp_path: Path) -> None:
    """An original veto and a rescore timeout both leave `caught` False, so only
    the status says the sample stopped being scorable at all."""
    root = _dir(tmp_path, [_report([_finding()])])
    (reports,) = load_report_dir(root)
    scored = rescore_case(
        _case(), reports, bar=0.5, judge=_scripted([_FAILED]), repeats=1
    )

    drift = drift_vs_summary(
        scored.judged,
        {"caught_per_sample": [False], "judge_status_per_sample": ["ok"]},
    )
    assert drift["caught_per_sample"]["flipped"] == []
    assert drift["judge_status_per_sample"]["flipped"] == [0]


def test_drift_does_not_compare_judge_status_without_a_judge(tmp_path: Path) -> None:
    """A structured-only rescore has no judge answer; flagging every case would
    bury the comparisons that do mean something."""
    root = _dir(tmp_path, [_report([_finding()])])
    (reports,) = load_report_dir(root)
    scored = rescore_case(_case(), reports, bar=0.5, judge=None, repeats=1)

    drift = drift_vs_summary(
        scored.judged, {"judge_status_per_sample": ["ok"]}, compare_judge_status=False
    )
    assert drift["judge_status_per_sample"] is None


# ── the bar the run was scored at ────────────────────────────────────────────


def test_bar_comes_from_the_recorded_summary() -> None:
    assert resolve_bar({"bar": 0.6}, None) == (0.6, "summary")


def test_an_explicit_bar_overrides_the_recorded_one() -> None:
    assert resolve_bar({"bar": 0.6}, 0.9) == (0.9, "flag")


def test_bar_falls_back_to_the_default_and_says_so() -> None:
    assert resolve_bar({}, None) == (0.8, "default")
    assert resolve_bar(None, None) == (0.8, "default")


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


def test_load_rejects_non_canonical_finding_status(tmp_path: Path) -> None:
    # PR #342 review P2: actionable_findings keys catch-eligibility on the
    # status value — a malformed one (e.g. a list) compares unequal to
    # "out-of-scope" and would silently credit a deferral as a catch in a
    # PAID rescore. Optional for pre-status reports; canonical when present.
    ok = _finding()
    ok["status"] = "out-of-scope"
    load_report_dir(_dir(tmp_path, [_report([ok])]))  # canonical: accepted

    bad = _finding()
    bad["status"] = ["out-of-scope"]
    with pytest.raises(RescoreError, match="status"):
        load_report_dir(_dir(tmp_path / "b", [_report([bad])]))

    bogus = _finding()
    bogus["status"] = "bogus"
    with pytest.raises(RescoreError, match="status"):
        load_report_dir(_dir(tmp_path / "c", [_report([bogus])]))
