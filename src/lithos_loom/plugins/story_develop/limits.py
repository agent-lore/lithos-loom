"""Failure classification + reaction policy (PRD decisions #4/#5; slice B, 5dbeb0c8).

Classification is **pattern-table driven** over the WHOLE failed turn — result
text, stderr and the retained structured payload (``raw``: claude's JSON
result, codex's ``failure_events``, or unparseable stdout kept verbatim) —
never pane scraping (ADR 0002). One classifier serves every turn site (coder,
reviewer, the handoff nudge); the per-class *reaction* is data
(:func:`reaction_for`) that those sites apply. The safe default is deliberate:
an UNRECOGNISED failure is a generic ``agent_error``, NOT ``usage_limited`` and
NOT infra — the system must never mis-pause or mis-retry an ordinary crash.

Classes (:class:`FailureClass`) and their reactions:

* ``usage_limited`` → pause / tool-switch (T5, unchanged);
* ``auth_failed`` → ONE retry after a short backoff, then escalate. The
  2026-09-12 lens#82 loss: the container's token refresh failed while the
  host's credentials were valid minutes later (a rotation race on the shared,
  RW-mounted credentials file is the working hypothesis), so a re-read is worth
  one attempt — but a genuinely revoked login fails identically until a human
  re-authenticates, so the second hit escalates;
* ``transient_infra`` → up to two retries with backoff (stream disconnects,
  5xx / 429 / overloaded, socket errors), then escalate;
* ``oom_or_spawn`` → one retry (a killed process or a dead container), then
  escalate;
* ``timeout`` / ``agent_error`` → the plain failure path, as before.

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
from enum import StrEnum
from pathlib import Path

from .turns import TurnResult


class FailureClass(StrEnum):
    """Why a turn failed — the one vocabulary every turn site reacts to."""

    USAGE_LIMITED = "usage_limited"
    AUTH_FAILED = "auth_failed"
    TRANSIENT_INFRA = "transient_infra"
    OOM_OR_SPAWN = "oom_or_spawn"
    TIMEOUT = "timeout"
    AGENT_ERROR = "agent_error"


# Legacy names — the same members, so `classify_failure(t) != USAGE_LIMITED`
# and the recorded fixtures' wording are unchanged.
USAGE_LIMITED = FailureClass.USAGE_LIMITED
AGENT_ERROR = FailureClass.AGENT_ERROR

_OOM_EXIT = 137  # SIGKILL (docker OOM-kill / a reaped container)

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

# Authentication failures: the claude CLI's OAuth wordings (session expired /
# token revoked / authentication_failed / a 401 API error) and an invalid API
# key. Retried once (see the module docstring), then escalated.
_AUTH_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"failed to authenticate", re.IGNORECASE),
    re.compile(r"oauth (?:session|token|access token).{0,40}(?:expired|revoked)", re.IGNORECASE),
    re.compile(r"authentication[_ ](?:failed|error)", re.IGNORECASE),
    re.compile(r"api[_ ]error(?:[_ ]status)?\W{0,4}401\b", re.IGNORECASE),
    re.compile(r"\b401\b.{0,40}\b(?:unauthori[sz]ed|oauth|authenticat)", re.IGNORECASE),
    re.compile(r"invalid (?:api key|authentication)", re.IGNORECASE),
)

# Transient infrastructure: the transport died or the provider is busy. Worth
# a backoff-and-retry; NOT worth a pause budget (no reset window to wait for).
_TRANSIENT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"stream disconnected", re.IGNORECASE),
    re.compile(r"idle timeout waiting for websocket", re.IGNORECASE),
    re.compile(r"\breconnecting\b", re.IGNORECASE),
    re.compile(r"\b(?:ECONNRESET|ECONNREFUSED|ETIMEDOUT|EAI_AGAIN|EPIPE|EHOSTUNREACH)\b"),
    re.compile(r"api[_ ]error(?:[_ ]status)?\W{0,4}(?:5\d\d|429)\b", re.IGNORECASE),
    re.compile(r"\b(?:overloaded|internal server error|service unavailable|bad gateway|gateway time-?out)\b", re.IGNORECASE),
    re.compile(r"\brate.?limit(?:ed|_error)?\b", re.IGNORECASE),
    re.compile(r"\b(?:5\d\d|429)\b.{0,20}\b(?:error|retry)", re.IGNORECASE),
)

# The process was killed or its container is gone. One retry re-execs into
# the (possibly restarted) container; a second death escalates.
_OOM_OR_SPAWN_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"container .{0,80}is not running", re.IGNORECASE),
    re.compile(r"no such container", re.IGNORECASE),
    re.compile(r"OCI runtime exec failed", re.IGNORECASE),
    re.compile(r"\bout of memory\b|cannot allocate memory", re.IGNORECASE),
)


def _failure_text(turn: TurnResult) -> str:
    """The searchable text of a failed turn: result text + stderr + ``raw``.

    ``raw`` matters: codex retains its failure events there (never in
    ``result_text``, #103), the claude parser keeps unparseable stdout there
    (the #405 shape printed the 401 as a bare line before the JSON), and a
    claude API error carries ``api_error_status`` there.
    """
    raw = json.dumps(turn.raw, default=str) if turn.raw else ""
    return f"{turn.result_text}\n{turn.stderr}\n{raw}"


def classify_failure(turn: TurnResult) -> FailureClass:
    """Classify a FAILED turn (see :class:`FailureClass`).

    Precedence: a timeout or an OOM-kill exit is decided by the exit code
    alone (whatever the process printed is stale); then usage limit (a limit
    message can mention rate limiting — it must pause, never retry into the
    same wall); then auth (a 401 inside a disconnect wording is the auth
    problem); then transient transport; then spawn/memory wordings. Unknown
    failures default to ``agent_error`` — never mis-pause, never mis-retry.
    """
    if turn.succeeded:
        raise ValueError("classify_failure() called on a successful turn")
    if turn.timed_out:
        return FailureClass.TIMEOUT
    if turn.exit_code == _OOM_EXIT:
        return FailureClass.OOM_OR_SPAWN
    text = _failure_text(turn)
    if any(p.search(text) for p in _USAGE_LIMIT_PATTERNS):
        return FailureClass.USAGE_LIMITED
    if any(p.search(text) for p in _AUTH_PATTERNS):
        return FailureClass.AUTH_FAILED
    if any(p.search(text) for p in _TRANSIENT_PATTERNS):
        return FailureClass.TRANSIENT_INFRA
    if any(p.search(text) for p in _OOM_OR_SPAWN_PATTERNS):
        return FailureClass.OOM_OR_SPAWN
    return FailureClass.AGENT_ERROR


_SUMMARY_MAX = 200


def failure_summary(turn: TurnResult) -> str:
    """One short line naming why the turn failed, for logs / exits / the gate.

    The first non-blank line of the result text, else of stderr, else of a
    codex failure event's message, else ``exit <code>``.
    """
    candidates = [turn.result_text, turn.stderr]
    if turn.raw:
        for ev in turn.raw.get("failure_events") or ():
            if isinstance(ev, dict):
                msg = ev.get("message") or (ev.get("error") or {}).get("message")
                if msg:
                    candidates.append(str(msg))
        unparsed = turn.raw.get("unparsed_stdout")
        if unparsed:
            candidates.append(str(unparsed))
    for text in candidates:
        for line in str(text).splitlines():
            line = line.strip()
            if line:
                return line[:_SUMMARY_MAX]
    return f"exit {turn.exit_code}"


@dataclass(frozen=True)
class Reaction:
    """What a turn site does with a failed turn of one class.

    ``kind``: ``"pause"`` (the T5 usage-limit path owns it), ``"retry"`` (sleep
    ``backoff_seconds[attempt]`` and re-run, resuming the session when its
    transcript survived and ``resume`` is set), ``"fail"`` (the plain failure
    path). When a retry class is exhausted and ``escalate`` is set, the run ends
    ``infra_failed`` — a needs-human stop whose brief carries ``host_action``.
    """

    kind: str
    retries: int = 0
    backoff_seconds: tuple[float, ...] = ()
    resume: bool = True
    escalate: bool = False
    host_action: str = ""


_COMPLETE_GATE = "then complete the gate to re-dispatch"

_REACTIONS: dict[FailureClass, Reaction] = {
    FailureClass.USAGE_LIMITED: Reaction(kind="pause"),
    FailureClass.AUTH_FAILED: Reaction(
        kind="retry",
        retries=1,
        backoff_seconds=(20.0,),
        escalate=True,
        host_action=(
            "re-authenticate the agent CLI on the host (`claude` / `codex` login) "
            f"and check the mounted credentials file, {_COMPLETE_GATE}"
        ),
    ),
    FailureClass.TRANSIENT_INFRA: Reaction(
        kind="retry",
        retries=2,
        backoff_seconds=(30.0, 120.0),
        escalate=True,
        host_action=(
            f"check the host's network and the provider's status page, {_COMPLETE_GATE}"
        ),
    ),
    FailureClass.OOM_OR_SPAWN: Reaction(
        kind="retry",
        retries=1,
        backoff_seconds=(10.0,),
        escalate=True,
        host_action=(
            "check docker (`docker ps -a`, memory limits, a daemon restart that "
            f"orphaned the run's containers), {_COMPLETE_GATE}"
        ),
    ),
    FailureClass.TIMEOUT: Reaction(kind="fail"),
    FailureClass.AGENT_ERROR: Reaction(kind="fail"),
}


def reaction_for(cls: FailureClass) -> Reaction:
    """The reaction table entry for *cls* (total over :class:`FailureClass`)."""
    return _REACTIONS[cls]


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
