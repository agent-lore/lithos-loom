"""Tests for usage-limit classification + reaction policy (T5)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from lithos_loom.plugins.story_develop.engines import CodexEngine
from lithos_loom.plugins.story_develop.limits import (
    AGENT_ERROR,
    USAGE_LIMITED,
    classify_failure,
    next_fallback_tool,
    pause_plan,
    record_failure_fixture,
    reset_hint,
)
from lithos_loom.plugins.story_develop.turns import TurnResult


def _failed(
    *, result_text: str = "", stderr: str = "", exit_code: int = 1
) -> TurnResult:
    return TurnResult(
        exit_code=exit_code,
        succeeded=False,
        session_id="",
        result_text=result_text,
        cost_usd=0.0,
        raw=None,
        stderr=stderr,
    )


# --- classification ----------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Claude AI usage limit reached|1750000000",
        "You've hit your usage limit. Upgrade to continue.",
        "5-hour limit reached ∙ resets 3am",
        "weekly limit reached",
        "session limit reached - try again later",
        "You are out of usage for today",
        "quota exceeded for this billing period",
    ],
)
def test_classifies_usage_limit_wordings(text: str) -> None:
    assert classify_failure(_failed(result_text=text)) == USAGE_LIMITED


def test_classifies_limit_in_stderr() -> None:
    assert (
        classify_failure(_failed(stderr="Claude AI usage limit reached|1"))
        == USAGE_LIMITED
    )


@pytest.mark.parametrize(
    "text",
    [
        "",  # nothing at all
        "TypeError: cannot read properties of undefined",
        "fatal: not a git repository",
        "API Error: 500 internal server error",
        "rate limited, retrying",  # transient 429s are NOT a usage limit
        "context window limit reached",  # context limit != usage limit
    ],
)
def test_unrecognised_failures_are_agent_errors(text: str) -> None:
    # The safe default: never mis-pause on an ordinary crash.
    assert classify_failure(_failed(result_text=text)) == AGENT_ERROR


def test_timeout_is_agent_error_even_with_limit_text() -> None:
    turn = _failed(result_text="usage limit reached", exit_code=124)
    assert classify_failure(turn) == AGENT_ERROR


def test_codex_raw_limit_events_are_not_yet_classified() -> None:
    """The G4 boundary (#103): a real codex usage-limit is captured but not classified.

    A real codex limit arrives as a JSONL ``turn.failed`` event, NOT an
    ``agent_message`` — so :meth:`CodexEngine.parse_turn` stores it verbatim in
    ``raw["failure_events"]`` and leaves ``result_text`` empty. Because
    :func:`classify_failure` searches only ``result_text`` + ``stderr`` (never
    ``raw``), the limit is NOT yet recognised as ``USAGE_LIMITED`` — it stays
    ``AGENT_ERROR``, so a *real* codex limit does not yet reach the pause/resume
    path. ARCH-2.E2 makes the resume mechanics correct (pinned by the coder test
    with a *synthetic already-classified* failure); promoting the captured raw
    wording into classification is the dormant G4 work. When G4 lands, flip this
    assertion to ``USAGE_LIMITED``.
    """
    stream = "\n".join(
        json.dumps(e)
        for e in (
            {"type": "thread.started", "thread_id": "t1"},
            {
                "type": "turn.failed",
                "error": {
                    "message": "You've hit your usage limit.",
                    "type": "usage_limit",
                },
            },
        )
    )
    turn = CodexEngine().parse_turn(stream, exit_code=1, stderr="")
    assert turn.succeeded is False
    # the limit wording is captured verbatim in raw, NOT in result_text / stderr …
    assert turn.result_text == "" and turn.stderr == ""
    assert turn.raw is not None and "failure_events" in turn.raw
    assert "usage limit" in json.dumps(turn.raw["failure_events"])
    # … so the current classifier (result_text + stderr only) does not see it.
    assert classify_failure(turn) == AGENT_ERROR


def test_classify_rejects_successful_turn() -> None:
    ok = TurnResult(
        exit_code=0,
        succeeded=True,
        session_id="s",
        result_text="",
        cost_usd=0.0,
        raw={},
        stderr="",
    )
    with pytest.raises(ValueError):
        classify_failure(ok)


# --- reset hint --------------------------------------------------------------


def test_reset_hint_parses_epoch_sentinel() -> None:
    now = datetime(2026, 6, 12, 12, 0, tzinfo=UTC)
    epoch = int((now + timedelta(hours=2)).timestamp())
    turn = _failed(result_text=f"Claude AI usage limit reached|{epoch}")
    assert reset_hint(turn, now=now) == datetime.fromtimestamp(epoch, tz=UTC)


def test_reset_hint_ignores_past_epoch() -> None:
    now = datetime(2026, 6, 12, 12, 0, tzinfo=UTC)
    epoch = int((now - timedelta(hours=1)).timestamp())
    turn = _failed(result_text=f"usage limit reached|{epoch}")
    assert reset_hint(turn, now=now) is None


def test_reset_hint_ignores_absurd_future() -> None:
    now = datetime(2026, 6, 12, 12, 0, tzinfo=UTC)
    epoch = int((now + timedelta(days=30)).timestamp())
    turn = _failed(result_text=f"usage limit reached|{epoch}")
    assert reset_hint(turn, now=now) is None


def test_reset_hint_none_for_fuzzy_wording() -> None:
    assert reset_hint(_failed(result_text="5-hour limit reached, resets 3am")) is None


# --- pause planning ----------------------------------------------------------


def test_pause_plan_uses_reset_hint_when_within_budget() -> None:
    now = datetime(2026, 6, 12, 12, 0, tzinfo=UTC)
    epoch = int((now + timedelta(minutes=10)).timestamp())
    turn = _failed(result_text=f"usage limit reached|{epoch}")
    plan = pause_plan(turn, poll_seconds=300, remaining_seconds=3600, now=now)
    assert plan is not None
    assert plan.wait_seconds == pytest.approx(630)  # 600s + 30s grace


def test_pause_plan_refuses_reset_beyond_budget() -> None:
    now = datetime(2026, 6, 12, 12, 0, tzinfo=UTC)
    epoch = int((now + timedelta(hours=3)).timestamp())
    turn = _failed(result_text=f"usage limit reached|{epoch}")
    assert pause_plan(turn, poll_seconds=300, remaining_seconds=3600, now=now) is None


def test_pause_plan_polls_without_hint() -> None:
    plan = pause_plan(
        _failed(result_text="usage limit reached"),
        poll_seconds=300,
        remaining_seconds=3600,
    )
    assert plan is not None and plan.wait_seconds == 300


def test_pause_plan_caps_poll_at_remaining_budget() -> None:
    plan = pause_plan(
        _failed(result_text="usage limit reached"),
        poll_seconds=300,
        remaining_seconds=120,
    )
    assert plan is not None and plan.wait_seconds == 120


def test_pause_plan_none_when_budget_spent() -> None:
    turn = _failed(result_text="usage limit reached")
    assert pause_plan(turn, poll_seconds=300, remaining_seconds=0) is None


# --- fallback chain ----------------------------------------------------------


def test_next_fallback_advances_through_chain() -> None:
    chain = ("claude", "codex", "gemini")
    assert next_fallback_tool(chain, "claude") == "codex"
    assert next_fallback_tool(chain, "codex") == "gemini"
    assert next_fallback_tool(chain, "gemini") is None


def test_next_fallback_unknown_current_returns_first_differing() -> None:
    assert next_fallback_tool(("claude", "codex"), "other") == "claude"


def test_next_fallback_empty_chain() -> None:
    assert next_fallback_tool((), "claude") is None


# --- fixture capture (G4 harness) ---------------------------------------------


def test_record_failure_fixture_round_trips(tmp_path: Path) -> None:
    turn = _failed(result_text="usage limit reached|1750000000", stderr="boom")
    path = record_failure_fixture(
        tmp_path / "failures", agent="coder", round_no=2, turn=turn
    )
    data = json.loads(path.read_text())
    assert path.name == "round_02_coder.json"
    assert data["classification"] == USAGE_LIMITED
    assert data["result_text"] == "usage limit reached|1750000000"
    assert data["stderr"] == "boom"
    assert data["exit_code"] == 1


def test_record_failure_fixture_never_overwrites(tmp_path: Path) -> None:
    # Repeated failures in the same round/agent each keep their wording.
    d = tmp_path / "failures"
    p1 = record_failure_fixture(
        d, agent="coder", round_no=1, turn=_failed(result_text="usage limit reached")
    )
    p2 = record_failure_fixture(
        d, agent="coder", round_no=1, turn=_failed(result_text="quota exceeded")
    )
    p3 = record_failure_fixture(
        d, agent="coder", round_no=1, turn=_failed(result_text="boom")
    )
    assert [p.name for p in (p1, p2, p3)] == [
        "round_01_coder.json",
        "round_01_coder_02.json",
        "round_01_coder_03.json",
    ]
    assert "usage limit reached" in p1.read_text()
    assert "quota exceeded" in p2.read_text()
