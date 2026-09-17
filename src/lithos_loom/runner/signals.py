"""SIGTERM as an exception, so ``finally`` blocks run (#407 slice 2a).

The daemon stops a dispatched ``develop converge`` / ``merge-gate`` / a
story-develop plugin run with SIGTERM. Python's default disposition for it
ends the process at once — no ``finally``, so the ``containers.stop_container``
teardown never runs and the run's ``--rm`` containers idle on (the orphaned
triage container of lens #88 r2). Turning the signal into ``SystemExit(143)``
lets every teardown block run on the way out; the exit code is the
conventional 128 + 15.
"""

from __future__ import annotations

import contextlib
import ctypes
import os
import signal
import threading
import time
from types import FrameType

#: Set on a child loom spawns, and ONLY then: a hand-run CLI in a terminal (or
#: a `setsid nohup … &`) must never die with its shell. The child reads it and
#: binds its lifetime to its parent.
BOUND_ENV = "LITHOS_LOOM_BOUND_TO_PARENT"
#: The spawner's own pid, passed beside :data:`BOUND_ENV`: the bind checks
#: the parent it actually has against this — a parent that died before the
#: bind is reparented (to pid 1, or under a child subreaper to something
#: else), so "is my parent pid 1?" is not the test.
PARENT_PID_ENV = "LITHOS_LOOM_PARENT_PID"
#: Linux prctl option: deliver a signal to this process when its parent dies.
PR_SET_PDEATHSIG = 1
#: How often the portable parent watch looks (seconds).
PARENT_WATCH_INTERVAL = 1.0

#: 128 + SIGTERM — what a shell reports for a process the signal ended.
SIGTERM_EXIT = 143


def _exit_on_sigterm(signum: int, frame: FrameType | None) -> None:
    # A second SIGTERM during teardown would raise from inside the `finally`
    # and abandon the remaining stop_container calls; make it a plain kill.
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    raise SystemExit(SIGTERM_EXIT)


def install_sigterm_exit() -> None:
    """Make SIGTERM raise ``SystemExit`` in the main thread (no-op where the
    interpreter cannot install handlers, e.g. a non-main thread)."""
    try:
        signal.signal(signal.SIGTERM, _exit_on_sigterm)
    except (ValueError, OSError):  # not the main thread / unsupported
        return


def set_pdeathsig(option: int, arg: int) -> None:
    """``prctl(option, arg)`` via libc; raises ``OSError`` when it fails or
    ``AttributeError`` where libc has no prctl (not Linux)."""
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(option, arg, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "prctl failed")


def watch_parent(
    expected: int,
    *,
    interval: float = PARENT_WATCH_INTERVAL,
    stop: threading.Event | None = None,
) -> None:
    """Poll our parent pid; the moment it is not *expected* any more, SIGTERM
    ourselves (the handler runs the teardown). The portable half of the
    bind — it needs no prctl, so it holds on macOS too — and the
    subreaper-proof half: it compares against the spawner's pid, not
    against 1. Returns when *stop* is set (tests) or after firing."""
    while stop is None or not stop.is_set():
        if os.getppid() != expected:
            os.kill(os.getpid(), signal.SIGTERM)
            return
        if stop is not None and stop.wait(interval):
            return
        if stop is None:
            time.sleep(interval)


def start_parent_watch(expected: int) -> None:
    threading.Thread(
        target=watch_parent, args=(expected,), name="loom-parent-watch", daemon=True
    ).start()


def bind_lifetime_to_parent() -> None:
    """Die with the loom that spawned us (#407 slice 2b re-reviews).

    The supervisor kills a child by pid, not by process group, and a SIGKILL
    of the watcher leaves its converge child running — the next boot would
    then refund the round and launch a second agent on the same branch.
    When :data:`BOUND_ENV` says loom spawned this process:

    * exit at once if our parent is not the pid the spawner passed
      (:data:`PARENT_PID_ENV`) — it died in the spawn-to-bind window and we
      were reparented, wherever to;
    * ask the kernel for SIGTERM on the parent's death where it can
      (``PR_SET_PDEATHSIG``, Linux — immediate);
    * and always start the portable parent watch, so the guarantee holds
      where prctl does not exist. No-op without the marker.
    """
    if os.environ.get(BOUND_ENV) != "1":
        return
    try:
        expected = int(os.environ.get(PARENT_PID_ENV, ""))
    except ValueError:
        expected = 0
    if not expected or os.getppid() != expected:
        raise SystemExit(SIGTERM_EXIT)
    # not Linux, or no prctl: the watch below is the guarantee
    with contextlib.suppress(OSError, AttributeError):
        set_pdeathsig(PR_SET_PDEATHSIG, signal.SIGTERM)
    start_parent_watch(expected)
