"""Unit tests for check_runner's public surface + the delivery test gate (ARCH-1.S2).

The check-set builders, gate runner, and floor decision moved here from
``develop.py``; their behaviour is exercised in depth by
``tests/test_story_develop_check_set.py`` (now targeting ``check_runner``). This
file pins the module's public import surface and the NEW
(the former ``run_delivery_test_gate`` policy wrapper left with the
inline Copilot round — S2 slice D); the intentional
delivery-vs-develop gate divergence, promoted from an inline ``pr_delivery``
filter to a named function so a develop-side gate change can't silently rewire it.
"""

from __future__ import annotations

from pathlib import Path

from lithos_loom.plugins.story_develop import check_runner
from lithos_loom.plugins.story_develop.check_set import (
    Check,
    CheckResult,
    CheckSetResult,
)
from lithos_loom.plugins.story_develop.config import DevelopConfig
from lithos_loom.plugins.story_develop.test_gate import GateResult


def _config(tmp_path: Path) -> DevelopConfig:
    return DevelopConfig(repo=tmp_path, description="x", work_dir=tmp_path / "w")


def _gate(passed: bool) -> GateResult:
    return GateResult(
        command="pytest",
        exit_code=0 if passed else 1,
        passed=passed,
        output_tail="ok" if passed else "boom",
    )


def test_public_surface_is_importable() -> None:
    for name in (
        "build_check_set",
        "run_check_set",
        "check_result_blocks",
        "gate_floor_blocks",
        "merge_check_sets",
        "load_gate_ledger",
        "persist_gate_ledger",
    ):
        assert callable(getattr(check_runner, name))


def test_merge_check_sets_preserves_order_and_handles_none() -> None:
    a = CheckSetResult(
        results=(CheckResult(Check("lint", "ruff", "required"), "ran", _gate(True)),)
    )
    b = CheckSetResult(
        results=(CheckResult(Check("test", "pytest", "required"), "ran", _gate(True)),)
    )
    merged = check_runner.merge_check_sets(a, b)
    assert merged is not None
    assert [r.check.name for r in merged.results] == ["lint", "test"]
    assert check_runner.merge_check_sets(None, b) is b  # either side may be None
    assert check_runner.merge_check_sets(a, None) is a
