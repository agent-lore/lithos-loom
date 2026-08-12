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

# What a case's ac.md IS relative to the escape it replays (#292 review):
#   "replay"    — the authentic criteria the original review context had;
#   "trimmed"   — an authentic source edited to isolate the measured escape
#                 (the description must document every trim);
#   "synthetic" — written for the fixture (no authentic AC existed, e.g. a
#                 hand-developed PR that never went through a panel).
# Declaring it keeps the benchmark honest about what a catch/miss measures.
# The loader treats it as optional (mid-authoring), but the shipped-case gate
# test requires it on every case under evals/review/cases/.
_AC_PROVENANCES = ("replay", "trimmed", "synthetic")

# Which benchmark tier a case scores in (RH-6):
#   "floor"    — saturated (and the panel prompts may have been tuned in its
#                presence, e.g. the #181 arc), so it contributes only a
#                regression gate: below-bar = hard failure of the whole run;
#   "frontier" — discriminating; the headline catch-rate pools frontier cases
#                only, so floor saturation can't flatter an A/B.
# (Unrelated to the check-set "floor" in profiles.py.) The loader treats it as
# optional (mid-authoring), but the shipped-case gate test requires it, and the
# CLI treats undeclared as frontier — a case never opts INTO the floor silently.
_TIERS = ("floor", "frontier")

# What an artifact case's checked-in captures ARE (RH-3 / #294):
#   "captured"  — real e2e output rendered at the case head (the authentic
#                 evidence the live artifact pass would have been shown);
#   "synthetic" — hand-made renders (no authentic capture existed).
# Mirrors ac_provenance's honesty rule; required whenever artifacts_dir is set.
_ARTIFACT_PROVENANCES = ("captured", "synthetic")

# Strict case.toml vocabulary: a typo'd knob (e.g. a misspelled artifacts_dir)
# would silently run the case on a DIFFERENT surface than it claims to measure —
# the same fail-closed rule the profile/persona checks apply.
_CASE_KEYS = frozenset(
    {
        "id",
        "description",
        "repo",
        "base",
        "head",
        "head_patch",
        "acceptance_criteria_file",
        "personas",
        "profile",
        "ac_provenance",
        "tier",
        "artifacts_dir",
        "artifact_provenance",
    }
)
_TOP_LEVEL_KEYS = frozenset({"case", "expected", "known_good"})
_KNOWN_GOOD_KEYS = frozenset({"base", "head", "head_patch"})
_EXPECTED_KEYS = frozenset({"file", "keywords", "min_severity", "mechanism"})


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
    # See _AC_PROVENANCES; None = undeclared (allowed only mid-authoring).
    ac_provenance: str | None = None
    # See _TIERS; None = undeclared (allowed only mid-authoring).
    tier: str | None = None
    # RH-3 (#294): a case-dir-relative directory of checked-in rendered-page
    # captures. When set, the harness seeds them into the run's artifacts dir
    # and measures the approval-hold ARTIFACT-REVIEW pass instead of the diff
    # panel. Validated at load: exists, non-empty, regular files only.
    artifacts_dir: str | None = None
    # See _ARTIFACT_PROVENANCES; required whenever artifacts_dir is set.
    artifact_provenance: str | None = None


def load_case(case_dir: Path) -> Case:
    """Load and validate the case in *case_dir* (``case.toml`` + the AC file)."""
    data = tomllib.loads((case_dir / "case.toml").read_text(encoding="utf-8"))
    case = data.get("case", {})
    _reject_unknown_keys(case_dir.name, data, case)

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

    ac_provenance = case.get("ac_provenance")
    if ac_provenance is not None and ac_provenance not in _AC_PROVENANCES:
        raise ValueError(
            f"case {case.get('id')}: ac_provenance must be one of "
            f"{_AC_PROVENANCES} (got {ac_provenance!r})"
        )

    tier = case.get("tier")
    if tier is not None and tier not in _TIERS:
        raise ValueError(
            f"case {case.get('id')}: tier must be one of {_TIERS} (got {tier!r})"
        )

    artifacts_dir = case.get("artifacts_dir")
    artifact_provenance = case.get("artifact_provenance")
    _validate_artifacts(case_dir, case.get("id"), artifacts_dir, artifact_provenance)

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
        ac_provenance=str(ac_provenance) if ac_provenance else None,
        tier=str(tier) if tier else None,
        artifacts_dir=str(artifacts_dir) if artifacts_dir else None,
        artifact_provenance=str(artifact_provenance) if artifact_provenance else None,
    )


def _reject_unknown_keys(case_name: str, data: dict, case: dict) -> None:
    """Fail closed on any unknown ``case.toml`` key at any level.

    tomllib has no unknown-key notion, so ``case.get(...)`` would silently
    ignore a typo — and a typo'd ``artifacts_dir`` runs the case as a diff-only
    review that measures a different surface than the case declares.
    """

    def _check(scope: str, keys: set[str], known: frozenset[str]) -> None:
        unknown = sorted(keys - known)
        if unknown:
            raise ValueError(
                f"case {case_name}: unknown {scope} key(s) {unknown}; "
                f"known: {', '.join(sorted(known))}"
            )

    _check("top-level", set(data), _TOP_LEVEL_KEYS)
    _check("[case]", set(case), _CASE_KEYS)
    _check("[known_good]", set(data.get("known_good", {})), _KNOWN_GOOD_KEYS)
    for e in data.get("expected", []):
        _check("[[expected]]", set(e), _EXPECTED_KEYS)


def _validate_artifacts(
    case_dir: Path, case_id: object, artifacts_dir: object, provenance: object
) -> None:
    """Validate the RH-3 artifact declaration pair, fail-closed at load.

    Both-or-neither: a dir without provenance leaves the benchmark unable to
    say what a catch measures; provenance without a dir means the author
    intended an artifact case that would silently run as a diff case. The files
    themselves must be regular and symlink-free (the seeder is a host-side
    writer — same hardening posture as the artifact collector, PR #289) and
    non-empty (a 0-byte capture reviews as nothing while claiming coverage).
    """
    if provenance is not None and provenance not in _ARTIFACT_PROVENANCES:
        raise ValueError(
            f"case {case_id}: artifact_provenance must be one of "
            f"{_ARTIFACT_PROVENANCES} (got {provenance!r})"
        )
    if artifacts_dir is None and provenance is None:
        return
    if artifacts_dir is None:
        raise ValueError(
            f"case {case_id}: artifact_provenance without artifacts_dir — declare "
            "the artifacts directory or drop the provenance"
        )
    if provenance is None:
        raise ValueError(
            f"case {case_id}: artifacts_dir requires artifact_provenance "
            f"({' | '.join(_ARTIFACT_PROVENANCES)})"
        )
    root = case_dir / str(artifacts_dir)
    if not root.is_dir():
        raise ValueError(
            f"case {case_id}: artifacts_dir {str(artifacts_dir)!r} is not a "
            f"directory in {case_dir.name}"
        )
    files = [p for p in sorted(root.rglob("*")) if not p.is_dir() or p.is_symlink()]
    if not files:
        raise ValueError(
            f"case {case_id}: artifacts_dir {str(artifacts_dir)!r} contains no files"
        )
    for p in files:
        rel = p.relative_to(root)
        if p.is_symlink():
            raise ValueError(
                f"case {case_id}: artifact {rel} is a symlink — artifacts must be "
                "regular files inside the case dir"
            )
        if not p.is_file():
            raise ValueError(f"case {case_id}: artifact {rel} is not a regular file")
        if p.stat().st_size == 0:
            raise ValueError(f"case {case_id}: artifact {rel} is empty")


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
