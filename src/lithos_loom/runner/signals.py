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

import ctypes
import os
import signal
from types import FrameType

#: Set on a child loom spawns, and ONLY then: a hand-run CLI in a terminal (or
#: a `setsid nohup … &`) must never die with its shell. The child reads it and
#: binds its lifetime to its parent.
BOUND_ENV = "LITHOS_LOOM_BOUND_TO_PARENT"
#: Linux prctl option: deliver a signal to this process when its parent dies.
PR_SET_PDEATHSIG = 1

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
    """``prctl(option, arg)`` via libc; raises ``OSError`` when it fails."""
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(option, arg, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "prctl failed")


def bind_lifetime_to_parent() -> None:
    """Die with the loom that spawned us (#407 slice 2b re-review).

    The supervisor kills a child by pid, not by process group, and a SIGKILL
    of the watcher leaves its converge child running — the next boot would
    then refund the round and launch a second agent on the same branch.
    When :data:`BOUND_ENV` says loom spawned this process, ask the kernel for
    SIGTERM on the parent's death (the SIGTERM handler runs the teardown),
    and exit at once if the parent already died in the spawn-to-bind window
    (PDEATHSIG would never fire for a death that already happened). No-op
    without the marker, or where prctl is unavailable.
    """
    if os.environ.get(BOUND_ENV) != "1":
        return
    try:
        set_pdeathsig(PR_SET_PDEATHSIG, signal.SIGTERM)
    except (OSError, AttributeError):
        return
    if os.getppid() == 1:
        raise SystemExit(SIGTERM_EXIT)
