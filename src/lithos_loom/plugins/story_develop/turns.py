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
from .engines import _TIMEOUT_EXIT, TurnResult

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
    session_id: str,
    resume: bool = False,
    timeout: int = 3600,
    tool: str = "claude",
    model: str | None = None,
    effort: str | None = None,
) -> TurnResult:
    """Execute one agent turn in *container* and return its parsed result.

    *tool* is threaded through to the exec builder, which raises for tools it
    cannot run — so an orchestration-level tool switch that the exec layer
    doesn't support yet fails loudly instead of silently running claude.
    *model* / *effort* (#93), when set, pin the agent model + reasoning effort
    for the turn; ``None`` leaves the agent default.
    """
    exec_cmd = containers.build_exec_command(
        name=container,
        tool=tool,
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
    if tool == "codex":
        return parse_codex_result(
            proc.stdout,
            exit_code=proc.returncode,
            stderr=proc.stderr,
            session_id=session_id,
            resume=resume,
        )
    return parse_claude_result(
        proc.stdout, exit_code=proc.returncode, stderr=proc.stderr
    )
