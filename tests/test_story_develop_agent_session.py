"""Direct, Services-injected tests for the usage-limit pause loop (ARCH-1.S4).

``turn_with_reactions`` used to be reachable only through a full ``develop()``
run; the :class:`Services` seam lets us drive its subtle rebind / budget /
resume-vs-fresh reaction directly with fakes. Real ``limits`` classification +
``pause_plan`` are exercised (only ``record_failure_fixture`` is stubbed to keep
the loop off disk), so these pin the orchestration, not a re-mock of the policy.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from lithos_loom.plugins.story_develop import agent_session, engines
from lithos_loom.plugins.story_develop.agent_session import (
    _CONTINUATION_PROMPT as CONTINUATION,
)
from lithos_loom.plugins.story_develop.agent_session import (
    INFRA_CONTINUATION_PROMPT as INFRA_CONTINUATION,
)
from lithos_loom.plugins.story_develop.agent_session import (
    PauseBudget,
    TurnAttempt,
    turn_with_reactions,
)
from lithos_loom.plugins.story_develop.config import DevelopConfig
from lithos_loom.plugins.story_develop.rounds import Services
from lithos_loom.plugins.story_develop.turns import TurnResult

# A wording classify_failure maps to USAGE_LIMITED, with no parseable reset epoch
# so pause_plan stays poll-based (predictable, budget-capped).
LIMIT = "You've hit your usage limit. Upgrade to continue."


def _turn(
    *, succeeded: bool, session_id: str = "", cost: float = 0.0, result_text: str = ""
) -> TurnResult:
    # A failed turn carries the claude CLI's `is_error` payload, so its
    # result_text is the CLI's error text (an infra channel), as in production.
    return TurnResult(
        exit_code=0 if succeeded else 1,
        succeeded=succeeded,
        completed=succeeded,
        session_id=session_id,
        result_text=result_text,
        cost_usd=cost,
        raw={"is_error": not succeeded},
        stderr="",
    )


class _FakeEngine:
    """The loop only calls session_transcript_exists — record what it's asked."""

    def __init__(self, transcript_exists: bool) -> None:
        self._exists = transcript_exists
        self.checked_session_ids: list[str] = []

    def session_transcript_exists(self, config_dir: Path, session_id: str) -> bool:
        self.checked_session_ids.append(session_id)
        return self._exists


def _services(turns: list[TurnResult]) -> tuple[Services, list[dict], list[float]]:
    queue = list(turns)
    calls: list[dict] = []
    sleeps: list[float] = []

    def fake_run_turn(**kw: object) -> TurnResult:
        calls.append(kw)
        return queue.pop(0)

    services = Services(
        run_turn=fake_run_turn,
        sleep=lambda seconds: sleeps.append(seconds),
        start_container=lambda cmd: "cid",
        stop_container=lambda name: None,
        run_check_set=lambda *a, **k: None,
    )
    return services, calls, sleeps


def _config(tmp_path: Path) -> DevelopConfig:
    return DevelopConfig(repo=tmp_path, description="x", work_dir=tmp_path / "w")


@pytest.fixture(autouse=True)
def recorded_fixtures(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, int]]:
    """Stub record_failure_fixture off disk; return the (agent, round_no) it saw."""
    recorded: list[tuple[str, int]] = []
    monkeypatch.setattr(
        agent_session.limits,
        "record_failure_fixture",
        lambda failures_dir, *, agent, round_no, turn: recorded.append(
            (agent, round_no)
        ),
    )
    return recorded


def _run(
    config: DevelopConfig,
    services: Services,
    engine: _FakeEngine,
    *,
    budget: PauseBudget,
    session_id: str = "sess-1",
) -> TurnAttempt:
    return turn_with_reactions(
        config,
        budget,
        services=services,
        agent="coder",
        container="c",
        config_dir=config.coder_config_dir,
        prompt="do it",
        session_id=session_id,
        resume=False,
        round_no=1,
        timeout=100,
        engine=cast(engines.Engine, engine),
    )


