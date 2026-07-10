"""Shared boot scaffolding for supervisor-spawned children.

Every child (``route_runner``, ``obsidian_sync``, ``github_watcher``) is a
``python -m lithos_loom.children.<name> --config <path>`` subprocess with
the same startup shape: parse ``--config``, configure root logging while
silencing the noisy HTTP + MCP-SSE loggers, and install SIGTERM/SIGINT
handlers that trip a stop event. This module owns that shape so a change
(a new noisy logger, a log-format tweak, a signal) lands once instead of
in three near-identical copies.

Signal handling is split install/remove so it fits each child's existing
teardown: :func:`install_stop_signals` returns the signals it actually
registered (``add_signal_handler`` is unavailable on some event loops /
platforms — e.g. Windows Proactor — so it suppresses ``NotImplementedError``
and reports what stuck), and :func:`remove_stop_signals` tears exactly
those down. A child that runs to process exit can drop the returned list;
the long-lived children remove on shutdown so a re-run in the same process
(tests) doesn't leak handlers.
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import signal
from asyncio import AbstractEventLoop
from collections.abc import Callable, Sequence
from pathlib import Path

from lithos_loom.config import LogLevel

_LEVEL_MAP: dict[LogLevel, int] = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}

# httpx logs every HTTP request at INFO — every Lithos MCP POST, the SSE
# GET, and (in the github watcher) every GitHub API call — which drowns
# out the source / subscriber / handler lifecycle the operator is watching
# for. At ``debug`` the operator asked for the firehose; otherwise these
# are pinned to WARNING so the application logs aren't lost in the noise.
NOISY_LIBRARY_LOGGERS = ("httpx", "httpx_sse")


def configure_logging(level: LogLevel) -> None:
    """Configure root logging at ``level`` and silence noisy libraries.

    At ``"debug"`` the library loggers are left at the root level so every
    HTTP request surfaces — operators asking for debug want the full
    firehose. At any other level they are pinned to WARNING.

    The MCP SDK's SSE reader (``mcp.client.sse.sse_reader``) logs a full
    ERROR-level traceback whenever its persistent session is torn down —
    e.g. every time Lithos restarts. The children hold long-lived
    ``LithosClient``s whose own reconnect loops (plus the subscription
    retry policy) own recovery, so that traceback is just noise burying the
    real reconnect timeline. Pin it to CRITICAL so genuine failures (auth,
    protocol) still surface but the routine "peer closed connection" trace
    does not.
    """
    logging.basicConfig(
        level=_LEVEL_MAP[level],
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    noisy_level = logging.NOTSET if level == "debug" else logging.WARNING
    for name in NOISY_LIBRARY_LOGGERS:
        logging.getLogger(name).setLevel(noisy_level)
    logging.getLogger("mcp.client.sse").setLevel(logging.CRITICAL)


def parse_child_args(
    prog: str, argv: Sequence[str] | None = None
) -> argparse.Namespace:
    """Parse the shared child CLI (``--config <path>``).

    ``prog`` is the module path the supervisor invokes
    (``lithos_loom.children.<name>``) so ``--help`` names the right child.
    """
    parser = argparse.ArgumentParser(prog=prog)
    parser.add_argument("--config", type=Path, default=None)
    return parser.parse_args(argv)


def install_stop_signals(
    loop: AbstractEventLoop, callback: Callable[[], object]
) -> list[int]:
    """Register ``callback`` for SIGTERM + SIGINT; return the signals that stuck.

    ``add_signal_handler`` raises ``NotImplementedError`` on some loops /
    platforms; that is suppressed so a child still boots (the supervisor
    terminates children via process signals regardless). Pass the returned
    list to :func:`remove_stop_signals` to tear exactly these down.
    """
    installed: list[int] = []
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, callback)
            installed.append(sig)
    return installed


def remove_stop_signals(loop: AbstractEventLoop, installed: Sequence[int]) -> None:
    """Remove the signal handlers :func:`install_stop_signals` registered."""
    for sig in installed:
        with contextlib.suppress(NotImplementedError):
            loop.remove_signal_handler(sig)
