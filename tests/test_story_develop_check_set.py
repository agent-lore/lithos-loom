"""Tests for the multi-check check-set abstraction (#131).

Pure types + the execution-outcome adapter, plus the ``build_check_set``
constructor (the Review-Profile-selected set, #140). No Docker — the container run
is exercised via the existing ``test_gate`` seam in the core orchestration tests.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lithos_loom.plugins.story_develop import develop as develop_mod
from lithos_loom.plugins.story_develop.check_set import (
    Check,
    CheckResult,
    CheckSetResult,
    CheckState,
    classify_execution,
    render_check_summary,
)
from lithos_loom.plugins.story_develop.config import DevelopConfig
from lithos_loom.plugins.story_develop.develop import build_check_set
from lithos_loom.plugins.story_develop.gate_findings import GateFinding, GateLedger
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


def test_finding_producing_check_exit_code_does_not_decide_blocking() -> None:
    # ADR §5 / #132 finding-2 contract: a finding-producing tool's exit code never
    # decides approval. pip-audit has no --exit-zero flag, so its process exits
    # non-zero on findings — while the check is informational that must not block,
    # and a required floor (#139) must read the ledger severity, not ``gate.passed``.
    nonzero = GateResult(
        command="pip-audit --format=json", exit_code=1, passed=False, output_tail="[]"
    )
    r = CheckResult(
        Check("dep-audit", "pip-audit --format=json", "informational"), "ran", nonzero
    )
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


# --- gate_floor_blocks: the #140 ledger-aware required-check floor -------------
# The approval gate reads a REQUIRED check's blocking verdict from the finding
# ledger's mapped severity (ADR §5/#159) for adapter-backed tools, and from the
# raw exit code for tools with no adapter. Only `required` checks count; an
# informational check (e.g. `sast` on `standard`) never blocks, even though its
# findings share the ledger.


def _ran(name: str, command: str, state: CheckState, gate: GateResult) -> CheckResult:
    return CheckResult(Check(name, command, state), "ran", gate)


def test_floor_required_adapter_check_blocks_on_major_ledger_finding() -> None:
    # ruff (adapter) exited GREEN this round, yet the ledger carries a MAJOR `lint`
    # finding -> the floor reads the ledger severity, NOT the exit code, and blocks.
    cs = CheckSetResult(
        (_ran("lint", "ruff check --output-format=json", "required", _green()),)
    )
    assert (
        develop_mod.gate_floor_blocks(cs, _ledger_with("lint", severity="major"))
        is True
    )


def test_floor_informational_sast_major_finding_does_not_block() -> None:
    # THE central Option-A regression guard: bandit (`sast`) is INFORMATIONAL on
    # `standard`; its MAJOR finding lands in the SHARED ledger, but the floor counts
    # ledger findings ONLY for REQUIRED checks, so it must NOT block the default.
    cs = CheckSetResult(
        (
            _ran("lint", "ruff check --output-format=json", "required", _green()),
            _ran("sast", "bandit -r . -f json", "informational", _green()),
        )
    )
    led = GateLedger()
    led.apply_round(
        "sast",
        [
            GateFinding(
                check="sast",
                tool="bandit",
                rule="B602",
                severity="major",
                message="shell",
            )
        ],
        1,
    )
    assert develop_mod.gate_floor_blocks(cs, led) is False


def test_floor_required_no_adapter_check_reads_raw_exit() -> None:
    # pyright has no adapter -> blocking reads the raw exit code.
    red = CheckSetResult((_ran("typecheck", "pyright", "required", _red()),))
    green = CheckSetResult((_ran("typecheck", "pyright", "required", _green()),))
    assert develop_mod.gate_floor_blocks(red, None) is True
    assert develop_mod.gate_floor_blocks(green, None) is False


def test_floor_uv_wrapped_adapter_reads_ledger_via_command_tool() -> None:
    # #165: a required `uv run pip-audit` check exits GREEN this round, but the ledger
    # carries a MAJOR `dep-audit` finding. The floor must resolve the REAL adapter tool
    # (pip-audit) past the `uv` entrypoint via command_tool and read the ledger — not
    # fall through to the raw-exit branch (which would wrongly pass it).
    cs = CheckSetResult(
        (_ran("dep-audit", "uv run pip-audit -f json", "required", _green()),)
    )
    assert (
        develop_mod.gate_floor_blocks(cs, _ledger_with("dep-audit", severity="major"))
        is True
    )


def test_floor_required_absent_check_blocks_without_indexerror() -> None:
    # An expected-but-absent required check has an EMPTY command (no container ran).
    # The floor must block it and must not crash on "".split()[0].
    cs = CheckSetResult(
        (CheckResult(Check("typecheck", "", "required"), "absent", None),)
    )
    assert develop_mod.gate_floor_blocks(cs, GateLedger()) is True


def test_floor_required_adapter_timeout_blocks_with_empty_ledger() -> None:
    # The essential branch: an ADAPTER check (ruff) that TIMED OUT produced no
    # findings, so the ledger is empty. It must still block via the timed_out branch,
    # not fall through to the adapter rule's "no findings -> pass".
    cs = CheckSetResult(
        (
            CheckResult(
                Check("lint", "ruff check --output-format=json", "required"),
                "timed_out",
                _timeout(),
            ),
        )
    )
    assert develop_mod.gate_floor_blocks(cs, GateLedger()) is True


def test_floor_required_no_adapter_timeout_blocks() -> None:
    cs = CheckSetResult(
        (CheckResult(Check("test", "pytest", "required"), "timed_out", _timeout()),)
    )
    assert develop_mod.gate_floor_blocks(cs, None) is True


def test_floor_errored_and_na_never_block() -> None:
    errored = CheckSetResult(
        (CheckResult(Check("test", "pytest", "required"), "errored", None),)
    )
    na = CheckSetResult(
        (CheckResult(Check("dep-audit", "", "not_applicable"), "n_a", None),)
    )
    assert develop_mod.gate_floor_blocks(errored, None) is False
    assert develop_mod.gate_floor_blocks(na, None) is False


def test_floor_informational_red_never_blocks() -> None:
    adapter = CheckSetResult(
        (_ran("lint", "ruff check --output-format=json", "informational", _red()),)
    )
    no_adapter = CheckSetResult((_ran("semgrep", "semgrep", "informational", _red()),))
    assert develop_mod.gate_floor_blocks(adapter, GateLedger()) is False
    assert develop_mod.gate_floor_blocks(no_adapter, None) is False


def test_floor_none_check_set_never_blocks() -> None:
    assert develop_mod.gate_floor_blocks(None, GateLedger()) is False
    assert develop_mod.gate_floor_blocks(None, None) is False


def test_floor_minor_finding_below_threshold_does_not_block() -> None:
    # A required adapter check whose only ledger finding is MINOR -> at the default
    # `major` threshold it is surfaced, not blocking (ruff `W` rules map to minor).
    cs = CheckSetResult(
        (_ran("lint", "ruff check --output-format=json", "required", _red()),)
    )
    assert (
        develop_mod.gate_floor_blocks(cs, _ledger_with("lint", severity="minor"))
        is False
    )


def test_floor_single_required_test_matches_blocking_passed() -> None:
    # Compatibility guard: for the legacy single-`test` set (no adapter, no ledger),
    # gate_floor_blocks must be exactly the negation of blocking_passed.
    for gate in (_green(), _red(), _timeout()):
        outcome = "timed_out" if gate.exit_code == 124 else "ran"
        cs = CheckSetResult(
            (CheckResult(Check("test", "pytest", "required"), outcome, gate),)
        )
        assert develop_mod.gate_floor_blocks(cs, None) is (not cs.blocking_passed)


# --- build_check_set: profile-selected set, informational-first (#140) --------


def _config(tmp_path: Path, **kw: object) -> DevelopConfig:
    return DevelopConfig(
        repo=tmp_path,
        description="x",
        work_dir=tmp_path,
        **kw,  # type: ignore[arg-type]
    )


def _python(
    monkeypatch: pytest.MonkeyPatch, *, present: tuple[str, ...] | None = None
) -> None:
    """Stub a python ecosystem with the named tools present (all tools, if None) —
    so ``build_check_set`` resolves the profile's checks without touching Docker."""
    monkeypatch.setattr(
        develop_mod.detection, "detect_ecosystems", lambda wt: ("python",)
    )
    monkeypatch.setattr(
        develop_mod.test_gate,
        "probe_tools",
        lambda image, tools: (
            list(tools) if present is None else [t for t in tools if t in present]
        ),
    )


