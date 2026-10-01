"""The durable resume record (U1, task 250d231f): parse, write, consume."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

from lithos_loom.subscriptions.dispatch_guards import last_attempt_key
from lithos_loom.subscriptions.resume_record import (
    RESUME_REASON_USAGE_LIMITED,
    ResumeRecord,
    consume_resume_record,
    resume_key,
    resume_record_for_route,
    write_resume_record,
)

_AT = datetime(2026, 10, 1, 15, 0, tzinfo=UTC)


def _record(**extra: Any) -> dict[str, Any]:
    return {
        "resume_after": "2026-10-01T15:00:00+00:00",
        "attempts": 2,
        "reason": RESUME_REASON_USAGE_LIMITED,
        "run_id": "r1",
        "scheduled_at": "2026-10-01T13:00:00+00:00",
        **extra,
    }


def test_resume_key_is_per_route() -> None:
    assert resume_key("story-develop") == "loom_resume:story-develop"
    assert resume_key("a") != resume_key("b")


def test_record_parses_from_metadata() -> None:
    rec = resume_record_for_route({resume_key("r"): _record()}, "r")
    assert rec == ResumeRecord(
        resume_after=_AT, attempts=2, run_id="r1", reason=RESUME_REASON_USAGE_LIMITED
    )


def test_record_absent_for_other_route_or_missing() -> None:
    assert resume_record_for_route({}, "r") is None
    assert resume_record_for_route({resume_key("other"): _record()}, "r") is None
    assert resume_record_for_route({resume_key("r"): "garbage"}, "r") is None


def test_record_without_parseable_resume_after_is_absent() -> None:
    """No time, no wait: the record degrades to the pre-U1 behaviour (dispatch
    now), never to a stuck task."""
    assert (
        resume_record_for_route({resume_key("r"): _record(resume_after="x")}, "r")
        is None
    )
    assert (
        resume_record_for_route({resume_key("r"): _record(resume_after=None)}, "r")
        is None
    )


def test_naive_resume_after_is_read_as_utc() -> None:
    rec = resume_record_for_route(
        {resume_key("r"): _record(resume_after="2026-10-01T15:00:00")}, "r"
    )
    assert rec is not None and rec.resume_after == _AT


def test_malformed_attempts_read_as_zero_and_bool_is_not_a_count() -> None:
    for bad in ("3", None, -1, True, 2.5):
        rec = resume_record_for_route({resume_key("r"): _record(attempts=bad)}, "r")
        assert rec is not None and rec.attempts == 0, bad


def test_run_id_must_be_a_plain_handle() -> None:
    """The id is joined onto the work dir on re-dispatch (security/f-001)."""
    for bad in ("../other", "/abs", "", None, 7):
        rec = resume_record_for_route({resume_key("r"): _record(run_id=bad)}, "r")
        assert rec is not None and rec.run_id is None, bad


def test_marker_round_trips() -> None:
    rec = ResumeRecord(resume_after=_AT, attempts=1, run_id="r1")
    marker = rec.as_marker(now=_AT - timedelta(hours=1))
    assert marker == {
        "resume_after": "2026-10-01T15:00:00+00:00",
        "attempts": 1,
        "reason": RESUME_REASON_USAGE_LIMITED,
        "run_id": "r1",
        "scheduled_at": "2026-10-01T14:00:00+00:00",
    }
    assert resume_record_for_route({resume_key("r"): marker}, "r") == rec


def test_marker_omits_an_absent_run_id() -> None:
    marker = ResumeRecord(resume_after=_AT, attempts=1, run_id=None).as_marker(now=_AT)
    assert "run_id" not in marker


def test_remaining_delay_floors_at_zero() -> None:
    rec = ResumeRecord(resume_after=_AT, attempts=0, run_id=None)
    assert rec.remaining_seconds(now=_AT - timedelta(seconds=90)) == 90.0
    assert rec.remaining_seconds(now=_AT + timedelta(seconds=90)) == 0.0


async def test_write_is_one_update_and_clears_the_marker_when_asked() -> None:
    lithos = AsyncMock()
    rec = ResumeRecord(resume_after=_AT, attempts=1, run_id="r1")
    ok = await write_resume_record(
        lithos,
        task_id="t",
        route="r",
        agent="a",
        record=rec,
        clear_failed_marker=True,
        now=_AT,
    )
    assert ok is True
    lithos.task_update.assert_awaited_once()
    kwargs = lithos.task_update.await_args.kwargs
    assert kwargs["task_id"] == "t" and kwargs["agent"] == "a"
    assert kwargs["metadata"] == {
        resume_key("r"): rec.as_marker(now=_AT),
        last_attempt_key("r"): None,
    }


async def test_write_without_marker_clear_touches_one_key() -> None:
    lithos = AsyncMock()
    rec = ResumeRecord(resume_after=_AT, attempts=1, run_id=None)
    await write_resume_record(
        lithos, task_id="t", route="r", agent="a", record=rec, clear_failed_marker=False
    )
    assert set(lithos.task_update.await_args.kwargs["metadata"]) == {resume_key("r")}


async def test_write_failure_is_reported_not_raised() -> None:
    lithos = AsyncMock()
    lithos.task_update.side_effect = RuntimeError("lithos down")
    rec = ResumeRecord(resume_after=_AT, attempts=1, run_id=None)
    ok = await write_resume_record(
        lithos, task_id="t", route="r", agent="a", record=rec, clear_failed_marker=False
    )
    assert ok is False


async def test_consume_deletes_the_key_only_when_the_payload_carried_it() -> None:
    lithos = AsyncMock()
    await consume_resume_record(
        lithos, task_id="t", route="r", agent="a", payload={"metadata": {}}
    )
    lithos.task_update.assert_not_called()

    await consume_resume_record(
        lithos,
        task_id="t",
        route="r",
        agent="a",
        payload={"metadata": {resume_key("r"): _record()}},
    )
    assert lithos.task_update.await_args.kwargs["metadata"] == {resume_key("r"): None}


async def test_consume_swallows_a_failed_delete() -> None:
    lithos = AsyncMock()
    lithos.task_update.side_effect = RuntimeError("lithos down")
    await consume_resume_record(
        lithos,
        task_id="t",
        route="r",
        agent="a",
        payload={"metadata": {resume_key("r"): _record()}},
    )  # no raise
