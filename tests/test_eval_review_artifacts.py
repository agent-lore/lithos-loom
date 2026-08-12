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


def _case(case_dir: Path, artifacts_dir: str | None = "artifacts") -> Case:
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

    n = seed_case_artifacts(_case(case_dir), config)

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

    seed_case_artifacts(_case(case_dir), config)

    note = render_artifacts_note(config)
    assert "page-800.png" in note
    assert "/workspace/.handoff/artifacts/round_01/seeded" in note


def test_seed_requires_an_artifact_case(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="artifacts"):
        seed_case_artifacts(
            _case(tmp_path / "case", artifacts_dir=None), _config(tmp_path)
        )


def test_seed_rejects_missing_dir(tmp_path: Path) -> None:
    case_dir = tmp_path / "case"
    case_dir.mkdir()
    with pytest.raises(ValueError, match="artifacts"):
        seed_case_artifacts(_case(case_dir), _config(tmp_path))


def test_seed_rejects_empty_dir(tmp_path: Path) -> None:
    case_dir = tmp_path / "case"
    (case_dir / "artifacts").mkdir(parents=True)
    with pytest.raises(ValueError, match="artifacts"):
        seed_case_artifacts(_case(case_dir), _config(tmp_path))


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
        seed_case_artifacts(_case(case_dir), _config(tmp_path))


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
        seed_case_artifacts(_case(case_dir, artifacts_dir="../outside"), config)
    assert not config.artifacts_dir.exists()  # nothing copied


def test_seed_rejects_absolute_root(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.png").write_bytes(b"s")
    case_dir = tmp_path / "case"
    case_dir.mkdir()
    with pytest.raises(ValueError, match="absolute"):
        seed_case_artifacts(
            _case(case_dir, artifacts_dir=str(outside)), _config(tmp_path)
        )


def test_seed_rejects_symlinked_root(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "page.png").write_bytes(b"x")
    case_dir = tmp_path / "case"
    case_dir.mkdir()
    (case_dir / "artifacts").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        seed_case_artifacts(_case(case_dir), _config(tmp_path))


def test_seed_rejects_blank_root(tmp_path: Path) -> None:
    # Path("") is the case dir itself — the shared root check rejects it at
    # the seeder too, not only at load
    case_dir = tmp_path / "case"
    case_dir.mkdir()
    (case_dir / "case.toml").write_text("x")
    with pytest.raises(ValueError, match="blank"):
        seed_case_artifacts(_case(case_dir, artifacts_dir=""), _config(tmp_path))


def test_seed_rejects_symlinked_root_component(tmp_path: Path) -> None:
    case_dir = tmp_path / "case"
    real = case_dir / "real" / "shots"
    real.mkdir(parents=True)
    (real / "page.png").write_bytes(b"x")
    (case_dir / "alias").symlink_to(case_dir / "real", target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        seed_case_artifacts(
            _case(case_dir, artifacts_dir="alias/shots"), _config(tmp_path)
        )


def test_seed_rejects_empty_file(tmp_path: Path) -> None:
    # #302 review (Low): the loader rejects 0-byte captures; the seeder must
    # too — a file truncated after load (or an unvalidated Case) would seed a
    # "reviewable" artifact that reviews as nothing
    case_dir = tmp_path / "case"
    _write(case_dir, "artifacts/page-800.png", data=b"")
    with pytest.raises(ValueError, match="empty"):
        seed_case_artifacts(_case(case_dir), _config(tmp_path))
