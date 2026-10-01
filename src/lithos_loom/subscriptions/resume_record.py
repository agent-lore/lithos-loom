"""The durable half of the usage-limit resume (U1, task 250d231f).

A story-develop run that hits a provider usage limit ends ``interrupted`` with
a ``resume`` block saying when a re-run is expected to succeed, and the
route-runner schedules an in-process re-dispatch for then (T10). Until this
module that schedule lived in the runner's memory only — its own docstring
said so — so a daemon restart during the wait forgot it, and the restart's
bootstrap replay re-dispatched the open task straight back into the wall with
a fresh attempt count. Four of the six T2-era interventions were host
restarts under a run.

The fix is the narrow durable-execution principle without a new service
(``docs/prd/unattended-duration.md``, decision 4): the next eligible attempt
and the attempts consumed are recorded **on the Lithos task** —
``metadata.loom_resume:<route>`` — and the runner rebuilds its timer from the
record on every event it sees. Business state stays in Lithos; this is an
execution record, not a second journal that could disagree with the task
graph.

Lifecycle, so that every exit is accounted for:

1. **Written on the ``interrupted`` path, before the claim release** (the
   failed-attempt marker's rule: the record exists by the time the task is
   externally visible as unclaimed), in the same ``task_update`` that clears a
   superseded failed marker — one write. ``attempts`` is the record carried
   by the dispatch payload plus one; the task is the count, the runner keeps
   none.
2. **Consumed at claim time** (a per-key delete, beside the resolved-escalation
   clear), so a present record means exactly *a re-dispatch is scheduled and
   has not started*. Success, gated delivery, failure, contract violation,
   timeout, launch error and unknown status then need no clearing — none of
   them can see a record.
3. **Exhaustion** is unchanged: attempts at ``MAX_RESUMES_PER_TASK`` raise the
   ``resume_exhausted`` gate and write no record, so ticking the gate is a
   fresh budget with no special case.
4. **Honoured on every origin** but the operator's gate tick, and before the
   in-process dedup — the record is the authority and the armed timer follows
   it: a timer armed for the recorded time is left alone, one armed for
   another time is re-armed (at once when the new time has passed), no timer
   and a time ahead arms one, no timer and a time passed falls through to an
   ordinary dispatch carrying its attempts into step 1. The sleeper disarms
   itself before its re-read. Never counts an attempt. This covers the
   restart bootstrap and a hand-edited time, before or after a restart.

The record also names the interrupted run, so the dispatch can hand the plugin
a resume pointer (5dbeb0c8 slice C) and continue the branch the wait was for —
paying the rounds again after waiting them out would defeat the wait.

Reserved namespace: ``loom_resume:*`` is runner-owned, like
``loom_last_attempt:*`` — plugins see it in their ``task.json`` metadata and
must not repurpose it (SPECIFICATION §2.2). Operator gestures: cancel the
story, or pull the route's trigger tag (both checked when the timer fires);
editing ``resume_after`` by hand moves the armed timer as soon as the edit's
``task.updated`` arrives.

Tolerant by design: a malformed record degrades to the pre-U1 behaviour
(dispatch now, count from zero), never to a stuck task.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from lithos_loom.gates import is_plain_run_id
from lithos_loom.subscriptions.dispatch_guards import last_attempt_key

__all__ = [
    "RESUME_KEY_PREFIX",
    "RESUME_REASON_USAGE_LIMITED",
    "ResumeRecord",
    "consume_resume_record",
    "parse_resume_after",
    "resume_key",
    "resume_record_for_route",
    "write_resume_record",
]

logger = logging.getLogger(__name__)

# Per-route task-metadata key prefix; the full key is ``loom_resume:<route>``
# and its value is {"resume_after", "attempts", "reason", "run_id"?,
# "scheduled_at"}. One key per route, as for the failed-attempt marker: two
# routes pausing on the same task each keep their own schedule.
RESUME_KEY_PREFIX = "loom_resume:"

# The one reason the record is written for today. A closed vocabulary from the
# start so a later writer (a provider capacity cool-off, #420 / U2) is a new
# member, not a free string.
RESUME_REASON_USAGE_LIMITED = "usage_limited"


def resume_key(route: str) -> str:
    """The task-metadata key holding ``route``'s pending resume."""
    return f"{RESUME_KEY_PREFIX}{route}"


def parse_resume_after(raw: Any) -> datetime | None:
    """*raw* as an aware UTC instant, or ``None`` when it is not a timestamp.

    A naive value is read as UTC (the plugin writes aware ISO 8601; a hand
    edit may not).
    """
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


