"""Tests for usage-limit classification + reaction policy (T5)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from lithos_loom.plugins.story_develop.engines import ClaudeEngine, CodexEngine
from lithos_loom.plugins.story_develop.limits import (
    AGENT_ERROR,
    USAGE_LIMITED,
    FailureClass,
    Reaction,
    classify_failure,
    failure_summary,
    next_fallback_tool,
    pause_plan,
    reaction_for,
    record_failure_fixture,
    reset_hint,
)
from lithos_loom.plugins.story_develop.turns import TurnResult

FIXTURES = Path(__file__).parent / "fixtures" / "agent_failures"


def _failed(
    *, result_text: str = "", stderr: str = "", exit_code: int = 1
) -> TurnResult:
    return TurnResult(
        exit_code=exit_code,
        succeeded=False,
        completed=False,
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
        "context window limit reached",  # context limit != usage limit
        "AssertionError: expected 3 findings, got 2",
    ],
)
def test_unrecognised_failures_are_agent_errors(text: str) -> None:
    # The safe default: never mis-pause on an ordinary crash.
    assert classify_failure(_failed(result_text=text)) == AGENT_ERROR


def test_timeout_is_its_own_class_even_with_limit_text() -> None:
    # Silence is not a limit signal (T5) — and not an infra retry either: the
    # turn ran to the wall, so its reaction stays the plain failure path.
    turn = _failed(result_text="usage limit reached", exit_code=124)
    assert classify_failure(turn) == FailureClass.TIMEOUT
    assert reaction_for(FailureClass.TIMEOUT).kind == "fail"


def test_codex_raw_limit_events_are_classified_from_raw() -> None:
    """G4 lands (#103): a real codex usage-limit is recognised from ``raw``.

    A real codex limit arrives as a JSONL ``turn.failed`` event, NOT an
    ``agent_message`` — :meth:`CodexEngine.parse_turn` stores it verbatim in
    ``raw["failure_events"]`` and leaves ``result_text`` empty. The classifier
    now searches the whole turn (result text + stderr + ``raw``), so the limit
    reaches the pause/switch path instead of failing the run as ``agent_error``.
    """
    turn = _fixture_turn("codex_usage_limit.json")
    assert turn.succeeded is False
    assert turn.result_text == "" and turn.stderr == ""
    assert turn.raw is not None and "failure_events" in turn.raw
    assert classify_failure(turn) == USAGE_LIMITED


def test_classify_rejects_successful_turn() -> None:
    ok = TurnResult(
        exit_code=0,
        succeeded=True,
        completed=True,
        session_id="s",
        result_text="",
        cost_usd=0.0,
        raw={},
        stderr="",
    )
    with pytest.raises(ValueError):
        classify_failure(ok)


# --- slice B: one classifier over the whole turn (5dbeb0c8) ------------------


def _fixture_turn(name: str) -> TurnResult:
    """Build the TurnResult a fixture describes.

    A fixture either carries the parsed fields (``result_text`` / ``stderr`` /
    ``raw`` — a captured ``failures/round_NN_<agent>.json`` record) or the raw
    ``stdout`` of the turn, which is fed through the named engine's parser —
    so the parser's retention of unparseable output is part of what the
    fixture pins.
    """
    data = json.loads((FIXTURES / name).read_text())
    engine = ClaudeEngine() if data["engine"] == "claude" else CodexEngine()
    if "stdout" in data:
        return engine.parse_turn(
            data["stdout"], exit_code=data["exit_code"], stderr=data["stderr"]
        )
    return TurnResult(
        exit_code=data["exit_code"],
        succeeded=False,
        completed=False,
        session_id="",
        result_text=data["result_text"],
        cost_usd=0.0,
        raw=data["raw"],
        stderr=data["stderr"],
    )


# Every fixture on disk must be listed here: an unlisted capture is a class
# nobody decided on. (The completeness assertion below enforces it.)
EXPECTED_CLASS = {
    "claude_oauth_expired_parsed.json": FailureClass.AUTH_FAILED,
    "claude_oauth_expired_raw_stdout.json": FailureClass.AUTH_FAILED,
    "claude_401_token_revoked.json": FailureClass.AUTH_FAILED,
    "claude_stream_disconnect.json": FailureClass.TRANSIENT_INFRA,
    "claude_api_overloaded_529.json": FailureClass.TRANSIENT_INFRA,
    "codex_stream_disconnect.json": FailureClass.TRANSIENT_INFRA,
    "codex_usage_limit.json": FailureClass.USAGE_LIMITED,
    "docker_exec_killed_137.json": FailureClass.OOM_OR_SPAWN,
    "docker_container_not_running.json": FailureClass.OOM_OR_SPAWN,
}


def test_every_fixture_has_a_decided_class() -> None:
    on_disk = {p.name for p in FIXTURES.glob("*.json")}
    assert on_disk == set(EXPECTED_CLASS)


@pytest.mark.parametrize("name", sorted(EXPECTED_CLASS))
def test_fixture_classifies(name: str) -> None:
    turn = _fixture_turn(name)
    assert turn.succeeded is False
    assert classify_failure(turn) == EXPECTED_CLASS[name]


def test_raw_stdout_shape_is_retained_not_dropped() -> None:
    # The #405 shape: unparseable stdout used to become raw=None / result_text=""
    # and the 401 was invisible. The parser now keeps it for the classifier.
    turn = _fixture_turn("claude_oauth_expired_raw_stdout.json")
    assert turn.completed is False and turn.raw is not None
    assert "OAuth session expired" in json.dumps(turn.raw)


@pytest.mark.parametrize(
    "text",
    [
        "Failed to authenticate: OAuth session expired and could not be refreshed",
        "API Error: 401 OAuth access token has been revoked",
        "authentication_failed: please run /login",
        "Invalid API key · Fix external API key",
        'API Error: 401 {"type":"error","error":{"type":"authentication_error"}}',
    ],
)
def test_auth_wordings(text: str) -> None:
    assert classify_failure(_failed(result_text=text)) == FailureClass.AUTH_FAILED


@pytest.mark.parametrize(
    "text",
    [
        "API Error: 500 internal server error",
        "rate limited, retrying",  # a 429 is transient, not a usage limit
        'API Error: 429 {"error":{"type":"rate_limit_error"}}',
        "Error: stream disconnected before completion",
        "idle timeout waiting for websocket message",
        "fetch failed: read ECONNRESET",
        "connect ETIMEDOUT 104.18.0.1:443",
        "getaddrinfo EAI_AGAIN api.anthropic.com",
        "API Error: 503 Service Unavailable",
        "API Error: 529 overloaded_error",
    ],
)
def test_transient_wordings(text: str) -> None:
    assert classify_failure(_failed(result_text=text)) == FailureClass.TRANSIENT_INFRA


@pytest.mark.parametrize(
    "stderr",
    [
        "Error response from daemon: container x is not running",
        "Error response from daemon: No such container: x",
        "OCI runtime exec failed: exec failed: unable to start container process",
        "fatal error: out of memory",
    ],
)
def test_spawn_and_memory_wordings(stderr: str) -> None:
    assert classify_failure(_failed(stderr=stderr)) == FailureClass.OOM_OR_SPAWN


def test_exit_137_is_oom_whatever_the_text_says() -> None:
    # The process was killed; whatever it printed before is stale.
    turn = _failed(result_text="usage limit reached", exit_code=137)
    assert classify_failure(turn) == FailureClass.OOM_OR_SPAWN


def test_usage_limit_outranks_transient_wording() -> None:
    # "rate limit" style text inside a real limit message must still pause,
    # never retry-with-backoff into the same wall.
    text = "You've hit your usage limit (rate limited). Upgrade to continue."
    assert classify_failure(_failed(result_text=text)) == USAGE_LIMITED


def test_auth_outranks_transient_wording() -> None:
    text = "stream disconnected: API Error: 401 OAuth access token has been revoked"
    assert classify_failure(_failed(result_text=text)) == FailureClass.AUTH_FAILED


def test_classes_are_strings_for_the_fixture_record() -> None:
    # record_failure_fixture json-dumps the class; the on-disk word is the enum
    # value, and the legacy constants are the same members (callers comparing
    # `classify_failure(t) != USAGE_LIMITED` keep working).
    assert FailureClass.AGENT_ERROR == "agent_error" == AGENT_ERROR
    assert FailureClass.USAGE_LIMITED == "usage_limited" == USAGE_LIMITED
    assert json.dumps(FailureClass.AUTH_FAILED) == '"auth_failed"'


# --- reaction table -----------------------------------------------------------


def test_reaction_table_is_total() -> None:
    for cls in FailureClass:
        assert isinstance(reaction_for(cls), Reaction)


def test_auth_failure_retries_once_then_escalates() -> None:
    # 2026-09-12: the host token was valid minutes after the container's
    # refresh failed and the next run proceeded — a plausible rotation race
    # on the shared credentials file. So: one re-read + retry, escalate on the
    # second hit (a genuinely revoked login fails identically until a human
    # re-authenticates).
    r = reaction_for(FailureClass.AUTH_FAILED)
    assert r.kind == "retry" and r.retries == 1 and r.escalate is True
    assert r.backoff_seconds == (20.0,) and r.resume is True


def test_transient_infra_retries_twice_with_backoff_then_escalates() -> None:
    r = reaction_for(FailureClass.TRANSIENT_INFRA)
    assert r.kind == "retry" and r.retries == 2 and r.escalate is True
    assert r.backoff_seconds == (30.0, 120.0) and r.resume is True


def test_oom_or_spawn_retries_once_then_escalates() -> None:
    r = reaction_for(FailureClass.OOM_OR_SPAWN)
    assert r.kind == "retry" and r.retries == 1 and r.escalate is True
    assert len(r.backoff_seconds) == 1


def test_usage_limit_pauses_and_plain_failures_fail() -> None:
    assert reaction_for(FailureClass.USAGE_LIMITED).kind == "pause"
    assert reaction_for(FailureClass.AGENT_ERROR).kind == "fail"
    assert reaction_for(FailureClass.TIMEOUT).kind == "fail"


def test_retry_reactions_have_one_backoff_per_retry() -> None:
    for cls in FailureClass:
        r = reaction_for(cls)
        if r.kind == "retry":
            assert len(r.backoff_seconds) == r.retries


def test_every_escalating_class_names_a_host_action() -> None:
    for cls in FailureClass:
        r = reaction_for(cls)
        if r.escalate:
            assert r.host_action and "complete the gate" in r.host_action


# --- failure summary ----------------------------------------------------------


def test_failure_summary_prefers_result_text_first_line() -> None:
    turn = _failed(result_text="first line\nsecond line", stderr="ignored")
    assert failure_summary(turn) == "first line"


def test_failure_summary_falls_back_to_stderr_then_raw_then_exit() -> None:
    assert failure_summary(_failed(stderr="  boom  \n")) == "boom"
    turn = _fixture_turn("codex_stream_disconnect.json")
    assert "ECONNRESET" in failure_summary(turn)
    assert failure_summary(_failed(exit_code=137)) == "exit 137"


def test_failure_summary_is_one_short_line() -> None:
    turn = _failed(result_text="x" * 1000)
    out = failure_summary(turn)
    assert "\n" not in out and len(out) <= 200


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
