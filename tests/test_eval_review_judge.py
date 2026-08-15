"""Tests for the mechanism LLM-judge (#183, hardened by #307).

The `build_agent_judge` tests stub the host-direct agent call (`_run_host_agent`),
so the judge's prompt-building + verdict-parsing are tested hermetically — no
agent, no subprocess. `_run_host_agent` itself is covered directly (ARCH-2.E5):
its argv + result parsing route through the `Engine` adapter, with
`subprocess.run` faked.

The #307 theme throughout: an empty match used to mean three different things —
a genuine veto, a reply nobody could read, and a call that never happened. Each
now has its own status, because only the first is a measurement.
"""

from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest

from lithos_loom.evals.review import judge as judge_mod
from lithos_loom.evals.review.judge import (
    JudgeUnavailable,
    _parse_verdict,
    _run_host_agent,
    build_agent_judge,
)
from lithos_loom.plugins.story_develop import engines

_FINDINGS = [
    {
        "reviewer": "correctness",
        "severity": "critical",
        "files": ["cli/develop.py:546"],
        "rationale": "summary omits the PR url",
        "finding_id": "f-001",
    },
    {
        "reviewer": "correctness",
        "severity": "critical",
        "files": ["cli/develop.py:385"],
        "rationale": "attach exits on approved before delivery",
        "finding_id": "f-002",
    },
]

_CLAUDE_SUCCESS = json.dumps(
    {"type": "result", "is_error": False, "result": "OK", "session_id": "sid-9"}
)
_CODEX_SUCCESS = "\n".join(
    json.dumps(e)
    for e in (
        {"type": "thread.started", "thread_id": "t-1"},
        {"type": "turn.started"},
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "Done the work."},
        },
        {"type": "turn.completed", "usage": {}},
    )
)


def _turn(text: str, anomaly: str = "") -> SimpleNamespace:
    """A stand-in for `_run_host_agent`'s return, structural not nominal.

    Duck-typed on purpose: the stub only needs `.text` / `.anomaly`, so the turn
    type stays private to `judge.py` and these tests spend no
    `tests_private_imports` budget on it.
    """
    return SimpleNamespace(text=text, anomaly=anomaly)


def _claude_reply(text: str, *, is_error: bool = False, session: str = "sid-9") -> str:
    return json.dumps(
        {
            "type": "result",
            "is_error": is_error,
            "result": text,
            "session_id": session,
        }
    )


# --- _parse_verdict ----------------------------------------------------------


def test_parses_the_matched_line() -> None:
    text = "Reasoning...\nf-002 describes it.\nMATCHED: f-002"
    verdict = _parse_verdict(text, {"f-001", "f-002"})
    assert verdict.matched_ids == ("f-002",)
    assert verdict.status == "ok"


def test_matched_none_is_a_veto() -> None:
    verdict = _parse_verdict("MATCHED: none", {"f-001"})
    assert verdict.matched_ids == ()
    assert verdict.status == "ok"  # a veto is an ANSWER, not a failure


def test_unknown_ids_are_dropped() -> None:
    assert _parse_verdict("MATCHED: f-001, f-999", {"f-001"}).matched_ids == ("f-001",)


def test_multiple_ids_comma_separated() -> None:
    got = _parse_verdict("MATCHED: f-001, f-002", {"f-001", "f-002"})
    assert set(got.matched_ids) == {"f-001", "f-002"}


def test_reply_without_a_matched_line_is_unparsed() -> None:
    """#307 defect 3: prose must never be scanned for finding ids.

    The prompt says "Reason briefly, then conclude", so reasoning precedes the
    verdict. The old whole-reply fallback scored this emphatic *rejection* as a
    match — manufacturing a catch out of a refusal.
    """
    verdict = _parse_verdict("f-002 does NOT describe this mechanism.", {"f-002"})
    assert verdict.matched_ids == ()
    assert verdict.status == "unparsed"
    assert verdict.reply == "f-002 does NOT describe this mechanism."


def test_matched_none_of_prefix_is_a_veto_not_a_full_match() -> None:
    """`== "none"` was an exact compare, so a chatty veto matched every id in it."""
    verdict = _parse_verdict("MATCHED: none of f-001, f-002", {"f-001", "f-002"})
    assert verdict.matched_ids == ()
    assert verdict.status == "ok"


def test_bare_matched_line_with_no_ids_is_unparsed() -> None:
    assert _parse_verdict("MATCHED:", {"f-001"}).status == "unparsed"


