"""Tests for the T6 reviewer-panel config surface.

Covers the ``--develop-config`` TOML loader's validation and the
``effective_reviewers`` legacy fold.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lithos_loom.plugins.story_develop.config import (
    CANONICAL_CHECK_NAMES,
    OVERRIDABLE_CHECK_NAMES,
    STATEABLE_CHECK_NAMES,
    DevelopConfig,
    ReviewerSpec,
    load_develop_config,
    parse_artifacts_path,
    parse_check_commands,
    parse_check_state_pairs,
    parse_check_states,
    parse_effort,
    parse_image,
    parse_model,
    parse_parity_command,
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
        ("[[reviewers]]\nname = 'a'\nmodel = ''\n", "model must be a non-empty string"),
        ("[[reviewers]]\nname = 'a'\nmodel = 3\n", "model must be a non-empty string"),
        (
            "[[reviewers]]\nname = 'a'\neffort = 'ultra'\n",
            "effort must be one of",
        ),
        (
            "[[reviewers]]\nname = 'a'\neffort = 3\n",
            "effort must be one of",
        ),
    ],
)
def test_loader_rejects_bad_schema(tmp_path: Path, body: str, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        load_develop_config(_write(tmp_path, body))


def test_loads_per_reviewer_model_and_effort(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        """
[[reviewers]]
name = "code-quality"
model = "sonnet"
effort = "high"

