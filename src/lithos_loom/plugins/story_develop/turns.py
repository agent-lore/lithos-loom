"""Run a single agent turn (coder or reviewer) and parse its structured result.

A turn is ``docker exec ...`` into a warm container: claude as
``claude --session-id <id> -p --output-format json``, or codex (#94) as
``codex exec [resume <thread_id>] --json``. Completion, error, and cost all come
from the parsed structured output + the process exit code — no terminal scraping
(ADR 0002). The machinery is identical for the coder and the reviewer; only the
prompt + container differ. The two tools parse differently (claude emits a
single JSON result object; codex emits a JSONL event stream), so :func:`run_turn`
dispatches to the matching parser on *tool*.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass

from . import containers


@dataclass(frozen=True)
class TurnResult:
    """Outcome of one agent turn."""

    exit_code: int
    succeeded: bool
    session_id: str
    result_text: str
    cost_usd: float
    raw: dict | None
    stderr: str

    @property
    def timed_out(self) -> bool:
        return self.exit_code == _TIMEOUT_EXIT


_TIMEOUT_EXIT = 124  # conventional timeout exit; we set it ourselves on timeout


def parse_claude_result(stdout: str, *, exit_code: int, stderr: str) -> TurnResult:
    """Parse ``claude --output-format json`` stdout into a TurnResult.

    The payload is a single JSON object (``type: "result"``) carrying
    ``is_error``, ``result``, ``session_id`` and ``total_cost_usd``. A
    non-zero exit *or* ``is_error: true`` *or* unparseable output is a failure.
    """
    raw: dict | None = None
    try:
        parsed = json.loads(stdout) if stdout.strip() else None
        if isinstance(parsed, dict):
            raw = parsed
    except json.JSONDecodeError:
        raw = None

    is_error = bool(raw.get("is_error")) if raw else True
    # ``or ""`` normalises an explicit JSON ``null`` to "" (not the string "None").
    session_id = str(raw.get("session_id") or "") if raw else ""
    result_text = str(raw.get("result") or "") if raw else ""
    cost_usd = float(raw.get("total_cost_usd") or 0.0) if raw else 0.0
    # A non-empty session_id is required for success so later resume turns (T3)
    # always have a handle to resume.
    succeeded = exit_code == 0 and raw is not None and not is_error and bool(session_id)

    return TurnResult(
        exit_code=exit_code,
        succeeded=succeeded,
        session_id=session_id,
        result_text=result_text,
        cost_usd=cost_usd,
        raw=raw,
        stderr=stderr,
    )


def parse_codex_result(
    stdout: str,
    *,
    exit_code: int,
    stderr: str,
    session_id: str = "",
    resume: bool = False,
) -> TurnResult:
    """Parse ``codex exec --json`` JSONL stdout into a TurnResult (#94).

    The stream is one JSON object per line. We read:

    * ``{"type": "thread.started", "thread_id": ...}`` → the session handle
      (codex *mints* it on turn 1; we capture it for ``codex exec resume``);
    * the last ``{"type": "item.completed", "item": {"type": "agent_message",
      "text": ...}}`` → the final message text;
    * ``{"type": "turn.completed", "usage": {...}}`` → success signal + token
      usage (stashed in ``raw``);
    * ``{"type": "turn.failed" | "error", ...}`` → failure; the event is
      retained **verbatim** in ``raw["failure_events"]`` (issue #103 Part A).
      The exact codex limit signal is not yet known, so we capture the events
      without interpreting them — ``record_failure_fixture`` serialises
      ``raw``, so a real usage-limit then lands in the failures dir with its
      precise wording, ready to classify (Part B). Interpreting the shape here
      would be guessing the very fields the capture is meant to discover.

    The returned ``session_id`` is the captured ``thread_id``, or — on a
    **resume** turn where the stream may not re-announce ``thread.started`` —
    the inbound *session_id* (the handle we resumed). Success requires a
    zero exit, a ``turn.completed``, no failure event, AND a usable handle (so
    later resume turns always have something to resume — mirrors the claude
    contract). ``cost_usd`` is ``0.0``: codex reports tokens, not USD; the
    ``max_cost_usd`` ceiling is claude-only (the cost-measure design is #102).
    Token usage is preserved in ``raw`` for the run summary. Unparseable lines
    are skipped defensively.
    """
    thread_id = ""
    result_text = ""
    usage: dict | None = None
    saw_completed = False
    failure_events: list[dict] = []

    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        etype = event.get("type")
        if etype == "thread.started":
            thread_id = str(event.get("thread_id") or "")
        elif etype == "item.completed":
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") == "agent_message":
                result_text = str(item.get("text") or "")
        elif etype == "turn.completed":
            saw_completed = True
            u = event.get("usage")
            if isinstance(u, dict):
                usage = u
        elif etype in ("turn.failed", "error"):
            failure_events.append(event)

    # On resume, keep the handle we resumed even if the stream didn't re-emit
    # thread.started; on the first turn the handle MUST be captured fresh.
    handle = thread_id or (session_id if resume else "")
    succeeded = exit_code == 0 and saw_completed and not failure_events and bool(handle)
    # ``raw`` carries the token usage (no USD) for the run summary / findings,
    # and any failure event(s) verbatim for the G4 capture harness (#103).
    raw_parts: dict = {}
    if usage is not None:
        raw_parts["usage"] = usage
    if failure_events:
        raw_parts["failure_events"] = failure_events
    raw = raw_parts or None

    return TurnResult(
        exit_code=exit_code,
        succeeded=succeeded,
        session_id=handle,
        result_text=result_text,
        cost_usd=0.0,
        raw=raw,
        stderr=stderr,
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
