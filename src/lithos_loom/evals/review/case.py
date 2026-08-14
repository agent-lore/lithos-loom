"""Review-correctness eval case model + loader (#183).

A *case* is a static tuple of (a change with a known defect, the acceptance
criteria the reviewer receives, the expected finding(s) a correct review must
surface). Cases live as directories under ``evals/review/cases/<id>/`` so adding
one is a small, documented step. The benchmark grows from real misses: every
defect that escapes review becomes a case.
"""

from __future__ import annotations

import filecmp
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
_KNOWN_GOOD_KEYS = frozenset({"base", "head", "head_patch", "artifacts_dir"})
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
    # RH-1: the SAME pages captured at the known-good head. The seeder picks the
    # variant matching the head under review, which is what makes an artifact
    # case's false-positive rate mean anything — reviewing the fixed code
    # against the buggy captures would measure the captures, not the review.
    # Required when an artifact case declares a [known_good] head.
    known_good_artifacts_dir: str | None = None


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

    # The known-good arm exists to review DIFFERENT code. Declaring the same
    # head (or the same patch, which builds the same tree) makes every reported
    # false positive really a catch — silently, after paid runs. #306 review.
    if kg_head and kg_head == head:
        raise ValueError(
            f"case {case.get('id')}: [known_good] declares the same head as the "
            "case — the false-positive arm would review the defect itself"
        )
    if kg_head_patch and kg_head_patch == head_patch:
        raise ValueError(
            f"case {case.get('id')}: [known_good] declares the same patch as the "
            "case — both heads would build identical trees, so the "
            "false-positive arm would review the defect itself"
        )

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
    kg_artifacts_dir = known_good.get("artifacts_dir")
    _validate_artifacts(
        case_dir,
        case.get("id"),
        artifacts_dir,
        artifact_provenance,
        kg_artifacts_dir,
        has_known_good=bool(kg_head or kg_head_patch),
    )

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
        known_good_artifacts_dir=(str(kg_artifacts_dir) if kg_artifacts_dir else None),
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


def resolve_artifacts_root(
    case_dir: Path, artifacts_dir: str, case_id: object, *, label: str = "artifacts_dir"
) -> Path:
    """Resolve + validate a case's artifact root — the ONE root check (#302 review).

    The root must be a real, non-symlink directory that stays inside the case
    dir: an absolute path, a ``..`` component, or a symlinked root would expose
    arbitrary host files to the reviewer container (and its provider). Shared
    by the loader and the seeder so the two can never drift.
    """
    if not artifacts_dir.strip():
        # Path("") is the case dir itself — whose case.toml/ac.md would satisfy
        # the walk while the Case constructor normalises "" to None, silently
        # running a DIFF review under a "captured" provenance claim.
        raise ValueError(
            f"case {case_id}: {label} must not be blank — name a real "
            "directory inside the case dir"
        )
    rel = Path(artifacts_dir)
    if rel.is_absolute():
        raise ValueError(
            f"case {case_id}: {label} must be relative to the case dir "
            f"(got absolute path {artifacts_dir!r})"
        )
    if ".." in rel.parts:
        raise ValueError(
            f"case {case_id}: {label} must not traverse outside the case "
            f"dir ('..' in {artifacts_dir!r})"
        )
    # "No symlinks anywhere" includes every component of the DECLARED path —
    # alias/shots with alias a symlink (even to a directory inside the case)
    # is a mutable validation boundary, not a real directory.
    probe = case_dir
    for part in rel.parts:
        probe = probe / part
        if probe.is_symlink():
            raise ValueError(
                f"case {case_id}: {label} {artifacts_dir!r} contains a "
                f"symlink component ({part!r}) — every component must be a "
                "real directory inside the case dir"
            )
    root = case_dir / rel
    if not root.is_dir():
        raise ValueError(
            f"case {case_id}: {label} {artifacts_dir!r} is not a "
            f"directory in {case_dir.name}"
        )
    if not root.resolve().is_relative_to(case_dir.resolve()):
        raise ValueError(
            f"case {case_id}: {label} {artifacts_dir!r} resolves outside the case dir"
        )
    return root


def iter_artifact_files(
    root: Path, case_id: object, *, label: str = "artifact"
) -> list[Path]:
    """Every artifact file under a *validated* root, fail-closed (#302 review).

    Rejects symlinks anywhere (files AND intermediate directories), non-regular
    and empty files, and anything resolving outside the root; requires ≥1 file.
    The single walk shared by the loader, the seeder, and the CLI summary so
    the rules cannot diverge again.
    """
    resolved_root = root.resolve()
    files: list[Path] = []
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root)
        if p.is_symlink():
            raise ValueError(
                f"case {case_id}: {label} {rel} is a symlink — artifacts must "
                "be regular files inside the case dir"
            )
        if p.is_dir():
            continue
        if not p.is_file():
            raise ValueError(f"case {case_id}: {label} {rel} is not a regular file")
        if p.stat().st_size == 0:
            raise ValueError(f"case {case_id}: {label} {rel} is empty")
        if not p.resolve().is_relative_to(resolved_root):
            raise ValueError(f"case {case_id}: {label} {rel} escapes the case dir")
        files.append(p)
    if not files:
        raise ValueError(f"case {case_id}: {label}s dir contains no files")
    return files


