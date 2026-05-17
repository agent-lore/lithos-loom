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


# ── Log level + library-silencing wiring ────────────────────────────────


def test_configure_logging_silences_httpx_at_info_level() -> None:
    """At info/warning/error, httpx logs are demoted to WARNING.

    Otherwise the per-request POST log line drowns the source +
    subscriber lifecycle the operator is watching for.
    """
    import logging

    from lithos_loom.children.route_runner import _configure_logging

    # Reset the loggers to a known state so order-of-test doesn't pollute.
    logging.getLogger("httpx").setLevel(logging.NOTSET)
    logging.getLogger("httpx_sse").setLevel(logging.NOTSET)

    _configure_logging("info")

    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpx_sse").level == logging.WARNING


def test_configure_logging_does_not_silence_httpx_at_debug_level() -> None:
    """At debug, httpx is left alone so the operator sees every request.

    Operators asking for ``log_level = "debug"`` want the firehose —
    blanket-silencing the library loggers there would defeat the
    purpose. They get pinned back to NOTSET (root passthrough) so the
    root DEBUG level applies.
    """
    import logging

    from lithos_loom.children.route_runner import _configure_logging

    # Pre-pollute the httpx loggers so we can confirm _configure_logging
    # actively resets them when debug is requested.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpx_sse").setLevel(logging.WARNING)

    _configure_logging("debug")

    assert logging.getLogger("httpx").level == logging.NOTSET
    assert logging.getLogger("httpx_sse").level == logging.NOTSET
