"""Unit tests for coder-turn result parsing."""

from __future__ import annotations

import json

from lithos_loom.plugins.story_develop.turns import (
    parse_claude_result,
    parse_codex_result,
)

_SUCCESS = json.dumps(
    {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "OK",
        "session_id": "11111111-2222-3333-4444-555555555555",
        "total_cost_usd": 0.1928,
    }
)


def test_parse_success() -> None:
    r = parse_claude_result(_SUCCESS, exit_code=0, stderr="")
    assert r.succeeded is True
    assert r.session_id == "11111111-2222-3333-4444-555555555555"
    assert r.result_text == "OK"
    assert r.cost_usd == 0.1928


def test_parse_is_error_fails_even_with_zero_exit() -> None:
    payload = json.dumps({"type": "result", "is_error": True, "result": "limit"})
    r = parse_claude_result(payload, exit_code=0, stderr="")
    assert r.succeeded is False


def test_parse_nonzero_exit_fails() -> None:
    r = parse_claude_result(_SUCCESS, exit_code=1, stderr="boom")
    assert r.succeeded is False
    assert r.stderr == "boom"


def test_parse_garbage_output_fails_safely() -> None:
    r = parse_claude_result("not json", exit_code=0, stderr="")
    assert r.succeeded is False
    assert r.raw is None
    assert r.cost_usd == 0.0


def test_parse_empty_output_fails_safely() -> None:
    r = parse_claude_result("", exit_code=0, stderr="")
    assert r.succeeded is False
    assert r.raw is None


def test_parse_requires_session_id_for_success() -> None:
    payload = json.dumps({"type": "result", "is_error": False, "result": "OK"})
    r = parse_claude_result(payload, exit_code=0, stderr="")
    assert r.succeeded is False  # no session_id -> not a usable success
    assert r.session_id == ""


def test_parse_normalises_null_fields_not_to_literal_none() -> None:
    payload = json.dumps(
        {"type": "result", "is_error": False, "result": None, "session_id": "s1"}
    )
    r = parse_claude_result(payload, exit_code=0, stderr="")
    assert r.result_text == ""  # not "None"
    assert r.succeeded is True


# ── codex JSONL parsing (#94) ──────────────────────────────────────────


def _codex_jsonl(*events: dict) -> str:
    return "\n".join(json.dumps(e) for e in events)


_CODEX_SUCCESS = _codex_jsonl(
    {"type": "thread.started", "thread_id": "0199a213-81c0-7800-8aa1-bbab2a035a53"},
    {"type": "turn.started"},
    {
        "type": "item.completed",
        "item": {"id": "item_3", "type": "agent_message", "text": "Done the work."},
    },
    {
        "type": "turn.completed",
        "usage": {"input_tokens": 24763, "output_tokens": 122},
    },
)


def test_parse_codex_first_turn_captures_thread_id_and_succeeds() -> None:
    r = parse_codex_result(_CODEX_SUCCESS, exit_code=0, stderr="")
    assert r.succeeded is True
    assert r.session_id == "0199a213-81c0-7800-8aa1-bbab2a035a53"
    assert r.result_text == "Done the work."
    assert r.cost_usd == 0.0  # codex reports tokens, not USD
    assert r.raw == {"usage": {"input_tokens": 24763, "output_tokens": 122}}


def test_parse_codex_last_agent_message_wins() -> None:
    stream = _codex_jsonl(
        {"type": "thread.started", "thread_id": "t1"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "first"}},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "final"}},
        {"type": "turn.completed", "usage": {}},
    )
    assert parse_codex_result(stream, exit_code=0, stderr="").result_text == "final"


def test_parse_codex_resume_keeps_handle_without_thread_started() -> None:
    # A resume stream may not re-announce thread.started; the handle we resumed
    # must carry through so success isn't lost.
    stream = _codex_jsonl(
        {"type": "turn.started"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "ok"}},
        {"type": "turn.completed", "usage": {}},
    )
    r = parse_codex_result(stream, exit_code=0, stderr="", session_id="t9", resume=True)
    assert r.succeeded is True
    assert r.session_id == "t9"


def test_parse_codex_first_turn_without_thread_id_fails() -> None:
    # No thread.started on a FIRST turn -> no usable handle -> not a success.
    stream = _codex_jsonl(
        {"type": "item.completed", "item": {"type": "agent_message", "text": "ok"}},
        {"type": "turn.completed", "usage": {}},
    )
    r = parse_codex_result(stream, exit_code=0, stderr="", resume=False)
    assert r.succeeded is False
    assert r.session_id == ""


def test_parse_codex_turn_failed_event_is_failure() -> None:
    stream = _codex_jsonl(
        {"type": "thread.started", "thread_id": "t1"},
        {"type": "turn.failed", "error": {"message": "boom"}},
    )
    assert parse_codex_result(stream, exit_code=1, stderr="").succeeded is False


def test_parse_codex_error_event_is_failure_even_on_zero_exit() -> None:
    stream = _codex_jsonl(
        {"type": "thread.started", "thread_id": "t1"},
        {"type": "error", "message": "usage limit"},
        {"type": "turn.completed", "usage": {}},
    )
    assert parse_codex_result(stream, exit_code=0, stderr="").succeeded is False


def test_parse_codex_nonzero_exit_fails() -> None:
    r = parse_codex_result(_CODEX_SUCCESS, exit_code=1, stderr="x")
    assert r.succeeded is False


def test_parse_codex_no_turn_completed_fails() -> None:
    stream = _codex_jsonl(
        {"type": "thread.started", "thread_id": "t1"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "ok"}},
    )
    assert parse_codex_result(stream, exit_code=0, stderr="").succeeded is False


def test_parse_codex_skips_unparseable_lines() -> None:
    stream = "not json\n" + _CODEX_SUCCESS + "\nalso not json"
    r = parse_codex_result(stream, exit_code=0, stderr="")
    assert r.succeeded is True
    assert r.session_id == "0199a213-81c0-7800-8aa1-bbab2a035a53"


def test_parse_codex_empty_output_fails_safely() -> None:
    r = parse_codex_result("", exit_code=0, stderr="")
    assert r.succeeded is False
    assert r.raw is None
    assert r.cost_usd == 0.0
