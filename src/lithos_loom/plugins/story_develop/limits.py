"""Usage-limit classification + role-aware reaction policy (PRD decisions #4/#5).

Classification is **pattern-table driven** over a failed turn's structured
output (result text + stderr — never pane scraping, ADR 0002). The safe
default is deliberate: an UNRECOGNISED failure is a generic ``agent_error``,
NOT ``usage_limited`` — the system must never mis-pause on an ordinary crash.

Because real limit events are rare and their wording shifts between CLI
versions, every failed turn is also captured as a **fixture** under the run's
``failures/`` dir (the Phase-0 G4 capture harness): when a real limit fires in
production, its raw output lands on disk ready to be added to the pattern
table and the test corpus.

Reaction policy (implemented by :mod:`develop`):

* **coder** → pause-and-wait for the reset window (the coder's in-session
  context is the thing being protected), capped by ``max_pause_minutes``;
* **reviewer** → switch to the next tool in the ``fallback_chain`` immediately
  (replace only that container, reseed from handoff history); pause only when
  no alternate exists.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .turns import TurnResult

USAGE_LIMITED = "usage_limited"
AGENT_ERROR = "agent_error"

# Patterns that positively identify a provider usage limit. Matched against
# the failed turn's result text AND stderr, case-insensitively. Keep this
# table tight — false positives pause the run; false negatives just fail it
# (recoverable by re-running). Extend from captured fixtures, not guesses.
_USAGE_LIMIT_PATTERNS: tuple[re.Pattern[str], ...] = (
    # API-style sentinel: "Claude AI usage limit reached|1717777777"
    re.compile(r"usage limit reached", re.IGNORECASE),
    re.compile(r"hit your usage limit", re.IGNORECASE),
    # CLI-style wording: "5-hour limit reached ∙ resets 3am"
    re.compile(r"\b(?:\d+-hour|weekly|session)\s+limit reached", re.IGNORECASE),
    re.compile(r"\bout of (?:usage|messages)\b", re.IGNORECASE),
    re.compile(r"\bquota exceeded\b", re.IGNORECASE),
)

# "...|1717777777" — epoch seconds appended after a pipe (API sentinel style).
_EPOCH_RE = re.compile(r"limit reached\|(\d{9,12})")


def _failure_text(turn: TurnResult) -> str:
    """The searchable text of a failed turn (result text + stderr)."""
    return f"{turn.result_text}\n{turn.stderr}"


def classify_failure(turn: TurnResult) -> str:
    """Classify a FAILED turn as ``usage_limited`` or ``agent_error``.

    Timeouts are agent errors (the limit signal is an explicit message, not
    silence). Unknown failures default to ``agent_error`` — never mis-pause.
    """
    if turn.succeeded:
        raise ValueError("classify_failure() called on a successful turn")
    if turn.timed_out:
        return AGENT_ERROR
    text = _failure_text(turn)
    if any(p.search(text) for p in _USAGE_LIMIT_PATTERNS):
        return USAGE_LIMITED
    return AGENT_ERROR


def reset_hint(turn: TurnResult, *, now: datetime | None = None) -> datetime | None:
    """Best-effort parse of WHEN the limit resets, or ``None`` if unknown.

    Only the unambiguous epoch sentinel (``...limit reached|<epoch>``) is
    parsed; fuzzy wordings ("resets 3am") are ignored — the caller falls back
    to interval polling, which self-corrects.
    """
    m = _EPOCH_RE.search(_failure_text(turn))
    if not m:
        return None
    try:
        ts = datetime.fromtimestamp(int(m.group(1)), tz=UTC)
    except (ValueError, OverflowError, OSError):
        return None
    current = now or datetime.now(tz=UTC)
    # A hint in the past (or absurdly far future) is noise, not a schedule.
    if ts <= current or (ts - current).total_seconds() > 14 * 24 * 3600:
        return None
    return ts


@dataclass(frozen=True)
class PausePlan:
    """How long to wait before retrying a usage-limited turn."""

    wait_seconds: float
    reason: str  # human-readable, for the countdown log line


def pause_plan(
    turn: TurnResult,
    *,
    poll_seconds: float,
    remaining_seconds: float,
    now: datetime | None = None,
) -> PausePlan | None:
    """Compute the next wait, or ``None`` when the pause budget is exhausted.

    With a parseable reset hint, wait until then (plus a small grace margin);
    otherwise poll at ``poll_seconds``. Either way the wait never exceeds the
    remaining pause budget — when it would, the budget is spent and the caller
    checkpoints instead.
    """
    if remaining_seconds <= 0:
        return None
    hint = reset_hint(turn, now=now)
    if hint is not None:
        current = now or datetime.now(tz=UTC)
        until_reset = (hint - current).total_seconds() + 30  # grace margin
        if until_reset > remaining_seconds:
            return None  # the reset lands beyond our budget: don't half-wait
        return PausePlan(
            wait_seconds=until_reset,
            reason=f"provider reset at {hint.isoformat(timespec='seconds')}",
        )
    return PausePlan(
        wait_seconds=min(poll_seconds, remaining_seconds),
        reason="no reset hint; polling",
    )


def next_fallback_tool(chain: tuple[str, ...], current: str) -> str | None:
    """The tool after *current* in *chain*, or ``None`` when exhausted.

    A *current* not present in the chain returns the chain's first entry that
    differs from it (the chain is the project's preference order, not a state
    machine).
    """
    if current in chain:
        idx = chain.index(current)
        return chain[idx + 1] if idx + 1 < len(chain) else None
    return next((t for t in chain if t != current), None)


def record_failure_fixture(
    failures_dir: Path, *, agent: str, round_no: int, turn: TurnResult
) -> Path:
    """Persist a failed turn's raw output as a classification fixture (G4).

    These files are the capture harness: real limit events land here with
    their exact wording, ready to be promoted into ``_USAGE_LIMIT_PATTERNS``
    and the test corpus. Repeated failures in the same round/agent (limit
    retries, malformed-handoff retries) get a numeric suffix rather than
    overwriting — each attempt's wording is preserved.
    """
    failures_dir.mkdir(parents=True, exist_ok=True)
    base = f"round_{round_no:02d}_{agent}"
    path = failures_dir / f"{base}.json"
    attempt = 2
    while path.exists():
        path = failures_dir / f"{base}_{attempt:02d}.json"
        attempt += 1
    path.write_text(
        json.dumps(
            {
                "agent": agent,
                "round": round_no,
                "exit_code": turn.exit_code,
                "classification": classify_failure(turn),
                "result_text": turn.result_text,
                "stderr": turn.stderr,
                "raw": turn.raw,
            },
            indent=2,
            default=str,
        )
        + "\n",
        encoding="utf-8",
    )
    return path
