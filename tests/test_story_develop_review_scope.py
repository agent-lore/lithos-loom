"""R1 (34bb82c4): the operational model findings are judged in."""

from __future__ import annotations

from pathlib import Path

import pytest

from lithos_loom.plugins.story_develop import review_scope
from lithos_loom.plugins.story_develop.config import DevelopConfig
from lithos_loom.plugins.story_develop.review_scope import (
    MAX_REVIEW_SCOPE_CHARS,
    coder_block,
    coder_route,
    parse_review_scope,
    reviewer_block,
)

_LOOM = (
    "Single operator. `lithos-loom develop …` and other hand-run commands are "
    "run by that operator, one at a time. Out of the model: a second host or a "
    "second daemon, two concurrent invocations of the same hand-run command."
)


def _config(tmp_path: Path, scope: str | None) -> DevelopConfig:
    return DevelopConfig(
        repo=tmp_path, description="x", work_dir=tmp_path / "w", review_scope=scope
    )


# --- parse --------------------------------------------------------------------


def test_parse_strips_and_cleans() -> None:
    assert parse_review_scope("  model‮ text \n", where="w") == "model text"
    assert parse_review_scope(None, where="w") is None


@pytest.mark.parametrize("bad", ["", "  ", 3, {"a": 1}])
def test_parse_rejects_non_text(bad: object) -> None:
    with pytest.raises(ValueError, match="develop_review_scope"):
        parse_review_scope(bad, where="w")


def test_parse_rejects_oversized_text() -> None:
    with pytest.raises(ValueError, match=str(MAX_REVIEW_SCOPE_CHARS)):
        parse_review_scope("x" * (MAX_REVIEW_SCOPE_CHARS + 1), where="w")
    assert parse_review_scope("x" * MAX_REVIEW_SCOPE_CHARS, where="w")


# --- blocks -------------------------------------------------------------------


def test_unset_scope_renders_nothing(tmp_path: Path) -> None:
    config = _config(tmp_path, None)
    assert reviewer_block(config) == ""
    assert coder_block(config) == ""
    assert coder_route(config, decisions_enabled=True) == ""
    assert coder_route(config, decisions_enabled=False) == ""


def test_reviewer_block_states_the_model_and_the_severity_rule(
    tmp_path: Path,
) -> None:
    block = reviewer_block(_config(tmp_path, _LOOM))
    assert "## Operational model (the scope findings are judged in)" in block
    assert _LOOM in block
    assert "**minor at most**" in block
    # in-model lifecycles keep their severity (a1817376 addendum)
    assert "keeps its full severity" in block
    assert "acceptance criteria can widen the model" in block
    assert "quote the criterion or model line" in block


def test_coder_block_builds_for_the_model(tmp_path: Path) -> None:
    block = coder_block(_config(tmp_path, _LOOM))
    assert _LOOM in block
    assert "do not add protocol for actors it does not name" in block


def test_story_develop_route_is_a_needs_decision(tmp_path: Path) -> None:
    raw = coder_route(_config(tmp_path, _LOOM), decisions_enabled=True)
    # indented as a paragraph of the coder prompts' numbered step 1
    assert all(line.startswith("   ") for line in raw.strip("\n").splitlines())
    route = " ".join(raw.split())
    assert "`needs-decision`" in route
    assert "in scope for" in route
    assert "widen the model and fix" in route
    assert "keep the model and record the limit" in route
    # the in-model contract-depth question (a1817376) takes the same exit
    assert "how deep" in route


def test_converge_route_is_a_dispute_citing_the_model(tmp_path: Path) -> None:
    route = " ".join(
        coder_route(_config(tmp_path, _LOOM), decisions_enabled=False).split()
    )
    assert "`disputed`" in route
    assert "needs-decision" not in route
    assert "quote" in route and "operational model" in route


def test_blocks_are_stable_text(tmp_path: Path) -> None:
    # The blocks are module text, not template files: keep them free of slots
    # render_prompt would try to fill.
    for text in (
        review_scope.reviewer_block(_config(tmp_path, _LOOM)),
        review_scope.coder_block(_config(tmp_path, _LOOM)),
        review_scope.coder_route(_config(tmp_path, _LOOM), decisions_enabled=True),
    ):
        assert "{" not in text.replace(_LOOM, "")


@pytest.mark.parametrize(
    "text", ["see {findings}", "{handoff_file}", "{ scope_route }"]
)
def test_parse_rejects_slot_like_text(text: str) -> None:
    with pytest.raises(ValueError, match="slot"):
        parse_review_scope(text, where="w")


def test_parse_keeps_ordinary_braces() -> None:
    # JSON-ish or set notation is fine — only `{name}` slot shapes collide.
    assert parse_review_scope('metadata like {"a": 1} is opaque', where="w")
