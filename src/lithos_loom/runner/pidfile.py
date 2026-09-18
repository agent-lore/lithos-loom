"""The supervisor's pidfile — how ``lithos-loom drain`` finds the daemon.

``lithos-loom run`` claims it first thing and holds it for its lifetime.
**Ownership is an exclusive ``flock`` on the file's inode**: taken
non-blocking at boot, released by the kernel when the process ends — however
it ends — and never by an unlink, because nothing unlinks the file. That is
what makes the claim race-free (PR #418 review): a stale file is simply one
nobody holds the lock on, so there is no read-then-evict step for two
concurrent boots to interleave — the second claimant's ``flock`` fails and
it stands down. Liveness is therefore the lock, not the pid.

The content records a process **identity** (pid + start time + host boot id
— :class:`~lithos_loom.runner.orphans.ProcessIdentity`, the #407 slice 2b
notion), never a bare pid: a pid is reused, and a drain must never signal
whatever now wears the number. When the identity cannot be captured the
file still names the pid (with zero markers); the lock still decides
liveness, and only where the lock itself is unknowable does a drain fall
back to the identity, then to the kernel's pid check.

The content is written in place under the lock (the inode must stay the
one locked), so a reader in the microseconds of that write sees an empty
file and reports "no daemon pidfile" — a `drain` that races the boot by that
much simply re-runs.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from lithos_loom.runner import orphans
from lithos_loom.runner.orphans import ProcessIdentity

__all__ = [
    "PIDFILE_NAME",
    "PidfileClaim",
    "claim_pidfile",
    "daemon_alive",
    "holder_alive",
    "pidfile_path",
    "read_pidfile",
]

PIDFILE_NAME = "supervisor.pid"


def pidfile_path(work_dir: Path) -> Path:
    """Where the daemon for *work_dir* records itself."""
    return work_dir / PIDFILE_NAME


@dataclass
class PidfileClaim:
    """This process's ownership of the pidfile: the lock, held until
    :meth:`release` (or the process ends)."""

    path: Path
    identity: ProcessIdentity
    _fd: int | None = field(default=None, repr=False)

    def release(self) -> None:
        """Let the lock go; the file stays (now stale). Idempotent."""
        if self._fd is not None:
            os.close(self._fd)  # closing the description releases the flock
            self._fd = None


def _me() -> ProcessIdentity:
    return orphans.process_identity(os.getpid()) or ProcessIdentity(
        pid=os.getpid(), start_ticks=0, host_boot=""
    )


def claim_pidfile(path: Path) -> PidfileClaim | None:
    """Take the pidfile for this process, or ``None`` if a live daemon holds it.

    Opens (creating) *path*, takes an exclusive non-blocking ``flock`` on it
    — the one arbiter between concurrent boots — and, holding it, rewrites
    the content with this process's identity. Raises ``OSError`` when the
    file cannot be opened or locked at all (no directory, no permission, a
    filesystem without ``flock``): the caller fails closed, since without
    the lock one-daemon-per-work-dir cannot be proven.
    """
    me = _me()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return None
        payload = {
            "pid": me.pid,
            "start_ticks": me.start_ticks,
            "host_boot": me.host_boot,
        }
        data = json.dumps(payload).encode("utf-8")
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        written = 0
        while written < len(data):  # a regular-file write may be short
            n = os.write(fd, data[written:])
            if n <= 0:
                raise OSError(errno.EIO, f"pidfile write made no progress at {path}")
            written += n
        os.fsync(fd)
    except OSError:
        os.close(fd)
        raise
    return PidfileClaim(path=path, identity=me, _fd=fd)


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


def holder_alive(path: Path) -> bool | None:
    """Whether some process holds the pidfile's lock: ``True`` (a daemon is
    up), ``False`` (no file, or nobody holds it), ``None`` (the lock is
    unknowable here — the probe itself failed)."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except FileNotFoundError:
        return False
    except OSError:
        return None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        except OSError:
            return None
        fcntl.flock(fd, fcntl.LOCK_UN)  # we took a free lock: nobody's
        return False
    finally:
        os.close(fd)


def daemon_alive(path: Path, identity: ProcessIdentity) -> bool:
    """Whether THE daemon *identity* names still runs and still owns the file.

    The lock alone proves that *some* process holds the pathname, not that
    it is this one (PR #418 re-review): a successor that claimed within a
    poll would keep a drain waiting forever, and in the claim handoff — the
    lock taken, the content not yet rewritten — the file still names the
    predecessor. So: a positively dead identity is dead whatever holds the
    lock; the content must still name the identity (a successor's, or
    nobody's, means this daemon is gone); only then does the lock answer,
    and only where the lock is unknowable do the identity and the kernel's
    pid check stand in (a pid the kernel cannot represent is not alive).
    """
    verdict = orphans.identity_alive(identity)
    if verdict is False:
        return False
    if read_pidfile(path) != identity:
        return False
    held = holder_alive(path)
    if held is not None:
        return held
    if verdict is not None:
        return verdict
    return orphans.pid_alive(identity.pid) is True
