"""Tests for the triage-eval case loader (PRD pr-reconciliation S8, triage shape).

A triage case is one repo sha plus a BATCH of external-style findings, each
declared ``proceed`` (known-true, or ambiguous — default-to-act) or ``reject``
(known-false, with the repo files a correct rejection must cite). The loader
fails closed on anything it cannot score.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lithos_loom.evals.triage.case import TriageCase, load_triage_case

_SHA = "a" * 40


def _write(case_dir: Path, toml: str, ac: str = "the AC") -> Path:
    case_dir.mkdir(parents=True, exist_ok=True)
    (case_dir / "case.toml").write_text(toml, encoding="utf-8")
    (case_dir / "ac.md").write_text(ac, encoding="utf-8")
    return case_dir


_VALID = f'''
[case]
id = "t1"
description = "a batch"
repo = "../somewhere"
sha = "{_SHA}"

[[finding]]
id = "f-001"
severity = "major"
files = ["src/a.py:10"]
rationale = "a true claim"
expected = "proceed"
provenance = "panel"

[[finding]]
id = "f-002"
severity = "minor"
rationale = "a false claim"
expected = "reject"
refutation_files = ["src/a.py", "docs/REQ.md"]
provenance = "synthetic"
'''


def test_valid_case_loads(tmp_path: Path) -> None:
    case = load_triage_case(_write(tmp_path / "t1", _VALID))

    assert isinstance(case, TriageCase)
    assert case.id == "t1"
    assert case.repo == "../somewhere"
    assert case.sha == _SHA
    assert case.acceptance_criteria == "the AC"
    assert case.case_dir == tmp_path / "t1"
    assert [f.finding_id for f in case.findings] == ["f-001", "f-002"]
    assert case.findings[0].files == ("src/a.py:10",)
    assert case.findings[1].files == ()
    assert case.findings[1].refutation_files == ("src/a.py", "docs/REQ.md")
    assert [f.finding_id for f in case.known_true] == ["f-001"]
    assert [f.finding_id for f in case.known_false] == ["f-002"]
    assert case.image is None


def test_repo_defaults_to_dot_and_provenance_to_external(tmp_path: Path) -> None:
    toml = _VALID.replace('repo = "../somewhere"\n', "").replace(
        'provenance = "panel"\n', ""
    )
    case = load_triage_case(_write(tmp_path / "t1", toml))
    assert case.repo == "."
    assert case.findings[0].provenance == "external"


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda t: t.replace(f'sha = "{_SHA}"\n', ""), "sha"),
        (
            lambda t: (
                t + '\n[[finding]]\nid = "f-001"\nseverity = "minor"\n'
                'rationale = "dup"\nexpected = "proceed"\n'
            ),
            "duplicate",
        ),
        (lambda t: t.replace('expected = "proceed"', 'expected = "maybe"'), "expected"),
        (
            lambda t: t.replace('refutation_files = ["src/a.py", "docs/REQ.md"]\n', ""),
            "refutation_files",
        ),
        (
            lambda t: t.replace(
                'expected = "proceed"\n',
                'expected = "proceed"\nrefutation_files = ["x.py"]\n',
            ),
            "refutation_files",
        ),
        (lambda t: t.replace('severity = "major"', 'severity = "huge"'), "severity"),
        (
            lambda t: t.replace('provenance = "panel"', 'provenance = "rumour"'),
            "provenance",
        ),
        (
            lambda t: t.replace('rationale = "a true claim"', 'rationale = ""'),
            "rationale",
        ),
        (lambda t: t + "\n[extra]\nx = 1\n", "unknown"),
        (
            lambda t: t.replace(
                'description = "a batch"', 'description = "a batch"\nfoo = 1'
            ),
            "unknown",
        ),
        (
            lambda t: t.replace(
                'provenance = "panel"', 'provenance = "panel"\nbar = 2'
            ),
            "unknown",
        ),
        (lambda t: t.replace(f'sha = "{_SHA}"', 'sha = "abc"'), "sha"),
    ],
)
def test_invalid_cases_fail_closed(tmp_path: Path, mutation, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        load_triage_case(_write(tmp_path / "bad", mutation(_VALID)))


def test_empty_batch_and_empty_ac_are_rejected(tmp_path: Path) -> None:
    head, _, _ = _VALID.partition("[[finding]]")
    with pytest.raises(ValueError, match="finding"):
        load_triage_case(_write(tmp_path / "empty", head))
    with pytest.raises(ValueError, match="acceptance"):
        load_triage_case(_write(tmp_path / "noac", _VALID, ac="  \n"))


def test_a_batch_of_only_proceeds_is_allowed(tmp_path: Path) -> None:
    # An over-suppression-only probe: no known-false, every finding must
    # survive. Legal — the reject rate is then simply not measured.
    head, sep, rest = _VALID.partition('[[finding]]\nid = "f-002"')
    case = load_triage_case(_write(tmp_path / "p", head))
    assert case.known_false == ()
    assert len(case.known_true) == 1
