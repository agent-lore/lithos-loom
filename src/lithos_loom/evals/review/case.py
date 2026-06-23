"""Review-correctness eval case model + loader (#183).

A *case* is a static tuple of (a change with a known defect, the acceptance
criteria the reviewer receives, the expected finding(s) a correct review must
surface). Cases live as directories under ``evals/review/cases/<id>/`` so adding
one is a small, documented step. The benchmark grows from real misses: every
defect that escapes review becomes a case.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

from ...plugins.story_develop.personas import canonical_personas
from ...plugins.story_develop.profiles import CANONICAL_PROFILES

_SEVERITIES = ("critical", "major", "minor")


@dataclass(frozen=True)
class Expected:
    """A defect a correct review MUST surface.

    A produced finding matches when it touches *file* AND mentions at least one
    of *keywords* (the structured match); *mechanism* is the prose an LLM-judge
    fallback is asked to confirm. *min_severity* is the band the finding must hit
    for the review to be severity-correct.
    """

    file: str
    keywords: tuple[str, ...]
    min_severity: str
    mechanism: str = ""


@dataclass(frozen=True)
class Case:
    """One seeded-defect benchmark case."""

    id: str
    description: str
    repo: str
    base: str
    head: str
    acceptance_criteria: str
    personas: tuple[str, ...]
    profile: str
    expected: tuple[Expected, ...]
    known_good_head: str | None = None
    # The base for the known-good review (defaults to ``base``). Lets a case pair
    # a defect diff with an independent clean diff — e.g. review the *removal* of
    # a fix as the defect, and the fix itself as the known-good.
    known_good_base: str | None = None
    case_dir: Path | None = None


def load_case(case_dir: Path) -> Case:
    """Load and validate the case in *case_dir* (``case.toml`` + the AC file)."""
    data = tomllib.loads((case_dir / "case.toml").read_text(encoding="utf-8"))
    case = data.get("case", {})

    required = ("id", "base", "head")
    missing = [k for k in required if not case.get(k)]
    if missing:
        raise ValueError(f"case {case_dir.name}: missing required field(s) {missing}")

    ac_file = case.get("acceptance_criteria_file", "ac.md")
    acceptance = (case_dir / ac_file).read_text(encoding="utf-8").strip()
    if not acceptance:
        raise ValueError(f"case {case.get('id')}: empty acceptance criteria")

    raw_expected = data.get("expected", [])
    if not raw_expected:
        raise ValueError(
            f"case {case.get('id')}: at least one [[expected]] is required"
        )
    expected = tuple(_parse_expected(case.get("id"), e) for e in raw_expected)

    # Fail closed on a typo'd profile / persona: a silent fallback would measure a
    # DIFFERENT panel or check-set than the case declares, so the reported
    # catch-rate would not describe the panel under test.
    profile = str(case.get("profile", "standard"))
    known_profiles = tuple(p.name for p in CANONICAL_PROFILES)
    if profile not in known_profiles:
        raise ValueError(
            f"case {case.get('id')}: unknown profile {profile!r}; "
            f"known: {', '.join(known_profiles)}"
        )
    personas = tuple(case.get("personas", ()))
    if not personas:
        raise ValueError(
            f"case {case.get('id')}: declare at least one persona (the panel under "
            "test) — an empty panel would silently fall back to the built-in reviewer"
        )
    registry = canonical_personas()
    unknown = [p for p in personas if p not in registry]
    if unknown:
        raise ValueError(
            f"case {case.get('id')}: unknown persona(s) {unknown}; "
            f"known: {', '.join(sorted(registry))}"
        )

    known_good = data.get("known_good", {})
    known_good_head = known_good.get("head")
    known_good_base = known_good.get("base")

    return Case(
        id=str(case["id"]),
        description=str(case.get("description", "")),
        repo=str(case.get("repo", ".")),
        base=str(case["base"]),
        head=str(case["head"]),
        acceptance_criteria=acceptance,
        personas=personas,
        profile=profile,
        expected=expected,
        known_good_head=str(known_good_head) if known_good_head else None,
        known_good_base=str(known_good_base) if known_good_base else None,
        case_dir=case_dir,
    )


def _parse_expected(case_id: str | None, e: dict) -> Expected:
    keywords = tuple(e.get("keywords", ()))
    if not keywords:
        raise ValueError(f"case {case_id}: an [[expected]] needs at least one keyword")
    min_severity = str(e.get("min_severity", "")).lower()
    if min_severity not in _SEVERITIES:
        raise ValueError(
            f"case {case_id}: min_severity must be one of {_SEVERITIES} "
            f"(got {min_severity!r})"
        )
    if not e.get("file"):
        raise ValueError(f"case {case_id}: an [[expected]] needs a file")
    return Expected(
        file=str(e["file"]),
        keywords=keywords,
        min_severity=min_severity,
        mechanism=str(e.get("mechanism", "")),
    )
