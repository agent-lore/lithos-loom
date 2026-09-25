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

from lithos_loom.plugins.story_develop.publish_text import (
    CONTROL_CHARS_RE,
    MAX_EXCERPT_CHARS,
    flatten_line,
    publish_line,
)
from lithos_loom.runner.signals import bound_child_env

__all__ = ["NO_MESSAGE", "message_tail", "spawn_command"]


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


# Rich renders an uncaught exception as a box-drawn panel of source frames and
# then WRAPS the exception line itself across several physical lines, so neither
# the last 600 chars of a crashed child's output nor its last physical line is
# the reason (#431: a story's `[Friction]` carried `│ 103 │ raise RuntimeError(`;
# its review f-003: the naive last-line pick carried `remote repository.`). What
# a finding wants is the TRAILING BLOCK of non-frame lines, rejoined into the one
# logical message — and, when the output ends inside a panel, nothing at all:
# walking back past loom's own traceback would attribute EARLIER output (the
# external review material converge echoes, review security f-002) as the
# failure. The full tail stays in the daemon log at WARNING either way.
_FRAME_MARKERS = (
    "│",
    "╭",
    "╰",
    "─",
    "┃",
    "┌",
    "└",
    "├",
    "|",
    "+-",
    'File "',
    "Traceback (most recent call last)",
)
NO_MESSAGE = "no message line — see the daemon log"


def _is_frame_line(line: str) -> bool:
    """Is *line* traceback / panel decoration rather than a message?

    Indentation is measured with :meth:`str.lstrip`, not ``startswith(" ")``:
    a leading NBSP or zero-width character would otherwise present a frame's
    source line as a message (review security f-002).
    """
    clean = CONTROL_CHARS_RE.sub("", line)
    if clean != clean.lstrip():
        return True  # a frame's source / caret line
    return clean.strip().startswith(_FRAME_MARKERS)


def message_tail(output: str, *, limit: int = MAX_EXCERPT_CHARS) -> str:
    """The crashed child's last logical MESSAGE, fit to publish in a finding.

    The trailing run of non-frame, non-blank lines, rejoined from the **last**
    line outwards while the result fits *limit*: a rich-wrapped
    ``RuntimeError: …`` comes back whole, identity first, while the ordinary
    case — pages of output and then the one line that says why — publishes
    that line instead of the long line above it (#431 review f-007). Bounded
    and stripped of anything that could render as something other than itself:
    the child's output carries text loom did not author and this lands on a
    screen the operator decides on (:func:`publish_line`). :data:`NO_MESSAGE`
    when the output holds no message line at all — a box frame is never the
    answer.
    """
    block: list[str] = []
    for line in reversed(output.splitlines()):
        if not line.strip():
            if block:
                break  # a blank line ends the message block
            continue
        if _is_frame_line(line):
            break  # a frame is a wall: never reach past it for a "message"
        block.insert(0, line.strip())
    if not block:
        return NO_MESSAGE
    kept = flatten_line(block[-1])
    for line in reversed(block[:-1]):
        wider = flatten_line(f"{line} {kept}")
        if len(wider) > limit:
            break  # earlier lines are a bonus; the message line is not
        kept = wider
    return publish_line(kept, limit=limit) or NO_MESSAGE
