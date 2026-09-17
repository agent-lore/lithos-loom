"""``lithos-loom drain`` — ask the running daemon to finish and exit (#407 slice 3).

The daemon is found through the supervisor's pidfile under the configured
work dir (:mod:`lithos_loom.runner.pidfile`). The identity it names is
verified to be the process still running (a reused pid is never signalled),
SIGUSR1 is sent, and the command waits for the daemon to exit: the
supervisor relays the signal, each child stops admitting new dispatch,
finishes what is in flight and exits 0, and the supervisor exits when the
last one has. So the operator's restart is safe by construction and costs
nothing — no run is killed, no remediation round refunded.

Exit codes: ``0`` the daemon exited; ``1`` there was no daemon to signal (no
pidfile, a stale one, or the signal could not be sent); ``2`` the daemon was
still draining when ``--timeout`` ran out — it keeps draining, and stopping
it now is the operator's call (SIGTERM, which refunds a killed round).
"""

from __future__ import annotations

import os
import signal
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from lithos_loom.runner.orphans import ProcessIdentity
from lithos_loom.runner.pidfile import daemon_alive, read_pidfile

__all__ = ["DEFAULT_POLL_SECONDS", "DrainOutcome", "drain_daemon"]

DEFAULT_POLL_SECONDS = 0.5


@dataclass(frozen=True)
class DrainOutcome:
    """What ``drain`` found and did: the exit code and the line to print."""

    code: int
    message: str


def drain_daemon(
    path: Path,
    *,
    timeout: float = 0.0,
    poll: float = DEFAULT_POLL_SECONDS,
    alive: Callable[[ProcessIdentity], bool] | None = None,
    kill: Callable[[int, int], None] | None = None,
    sleep: Callable[[float], None] | None = None,
    clock: Callable[[], float] | None = None,
) -> DrainOutcome:
    """Signal the daemon recorded at *path* to drain and wait for it to exit.

    ``timeout`` ``0`` waits without a deadline. The host seams (``alive``,
    ``kill``, ``sleep``, ``clock``) exist for tests and are resolved at call
    time — a default bound at import would send a real SIGUSR1 past a
    patched ``os.kill`` (which terminates an unprepared process).
    """
    alive = alive if alive is not None else daemon_alive
    kill = kill if kill is not None else os.kill
    sleep = sleep if sleep is not None else time.sleep
    clock = clock if clock is not None else time.monotonic
    identity = read_pidfile(path)
    if identity is None:
        return DrainOutcome(
            1,
            f"drain: no daemon pidfile at {path}; is `lithos-loom run` up with "
            "this config's work_dir?",
        )
    if not alive(identity):
        return DrainOutcome(
            1,
            f"drain: stale pidfile {path} — pid {identity.pid} is not the daemon "
            "that wrote it (it exited, or the host rebooted); nothing signalled",
        )
    try:
        kill(identity.pid, signal.SIGUSR1)
    except ProcessLookupError:
        return DrainOutcome(
            1, f"drain: daemon pid {identity.pid} exited before it could be signalled"
        )
    except PermissionError:
        return DrainOutcome(
            1,
            f"drain: no permission to signal daemon pid {identity.pid} (is it "
            "running as another user?)",
        )
    except OSError as exc:
        return DrainOutcome(
            1, f"drain: could not signal daemon pid {identity.pid}: {exc}"
        )
    started = clock()
    while alive(identity):
        if timeout > 0 and clock() - started >= timeout:
            return DrainOutcome(
                2,
                f"drain: daemon pid {identity.pid} still draining after "
                f"{timeout:g}s — it keeps finishing its in-flight runs; SIGTERM "
                "it to stop now instead (a killed remediation round is refunded)",
            )
        sleep(poll)
    return DrainOutcome(
        0,
        f"drain: daemon pid {identity.pid} exited after {clock() - started:.1f}s; "
        "safe to restart",
    )
