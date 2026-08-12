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


# ── patch-based heads (#193): a case can apply a .patch instead of pinning a sha ──

_PATCH_TOML = """
[case]
id = "194-delivery-failure-status"
description = "a failed delivery recorded as succeeded"
repo = "."
base = "aaaaaaaa"
head_patch = "head.patch"
personas = ["correctness"]
profile = "standard"
acceptance_criteria_file = "ac.md"

[[expected]]
file = "src/lithos_loom/plugins/story_develop/daemon_io.py"
keywords = ["delivery", "succeeded"]
min_severity = "critical"
mechanism = "a failed PR delivery is recorded as succeeded"
"""


def _write_patch(case_dir: Path, name: str = "head.patch") -> None:
    # contents are irrelevant at load time — load_case only checks the file exists
    (case_dir / name).write_text("--- a/x\n+++ b/x\n")


def test_loads_a_patch_case(tmp_path: Path) -> None:
    # #193: a case may define its head as a .patch applied at runtime, not a sha.
    case_dir = tmp_path / "194-delivery-failure-status"
    _write_case(case_dir, toml=_PATCH_TOML)
    _write_patch(case_dir)
    case = load_case(case_dir)
    assert case.head_patch == "head.patch"
    assert case.head == ""  # no sha — resolved to an ephemeral commit at run time
    assert case.base == "aaaaaaaa"


def test_rejects_both_head_and_head_patch(tmp_path: Path) -> None:
    toml = _PATCH_TOML.replace(
        'head_patch = "head.patch"', 'head = "bbbbbbbb"\nhead_patch = "head.patch"'
    )
    case_dir = tmp_path / "c"
    _write_case(case_dir, toml=toml)
    _write_patch(case_dir)
    with pytest.raises(ValueError, match="head"):
        load_case(case_dir)


def test_rejects_neither_head_nor_head_patch(tmp_path: Path) -> None:
    toml = _PATCH_TOML.replace('head_patch = "head.patch"\n', "")
    case_dir = tmp_path / "c"
    _write_case(case_dir, toml=toml)
    with pytest.raises(ValueError, match="head"):
        load_case(case_dir)


def test_rejects_missing_head_patch_file(tmp_path: Path) -> None:
    case_dir = tmp_path / "c"
    _write_case(case_dir, toml=_PATCH_TOML)  # the .patch file is NOT written
    with pytest.raises(ValueError, match="head.patch"):
        load_case(case_dir)


def test_loads_known_good_patch(tmp_path: Path) -> None:
    toml = _PATCH_TOML + '\n[known_good]\nhead_patch = "clean.patch"\n'
    case_dir = tmp_path / "c"
    _write_case(case_dir, toml=toml)
    _write_patch(case_dir)
    _write_patch(case_dir, "clean.patch")
    case = load_case(case_dir)
    assert case.known_good_head_patch == "clean.patch"
    assert case.known_good_head is None


def test_rejects_known_good_with_both_head_and_patch(tmp_path: Path) -> None:
    toml = (
        _PATCH_TOML + '\n[known_good]\nhead = "cccccccc"\nhead_patch = "clean.patch"\n'
    )
    case_dir = tmp_path / "c"
    _write_case(case_dir, toml=toml)
    _write_patch(case_dir)
    _write_patch(case_dir, "clean.patch")
    with pytest.raises(ValueError, match="known_good"):
        load_case(case_dir)


# ── shipped cases: the real cases under evals/review/cases/ must stay valid ──
# load_case is pure (TOML + ac.md, no git), so this guard is hermetic — it runs
# in `make check`/CI without fetching the cases' (possibly off-branch) commits.

_SHIPPED_CASES_DIR = Path(__file__).resolve().parents[1] / "evals" / "review" / "cases"


def _shipped_case_dirs() -> list[Path]:
    return sorted(
        d for d in _SHIPPED_CASES_DIR.iterdir() if (d / "case.toml").is_file()
    )


