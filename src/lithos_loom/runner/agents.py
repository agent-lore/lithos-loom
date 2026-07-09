"""Coding-agent subprocess runner (US-12).

Stub — lifted from Ralph++ and adapted to Loom's plugin work-dir layout.
Captures stream-json to ``{work_dir}/{task.id}/agent-output.jsonl`` and parses
for cost / turn count / tool-call summaries.

On timeout: SIGTERM, then SIGKILL after 5s grace.

FLAGGED FOR DELETION (ARCH-2.E5 → lithos-loom#232): ``run_claude`` / ``run_codex``
are unimplemented Ralph++-salvage stubs (both raise ``NotImplementedError``) that
nothing imports. The live coding-agent turn path is
:func:`lithos_loom.plugins.story_develop.turns.run_turn` over the
:class:`~lithos_loom.plugins.story_develop.engines.Engine` adapter; this module is
superseded. Any future host-side runner should be designed against ``Engine``,
not resurrected from here.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AgentResult:
    exit_code: int
    duration_seconds: float
    turns: int
    cost_usd: float
    output_path: Path
    interrupted: bool


def run_claude(
    prompt: str,
    cwd: Path,
    claude_config_dir: Path | None = None,
    output_format: str = "stream-json",
    timeout: int = 3600,
) -> AgentResult:
    """Stub — implement per docs/prd/orchestration.md."""
    raise NotImplementedError("runner.agents.run_claude — not yet implemented")


def run_codex(
    prompt: str,
    cwd: Path,
    codex_config_dir: Path | None = None,
    timeout: int = 3600,
) -> AgentResult:
    """Stub — Codex mirror of :func:`run_claude`."""
    raise NotImplementedError("runner.agents.run_codex — not yet implemented")
