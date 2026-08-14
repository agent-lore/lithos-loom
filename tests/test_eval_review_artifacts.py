"""Tests for the eval artifact seeder (RH-3 / #294).

``seed_case_artifacts`` copies a case's checked-in screenshots into the run's
``config.artifacts_dir`` in the layout ``render_artifacts_note`` walks
(``round_01/seeded/…``), so the artifact-review pass sees them exactly as it
would see collector output. The seeder re-validates on the host side — it is a
second writer to a directory that is otherwise host-collector-only by design.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lithos_loom.evals.review.artifacts import seed_case_artifacts
from lithos_loom.evals.review.case import Case, Expected
from lithos_loom.plugins.story_develop.config import DevelopConfig

_EXPECTED = Expected(file="f.py", keywords=("k",), min_severity="major")


def _case(
    case_dir: Path,
    artifacts_dir: str | None = "artifacts",
    *,
    known_good_head: str | None = None,
    known_good_artifacts_dir: str | None = None,
) -> Case:
    return Case(
        id="c",
        description="",
        repo=".",
        base="b",
        head="h",
        acceptance_criteria="ac",
        personas=("correctness",),
        profile="standard",
        expected=(_EXPECTED,),
        case_dir=case_dir,
        artifacts_dir=artifacts_dir,
        artifact_provenance="captured" if artifacts_dir else None,
        known_good_head=known_good_head,
        known_good_artifacts_dir=known_good_artifacts_dir,
    )


def _config(tmp_path: Path) -> DevelopConfig:
    cfg_dir = tmp_path / "fake-claude"
    cfg_dir.mkdir(exist_ok=True)
    return DevelopConfig(
        repo=tmp_path,
        description="eval case c",
        work_dir=tmp_path / "work",
        claude_config_dir=cfg_dir,
    )


def _write(case_dir: Path, rel: str, data: bytes = b"\x89PNG...") -> Path:
    p = case_dir / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def test_seeds_files_preserving_layout(tmp_path: Path) -> None:
    case_dir = tmp_path / "case"
    _write(case_dir, "artifacts/page-800.png")
    _write(case_dir, "artifacts/notes/detail-800.png")
    config = _config(tmp_path)

    n = seed_case_artifacts(_case(case_dir), config, head_sha="h")

    dest = config.artifacts_dir / "round_01" / "seeded"
    assert n == 2
    assert (dest / "page-800.png").is_file()
    assert (dest / "notes" / "detail-800.png").is_file()


def test_seeded_files_are_what_the_note_renders(tmp_path: Path) -> None:
    # the whole point: after seeding, the artifact-pass prompt's note is
    # non-empty and lists the seeded files at their in-container paths
    from lithos_loom.plugins.story_develop.check_artifacts import (
        render_artifacts_note,
    )

    case_dir = tmp_path / "case"
    _write(case_dir, "artifacts/page-800.png")
    config = _config(tmp_path)

    seed_case_artifacts(_case(case_dir), config, head_sha="h")

    note = render_artifacts_note(config)
    assert "page-800.png" in note
    assert "/workspace/.handoff/artifacts/round_01/seeded" in note


def test_seed_requires_an_artifact_case(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="artifacts"):
        seed_case_artifacts(
            _case(tmp_path / "case", artifacts_dir=None),
            _config(tmp_path),
            head_sha="h",
        )


def test_seed_rejects_missing_dir(tmp_path: Path) -> None:
    case_dir = tmp_path / "case"
    case_dir.mkdir()
    with pytest.raises(ValueError, match="artifacts"):
        seed_case_artifacts(_case(case_dir), _config(tmp_path), head_sha="h")


def test_seed_rejects_empty_dir(tmp_path: Path) -> None:
    case_dir = tmp_path / "case"
    (case_dir / "artifacts").mkdir(parents=True)
    with pytest.raises(ValueError, match="artifacts"):
        seed_case_artifacts(_case(case_dir), _config(tmp_path), head_sha="h")


def test_seed_rejects_symlinks(tmp_path: Path) -> None:
    # defence in depth: load_case validates too, but the seeder is the actual
    # host-side writer, so it must not follow a link out of the case dir even
    # if handed an unvalidated Case
    case_dir = tmp_path / "case"
    _write(case_dir, "artifacts/page-800.png")
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"x")
    (case_dir / "artifacts" / "link.png").symlink_to(outside)
    with pytest.raises(ValueError, match="symlink"):
        seed_case_artifacts(_case(case_dir), _config(tmp_path), head_sha="h")


# ── per-variant captures (RH-1): which head is under review decides which
# captures are seeded. Seeding the buggy captures for the known-good head would
# make the false-positive rate measure nothing.


def _paired(case_dir: Path) -> Case:
    _write(case_dir, "artifacts/page-800.png", data=b"\x89PNG-buggy")
    _write(case_dir, "known-good-artifacts/page-800.png", data=b"\x89PNG-fixed")
    return _case(
        case_dir,
        known_good_head="kg",
        known_good_artifacts_dir="known-good-artifacts",
    )


def _seeded_bytes(config: DevelopConfig) -> bytes:
    dest = config.artifacts_dir / "round_01" / "seeded" / "page-800.png"
    return dest.read_bytes()


def test_seeds_the_defect_captures_for_the_buggy_head(tmp_path: Path) -> None:
    config = _config(tmp_path)
    seed_case_artifacts(_paired(tmp_path / "case"), config, head_sha="h")
    assert _seeded_bytes(config) == b"\x89PNG-buggy"


def test_seeds_the_known_good_captures_for_the_known_good_head(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    seed_case_artifacts(_paired(tmp_path / "case"), config, head_sha="kg")
    assert _seeded_bytes(config) == b"\x89PNG-fixed"


def test_seed_rejects_a_known_good_head_without_known_good_captures(
    tmp_path: Path,
) -> None:
    # defence in depth: load_case rejects the pairing gap, but the seeder is
    # the writer — an unvalidated Case must not silently seed the buggy
    # captures for the fixed head
    case_dir = tmp_path / "case"
    _write(case_dir, "artifacts/page-800.png")
    case = _case(case_dir, known_good_head="kg")
    with pytest.raises(ValueError, match="known-good captures"):
        seed_case_artifacts(case, _config(tmp_path), head_sha="kg")


def test_seed_rejects_a_head_matching_both_variants(tmp_path: Path) -> None:
    # PR #306 review (Medium): with head == known_good_head the first branch
    # wins and BOTH variants silently get the defect captures. load_case
    # rejects the case, but the seeder must not resolve the ambiguity by
    # picking one — it is the writer, and the choice decides what gets measured.
    case_dir = tmp_path / "case"
    _write(case_dir, "artifacts/page-800.png", data=b"\x89PNG-buggy")
    _write(case_dir, "known-good-artifacts/page-800.png", data=b"\x89PNG-fixed")
    case = _case(
        case_dir,
        known_good_head="h",  # same as case.head
        known_good_artifacts_dir="known-good-artifacts",
    )
    config = _config(tmp_path)
    with pytest.raises(ValueError, match="both the defect and the known-good"):
        seed_case_artifacts(case, config, head_sha="h")
    assert not config.artifacts_dir.exists()


def test_seed_rejects_an_unrecognised_head(tmp_path: Path) -> None:
    # neither head: seeding *something* would attribute a catch to the wrong
    # variant, so fail closed rather than guess
    config = _config(tmp_path)
    with pytest.raises(ValueError, match="head"):
        seed_case_artifacts(_paired(tmp_path / "case"), config, head_sha="other")
    assert not config.artifacts_dir.exists()


# ── root escapes (#302 review, High): the seeder is the host-side writer, so it
# re-validates the ROOT too — an unvalidated Case must not copy host files from
# outside the case dir into the reviewer-visible mount.


def test_seed_rejects_parent_traversal_root(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_bytes(b"s3cret")
    case_dir = tmp_path / "case"
    case_dir.mkdir()
    config = _config(tmp_path)
    with pytest.raises(ValueError, match=r"\.\."):
        seed_case_artifacts(
            _case(case_dir, artifacts_dir="../outside"), config, head_sha="h"
        )
    assert not config.artifacts_dir.exists()  # nothing copied


def test_seed_rejects_absolute_root(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.png").write_bytes(b"s")
    case_dir = tmp_path / "case"
    case_dir.mkdir()
    with pytest.raises(ValueError, match="absolute"):
        seed_case_artifacts(
            _case(case_dir, artifacts_dir=str(outside)), _config(tmp_path), head_sha="h"
        )


def test_seed_rejects_symlinked_root(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "page.png").write_bytes(b"x")
    case_dir = tmp_path / "case"
    case_dir.mkdir()
    (case_dir / "artifacts").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        seed_case_artifacts(_case(case_dir), _config(tmp_path), head_sha="h")


def test_seed_rejects_blank_root(tmp_path: Path) -> None:
    # Path("") is the case dir itself — the shared root check rejects it at
    # the seeder too, not only at load
    case_dir = tmp_path / "case"
    case_dir.mkdir()
    (case_dir / "case.toml").write_text("x")
    with pytest.raises(ValueError, match="blank"):
        seed_case_artifacts(
            _case(case_dir, artifacts_dir=""), _config(tmp_path), head_sha="h"
        )


def test_seed_rejects_symlinked_root_component(tmp_path: Path) -> None:
    case_dir = tmp_path / "case"
    real = case_dir / "real" / "shots"
    real.mkdir(parents=True)
    (real / "page.png").write_bytes(b"x")
    (case_dir / "alias").symlink_to(case_dir / "real", target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        seed_case_artifacts(
            _case(case_dir, artifacts_dir="alias/shots"),
            _config(tmp_path),
            head_sha="h",
        )


def test_seed_rejects_empty_file(tmp_path: Path) -> None:
    # #302 review (Low): the loader rejects 0-byte captures; the seeder must
    # too — a file truncated after load (or an unvalidated Case) would seed a
    # "reviewable" artifact that reviews as nothing
    case_dir = tmp_path / "case"
    _write(case_dir, "artifacts/page-800.png", data=b"")
    with pytest.raises(ValueError, match="empty"):
        seed_case_artifacts(_case(case_dir), _config(tmp_path), head_sha="h")