def test_succeeds_on_first_turn(tmp_path: Path) -> None:
    services, calls, sleeps = _services([_turn(succeeded=True, cost=0.1)])
    att = _run(_config(tmp_path), services, _FakeEngine(True), budget=PauseBudget(600))
    assert att.turn.succeeded and att.interrupted is False
    assert att.cost == pytest.approx(0.1)
    assert len(calls) == 1 and sleeps == []


def test_non_limit_failure_returns_without_pausing(tmp_path: Path) -> None:
    # A non-usage-limit failure is the existing failure path's business — return
    # immediately, no retry, no pause.
    services, calls, sleeps = _services(
        [_turn(succeeded=False, result_text="boom", cost=0.2)]
    )
    att = _run(_config(tmp_path), services, _FakeEngine(True), budget=PauseBudget(600))
    assert att.interrupted is False and att.turn.succeeded is False
    assert att.cost == pytest.approx(0.2)
    assert len(calls) == 1 and sleeps == []


def test_transcript_survived_retry_resumes_with_continuation(tmp_path: Path) -> None:
    services, calls, sleeps = _services(
        [
            _turn(succeeded=False, result_text=LIMIT, cost=0.1),
            _turn(succeeded=True, cost=0.2),
        ]
    )
    budget = PauseBudget(600)
    att = _run(
        _config(tmp_path), services, _FakeEngine(transcript_exists=True), budget=budget
    )
    assert att.interrupted is False
    assert att.cost == pytest.approx(0.3)  # both attempts summed
    assert len(calls) == 2
    # the retry resumed the SAME session with the continuation prompt
    assert calls[1]["prompt"] == CONTINUATION
    assert calls[1]["resume"] is True
    # a pause happened: sleep once, budget decremented by exactly that
    assert len(sleeps) == 1 and sleeps[0] > 0
    assert budget.remaining == pytest.approx(600 - sleeps[0])


def test_transcript_gone_reissues_original_prompt_fresh(tmp_path: Path) -> None:
    services, calls, sleeps = _services(
        [
            _turn(succeeded=False, result_text=LIMIT, cost=0.1),
            _turn(succeeded=True, cost=0.1),
        ]
    )
    att = _run(
        _config(tmp_path),
        services,
        _FakeEngine(transcript_exists=False),
        budget=PauseBudget(600),
    )
    assert att.interrupted is False and len(calls) == 2
    # no surviving transcript -> re-issue the ORIGINAL prompt, fresh (not resume)
    assert calls[1]["prompt"] == "do it"
    assert calls[1]["resume"] is False


def test_minted_handle_rebinds_the_resumed_session(tmp_path: Path) -> None:
    # codex mints a thread_id on turn 1; the transcript check + the retry must use
    # the MINTED handle, not the stale pre-mint uuid the caller supplied.
    services, calls, sleeps = _services(
        [
            _turn(succeeded=False, session_id="thread-minted", result_text=LIMIT),
            _turn(succeeded=True, session_id="thread-minted"),
        ]
    )
    engine = _FakeEngine(transcript_exists=True)
    _run(
        _config(tmp_path),
        services,
        engine,
        budget=PauseBudget(600),
        session_id="pre-mint-uuid",
    )
    assert engine.checked_session_ids == ["thread-minted"]  # not "pre-mint-uuid"
    assert calls[1]["session_id"] == "thread-minted"  # retry uses the minted id


def test_budget_exhausted_checkpoints_as_interrupted(tmp_path: Path) -> None:
    # Usage-limited with no pause budget left -> checkpoint (interrupted=True),
    # NOT an agent failure, and no pause is attempted.
    services, calls, sleeps = _services(
        [_turn(succeeded=False, result_text=LIMIT, cost=0.1)]
    )
    att = _run(_config(tmp_path), services, _FakeEngine(True), budget=PauseBudget(0))
    assert att.interrupted is True
    assert att.cost == pytest.approx(0.1)
    assert len(calls) == 1 and sleeps == []


