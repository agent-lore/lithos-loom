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


def test_correctness_brief_asks_for_value_domains_and_both_outcomes() -> None:
    # RH-1: lens33-confidence-crash measured 0/5, and the per-expected split put
    # the whole deficit on ONE of its two forms — the reviewer sat on the right
    # line, saw a value formatted without validation, and asked only "what input
    # makes this raise?" (finding the NaN crash) but never "what input makes this
    # return something wrong?" (missing the finite out-of-range render). The
    # brief's failure-mode list was exception-shaped throughout: its one boundary
    # bullet gave only collection examples, and contract fidelity was framed as
    # types and None-handling. The measured lever is value DOMAINS plus the rule
    # that one bad-value class obliges you to enumerate the rest — the same shape
    # as the security brief's mirror rule (#318). A re-tune that drops any of the
    # three parts silently reverts the arm.
    c = canonical_personas()["correctness"].system_prompt
    assert c is not None
    # Collapse wrapping: where the prose breaks lines is formatting, not content.
    flat = " ".join(c.split())
    assert "outside the range, unit, scale, or set" in flat
    assert "does it raise, or does it silently produce a wrong answer" in flat
    assert "enumerate the rest" in flat
    # PR #321 review: the bullet must not rank the silent case above an exception.
    # It is a DETECTION lever — the attention it buys is the point — and a blanket
    # "worse defect" would inflate severity independently of finding count, which
    # lens33 cannot detect (its known-good blocking is saturated).
    assert "not a reason to rate it higher" in flat


_CASE_VOCABULARY = (
    "confidence",
    "percent",
    "fraction",
    "round(",
    "nan",
    "infinit",
    "frontmatter",
    "0..1",
)


def test_correctness_brief_stays_off_the_benchmark_case_vocabulary() -> None:
    # RH-1 over-fit guard (#308's review flagged exactly this on the artifact
    # prompt). The input-domain bullet was tuned against lens33-confidence-crash,
    # so the arm is evidence of a GENERAL lever only while the brief never names
    # that case's shape. Grepping by hand at authoring time does not survive the
    # next re-tune; with one case per persona, over-fit is otherwise unfalsifiable.
    brief = canonical_personas()["correctness"].system_prompt
    assert brief is not None
    lowered = brief.lower()
    for term in _CASE_VOCABULARY:
        assert term not in lowered, f"benchmark-case vocabulary in the brief: {term!r}"


def test_security_brief_cites_owasp_and_cwe() -> None:
    sec = canonical_personas()["security"].system_prompt
    assert sec is not None
    assert "OWASP" in sec
    assert "CWE" in sec


def test_security_brief_asks_for_both_boundary_directions() -> None:
    # RH-1: the source -> sink template alone measured 3/5 on 289-symlink-artifacts,
    # missing the write direction every time it missed — the reviewer traced what a
    # privileged actor READS from an untrusted place and never asked who controls
    # where it WRITES. The measured lever is this pair of questions plus the rule
    # that finding one direction obliges you to state the other; a re-tune that
    # drops either half silently reverts the arm.
    sec = canonical_personas()["security"].system_prompt
    assert sec is not None
    assert "who controls each end" in sec
    assert "inbound" in sec and "outbound" in sec
    assert "state the mirror" in sec
