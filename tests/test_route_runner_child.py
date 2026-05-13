"""Subprocess smoke tests for the route-runner child entry (Slice 0 US5).

Confirms the supervisor can ``python -m`` the child cleanly. The no-routes
path returns 0 immediately and is the minimum we can exercise without a
live Lithos. SIGTERM-handling on the routes path is exercised indirectly
via :mod:`tests.test_supervisor` (the supervisor's ``_echo`` child uses
the same signal-handler scaffolding) and through operator smoke runs;
mocking the MCP-over-SSE transport in a subprocess test is out of scope
for slice 0.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from textwrap import dedent


def _no_routes_config(tmp_path: Path) -> Path:
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


async def test_route_runner_child_exits_zero_with_no_routes(tmp_path: Path) -> None:
    """A config with zero routes makes the child exit cleanly without a Lithos."""
    cfg = _no_routes_config(tmp_path)
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "lithos_loom.children.route_runner",
        "--config",
        str(cfg),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    rc = await asyncio.wait_for(proc.wait(), timeout=10.0)
    assert rc == 0


async def test_route_runner_child_module_is_importable() -> None:
    """The child module must expose ``main`` and be runnable via -m."""
    import lithos_loom.children.route_runner as mod

    assert callable(mod.main)
