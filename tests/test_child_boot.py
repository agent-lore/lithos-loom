"""Tests for the shared child-boot scaffolding (ARCH-6).

The three supervisor-spawned children (route_runner / obsidian_sync /
github_watcher) used to each carry a byte-identical copy of this logging /
arg-parsing / signal-install code; it now lives once in
``children/_boot.py``. These tests pin the extracted behaviour directly.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from collections.abc import Iterator
from typing import cast

import pytest

from lithos_loom.children import _boot


@pytest.fixture
def _restore_logger_levels() -> Iterator[None]:
    """Snapshot + restore the loggers ``configure_logging`` mutates.

    ``configure_logging`` sets global logger levels; without this a test's
    mutation would leak into later tests' logging assertions."""
    names = [*_boot.NOISY_LIBRARY_LOGGERS, "mcp.client.sse"]
    saved = {name: logging.getLogger(name).level for name in names}
    yield
    for name, level in saved.items():
        logging.getLogger(name).setLevel(level)


def test_configure_logging_pins_noisy_loggers_below_debug(
    _restore_logger_levels: None,
) -> None:
    _boot.configure_logging("info")
    for name in _boot.NOISY_LIBRARY_LOGGERS:
        assert logging.getLogger(name).level == logging.WARNING
    assert logging.getLogger("mcp.client.sse").level == logging.CRITICAL


def test_configure_logging_frees_noisy_loggers_at_debug(
    _restore_logger_levels: None,
) -> None:
    _boot.configure_logging("debug")
    for name in _boot.NOISY_LIBRARY_LOGGERS:
        # NOTSET = inherit the root level, i.e. the full firehose.
        assert logging.getLogger(name).level == logging.NOTSET
    # The MCP SSE traceback is pinned CRITICAL regardless of level.
    assert logging.getLogger("mcp.client.sse").level == logging.CRITICAL


def test_parse_child_args_reads_config_path() -> None:
    ns = _boot.parse_child_args("prog", ["--config", "/tmp/x.toml"])
    assert str(ns.config) == "/tmp/x.toml"


def test_parse_child_args_defaults_config_to_none() -> None:
    ns = _boot.parse_child_args("prog", [])
    assert ns.config is None


class _NoSignalLoop:
    """A loop whose signal methods are unsupported.

    Models the platforms ``install_stop_signals`` is written to survive
    (e.g. Windows Proactor, some embedded loops) where ``add_signal_handler``
    raises ``NotImplementedError``."""

    def add_signal_handler(self, *args: object, **kwargs: object) -> None:
        raise NotImplementedError

    def remove_signal_handler(self, *args: object, **kwargs: object) -> None:
        raise NotImplementedError


def test_install_stop_signals_suppresses_unsupported_platform() -> None:
    """On a loop without signal support, install registers nothing and
    does not raise — the child still boots (the supervisor terminates it
    via process signals regardless)."""
    loop = cast("asyncio.AbstractEventLoop", _NoSignalLoop())
    installed = _boot.install_stop_signals(loop, lambda: None)
    assert installed == []
    # remove must swallow NotImplementedError too, even handed a signal.
    _boot.remove_stop_signals(loop, [signal.SIGTERM])  # no raise


async def test_install_stop_signals_returns_registered_signals() -> None:
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    installed = _boot.install_stop_signals(loop, stop.set)
    try:
        assert set(installed) == {signal.SIGTERM, signal.SIGINT}
    finally:
        _boot.remove_stop_signals(loop, installed)


async def test_installed_handler_trips_the_stop_callback() -> None:
    """End-to-end: a delivered SIGINT sets the event via the installed
    handler (SIGINT's default is overridden while our handler is live).

    Skipped on a loop that can't install signal handlers — the helper
    returns an empty list there, which the suppress-branch test covers."""
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    installed = _boot.install_stop_signals(loop, stop.set)
    if signal.SIGINT not in installed:
        _boot.remove_stop_signals(loop, installed)
        pytest.skip("event loop does not support signal handlers")
    try:
        signal.raise_signal(signal.SIGINT)
        async with asyncio.timeout(1.0):
            await stop.wait()
        assert stop.is_set()
    finally:
        _boot.remove_stop_signals(loop, installed)


