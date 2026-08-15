"""Tests for expected->produced matching + run scoring (#183).

Pure, deterministic logic — the structured matcher is hermetic; the optional
LLM-judge fallback is injected as a callable so these tests never call an agent.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from lithos_loom.evals.review.case import Case, Expected, load_case
from lithos_loom.evals.review.match import (
    JudgeVerdict,
    _structured_match,
    finding_count,
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

    def judge(mechanism: str, findings: list[dict]) -> JudgeVerdict:
        seen["mechanism"] = mechanism
        seen["ids"] = [f["finding_id"] for f in findings]
        return JudgeVerdict(matched_ids=("f-007",))

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

    m = match_expected(_EXPECTED, produced, judge=lambda mech, fs: JudgeVerdict())
    assert m.caught is False
    assert m.method == "judge"


def test_judge_rescues_a_keyword_less_finding() -> None:
    # no keyword overlap (structural miss), but the judge affirms the mechanism
    produced = [_finding("critical", ["cli/develop.py"], "premature exit", fid="f-009")]
    assert _structured_match(_EXPECTED, produced[0]) is False

    verdict = JudgeVerdict(matched_ids=("f-009",))
    m = match_expected(_EXPECTED, produced, judge=lambda mech, fs: verdict)
    assert m.caught is True
    assert m.finding_id == "f-009"


def test_judge_match_below_min_severity_is_not_severity_correct() -> None:
    produced = [
        _finding("minor", ["cli/develop.py"], "exits before delivery", fid="f-1")
    ]
    verdict = JudgeVerdict(matched_ids=("f-1",))
    m = match_expected(_EXPECTED, produced, judge=lambda mech, fs: verdict)
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


# ── shipped split-expected cases: partial catches must not score as full ──────
# Every case whose escape has two independently-remediable defects gets the
# same five guarantees, against the REAL shipped fixture so the keyword sets
# stay honest: a representative single-defect finding matches exactly its own
# [[expected]]; two findings — or one comprehensive finding — catch both; an
# unrelated same-file finding matches neither. Table-driven since the five
# cases differ only in their representative wordings (and, for the cross-file
# cases, which file each defect lives in).

_SHIPPED_CASES_DIR = Path(__file__).resolve().parents[1] / "evals" / "review" / "cases"


@dataclass(frozen=True)
class _SplitCase:
    case_id: str
    file_a: str  # expected[0]'s file
    file_b: str  # expected[1]'s file
    only_a: str  # representative finding catching ONLY expected[0]
    only_b: str  # representative finding catching ONLY expected[1]
    unrelated_a: str  # finding on file_a that must match NEITHER expected
    unrelated_b: str  # finding on file_b that must match NEITHER expected


_CHECK_RUNNER = "src/lithos_loom/plugins/story_develop/check_runner.py"
_KNOWLEDGE_METADATA = "src/lithos_lens/knowledge_metadata.py"
_FRONTIER = "src/lithos_lens/frontier.py"
_LENS_TASKS = "src/lithos_lens/tasks.py"
_LENS_CLIENT = "src/lithos_lens/lithos_client.py"
_LENS_KNOWLEDGE = "src/lithos_lens/knowledge.py"

_SPLIT_CASES = (
    _SplitCase(
        case_id="289-symlink-artifacts",
        file_a=_CHECK_RUNNER,
        file_b=_CHECK_RUNNER,
        only_a=(
            "shutil.copytree in check_runner.py follows symlinks, so a symlink "
            "planted in the artifacts dir makes the host collector read files "
            "from outside the export into the handoff"
        ),
        only_b=(
            "the destination in check_runner.py is agent-writable: an agent can "
            "pre-create the predictable round/check path as a symlink and the "
            "host writes through it onto arbitrary host files"
        ),
        unrelated_a=(
            "the collector in check_runner.py logs at warning level for every "
            "skipped file which will be noisy on large artifact sets"
        ),
        unrelated_b=(
            "check_runner.py rereads result.json once per round instead of "
            "caching the parsed document"
        ),
    ),
    _SplitCase(
        case_id="lens33-confidence-crash",
        file_a=_KNOWLEDGE_METADATA,
        file_b=_KNOWLEDGE_METADATA,
        only_a=(
            "_format_confidence formats any finite numeric, so confidence "
            "values outside the documented 0..1 range render misleading chips "
            "like 200%"
        ),
        only_b=(
            "confidence: .nan passes the isinstance guard and round() blows up "
            "with ValueError inside template rendering, crashing the whole "
            "note page"
        ),
        unrelated_a=(
            "the confidence chip has insufficient colour contrast against the "
            "surface background and should meet WCAG AA"
        ),
        unrelated_b=(
            "the metadata table sorts keys alphabetically so confidence sits "
            "below tags; consider preserving source order"
        ),
    ),
    _SplitCase(
        case_id="lens34-truncation",
        file_a=_FRONTIER,
        file_b=_FRONTIER,
        only_a=(
            "truncated is inferred from unclassified rows without ever checking "
            "that a response actually reached frontier_limit, so read-skew "
            "between the three independent reads renders a false truncation "
            "warning"
        ),
        only_b=(
            "a task returned by both frontier responses is silently classified "
            "Ready because the elif chain tests the ready set first — a "
            "contested row is shown as workable with no warning"
        ),
        unrelated_a=(
            "the master-open read in frontier.py should paginate instead of "
            "loading every open task in one call"
        ),
        unrelated_b=(
            "the ready table re-renders fully on every SSE event and should "
            "diff rows instead"
        ),
    ),
    _SplitCase(
        case_id="lens23-blocker-contract",
        file_a=_LENS_TASKS,
        file_b=_LENS_CLIENT,
        only_a=(
            "BlockerRecord in tasks.py models title and gate_type on blocker "
            "rows — fields lithos_task_blocked never returns; the real payload "
            "carries type and message, so blockers render hollow against a "
            "real server"
        ),
        only_b=(
            "the client omits with_claims when False — it is only sent when "
            "True — but upstream defaults it to true, so lens's False default "
            "is silently inverted"
        ),
        unrelated_a=(
            "the normalizer in tasks.py reparses ISO timestamps per row and "
            "should cache the parse"
        ),
        # The review-round hazard: an omitted-default finding on the client
        # must not satisfy the with_claims expected now that the generic
        # "default"/"omit" keywords are gone.
        unrelated_b=(
            "the client omits a request timeout so a hung server blocks the "
            "TUI indefinitely; the default httpx timeout should be overridden"
        ),
    ),
    _SplitCase(
        case_id="lens26-related-contract",
        file_a=_LENS_CLIENT,
        file_b=_LENS_KNOWLEDGE,
        only_a=(
            "the related call in lithos_client.py sends agent_id, an argument "
            "lithos_related does not accept, so the server rejects every call "
            "outright"
        ),
        only_b=(
            "normalize_related reads flat links and backlinks keys but the real "
            "payload nests rows under links.outgoing and links.incoming, so "
            "every section renders empty"
        ),
        unrelated_a=(
            "the title fan-out in lithos_client.py awaits reads sequentially "
            "and should batch them"
        ),
        # The review-round hazard: an incoming-title observation on
        # knowledge.py must not satisfy the nested-shape expected now that the
        # bare "incoming" keyword is the dotted links.incoming form.
        unrelated_b=(
            "normalize_related repeats the incoming-title lookup for every "
            "row sequentially; results should be memoized per id"
        ),
    ),
)

_SPLIT_IDS = [c.case_id for c in _SPLIT_CASES]


def _split_report(*findings: tuple[str, str]) -> dict:
    """A one-reviewer report; each finding is a (file, rationale) pair."""
    return {
        "reviewers": [
            {
                "name": "correctness",
                "status": "FINDINGS",
                "findings": [
                    _finding("critical", [file], rationale, fid=f"f-{i:03d}")
                    for i, (file, rationale) in enumerate(findings, start=1)
                ],
            }
        ]
    }


def _split_case(spec: _SplitCase) -> Case:
    return load_case(_SHIPPED_CASES_DIR / spec.case_id)


@pytest.mark.parametrize("spec", _SPLIT_CASES, ids=_SPLIT_IDS)
def test_split_case_has_two_expected_defects(spec: _SplitCase) -> None:
    assert len(_split_case(spec).expected) == 2


@pytest.mark.parametrize("spec", _SPLIT_CASES, ids=_SPLIT_IDS)
def test_split_case_first_defect_alone_is_a_partial_miss(spec: _SplitCase) -> None:
    score = score_run(_split_case(spec), _split_report((spec.file_a, spec.only_a)))
    assert score.caught is False
    assert [m.caught for m in score.matches] == [True, False]


@pytest.mark.parametrize("spec", _SPLIT_CASES, ids=_SPLIT_IDS)
def test_split_case_second_defect_alone_is_a_partial_miss(spec: _SplitCase) -> None:
    score = score_run(_split_case(spec), _split_report((spec.file_b, spec.only_b)))
    assert score.caught is False
    assert [m.caught for m in score.matches] == [False, True]


@pytest.mark.parametrize("spec", _SPLIT_CASES, ids=_SPLIT_IDS)
def test_split_case_both_defects_as_separate_findings_is_caught(
    spec: _SplitCase,
) -> None:
    score = score_run(
        _split_case(spec),
        _split_report((spec.file_a, spec.only_a), (spec.file_b, spec.only_b)),
    )
    assert score.caught is True


@pytest.mark.parametrize("spec", _SPLIT_CASES, ids=_SPLIT_IDS)
def test_split_case_one_comprehensive_finding_catches_both(
    spec: _SplitCase,
) -> None:
    # One finding whose files list spans both sites and whose rationale covers
    # both mechanisms satisfies both expecteds.
    report = {
        "reviewers": [
            {
                "name": "correctness",
                "status": "FINDINGS",
                "findings": [
                    {
                        "reviewer": "correctness",
                        "severity": "critical",
                        "files": [spec.file_a, spec.file_b],
                        "rationale": spec.only_a + "; worse, " + spec.only_b,
                        "finding_id": "f-001",
                    }
                ],
            }
        ]
    }
    assert score_run(_split_case(spec), report).caught is True


@pytest.mark.parametrize("spec", _SPLIT_CASES, ids=_SPLIT_IDS)
def test_split_case_unrelated_finding_matches_neither_expected(
    spec: _SplitCase,
) -> None:
    # Both defect files get a negative: for the cross-file cases the second
    # expected's keyword precision is otherwise never exercised.
    for file, rationale in (
        (spec.file_a, spec.unrelated_a),
        (spec.file_b, spec.unrelated_b),
    ):
        score = score_run(_split_case(spec), _split_report((file, rationale)))
        assert score.caught is False
        assert [m.caught for m in score.matches] == [False, False]


# ── lens30: single-expected precision ─────────────────────────────────────────
# One topic-adjacent structured match flips the whole case, so its keyword set
# gets a positive/negative pair of its own.


def _lens30() -> Case:
    return load_case(_SHIPPED_CASES_DIR / "lens30-list-envelope")


def test_lens30_envelope_finding_is_caught() -> None:
    score = score_run(
        _lens30(),
        _split_report(
            (
                _LENS_CLIENT,
                "list_notes reads response rows from invented keys but the "
                "tool's only container key is items, so every disambiguation "
                "lookup yields an empty candidate list",
            )
        ),
    )
    assert score.caught is True


def test_lens30_topic_adjacent_finding_is_not_a_catch() -> None:
    # A same-file pagination finding wordable as "returns every document"
    # must not satisfy the envelope expected.
    score = score_run(
        _lens30(),
        _split_report(
            (
                _LENS_CLIENT,
                "list_notes returns every document without pagination and "
                "should pass a limit",
            )
        ),
    )
    assert score.caught is False


# ── lens22-artifact: the RH-3 artifact-pass twin scores on rendered evidence ──
# Findings come from the artifact-review pass (screenshots), so the rationale
# describes what the captures show; scoring is unchanged — same matcher, same
# same-file-negative discipline.

_LENS_CSS = "src/lithos_lens/static/lens.css"


def _lens22_artifact() -> Case:
    return load_case(_SHIPPED_CASES_DIR / "lens22-artifact-prewrap")


def test_lens22_artifact_prewrap_finding_is_caught() -> None:
    score = score_run(
        _lens22_artifact(),
        _split_report(
            (
                _LENS_CSS,
                "the rendered note pages show anomalous vertical gaps between "
                "every list item and doubled blank space after headings — "
                ".markdown-body still applies white-space: pre-wrap, so the "
                "newlines between generated block elements render literally",
            )
        ),
    )
    assert score.caught is True


def test_lens22_artifact_same_file_style_finding_is_not_a_catch() -> None:
    # A same-file cosmetic finding (palette/contrast critique of the same
    # stylesheet) must not satisfy the whitespace-rendering expected.
    score = score_run(
        _lens22_artifact(),
        _split_report(
            (
                _LENS_CSS,
                "the note card accent color fails WCAG contrast against the "
                "ochre background and the serif body face reads poorly at "
                "320px — revisit the palette tokens",
            )
        ),
    )
    assert score.caught is False


# ── noise instrumentation (#310) ──────────────────────────────────────────────
#
# The defect-specific catch/FP answer says nothing about what ELSE a run
# reported. ``score_run`` therefore also records the two raw observations the
# report already carries: how many findings the pass produced, and whether it
# held approval.


def test_score_run_counts_every_finding_across_reviewers() -> None:
    report = {
        "reviewers": [
            {
                "name": "correctness",
                "status": "FINDINGS",
                "findings": [
                    _finding("minor", ["a.py"], "nit one", fid="f-001"),
                    _finding("minor", ["b.py"], "nit two", fid="f-002"),
                ],
            },
            {
                "name": "security",
                "status": "FINDINGS",
                "findings": [_finding("major", ["c.py"], "nit three", fid="f-003")],
            },
        ]
    }
    assert score_run(_case(), report).n_findings == 3


def test_score_run_counts_zero_findings_for_a_clean_pass() -> None:
    assert score_run(_case(), _report_status("LGTM")).n_findings == 0


def test_score_run_reads_the_reports_blocking_flag() -> None:
    blocked = {"blocking": True, "reviewers": [{"name": "c", "findings": []}]}
    clean = {"blocking": False, "reviewers": [{"name": "c", "findings": []}]}
    assert score_run(_case(), blocked).blocked is True
    assert score_run(_case(), clean).blocked is False


def test_score_run_treats_a_blockingless_report_as_not_blocked() -> None:
    # Robustness only: every real ReviewReport.to_json() carries `blocking`, so
    # the default is reached by test stubs alone.
    assert score_run(_case(), _report([])).blocked is False


def test_score_run_records_noise_on_a_run_that_missed_the_defect() -> None:
    # The #310 shape: findings that are NOT the expected defect. The catch
    # answer is "miss"; the instrumentation must still see three findings and
    # a held approval.
    report = {
        "blocking": True,
        "reviewers": [
            {
                "name": "correctness",
                "status": "FINDINGS",
                "findings": [
                    _finding("minor", ["style.css"], "no max line length", fid="f-1"),
                    _finding("minor", ["style.css"], "unstyled tag row", fid="f-2"),
                    _finding("major", ["style.css"], "pre-existing margin", fid="f-3"),
                ],
            }
        ],
    }
    score = score_run(_case(), report)
    assert score.caught is False
    assert score.n_findings == 3
    assert score.blocked is True


def test_finding_count_is_the_public_offline_helper() -> None:
    # Documented in evals/review/README.md for re-deriving noise from report
    # dirs that predate #310 — so it must stay public and agree with score_run.
    report = {
        "reviewers": [
            {"name": "a", "findings": [_finding("minor", ["x.py"], "one")]},
            {"name": "b", "findings": [_finding("minor", ["y.py"], "two", fid="f-2")]},
        ]
    }
    assert finding_count(report) == 2
    assert finding_count(report) == score_run(_case(), report).n_findings
    assert finding_count({"reviewers": []}) == 0


# ── judge status: veto vs unreadable vs unreachable (#307) ───────────────────


def _judging(verdict: JudgeVerdict):
    return lambda mech, fs: verdict


def test_judge_failure_is_not_a_veto() -> None:
    """An unreachable judge answered nothing — that is not evidence of a miss."""
    produced = [_finding("critical", ["cli/develop.py"], "approved before delivery")]
    m = match_expected(
        _EXPECTED, produced, judge=_judging(JudgeVerdict(status="failed"))
    )

    assert m.caught is False
    assert m.judge is not None and m.judge.status == "failed"


def test_match_records_the_judges_verdict() -> None:
    verdict = JudgeVerdict(matched_ids=("f-001",), reply="because X\nMATCHED: f-001")
    produced = [_finding("critical", ["cli/develop.py"], "approved before delivery")]
    m = match_expected(_EXPECTED, produced, judge=_judging(verdict))

    assert m.judge is not None
    assert m.judge.matched_ids == ("f-001",)
    assert "because X" in m.judge.reply  # the audit trail #307 asked for


def test_structured_answer_is_computed_alongside_the_judge() -> None:
    """The #307 shape: judge vetoes a finding the structured matcher accepts."""
    produced = [
        _finding("critical", ["cli/develop.py"], "the approved-state delivery summary")
    ]
    m = match_expected(_EXPECTED, produced, judge=_judging(JudgeVerdict()))

    assert m.caught is False  # the judge's answer is authoritative
    assert m.structured_caught is True  # ...and the divergence is recorded
    assert m.structured_finding_id == "f-001"
    assert m.method == "judge"


