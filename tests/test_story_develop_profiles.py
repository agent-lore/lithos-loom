"""Tests for Review Profiles — the strength dial (#139, ADR 0003 §1/§2/§3)."""

from __future__ import annotations

import pytest

from lithos_loom.plugins.story_develop.profiles import (
    CANONICAL_PROFILES,
    DEFAULT_PROFILE_NAME,
    MonotonicityError,
    ProfileCheck,
    ProfileResolution,
    ReviewProfile,
    resolve_profile,
    validate_monotonic,
)

_BY_NAME = {p.name: p for p in CANONICAL_PROFILES}


# --- the three canonical profiles match the ADR §3 floor table ----------------


def test_minimal_floor() -> None:
    p = _BY_NAME["minimal"]
    assert p.strength_rank == 10
    assert p.required_personas == frozenset()
    assert p.required_check_names == {"format", "lint", "test"}


def test_standard_is_the_default_and_its_floor() -> None:
    assert DEFAULT_PROFILE_NAME == "standard"
    p = _BY_NAME["standard"]
    assert p.strength_rank == 20
    assert p.required_personas == {"correctness", "security"}
    assert p.required_check_names == {"format", "lint", "typecheck", "sast", "test"}


def test_thorough_floor_and_staging() -> None:
    p = _BY_NAME["thorough"]
    assert p.strength_rank == 30
    assert p.required_personas == {
        "correctness",
        "security",
        "architecture",
        "test-quality",
        "dependency-hygiene",
    }
    assert p.required_check_names == {
        "format",
        "lint",
        "typecheck",
        "sast",
        "test",
        "dep-audit",
        "coverage",
    }
    # semgrep is informational — surfaced, not part of the floor.
    assert "semgrep" not in p.required_check_names
    # the expensive checks are staged to the approval candidate.
    assert {c.name for c in p.checks if c.stage == "candidate"} == {
        "dep-audit",
        "coverage",
        "semgrep",
    }


# --- monotonicity invariant (ADR §2) ------------------------------------------


def test_canonical_profiles_are_monotonic() -> None:
    validate_monotonic(CANONICAL_PROFILES)  # does not raise


def test_module_import_validated_the_builtins() -> None:
    # The module runs validate_monotonic(CANONICAL_PROFILES) at import, so this
    # file importing at all (top-level `from ...profiles import ...`) already
    # proves the built-in chain is monotonic — a non-monotonic edit would raise
    # MonotonicityError on import and fail collection.
    assert CANONICAL_PROFILES


def test_non_monotonic_dropped_required_check_is_a_load_error() -> None:
    chain = (
        ReviewProfile(
            "low",
            10,
            (),
            (ProfileCheck("lint", "required"), ProfileCheck("sast", "required")),
        ),
        # rank 20 drops the `sast` the rank-10 profile required.
        ReviewProfile("high", 20, (), (ProfileCheck("lint", "required"),)),
    )
    with pytest.raises(MonotonicityError, match="sast"):
        validate_monotonic(chain)


def test_non_monotonic_dropped_required_persona_is_a_load_error() -> None:
    chain = (
        ReviewProfile("low", 10, ("security",), (ProfileCheck("test", "required"),)),
        # rank 20 drops the security persona the rank-10 profile required.
        ReviewProfile(
            "high", 20, ("correctness",), (ProfileCheck("test", "required"),)
        ),
    )
    with pytest.raises(MonotonicityError, match="security"):
        validate_monotonic(chain)


def test_stage_change_is_not_a_monotonicity_violation() -> None:
    # Moving a required check to a later stage keeps it in the floor (stage ignored).
    chain = (
        ReviewProfile("low", 10, (), (ProfileCheck("dep-audit", "required", "fast"),)),
        ReviewProfile(
            "high", 20, (), (ProfileCheck("dep-audit", "required", "candidate"),)
        ),
    )
    validate_monotonic(chain)  # does not raise


# --- resolve_profile: precedence + fail-closed (ADR §2) -----------------------


def _resolve(
    task: str | None = None,
    project: str | None = None,
    host: str | None = None,
    unknown: str = "halt",
) -> ProfileResolution:
    return resolve_profile(
        task_value=task,
        project_value=project,
        host_value=host,
        unknown_profile=unknown,
    )


def test_all_unset_inherits_standard_silently() -> None:
    r = _resolve()
    assert r.profile.name == "standard"
    assert r.frictions == ()
    assert r.halt is False


def test_precedence_task_beats_project_beats_host() -> None:
    got = _resolve(task="thorough", project="minimal", host="standard")
    assert got.profile.name == "thorough"
    assert _resolve(project="minimal", host="standard").profile.name == "minimal"
    assert _resolve(host="thorough").profile.name == "thorough"


def test_blank_layer_inherits_below() -> None:
    # A blank/whitespace value at a higher layer is "unset" and inherits below.
    assert _resolve(task="   ", project="minimal").profile.name == "minimal"


def test_known_name_has_no_friction_and_no_halt() -> None:
    r = _resolve(task="minimal")
    assert r.profile.name == "minimal"
    assert r.frictions == ()
    assert r.halt is False


def test_unknown_name_halts_by_default() -> None:
    r = _resolve(task="thorogh")
    assert r.halt is True
    assert r.frictions and "thorogh" in r.frictions[0]


def test_unknown_name_strongest_falls_back_never_weaker() -> None:
    r = _resolve(host="nope", unknown="strongest")
    assert r.halt is False
    assert r.frictions and "nope" in r.frictions[0]
    strongest_rank = max(p.strength_rank for p in CANONICAL_PROFILES)
    assert r.profile.strength_rank == strongest_rank
    assert r.profile.name == "thorough"