def test_every_shipped_case_loads() -> None:
    # a malformed case.toml (bad sha string aside — not git-checked here — a typo'd
    # profile/persona, a missing [[expected]], an empty AC) must fail the gate, not
    # the live eval hours later.
    dirs = _shipped_case_dirs()
    assert dirs, f"no shipped cases under {_SHIPPED_CASES_DIR}"
    for case_dir in dirs:
        case = load_case(case_dir)  # raises ValueError on any structural problem
        assert case.id == case_dir.name
        assert case.expected, f"{case.id}: at least one [[expected]] is required"


def test_seed_180_is_a_clean_mirror() -> None:
    # The 180 seed is a synthetic clean mirror (ADR 0005, Update 2026-06-24): the
    # known-good is the EXACT reverse of the buggy pair — same two commits, no
    # third (contaminating) commit — so reviewing it adds the guard back on
    # otherwise-clean code and FP is meaningful without the judge. Guard against a
    # regression to a non-mirror known-good that re-introduces a contaminated base.
    case = load_case(_SHIPPED_CASES_DIR / "180-attach-delivery")

    assert case.base and case.head and case.base != case.head
    assert case.known_good_base == case.head, "known-good base must be the buggy head"
    assert case.known_good_head == case.base, "known-good head must be the buggy base"

    expected = case.expected[0]
    assert expected.file == "src/lithos_loom/cli/develop.py"
    assert expected.min_severity == "critical"


# ── ac_provenance: replay vs trimmed vs synthetic fixture AC (#292 review) ──
# A case's ac.md may be the authentic input the original review context had
# ("replay"), an authentic source edited to isolate the measured escape
# ("trimmed"), or written for the fixture ("synthetic"). Declaring it keeps the
# benchmark honest about what a catch/miss on the case measures.


def test_ac_provenance_loads_when_declared(tmp_path: Path) -> None:
    toml = _SEED_TOML.replace('id = "180', 'ac_provenance = "trimmed"\nid = "180')
    case_dir = tmp_path / "c"
    _write_case(case_dir, toml=toml)
    assert load_case(case_dir).ac_provenance == "trimmed"


def test_ac_provenance_defaults_to_none(tmp_path: Path) -> None:
    case_dir = tmp_path / "c"
    _write_case(case_dir, toml=_SEED_TOML)
    assert load_case(case_dir).ac_provenance is None


def test_ac_provenance_rejects_unknown_value(tmp_path: Path) -> None:
    toml = _SEED_TOML.replace('id = "180', 'ac_provenance = "authentic"\nid = "180')
    case_dir = tmp_path / "c"
    _write_case(case_dir, toml=toml)
    with pytest.raises(ValueError, match="ac_provenance"):
        load_case(case_dir)


def test_every_shipped_case_declares_ac_provenance() -> None:
    # EVERY shipped case must say what its AC is — no ID allowlist, so a future
    # case can't silently regress to undocumented provenance. (The loader keeps
    # the field optional only for cases mid-authoring outside the shipped set.)
    for case_dir in _shipped_case_dirs():
        case = load_case(case_dir)
        assert case.ac_provenance is not None, (
            f"{case.id}: declare ac_provenance (replay | trimmed | synthetic)"
        )


# ── tier: saturated regression floor vs discriminating frontier (RH-6) ──
# A "floor" case is saturated (and the panel prompts may have been tuned in its
# presence), so it contributes a regression gate, never headline signal; a
# "frontier" case is what the headline catch-rate is measured over.


def test_tier_loads_when_declared(tmp_path: Path) -> None:
    toml = _SEED_TOML.replace('id = "180', 'tier = "floor"\nid = "180')
    case_dir = tmp_path / "c"
    _write_case(case_dir, toml=toml)
    assert load_case(case_dir).tier == "floor"


def test_tier_defaults_to_none(tmp_path: Path) -> None:
    case_dir = tmp_path / "c"
    _write_case(case_dir, toml=_SEED_TOML)
    assert load_case(case_dir).tier is None