def test_empty_reply_is_unparsed() -> None:
    verdict = _parse_verdict("", {"f-001"})
    assert verdict.matched_ids == ()
    assert verdict.status == "unparsed"


def test_the_last_matched_line_wins() -> None:
    text = "MATCHED: f-001\nOn reflection, no.\nMATCHED: none"
    assert _parse_verdict(text, {"f-001"}).matched_ids == ()


def test_verdict_keeps_the_raw_reply_for_audit() -> None:
    text = "It doubles the margin.\nMATCHED: f-001"
    assert _parse_verdict(text, {"f-001"}).reply == text


# --- build_agent_judge -------------------------------------------------------


def test_judge_prompt_carries_mechanism_and_findings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    def fake_run(tool, prompt, model, timeout):
        captured["tool"] = tool
        captured["prompt"] = prompt
        return _turn("MATCHED: f-002")

    monkeypatch.setattr(judge_mod, "_run_host_agent", fake_run)
    judge = build_agent_judge(tool="claude")
    verdict = judge("attach exits on approved before delivery", _FINDINGS)

    assert verdict.matched_ids == ("f-002",)
    assert captured["tool"] == "claude"
    assert "attach exits on approved before delivery" in captured["prompt"]
    # the prompt lists the findings by id so the agent can answer with ids
    assert "f-001" in captured["prompt"] and "f-002" in captured["prompt"]
    assert "MATCHED:" in captured["prompt"]  # the requested output format


def test_judge_vetoes_when_agent_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        judge_mod,
        "_run_host_agent",
        lambda *a: _turn("MATCHED: none"),
    )
    verdict = build_agent_judge()("some mechanism", _FINDINGS)
    assert verdict.matched_ids == ()
    assert verdict.status == "ok"


def test_judge_short_circuits_on_no_findings(monkeypatch: pytest.MonkeyPatch) -> None:
    called = {"n": 0}

    def fake_run(*a):
        called["n"] += 1
        return _turn("MATCHED: none")

    monkeypatch.setattr(judge_mod, "_run_host_agent", fake_run)
    verdict = build_agent_judge()("mech", [])

    assert verdict.matched_ids == ()
    assert verdict.status == "ok"  # nothing to judge is an answer, not a failure
    assert called["n"] == 0  # no agent call when there is nothing to judge


def test_judge_reports_failed_when_the_agent_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#307 defect 1: an unreachable judge is not a veto."""

    def boom(*a):
        raise JudgeUnavailable("TimeoutExpired: judge timed out after 300s")

    monkeypatch.setattr(judge_mod, "_run_host_agent", boom)
    verdict = build_agent_judge()("mech", _FINDINGS)

    assert verdict.status == "failed"
    assert verdict.matched_ids == ()
    assert "TimeoutExpired" in verdict.detail


def test_judge_retries_once_before_giving_up(monkeypatch: pytest.MonkeyPatch) -> None:
    """The reviewer sample being scored was already paid for."""
    calls = {"n": 0}

    def flaky(*a):
        calls["n"] += 1
        if calls["n"] == 1:
            raise JudgeUnavailable("TimeoutExpired: first attempt")
        return _turn("MATCHED: f-001")

    monkeypatch.setattr(judge_mod, "_run_host_agent", flaky)
    verdict = build_agent_judge()("mech", _FINDINGS)

    assert calls["n"] == 2
    assert verdict.status == "ok"
    assert verdict.matched_ids == ("f-001",)


def test_judge_gives_up_after_the_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def always_boom(*a):
        calls["n"] += 1
        raise JudgeUnavailable("FileNotFoundError: claude not found")

    monkeypatch.setattr(judge_mod, "_run_host_agent", always_boom)
    verdict = build_agent_judge()("mech", _FINDINGS)

    assert calls["n"] == 2  # one attempt, one retry — not an unbounded loop
    assert verdict.status == "failed"


def test_judge_reports_unparsed_when_the_reply_has_no_verdict_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        judge_mod,
        "_run_host_agent",
        lambda *a: _turn("I am unsure about f-001."),
    )
    verdict = build_agent_judge()("mech", _FINDINGS)

    assert verdict.status == "unparsed"
    assert verdict.matched_ids == ()


