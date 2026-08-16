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
from dataclasses import dataclass, replace

from ...plugins.story_develop import engines
from .match import Judge, JudgeVerdict

logger = logging.getLogger(__name__)

DEFAULT_JUDGE_TIMEOUT = 300

# How many agent invocations one verdict request can cost: the first attempt
# plus one retry on infra failure. Lives here, next to the loop that spends
# them, so a cost ceiling quoted elsewhere (`eval rescore`'s preflight) cannot
# drift away from the policy it describes.
MAX_ATTEMPTS_PER_REQUEST = 2
_MATCHED_RE = re.compile(r"MATCHED:\s*(.+)", re.IGNORECASE)
# "none", "none of the findings", "none — they all describe ..." — the prompt asks
# for a bare "none", but a chatty veto is common and used to match every id in it.
_NONE_RE = re.compile(r"^none\b", re.IGNORECASE)


class JudgeUnavailable(RuntimeError):
    """The judge agent produced no usable turn — infra, not a verdict."""

    def __init__(self, detail: str, *, text: str = "") -> None:
        super().__init__(detail)
        self.detail = detail
        self.text = text  # whatever the failed turn did emit, kept for the audit


@dataclass(frozen=True)
class _HostTurn:
    """One host-direct agent turn: its message text, and anything odd about it."""

    text: str
    anomaly: str = ""  # "" when the turn met the engine's own success bar


def build_agent_judge(
    tool: str = "claude",
    model: str | None = None,
    timeout: int = DEFAULT_JUDGE_TIMEOUT,
) -> Judge:
    """A :data:`~.match.Judge` backed by a host-direct agent call."""

    def attempt(prompt: str, valid: set[str]) -> JudgeVerdict:
        try:
            turn = _run_host_agent(tool, prompt, model, timeout)
        except JudgeUnavailable as exc:
            return JudgeVerdict(status="failed", reply=exc.text, detail=exc.detail)
        verdict = _parse_verdict(turn.text, valid)
        # The turn completed, so its text is a real answer; the only thing it
        # lacked was a resumable handle, which this one-shot call never uses.
        # Recorded rather than acted on, so a change in the CLI's session
        # plumbing shows up in the audit trail instead of voiding the run.
        return replace(verdict, detail=turn.anomaly) if turn.anomaly else verdict

    def judge(mechanism: str, findings: list[dict]) -> JudgeVerdict:
        if not findings:
            # Nothing to judge — an answer ("none match"), not a failure.
            return JudgeVerdict()
        valid = {str(f.get("finding_id", "")) for f in findings if f.get("finding_id")}
        prompt = _judge_prompt(mechanism, findings)
        # Every attempt but the last is retryable: the reviewer sample this
        # scores was already paid for, so one flaky subprocess must not discard
        # it. An `unparsed` reply is NOT retried — re-rolling until the model
        # answers readably would bias the sample.
        for attempt_no in range(1, MAX_ATTEMPTS_PER_REQUEST):
            verdict = attempt(prompt, valid)
            if verdict.status != "failed":
                return verdict
            logger.warning(
                "judge agent call failed (attempt %d/%d: %s); retrying",
                attempt_no,
                MAX_ATTEMPTS_PER_REQUEST,
                verdict.detail,
            )
        verdict = attempt(prompt, valid)  # the last attempt's answer stands
        if verdict.status == "failed":
            logger.warning(
                "judge agent gave no verdict (%s); sample excluded", verdict.detail
            )
        return verdict

    return judge


