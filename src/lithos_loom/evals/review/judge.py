"""Mechanism LLM-judge for the eval harness (#183).

The matcher's confirmer/veto (see :mod:`match`): given a reviewer's findings and
the SPECIFIC defect mechanism, return the finding ids that describe *that*
mechanism — not merely the same file/topic. This is a pure text Q&A (no repo, no
container), so it runs as a **host-direct** agent call rather than the
container-bound :func:`~.turns.run_turn`. Host-only — needs the agent CLI on PATH.
"""

from __future__ import annotations

import logging
import re
import subprocess
from collections.abc import Iterable

from ...plugins.story_develop.turns import parse_claude_result, parse_codex_result
from .match import Judge

logger = logging.getLogger(__name__)

DEFAULT_JUDGE_TIMEOUT = 300
_MATCHED_RE = re.compile(r"MATCHED:\s*(.+)", re.IGNORECASE)


def build_agent_judge(
    tool: str = "claude",
    model: str | None = None,
    timeout: int = DEFAULT_JUDGE_TIMEOUT,
) -> Judge:
    """A :data:`~.match.Judge` backed by a host-direct agent call."""

    def judge(mechanism: str, findings: list[dict]) -> list[str]:
        if not findings:
            return []  # nothing to judge — skip the agent call
        valid = {str(f.get("finding_id", "")) for f in findings if f.get("finding_id")}
        text = _run_host_agent(tool, _judge_prompt(mechanism, findings), model, timeout)
        return _parse_matched_ids(text, valid)

    return judge


def _judge_prompt(mechanism: str, findings: list[dict]) -> str:
    lines = [
        "You are scoring an automated code review for a benchmark. Below are the "
        "findings a reviewer produced. Decide which (if any) describe THIS SPECIFIC "
        "defect — not merely the same file or topic.",
        "",
        f"DEFECT: {mechanism}",
        "",
        "FINDINGS:",
    ]
    for f in findings:
        files = ", ".join(f.get("files", []))
        lines.append(
            f"- {f.get('finding_id', '')} [{f.get('severity', '')}] "
            f"({files}) {f.get('rationale', '')}"
        )
    lines += [
        "",
        "A finding matches ONLY if it identifies the same defect mechanism stated "
        "above. A different bug in the same file or area does NOT match. Reason "
        "briefly, then conclude with a single final line exactly:",
        "`MATCHED: <comma-separated ids>`  or  `MATCHED: none`",
    ]
    return "\n".join(lines)


def _run_host_agent(tool: str, prompt: str, model: str | None, timeout: int) -> str:
    """Run one host-direct agent turn and return its message text ("" on failure)."""
    if tool == "claude":
        cmd = [
            "claude",
            *(["--model", model] if model else []),
            "-p",
            "--dangerously-skip-permissions",
            "--output-format",
            "json",
            prompt,
        ]
    elif tool == "codex":
        cmd = [
            "codex",
            "exec",
            "--json",
            "--dangerously-bypass-approvals-and-sandbox",
            *(["-m", model] if model else []),
            prompt,
        ]
    else:
        raise ValueError(f"unsupported judge tool {tool!r} (expected claude or codex)")

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        logger.warning("judge agent call failed (%s); treating as no match", exc)
        return ""
    parse = parse_codex_result if tool == "codex" else parse_claude_result
    return parse(proc.stdout, exit_code=proc.returncode, stderr=proc.stderr).result_text


def _parse_matched_ids(text: str, valid_ids: Iterable[str]) -> list[str]:
    """Parse the agent's reply into the matched finding ids.

    Authoritative source is the final ``MATCHED:`` line; absent that, a
    best-effort scan of the whole reply. Only ids that are real findings count
    (word-boundary, so ``f-1`` never matches inside ``f-10``).
    """
    valid = list(dict.fromkeys(valid_ids))
    if not text.strip() or not valid:
        return []
    matched_line: str | None = None
    for line in text.splitlines():
        m = _MATCHED_RE.search(line)
        if m:
            matched_line = m.group(1).strip()
    if matched_line is not None and matched_line.lower() == "none":
        return []
    haystack = matched_line if matched_line is not None else text
    return [vid for vid in valid if re.search(rf"\b{re.escape(vid)}\b", haystack)]
