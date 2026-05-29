"""Subprocess + smoke tests for the github-watcher child entry.

Confirms the supervisor can ``python -m`` the child cleanly. Without a
real Lithos and a real ``gh`` login the child can't actually do work,
but the disabled-gate path returns 0 immediately and is testable in
the CI sandbox.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from textwrap import dedent


def _no_watcher_config(tmp_path: Path) -> Path:
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        dedent(
            """
            [orchestrator]
            agent_id = "lithos-orchestrator-test"
            lithos_url = "http://localhost:8765"
            """
        )
    )
    return cfg


def _disabled_watcher_config(tmp_path: Path) -> Path:
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        dedent(
            """
            [orchestrator]
            agent_id = "lithos-orchestrator-test"
            lithos_url = "http://localhost:8765"

            [github_watcher]
            enabled = false
            """
        )
    )
    return cfg


async def test_github_watcher_child_exits_nonzero_without_section(
    tmp_path: Path,
) -> None:
    """Defensive: section missing → child exits non-zero so supervisor sees it."""
    cfg = _no_watcher_config(tmp_path)
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "lithos_loom.children.github_watcher",
        "--config",
        str(cfg),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    rc = await asyncio.wait_for(proc.wait(), timeout=10.0)
    assert rc == 1


async def test_github_watcher_child_exits_nonzero_when_disabled(
    tmp_path: Path,
) -> None:
    """Same defensive behaviour when the section is present but enabled=false."""
    cfg = _disabled_watcher_config(tmp_path)
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "lithos_loom.children.github_watcher",
        "--config",
        str(cfg),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    rc = await asyncio.wait_for(proc.wait(), timeout=10.0)
    assert rc == 1


async def test_github_watcher_child_module_is_importable() -> None:
    """The child module must expose ``main`` and be runnable via -m."""
    import lithos_loom.children.github_watcher as mod

    assert callable(mod.main)


def test_configure_logging_silences_mcp_sse_at_critical() -> None:
    """At any level, the MCP SDK's per-reconnect tracebacks are pinned to CRITICAL.

    Same noise suppression as obsidian-sync — without this, every Lithos
    restart shows an SDK traceback that buries our own reconnect timeline.
    """
    import logging

    from lithos_loom.children.github_watcher import _configure_logging

    logging.getLogger("mcp.client.sse").setLevel(logging.NOTSET)
    _configure_logging("info")
    assert logging.getLogger("mcp.client.sse").level == logging.CRITICAL
