"""Supervisor: top-level lifecycle manager for the Loom daemon.

Reads a ``LoomConfig``, fans out one subprocess child per *enabled category*,
propagates a graceful shutdown to all of them on SIGTERM/SIGINT, and surfaces
a single exit code summarising the run.

A *category* is a coarse-grained piece of behaviour the operator might want
to isolate from the rest — e.g. the route-runner or the obsidian-sync child.
Categories run as subprocess children so a crash in one cannot take down
siblings; v1 lifecycle is monolithic and does not auto-restart (child crash
detection is informational; restart is not required in v1).

Concretely the supervisor:

1. Filters the configured categories by ``CategorySpec.enabled(cfg)``.
2. Spawns each as ``python -m <module> --config <source_path> <extra_args...>``.
3. Installs SIGTERM/SIGINT handlers that signal a single shutdown event.
4. Waits on either the shutdown event *or* an early child exit (a crash);
   either way it then SIGTERMs all still-alive children, waits up to
   ``shutdown_grace_seconds``, and SIGKILLs anything that overstays.
5. Returns 0 if every child exited cleanly via supervisor-initiated shutdown;
   non-zero if any child crashed before shutdown or had to be force-killed.

**Drain (#407 slice 3).** ``lithos-loom drain`` sends the supervisor
SIGUSR1. The supervisor relays it to every live child and then *waits*: a
draining child stops admitting new dispatch, finishes what is in flight and
exits 0 on its own — no SIGTERM, no grace clock — and the supervisor
returns 0 once the last one has. A non-zero exit during the drain is a
crash like any other (recorded; the rest are stopped), and SIGTERM / SIGINT
during a drain is the ordinary shutdown (the remaining children are
terminated, in-flight remediation refunded by the child as usual).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from lithos_loom.config import LoomConfig

__all__ = [
    "CategorySpec",
    "ChildProcess",
    "Supervisor",
    "default_categories",
]

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CategorySpec:
    """A bundle of behaviour the supervisor can spawn as a subprocess child.

    ``module`` is invoked as ``python -m <module>``. The supervisor appends
    ``--config <cfg.source_path>`` (when set) and any ``extra_args``.
    """

    name: str
    module: str
    enabled: Callable[[LoomConfig], bool] = field(default=lambda _cfg: True)
    extra_args: tuple[str, ...] = ()


@dataclass
class ChildProcess:
    """Live wrapper around an asyncio subprocess plus its category spec."""

    spec: CategorySpec
    proc: asyncio.subprocess.Process
    argv: tuple[str, ...]


class Supervisor:
    """Manages subprocess children for the configured categories."""

    def __init__(
        self,
        cfg: LoomConfig,
        categories: Iterable[CategorySpec],
        *,
        shutdown_grace_seconds: float = 5.0,
    ) -> None:
        self.cfg = cfg
        self._enabled = tuple(c for c in categories if c.enabled(cfg))
        self._shutdown_grace_seconds = shutdown_grace_seconds
        self._children: list[ChildProcess] = []
        self._shutdown_event: asyncio.Event | None = None
        self._drain_event: asyncio.Event | None = None
        self._draining = False
        self._crashes: list[str] = []

    @property
    def children(self) -> tuple[ChildProcess, ...]:
        return tuple(self._children)

    @property
    def crashes(self) -> tuple[str, ...]:
        return tuple(self._crashes)

    async def shutdown(self) -> None:
        """Signal a graceful shutdown. Safe to call before or during ``run``."""
        if self._shutdown_event is not None:
            self._shutdown_event.set()

    async def drain(self) -> None:
        """Ask every child to finish what is in flight and exit (SIGUSR1).

        Safe to call before or during ``run``; a no-op before ``run``
        (there is nothing to drain — the event is created by ``run``).
        Called again during a drain it relays the signal again — a child
        that was still importing ignored the first one.
        """
        self._on_drain_signal()

    async def run(self) -> int:
        """Spawn children, wait for shutdown or a crash, clean up, return exit code."""
        if not self._enabled:
            return 0

        loop = asyncio.get_running_loop()
        self._shutdown_event = asyncio.Event()
        self._drain_event = asyncio.Event()
        self._draining = False

        self._install_signal_handlers(loop)

        try:
            try:
                await self._spawn_all()
                await self._wait_for_shutdown_or_crash()
            finally:
                # Always reap children — including those spawned before a
                # later spawn raised, or before the run task was cancelled.
                # Without this, a partial-startup failure would orphan
                # subprocesses and break the "single start/stop surface".
                await self._terminate_remaining()
        finally:
            self._uninstall_signal_handlers(loop)

        return 0 if not self._failed() else 1

    # ── Internals ──────────────────────────────────────────────────────

    async def _spawn_all(self) -> None:
        for spec in self._enabled:
            child = await self._spawn(spec)
            self._children.append(child)

    async def _spawn(self, spec: CategorySpec) -> ChildProcess:
        argv = [sys.executable, "-m", spec.module]
        if self.cfg.source_path is not None:
            argv += ["--config", str(self.cfg.source_path)]
        argv += list(spec.extra_args)
        proc = await asyncio.create_subprocess_exec(*argv)
        logger.info("supervisor: spawned child %s (pid=%d)", spec.name, proc.pid)
        return ChildProcess(spec=spec, proc=proc, argv=tuple(argv))

    async def _wait_for_shutdown_or_crash(self) -> None:
        """Park until shutdown, a crash, or — after a drain — the last child exit.

        Outside a drain any child exit is a crash. During a drain a child's
        exit 0 is the expected end of its drain; the wait continues until
        every child has exited that way (return: nothing left to terminate).
        A non-zero exit is a crash in either mode, and shutdown wins over a
        drain in progress.
        """
        assert self._shutdown_event is not None
        assert self._drain_event is not None
        waits = {
            asyncio.create_task(c.proc.wait(), name=f"wait-{c.spec.name}"): c
            for c in self._children
        }
        shutdown_task = asyncio.create_task(
            self._shutdown_event.wait(), name="shutdown"
        )
        drain_task = asyncio.create_task(self._drain_event.wait(), name="drain")
        try:
            while waits:
                # the drain task leaves the set once it fired: a completed
                # task in the set would return every wait at once (a core
                # spent spinning beside the run the drain protects)
                pending = set(waits) | {shutdown_task}
                if not self._draining:
                    pending.add(drain_task)
                done, _ = await asyncio.wait(
                    pending, return_when=asyncio.FIRST_COMPLETED
                )
                if shutdown_task in done:
                    return
                # child exits are judged under the flag as it was when they
                # happened: an exit that landed with the drain event in the
                # same tick preceded the relay, so it is a crash
                for task in [t for t in done if t in waits]:
                    child = waits.pop(task)
                    rc = child.proc.returncode
                    if self._draining and rc == 0:
                        logger.info("supervisor: child %s drained", child.spec.name)
                        continue
                    self._crashes.append(child.spec.name)
                    logger.warning(
                        "[Friction] child %s exited unexpectedly with code %s",
                        child.spec.name,
                        rc,
                    )
                    return
                if drain_task in done and not self._draining:
                    self._draining = True
                    self._relay_drain()
            logger.info("supervisor: every child drained; exiting")
        finally:
            for task in (*waits, shutdown_task, drain_task):
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    def _relay_drain(self) -> None:
        alive = [c for c in self._children if c.proc.returncode is None]
        logger.info(
            "supervisor: draining — relaying SIGUSR1 to %d child(ren); waiting "
            "for in-flight runs to finish (SIGTERM stops them now instead)",
            len(alive),
        )
        for child in alive:
            with contextlib.suppress(ProcessLookupError):
                child.proc.send_signal(signal.SIGUSR1)

    async def _terminate_remaining(self) -> None:
        alive = [c for c in self._children if c.proc.returncode is None]
        for child in alive:
            with contextlib.suppress(ProcessLookupError):
                child.proc.terminate()

        if not alive:
            return

        try:
            await asyncio.wait_for(
                asyncio.gather(
                    *(c.proc.wait() for c in alive),
                    return_exceptions=True,
                ),
                timeout=self._shutdown_grace_seconds,
            )
        except TimeoutError:
            still_alive = [c for c in alive if c.proc.returncode is None]
            for child in still_alive:
                logger.warning(
                    "supervisor: SIGKILLing %s (did not exit within %ss)",
                    child.spec.name,
                    self._shutdown_grace_seconds,
                )
                with contextlib.suppress(ProcessLookupError):
                    child.proc.kill()
            await asyncio.gather(
                *(c.proc.wait() for c in still_alive),
                return_exceptions=True,
            )

    def _failed(self) -> bool:
        if self._crashes:
            return True
        for child in self._children:
            rc = child.proc.returncode
            # Clean exits: 0, or killed by SIGTERM (-15). SIGKILL (-9) is a fail.
            if rc not in (0, -signal.SIGTERM):
                return True
        return False

    def _install_signal_handlers(self, loop: asyncio.AbstractEventLoop) -> None:
        for sig in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, self._on_signal)
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(signal.SIGUSR1, self._on_drain_signal)

    def _uninstall_signal_handlers(self, loop: asyncio.AbstractEventLoop) -> None:
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGUSR1):
            with contextlib.suppress(NotImplementedError):
                loop.remove_signal_handler(sig)

    def _on_signal(self) -> None:
        if self._shutdown_event is not None:
            self._shutdown_event.set()

    def _on_drain_signal(self) -> None:
        if self._drain_event is None:
            return
        if self._draining:
            self._relay_drain()  # again: a child may have missed the first
        self._drain_event.set()


def default_categories() -> list[CategorySpec]:
    """Categories the supervisor spawns by default in ``lithos-loom run``.

    The route-runner category runs when routes are configured. The
    obsidian-sync category runs when ``[obsidian_sync]`` is present in
    the loaded config — operators deploy the section on the vault host
    and omit it on headless hosts. The github-watcher category runs
    when ``[github_watcher]`` is present AND ``enabled = true`` — the
    presence-plus-flag combo lets an operator park the section in
    their config while turned off, mirroring the watch toggle on the
    project doc itself.
    """
    return [
        CategorySpec(
            name="route-runner",
            module="lithos_loom.children.route_runner",
            enabled=lambda cfg: bool(cfg.routes),
        ),
        CategorySpec(
            name="obsidian-sync",
            module="lithos_loom.children.obsidian_sync",
            enabled=lambda cfg: cfg.obsidian_sync is not None,
        ),
        CategorySpec(
            name="github-watcher",
            module="lithos_loom.children.github_watcher",
            enabled=lambda cfg: (
                cfg.github_watcher is not None and cfg.github_watcher.enabled
            ),
        ),
    ]
