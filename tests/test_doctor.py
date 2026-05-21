"""Unit tests for the doctor check framework + vault probes (US15)."""

from __future__ import annotations

from pathlib import Path

import pytest

from lithos_loom.config import (
    LoomConfig,
    ObsidianSyncConfig,
    OrchestratorConfig,
)
from lithos_loom.doctor import (
    PROBE_FILENAME,
    CheckResult,
    _check_lithos_subdir_creatable,
    _check_probe_write_read,
    _check_vault_path_exists,
    format_results,
    run_vault_checks,
)


def _cfg(
    tmp_path: Path,
    *,
    with_obsidian_sync: bool = True,
) -> LoomConfig:
    """Minimal config with vault_path = tmp_path; obsidian_sync optional."""
    obs = None
    if with_obsidian_sync:
        obs = ObsidianSyncConfig(
            vault_path=tmp_path,
            tasks_file=Path("_lithos/tasks.md"),
        )
    return LoomConfig(
        orchestrator=OrchestratorConfig(
            agent_id="lithos-orchestrator-test",
            lithos_url="http://localhost:8765",
        ),
        routes=(),
        obsidian_sync=obs,
    )


# ── _check_vault_path_exists ───────────────────────────────────────────


def test_vault_path_exists_passes_for_directory(tmp_path: Path) -> None:
    result = _check_vault_path_exists(tmp_path)
    assert result.passed is True
    assert result.name == "vault_path_exists"
    assert str(tmp_path) in result.message


def test_vault_path_exists_fails_when_missing(tmp_path: Path) -> None:
    missing = tmp_path / "no-such-vault"
    result = _check_vault_path_exists(missing)
    assert result.passed is False
    assert "does not exist" in result.message
    assert str(missing) in result.message


def test_vault_path_exists_fails_when_path_is_a_file(tmp_path: Path) -> None:
    """A regular file at vault_path is a config error — operator pointed
    at the wrong thing. Report clearly."""
    not_a_dir = tmp_path / "file"
    not_a_dir.write_text("oops")
    result = _check_vault_path_exists(not_a_dir)
    assert result.passed is False
    assert "not a directory" in result.message


# ── _check_lithos_subdir_creatable ─────────────────────────────────────


def test_lithos_subdir_creatable_creates_when_missing(tmp_path: Path) -> None:
    assert not (tmp_path / "_lithos").exists()
    result = _check_lithos_subdir_creatable(tmp_path)
    assert result.passed is True
    assert (tmp_path / "_lithos").is_dir()


def test_lithos_subdir_creatable_idempotent_when_present(tmp_path: Path) -> None:
    """Pre-existing _lithos/ → passes without error (mkdir exist_ok=True)."""
    (tmp_path / "_lithos").mkdir()
    result = _check_lithos_subdir_creatable(tmp_path)
    assert result.passed is True


def test_lithos_subdir_creatable_fails_on_oserror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Permission flip / read-only mount → OSError → reported failure
    with the underlying message so the operator can act."""

    def _explode(self: Path, *, parents: bool = False, exist_ok: bool = False) -> None:
        raise PermissionError("simulated read-only filesystem")

    monkeypatch.setattr(Path, "mkdir", _explode)
    result = _check_lithos_subdir_creatable(tmp_path)
    assert result.passed is False
    assert "simulated read-only filesystem" in result.message


# ── _check_probe_write_read ────────────────────────────────────────────


def test_probe_write_read_roundtrip_succeeds_and_cleans_up(
    tmp_path: Path,
) -> None:
    (tmp_path / "_lithos").mkdir()
    result = _check_probe_write_read(tmp_path)
    assert result.passed is True
    assert "round-tripped" in result.message
    # Cleanup ran — no probe file lingering.
    assert not (tmp_path / "_lithos" / PROBE_FILENAME).exists()


def test_probe_write_read_fails_on_write_oserror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "_lithos").mkdir()

    def _fail_write(self: Path, data: str, encoding: str | None = None) -> int:
        raise PermissionError("simulated write failure")

    monkeypatch.setattr(Path, "write_text", _fail_write)
    result = _check_probe_write_read(tmp_path)
    assert result.passed is False
    assert "simulated write failure" in result.message
    # The write didn't happen — no probe file present.
    assert not (tmp_path / "_lithos" / PROBE_FILENAME).exists()


def test_probe_write_read_fails_on_readback_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If readback returns content that doesn't match what was written
    (corrupted FS, weird mount, hardware fault), report mismatch and
    leave the probe on disk for inspection."""
    (tmp_path / "_lithos").mkdir()

    def _tamper(self: Path, encoding: str | None = None) -> str:
        return "not what was written\n"

    monkeypatch.setattr(Path, "read_text", _tamper)
    result = _check_probe_write_read(tmp_path)
    assert result.passed is False
    assert "readback mismatch" in result.message
    # Probe file remains on disk (we wrote it before reading) — useful
    # for the operator to inspect.
    assert (tmp_path / "_lithos" / PROBE_FILENAME).exists()


# ── run_vault_checks orchestration ─────────────────────────────────────


def test_run_vault_checks_returns_empty_when_no_obsidian_sync(
    tmp_path: Path,
) -> None:
    """Hosts without [obsidian_sync] configured shouldn't see vault
    failures — caller (CLI) reports the skip."""
    cfg = _cfg(tmp_path, with_obsidian_sync=False)
    assert run_vault_checks(cfg) == []


def test_run_vault_checks_short_circuits_on_first_failure(tmp_path: Path) -> None:
    """If vault_path doesn't exist, don't bother trying to mkdir or
    probe inside it — subsequent failures would cascade pointlessly."""
    cfg = _cfg(tmp_path / "absent")
    results = run_vault_checks(cfg)
    assert len(results) == 1
    assert results[0].name == "vault_path_exists"
    assert results[0].passed is False


def test_run_vault_checks_returns_three_passes_for_healthy_vault(
    tmp_path: Path,
) -> None:
    cfg = _cfg(tmp_path)
    results = run_vault_checks(cfg)
    assert [r.name for r in results] == [
        "vault_path_exists",
        "lithos_subdir_creatable",
        "probe_write_read_roundtrip",
    ]
    assert all(r.passed for r in results)


# ── format_results ─────────────────────────────────────────────────────


def test_format_results_renders_check_marks_and_messages() -> None:
    rendered = format_results(
        [
            CheckResult("alpha", True, "all good"),
            CheckResult("beta", False, "oh no"),
        ]
    )
    assert rendered == [
        "  ✓ alpha: all good",
        "  ✗ beta: oh no",
    ]
