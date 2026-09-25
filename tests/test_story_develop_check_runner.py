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
from lithos_loom.plugins.story_develop.gate_findings import GateFinding, GateLedger
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


# ── the durable record of WHAT blocked (converge-push's report) ──────────


def _ledger_with_ruff_finding() -> GateLedger:
    ledger = GateLedger()
    ledger.apply_round(
        "lint",
        [
            GateFinding(
                check="lint",
                tool="ruff",
                rule="F821",
                severity="major",
                message="undefined name `widget`",
                file="src/app.py",
                line=12,
            )
        ],
        round_no=1,
    )
    return ledger


def test_blocking_check_records_name_adapter_and_catalog_checks() -> None:
    """A standard-profile run stops with required ruff + pyright red and the
    test check GREEN. Neither red check is the legacy `test` gate, and neither
    is `raw_exit`, so a record built from `test_gate` + `failing_raw_checks`
    says "test GREEN, nothing blocked" — the report `develop converge-push`
    puts in front of the operator about to push an unapproved run's rounds.
    Both must be named, and ruff's finding (which IS its verdict) with it."""
    ledger = _ledger_with_ruff_finding()
    check_set = CheckSetResult(
        results=(
            # ruff runs with --exit-zero, so its raw verdict is GREEN: the
            # ledger's mapped severity is the only thing that blocks it
            CheckResult(Check("lint", "ruff check .", "required"), "ran", _gate(True)),
            CheckResult(Check("typecheck", "pyright", "required"), "ran", _gate(False)),
            CheckResult(Check("test", "pytest", "required"), "ran", _gate(True)),
        )
    )

    # what the record used to be built from, on this very check-set:
    assert check_set.failing_raw_checks == ()
    assert check_set.test_gate is not None and check_set.test_gate.passed

    records = check_runner.blocking_check_records(check_set, ledger)

    assert [r["name"] for r in records] == ["lint", "typecheck"]
    # the raw exit never blocked it — but the EFFECTIVE verdict is what held
    # approval, so the headline is RED with the process result kept beside it
    assert records[0]["verdict"] == "RED"
    assert records[0]["execution_verdict"] == "GREEN"
    assert records[0]["findings"] == [
        {
            "finding_id": "gate/lint-001",
            "severity": "major",
            "rule": "F821",
            "file": "src/app.py",
            "line": 12,
            "message": "undefined name `widget`",
        }
    ]
    assert records[1]["verdict"] == "RED" and records[1]["command"] == "pyright"
    assert records[1]["findings"] == []  # pyright has no adapter; the exit is all


def test_blocking_check_records_skip_what_does_not_block() -> None:
    # An informational check never blocks (its findings share the ledger), and
    # a green required check is not a blocker — naming either would teach the
    # operator to skim the list they are meant to read.
    ledger = _ledger_with_ruff_finding()
    check_set = CheckSetResult(
        results=(
            CheckResult(
                Check("lint", "ruff check .", "informational"), "ran", _gate(True)
            ),
            CheckResult(Check("test", "pytest", "required"), "ran", _gate(True)),
        )
    )
    assert check_runner.blocking_check_records(check_set, ledger) == []
    assert check_runner.blocking_check_records(None, ledger) == []


def test_blocking_check_records_name_a_verdictless_required_check() -> None:
    # An expected-but-absent required check blocks with no GateResult at all
    # (#133) — the record says what happened instead of dropping the row.
    check_set = CheckSetResult(
        results=(CheckResult(Check("test", "pytest", "required"), "absent", None),)
    )
    records = check_runner.blocking_check_records(check_set, None)
    assert [(r["name"], r["verdict"]) for r in records] == [("test", "ABSENT")]