[[reviewers]]
name = "security"
""",
    )
    cq, sec = load_develop_config(p)
    assert cq.model == "sonnet" and cq.effort == "high"
    assert sec.model is None and sec.effort is None  # default = agent default


@pytest.mark.parametrize("good", ["opus", "claude-opus-4-8", "sonnet"])
def test_parse_model_accepts_non_empty_strings(good: str) -> None:
    assert parse_model(good, where="x") == good


def test_parse_model_none_passes_through() -> None:
    assert parse_model(None, where="x") is None


def test_parse_model_strips_surrounding_whitespace() -> None:
    # validate-on-strip but return-raw would let " opus " reach the CLI verbatim
    assert parse_model("  opus  ", where="x") == "opus"


@pytest.mark.parametrize("bad", ["", "   ", 7, []])
def test_parse_model_rejects_bad(bad: object) -> None:
    with pytest.raises(ValueError, match="model must be a non-empty string"):
        parse_model(bad, where="x")


@pytest.mark.parametrize("good", ["low", "medium", "high", "xhigh", "max"])
def test_parse_effort_accepts_levels(good: str) -> None:
    assert parse_effort(good, where="x") == good


def test_parse_effort_normalises_case_and_whitespace() -> None:
    assert parse_effort("  HIGH ", where="x") == "high"


def test_parse_effort_none_passes_through() -> None:
    assert parse_effort(None, where="x") is None


@pytest.mark.parametrize("bad", ["minimal", "ultra", "none", 3, ""])
def test_parse_effort_rejects_bad(bad: object) -> None:
    # Loom's canonical vocabulary is Claude's; `minimal` (OpenCode/Codex) is
    # not a Claude effort level and is rejected at this layer.
    with pytest.raises(ValueError, match="effort must be one of"):
        parse_effort(bad, where="x")


@pytest.mark.parametrize(
    "good",
    ["ralph-sandbox:latest", "ghcr.io/acme/dev:2026-06", "img@sha256:abc"],
)
def test_parse_image_accepts_non_empty_strings(good: str) -> None:
    assert parse_image(good, where="x") == good


@pytest.mark.parametrize("good", ["e2e/artifacts", "artifacts", "out/shots "])
def test_parse_artifacts_path_accepts_repo_relative_dirs(good: str) -> None:
    assert parse_artifacts_path(good, where="x") == good.strip()


def test_parse_artifacts_path_none_passes_through() -> None:
    assert parse_artifacts_path(None, where="x") is None


@pytest.mark.parametrize("bad", ["", "   ", 7, []])
def test_parse_artifacts_path_rejects_non_strings(bad: object) -> None:
    with pytest.raises(ValueError, match="artifacts path must be a non-empty string"):
        parse_artifacts_path(bad, where="x")


@pytest.mark.parametrize("escapes", ["/abs/path", "../outside", "a/../../b", "a/.."])
def test_parse_artifacts_path_rejects_escaping_paths(escapes: str) -> None:
    # The path is joined onto the check's tree export — it must not be able to
    # point the collector at anything outside it.
    with pytest.raises(ValueError, match="repo-relative without"):
        parse_artifacts_path(escapes, where="x")


def test_parse_artifacts_path_rejects_repo_root() -> None:
    # "." would snapshot the entire exported repo for every check.
    with pytest.raises(ValueError, match="must name a subdirectory"):
        parse_artifacts_path(".", where="x")


def test_parse_image_none_passes_through() -> None:
    assert parse_image(None, where="x") is None


def test_parse_image_strips_surrounding_whitespace() -> None:
    assert parse_image("  ralph-sandbox:latest  ", where="x") == "ralph-sandbox:latest"


@pytest.mark.parametrize("bad", ["", "   ", 7, []])
def test_parse_image_rejects_bad(bad: object) -> None:
    with pytest.raises(ValueError, match="image must be a non-empty string"):
        parse_image(bad, where="x")


# --- parse_check_commands (#273: per-check command override) -----------------


def test_parse_check_commands_none_is_empty() -> None:
    assert parse_check_commands(None, where="x") == {}


def test_parse_check_commands_accepts_and_strips() -> None:
    got = parse_check_commands(
        {"typecheck": "  make typecheck  ", "lint": "make lint"}, where="x"
    )
    assert got == {"typecheck": "make typecheck", "lint": "make lint"}


def test_parse_check_commands_rejects_non_table() -> None:
    with pytest.raises(ValueError, match="must be a table"):
        parse_check_commands("make typecheck", where="x")


@pytest.mark.parametrize("bad", ["", "   ", 7, [], None])
def test_parse_check_commands_rejects_empty_or_nonstring_command(bad: object) -> None:
    with pytest.raises(ValueError, match="must be a non-empty string"):
        parse_check_commands({"typecheck": bad}, where="x")


def test_parse_check_commands_rejects_unknown_check() -> None:
    with pytest.raises(ValueError, match="unknown check"):
        parse_check_commands({"typcheck": "make typecheck"}, where="x")


def test_parse_check_commands_rejects_test_key_steers_to_test_command() -> None:
    # `test` has bespoke detection/selection — steer to test_command, never shadow it.
    with pytest.raises(ValueError, match="test_command"):
        parse_check_commands({"test": "make test"}, where="x")


def test_parse_check_commands_rejects_format_key_as_inert() -> None:
    # `format` is not a standalone gate check (autoformat handles it) → override inert.
    with pytest.raises(ValueError, match="format"):
        parse_check_commands({"format": "ruff format"}, where="x")


def test_canonical_check_names_matches_catalog() -> None:
    # config keeps a LITERAL name-set (importing check_catalog would cycle:
    # config → profiles → check_set → test_gate → config); pin it to the catalog.
    from lithos_loom.plugins.story_develop.check_catalog import CANONICAL_CHECKS

    assert {m.name for m in CANONICAL_CHECKS} == CANONICAL_CHECK_NAMES
    assert CANONICAL_CHECK_NAMES - {"test", "format"} == OVERRIDABLE_CHECK_NAMES


def test_develop_config_check_commands_defaults_empty(tmp_path: Path) -> None:
    cfg = DevelopConfig(repo=tmp_path, description="x", work_dir=tmp_path / "w")
    assert cfg.check_commands == {}


# --- parse_check_states (#273 slice 2: per-check 3-state) ---------------------


def test_parse_check_states_none_is_empty() -> None:
    assert parse_check_states(None, where="x") == {}


def test_parse_check_states_accepts_valid() -> None:
    got = parse_check_states({"sast": "required", "test": "off"}, where="x")
    assert got == {"sast": "required", "test": "off"}


def test_parse_check_states_allows_test_key() -> None:
    # `test` IS stateable — off generalizes the legacy test_gate=false escape hatch.
    assert parse_check_states({"test": "off"}, where="x") == {"test": "off"}


def test_parse_check_states_rejects_non_table() -> None:
    with pytest.raises(ValueError, match="must be a table"):
        parse_check_states("required", where="x")


def test_parse_check_states_rejects_bad_state_value() -> None:
    with pytest.raises(ValueError, match="must be one of"):
        parse_check_states({"sast": "advisory"}, where="x")


@pytest.mark.parametrize("bad", [["off"], {"a": 1}, 3, None])
def test_parse_check_states_rejects_non_string_value(bad: object) -> None:
    # #280 review finding 1: a non-scalar (unhashable) state must raise ValueError, not
    # a TypeError from `state not in <frozenset>` (only ValueError is caught upstream).
    with pytest.raises(ValueError, match="must be one of"):
        parse_check_states({"sast": bad}, where="x")


def test_parse_check_states_rejects_unknown_check() -> None:
    with pytest.raises(ValueError, match="unknown check"):
        parse_check_states({"typcheck": "off"}, where="x")


def test_parse_check_states_rejects_format_key_as_inert() -> None:
    with pytest.raises(ValueError, match="format"):
        parse_check_states({"format": "off"}, where="x")


def test_stateable_check_names_is_canonical_minus_format() -> None:
    assert CANONICAL_CHECK_NAMES - {"format"} == STATEABLE_CHECK_NAMES


def test_parse_check_state_pairs_cli() -> None:
    assert parse_check_state_pairs(["sast=off", "lint=informational"], where="x") == {
        "sast": "off",
        "lint": "informational",
    }


def test_parse_check_state_pairs_rejects_missing_equals() -> None:
    with pytest.raises(ValueError, match="NAME=STATE"):
        parse_check_state_pairs(["sast off"], where="x")


def test_develop_config_check_states_defaults_empty(tmp_path: Path) -> None:
    cfg = DevelopConfig(repo=tmp_path, description="x", work_dir=tmp_path / "w")
    assert cfg.check_states == {}


# --- parse_parity_command (#273 slice 3: aggregate repo-parity check) ---------


def test_parse_parity_command_none_passes_through() -> None:
    assert parse_parity_command(None, where="x") is None


def test_parse_parity_command_strips() -> None:
    assert parse_parity_command("  make check  ", where="x") == "make check"


@pytest.mark.parametrize("bad", ["", "   ", 7, []])
def test_parse_parity_command_rejects_bad(bad: object) -> None:
    with pytest.raises(ValueError, match="parity_command must be a non-empty string"):
        parse_parity_command(bad, where="x")


def test_develop_config_parity_command_defaults_none(tmp_path: Path) -> None:
    cfg = DevelopConfig(repo=tmp_path, description="x", work_dir=tmp_path / "w")
    assert cfg.parity_command is None


# --- codex agent config (#94) -----------------------------------------------


def test_codex_config_dir_defaults_to_home_dotcodex(tmp_path: Path) -> None:
    cfg = DevelopConfig(repo=tmp_path, description="x", work_dir=tmp_path / "w")
    assert cfg.codex_config_dir == Path.home() / ".codex"


# auth_source_dir + the CLAUDE_/CODEX_ auth/mount constants moved to the Engine
# adapter in ARCH-2.E3 — now covered by tests/test_story_develop_engines.py
# (test_auth_source_dir_picks_the_tools_config_dir, test_auth_file_candidates).


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