@dataclass(frozen=True)
class ResumeRecord:
    """A pending re-dispatch as recorded on the task."""

    resume_after: datetime
    attempts: int
    run_id: str | None
    reason: str = RESUME_REASON_USAGE_LIMITED

    def remaining_seconds(self, *, now: datetime | None = None) -> float:
        """Seconds until ``resume_after``, floored at zero."""
        now = now or datetime.now(UTC)
        return max(0.0, (self.resume_after - now).total_seconds())

    def as_marker(self, *, now: datetime | None = None) -> dict[str, Any]:
        """The metadata value — ``run_id`` only when there is one."""
        now = now or datetime.now(UTC)
        marker: dict[str, Any] = {
            "resume_after": self.resume_after.isoformat(timespec="seconds"),
            "attempts": self.attempts,
            "reason": self.reason,
            "scheduled_at": now.isoformat(timespec="seconds"),
        }
        if self.run_id:
            marker["run_id"] = self.run_id
        return marker


def resume_record_for_route(
    metadata: Mapping[str, Any], route: str
) -> ResumeRecord | None:
    """``route``'s pending resume as recorded on the task, or ``None``.

    ``None`` for a missing record and for one with no parseable
    ``resume_after`` (no time, no wait). Anything else degrades field by
    field: a malformed ``attempts`` reads as zero (``bool`` is not a count),
    and a ``run_id`` that is not a plain handle is dropped — it is joined onto
    the work dir on re-dispatch (security/f-001), so an unsafe one is simply
    "no pointer", exactly as a missing one.
    """
    raw = metadata.get(resume_key(route))
    if not isinstance(raw, Mapping):
        return None
    resume_after = parse_resume_after(raw.get("resume_after"))
    if resume_after is None:
        return None
    attempts_raw = raw.get("attempts")
    attempts = (
        attempts_raw
        if isinstance(attempts_raw, int)
        and not isinstance(attempts_raw, bool)
        and attempts_raw >= 0
        else 0
    )
    run_id_raw = raw.get("run_id")
    run_id = (
        run_id_raw
        if isinstance(run_id_raw, str) and is_plain_run_id(run_id_raw)
        else None
    )
    reason_raw = raw.get("reason")
    reason = (
        reason_raw
        if isinstance(reason_raw, str) and reason_raw
        else RESUME_REASON_USAGE_LIMITED
    )
    return ResumeRecord(
        resume_after=resume_after, attempts=attempts, run_id=run_id, reason=reason
    )


class _RecordClient(Protocol):
    async def task_update(
        self,
        *,
        task_id: str,
        agent: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Any: ...


async def write_resume_record(
    lithos: _RecordClient,
    *,
    task_id: str,
    route: str,
    agent: str,
    record: ResumeRecord,
    clear_failed_marker: bool,
    now: datetime | None = None,
) -> bool:
    """Persist *record* on the task; returns whether the write landed.

    With *clear_failed_marker* the route's failed-attempt marker is deleted in
    the SAME write (an interrupted attempt supersedes an earlier failure —
    ``dispatch_guards.clear_superseded_failure``'s rule, folded in so the two
    cannot land apart). ``task_update`` metadata is an additive per-key merge,
    so nothing else on the task is touched. Best-effort: a Lithos hiccup is
    logged, and the in-process timer still runs — the record is the durable
    half, not a precondition.
    """
    metadata: dict[str, Any] = {resume_key(route): record.as_marker(now=now)}
    if clear_failed_marker:
        metadata[last_attempt_key(route)] = None
    try:
        await lithos.task_update(task_id=task_id, agent=agent, metadata=metadata)
    except Exception:
        logger.exception(
            "route %s: recording the pending resume on %s failed", route, task_id
        )
        return False
    return True


async def consume_resume_record(
    lithos: _RecordClient,
    *,
    task_id: str,
    route: str,
    agent: str,
    payload: Mapping[str, Any],
) -> None:
    """Best-effort per-key delete of ``route``'s resume record, iff the
    dispatch-time *payload* carried one (no round trip otherwise).

    Runs after a successful claim, on every origin: the re-dispatch the record
    scheduled is now under way, so "scheduled and not started" is no longer
    true. Failures are logged and swallowed — a stale record is honoured only
    while its time is ahead, and the next interruption overwrites it.
    """
    if resume_key(route) not in (payload.get("metadata") or {}):
        return
    try:
        await lithos.task_update(
            task_id=task_id, agent=agent, metadata={resume_key(route): None}
        )
    except Exception:
        logger.exception(
            "route %s: consuming the pending resume on %s failed", route, task_id
        )
