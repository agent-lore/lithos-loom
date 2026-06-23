"""Tests for the review-correctness eval case model + loader (#183)."""

from __future__ import annotations

from pathlib import Path

import pytest

from lithos_loom.evals.review.case import Case, Expected, load_case


def _write_case(case_dir: Path, *, toml: str, ac: str = "make it correct") -> None:
    case_dir.mkdir(parents=True)
    (case_dir / "case.toml").write_text(toml)
    (case_dir / "ac.md").write_text(ac)


_SEED_TOML = """
[case]
id = "180-attach-delivery"
description = "attach exits on approved before PR delivery"
repo = "."
base = "aaaaaaaa"
head = "bbbbbbbb"
personas = ["correctness"]
profile = "standard"
acceptance_criteria_file = "ac.md"

[known_good]
head = "cccccccc"

[[expected]]
file = "src/lithos_loom/cli/develop.py"
keywords = ["delivery", "approved", "before"]
min_severity = "critical"
mechanism = "attach treats approved as terminal and exits before delivery"
"""


def test_loads_a_well_formed_case(tmp_path: Path) -> None:
    case_dir = tmp_path / "180-attach-delivery"
    _write_case(case_dir, toml=_SEED_TOML, ac="attach must wait for PR delivery")

    case = load_case(case_dir)

    assert isinstance(case, Case)
    assert case.id == "180-attach-delivery"
    assert case.base == "aaaaaaaa"
    assert case.head == "bbbbbbbb"
    assert case.known_good_head == "cccccccc"
    assert case.personas == ("correctness",)
    assert case.profile == "standard"
    # the acceptance criteria are loaded from the referenced file
    assert case.acceptance_criteria == "attach must wait for PR delivery"
    assert len(case.expected) == 1
    exp = case.expected[0]
    assert isinstance(exp, Expected)
    assert exp.file == "src/lithos_loom/cli/develop.py"
    assert "delivery" in exp.keywords
    assert exp.min_severity == "critical"
    assert exp.mechanism


def test_known_good_is_optional(tmp_path: Path) -> None:
    toml = _SEED_TOML.replace('[known_good]\nhead = "cccccccc"\n', "")
    case_dir = tmp_path / "c"
    _write_case(case_dir, toml=toml)
    case = load_case(case_dir)
    assert case.known_good_head is None
    assert case.known_good_base is None


def test_known_good_base_is_parsed(tmp_path: Path) -> None:
    toml = _SEED_TOML.replace(
        '[known_good]\nhead = "cccccccc"\n',
        '[known_good]\nbase = "dddddddd"\nhead = "cccccccc"\n',
    )
    case_dir = tmp_path / "c"
    _write_case(case_dir, toml=toml)
    case = load_case(case_dir)
    assert case.known_good_base == "dddddddd"
    assert case.known_good_head == "cccccccc"


def test_rejects_case_with_no_expected(tmp_path: Path) -> None:
    # drop the [[expected]] block entirely
    toml = _SEED_TOML.split("[[expected]]")[0]
    case_dir = tmp_path / "c"
    _write_case(case_dir, toml=toml)
    with pytest.raises(ValueError):
        load_case(case_dir)


def test_rejects_bad_min_severity(tmp_path: Path) -> None:
    toml = _SEED_TOML.replace('min_severity = "critical"', 'min_severity = "huge"')
    case_dir = tmp_path / "c"
    _write_case(case_dir, toml=toml)
    with pytest.raises(ValueError):
        load_case(case_dir)


def test_rejects_expected_with_no_keywords(tmp_path: Path) -> None:
    toml = _SEED_TOML.replace(
        'keywords = ["delivery", "approved", "before"]', "keywords = []"
    )
    case_dir = tmp_path / "c"
    _write_case(case_dir, toml=toml)
    with pytest.raises(ValueError):
        load_case(case_dir)


def test_rejects_unknown_profile(tmp_path: Path) -> None:
    # a typo'd profile would silently measure the `standard` panel/check-set
    # while the report claims the typo'd name — fail closed.
    toml = _SEED_TOML.replace('profile = "standard"', 'profile = "thorogh"')
    case_dir = tmp_path / "c"
    _write_case(case_dir, toml=toml)
    with pytest.raises(ValueError, match="profile"):
        load_case(case_dir)


def test_rejects_unknown_persona(tmp_path: Path) -> None:
    # a typo'd persona would be silently dropped, measuring a different panel
    toml = _SEED_TOML.replace('personas = ["correctness"]', 'personas = ["corectness"]')
    case_dir = tmp_path / "c"
    _write_case(case_dir, toml=toml)
    with pytest.raises(ValueError, match="persona"):
        load_case(case_dir)


def test_rejects_empty_personas(tmp_path: Path) -> None:
    # a case must declare its panel explicitly (else DevelopConfig would fall
    # back to the built-in reviewer — not what the case claims to measure)
    toml = _SEED_TOML.replace('personas = ["correctness"]', "personas = []")
    case_dir = tmp_path / "c"
    _write_case(case_dir, toml=toml)
    with pytest.raises(ValueError, match="persona"):
        load_case(case_dir)