def test_unparsed_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retrying an unreadable answer would bias the sample toward parseable ones."""
    calls = {"n": 0}

    def fake_run(*a):
        calls["n"] += 1
        return _turn("no verdict here")

    monkeypatch.setattr(judge_mod, "_run_host_agent", fake_run)
    assert build_agent_judge()("mech", _FINDINGS).status == "unparsed"
    assert calls["n"] == 1


# --- the `succeeded` question (#307 defect 2) --------------------------------


def test_an_unsuccessful_turn_with_a_readable_verdict_is_still_scored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`succeeded` folds in a SESSION HANDLE, which a one-shot judge never needs.

    Both engines require a session id/thread id for `succeeded` so a later resume
    turn can re-attach. Letting that gate the eval's scorer would mean a CLI that
    stopped echoing a handle turned every case into zero valid samples. The model
    answered; the answer counts — the anomaly is recorded instead.
    """
    monkeypatch.setattr(
        judge_mod.subprocess,
        "run",
        lambda cmd, *a, **k: subprocess.CompletedProcess(
            cmd, 0, stdout=_claude_reply("MATCHED: f-001", session=""), stderr=""
        ),
    )
    verdict = build_agent_judge(tool="claude", model="opus")("mech", _FINDINGS)

    assert verdict.status == "ok"
    assert verdict.matched_ids == ("f-001",)
    assert verdict.detail  # the anomaly is recorded, not silently dropped


def test_an_unsuccessful_turn_without_a_verdict_is_failed_not_unparsed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """So the audit record says WHY there is no verdict."""
    monkeypatch.setattr(
        judge_mod.subprocess,
        "run",
        lambda cmd, *a, **k: subprocess.CompletedProcess(
            cmd,
            1,
            stdout=_claude_reply("usage limit reached", is_error=True),
            stderr="",
        ),
    )
    verdict = build_agent_judge(tool="claude", model="opus")("mech", _FINDINGS)

    assert verdict.status == "failed"
    assert verdict.matched_ids == ()


# --- _run_host_agent (the migrated Engine wiring, ARCH-2.E5) -----------------


def test_run_host_agent_claude_builds_engine_argv_and_parses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict = {}

    def fake_run(cmd, *args, **kwargs):
        calls["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout=_CLAUDE_SUCCESS, stderr="")

    monkeypatch.setattr(judge_mod.subprocess, "run", fake_run)
    turn = _run_host_agent("claude", "judge this", model="opus", timeout=30)

    # The migration's whole point: the argv is the Engine's bare host-side argv,
    # not a hard-coded per-tool branch in the judge.
    assert calls["cmd"] == engines.get_engine("claude").cli_argv(
        prompt="judge this", model="opus"
    )
    # ...and the subprocess output is parsed via Engine.parse_turn.
    assert turn.text == "OK"
    assert turn.anomaly == ""


def test_run_host_agent_codex_builds_engine_argv_and_parses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict = {}

    def fake_run(cmd, *args, **kwargs):
        calls["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout=_CODEX_SUCCESS, stderr="")

    monkeypatch.setattr(judge_mod.subprocess, "run", fake_run)
    turn = _run_host_agent("codex", "judge this", model=None, timeout=30)

    assert calls["cmd"] == engines.get_engine("codex").cli_argv(
        prompt="judge this", model=None
    )
    assert turn.text == "Done the work."


def test_run_host_agent_records_an_unsuccessful_turn_as_an_anomaly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        judge_mod.subprocess,
        "run",
        lambda cmd, *a, **k: subprocess.CompletedProcess(
            cmd, 0, stdout=_claude_reply("MATCHED: none", session=""), stderr="warn"
        ),
    )
    turn = _run_host_agent("claude", "p", model=None, timeout=1)

    assert turn.text == "MATCHED: none"
    assert turn.anomaly  # non-empty: the turn did not meet the engine's bar


def test_run_host_agent_rejects_unsupported_tool() -> None:
    # Registry-derived validation — no subprocess is spawned for an unknown tool.
    with pytest.raises(ValueError, match="unsupported judge tool"):
        _run_host_agent("opencode", "p", model=None, timeout=1)


def test_run_host_agent_raises_when_the_cli_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*args, **kwargs):
        raise FileNotFoundError("claude: command not found")

    monkeypatch.setattr(judge_mod.subprocess, "run", boom)
    with pytest.raises(JudgeUnavailable, match="FileNotFoundError"):
        _run_host_agent("claude", "p", model=None, timeout=1)


