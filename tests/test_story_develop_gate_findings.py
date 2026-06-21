"""The deterministic-finding ledger: stable ids, closure-by-rerun, suppression,
gate-ownership, persistence (#132)."""

from __future__ import annotations

from lithos_loom.plugins.story_develop.gate_findings import GateFinding, GateLedger


def _gf(
    check: str,
    rule: str,
    *,
    file: str = "a.py",
    line: int | None = 1,
    severity: str = "major",
    status: str = "open",
) -> GateFinding:
    return GateFinding(
        check=check,
        tool="ruff",
        rule=rule,
        severity=severity,
        message=f"{rule} msg",
        file=file,
        line=line,
        status=status,
    )


# --- GateFinding ---------------------------------------------------------------


def test_fingerprint_is_id_independent() -> None:
    a = _gf("lint", "E501")
    b = GateFinding(
        check="lint",
        tool="ruff",
        rule="E501",
        severity="minor",
        message="other",
        file="a.py",
        line=1,
        finding_id="gate/lint-009",
    )
    assert a.fingerprint == b.fingerprint  # identity ignores id/severity/message


def test_blocks_only_when_open_and_at_threshold() -> None:
    assert _gf("lint", "E501", severity="major").blocks("major") is True
    assert _gf("lint", "E501", severity="minor").blocks("major") is False
    assert _gf("lint", "E501", severity="critical").blocks("major") is True
    assert _gf("lint", "E501", status="fixed").blocks("minor") is False


# --- GateLedger: ids + closure -------------------------------------------------


def test_new_findings_get_monotonic_per_check_ids() -> None:
    led = GateLedger()
    led.apply_round(
        "lint", [_gf("lint", "E501", line=1), _gf("lint", "F401", line=2)], 1
    )
    ids = {f.rule: f.finding_id for f in led.all_findings()}
    assert ids == {"E501": "gate/lint-001", "F401": "gate/lint-002"}


def test_ids_are_namespaced_per_check() -> None:
    led = GateLedger()
    led.apply_round("lint", [_gf("lint", "E501")], 1)
    led.apply_round("sast", [_gf("sast", "B602")], 1)
    by_rule = {f.rule: f.finding_id for f in led.all_findings()}
    assert by_rule["E501"] == "gate/lint-001"
    assert by_rule["B602"] == "gate/sast-001"  # separate counter per check


def test_reappearing_fingerprint_keeps_its_id_and_stays_open() -> None:
    led = GateLedger()
    led.apply_round("lint", [_gf("lint", "E501", line=7)], 1)
    led.apply_round("lint", [_gf("lint", "E501", line=7)], 2)
    findings = led.all_findings()
    assert len(findings) == 1
    assert findings[0].finding_id == "gate/lint-001"
    assert findings[0].is_open


def test_vanished_finding_is_closed_fixed_by_rerun() -> None:
    led = GateLedger()
    led.apply_round("lint", [_gf("lint", "E501")], 1)
    led.apply_round("lint", [], 2)  # the check ran green — E501 gone
    findings = led.all_findings()
    assert len(findings) == 1
    assert findings[0].status == "fixed"
    assert led.open_findings() == []


def test_closure_is_scoped_to_the_check_that_ran() -> None:
    led = GateLedger()
    led.apply_round("sast", [_gf("sast", "B602")], 1)
    led.apply_round("lint", [], 2)  # only lint re-ran; sast did not
    sast = next(f for f in led.all_findings() if f.check == "sast")
    assert sast.is_open  # a sast finding is NOT closed by a lint round


def test_fixed_finding_reopens_with_same_id_when_it_returns() -> None:
    led = GateLedger()
    led.apply_round("lint", [_gf("lint", "E501")], 1)
    led.apply_round("lint", [], 2)  # fixed
    led.apply_round("lint", [_gf("lint", "E501")], 3)  # regressed
    f = led.all_findings()[0]
    assert f.finding_id == "gate/lint-001" and f.is_open


# --- suppression + blocking ----------------------------------------------------


def test_suppressed_finding_is_recorded_but_never_blocks() -> None:
    led = GateLedger()
    led.apply_round("lint", [_gf("lint", "E501", status="suppressed")], 1)
    assert led.open_findings() == []
    assert led.blocking("minor") == []
    assert led.all_findings()[0].status == "suppressed"


def test_blocking_respects_threshold() -> None:
    led = GateLedger()
    led.apply_round(
        "lint",
        [
            _gf("lint", "E501", severity="minor", line=1),
            _gf("lint", "B602", severity="critical", line=2),
        ],
        1,
    )
    blocking = {f.rule for f in led.blocking("major")}
    assert blocking == {"B602"}  # the minor one is recorded, non-blocking
    assert led.blocking_passed("major") is False
    assert led.blocking_passed("critical") is False


# --- persistence (cross-round / resume) ----------------------------------------


def test_persistence_round_trips_ids_status_and_counter() -> None:
    led = GateLedger()
    led.apply_round(
        "lint", [_gf("lint", "E501", line=1), _gf("lint", "F401", line=2)], 1
    )
    led.apply_round("lint", [_gf("lint", "E501", line=1)], 2)  # F401 -> fixed

    restored = GateLedger.from_jsonable(led.to_jsonable())
    by_rule = {f.rule: f for f in restored.all_findings()}
    assert by_rule["E501"].finding_id == "gate/lint-001" and by_rule["E501"].is_open
    assert by_rule["F401"].status == "fixed"

    # the per-check counter survives, so the next new finding continues the sequence
    restored.apply_round(
        "lint", [_gf("lint", "E501", line=1), _gf("lint", "E711", line=9)], 3
    )
    new = next(f for f in restored.all_findings() if f.rule == "E711")
    assert new.finding_id == "gate/lint-003"
