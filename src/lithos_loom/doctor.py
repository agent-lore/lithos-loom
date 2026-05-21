"""Check-runner framework + per-domain probes for ``lithos-loom doctor``.

US15 (Slice 1) ships the vault probes — verify ``vault_path`` exists,
``_lithos/`` is creatable, and a write+read round-trip works. US-35
(MVP) will add Lithos connectivity and ``task.metadata`` round-trip
checks via the same framework.

Public surface:

* :class:`CheckResult` — frozen dataclass with ``name``, ``passed``,
  ``message``.
* :func:`run_vault_checks` — returns ``list[CheckResult]``; empty
  when ``[obsidian_sync]`` isn't configured (caller decides how to
  report the skip).
* :func:`format_results` — pretty-print to a list of lines for the
  CLI to echo.

Pure functions, no I/O outside the probes themselves. No MCP / Lithos
calls in this module — that's US-35 territory.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from lithos_loom.config import LoomConfig

PROBE_FILENAME = ".doctor-probe.tmp"
"""Fixed filename used for the write+read round-trip probe.

Single name (rather than timestamped) so re-runs don't accumulate
files in the vault. Deleted on successful round-trip; left on
failure for operator inspection.
"""


@dataclass(frozen=True)
class CheckResult:
    """One doctor-check outcome.

    The CLI walks a ``list[CheckResult]`` to compute the summary +
    exit code. Frozen so test fixtures can rely on equality.
    """

    name: str
    passed: bool
    message: str


def run_vault_checks(cfg: LoomConfig) -> list[CheckResult]:
    """Run the three vault probes from US15 against ``cfg``.

    Returns an empty list when ``[obsidian_sync]`` isn't configured —
    the caller (the ``doctor`` CLI command) prints a skip note in that
    case rather than treating it as a failure. Respects the spawn-gate
    model: hosts that don't run the projection child shouldn't see
    spurious vault failures.

    Short-circuits on the first failed check (subsequent checks would
    cascade — there's no point trying to write a probe file when the
    vault directory itself doesn't exist).
    """
    obs = cfg.obsidian_sync
    if obs is None:
        return []
    results: list[CheckResult] = [_check_vault_path_exists(obs.vault_path)]
    if not results[-1].passed:
        return results
    results.append(_check_lithos_subdir_creatable(obs.vault_path))
    if not results[-1].passed:
        return results
    results.append(_check_probe_write_read(obs.vault_path))
    return results


def _check_vault_path_exists(vault_path: Path) -> CheckResult:
    """Verify ``vault_path`` exists as a directory.

    ``Path.exists()`` follows symlinks, so a broken symlink reports
    as missing — the right behaviour (operator's config points at
    something that isn't actually there).
    """
    if not vault_path.exists():
        return CheckResult(
            "vault_path_exists",
            False,
            f"{vault_path} does not exist",
        )
    if not vault_path.is_dir():
        return CheckResult(
            "vault_path_exists",
            False,
            f"{vault_path} exists but is not a directory",
        )
    return CheckResult("vault_path_exists", True, str(vault_path))


def _check_lithos_subdir_creatable(vault_path: Path) -> CheckResult:
    """Verify ``<vault_path>/_lithos/`` exists or can be created.

    ``mkdir(parents=True, exist_ok=True)`` is idempotent — already-
    present subdir is fine. Catches ``OSError`` (permissions,
    read-only mount, weird filesystem) and reports the underlying
    message so the operator can act.
    """
    subdir = vault_path / "_lithos"
    try:
        subdir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return CheckResult(
            "lithos_subdir_creatable",
            False,
            f"could not create {subdir}: {exc}",
        )
    return CheckResult("lithos_subdir_creatable", True, str(subdir))


def _check_probe_write_read(vault_path: Path) -> CheckResult:
    """Write a dated probe string, read it back, assert equality.

    Cleans up on success. Leaves the probe file on disk for operator
    inspection when the round-trip fails — the dated content makes
    it easy to spot when investigating.
    """
    probe = vault_path / "_lithos" / PROBE_FILENAME
    content = f"lithos-loom doctor probe at {datetime.now(UTC).isoformat()}\n"
    try:
        probe.write_text(content, encoding="utf-8")
        readback = probe.read_text(encoding="utf-8")
    except OSError as exc:
        return CheckResult(
            "probe_write_read_roundtrip",
            False,
            f"could not write/read {probe}: {exc}",
        )
    if readback != content:
        return CheckResult(
            "probe_write_read_roundtrip",
            False,
            f"readback mismatch at {probe}",
        )
    # Best-effort cleanup; a failed unlink doesn't invalidate the
    # round-trip success (the file's just lingering, not corrupting).
    with contextlib.suppress(OSError):
        probe.unlink()
    return CheckResult(
        "probe_write_read_roundtrip",
        True,
        f"{len(content)} bytes round-tripped",
    )


def format_results(results: list[CheckResult]) -> list[str]:
    """Render check results as indented bullet lines for CLI echo."""
    lines: list[str] = []
    for r in results:
        mark = "✓" if r.passed else "✗"
        lines.append(f"  {mark} {r.name}: {r.message}")
    return lines
