"""Run a single agent turn (coder or reviewer) and parse its structured result.

A turn is ``docker exec ...`` into a warm container. Completion, error, and cost
all come from the parsed structured output + the process exit code — no terminal
scraping (ADR 0002). The per-tool CLI argv + result parsing now live on the
:class:`~lithos_loom.plugins.story_develop.engines.Engine` adapter; this module
is the tool-agnostic turn driver over it.

:class:`TurnResult` and the ``parse_*`` functions are re-exported / kept as
one-line delegates here so existing importers keep working while callers migrate
to :meth:`Engine.parse_turn` directly (ARCH-2.E2).
"""

from __future__ import annotations

import subprocess

from . import containers, engines
from .engines import _TIMEOUT_EXIT, Engine, TurnResult

__all__ = ["TurnResult", "parse_claude_result", "parse_codex_result", "run_turn"]


def parse_claude_result(stdout: str, *, exit_code: int, stderr: str) -> TurnResult:
    """Delegate to :meth:`ClaudeEngine.parse_turn` (kept until callers migrate, E2)."""
    return engines.get_engine("claude").parse_turn(
        stdout, exit_code=exit_code, stderr=stderr
    )


def parse_codex_result(
    stdout: str,
    *,
    exit_code: int,
    stderr: str,
    session_id: str = "",
    resume: bool = False,
) -> TurnResult:
    """Delegate to :meth:`CodexEngine.parse_turn` (kept until callers migrate, E2)."""
    return engines.get_engine("codex").parse_turn(
        stdout, exit_code=exit_code, stderr=stderr, session_id=session_id, resume=resume
    )


def run_turn(
    *,
    container: str,
    prompt: str,
    engine: Engine,
    session_id: str,
    resume: bool = False,
    timeout: int = 3600,
    model: str | None = None,
    effort: str | None = None,
) -> TurnResult:
    """Execute one agent turn in *container* via *engine* and return its result.

    *engine* builds the ``docker exec`` argv and parses the turn's structured
    output — the tool-specific mechanics (claude JSON vs codex JSONL, session
    handling) live behind that one interface, so this driver is tool-agnostic.
    *model* / *effort* (#93), when set, pin the agent model + reasoning effort
    for the turn; ``None`` leaves the agent default.
    """
    exec_cmd = engine.build_exec_argv(
        name=container,
        prompt=prompt,
        session_id=session_id,
        resume=resume,
        model=model,
        effort=effort,
    )
    try:
        proc = containers.exec_turn(exec_cmd, timeout=timeout)
    except subprocess.TimeoutExpired:
        return TurnResult(
            exit_code=_TIMEOUT_EXIT,
            succeeded=False,
            session_id=session_id,
            result_text="",
            cost_usd=0.0,
            raw=None,
            stderr=f"agent turn timed out after {timeout}s",
        )
    return engine.parse_turn(
        proc.stdout,
        exit_code=proc.returncode,
        stderr=proc.stderr,
        session_id=session_id,
        resume=resume,
    )