def test_structured_answer_agrees_with_the_verdict_without_a_judge() -> None:
    produced = [_finding("critical", ["cli/develop.py"], "approved before delivery")]
    m = match_expected(_EXPECTED, produced, judge=None)
    assert m.caught is True and m.structured_caught is True
    assert m.judge is None


def test_score_run_flags_judge_errored_without_setting_incomplete() -> None:
    """The two halves of the instrument fail independently and are reported so."""
    report = _report(
        [_finding("critical", ["cli/develop.py"], "approved before delivery")]
    )
    score = score_run(_case(), report, judge=_judging(JudgeVerdict(status="failed")))

    assert score.judge_errored is True
    assert score.incomplete is False  # the REVIEWER was fine
    assert score.judge_status == "failed"


def test_score_run_unparsed_is_judge_errored_too() -> None:
    report = _report([_finding("critical", ["cli/develop.py"], "approved delivery")])
    score = score_run(_case(), report, judge=_judging(JudgeVerdict(status="unparsed")))
    assert score.judge_errored is True


def test_score_run_veto_is_not_judge_errored() -> None:
    report = _report([_finding("critical", ["cli/develop.py"], "approved delivery")])
    score = score_run(_case(), report, judge=_judging(JudgeVerdict()))
    assert score.caught is False
    assert score.judge_errored is False  # a veto IS a measurement


def test_score_run_records_the_structured_counterfactual() -> None:
    report = _report(
        [_finding("critical", ["cli/develop.py"], "approved before delivery")]
    )
    score = score_run(_case(), report, judge=_judging(JudgeVerdict()))
    assert score.caught is False
    assert score.structured_caught is True


def test_run_score_judge_status_is_empty_without_a_judge() -> None:
    report = _report([_finding("critical", ["cli/develop.py"], "approved delivery")])
    score = score_run(_case(), report)
    assert score.judge_status == ""
    assert score.judge_errored is False


def test_judged_happy_path_verdict_is_unchanged() -> None:
    """Comparability guard: a well-formed verdict scores exactly as it always did.

    Only the failure paths changed behaviour in #307, so the pinned baseline
    stays a valid comparison target.
    """
    produced = [
        _finding("critical", ["cli/develop.py"], "exits before delivery", fid="f-007")
    ]
    m = match_expected(
        _EXPECTED, produced, judge=_judging(JudgeVerdict(matched_ids=("f-007",)))
    )
    assert (m.caught, m.severity_correct, m.method, m.finding_id) == (
        True,
        True,
        "judge",
        "f-007",
    )
