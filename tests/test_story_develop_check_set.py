"""Tests for the multi-check check-set abstraction (#131).

Pure types + the execution-outcome adapter, plus the ``build_default_check_set``
constructor. No Docker — the container run is exercised via the existing
``test_gate`` seam in the core orchestration tests.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lithos_loom.plugins.story_develop import develop as develop_mod
from lithos_loom.plugins.story_develop.check_set import (
    Check,
    CheckResult,
    CheckSetResult,
    classify_execution,
    render_check_summary,
)
from lithos_loom.plugins.story_develop.config import DevelopConfig
from lithos_loom.plugins.story_develop.develop import build_default_check_set
from lithos_loom.plugins.story_develop.test_gate import GateResult


def _green() -> GateResult:
    return GateResult(command="pytest", exit_code=0, passed=True, output_tail="ok")


def _red() -> GateResult:
    return GateResult(command="pytest", exit_code=1, passed=False, output_tail="boom")


def _timeout() -> GateResult:
    return GateResult(command="pytest", exit_code=124, passed=False, output_tail="")


# --- classify_execution: the (exit_code, output) -> execution_outcome axis ----


def test_classify_execution_ran_for_green_and_red() -> None:
    # A RED run still RAN — execution success is a separate axis from blocking.
    assert classify_execution(_green()) == "ran"
    assert classify_execution(_red()) == "ran"


def test_classify_execution_timed_out_and_errored() -> None:
    assert classify_execution(_timeout()) == "timed_out"
    assert classify_execution(None) == "errored"  # infra error -> never executed


# --- CheckResult.passed: the blocking semantics ------------------------------


def test_required_check_blocks_on_red() -> None:
    r = CheckResult(Check("test", "pytest", "required"), "ran", _red())
    assert r.passed is False


def test_required_check_passes_on_green() -> None:
    r = CheckResult(Check("test", "pytest", "required"), "ran", _green())
    assert r.passed is True


def test_required_check_blocks_on_timeout() -> None:
    r = CheckResult(Check("test", "pytest", "required"), "timed_out", _timeout())
    assert r.passed is False


def test_informational_check_never_blocks_even_red() -> None:
    r = CheckResult(Check("lint", "ruff", "informational"), "ran", _red())
    assert r.passed is True


def test_required_check_errored_does_not_block() -> None:
    # The foundation-slice rule (matches today's "infra error skips the gate"):
    # a required check that errored at the infra level never BLOCKS.
    r = CheckResult(Check("test", "pytest", "required"), "errored", None)
    assert r.passed is True


# --- CheckSetResult aggregate views ------------------------------------------


def test_single_test_check_views_reduce_to_the_gate() -> None:
    g = _green()
    cs = CheckSetResult((CheckResult(Check("test", "pytest", "required"), "ran", g),))
    assert cs.test_gate is g
    assert cs.blocking_passed is True
    assert cs.aggregate_verdict == "GREEN"


def test_single_red_required_check_blocks() -> None:
    cs = CheckSetResult(
        (CheckResult(Check("test", "pytest", "required"), "ran", _red()),)
    )
    assert cs.blocking_passed is False
    assert cs.aggregate_verdict == "RED"


def test_two_check_set_separates_blocking_from_verdict() -> None:
    # Proves the structure is real (ordered, multi-check, separated axes) without
    # shipping a second check in the default set: a green REQUIRED test plus a RED
    # INFORMATIONAL check -> nothing blocks, but the rolled-up verdict is RED.
    cs = CheckSetResult(
        (
            CheckResult(Check("test", "pytest", "required"), "ran", _green()),
            CheckResult(Check("lint", "ruff", "informational"), "ran", _red()),
        )
    )
    assert cs.blocking_passed is True
    assert cs.aggregate_verdict == "RED"


def test_timeout_dominates_aggregate_verdict() -> None:
    cs = CheckSetResult(
        (
            CheckResult(Check("test", "pytest", "required"), "ran", _green()),
            CheckResult(
                Check("lint", "ruff", "informational"), "timed_out", _timeout()
            ),
        )
    )
    assert cs.aggregate_verdict == "TIMEOUT"


def test_test_gate_view_is_none_without_a_test_check() -> None:
    cs = CheckSetResult(
        (CheckResult(Check("lint", "ruff", "informational"), "ran", _green()),)
    )
    assert cs.test_gate is None


def test_errored_test_check_clears_the_gate_view() -> None:
    # The stale-RED-clearing path: a test check that errored -> test_gate is None
    # (so DevelopResult.test_gate is None) AND blocking_passed is True.
    cs = CheckSetResult(
        (CheckResult(Check("test", "pytest", "required"), "errored", None),)
    )
    assert cs.test_gate is None
    assert cs.blocking_passed is True
    assert cs.aggregate_verdict is None  # no check produced a verdict


# --- build_default_check_set: the {test} default + the §10 re-scope ----------


def _config(tmp_path: Path, **kw: object) -> DevelopConfig:
    return DevelopConfig(
        repo=tmp_path,
        description="x",
        work_dir=tmp_path,
        **kw,  # type: ignore[arg-type]
    )


def test_default_set_is_one_informational_test_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        develop_mod, "_resolve_test_command", lambda config, wt: "pytest"
    )
    checks = build_default_check_set(_config(tmp_path, block_on_red=False), tmp_path)
    assert len(checks) == 1
    assert checks[0] == Check(name="test", command="pytest", state="informational")


def test_block_on_red_makes_the_test_check_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        develop_mod, "_resolve_test_command", lambda config, wt: "pytest"
    )
    checks = build_default_check_set(_config(tmp_path, block_on_red=True), tmp_path)
    assert checks[0].state == "required"


def test_test_gate_false_excludes_the_test_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # ADR §10: develop_test_gate=false drops only the `test` check; with a
    # one-element default set that is an empty set (observably "no gate").
    def _boom(config: object, wt: object) -> str:
        raise AssertionError("must not resolve a command when the gate is off")

    monkeypatch.setattr(develop_mod, "_resolve_test_command", _boom)
    assert build_default_check_set(_config(tmp_path, test_gate=False), tmp_path) == ()


def test_no_detected_command_yields_empty_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(develop_mod, "_resolve_test_command", lambda config, wt: None)
    assert build_default_check_set(_config(tmp_path, test_gate=True), tmp_path) == ()


# --- render_check_summary: feed the gate to coder + reviewer prompts (#136) ---


def _cs(*results: CheckResult) -> CheckSetResult:
    return CheckSetResult(results)


def test_summary_none_is_empty_for_coder() -> None:
    assert render_check_summary(None, for_coder=True) == ""


def test_summary_none_notes_no_gate_for_reviewer() -> None:
    assert (
        "no deterministic gate" in render_check_summary(None, for_coder=False).lower()
    )


def test_summary_coder_empty_when_all_pass() -> None:
    cs = _cs(CheckResult(Check("test", "pytest", "required"), "ran", _green()))
    assert render_check_summary(cs, for_coder=True) == ""


def test_summary_coder_failing_check_is_behaviour_preserving() -> None:
    # The coder side must reproduce the old `_gate_note`: the heading + output
    # tail + "authoritative" framing the regression test in core asserts on.
    cs = _cs(CheckResult(Check("test", "pytest", "required"), "ran", _red()))
    out = render_check_summary(cs, for_coder=True)
    assert "## Independent test gate (FAILED)" in out
    assert "boom" in out  # _red() output_tail
    assert "authoritative" in out


def test_summary_coder_shows_only_failing_checks() -> None:
    cs = _cs(
        CheckResult(Check("test", "pytest", "required"), "ran", _green()),
        CheckResult(Check("lint", "ruff", "informational"), "ran", _red()),
    )
    out = render_check_summary(cs, for_coder=True)
    assert "## Independent lint gate (FAILED)" in out
    assert "Independent test gate" not in out  # the green test check is omitted


def test_summary_reviewer_lists_every_check_with_verdict() -> None:
    cs = _cs(
        CheckResult(Check("test", "pytest", "required"), "ran", _green()),
        CheckResult(Check("lint", "ruff", "informational"), "ran", _red()),
    )
    out = render_check_summary(cs, for_coder=False)
    assert "`test`" in out and "GREEN" in out  # green checks shown to reviewers
    assert "`lint`" in out and "RED" in out
    assert "boom" in out  # the failing check's output tail is appended


def test_summary_reviewer_empty_set_notes_no_gate() -> None:
    assert (
        "no deterministic gate" in render_check_summary(_cs(), for_coder=False).lower()
    )


# --- #133: expected-but-absent blocks; declared N/A does not --------------------


def test_required_check_blocks_when_expected_but_absent() -> None:
    # A required check whose tool/target is expected-but-absent (empty command ->
    # `absent`, no gate) BLOCKS — distinct from an infra error, which skips.
    r = CheckResult(Check("test", "", "required"), "absent", None)
    assert r.passed is False


def test_informational_check_passes_when_absent() -> None:
    r = CheckResult(Check("lint", "", "informational"), "absent", None)
    assert r.passed is True


def test_not_applicable_check_passes_when_na() -> None:
    r = CheckResult(Check("typecheck", "", "not_applicable"), "n_a", None)
    assert r.passed is True


def test_required_absent_check_blocks_the_whole_set() -> None:
    cs = CheckSetResult((CheckResult(Check("test", "", "required"), "absent", None),))
    assert cs.blocking_passed is False
    assert cs.aggregate_verdict is None  # no check produced a verdict


def test_summary_coder_surfaces_expected_but_absent_required_check() -> None:
    cs = _cs(CheckResult(Check("test", "", "required"), "absent", None))
    out = render_check_summary(cs, for_coder=True)
    assert "EXPECTED BUT ABSENT" in out
    assert "`test`" in out


def test_summary_coder_omits_absent_non_blocking_check() -> None:
    # An informational absent check does not block, so it is not surfaced.
    cs = _cs(CheckResult(Check("lint", "", "informational"), "absent", None))
    assert render_check_summary(cs, for_coder=True) == ""


# --- #133: build_default_check_set applicability of the `test` check ------------


def test_required_test_absent_blocks_when_ecosystem_detected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A code repo (pyproject => python) with a *required* test check but no
    # runnable test command -> expected-but-absent placeholder that blocks.
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    monkeypatch.setattr(develop_mod, "_resolve_test_command", lambda config, wt: None)
    checks = build_default_check_set(_config(tmp_path, block_on_red=True), tmp_path)
    assert checks == (Check(name="test", command="", state="required"),)


def test_markerless_repo_yields_no_gate_even_when_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No ecosystem marker: `test` is declared N/A (docs-only), so even a required
    # gate with no command is simply empty rather than a blocking absent check.
    monkeypatch.setattr(develop_mod, "_resolve_test_command", lambda config, wt: None)
    assert build_default_check_set(_config(tmp_path, block_on_red=True), tmp_path) == ()
