"""Tests for the mechanism LLM-judge (#183).

The host-direct agent call (`_run_host_agent`) is stubbed, so the judge's
prompt-building + id-parsing are tested hermetically — no agent, no subprocess.
"""

from __future__ import annotations

import pytest

from lithos_loom.evals.review import judge as judge_mod
from lithos_loom.evals.review.judge import _parse_matched_ids, build_agent_judge

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
