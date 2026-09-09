"""One cancellation-safe subprocess spawn for the watcher's dispatchers.

Both autonomous dispatchers — external remediation (``develop converge
--from-github``, PRD S2) and the base-move re-gate (``develop merge-gate``,
PRD S3) — run a loom CLI as a crash-isolated child and read its ``--json``
record back. The spawn is the same in both: a wall-clock cap so a hung
container can never hold a single-flight slot forever, and a cancellation
(watcher shutdown) that ends the child before re-raising, so a stopped loom
never leaves an orphan that keeps pushing.
"""

from __future__ import annotations

import asyncio

__all__ = ["spawn_command"]


async def _end_process(proc: asyncio.subprocess.Process) -> None:
    """Terminate → grace → kill; tolerant of an already-exited child."""
    if proc.returncode is not None:
        return
    proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), timeout=10)
    except TimeoutError:
        proc.kill()
        await proc.wait()


async def spawn_command(
    cmd: list[str], *, timeout: float, label: str
) -> tuple[int, str]:
    """Run *cmd*, return ``(returncode, combined output)``.

    A run past *timeout* seconds is ended and reported as rc -1 with a
    one-line explanation naming *label*. A **cancellation** ends the child
    too before re-raising (PR #346 review F5).
    """
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        await _end_process(proc)
        return -1, f"{label} exceeded {timeout:g}s and was killed"
    except asyncio.CancelledError:
        await _end_process(proc)
        raise
    return proc.returncode if proc.returncode is not None else -1, out.decode(
        "utf-8", errors="replace"
    )
