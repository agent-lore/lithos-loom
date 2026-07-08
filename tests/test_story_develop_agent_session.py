"""Direct, Services-injected tests for the usage-limit pause loop (ARCH-1.S4).

``turn_with_limit_pauses`` used to be reachable only through a full ``develop()``
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
    PauseBudget,
    turn_with_limit_pauses,
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
    return TurnResult(
        exit_code=0 if succeeded else 1,
        succeeded=succeeded,
        session_id=session_id,
        result_text=result_text,
        cost_usd=cost,
        raw=None,
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
) -> tuple[TurnResult, bool, float]:
    return turn_with_limit_pauses(
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
    turn, interrupted, cost = _run(
        _config(tmp_path), services, _FakeEngine(True), budget=PauseBudget(600)
    )
    assert turn.succeeded and interrupted is False
    assert cost == pytest.approx(0.1)
    assert len(calls) == 1 and sleeps == []


def test_non_limit_failure_returns_without_pausing(tmp_path: Path) -> None:
    # A non-usage-limit failure is the existing failure path's business — return
    # immediately, no retry, no pause.
    services, calls, sleeps = _services(
        [_turn(succeeded=False, result_text="boom", cost=0.2)]
    )
    turn, interrupted, cost = _run(
        _config(tmp_path), services, _FakeEngine(True), budget=PauseBudget(600)
    )
    assert interrupted is False and turn.succeeded is False
    assert cost == pytest.approx(0.2)
    assert len(calls) == 1 and sleeps == []


def test_transcript_survived_retry_resumes_with_continuation(tmp_path: Path) -> None:
    services, calls, sleeps = _services(
        [
            _turn(succeeded=False, result_text=LIMIT, cost=0.1),
            _turn(succeeded=True, cost=0.2),
        ]
    )
    budget = PauseBudget(600)
    turn, interrupted, cost = _run(
        _config(tmp_path), services, _FakeEngine(transcript_exists=True), budget=budget
    )
    assert interrupted is False
    assert cost == pytest.approx(0.3)  # both attempts summed
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
    turn, interrupted, cost = _run(
        _config(tmp_path),
        services,
        _FakeEngine(transcript_exists=False),
        budget=PauseBudget(600),
    )
    assert interrupted is False and len(calls) == 2
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
    turn, interrupted, cost = _run(
        _config(tmp_path), services, _FakeEngine(True), budget=PauseBudget(0)
    )
    assert interrupted is True
    assert cost == pytest.approx(0.1)
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