def test_default_set_is_one_required_test_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Markerless repo: the profile's non-test checks all resolve absent and drop,
    # leaving just the `test` check — required under the default `standard` profile
    # (#140: its state is the profile's, blocking on RED with no extra config).
    monkeypatch.setattr(
        develop_mod, "_resolve_test_command", lambda config, wt: "pytest"
    )
    checks = build_check_set(_config(tmp_path), tmp_path)
    assert checks == (Check(name="test", command="pytest", state="required"),)


def test_test_gate_false_drops_test_check_but_keeps_the_informational_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # ADR §10 / #159: develop_test_gate=false is a *test* escape hatch — it scopes
    # off the `test` check only, never the rest of the profile's (informational)
    # set. The test command must not even be resolved; the lint check survives.
    def _boom(config: object, wt: object) -> str:
        raise AssertionError("must not resolve a test command when the gate is off")

    lint = Check(
        name="lint",
        command="ruff check --output-format=json --exit-zero",
        state="informational",
    )
    monkeypatch.setattr(develop_mod, "_resolve_test_command", _boom)
    monkeypatch.setattr(
        develop_mod, "_build_profile_checks", lambda config, profile, eco, wt: [lint]
    )
    checks = build_check_set(_config(tmp_path, test_gate=False), tmp_path)
    assert checks == (lint,)
    assert all(c.name != "test" for c in checks)


