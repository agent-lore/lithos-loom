"""Unit tests for check_runner's public surface + the delivery test gate (ARCH-1.S2).

The check-set builders, gate runner, and floor decision moved here from
``develop.py``; their behaviour is exercised in depth by
``tests/test_story_develop_check_set.py`` (now targeting ``check_runner``). This
file pins the module's public import surface and the NEW
:func:`run_delivery_test_gate` policy wrapper — the intentional
delivery-vs-develop gate divergence, promoted from an inline ``pr_delivery``
filter to a named function so a develop-side gate change can't silently rewire it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

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
        "run_delivery_test_gate",
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


# --- run_delivery_test_gate: the delivery-only, ledger-less test gate ---------


def test_run_delivery_test_gate_runs_only_the_test_check_with_no_ledger(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # #140: the profile set carries advisory + candidate checks, but delivery keys
    # ONLY on `test` and passes NO ledger — running the others would burn
    # containers without affecting the push decision.
    test_check = Check("test", "pytest", "required")
    lint_check = Check("lint", "ruff", "informational")
    monkeypatch.setattr(
        check_runner, "build_check_set", lambda config, wt: (lint_check, test_check)
    )
    seen: dict = {}
    green = _gate(True)

    def fake_run_check_set(config, wt, sha, round_no, checks, gate_ledger=None):
        seen["checks"] = checks
        seen["ledger"] = gate_ledger
        return CheckSetResult(results=(CheckResult(test_check, "ran", green),))

    monkeypatch.setattr(check_runner, "run_check_set", fake_run_check_set)

    gate = check_runner.run_delivery_test_gate(_config(tmp_path), tmp_path, "sha", 1)
    assert [c.name for c in seen["checks"]] == ["test"]  # ONLY the test check ran
    assert seen["ledger"] is None  # no gate ledger passed
    assert gate is green  # the raw test GateResult


def test_run_delivery_test_gate_returns_none_when_no_test_check(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # develop_test_gate=false / no runnable test command -> no `test` check, and
    # the wrapper short-circuits before doing any container work.
    monkeypatch.setattr(
        check_runner,
        "build_check_set",
        lambda config, wt: (Check("lint", "ruff", "required"),),
    )
    called = False

    def fake_run_check_set(*a, **k):
        nonlocal called
        called = True
        return None

    monkeypatch.setattr(check_runner, "run_check_set", fake_run_check_set)
    got = check_runner.run_delivery_test_gate(_config(tmp_path), tmp_path, "sha", 1)
    assert got is None
    assert called is False  # no run when there's no test check


def test_run_delivery_test_gate_returns_raw_verdict_ignoring_profile_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The delivery gate reads the RAW `test` GateResult, NOT gate_floor_blocks — so
    # even an *informational* `test` returns its RED verdict and delivery holds the
    # push. This is the intentional divergence from the develop-side floor.
    info_test = Check("test", "pytest", "informational")
    monkeypatch.setattr(
        check_runner, "build_check_set", lambda config, wt: (info_test,)
    )
    red = _gate(False)
    monkeypatch.setattr(
        check_runner,
        "run_check_set",
        lambda *a, **k: CheckSetResult(results=(CheckResult(info_test, "ran", red),)),
    )
    gate = check_runner.run_delivery_test_gate(_config(tmp_path), tmp_path, "sha", 1)
    assert gate is red  # the raw test GateResult, floor ignored
    assert red.passed is False  # ... and it's RED, so delivery would hold the push


def test_run_delivery_test_gate_returns_none_on_export_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # run_check_set returns None on a tree-export error -> the wrapper yields None,
    # matching the prior inline `cs.test_gate if cs is not None else None`.
    monkeypatch.setattr(
        check_runner,
        "build_check_set",
        lambda config, wt: (Check("test", "pytest", "required"),),
    )
    monkeypatch.setattr(check_runner, "run_check_set", lambda *a, **k: None)
    got = check_runner.run_delivery_test_gate(_config(tmp_path), tmp_path, "sha", 1)
    assert got is None
