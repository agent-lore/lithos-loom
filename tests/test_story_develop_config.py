"""Tests for the T6 reviewer-panel config surface.

Covers the ``--develop-config`` TOML loader's validation and the
``effective_reviewers`` legacy fold.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lithos_loom.plugins.story_develop.config import (
    DevelopConfig,
    ReviewerSpec,
    load_develop_config,
)


def _write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "develop.toml"
    p.write_text(text)
    return p


def test_loads_full_specs(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        """
[[reviewers]]
name = "code-quality"

[[reviewers]]
name = "security"
block_threshold = "minor"
system_prompt = "Hunt for injection and authz issues."
fallback_chain = ["codex"]
tool = "claude"
""",
    )
    specs = load_develop_config(p)
    assert [s.name for s in specs] == ["code-quality", "security"]
    cq, sec = specs
    assert cq.block_threshold == "major"  # default
    assert cq.system_prompt is None and cq.fallback_chain == ()
    assert sec.block_threshold == "minor"
    assert sec.system_prompt is not None and "injection" in sec.system_prompt
    assert sec.fallback_chain == ("codex",)


@pytest.mark.parametrize(
    ("body", "match"),
    [
        ("", r"at least one \[\[reviewers\]\]"),
        ("[[reviewers]]\nname = 'Bad Name'\n", "must be a lowercase"),
        ("[[reviewers]]\n", "must be a lowercase"),  # missing name
        (
            "[[reviewers]]\nname = 'a'\n[[reviewers]]\nname = 'a'\n",
            "duplicate reviewer name",
        ),
        (
            "[[reviewers]]\nname = 'a'\nblock_threshold = 'fatal'\n",
            "block_threshold must be one of",
        ),
        ("[[reviewers]]\nname = 'a'\nfocus = 'x'\n", "unknown keys"),
        (
            "[[reviewers]]\nname = 'a'\nfallback_chain = 'codex'\n",
            "fallback_chain must be a list",
        ),
        ("[[reviewers]]\nname = 'a'\nsystem_prompt = 3\n", "system_prompt must be"),
    ],
)
def test_loader_rejects_bad_schema(tmp_path: Path, body: str, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        load_develop_config(_write(tmp_path, body))


def test_loader_rejects_invalid_toml(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="cannot read develop config"):
        load_develop_config(_write(tmp_path, "this is [not toml"))


def test_loader_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="cannot read develop config"):
        load_develop_config(tmp_path / "nope.toml")


# --- effective_reviewers ------------------------------------------------------


def test_effective_reviewers_folds_legacy_fields(tmp_path: Path) -> None:
    cfg = DevelopConfig(
        repo=tmp_path,
        description="x",
        work_dir=tmp_path / "w",
        reviewer="my-reviewer",
        block_threshold="minor",
        reviewer_fallback_chain=("codex",),
    )
    (spec,) = cfg.effective_reviewers
    assert spec == ReviewerSpec(
        name="my-reviewer",
        tool="claude",
        block_threshold="minor",
        fallback_chain=("codex",),
    )


def test_effective_reviewers_prefers_explicit_specs(tmp_path: Path) -> None:
    specs = (ReviewerSpec(name="a"), ReviewerSpec(name="b"))
    cfg = DevelopConfig(
        repo=tmp_path,
        description="x",
        work_dir=tmp_path / "w",
        reviewer="ignored-legacy-name",
        reviewers=specs,
    )
    assert cfg.effective_reviewers == specs
