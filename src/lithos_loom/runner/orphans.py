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

__all__ = ["PID_LABEL", "reap_orphaned_containers"]

logger = logging.getLogger(__name__)

#: The docker label naming a run container's owner process.
PID_LABEL = "loom.pid"

#: One docker call must never hang the boot on a dead daemon.
_DOCKER_TIMEOUT_S = 30


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
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
        name, pid = parts[0], int(parts[1])
        if _pid_alive(pid):
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
