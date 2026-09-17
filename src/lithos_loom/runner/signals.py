"""SIGTERM as an exception, so ``finally`` blocks run (#407 slice 2a).

The daemon stops a dispatched ``develop converge`` / ``merge-gate`` / a
story-develop plugin run with SIGTERM. Python's default disposition for it
ends the process at once — no ``finally``, so the ``containers.stop_container``
teardown never runs and the run's ``--rm`` containers idle on (the orphaned
triage container of lens #88 r2). Turning the signal into ``SystemExit(143)``
lets every teardown block run on the way out; the exit code is the
conventional 128 + 15.

The lifetime bind (#407 slice 2b re-reviews, then f6537c52): a child loom
spawned dies with loom. The child is bound to the **spawner**, not to
whatever process happens to be its immediate parent — the route command is
``uv run python -m …`` and ``uv run`` spawns python rather than exec'ing it,
so the plugin's parent is ``uv``; a bind that asked "is my parent the
spawner?" exited every story-develop run at startup (loom task f6537c52).
"""

from __future__ import annotations

import contextlib
import ctypes
import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from types import FrameType

from lithos_loom.runner.orphans import pid_alive, start_ticks

#: Set on a child loom spawns, and ONLY then: a hand-run CLI in a terminal (or
#: a `setsid nohup … &`) must never die with its shell. The child reads it and
#: binds its lifetime to its spawner.
BOUND_ENV = "LITHOS_LOOM_BOUND_TO_PARENT"
#: The spawner's own pid, passed beside :data:`BOUND_ENV`: the bind checks
#: that this pid is an ANCESTOR of the child — not its parent, because the
#: configured command may put an intermediary (``uv run``, ``sh -c``, a
#: wrapper) between them. A spawner that died before the bind is no longer
#: an ancestor: the child was reparented (to pid 1, or under a child
#: subreaper to something else), so "is my parent pid 1?" is not the test.
PARENT_PID_ENV = "LITHOS_LOOM_PARENT_PID"
#: The spawner's start marker (``orphans.start_ticks`` of its own pid), so the
#: watch follows THAT process and a reused pid never reads as alive. Empty
#: when the spawner could not read its own marker; the child then captures
#: one at bind time, or falls back to pid liveness.
PARENT_START_ENV = "LITHOS_LOOM_PARENT_START"
#: Linux prctl option: deliver a signal to this process when its parent dies.
PR_SET_PDEATHSIG = 1
#: How often the portable spawner watch looks (seconds).
PARENT_WATCH_INTERVAL = 1.0
#: How many hops the ancestor walk follows before giving up. A loom child sits
#: one or two below its spawner; a chain this deep is not ours.
MAX_ANCESTOR_HOPS = 64

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


