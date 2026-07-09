"""Tests for the mechanism LLM-judge (#183).

The `build_agent_judge` tests stub the host-direct agent call (`_run_host_agent`),
so the judge's prompt-building + id-parsing are tested hermetically — no agent, no
subprocess. `_run_host_agent` itself is covered directly (ARCH-2.E5): its argv +
result parsing route through the `Engine` adapter, with `subprocess.run` faked.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from lithos_loom.evals.review import judge as judge_mod
from lithos_loom.evals.review.judge import (
    _parse_matched_ids,
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


# --- _parse_matched_ids ------------------------------------------------------


def test_parses_the_matched_line() -> None:
    text = "Reasoning...\nf-002 describes it.\nMATCHED: f-002"
    assert _parse_matched_ids(text, {"f-001", "f-002"}) == ["f-002"]


def test_matched_none_is_empty() -> None:
    assert _parse_matched_ids("MATCHED: none", {"f-001"}) == []


def test_unknown_ids_are_dropped() -> None:
    assert _parse_matched_ids("MATCHED: f-001, f-999", {"f-001"}) == ["f-001"]


def test_multiple_ids_comma_separated() -> None:
    got = _parse_matched_ids("MATCHED: f-001, f-002", {"f-001", "f-002"})
    assert set(got) == {"f-001", "f-002"}


def test_fallback_scans_for_valid_ids_without_a_matched_line() -> None:
    # no MATCHED line — best-effort scan for valid ids mentioned in the text
    assert _parse_matched_ids("I think f-002 is the one", {"f-001", "f-002"}) == [
        "f-002"
    ]


def test_empty_text_matches_nothing() -> None:
    assert _parse_matched_ids("", {"f-001"}) == []


# --- build_agent_judge -------------------------------------------------------


def test_judge_prompt_carries_mechanism_and_findings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    def fake_run(tool, prompt, model, timeout):
        captured["tool"] = tool
        captured["prompt"] = prompt
        return "MATCHED: f-002"

    monkeypatch.setattr(judge_mod, "_run_host_agent", fake_run)
    judge = build_agent_judge(tool="claude")
    ids = judge("attach exits on approved before delivery", _FINDINGS)

    assert ids == ["f-002"]
    assert captured["tool"] == "claude"
    assert "attach exits on approved before delivery" in captured["prompt"]
    # the prompt lists the findings by id so the agent can answer with ids
    assert "f-001" in captured["prompt"] and "f-002" in captured["prompt"]
    assert "MATCHED:" in captured["prompt"]  # the requested output format


def test_judge_vetoes_when_agent_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(judge_mod, "_run_host_agent", lambda *a: "MATCHED: none")
    judge = build_agent_judge()
    assert judge("some mechanism", _FINDINGS) == []


def test_judge_short_circuits_on_no_findings(monkeypatch: pytest.MonkeyPatch) -> None:
    called = {"n": 0}

    def fake_run(*a):
        called["n"] += 1
        return "MATCHED: none"

    monkeypatch.setattr(judge_mod, "_run_host_agent", fake_run)
    judge = build_agent_judge()
    assert judge("mech", []) == []
    assert called["n"] == 0  # no agent call when there is nothing to judge


# --- _run_host_agent (the migrated Engine wiring, ARCH-2.E5) -----------------

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


def test_run_host_agent_claude_builds_engine_argv_and_parses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict = {}

    def fake_run(cmd, *args, **kwargs):
        calls["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout=_CLAUDE_SUCCESS, stderr="")

    monkeypatch.setattr(judge_mod.subprocess, "run", fake_run)
    out = _run_host_agent("claude", "judge this", model="opus", timeout=30)

    # The migration's whole point: the argv is the Engine's bare host-side argv,
    # not a hard-coded per-tool branch in the judge.
    assert calls["cmd"] == engines.get_engine("claude").cli_argv(
        prompt="judge this", model="opus"
    )
    # ...and the subprocess output is parsed via Engine.parse_turn.
    assert out == "OK"


def test_run_host_agent_codex_builds_engine_argv_and_parses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict = {}

    def fake_run(cmd, *args, **kwargs):
        calls["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout=_CODEX_SUCCESS, stderr="")

    monkeypatch.setattr(judge_mod.subprocess, "run", fake_run)
    out = _run_host_agent("codex", "judge this", model=None, timeout=30)

    assert calls["cmd"] == engines.get_engine("codex").cli_argv(
        prompt="judge this", model=None
    )
    assert out == "Done the work."


def test_run_host_agent_rejects_unsupported_tool() -> None:
    # Registry-derived validation — no subprocess is spawned for an unknown tool.
    with pytest.raises(ValueError, match="unsupported judge tool"):
        _run_host_agent("opencode", "p", model=None, timeout=1)


def test_run_host_agent_returns_empty_when_cli_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*args, **kwargs):
        raise FileNotFoundError("claude: command not found")

    monkeypatch.setattr(judge_mod.subprocess, "run", boom)
    # A missing/timing-out agent CLI is treated as "no match", never a crash.
    assert _run_host_agent("claude", "p", model=None, timeout=1) == ""
