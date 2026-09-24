"""One cancellation-safe subprocess spawn for the watcher's dispatchers.

Both autonomous dispatchers — external remediation (``develop converge
--from-github``, PRD S2) and the base-move re-gate (``develop merge-gate``,
PRD S3) — run a loom CLI as a crash-isolated child and read its ``--json``
record back. The spawn is the same in both: a wall-clock cap so a hung
container can never hold a single-flight slot forever, and a cancellation
(watcher shutdown) that ends the child before re-raising, so a stopped loom
never leaves an orphan that keeps pushing.

Reading the child's output back is shared too: :func:`message_tail` picks the
one line a finding should carry when the child died without writing its
record (#431).
"""

from __future__ import annotations

import asyncio

from lithos_loom.runner.signals import bound_child_env

__all__ = ["message_tail", "spawn_command"]


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
        # #407: the child binds its lifetime to this process, so a SIGKILLed
        # watcher cannot leave a converge running that the next boot would
        # then dispatch beside
        env=bound_child_env(),
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


# Rich renders an uncaught exception as a box-drawn panel of source frames, so
# the LAST 600 chars of a crashed child's output are usually frame decoration
# (#431: a story's `[Friction]` carried `│   103 │   raise RuntimeError(...)`
# instead of "SSH fetch failed"). A finding gets the last line that is a
# MESSAGE — the exception line, git's own `fatal:` — and the full tail stays
# in the daemon log at WARNING.
_FRAME_MARKERS = ("│", "╭", "╰", "─", "┃", "┌", "└", "├", "|", "+-")
_FRAME_OPENERS = ('File "', "Traceback (most recent call last)")


def message_tail(output: str, *, limit: int = 300) -> str:
    """The last non-traceback line of *output*, for an operator-facing finding.

    Falls back to the last non-blank line when every line looks like a frame
    (a panel truncated mid-render), and to ``"(no output)"`` for no output at
    all — a finding never loses the only evidence there was.
    """
    lines = [line.rstrip() for line in output.splitlines() if line.strip()]
    for line in reversed(lines):
        if line.startswith((" ", "\t")):
            continue  # a frame's source / caret line
        stripped = line.strip()
        if stripped.startswith(_FRAME_MARKERS) or stripped.startswith(_FRAME_OPENERS):
            continue
        return stripped[-limit:]
    return lines[-1].strip()[-limit:] if lines else "(no output)"