def bound_child_env(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment a loom spawner hands a child it wants bound to it:
    *base* (this process's environment when omitted) plus the marker, the
    spawner's pid and its start marker. Both spawn sites use this, so they
    cannot drift from what :func:`bind_lifetime_to_parent` reads."""
    me = os.getpid()
    start = start_ticks(me)
    return {
        **(os.environ if base is None else base),
        BOUND_ENV: "1",
        PARENT_PID_ENV: str(me),
        PARENT_START_ENV: "" if start is None else str(start),
    }


def set_pdeathsig(option: int, arg: int) -> None:
    """``prctl(option, arg)`` via libc; raises ``OSError`` when it fails or
    ``AttributeError`` where libc has no prctl (not Linux)."""
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(option, arg, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "prctl failed")


def _ps_ppid(pid: int) -> int | None:
    """Anywhere with ``ps``: the parent pid of *pid*."""
    try:
        proc = subprocess.run(
            ["ps", "-o", "ppid=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    text = proc.stdout.strip()
    if proc.returncode != 0 or not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def parent_of(pid: int) -> int | None:
    """The parent pid of *pid* — ``/proc/<pid>/stat`` field 4 on Linux,
    ``ps -o ppid=`` elsewhere — or ``None`` when there is no such process
    (or no way to ask)."""
    if pid <= 0:
        return None
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return _ps_ppid(pid)
    # the comm field is parenthesised and may itself hold spaces or parens:
    # everything after the LAST ")" is the numbered fields from 3 on
    tail = stat.rsplit(")", 1)[-1].split()
    try:
        return int(tail[1])  # field 4 (1-based) → index 1 past state (3)
    except (IndexError, ValueError):
        return None


def spawner_is_ancestor(expected: int) -> bool:
    """Whether *expected* is this process's parent, grandparent, … — walking
    up from :func:`os.getppid` through :func:`parent_of`, at most
    :data:`MAX_ANCESTOR_HOPS` hops. Pid 1 and below are never a spawner: a
    child whose chain reaches init without meeting *expected* was reparented
    because *expected* died."""
    if expected <= 1:
        return False
    pid = os.getppid()
    for _ in range(MAX_ANCESTOR_HOPS):
        if pid == expected:
            return True
        if pid <= 1:
            return False
        parent = parent_of(pid)
        if parent is None:
            return False
        pid = parent
    return False


def spawner_alive(pid: int, start: int | None) -> bool | None:
    """Whether the spawner — *pid* as the incarnation that had start marker
    *start* — is alive, dead, or currently unverifiable (``None``: a
    transient probe failure must not end a run).

    With a marker, a live *pid* whose marker differs is a REUSED pid — the
    spawner is dead. Without one, pid liveness is the portable floor."""
    now = start_ticks(pid)
    if now is not None:
        return start is None or now == start
    return pid_alive(pid)


def watch_parent(
    alive: Callable[[], bool | None],
    *,
    interval: float = PARENT_WATCH_INTERVAL,
    stop: threading.Event | None = None,
) -> None:
    """Poll *alive*; the moment it answers ``False``, SIGTERM ourselves (the
    handler runs the teardown). ``None`` (unverifiable) keeps polling. The
    portable half of the bind — it needs no prctl, so it holds on macOS too
    — and the half that sees THROUGH an intermediary: ``PR_SET_PDEATHSIG``
    fires on the immediate parent's death only, and with ``uv`` between loom
    and the plugin that parent outlives a SIGKILLed loom. Returns when *stop*
    is set (tests) or after firing."""
    while stop is None or not stop.is_set():
        if alive() is False:
            os.kill(os.getpid(), signal.SIGTERM)
            return
        if stop is not None and stop.wait(interval):
            return
        if stop is None:
            time.sleep(interval)


def start_parent_watch(alive: Callable[[], bool | None]) -> None:
    threading.Thread(
        target=watch_parent, args=(alive,), name="loom-parent-watch", daemon=True
    ).start()


def _int_env(name: str) -> int | None:
    try:
        return int(os.environ.get(name, ""))
    except ValueError:
        return None


def bind_lifetime_to_parent() -> None:
    """Die with the loom that spawned us (#407 slice 2b re-reviews; f6537c52).

    The supervisor kills a child by pid, not by process group, and a SIGKILL
    of the watcher leaves its converge child running — the next boot would
    then refund the round and launch a second agent on the same branch.
    When :data:`BOUND_ENV` says loom spawned this process:

    * exit at once unless the pid the spawner passed (:data:`PARENT_PID_ENV`)
      is an **ancestor** of ours — it may be our parent (converge, merge-gate:
      ``sys.executable`` spawned directly) or further up (a route plugin
      behind ``uv run``). A spawner that is not an ancestor died in the
      spawn-to-bind window and we were reparented, wherever to;
    * ask the kernel for SIGTERM on our immediate parent's death where it can
      (``PR_SET_PDEATHSIG``, Linux — immediate). Right whoever that parent
      is: an intermediary that dies has taken our pipes and our exit code
      with it;
    * and always start the portable spawner watch on the spawner's
      **identity** (pid + start marker — :data:`PARENT_START_ENV`, or
      captured now while the spawner is provably alive), so the guarantee
      holds where prctl does not exist, holds through an intermediary, and
      a reused pid never reads as alive. No-op without the marker.
    """
    if os.environ.get(BOUND_ENV) != "1":
        return
    expected = _int_env(PARENT_PID_ENV)
    if expected is None or not spawner_is_ancestor(expected):
        raise SystemExit(SIGTERM_EXIT)
    start = _int_env(PARENT_START_ENV)
    if start is None:
        start = start_ticks(expected)
    # not Linux, or no prctl: the watch below is the guarantee
    with contextlib.suppress(OSError, AttributeError):
        set_pdeathsig(PR_SET_PDEATHSIG, signal.SIGTERM)
    start_parent_watch(lambda: spawner_alive(expected, start))