def test_build_check_set_resolves_uv_aware_typecheck_on_a_uv_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # End-to-end (#165): a uv-managed python repo resolves the required `typecheck`
    # check to `uv run pyright` (bare `pyright` false-positives in the gate container),
    # while the static-analysis `lint` check stays bare `ruff`.
    _python(monkeypatch, present=("uv", "ruff", "bandit"))
    monkeypatch.setattr(
        develop_mod, "_resolve_test_command", lambda config, wt: "uv run pytest"
    )
    (tmp_path / "uv.lock").write_text("")
    checks = build_check_set(_config(tmp_path, review_profile="standard"), tmp_path)
    by = {c.name: c for c in checks}
    assert by["typecheck"].command == "uv run pyright"
    assert by["lint"].command.startswith("ruff check")


def test_no_detected_command_yields_empty_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(develop_mod, "_resolve_test_command", lambda config, wt: None)
    assert build_check_set(_config(tmp_path, test_gate=True), tmp_path) == ()


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


# --- #133/#140: build_check_set applicability of the `test` check --------------


def test_required_test_absent_blocks_when_ecosystem_detected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A code repo (pyproject => python) with a *required* test check but no
    # runnable test command -> expected-but-absent placeholder that blocks.
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    monkeypatch.setattr(develop_mod, "_resolve_test_command", lambda config, wt: None)
    # isolate the test check; the profile check-set is exercised separately below.
    monkeypatch.setattr(
        develop_mod, "_build_profile_checks", lambda config, profile, eco, wt: []
    )
    # `standard` declares test required, so an absent test command is a blocking
    # expected-but-absent placeholder (no block_on_red knob needed — #140).
    checks = build_check_set(_config(tmp_path), tmp_path)
    assert checks == (Check(name="test", command="", state="required"),)


def test_markerless_repo_yields_no_gate_even_when_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No ecosystem marker: `test` is declared N/A (docs-only), so even a required
    # gate with no command is simply empty rather than a blocking absent check.
    monkeypatch.setattr(develop_mod, "_resolve_test_command", lambda config, wt: None)
    assert build_check_set(_config(tmp_path), tmp_path) == ()


# --- #132 Slice 3: gate-ledger surfacing in render_check_summary ---------------


def _ledger_with(check: str, rule: str = "E501", severity: str = "major") -> GateLedger:
    led = GateLedger()
    led.apply_round(
        check,
        [
            GateFinding(
                check=check,
                tool="ruff",
                rule=rule,
                severity=severity,
                message=f"{rule} msg",
                file="a.py",
                line=3,
            )
        ],
        1,
    )
    return led


def test_summary_coder_renders_gate_findings_from_ledger() -> None:
    cs = _cs(CheckResult(Check("lint", "ruff …", "informational"), "ran", _green()))
    out = render_check_summary(cs, for_coder=True, gate_ledger=_ledger_with("lint"))
    assert "## Independent lint gate findings" in out
    assert "gate/lint-001 (major): E501 [a.py:3] E501 msg" in out


