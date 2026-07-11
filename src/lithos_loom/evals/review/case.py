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
from ...plugins.story_develop.profiles import UnknownProfileError, get_profile

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
    # #193: a head defined as a ``.patch`` applied to ``base`` at runtime, instead
    # of a pinned sha (so a case needs no off-branch commit + tag). The filename
    # is relative to ``case_dir``; the harness materialises an ephemeral commit and
    # fills ``head`` / ``known_good_head`` with its sha. ``load_case`` enforces
    # exactly one of head / head_patch (and likewise for the known-good).
    head_patch: str | None = None
    known_good_head_patch: str | None = None
    case_dir: Path | None = None


def load_case(case_dir: Path) -> Case:
    """Load and validate the case in *case_dir* (``case.toml`` + the AC file)."""
    data = tomllib.loads((case_dir / "case.toml").read_text(encoding="utf-8"))
    case = data.get("case", {})

    required = ("id", "base")
    missing = [k for k in required if not case.get(k)]
    if missing:
        raise ValueError(f"case {case_dir.name}: missing required field(s) {missing}")

    # The buggy head is exactly one of a sha (`head`) or a runtime patch
    # (`head_patch`, #193). The sha form fills `head`; the patch form leaves it
    # "" (the harness fills it with the ephemeral commit's sha at run time).
    head, head_patch = _head_spec(
        case_dir, case.get("id"), "head", case.get("head"), case.get("head_patch")
    )

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
    try:
        get_profile(profile)
    except UnknownProfileError as exc:
        raise ValueError(f"case {case.get('id')}: {exc}") from exc
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

    # The optional known-good pair: if the [known_good] table is present it, too,
    # is exactly one of head / head_patch; an absent table means no known-good.
    known_good = data.get("known_good", {})
    if known_good:
        kg_head, kg_head_patch = _head_spec(
            case_dir,
            case.get("id"),
            "known_good.head",
            known_good.get("head"),
            known_good.get("head_patch"),
        )
    else:
        kg_head, kg_head_patch = "", None
    known_good_base = known_good.get("base")

    return Case(
        id=str(case["id"]),
        description=str(case.get("description", "")),
        repo=str(case.get("repo", ".")),
        base=str(case["base"]),
        head=head,
        acceptance_criteria=acceptance,
        personas=personas,
        profile=profile,
        expected=expected,
        known_good_head=kg_head or None,
        known_good_base=str(known_good_base) if known_good_base else None,
        head_patch=head_patch,
        known_good_head_patch=kg_head_patch,
        case_dir=case_dir,
    )


def _head_spec(
    case_dir: Path, case_id: object, label: str, sha: object, patch: object
) -> tuple[str, str | None]:
    """Resolve a head spec to ``(sha, patch_filename)`` — exactly one of the two.

    A case's head is either a pinned commit sha or a ``.patch`` applied to ``base``
    at runtime (#193). The patch form returns ``("", filename)`` (the sha is filled
    in later from the ephemeral commit); the sha form returns ``(sha, None)``. The
    patch file must exist in the case dir (fail closed at load, not hours into the
    live run).
    """
    if sha and patch:
        raise ValueError(
            f"case {case_id}: {label} and {label}_patch are mutually exclusive — "
            "declare exactly one"
        )
    if not sha and not patch:
        raise ValueError(
            f"case {case_id}: declare exactly one of {label} / {label}_patch"
        )
    if patch:
        if not (case_dir / str(patch)).is_file():
            raise ValueError(
                f"case {case_id}: {label}_patch file {str(patch)!r} not found in "
                f"{case_dir.name}"
            )
        return "", str(patch)
    return str(sha), None


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
