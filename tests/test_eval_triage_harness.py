"""Tests for the triage-eval harness: tree, batch scoring, aggregation, intake.

Hermetic: the triage function is injected. The live one (a read-only
container turn) is host-only and never runs here.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from lithos_loom.evals.triage.case import Refutation, TriageCase, TriageFinding
from lithos_loom.evals.triage.harness import (
    TriageCaseResult,
    aggregate_triage,
    expected_fingerprint,
    external_findings_for,
    materialise_tree,
    run_triage_case,
    score_sample,
)
from lithos_loom.plugins.story_develop.external_reviews import external_intake_reviews
from lithos_loom.plugins.story_develop.external_triage import (
    LINE_PROCEED,
    LINE_REJECT,
    TriageVerdicts,
)

_SHA = "a" * 40


def _finding(
    fid: str,
    expected: str,
    refutation: tuple[Refutation, ...] = (),
    *,
    ambiguous: bool = False,
    path: str = "src/a.py",
    line: int | None = 1,
) -> TriageFinding:
    return TriageFinding(
        finding_id=fid,
        author="bot",
        path=path,
        line=line,
        body=f"claim {fid}",
        expected=expected,
        ambiguous=ambiguous,
        refutation=refutation,
        provenance="synthetic",
    )


def _case(**kw: Any) -> TriageCase:
    base: dict[str, Any] = dict(
        id="t",
        description="d",
        repo=".",
        sha=_SHA,
        acceptance_criteria="ac",
        findings=(
            _finding("f-001", "proceed"),
            _finding("f-002", "proceed", ambiguous=True),
            _finding("f-003", "reject", (Refutation("src/a.py", 20, 24),)),
            _finding(
                "f-004", "reject", (Refutation("src/b.py"), Refutation("docs/R.md", 3))
            ),
        ),
    )
    base.update(kw)
    return TriageCase(**base)


def _verdicts(
    proceed: tuple[str, ...], rejections: dict[str, str], note: str = ""
) -> TriageVerdicts:
    kinds = {fid: LINE_PROCEED for fid in proceed} | {
        fid: LINE_REJECT for fid in rejections
    }
    return TriageVerdicts(
        proceed=proceed,
        rejections=rejections,
        cost_usd=0.1,
        note=note,
        line_kinds=kinds,
        verdict_text="## Verdicts\n",
    )


def test_perfect_sample_scores_every_finding_correct() -> None:
    score = score_sample(
        _case(),
        _verdicts(
            ("f-001", "f-002"),
            {"f-003": "src/a.py:22 guards it", "f-004": "docs/R.md:3 says otherwise"},
        ),
    )
    assert score.correct == {"f-001": True, "f-002": True, "f-003": True, "f-004": True}
    assert score.rejected_known_false == 2
    assert score.suppressed_known_true == 0
    assert score.errored is False
    assert score.line_kinds["f-003"] == LINE_REJECT
    assert score.verdict_text == "## Verdicts\n"


@pytest.mark.parametrize(
    "evidence",
    [
        "src/zzz.py:1 unrelated",  # wrong file
        "src/a.py:1 the claim's own anchor",  # right file, outside the range
        "src/a.py:25 one past the range",
        "za.py:22 a suffix of the path is not the path",
        "/workspace/src/a.py:19 container-rooted but outside",
    ],
)
def test_reject_citing_outside_the_refutation_is_not_a_correct_reject(
    evidence: str,
) -> None:
    # The parser already requires a resolving file:line; the eval asks for
    # MORE — the rejection must cite the code that actually refutes it, and a
    # declared range keeps the claim's own anchor from counting as evidence.
    score = score_sample(
        _case(), _verdicts(("f-001", "f-002", "f-004"), {"f-003": evidence})
    )
    assert score.correct["f-003"] is False
    assert score.rejected_known_false == 0


@pytest.mark.parametrize(
    "evidence",
    [
        "src/a.py:20 start of range",
        "src/a.py:24 end of range",
        "/workspace/src/a.py:22 container-rooted spelling",
        "./src/a.py:21 relative spelling",
        "src/zzz.py:9 first, then src/a.py:23 — any one citation in range counts",
    ],
)
def test_reject_citing_into_the_range_is_a_correct_reject(evidence: str) -> None:
    score = score_sample(
        _case(), _verdicts(("f-001", "f-002", "f-004"), {"f-003": evidence})
    )
    assert score.correct["f-003"] is True


def test_rejecting_a_must_proceed_finding_is_over_suppression() -> None:
    score = score_sample(
        _case(),
        _verdicts(
            ("f-002",),
            {
                "f-001": "src/a.py:5 nope",
                "f-003": "src/a.py:22 x",
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
            ("f-001", "f-002"), {"f-003": "src/a.py:21 a", "f-004": "src/b.py:1 b"}
        ),
    )
    half = score_sample(
        case, _verdicts(("f-001", "f-002", "f-004"), {"f-003": "src/a.py:21 a"})
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
    assert (r.n, r.n_valid) == (4, 3)
    assert r.errored_per_sample == (False, False, False, True)
    # known-false rejected: 2 + 1 + 0 over 3 valid samples × 2 known-false
    assert (r.rejected_known_false, r.known_false_opportunities) == (3, 6)
    assert r.reject_rate == 0.5
    # must-proceed suppressed: 0 + 0 + 1 over 3 valid × 2 must-proceed
    assert (r.suppressed_known_true, r.known_true_opportunities) == (1, 6)
    assert abs(r.over_suppression_rate - 1 / 6) < 1e-9
    assert r.samples_with_suppression == 1
    assert r.passed is False
    assert r.per_finding_correct == {"f-001": 2, "f-002": 3, "f-003": 2, "f-004": 1}
    assert r.per_finding_expected == {
        "f-001": "proceed",
        "f-002": "proceed",
        "f-003": "reject",
        "f-004": "reject",
    }
    assert r.per_finding_ambiguous == {
        "f-001": False,
        "f-002": True,
        "f-003": False,
        "f-004": False,
    }
    lo, hi = r.reject_rate_ci
    assert 0.0 <= lo < 0.5 < hi <= 1.0


def test_passed_requires_bar_and_no_over_suppression_by_default() -> None:
    case = _case()
    perfect = score_sample(
        case,
        _verdicts(
            ("f-001", "f-002"), {"f-003": "src/a.py:21 a", "f-004": "docs/R.md:3 b"}
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
                "f-003": "src/a.py:21 a",
                "f-004": "docs/R.md:3 b",
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
    case = _case(findings=(_finding("f-001", "proceed"),))
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


def test_run_case_materialises_once_calls_triage_k_times_and_feeds_the_sink() -> None:
    case = _case()
    calls: list[tuple[TriageCase, str]] = []
    sunk: list[tuple[str, int, dict]] = []

    def fn(c: TriageCase, sha: str) -> TriageVerdicts:
        calls.append((c, sha))
        return _verdicts(
            ("f-001", "f-002"), {"f-003": "src/a.py:21 a", "f-004": "src/b.py:1 b"}
        )

    r = run_triage_case(
        case,
        k=3,
        bar=0.8,
        max_over_suppression=0.0,
        triage_fn=fn,
        sink=lambda cid, i, payload: sunk.append((cid, i, payload)),
    )
    assert calls == [(case, _SHA)] * 3
    assert r.reject_rate == 1.0 and r.passed is True
    assert [(cid, i) for cid, i, _ in sunk] == [("t", 0), ("t", 1), ("t", 2)]
    payload = sunk[0][2]
    assert payload["sha"] == _SHA
    assert payload["rejections"] == {"f-003": "src/a.py:21 a", "f-004": "src/b.py:1 b"}
    assert payload["correct"]["f-003"] is True
    assert payload["line_kinds"]["f-003"] == LINE_REJECT
    assert payload["verdict_text"] == "## Verdicts\n"
    assert payload["cost_usd"] == 0.1


def test_expected_fingerprint_moves_with_what_the_scorer_reads() -> None:
    base = _case()
    assert expected_fingerprint(base) == expected_fingerprint(_case())
    reworded = _case(
        findings=(
            *base.findings[:1],
            _finding("f-002", "proceed", ambiguous=True, path="src/z.py"),
        )
        + base.findings[2:]
    )
    assert expected_fingerprint(reworded) != expected_fingerprint(base)
    widened = _case(
        findings=base.findings[:2]
        + (_finding("f-003", "reject", (Refutation("src/a.py"),)),)
        + base.findings[3:]
    )
    assert expected_fingerprint(widened) != expected_fingerprint(base)
    # but a description / repo change is not a scoring change
    assert expected_fingerprint(_case(description="other")) == expected_fingerprint(
        base
    )


# ── the batch goes through the PRODUCTION intake ───────────────────────────


def test_external_findings_take_the_production_shape() -> None:
    case = _case(
        findings=(
            _finding("f-001", "proceed", path="src/a.py", line=12),
            _finding("f-002", "proceed", path="", line=None),
        )
    )
    ext = external_findings_for(case, _SHA)
    assert [e.path for e in ext] == ["src/a.py", ""]
    assert [e.line for e in ext] == [12, None]
    assert [e.stream.value for e in ext] == ["inline", "conversation"]
    assert all(e.head_sha == _SHA and e.trusted and e.severity == "minor" for e in ext)

    seed, id_map = external_intake_reviews(ext, current_head_sha=_SHA)
    outcome = seed[0]
    # The ledger assigns the positional ids the case declared; the rationale
    # carries the [author] prefix; severity is production's `minor`; exactly
    # one anchor (or none) — never the eval's own idea of any of those.
    assert [f.finding_id for f in outcome.findings] == ["f-001", "f-002"]
    assert [f.severity for f in outcome.findings] == ["minor", "minor"]
    assert outcome.findings[0].rationale == "[bot] claim f-001"
    assert outcome.findings[0].files == ["src/a.py:12"]
    assert outcome.findings[1].files == []
    assert "older than the current head" not in outcome.findings[0].rationale
    assert id_map["f-001"] is ext[0]


# ── the tree ────────────────────────────────────────────────────────────────


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def test_materialise_tree_is_identity_for_the_sha_form() -> None:
    sha, cleanup = materialise_tree(_case())
    assert sha == _SHA
    cleanup()


def test_materialise_tree_builds_base_plus_patch(
    tmp_git_repo: Path, tmp_path: Path
) -> None:
    (tmp_git_repo / "f.txt").write_text("one\n")
    _git(tmp_git_repo, "add", "f.txt")
    _git(
        tmp_git_repo,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@t",
        "commit",
        "-qm",
        "base",
    )
    base = _git(tmp_git_repo, "rev-parse", "HEAD")
    (tmp_git_repo / "f.txt").write_text("two\n")
    patch_text = _git(tmp_git_repo, "diff")
    _git(tmp_git_repo, "checkout", "--", "f.txt")
    case_dir = tmp_path / "case"
    case_dir.mkdir()
    (case_dir / "seed.patch").write_text(patch_text + "\n")
    case = _case(
        repo=str(tmp_git_repo),
        sha="",
        base=base,
        head_patch="seed.patch",
        case_dir=case_dir,
    )

    sha, cleanup = materialise_tree(case)
    try:
        assert sha != base and len(sha) == 40
        assert _git(tmp_git_repo, "show", f"{sha}:f.txt") == "two"
    finally:
        cleanup()
    # the build worktree is gone after cleanup
    assert sha not in _git(tmp_git_repo, "worktree", "list")