def test_every_failed_turn_is_recorded_as_a_fixture(
    tmp_path: Path, recorded_fixtures: list[tuple[str, int]]
) -> None:
    services, calls, sleeps = _services(
        [
            _turn(succeeded=False, result_text=LIMIT),
            _turn(succeeded=True),
        ]
    )
    _run(_config(tmp_path), services, _FakeEngine(True), budget=PauseBudget(600))
    assert recorded_fixtures == [("coder", 1)]  # exactly the one failed attempt


# --- slice B: infra reactions (retry with backoff, then escalate) ------------

AUTH = "Failed to authenticate: OAuth session expired and could not be refreshed"
DISCONNECT = "Error: stream disconnected before completion"


def test_auth_failure_retries_once_after_backoff_resuming_the_session(
    tmp_path: Path,
) -> None:
    services, calls, sleeps = _services(
        [
            _turn(succeeded=False, result_text=AUTH, cost=0.0),
            _turn(succeeded=True, cost=0.3),
        ]
    )
    att = _run(_config(tmp_path), services, _FakeEngine(True), budget=PauseBudget(600))
    assert att.turn.succeeded and att.escalation is None
    assert att.cost == pytest.approx(0.3)
    assert sleeps == [20.0]
    assert len(calls) == 2
    assert calls[1]["resume"] is True
    assert calls[1]["prompt"] == INFRA_CONTINUATION
    assert "infrastructure" in INFRA_CONTINUATION
    assert "usage limit" not in INFRA_CONTINUATION


def test_auth_failure_twice_escalates_with_the_host_action(tmp_path: Path) -> None:
    services, calls, sleeps = _services(
        [
            _turn(succeeded=False, result_text=AUTH, cost=0.01),
            _turn(succeeded=False, result_text=AUTH, cost=0.01),
        ]
    )
    budget = PauseBudget(600)
    att = _run(_config(tmp_path), services, _FakeEngine(True), budget=budget)
    assert att.turn.succeeded is False and att.interrupted is False
    assert att.escalation is not None
    assert "coder" in att.escalation and "auth_failed" in att.escalation
    assert "2 attempts" in att.escalation
    assert "OAuth session expired" in att.escalation
    # the host action rides beside the line, never inside it — the gate caps
    # the summary at 200 chars and must not truncate the action away
    assert "complete the gate" not in att.escalation
    assert "complete the gate" in att.host_action
    assert len(f"round 9: {att.escalation}") <= 200
    assert sleeps == [20.0] and len(calls) == 2  # no third attempt
    assert budget.remaining == 600  # infra backoff never spends the pause budget
    assert att.cost == pytest.approx(0.02)


def test_transient_infra_retries_twice_with_backoff_then_escalates(
    tmp_path: Path,
) -> None:
    services, calls, sleeps = _services(
        [_turn(succeeded=False, result_text=DISCONNECT) for _ in range(3)]
    )
    att = _run(_config(tmp_path), services, _FakeEngine(True), budget=PauseBudget(600))
    assert sleeps == [30.0, 120.0] and len(calls) == 3
    assert att.escalation is not None and "transient_infra" in att.escalation
    assert "3 attempts" in att.escalation


def test_transient_infra_recovers_on_the_second_attempt(tmp_path: Path) -> None:
    services, calls, sleeps = _services(
        [
            _turn(succeeded=False, result_text=DISCONNECT),
            _turn(succeeded=True, session_id="s"),
        ]
    )
    att = _run(_config(tmp_path), services, _FakeEngine(True), budget=PauseBudget(600))
    assert att.turn.succeeded and att.escalation is None
    assert sleeps == [30.0] and len(calls) == 2


def test_infra_retry_reissues_the_original_prompt_when_the_transcript_is_gone(
    tmp_path: Path,
) -> None:
    services, calls, sleeps = _services(
        [_turn(succeeded=False, result_text=DISCONNECT), _turn(succeeded=True)]
    )
    _run(_config(tmp_path), services, _FakeEngine(False), budget=PauseBudget(600))
    assert calls[1]["prompt"] == "do it" and calls[1]["resume"] is False


