"""The supervisor's pidfile — how ``lithos-loom drain`` finds the daemon.

``lithos-loom run`` writes it under the work dir after the boot gate and
removes it on exit. It records a process **identity** (pid + start time +
host boot id — :class:`~lithos_loom.runner.orphans.ProcessIdentity`, the
#407 slice 2b notion), never a bare pid: a pid is reused, and a drain must
never signal whatever now wears the number. When the identity cannot be
captured the file still names the pid (with zero markers) — a drain then
falls back to the kernel's own liveness check, the best available.
"""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path

from lithos_loom.runner import orphans
from lithos_loom.runner.orphans import ProcessIdentity

__all__ = [
    "CLAIM_ATTEMPTS",
    "PIDFILE_NAME",
    "claim_pidfile",
    "daemon_alive",
    "pidfile_path",
    "read_pidfile",
    "remove_pidfile",
    "write_pidfile",
]

PIDFILE_NAME = "supervisor.pid"
CLAIM_ATTEMPTS = 3


def pidfile_path(work_dir: Path) -> Path:
    """Where the daemon for *work_dir* records itself."""
    return work_dir / PIDFILE_NAME


def _me() -> ProcessIdentity:
    return orphans.process_identity(os.getpid()) or ProcessIdentity(
        pid=os.getpid(), start_ticks=0, host_boot=""
    )


def _write_temp(path: Path, me: ProcessIdentity) -> Path:
    """Write *me* to a sibling temp file (complete before it is visible)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    payload = {"pid": me.pid, "start_ticks": me.start_ticks, "host_boot": me.host_boot}
    temp.write_text(json.dumps(payload), encoding="utf-8")
    return temp


def write_pidfile(path: Path) -> ProcessIdentity:
    """Record this process at *path* unconditionally (atomic replace) and
    return what was written, for :func:`remove_pidfile` to match later.
    The daemon uses :func:`claim_pidfile`; this is the unguarded form."""
    me = _me()
    temp = _write_temp(path, me)
    os.replace(temp, path)
    return me


def claim_pidfile(path: Path) -> ProcessIdentity | None:
    """Record this process at *path* unless a live daemon already has.

    The exclusive hard link is the arbiter — two boots racing the same
    (absent or stale) file admit exactly one: the loser's link fails, it
    re-reads, finds a live holder and returns ``None``. A stale or malformed
    file is removed and the claim retried (bounded). Raises ``OSError`` when
    the file cannot be written at all (the caller decides what a missing
    pidfile costs).
    """
    me = _me()
    for _ in range(CLAIM_ATTEMPTS):
        temp = _write_temp(path, me)
        try:
            os.link(temp, path)
        except FileExistsError:
            holder = read_pidfile(path)
            if holder is not None and daemon_alive(holder):
                return None
            with contextlib.suppress(FileNotFoundError):
                path.unlink()  # stale or torn: nobody's
            continue
        else:
            return me
        finally:
            with contextlib.suppress(FileNotFoundError):
                temp.unlink()
    holder = read_pidfile(path)
    return None if holder is not None and daemon_alive(holder) else me


def read_pidfile(path: Path) -> ProcessIdentity | None:
    """The identity recorded at *path*, or ``None`` when there is no
    well-formed file there (missing, unreadable, not the expected shape)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    pid, start, boot = data.get("pid"), data.get("start_ticks"), data.get("host_boot")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return None
    if isinstance(start, bool) or not isinstance(start, int) or start < 0:
        return None
    if not isinstance(boot, str):
        return None
    return ProcessIdentity(pid=pid, start_ticks=start, host_boot=boot)


def remove_pidfile(path: Path, mine: ProcessIdentity) -> None:
    """Remove *path* if it still records *mine* — a daemon that booted over a
    stale file owns it now, and a missing file is nothing to remove."""
    if read_pidfile(path) != mine:
        return
    with contextlib.suppress(FileNotFoundError):
        path.unlink()


def daemon_alive(identity: ProcessIdentity) -> bool:
    """Whether the daemon the pidfile names is still running.

    A verifiable identity answers for itself (a reused pid, or one from
    another boot, is dead). An unverifiable one falls back to the kernel's
    pid check; a pid the kernel cannot even represent is not alive.
    """
    verdict = orphans.identity_alive(identity)
    if verdict is not None:
        return verdict
    return orphans.pid_alive(identity.pid) is True