def test_summary_reviewer_renders_gate_findings_from_ledger() -> None:
    cs = _cs(CheckResult(Check("lint", "ruff …", "informational"), "ran", _green()))
    out = render_check_summary(cs, for_coder=False, gate_ledger=_ledger_with("lint"))
    assert "`lint` deterministic findings:" in out
    assert "gate/lint-001 (major): E501 [a.py:3] E501 msg" in out


def test_summary_coder_keeps_raw_tail_for_checks_without_findings() -> None:
    # The `test` check has no adapter -> no ledger findings -> raw-tail behaviour.
    cs = _cs(CheckResult(Check("test", "pytest", "required"), "ran", _red()))
    out = render_check_summary(cs, for_coder=True, gate_ledger=GateLedger())
    assert "## Independent test gate (FAILED)" in out
    assert "boom" in out


# --- #140: the profile selects the informational check-set --------------------


def test_standard_profile_requires_lint_typecheck_surfaces_sast(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # #140 floor slice (Option A): `standard` brings lint + typecheck + sast
    # alongside test. lint/typecheck are now REQUIRED (they block — exactly what
    # `make check` already enforces), while sast (bandit) stays INFORMATIONAL
    # (surfaced, not blocking the default). Finding-producing tools (ruff/bandit) are
    # machine-ified for the ledger; a no-adapter tool (pyright) runs as-is.
    monkeypatch.setattr(develop_mod, "_resolve_test_command", lambda c, w: "pytest")
    _python(monkeypatch)
    checks = build_check_set(_config(tmp_path), tmp_path)
    by_name = {c.name: c for c in checks}
    assert by_name["test"].command == "pytest"
    assert by_name["lint"].command == "ruff check --output-format=json --exit-zero"
    assert by_name["sast"].command == "bandit -r . -x ./.venv -f json --exit-zero"
    assert by_name["typecheck"].command == "pyright"  # no adapter -> run as-is
    assert by_name["lint"].state == "required"
    assert by_name["typecheck"].state == "required"
    assert by_name["sast"].state == "informational"
    # `format` is declared by the profile but its live pass is #134 -> not run.
    assert "format" not in by_name
    assert all(c.stage == "fast" for c in checks)


def test_default_profile_makes_the_test_check_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # #140 finding-1 fix: the `test` check's blocking is governed by the resolved
    # profile's `ProfileCheck("test", ...)` — the single source of truth — NOT a
    # separate `block_on_red` knob (removed). `standard` declares test required, so
    # the default test check blocks on RED with no extra config.
    monkeypatch.setattr(develop_mod, "_resolve_test_command", lambda c, w: "pytest")
    checks = build_check_set(_config(tmp_path), tmp_path)
    test = next(c for c in checks if c.name == "test")
    assert test.state == "required"


def test_minimal_profile_runs_only_lint_and_test(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(develop_mod, "_resolve_test_command", lambda c, w: "pytest")
    _python(monkeypatch)
    checks = build_check_set(_config(tmp_path, review_profile="minimal"), tmp_path)
    assert {c.name for c in checks} == {"lint", "test"}


def test_thorough_profile_stages_the_expensive_checks_as_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(develop_mod, "_resolve_test_command", lambda c, w: "pytest")
    _python(monkeypatch)
    checks = build_check_set(_config(tmp_path, review_profile="thorough"), tmp_path)
    stage = {c.name: c.stage for c in checks}
    assert stage["dep-audit"] == "candidate"
    assert stage["coverage"] == "candidate"
    assert stage["semgrep"] == "candidate"
    assert stage["lint"] == "fast"
    assert stage["typecheck"] == "fast"
    assert stage["test"] == "fast"


def test_required_absent_tool_blocks_informational_absent_drops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # #140 floor: with only ruff present in the image, the REQUIRED `typecheck`
    # (pyright) is absent -> a blocking expected-but-absent placeholder (empty
    # command, state required), while the INFORMATIONAL `sast` (bandit) absent stays
    # a silent drop. The required floor is no longer silently weakened by a missing
    # tool — it becomes an actionable block.
    monkeypatch.setattr(develop_mod, "_resolve_test_command", lambda c, w: "pytest")
    _python(monkeypatch, present=("ruff",))
    checks = build_check_set(_config(tmp_path), tmp_path)
    by_name = {c.name: c for c in checks}
    assert {c.name for c in checks} == {"lint", "typecheck", "test"}
    assert by_name["typecheck"].command == ""  # expected-but-absent placeholder
    assert by_name["typecheck"].state == "required"
    assert "sast" not in by_name  # informational absent -> dropped


def test_required_check_inapplicable_to_ecosystem_is_na_not_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # #140 floor + #133: `standard` declares `typecheck` (pyright) + `sast` (bandit)
    # required, but both are python/node-only. On a Rust repo they have no analogue —
    # they must resolve N/A (dropped), NOT raise CheckApplicabilityError before any
    # agent work (which would turn the default profile into a config failure for a
    # supported ecosystem). `lint` (cargo clippy) + `test` still apply.
    monkeypatch.setattr(develop_mod, "_resolve_test_command", lambda c, w: "cargo test")
    monkeypatch.setattr(
        develop_mod.detection, "detect_ecosystems", lambda wt: ("rust",)
    )
    monkeypatch.setattr(
        develop_mod.test_gate, "probe_tools", lambda image, tools: list(tools)
    )
    checks = build_check_set(_config(tmp_path), tmp_path)  # must not raise
    by_name = {c.name: c for c in checks}
    assert "typecheck" not in by_name  # python/node-only -> N/A for rust
    assert "sast" not in by_name  # bandit is python/node-only -> N/A for rust
    assert "lint" in by_name  # cargo clippy applies
    assert "test" in by_name


def test_no_informational_checks_without_an_ecosystem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(develop_mod, "_resolve_test_command", lambda c, w: "pytest")
    monkeypatch.setattr(develop_mod.detection, "detect_ecosystems", lambda wt: ())
    checks = build_check_set(_config(tmp_path), tmp_path)
    assert [c.name for c in checks] == ["test"]


def test_check_stage_defaults_to_fast() -> None:
    assert Check(name="lint", command="x", state="informational").stage == "fast"


# --- #132 Slice 3: _run_check_set ledgers findings + persistence ---------------

_RUFF_ONE = '[{"code":"E501","filename":"a.py","location":{"row":3},"message":"long"}]'


def _fake_export(wt: object, sha: object, dest: Path) -> None:
    Path(dest).mkdir(parents=True, exist_ok=True)


def test_run_check_set_ledgers_findings_and_drops_full_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(develop_mod.test_gate, "export_tree", _fake_export)
    monkeypatch.setattr(
        develop_mod.test_gate,
        "run_gate_container",
        lambda gate_cmd, *, name, command, timeout: GateResult(
            command=command,
            exit_code=0,
            passed=True,
            output_tail="t",
            full_output=_RUFF_ONE,
        ),
    )
    led = GateLedger()
    lint = Check("lint", "ruff check --output-format=json --exit-zero", "informational")
    cs = develop_mod._run_check_set(_config(tmp_path), tmp_path, "sha", 1, (lint,), led)
    assert [f.rule for f in led.open_findings()] == ["E501"]
    assert led.open_findings()[0].finding_id == "gate/lint-001"
    assert cs is not None
    assert cs.results[0].gate is not None
    assert cs.results[0].gate.full_output == ""  # consumed + dropped
    assert cs.results[0].gate.output_tail == "t"


def test_run_check_set_closes_lint_finding_on_clean_rerun(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(develop_mod.test_gate, "export_tree", _fake_export)
    outputs = iter([_RUFF_ONE, "[]"])
    monkeypatch.setattr(
        develop_mod.test_gate,
        "run_gate_container",
        lambda gate_cmd, *, name, command, timeout: GateResult(
            command=command,
            exit_code=0,
            passed=True,
            output_tail="t",
            full_output=next(outputs),
        ),
    )
    led = GateLedger()
    lint = Check("lint", "ruff check --output-format=json --exit-zero", "informational")
    develop_mod._run_check_set(_config(tmp_path), tmp_path, "s1", 1, (lint,), led)
    develop_mod._run_check_set(_config(tmp_path), tmp_path, "s2", 2, (lint,), led)
    assert led.open_findings() == []  # gone on the clean re-run -> closed
    assert led.all_findings()[0].status == "fixed"


def test_gate_ledger_persists_and_reloads(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    led = GateLedger()
    led.apply_round(
        "lint",
        [
            GateFinding(
                check="lint", tool="ruff", rule="E501", severity="major", message="x"
            )
        ],
        1,
    )
    develop_mod._persist_gate_ledger(cfg, led)
    assert develop_mod._gate_ledger_path(cfg).is_file()
    reloaded = develop_mod._load_gate_ledger(cfg)
    assert [f.finding_id for f in reloaded.all_findings()] == ["gate/lint-001"]
