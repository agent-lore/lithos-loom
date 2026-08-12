"""Tests for the eval panel-override axis (RH-7).

`parse_reviewer_overrides` / `resolve_panel` let `eval review` vary the panel
per run — profile replacement, explicit `--reviewer` enumeration, per-reviewer
model/effort/tool overrides — without editing case files or persona
definitions. Everything fails closed BEFORE any paid run.
"""

from __future__ import annotations

import pytest

from lithos_loom.evals.review.case import Case, Expected
from lithos_loom.evals.review.overrides import (
    parse_reviewer_overrides,
    resolve_panel,
)
from lithos_loom.plugins.story_develop.personas import canonical_personas

_EXPECTED = Expected(file="f.py", keywords=("k",), min_severity="major")


def _case(
    personas: tuple[str, ...] = ("correctness",), profile: str = "standard"
) -> Case:
    return Case(
        id="c",
        description="",
        repo=".",
        base="b",
        head="h",
        acceptance_criteria="ac",
        personas=personas,
        profile=profile,
        expected=(_EXPECTED,),
    )


# ── parse_reviewer_overrides ──────────────────────────────────────────────────


def test_parse_model_override() -> None:
    parsed = parse_reviewer_overrides(["correctness.model=some-model "])
    assert parsed == {"correctness": {"model": "some-model"}}  # stripped


def test_parse_effort_override_normalises() -> None:
    parsed = parse_reviewer_overrides(["security.effort=XHIGH"])
    assert parsed == {"security": {"effort": "xhigh"}}


def test_parse_tool_override() -> None:
    parsed = parse_reviewer_overrides(["security.tool=codex"])
    assert parsed == {"security": {"tool": "codex"}}


def test_parse_merges_fields_and_last_wins() -> None:
    parsed = parse_reviewer_overrides(
        [
            "correctness.model=first",
            "correctness.effort=low",
            "correctness.model=second",
        ]
    )
    assert parsed == {"correctness": {"model": "second", "effort": "low"}}


def test_parse_rejects_unknown_persona() -> None:
    with pytest.raises(ValueError, match="unknown persona"):
        parse_reviewer_overrides(["corectness.model=x"])


def test_parse_rejects_unknown_field() -> None:
    with pytest.raises(ValueError, match="field"):
        parse_reviewer_overrides(["correctness.speed=x"])


def test_parse_rejects_missing_dot() -> None:
    with pytest.raises(ValueError, match=r"PERSONA\.FIELD=VALUE"):
        parse_reviewer_overrides(["correctness=x"])


def test_parse_rejects_missing_equals() -> None:
    with pytest.raises(ValueError, match=r"PERSONA\.FIELD=VALUE"):
        parse_reviewer_overrides(["correctness.model"])


def test_parse_rejects_invalid_effort() -> None:
    with pytest.raises(ValueError, match="effort"):
        parse_reviewer_overrides(["correctness.effort=ultra"])


def test_parse_rejects_unsupported_tool() -> None:
    with pytest.raises(ValueError, match="unsupported tool"):
        parse_reviewer_overrides(["correctness.tool=gemini"])


def test_parse_rejects_empty_model() -> None:
    with pytest.raises(ValueError, match="model"):
        parse_reviewer_overrides(["correctness.model="])


# ── resolve_panel ─────────────────────────────────────────────────────────────


def test_default_panel_is_case_derived() -> None:
    profile, panel = resolve_panel(_case())
    assert profile == "standard"
    assert panel == (canonical_personas()["correctness"],)


def test_override_applies_to_present_persona() -> None:
    overrides = parse_reviewer_overrides(
        ["correctness.model=some-model", "correctness.effort=low"]
    )
    _, panel = resolve_panel(_case(), overrides=overrides)
    (spec,) = panel
    assert spec.name == "correctness"
    assert spec.model == "some-model"
    assert spec.effort == "low"
    # the shared canonical registry must never be mutated
    assert canonical_personas()["correctness"].model is None


def test_override_for_absent_persona_leaves_panel_untouched() -> None:
    # Apply-where-present: a full-benchmark sweep mixes panels, so an override
    # naming a persona a case doesn't field must not error or alter the panel.
    overrides = parse_reviewer_overrides(["correctness.model=x"])
    _, panel = resolve_panel(_case(personas=("security",)), overrides=overrides)
    assert panel == (canonical_personas()["security"],)


def test_profile_replaces_panel_and_checkset() -> None:
    profile, panel = resolve_panel(_case(), profile="thorough")
    assert profile == "thorough"
    assert [s.name for s in panel] == [
        "correctness",
        "security",
        "architecture",
        "test-quality",
        "dependency-hygiene",
    ]


def test_gate_only_profile_without_reviewers_raises() -> None:
    with pytest.raises(ValueError, match="minimal"):
        resolve_panel(_case(), profile="minimal")


def test_gate_only_profile_with_reviewers_is_checkset_only() -> None:
    profile, panel = resolve_panel(_case(), profile="minimal", reviewers=["security"])
    assert profile == "minimal"
    assert panel == (canonical_personas()["security"],)


def test_reviewers_win_over_profile_panel() -> None:
    profile, panel = resolve_panel(
        _case(), profile="thorough", reviewers=["security", "correctness"]
    )
    assert profile == "thorough"
    assert [s.name for s in panel] == ["security", "correctness"]


def test_reviewers_dedup_preserving_order() -> None:
    _, panel = resolve_panel(_case(), reviewers=["security", "security", "correctness"])
    assert [s.name for s in panel] == ["security", "correctness"]


def test_unknown_reviewer_name_raises() -> None:
    with pytest.raises(ValueError, match="unknown"):
        resolve_panel(_case(), reviewers=["corectness"])


def test_overrides_apply_on_top_of_profile_panel() -> None:
    overrides = parse_reviewer_overrides(["correctness.model=some-model"])
    _, panel = resolve_panel(_case(), profile="thorough", overrides=overrides)
    by_name = {s.name: s for s in panel}
    assert by_name["correctness"].model == "some-model"
    assert by_name["security"].model is None
