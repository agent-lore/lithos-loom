"""Reap the run containers a previous daemon left behind (#407 slice 2a).

A loom restart kills every dispatched run's process but not its ``--rm``
sandbox containers — docker keeps those until they are stopped, and nothing
stops them (the idle triage container of lens #88 r2). Every run container
is started with a ``loom.pid=<owner pid>`` label (``plugins.story_develop.
containers.build_run_command``); at boot the daemon removes the ones whose
owner is gone and leaves the operator's own hand-run CLIs — alive processes —
alone.
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "PID_LABEL",
    "ProcessIdentity",
    "host_boot_id",
    "start_ticks",
    "identity_alive",
    "pid_alive",
    "process_identity",
    "reap_orphaned_containers",
]

logger = logging.getLogger(__name__)

#: The docker label naming a run container's owner process.
PID_LABEL = "loom.pid"

#: One docker call must never hang the boot on a dead daemon.
_DOCKER_TIMEOUT_S = 30


@dataclass(frozen=True)
class ProcessIdentity:
    """A process, not a number (#407 slice 2b re-review): a pid is reused —
    after a host reboot quite plausibly by the new watcher itself — so
    "pid alive" alone reads a genuinely lost run as alive forever. The
    kernel's start time (clock ticks since boot, ``/proc/<pid>/stat`` field
    22) and the host's boot id pin the number to one incarnation."""

    pid: int
    start_ticks: int
    host_boot: str


#: Whether this host has a procfs to read process facts from — decided once,
#: because it does not change while the process runs. It selects the ONE
#: provider each fact is read through (PR #417 review, Medium): ``/proc``
#: reports a start time in clock ticks since boot and ``ps -o lstart=`` in
#: epoch seconds, so a marker stamped by one provider and re-read through the
#: other is a different number for the SAME live process — which reads as a
#: reused pid, i.e. positive death, and would SIGTERM a bound run or refund a
#: live remediation. A provider is therefore chosen by platform, never by
#: whether a read happened to succeed; a failed read is *unknown*, not the
#: other provider's unit.
_PROCFS = Path("/proc/self/stat").exists()


def _boot_id_file() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return ""


