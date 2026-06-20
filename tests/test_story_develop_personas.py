"""Tests for the canonical reviewer personas (#137, ADR 0003 §8).

Pure registry + brief content. No Docker — the specs feed the existing
reviewer-render seam, exercised in the core orchestration tests.
"""

from __future__ import annotations

from lithos_loom.plugins.story_develop.config import (
    VALID_EFFORTS,
    is_valid_reviewer_name,
)
from lithos_loom.plugins.story_develop.personas import canonical_personas

_THRESHOLDS = {"critical", "major", "minor"}
EXPECTED = {
    "correctness",
    "security",
    "architecture",
    "test-quality",
    "dependency-hygiene",
}


def test_registry_has_exactly_the_five_personas() -> None:
    assert set(canonical_personas()) == EXPECTED


def test_registry_is_a_cached_singleton() -> None:
    assert canonical_personas() is canonical_personas()


def test_every_spec_is_well_formed() -> None:
    for name, spec in canonical_personas().items():
        assert spec.name == name
        assert is_valid_reviewer_name(spec.name)
        assert spec.tool in {"claude", "codex"}
        assert spec.block_threshold in _THRESHOLDS
        assert spec.system_prompt is not None and spec.system_prompt.strip()
        if spec.effort is not None:
            assert spec.effort in VALID_EFFORTS
        # Models are left to inherit the route/project default (#137) rather than
        # hard-pinning a possibly-stale id.
        assert spec.model is None


def test_engine_and_threshold_map_matches_the_decision() -> None:
    p = canonical_personas()
    assert (p["correctness"].tool, p["correctness"].block_threshold) == (
        "codex",
        "major",
    )
    assert (
        p["security"].tool,
        p["security"].block_threshold,
        p["security"].effort,
    ) == ("claude", "minor", "xhigh")
    assert (p["architecture"].tool, p["architecture"].block_threshold) == (
        "codex",
        "major",
    )
    assert (p["test-quality"].tool, p["test-quality"].block_threshold) == (
        "codex",
        "minor",
    )
    assert (
        p["dependency-hygiene"].tool,
        p["dependency-hygiene"].block_threshold,
    ) == ("claude", "minor")


def test_codex_personas_carry_no_effort() -> None:
    # effort is honoured by claude only; codex depth is model-driven (containers.py).
    p = canonical_personas()
    for name in ("correctness", "architecture", "test-quality"):
        assert p[name].effort is None


def test_each_brief_is_one_dimension_with_an_explicit_deferral() -> None:
    # The "NOT your job" line is what keeps each persona in its lane.
    for spec in canonical_personas().values():
        assert spec.system_prompt is not None
        assert "NOT your job" in spec.system_prompt


def test_security_brief_cites_owasp_and_cwe() -> None:
    sec = canonical_personas()["security"].system_prompt
    assert sec is not None
    assert "OWASP" in sec
    assert "CWE" in sec