async def test_remove_stop_signals_is_safe_on_empty_list() -> None:
    loop = asyncio.get_running_loop()
    # No-op, must not raise — the "child ran to process exit" path.
    _boot.remove_stop_signals(loop, [])


# ── drain signal (#407 slice 3) ──────────────────────────────────────────


def test_install_drain_signal_suppresses_unsupported_platform() -> None:
    loop = cast("asyncio.AbstractEventLoop", _NoSignalLoop())
    assert _boot.install_drain_signal(loop, lambda: None) == []


async def test_install_drain_signal_registers_sigusr1_only() -> None:
    loop = asyncio.get_running_loop()
    drain = asyncio.Event()
    installed = _boot.install_drain_signal(loop, drain.set)
    try:
        assert installed == [signal.SIGUSR1]
    finally:
        _boot.remove_stop_signals(loop, installed)


async def test_a_delivered_sigusr1_trips_the_drain_callback_not_the_stop() -> None:
    loop = asyncio.get_running_loop()
    stop, drain = asyncio.Event(), asyncio.Event()
    installed = _boot.install_stop_signals(loop, stop.set)
    installed += _boot.install_drain_signal(loop, drain.set)
    if signal.SIGUSR1 not in installed:
        _boot.remove_stop_signals(loop, installed)
        pytest.skip("event loop does not support signal handlers")
    try:
        signal.raise_signal(signal.SIGUSR1)
        async with asyncio.timeout(1.0):
            await drain.wait()
        assert not stop.is_set()
    finally:
        _boot.remove_stop_signals(loop, installed)


class _FakeDrainable:
    def __init__(self, *, finish: asyncio.Event | None = None) -> None:
        self.began = False
        self._finish = finish

    def begin_drain(self) -> None:
        self.began = True

    async def drained(self) -> None:
        if self._finish is not None:
            await self._finish.wait()


async def test_run_until_stopped_returns_on_stop_without_draining() -> None:
    stop, drain = asyncio.Event(), asyncio.Event()
    d = _FakeDrainable()
    task = asyncio.create_task(_boot.run_until_stopped(stop, drain, [d]))
    await asyncio.sleep(0.01)
    assert not task.done()
    stop.set()
    await asyncio.wait_for(task, 1.0)
    assert d.began is False


async def test_run_until_stopped_drains_every_drainable_then_returns() -> None:
    stop, drain = asyncio.Event(), asyncio.Event()
    finish = asyncio.Event()
    slow, quick = _FakeDrainable(finish=finish), _FakeDrainable()
    task = asyncio.create_task(_boot.run_until_stopped(stop, drain, [slow, quick]))
    drain.set()
    await asyncio.sleep(0.01)
    assert slow.began and quick.began
    assert not task.done()  # the slow one is still finishing its run
    finish.set()
    await asyncio.wait_for(task, 1.0)


async def test_a_stop_during_the_drain_ends_the_wait_at_once() -> None:
    stop, drain = asyncio.Event(), asyncio.Event()
    never = asyncio.Event()
    d = _FakeDrainable(finish=never)
    task = asyncio.create_task(_boot.run_until_stopped(stop, drain, [d]))
    drain.set()
    await asyncio.sleep(0.01)
    assert d.began and not task.done()
    stop.set()
    await asyncio.wait_for(task, 1.0)


async def test_run_until_stopped_with_drain_already_set_still_drains() -> None:
    stop, drain = asyncio.Event(), asyncio.Event()
    drain.set()
    d = _FakeDrainable()
    await asyncio.wait_for(_boot.run_until_stopped(stop, drain, [d]), 1.0)
    assert d.began


def test_importing_the_boot_helper_ignores_sigusr1_until_a_loop_handler_exists() -> (
    None
):
    """SIGUSR1's default is to terminate: a drain relayed while a child is
    still importing (~0.5 s) would kill it and read as a crash. The boot
    helper is imported before the heavy modules, so the ignore lands first;
    `install_drain_signal` then replaces it with the real handler."""
    import subprocess
    import sys

    out = subprocess.run(
        [
            sys.executable,
            "-c",
            "import signal, lithos_loom.children._boot; "
            "print(signal.getsignal(signal.SIGUSR1) is signal.SIG_IGN)",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout.strip() == "True"
