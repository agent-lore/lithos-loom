"""``daemon_io.layer_run_settings`` / ``story_config_overrides`` — the settings
layering the daemon path and an on-demand ``--story`` run share."""

from __future__ import annotations

from lithos_loom.plugins.story_develop.config import DEFAULT_IMAGE, ReviewerSpec
from lithos_loom.plugins.story_develop.daemon_io import (
    BUILTIN_REVIEWERS,
    ProjectDevelopSettings,
    layer_run_settings,
    story_config_overrides,
)


def test_layering_applies_profile_panel_and_default_models() -> None:
    settings = ProjectDevelopSettings(review_profile_project="thorough")
    layered = layer_run_settings(
        settings,
        host_default_profile="standard",
        unknown_profile="halt",
        default_models={"codex": "gpt-x", "claude": "claude-x"},
    )
    assert layered.review_profile == "thorough"
    assert layered.reviewers is not BUILTIN_REVIEWERS  # the profile's panel
    assert all(s.model for s in layered.reviewers)  # default models filled
    assert layered.coder_model in {"gpt-x", "claude-x"}


def test_layering_task_profile_beats_project_and_explicit_panel_stays() -> None:
    panel = (ReviewerSpec(name="tests", tool="claude"),)
    settings = ProjectDevelopSettings(
        review_profile_project="thorough",
        review_profile_task="standard",
        reviewers=panel,
        reviewers_explicit=True,
    )
    layered = layer_run_settings(
        settings,
        host_default_profile=None,
        unknown_profile="halt",
        default_models={"claude": "claude-x"},
    )
    assert layered.review_profile == "standard"
    assert [s.name for s in layered.reviewers] == ["tests"]


def test_overrides_carry_only_what_the_story_pinned() -> None:
    bare = story_config_overrides(ProjectDevelopSettings())
    # always present: the resolved agents + profile (they always resolve)
    assert set(bare) == {
        "coder",
        "coder_model",
        "coder_effort",
        "reviewers",
        "review_profile",
        "artifacts_path",
    }
    pinned = story_config_overrides(
        ProjectDevelopSettings(
            max_rounds=8,
            max_cost_usd=20.0,
            test_gate=False,
            test_command="make check",
            check_commands={"lint": "ruff"},
            check_states={"lint": "required"},
            parity_command="make parity",
            image="img:x",
            fallback_chain=("codex",),
        )
    )
    assert pinned["max_rounds"] == 8
    assert pinned["max_cost_usd"] == 20.0
    assert pinned["test_gate"] is False
    assert pinned["test_command"] == "make check"
    assert pinned["check_commands"] == {"lint": "ruff"}
    assert pinned["check_states"] == {"lint": "required"}
    assert pinned["parity_command"] == "make parity"
    assert pinned["image"] == "img:x"
    assert pinned["reviewer_fallback_chain"] == ("codex",)
    assert DEFAULT_IMAGE not in pinned.values()