def test_tier_rejects_unknown_value(tmp_path: Path) -> None:
    toml = _SEED_TOML.replace('id = "180', 'tier = "legacy"\nid = "180')
    case_dir = tmp_path / "c"
    _write_case(case_dir, toml=toml)
    with pytest.raises(ValueError, match="tier"):
        load_case(case_dir)


def test_every_shipped_case_declares_tier() -> None:
    # EVERY shipped case must place itself in a tier — no ID allowlist, so a
    # future saturated case can't silently keep padding the headline. (The
    # loader keeps the field optional only for cases mid-authoring outside the
    # shipped set; the CLI treats undeclared as frontier.)
    for case_dir in _shipped_case_dirs():
        case = load_case(case_dir)
        assert case.tier is not None, f"{case.id}: declare tier (floor | frontier)"


# ── artifact cases (RH-3 / #294): seeded rendered-page artifacts ──
# A case may carry the screenshots the approval-hold artifact-review pass would
# have been shown; the harness then measures that pass instead of the diff
# panel. The files are validated at load (fail closed before any paid run).


def _write_artifact(
    case_dir: Path, rel: str = "artifacts/page-800.png", data: bytes = b"\x89PNG..."
) -> Path:
    p = case_dir / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


_ARTIFACT_TOML = _SEED_TOML.replace(
    'id = "180',
    'artifacts_dir = "artifacts"\nartifact_provenance = "captured"\nid = "180',
)


def test_artifact_case_loads(tmp_path: Path) -> None:
    case_dir = tmp_path / "c"
    _write_case(case_dir, toml=_ARTIFACT_TOML)
    _write_artifact(case_dir)
    case = load_case(case_dir)
    assert case.artifacts_dir == "artifacts"
    assert case.artifact_provenance == "captured"


def test_artifact_fields_default_to_none(tmp_path: Path) -> None:
    case_dir = tmp_path / "c"
    _write_case(case_dir, toml=_SEED_TOML)
    case = load_case(case_dir)
    assert case.artifacts_dir is None
    assert case.artifact_provenance is None


def test_artifact_provenance_rejects_unknown_value(tmp_path: Path) -> None:
    toml = _ARTIFACT_TOML.replace(
        'artifact_provenance = "captured"', 'artifact_provenance = "real"'
    )
    case_dir = tmp_path / "c"
    _write_case(case_dir, toml=toml)
    _write_artifact(case_dir)
    with pytest.raises(ValueError, match="artifact_provenance"):
        load_case(case_dir)


def test_artifacts_dir_requires_provenance(tmp_path: Path) -> None:
    # an artifact case must say whether its captures are authentic e2e output
    # or hand-made — the benchmark-honesty rule ac_provenance already applies
    toml = _ARTIFACT_TOML.replace('artifact_provenance = "captured"\n', "")
    case_dir = tmp_path / "c"
    _write_case(case_dir, toml=toml)
    _write_artifact(case_dir)
    with pytest.raises(ValueError, match="artifact_provenance"):
        load_case(case_dir)


def test_provenance_without_artifacts_dir_rejected(tmp_path: Path) -> None:
    # a dangling provenance means the author INTENDED an artifact case; running
    # it as a diff case would silently measure the wrong surface
    toml = _ARTIFACT_TOML.replace('artifacts_dir = "artifacts"\n', "")
    case_dir = tmp_path / "c"
    _write_case(case_dir, toml=toml)
    with pytest.raises(ValueError, match="artifacts_dir"):
        load_case(case_dir)


def test_rejects_missing_artifacts_dir(tmp_path: Path) -> None:
    case_dir = tmp_path / "c"
    _write_case(case_dir, toml=_ARTIFACT_TOML)  # the dir is NOT created
    with pytest.raises(ValueError, match="artifacts"):
        load_case(case_dir)


def test_rejects_empty_artifacts_dir(tmp_path: Path) -> None:
    case_dir = tmp_path / "c"
    _write_case(case_dir, toml=_ARTIFACT_TOML)
    (case_dir / "artifacts").mkdir()
    with pytest.raises(ValueError, match="artifacts"):
        load_case(case_dir)