def _sysctl_boottime() -> str:
    """macOS / BSD: ``sysctl -n kern.boottime`` — a string that changes on
    every boot, which is all the identity needs."""
    try:
        proc = subprocess.run(
            ["sysctl", "-n", "kern.boottime"], capture_output=True, text=True, timeout=5
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def host_boot_id() -> str:
    """Something that changes on every host boot: the kernel's boot id on a
    procfs host, ``kern.boottime`` (macOS / BSD) elsewhere, ``""`` when the
    host's provider cannot answer. One provider per host — see
    :data:`_PROCFS`."""
    return _boot_id_file() if _PROCFS else _sysctl_boottime()


def _proc_start_ticks(pid: int) -> int | None:
    """Linux: the process's start time in clock ticks since boot."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    # the comm field is parenthesised and may itself hold spaces or parens:
    # everything after the LAST ")" is the numbered fields from 3 on
    tail = stat.rsplit(")", 1)[-1].split()
    try:
        return int(tail[19])  # field 22 (1-based) → index 19 past state (3)
    except (IndexError, ValueError):
        return None


def _ps_start_epoch(pid: int) -> int | None:
    """Anywhere with ``ps``: the process's start time as epoch seconds
    (``lstart`` is a full timestamp; ``start`` truncates to the day)."""
    try:
        proc = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    text = proc.stdout.strip()
    if proc.returncode != 0 or not text:
        return None
    for fmt in ("%a %b %d %H:%M:%S %Y", "%a %d %b %H:%M:%S %Y"):
        try:
            return int(time.mktime(time.strptime(text, fmt)))
        except ValueError:
            continue
    return None


def start_ticks(pid: int) -> int | None:
    """A start-time marker for the process — kernel ticks on a procfs host,
    epoch seconds via ``ps`` elsewhere — or ``None`` when there is no such
    process or the host's provider cannot answer. The two units are never
    mixed on one host: a marker is only ever compared against a re-read
    through the same provider (see :data:`_PROCFS`)."""
    if pid <= 0:
        return None
    return _proc_start_ticks(pid) if _PROCFS else _ps_start_epoch(pid)


def process_identity(pid: int) -> ProcessIdentity | None:
    """The stable identity of a live process, or ``None`` when either half
    cannot be captured. A start marker without a boot marker is not durable
    across a reboot and therefore must not authorize a crash-safe dispatch."""
    start = start_ticks(pid)
    if start is None:
        return None
    boot = host_boot_id()
    if not boot:
        return None
    return ProcessIdentity(pid=pid, start_ticks=start, host_boot=boot)


def identity_alive(identity: ProcessIdentity) -> bool | None:
    """Whether THAT process is alive, dead, or currently unverifiable.

    ``None`` is deliberately distinct from death: a transient ``/proc``,
    ``ps``, or ``sysctl`` failure must hold a push-capable run rather than
    permit a second one beside it.
    """
    if identity.pid <= 0 or identity.start_ticks <= 0 or not identity.host_boot:
        return None
    boot = host_boot_id()
    if not boot:
        return None
    if identity.host_boot != boot:
        return False
    start = start_ticks(identity.pid)
    if start is not None:
        return start == identity.start_ticks
    # A missing start marker can mean either "gone" or "the probe failed".
    # Only the kernel positively denying the pid is evidence of death.
    return False if pid_alive(identity.pid) is False else None


def pid_alive(pid: int) -> bool | None:
    """``None`` when the label is not a pid the kernel can be asked about (an
    all-digit label too large for a C long raises ``OverflowError`` — PR #415
    review: the reaper runs before the boot gate, so one stale label must
    never keep loom from starting)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    except (OverflowError, ValueError, OSError):
        return None
    return True


def reap_orphaned_containers() -> list[str]:
    """#407: remove every loom-labelled container whose owner process is gone.

    Called once at daemon boot, before any child could start a run beside
    them. A container owned by a live process — the operator's own hand-run
    `develop converge`, a plugin still finishing — is left alone. Never
    raises; returns the names removed.
    """
    try:
        listing = subprocess.run(
            [
                "docker",
                "ps",
                "-a",  # a container stuck in `created` holds its name too
                "--filter",
                f"label={PID_LABEL}",
                "--format",
                f'{{{{.Names}}}} {{{{.Label "{PID_LABEL}"}}}}',
            ],
            capture_output=True,
            text=True,
            timeout=_DOCKER_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("orphan-container reap could not list containers: %s", exc)
        return []
    if listing.returncode != 0:
        logger.warning(
            "orphan-container reap: docker ps failed (rc %d): %s",
            listing.returncode,
            listing.stderr.strip()[:200],
        )
        return []
    reaped: list[str] = []
    for line in listing.stdout.splitlines():
        parts = line.split()
        if len(parts) != 2 or not parts[1].isdigit():
            continue
        name = parts[0]
        # A docker label value is an arbitrary string: an all-digit one past
        # Python's int-conversion limit (4300 digits by default) raises here,
        # before any kernel probe (PR #415 re-review) — contained the same way.
        try:
            pid = int(parts[1])
        except ValueError:
            pid = None
        alive = pid_alive(pid) if pid is not None else None
        if alive is None:
            logger.warning(
                "orphan-container reap: %s carries an unusable owner label %s; skipped",
                name,
                parts[1],
            )
            continue
        if alive:
            continue
        try:
            rm = subprocess.run(
                ["docker", "rm", "-f", name],
                capture_output=True,
                text=True,
                timeout=_DOCKER_TIMEOUT_S,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.warning("orphan-container reap of %s failed: %s", name, exc)
            continue
        if rm.returncode == 0:
            logger.warning(
                "orphan-container reap: removed %s (owner pid %d is gone — a loom "
                "restart under its run)",
                name,
                pid,
            )
            reaped.append(name)
        else:
            logger.warning(
                "orphan-container reap of %s failed (rc %d): %s",
                name,
                rm.returncode,
                rm.stderr.strip()[:200],
            )
    return reaped
