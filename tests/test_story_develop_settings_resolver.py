"""Unit tests for the pure metadata → scalar develop-settings resolver (ARCH-9).

``resolve_scalar_settings`` is the table-driven precedence + parse + friction core
lifted out of ``daemon_io.resolve_project_settings`` (ARCH-9 slice 2). It takes two
plain dicts — project context-doc metadata, task metadata — plus a ``frictions``
list, and returns a frozen :class:`ScalarSettings` with **no I/O**. This is the new
test surface the extraction earns: the precedence table, the two-suffix friction
contract, and the exact append order are pinned here directly. The end-to-end path
(``resolve_project_settings`` through a fake Lithos) stays pinned by
``test_story_develop_daemon.py``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from lithos_loom.plugins.story_develop.settings_resolver import (
    ScalarSettings,
    resolve_scalar_settings,
)


def _resolve(
    meta: Mapping[str, Any] | None = None,
    task: Mapping[str, Any] | None = None,
) -> tuple[ScalarSettings, tuple[str, ...]]:
    frictions: list[str] = []
    settings = resolve_scalar_settings(meta or {}, task or {}, frictions)
    return settings, tuple(frictions)


# ── defaults + happy path ─────────────────────────────────────────────


def test_empty_metadata_yields_defaults_no_friction() -> None:
    settings, frictions = _resolve()
    assert settings == ScalarSettings()
    assert settings.coder == "claude"
    assert frictions == ()


def test_project_layer_sets_every_scalar() -> None:
    settings, frictions = _resolve(
        {
            "develop_image": "ghcr.io/acme/dev:1",
            "develop_test_command": "pytest -q",
            "develop_test_gate": False,
            "develop_coder": {"tool": "codex", "model": "o3", "effort": "high"},
            "develop_fallback_chain": ["claude", "codex"],
            "develop_max_rounds": 8,
            "develop_max_cost_usd": 12.5,
            "develop_review_profile": "thorough",
        }
    )
    assert frictions == ()
    assert settings.image == "ghcr.io/acme/dev:1"
    assert settings.test_command == "pytest -q"
    assert settings.test_gate is False
    assert settings.coder == "codex"
    assert settings.coder_model == "o3"
    assert settings.coder_effort == "high"
    assert settings.fallback_chain == ("claude", "codex")
    assert settings.max_rounds == 8
    assert settings.max_cost_usd == 12.5
    assert settings.review_profile_project == "thorough"


# ── precedence: task overrides project ────────────────────────────────


def test_task_overrides_project_for_coder_model_and_effort() -> None:
    settings, frictions = _resolve(
        {"develop_coder": {"model": "o3", "effort": "low"}},
        {"develop_model": "opus", "develop_effort": "max"},
    )
    assert settings.coder_model == "opus"
    assert settings.coder_effort == "max"
    assert frictions == ()


def test_task_overrides_project_for_image_command_gate() -> None:
    settings, _ = _resolve(
        {
            "develop_image": "img:proj",
            "develop_test_command": "make test",
            "develop_test_gate": True,
        },
        {
            "develop_image": "img:task",
            "develop_test_command": "pytest",
            "develop_test_gate": False,
        },
    )
    assert settings.image == "img:task"
    assert settings.test_command == "pytest"
    assert settings.test_gate is False


def test_task_overrides_project_for_artifacts_path() -> None:
    settings, _ = _resolve(
        {"develop_artifacts_path": "e2e/artifacts"},
        {"develop_artifacts_path": "out/shots"},
    )
    assert settings.artifacts_path == "out/shots"


def test_bad_artifacts_path_frictions_and_keeps_default() -> None:
    settings, frictions = _resolve({"develop_artifacts_path": "../escape"})
    assert settings.artifacts_path is None
    assert frictions == (
        "develop_artifacts_path: artifacts path must be repo-relative without "
        "'..' (got '../escape'); ignoring",
    )


# ── the two-suffix friction contract ──────────────────────────────────


def test_bad_project_value_frictions_and_keeps_default() -> None:
    settings, frictions = _resolve({"develop_image": ""})  # empty string invalid
    assert settings.image is None
    assert frictions == (
        "develop_image: image must be a non-empty string (got ''); ignoring",
    )


@pytest.mark.parametrize(
    ("key", "attr", "project_value", "bad_task_value", "expected_friction"),
    [
        (
            "develop_image",
            "image",
            "img:proj",
            123,
            "task metadata.develop_image: image must be a non-empty string "
            "(got 123); keeping project default",
        ),
        (
            "develop_test_command",
            "test_command",
            "cmd:proj",
            123,
            "task metadata.develop_test_command: test_command must be a non-empty "
            "string (got 123); keeping project default",
        ),
        (
            "develop_test_gate",
            "test_gate",
            True,
            "true",  # a string is not a bool
            "task metadata.develop_test_gate: must be a boolean true/false "
            "(got 'true'); keeping project default",
        ),
    ],
)
def test_bad_task_override_frictions_and_keeps_project_for_every_table_field(
    key: str,
    attr: str,
    project_value: object,
    bad_task_value: object,
    expected_friction: str,
) -> None:
    # Every _PROJECT_THEN_TASK_FIELDS row shares the invalid-task-override path: the
    # project value is kept and the "; keeping project default" suffix is appended.
    # Pinned per row so a future table/key/parser mistake for one field can't slip
    # past the "byte-identical friction text" claim.
    settings, frictions = _resolve({key: project_value}, {key: bad_task_value})
    assert getattr(settings, attr) == project_value  # the project value is kept
    assert frictions == (expected_friction,)


def test_test_gate_rejects_non_bool() -> None:
    settings, frictions = _resolve({"develop_test_gate": "true"})  # str, not bool
    assert settings.test_gate is None
    assert frictions == (
        "develop_test_gate: must be a boolean true/false (got 'true'); ignoring",
    )


# ── coder object shape ────────────────────────────────────────────────


def test_coder_not_an_object_frictions() -> None:
    settings, frictions = _resolve({"develop_coder": "codex"})  # string, not a table
    assert settings.coder == "claude"  # default
    assert settings.coder_model is None
    assert frictions == (
        "develop_coder must be an object with optional tool/model/effort; ignoring",
    )


def test_coder_tool_not_string_frictions_but_keeps_model() -> None:
    settings, frictions = _resolve({"develop_coder": {"tool": 5, "model": "o3"}})
    assert settings.coder == "claude"
    assert settings.coder_model == "o3"  # a bad tool does not drop a good model
    assert frictions == ("develop_coder.tool must be a string; using default",)


# ── project-only inline-validated fields ──────────────────────────────


def test_max_rounds_invalid_frictions() -> None:
    settings, frictions = _resolve({"develop_max_rounds": 0})
    assert settings.max_rounds is None
    assert frictions == ("develop_max_rounds 0 invalid; ignoring",)


def test_max_cost_invalid_frictions() -> None:
    settings, frictions = _resolve({"develop_max_cost_usd": -1})
    assert settings.max_cost_usd is None
    assert frictions == ("develop_max_cost_usd -1 invalid; ignoring",)


def test_max_cost_int_coerced_to_float() -> None:
    settings, frictions = _resolve({"develop_max_cost_usd": 10})
    assert settings.max_cost_usd == 10.0
    assert isinstance(settings.max_cost_usd, float)
    assert frictions == ()


def test_fallback_chain_not_list_of_strings_frictions() -> None:
    settings, frictions = _resolve({"develop_fallback_chain": ["a", 2]})
    assert settings.fallback_chain == ()
    assert frictions == ("develop_fallback_chain must be a list of strings; ignoring",)


def test_review_profile_project_blank_frictions() -> None:
    settings, frictions = _resolve({"develop_review_profile": "  "})
    assert settings.review_profile_project is None
    assert frictions == (
        "develop_review_profile '  ' invalid; ignoring (must be a non-empty string)",
    )


def test_block_on_red_deprecation_friction() -> None:
    _, frictions = _resolve({"develop_block_on_red": True})
    assert len(frictions) == 1
    assert frictions[0].startswith("develop_block_on_red is removed and ignored")


# ── per-check command overrides (#273) ─────────────────────────────────


def test_check_commands_project_layer() -> None:
    settings, frictions = _resolve(
        {"develop_check_commands": {"typecheck": "make typecheck"}}
    )
    assert settings.check_commands == {"typecheck": "make typecheck"}
    assert frictions == ()


def test_check_commands_default_empty_no_friction() -> None:
    settings, frictions = _resolve()
    assert settings.check_commands == {}
    assert frictions == ()


def test_check_commands_task_merges_over_project_per_key() -> None:
    # A task override merges PER-KEY onto the project map (not wholesale replace): the
    # task re-points typecheck, the project's lint override survives.
    settings, frictions = _resolve(
        {"develop_check_commands": {"typecheck": "make tc", "lint": "make lint"}},
        {"develop_check_commands": {"typecheck": "make tc2"}},
    )
    assert settings.check_commands == {"typecheck": "make tc2", "lint": "make lint"}
    assert frictions == ()


def test_check_commands_bad_project_value_frictions_and_empties() -> None:
    settings, frictions = _resolve({"develop_check_commands": {"typcheck": "x"}})
    assert settings.check_commands == {}
    assert len(frictions) == 1
    assert frictions[0].startswith("develop_check_commands: unknown check 'typcheck'")
    assert frictions[0].endswith("; ignoring")


def test_check_commands_bad_task_override_keeps_project() -> None:
    settings, frictions = _resolve(
        {"develop_check_commands": {"lint": "make lint"}},
        {"develop_check_commands": "notatable"},
    )
    assert settings.check_commands == {"lint": "make lint"}  # project kept
    assert len(frictions) == 1
    assert frictions[0].endswith("; keeping project default")


# --- per-check state overrides (#273 slice 2) ─────────────────────────────


def test_check_states_project_and_task_merge_per_key() -> None:
    settings, frictions = _resolve(
        {"develop_check_states": {"sast": "required", "lint": "off"}},
        {"develop_check_states": {"sast": "off"}},
    )
    assert settings.check_states == {"sast": "off", "lint": "off"}  # per-key merge
    assert frictions == ()


def test_check_states_default_empty_no_friction() -> None:
    settings, frictions = _resolve()
    assert settings.check_states == {}
    assert frictions == ()


def test_check_states_bad_project_value_frictions_and_empties() -> None:
    settings, frictions = _resolve({"develop_check_states": {"sast": "advisory"}})
    assert settings.check_states == {}
    assert len(frictions) == 1
    assert frictions[0].endswith("; ignoring")


def test_check_states_non_scalar_value_frictions_not_crash() -> None:
    # #280 review finding 1: an unhashable state (a list from TOML/JSON metadata) must
    # degrade to a [Friction], not crash the resolver with a TypeError (untrusted cfg).
    settings, frictions = _resolve({"develop_check_states": {"sast": ["off"]}})
    assert settings.check_states == {}
    assert len(frictions) == 1
    assert frictions[0].endswith("; ignoring")


# --- aggregate repo-parity command (#273 slice 3) ─────────────────────────


def test_parity_command_project_then_task() -> None:
    # A scalar develop_parity_command: project value, task override wins.
    project, _ = _resolve({"develop_parity_command": "make check"})
    assert project.parity_command == "make check"
    both, _ = _resolve(
        {"develop_parity_command": "make check"},
        {"develop_parity_command": "make ci"},
    )
    assert both.parity_command == "make ci"


def test_parity_command_default_none_no_friction() -> None:
    settings, frictions = _resolve()
    assert settings.parity_command is None
    assert frictions == ()


def test_parity_command_bad_value_frictions_and_keeps_none() -> None:
    settings, frictions = _resolve({"develop_parity_command": ""})
    assert settings.parity_command is None
    assert len(frictions) == 1
    assert frictions[0].endswith("; ignoring")


# ── friction ORDER (must match the original resolve_project_settings) ──


def test_friction_order_project_coder_before_task_override() -> None:
    # A bad project effort AND a bad task model both friction: the project-layer
    # coder frictions precede the task-override frictions — the exact append order
    # of the original resolve_project_settings (project model+effort, then task
    # model+effort). Pinned so the table refactor cannot silently reorder them.
    _, frictions = _resolve(
        {"develop_coder": {"effort": "bogus"}},  # bad project effort
        {"develop_model": ""},  # bad task model
    )
    assert len(frictions) == 2
    assert "develop_coder.effort" in frictions[0]
    assert "task metadata.develop_model" in frictions[1]


# ── develop_copilot_review (gate 15690a0e / task 0e8d96ba) ─────────────


def test_copilot_review_defaults_unset() -> None:
    settings, frictions = _resolve()
    assert settings.copilot_review is None  # caller falls back to the route flag
    assert frictions == ()


def test_copilot_review_project_layer_and_task_override() -> None:
    # The point of the task: a project can default the request OFF while a
    # single story opts back in (spend Copilot deliberately, per story).
    settings, frictions = _resolve({"develop_copilot_review": False})
    assert settings.copilot_review is False
    assert frictions == ()

    settings, frictions = _resolve(
        {"develop_copilot_review": False}, {"develop_copilot_review": True}
    )
    assert settings.copilot_review is True
    assert frictions == ()


def test_bad_copilot_review_frictions_and_falls_back() -> None:
    settings, frictions = _resolve({"develop_copilot_review": "yes please"})
    assert settings.copilot_review is None
    assert len(frictions) == 1 and "develop_copilot_review" in frictions[0]
