"""Tests for the triage-eval case loader (PRD pr-reconciliation S8, triage shape).

A triage case is one tree plus a BATCH of external-shaped findings, each
declared ``proceed`` (known-true, or ``ambiguous`` — default-to-act) or
``reject`` (known-false, with the files/line ranges a correct rejection must
cite). The loader fails closed on anything it cannot score.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lithos_loom.evals.triage.case import Refutation, TriageCase, load_triage_case

_SHA = "a" * 40
_BASE = "b" * 40


def _write(
    case_dir: Path, toml: str, ac: str = "the AC", patch: str | None = None
) -> Path:
    case_dir.mkdir(parents=True, exist_ok=True)
    (case_dir / "case.toml").write_text(toml, encoding="utf-8")
    (case_dir / "ac.md").write_text(ac, encoding="utf-8")
    if patch is not None:
        (case_dir / "seed.patch").write_text(patch, encoding="utf-8")
    return case_dir


_VALID = f'''
[case]
id = "t1"
description = "a batch"
repo = "../somewhere"
sha = "{_SHA}"

[[finding]]
id = "f-001"
author = "some-bot"
path = "src/a.py"
line = 10
body = """a true
claim"""
expected = "proceed"
provenance = "panel"

[[finding]]
id = "f-002"
body = "a design judgement"
expected = "proceed"
ambiguous = true

[[finding]]
id = "f-003"
body = "a false claim"
expected = "reject"
refutation = ["src/a.py:20-24", "docs/REQ.md", "src/b.py:7"]
provenance = "synthetic"
'''


def test_valid_case_loads(tmp_path: Path) -> None:
    case = load_triage_case(_write(tmp_path / "t1", _VALID))

    assert isinstance(case, TriageCase)
    assert (case.id, case.repo, case.sha, case.base, case.head_patch) == (
        "t1",
        "../somewhere",
        _SHA,
        "",
        None,
    )
    assert case.acceptance_criteria == "the AC"
    assert case.case_dir == tmp_path / "t1"
    assert case.image is None
    assert case.tree_label == _SHA[:12]
    f1, f2, f3 = case.findings
    assert (f1.finding_id, f1.author, f1.path, f1.line) == (
        "f-001",
        "some-bot",
        "src/a.py",
        10,
    )
    assert f1.body == "a true claim"  # whitespace collapsed, as the intake renders it
    assert f1.anchor == "src/a.py:10"
    assert (f2.author, f2.path, f2.line, f2.ambiguous, f2.anchor) == (
        "reviewer",
        "",
        None,
        True,
        "",
    )
    assert f3.refutation == (
        Refutation("src/a.py", 20, 24),
        Refutation("docs/REQ.md"),
        Refutation("src/b.py", 7, None),
    )
    assert [f.finding_id for f in case.known_true] == ["f-001", "f-002"]
    assert [f.finding_id for f in case.known_false] == ["f-003"]


def test_refutation_covers_paths_and_ranges() -> None:
    r = Refutation("src/a.py", 20, 24)
    assert r.covers("src/a.py", 20) and r.covers("src/a.py", 24)
    assert not r.covers("src/a.py", 19) and not r.covers("src/a.py", 25)
    assert not r.covers("za.py", 22)
    assert Refutation("src/a.py").covers("src/a.py", 999)
    assert Refutation("src/a.py", 7).covers("src/a.py", 7)
    assert not Refutation("src/a.py", 7).covers("src/a.py", 8)
    assert Refutation("src/a.py", 20, 24).spec == "src/a.py:20-24"
    assert Refutation("src/a.py", 7).spec == "src/a.py:7"
    assert Refutation("src/a.py").spec == "src/a.py"


def test_patch_form_tree(tmp_path: Path) -> None:
    toml = _VALID.replace(
        f'sha = "{_SHA}"', f'base = "{_BASE}"\nhead_patch = "seed.patch"'
    )
    case = load_triage_case(_write(tmp_path / "p", toml, patch="diff --git a/x b/x\n"))
    assert (case.sha, case.base, case.head_patch) == ("", _BASE, "seed.patch")
    assert case.tree_label == f"{_BASE[:12]}+seed.patch"


def test_repo_defaults_to_dot_and_image_is_parsed(tmp_path: Path) -> None:
    toml = _VALID.replace('repo = "../somewhere"\n', 'image = " ralph-sandbox:x "\n')
    case = load_triage_case(_write(tmp_path / "t1", toml))
    assert case.repo == "."
    assert case.image == "ralph-sandbox:x"


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda t: t.replace(f'sha = "{_SHA}"\n', ""), "exactly one tree"),
        (lambda t: t.replace(f'sha = "{_SHA}"', 'sha = "abc"'), "sha"),
        (
            lambda t: t.replace(f'sha = "{_SHA}"', f'sha = "{_SHA}"\nbase = "{_BASE}"'),
            "exactly one tree",
        ),
        (
            lambda t: t.replace(f'sha = "{_SHA}"', f'base = "{_BASE}"'),
            "exactly one tree",
        ),
        (
            lambda t: t.replace(
                f'sha = "{_SHA}"', f'base = "{_BASE}"\nhead_patch = "nope.patch"'
            ),
            "head_patch",
        ),
        (
            lambda t: t.replace(
                f'sha = "{_SHA}"', f'base = "{_BASE}"\nhead_patch = "../x.patch"'
            ),
            "head_patch",
        ),
        (lambda t: t.replace('id = "t1"\n', ""), "id"),
        (
            lambda t: t.replace('description = "a batch"', 'description = "  "'),
            "description",
        ),
        (lambda t: t.replace('id = "f-002"', 'id = "f-003"'), "positional"),
        (lambda t: t.replace('id = "f-001"', 'id = "finding-1"'), "positional"),
        (
            lambda t: t.replace(
                'expected = "proceed"\nprovenance', 'expected = "maybe"\nprovenance'
            ),
            "expected",
        ),
        (
            lambda t: t.replace(
                'refutation = ["src/a.py:20-24", "docs/REQ.md", "src/b.py:7"]\n', ""
            ),
            "refutation",
        ),
        (
            lambda t: t.replace(
                "ambiguous = true\n", 'ambiguous = true\nrefutation = ["x.py"]\n'
            ),
            "refutation",
        ),
        (
            lambda t: t.replace(
                'refutation = ["src/a.py:20-24"', 'refutation = ["src/a.py:24-20"'
            ),
            "inverted",
        ),
        (
            lambda t: t.replace(
                'refutation = ["src/a.py:20-24"', 'refutation = ["src/a.py:0"'
            ),
            "start at 1",
        ),
        (
            lambda t: t.replace('refutation = ["src/a.py:20-24"', 'refutation = [":5"'),
            "refutation entry",
        ),
        (
            lambda t: t.replace(
                'expected = "reject"', 'expected = "reject"\nambiguous = true'
            ),
            "ambiguous",
        ),
        (lambda t: t.replace("ambiguous = true", 'ambiguous = "yes"'), "boolean"),
        (
            lambda t: t.replace('provenance = "panel"', 'provenance = "rumour"'),
            "provenance",
        ),
        (lambda t: t.replace('body = "a design judgement"', 'body = "  "'), "body"),
        (lambda t: t.replace('author = "some-bot"', 'author = " "'), "author"),
        (lambda t: t.replace("line = 10", "line = 0"), "line"),
        (lambda t: t.replace("line = 10", "line = true"), "line"),
        (lambda t: t.replace('path = "src/a.py"\n', ""), "line needs a path"),
        (lambda t: t + "\n[extra]\nx = 1\n", "unknown"),
        (
            lambda t: t.replace(
                'description = "a batch"', 'description = "a batch"\nfoo = 1'
            ),
            "unknown",
        ),
        (
            lambda t: t.replace(
                'provenance = "panel"', 'provenance = "panel"\nseverity = "major"'
            ),
            "unknown",
        ),
        (
            lambda t: t.replace(
                'repo = "../somewhere"', 'repo = "../somewhere"\nimage = ""'
            ),
            "image",
        ),
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
    head, _, _ = _VALID.partition('[[finding]]\nid = "f-003"')
    case = load_triage_case(_write(tmp_path / "p", head))
    assert case.known_false == ()
    assert len(case.known_true) == 2