def test_rejects_empty_artifact_file(tmp_path: Path) -> None:
    # a 0-byte "screenshot" reviews as nothing while the case claims coverage
    case_dir = tmp_path / "c"
    _write_case(case_dir, toml=_ARTIFACT_TOML)
    _write_artifact(case_dir, data=b"")
    with pytest.raises(ValueError, match="empty"):
        load_case(case_dir)


def test_rejects_symlink_in_artifacts_dir(tmp_path: Path) -> None:
    # same hardening posture as the host artifact collector (PR #289): the case
    # dir is data, and a symlink could smuggle content from outside it
    case_dir = tmp_path / "c"
    _write_case(case_dir, toml=_ARTIFACT_TOML)
    _write_artifact(case_dir)
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"x")
    (case_dir / "artifacts" / "link.png").symlink_to(outside)
    with pytest.raises(ValueError, match="symlink"):
        load_case(case_dir)


# ── strict case.toml keys: a typo'd knob must fail, not silently no-op ──
# e.g. a misspelled artifacts_dir would silently degrade the case to a diff-only
# run — the same measured-surface poison the profile/persona checks fail closed on.


def test_rejects_unknown_case_key(tmp_path: Path) -> None:
    toml = _SEED_TOML.replace('id = "180', 'artifcats_dir = "artifacts"\nid = "180')
    case_dir = tmp_path / "c"
    _write_case(case_dir, toml=toml)
    with pytest.raises(ValueError, match="artifcats_dir"):
        load_case(case_dir)


def test_rejects_unknown_top_level_table(tmp_path: Path) -> None:
    toml = _SEED_TOML + '\n[extras]\nnote = "x"\n'
    case_dir = tmp_path / "c"
    _write_case(case_dir, toml=toml)
    with pytest.raises(ValueError, match="extras"):
        load_case(case_dir)


def test_rejects_unknown_expected_key(tmp_path: Path) -> None:
    toml = _SEED_TOML.replace(
        'min_severity = "critical"', 'min_severity = "critical"\nseverity = "major"'
    )
    case_dir = tmp_path / "c"
    _write_case(case_dir, toml=toml)
    with pytest.raises(ValueError, match="severity"):
        load_case(case_dir)


def test_rejects_unknown_known_good_key(tmp_path: Path) -> None:
    toml = _SEED_TOML.replace(
        '[known_good]\nhead = "cccccccc"', '[known_good]\nhead = "cccccccc"\nsha = "d"'
    )
    case_dir = tmp_path / "c"
    _write_case(case_dir, toml=toml)
    with pytest.raises(ValueError, match="sha"):
        load_case(case_dir)


# ── shipped artifact cases: byte budget ──
# Committed screenshots are the repo's first binaries; the budget keeps a case
# from quietly growing past what a clone should carry.

_ARTIFACTS_BUDGET_BYTES = 2 * 1024 * 1024


def test_shipped_artifact_cases_fit_the_byte_budget() -> None:
    for case_dir in _shipped_case_dirs():
        case = load_case(case_dir)
        if case.artifacts_dir is None:
            continue
        total = sum(
            p.stat().st_size
            for p in (case_dir / case.artifacts_dir).rglob("*")
            if p.is_file()
        )
        assert total <= _ARTIFACTS_BUDGET_BYTES, (
            f"{case.id}: artifacts total {total} bytes exceeds the "
            f"{_ARTIFACTS_BUDGET_BYTES}-byte per-case budget — downscale/crop"
        )


def test_at_least_one_shipped_artifact_case_exists() -> None:
    # RH-3 ships the mode WITH a real case (lens22 twin) — this pins that the
    # artifact surface stays represented in the benchmark.
    assert any(load_case(d).artifacts_dir is not None for d in _shipped_case_dirs()), (
        "no shipped artifact case — the artifact-review surface is unmeasured"
    )