def test_killed_process_retries_once(tmp_path: Path) -> None:
    killed = TurnResult(
        exit_code=137,
        succeeded=False,
        completed=False,
        session_id="",
        result_text="",
        cost_usd=0.0,
        raw=None,
        stderr="",
    )
    services, calls, sleeps = _services([killed, _turn(succeeded=True)])
    att = _run(_config(tmp_path), services, _FakeEngine(True), budget=PauseBudget(600))
    assert att.turn.succeeded and sleeps == [10.0] and len(calls) == 2


def test_plain_agent_error_never_retries_or_escalates(tmp_path: Path) -> None:
    services, calls, sleeps = _services(
        [_turn(succeeded=False, result_text="AssertionError: nope", cost=0.2)]
    )
    att = _run(_config(tmp_path), services, _FakeEngine(True), budget=PauseBudget(600))
    assert att.escalation is None and att.interrupted is False
    assert len(calls) == 1 and sleeps == []


def test_retry_counts_are_per_class_and_every_attempt_is_recorded(
    tmp_path: Path, recorded_fixtures: list[tuple[str, int]]
) -> None:
    # A disconnect (retry #1 of 2) followed by an auth failure (retry #1 of 1)
    # then success: each class spends its own budget, every failed attempt
    # lands as a fixture, and the classification of each is what was seen.
    services, calls, sleeps = _services(
        [
            _turn(succeeded=False, result_text=DISCONNECT),
            _turn(succeeded=False, result_text=AUTH),
            _turn(succeeded=True),
        ]
    )
    att = _run(_config(tmp_path), services, _FakeEngine(True), budget=PauseBudget(600))
    assert att.turn.succeeded and att.escalation is None
    assert sleeps == [30.0, 20.0]
    assert recorded_fixtures == [("coder", 1), ("coder", 1)]
    assert att.host_action == ""


def test_usage_limit_after_an_infra_retry_still_pauses(tmp_path: Path) -> None:
    # The two reactions compose: an infra retry that then hits a usage limit
    # takes the T5 pause path, and the pause is what the budget pays for.
    services, calls, sleeps = _services(
        [
            _turn(succeeded=False, result_text=DISCONNECT),
            _turn(succeeded=False, result_text=LIMIT),
            _turn(succeeded=True),
        ]
    )
    budget = PauseBudget(600)
    att = _run(_config(tmp_path), services, _FakeEngine(True), budget=budget)
    assert att.turn.succeeded and att.interrupted is False
    assert sleeps[0] == 30.0 and len(sleeps) == 2
    assert budget.remaining == pytest.approx(600 - sleeps[1])


def test_attempt_carries_the_rebound_session_not_the_last_turns(tmp_path: Path) -> None:
    # Codex mints its thread_id on turn 1; a FRESH retry (no transcript) that
    # dies before `thread.started` returns "". The run's handle is the minted
    # one, which only the wrapper saw — it must come back on the attempt.
    services, calls, sleeps = _services(
        [
            _turn(succeeded=False, session_id="thread-minted", result_text=DISCONNECT),
            _turn(succeeded=False, session_id="", result_text=DISCONNECT),
            _turn(succeeded=False, session_id="", result_text=DISCONNECT),
        ]
    )
    att = _run(
        _config(tmp_path),
        services,
        _FakeEngine(False),
        budget=PauseBudget(600),
        session_id="pre-mint-uuid",
    )
    assert att.escalation is not None
    assert att.session_id == "thread-minted"
    assert att.turn.session_id == ""  # the last turn alone would lose it


def test_successful_attempt_reports_its_session(tmp_path: Path) -> None:
    services, _, _ = _services([_turn(succeeded=True, session_id="thread-minted")])
    att = _run(
        _config(tmp_path),
        services,
        _FakeEngine(True),
        budget=PauseBudget(600),
        session_id="pre-mint-uuid",
    )
    assert att.session_id == "thread-minted"