def _judge_prompt(mechanism: str, findings: list[dict]) -> str:
    # Paragraphs are named locals (not adjacent literals inside the list) so the
    # implicit concatenation is unambiguous — no "maybe a missing comma?" footgun.
    intro = (
        "You are scoring an automated code review for a benchmark. Below are the "
        "findings a reviewer produced. Decide which (if any) describe THIS SPECIFIC "
        "defect — not merely the same file or topic."
    )
    rule = (
        "A finding matches ONLY if it identifies the same defect mechanism stated "
        "above. A different bug in the same file or area does NOT match. Reason "
        "briefly, then conclude with a single final line exactly:"
    )
    lines = [intro, "", f"DEFECT: {mechanism}", "", "FINDINGS:"]
    for f in findings:
        files = ", ".join(f.get("files", []))
        lines.append(
            f"- {f.get('finding_id', '')} [{f.get('severity', '')}] "
            f"({files}) {f.get('rationale', '')}"
        )
    lines += [
        "",
        rule,
        "`MATCHED: <comma-separated ids>`  or  `MATCHED: none`",
    ]
    return "\n".join(lines)


def _run_host_agent(
    tool: str, prompt: str, model: str | None, timeout: int
) -> _HostTurn:
    """Run one host-direct agent turn; raise :class:`JudgeUnavailable` on infra.

    Drives the same :class:`~...plugins.story_develop.engines.Engine` adapter the
    container turn path uses (ARCH-2.E5) instead of a second per-tool argv +
    parser pick: the bare host-side argv (no docker, no session) is
    ``engine.cli_argv(session_id=None)`` and the result parse is
    ``engine.parse_turn`` — one implementation.

    A turn that did not **complete** (non-zero exit, claude ``is_error``, a codex
    ``turn.failed`` / missing ``turn.completed``) raises: its text may be partial
    or stale — the codex stream retains the last agent message even when a later
    event fails the turn — so parsing it could manufacture a verdict out of a
    crash (#307).

    A turn that completed but minted **no resumable handle** is only an
    *anomaly*: both engines fold that handle into ``succeeded`` so a later resume
    turn can re-attach, which a one-shot judge Q&A never needs. Gating the eval's
    scorer on it would mean a CLI that stopped echoing a handle turned every case
    into zero valid samples.
    """
    if not engines.is_supported(tool):
        raise ValueError(
            f"unsupported judge tool {tool!r} "
            f"(expected {engines.supported_tools_phrase()})"
        )
    engine = engines.get_engine(tool)
    cmd = engine.cli_argv(prompt=prompt, model=model)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        raise JudgeUnavailable(f"{type(exc).__name__}: {exc}") from exc
    result = engine.parse_turn(
        proc.stdout, exit_code=proc.returncode, stderr=proc.stderr
    )
    if not result.completed:
        raise JudgeUnavailable(
            f"turn did not complete (exit {result.exit_code}): "
            f"{result.stderr.strip()[:200]}",
            text=result.result_text,
        )
    anomaly = ""
    if not result.succeeded:
        anomaly = f"turn minted no resumable session handle (exit {result.exit_code})"
    return _HostTurn(text=result.result_text, anomaly=anomaly)


def _parse_verdict(text: str, valid_ids: Iterable[str]) -> JudgeVerdict:
    """Parse the agent's reply into a :class:`~.match.JudgeVerdict`.

    The authoritative source is the final ``MATCHED:`` line, and **only** that
    line. There is deliberately no whole-reply fallback: the prompt asks the
    model to reason before concluding, so scanning the prose would score
    "f-001 does NOT describe this" as a match — manufacturing a catch out of a
    rejection (#307). A reply with no readable verdict is ``unparsed``: an
    absence of an answer, not a veto.
    """
    valid = list(dict.fromkeys(valid_ids))
    matched_line: str | None = None
    for line in text.splitlines():
        m = _MATCHED_RE.search(line)
        if m:
            matched_line = m.group(1).strip()
    if matched_line is None:
        return JudgeVerdict(status="unparsed", reply=text)
    if _NONE_RE.match(matched_line):
        return JudgeVerdict(reply=text)  # an explicit veto
    # Only ids that are real findings count (word-boundary, so `f-1` never
    # matches inside `f-10`), in findings order.
    ids = tuple(
        vid for vid in valid if re.search(rf"\b{re.escape(vid)}\b", matched_line)
    )
    return JudgeVerdict(matched_ids=ids, reply=text)