def _validate_artifacts(
    case_dir: Path,
    case_id: object,
    artifacts_dir: object,
    provenance: object,
    known_good_artifacts_dir: object,
    *,
    has_known_good: bool,
) -> None:
    """Validate the RH-3 artifact declaration set, fail-closed at load.

    Both-or-neither: a dir without provenance leaves the benchmark unable to
    say what a catch measures; provenance without a dir means the author
    intended an artifact case that would silently run as a diff case. Root and
    files go through the shared :func:`resolve_artifacts_root` /
    :func:`iter_artifact_files` checks (host-collector posture, PR #289).

    A known-good head needs its **own** captures (RH-1). ``run_case`` reviews
    that head with the same Case, so without them the fixed code would be
    reviewed against the buggy captures and the false-positive number would
    measure nothing — which is why #302 rejected the pairing outright. Both
    variants share one ``artifact_provenance``: they are the same pages from
    the same recipe at two heads.
    """
    if provenance is not None and provenance not in _ARTIFACT_PROVENANCES:
        raise ValueError(
            f"case {case_id}: artifact_provenance must be one of "
            f"{_ARTIFACT_PROVENANCES} (got {provenance!r})"
        )
    if artifacts_dir is None and known_good_artifacts_dir is not None:
        raise ValueError(
            f"case {case_id}: [known_good] artifacts_dir without [case] "
            "artifacts_dir — a diff case has no artifact surface to compare "
            "against; declare the defect captures or drop the known-good ones"
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
    if has_known_good and known_good_artifacts_dir is None:
        raise ValueError(
            f"case {case_id}: [known_good] on an artifact case requires its own "
            "known-good captures ([known_good] artifacts_dir) — the fixed head "
            "reviewed against the buggy captures would make the false-positive "
            "rate meaningless"
        )
    if known_good_artifacts_dir is not None and not has_known_good:
        raise ValueError(
            f"case {case_id}: [known_good] artifacts_dir without a known-good "
            "head — declare [known_good] head / head_patch or drop the captures"
        )
    root = resolve_artifacts_root(case_dir, str(artifacts_dir), case_id)
    files = iter_artifact_files(root, case_id)
    if known_good_artifacts_dir is not None:
        kg_root = resolve_artifacts_root(
            case_dir,
            str(known_good_artifacts_dir),
            case_id,
            label="[known_good] artifacts_dir",
        )
        kg_files = iter_artifact_files(kg_root, case_id, label="known-good artifact")
        _validate_capture_pairing(case_id, root, kg_root, files, kg_files)


def _validate_capture_pairing(
    case_id: object,
    root: Path,
    kg_root: Path,
    files: list[Path],
    kg_files: list[Path],
) -> None:
    """Two independently-valid capture roots are not yet a PAIR (#306 review).

    The pairing contract is "the same pages, from the same recipe, at two
    heads" — the reviewer sees only the images, so anything that differs
    between the variants other than the defect is a signal it could read
    instead. Each rejection below is a way to report a false-positive rate that
    measures the captures rather than the review:

    - **one directory for both variants** — the known-good arm is shown the
      defect renders, exactly what #302 fail-closed on;
    - **a nested root** — the outer root's recursive walk swallows the inner
      variant's files, so the "defect" set contains known-good renders;
    - **different relative paths** — e.g. two defect viewports against one
      known-good viewport compares different stimuli;
    - **byte-identical captures** — the surface under measurement cannot tell
      the heads apart at all.
    """
    resolved, kg_resolved = root.resolve(), kg_root.resolve()
    if resolved == kg_resolved:
        raise ValueError(
            f"case {case_id}: both variants declare the same artifacts "
            f"directory ({root.name!r}) — they must be distinct, or the "
            "known-good arm reviews the defect captures"
        )
    if resolved.is_relative_to(kg_resolved) or kg_resolved.is_relative_to(resolved):
        raise ValueError(
            f"case {case_id}: the variants' artifacts directories are nested "
            f"({root.name!r} / {kg_root.name!r}) — the outer one's walk would "
            "include the inner one's captures"
        )
    rel = {p.relative_to(root).as_posix() for p in files}
    kg_rel = {p.relative_to(kg_root).as_posix() for p in kg_files}
    if rel != kg_rel:
        raise ValueError(
            f"case {case_id}: the variants must capture the same relative "
            f"paths (the same pages at the same widths); only one side has "
            f"{sorted(rel ^ kg_rel)}"
        )
    if all(filecmp.cmp(root / r, kg_root / r, shallow=False) for r in sorted(rel)):
        raise ValueError(
            f"case {case_id}: the variants' captures are byte-identical — the "
            "artifact surface cannot tell the two heads apart, so the "
            "false-positive arm would review the defect render"
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
