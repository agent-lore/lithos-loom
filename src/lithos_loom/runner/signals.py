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

import signal
from types import FrameType

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