def test_run_host_agent_raises_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=["claude"], timeout=1)

    monkeypatch.setattr(judge_mod.subprocess, "run", boom)
    with pytest.raises(JudgeUnavailable, match="TimeoutExpired"):
        _run_host_agent("claude", "p", model=None, timeout=1)


def test_run_host_agent_raises_on_an_os_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-executable agent binary is the same class of infra failure."""

    def boom(*args, **kwargs):
        raise PermissionError("[Errno 13] Permission denied: 'claude'")

    monkeypatch.setattr(judge_mod.subprocess, "run", boom)
    with pytest.raises(JudgeUnavailable, match="PermissionError"):
        _run_host_agent("claude", "p", model=None, timeout=1)


# --- a FAILED turn's text is never a verdict (#307 review round 1) -----------


def _codex_stream(*events: dict) -> str:
    return "\n".join(json.dumps(e) for e in events)


def test_a_hard_failed_claude_turn_with_a_readable_verdict_is_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Partial or stale output must never be scored as an answer.

    `succeeded` folds together two very different things: "this turn worked" and
    "it minted a resumable handle". Treating the whole of it as a benign anomaly
    let a crashed turn whose retained text happened to end in `MATCHED: f-001`
    manufacture a catch — the very defect this change set out to close.
    """
    monkeypatch.setattr(
        judge_mod.subprocess,
        "run",
        lambda cmd, *a, **k: subprocess.CompletedProcess(
            cmd,
            1,
            stdout=_claude_reply("analysis…\nMATCHED: f-001", is_error=True),
            stderr="fatal",
        ),
    )
    verdict = build_agent_judge(tool="claude", model="opus")("mech", _FINDINGS)

    assert verdict.status == "failed"
    assert verdict.matched_ids == ()
    assert verdict.reply  # the text is still retained, for the audit record


def test_a_codex_turn_that_fails_after_answering_is_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The codex stream keeps the last agent message even when the turn fails."""
    stream = _codex_stream(
        {"type": "thread.started", "thread_id": "t-1"},
        {"type": "turn.started"},
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "MATCHED: f-001"},
        },
        {"type": "turn.failed", "error": "boom"},
    )
    monkeypatch.setattr(
        judge_mod.subprocess,
        "run",
        lambda cmd, *a, **k: subprocess.CompletedProcess(
            cmd, 0, stdout=stream, stderr=""
        ),
    )
    verdict = build_agent_judge(tool="codex", model="gpt")("mech", _FINDINGS)

    assert verdict.status == "failed"
    assert verdict.matched_ids == ()


def test_a_codex_answer_without_a_thread_id_is_still_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The narrow exception: completed cleanly, just not resumable."""
    stream = _codex_stream(
        {"type": "turn.started"},
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "MATCHED: f-001"},
        },
        {"type": "turn.completed", "usage": {}},
    )
    monkeypatch.setattr(
        judge_mod.subprocess,
        "run",
        lambda cmd, *a, **k: subprocess.CompletedProcess(
            cmd, 0, stdout=stream, stderr=""
        ),
    )
    verdict = build_agent_judge(tool="codex", model="gpt")("mech", _FINDINGS)

    assert verdict.status == "ok"
    assert verdict.matched_ids == ("f-001",)
    assert "resumable" in verdict.detail  # recorded, not acted on


def test_a_hard_failed_turn_is_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def flaky(cmd, *a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            return subprocess.CompletedProcess(
                cmd,
                1,
                stdout=_claude_reply("MATCHED: f-001", is_error=True),
                stderr="x",
            )
        return subprocess.CompletedProcess(
            cmd, 0, stdout=_claude_reply("MATCHED: f-002"), stderr=""
        )

    monkeypatch.setattr(judge_mod.subprocess, "run", flaky)
    verdict = build_agent_judge(tool="claude", model="opus")("mech", _FINDINGS)

    assert calls["n"] == 2
    assert verdict.status == "ok"
    assert verdict.matched_ids == ("f-002",)


def test_run_host_agent_raises_on_a_turn_that_did_not_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        judge_mod.subprocess,
        "run",
        lambda cmd, *a, **k: subprocess.CompletedProcess(
            cmd, 1, stdout=_claude_reply("partial", is_error=True), stderr="fatal"
        ),
    )
    with pytest.raises(JudgeUnavailable, match="did not complete") as exc:
        _run_host_agent("claude", "p", model=None, timeout=1)
    assert exc.value.text == "partial"  # retained for the audit record
