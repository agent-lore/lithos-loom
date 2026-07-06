"""Unit tests for the per-tool Engine adapter (ARCH-2.E1).

Pins the Engine API directly: identity/capabilities, the bare (:meth:`cli_argv`)
and docker-wrapped (:meth:`build_exec_argv`) argv builders, unified turn parsing
(the codex cases ported from the turns suite), auth/mount provisioning, and —
first-ever — the per-tool session-transcript layout probe.

The old delegating surfaces (``containers.build_exec_command``, ``turns.parse_*``,
``develop._session_transcript_exists``) stay green in their own suites, so the
behaviour is pinned twice: old call sites + this direct engine coverage.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lithos_loom.plugins.story_develop.config import DevelopConfig
from lithos_loom.plugins.story_develop.engines import (
    ClaudeEngine,
    CodexEngine,
    Engine,
    get_engine,
    is_supported,
    supported_tools,
)


@pytest.fixture
def config(tmp_git_repo: Path, tmp_path: Path) -> DevelopConfig:
    claude_dir = tmp_path / "claude"
    codex_dir = tmp_path / "codex"
    claude_dir.mkdir()
    codex_dir.mkdir()
    return DevelopConfig(
        repo=tmp_git_repo,
        description="x",
        work_dir=tmp_path / "work",
        claude_config_dir=claude_dir,
        codex_config_dir=codex_dir,
    )


# ── registry ──────────────────────────────────────────────────────────────


def test_supported_tools_and_membership() -> None:
    assert supported_tools() == ("claude", "codex")
    assert is_supported("claude") and is_supported("codex")
    assert not is_supported("opencode")


def test_get_engine_returns_the_matching_engine() -> None:
    assert isinstance(get_engine("claude"), ClaudeEngine)
    assert isinstance(get_engine("codex"), CodexEngine)
    # singletons — the registry hands back the same instance each call.
    assert get_engine("claude") is get_engine("claude")


def test_get_engine_rejects_unknown_tool_with_actionable_message() -> None:
    with pytest.raises(ValueError) as exc:
        get_engine("opencode")
    msg = str(exc.value)
    assert "opencode" in msg
    assert "'claude'" in msg and "'codex'" in msg  # names the supported set


# ── identity / capabilities (ADR 0002 + #94 facts, expressed) ──────────────


def test_claude_capabilities() -> None:
    e = ClaudeEngine()
    assert e.name == "claude"
    assert e.meters_cost_usd is True  # claude reports total_cost_usd
    assert e.mints_session_handle is False  # echoes the caller-supplied uuid
    assert e.supports_effort is True  # canonical --effort levels


def test_codex_capabilities() -> None:
    e = CodexEngine()
    assert e.name == "codex"
    assert e.meters_cost_usd is False  # tokens, not USD — the #102 boundary
    assert e.mints_session_handle is True  # thread_id from turn-1 thread.started
    assert e.supports_effort is False  # depth is model-driven


# ── cli_argv (bare tool argv) + build_exec_argv (docker exec wrapper) ───────


def test_claude_cli_argv_first_turn_uses_session_id() -> None:
    argv = ClaudeEngine().cli_argv(prompt="do it", session_id="sid-1")
    assert argv[0] == "claude"
    assert argv[argv.index("--session-id") + 1] == "sid-1"
    assert "-p" in argv and "--dangerously-skip-permissions" in argv
    assert argv[argv.index("--output-format") + 1] == "json"
    assert argv[-1] == "do it"  # prompt is a single trailing argv element


def test_claude_cli_argv_resume_uses_resume_flag() -> None:
    argv = ClaudeEngine().cli_argv(prompt="p", session_id="sid-1", resume=True)
    assert "--resume" in argv and "--session-id" not in argv
    assert argv[argv.index("--resume") + 1] == "sid-1"


def test_claude_cli_argv_model_and_effort() -> None:
    argv = ClaudeEngine().cli_argv(
        prompt="p", session_id="s", model="opus", effort="xhigh"
    )
    assert argv[argv.index("--model") + 1] == "opus"
    assert argv[argv.index("--effort") + 1] == "xhigh"


def test_claude_cli_argv_omits_model_and_effort_when_none() -> None:
    argv = ClaudeEngine().cli_argv(prompt="p", session_id="s")
    assert "--model" not in argv and "--effort" not in argv


def test_claude_cli_argv_session_id_none_omits_session_flags() -> None:
    # The bare host-side invocation (the eval judge, E5): no container, no
    # session — so neither --session-id nor --resume is emitted.
    argv = ClaudeEngine().cli_argv(prompt="p", session_id=None)
    assert "--session-id" not in argv and "--resume" not in argv
    assert argv[0] == "claude" and argv[-1] == "p"


def test_codex_cli_argv_first_turn_omits_supplied_handle() -> None:
    argv = CodexEngine().cli_argv(prompt="do it", session_id="unused-uuid")
    # `codex exec` (no `resume`); codex mints the thread_id itself on turn 1, so
    # the supplied session_id is NOT in the argv.
    assert argv[0] == "codex" and argv[1] == "exec"
    assert "resume" not in argv and "unused-uuid" not in argv
    assert "--json" in argv and "--dangerously-bypass-approvals-and-sandbox" in argv
    assert argv[-1] == "do it"


def test_codex_cli_argv_resume_passes_thread_id_positionally() -> None:
    argv = CodexEngine().cli_argv(prompt="p", session_id="thread-7", resume=True)
    idx = argv.index("resume")
    assert argv[idx - 1] == "exec"  # `codex exec resume <thread_id>`
    assert argv[idx + 1] == "thread-7"
    assert argv[-1] == "p"


def test_codex_cli_argv_model_flag_and_effort_ignored() -> None:
    argv = CodexEngine().cli_argv(prompt="p", session_id="s", model="o3", effort="high")
    assert argv[argv.index("-m") + 1] == "o3"
    assert "--effort" not in argv  # codex depth is model-driven


def test_codex_cli_argv_session_id_none_is_plain_exec() -> None:
    # A bare host-side invocation is never a resume — degrades to plain `exec`.
    argv = CodexEngine().cli_argv(prompt="p", session_id=None, resume=False)
    assert argv[:2] == ["codex", "exec"]
    assert "resume" not in argv


@pytest.mark.parametrize("tool", ["claude", "codex"])
def test_build_exec_argv_wraps_cli_argv_in_docker_exec(tool: str) -> None:
    engine = get_engine(tool)
    argv = engine.build_exec_argv(name="cont", prompt="p", session_id="s")
    assert argv[:5] == ["docker", "exec", "-w", "/workspace", "cont"]
    # everything after the wrapper is exactly the bare CLI argv.
    assert argv[5:] == engine.cli_argv(prompt="p", session_id="s")


def test_build_exec_argv_honours_custom_workdir() -> None:
    argv = ClaudeEngine().build_exec_argv(
        name="c", prompt="p", session_id="s", workdir="/elsewhere"
    )
    assert argv[argv.index("-w") + 1] == "/elsewhere"


# ── parse_turn — codex cases ported from the turns suite ───────────────────


def _codex_jsonl(*events: dict) -> str:
    return "\n".join(json.dumps(e) for e in events)


_CODEX_SUCCESS = _codex_jsonl(
    {"type": "thread.started", "thread_id": "0199a213-81c0-7800-8aa1-bbab2a035a53"},
    {"type": "turn.started"},
    {
        "type": "item.completed",
        "item": {"id": "item_3", "type": "agent_message", "text": "Done the work."},
    },
    {"type": "turn.completed", "usage": {"input_tokens": 24763, "output_tokens": 122}},
)


def test_codex_parse_first_turn_captures_thread_id_and_succeeds() -> None:
    r = CodexEngine().parse_turn(_CODEX_SUCCESS, exit_code=0, stderr="")
    assert r.succeeded is True
    assert r.session_id == "0199a213-81c0-7800-8aa1-bbab2a035a53"
    assert r.result_text == "Done the work."
    assert r.cost_usd == 0.0  # codex reports tokens, not USD
    assert r.raw == {"usage": {"input_tokens": 24763, "output_tokens": 122}}


def test_codex_parse_resume_keeps_handle_without_thread_started() -> None:
    stream = _codex_jsonl(
        {"type": "turn.started"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "ok"}},
        {"type": "turn.completed", "usage": {}},
    )
    r = CodexEngine().parse_turn(
        stream, exit_code=0, stderr="", session_id="t9", resume=True
    )
    assert r.succeeded is True and r.session_id == "t9"


def test_codex_parse_first_turn_without_thread_id_fails() -> None:
    stream = _codex_jsonl(
        {"type": "item.completed", "item": {"type": "agent_message", "text": "ok"}},
        {"type": "turn.completed", "usage": {}},
    )
    r = CodexEngine().parse_turn(stream, exit_code=0, stderr="", resume=False)
    assert r.succeeded is False and r.session_id == ""


def test_codex_parse_turn_failed_event_retained_verbatim_in_raw() -> None:
    failure = {
        "type": "turn.failed",
        "error": {"message": "you have hit your usage limit", "type": "rate_limit"},
    }
    stream = _codex_jsonl({"type": "thread.started", "thread_id": "t1"}, failure)
    r = CodexEngine().parse_turn(stream, exit_code=1, stderr="")
    assert r.succeeded is False
    assert r.raw == {"failure_events": [failure]}  # #103: verbatim, no interpretation


def test_codex_parse_empty_output_fails_safely() -> None:
    r = CodexEngine().parse_turn("", exit_code=0, stderr="")
    assert r.succeeded is False and r.raw is None and r.cost_usd == 0.0


def test_claude_parse_success() -> None:
    payload = json.dumps(
        {
            "type": "result",
            "is_error": False,
            "result": "OK",
            "session_id": "sid-9",
            "total_cost_usd": 0.19,
        }
    )
    r = ClaudeEngine().parse_turn(payload, exit_code=0, stderr="")
    assert r.succeeded is True and r.session_id == "sid-9" and r.cost_usd == 0.19


def test_claude_parse_ignores_session_id_and_resume_kwargs() -> None:
    # The unified signature accepts session_id/resume; claude parsing ignores them
    # (the handle comes from the payload, not the caller).
    payload = json.dumps(
        {"type": "result", "is_error": False, "result": "OK", "session_id": "from-json"}
    )
    r = ClaudeEngine().parse_turn(
        payload, exit_code=0, stderr="", session_id="ignored", resume=True
    )
    assert r.session_id == "from-json"


# ── provisioning: mounts / env / auth / skills ─────────────────────────────


def test_config_mount_and_env_var() -> None:
    claude = ClaudeEngine()
    assert claude.config_mount == "/claude_config"
    assert claude.config_env_var == "CLAUDE_CONFIG_DIR"
    codex = CodexEngine()
    assert codex.config_mount == "/codex_home"
    assert codex.config_env_var == "CODEX_HOME"


def test_auth_file_candidates() -> None:
    assert ClaudeEngine().auth_file_candidates == (".credentials.json",)
    assert CodexEngine().auth_file_candidates == ("auth.json",)


def test_auth_source_dir_picks_the_tools_config_dir(config: DevelopConfig) -> None:
    assert ClaudeEngine().auth_source_dir(config) == config.claude_config_dir
    assert CodexEngine().auth_source_dir(config) == config.codex_config_dir


def test_auth_files_returns_only_present_candidates(config: DevelopConfig) -> None:
    assert ClaudeEngine().auth_files(config) == []  # nothing on disk yet
    (config.claude_config_dir / ".credentials.json").write_text("{}")
    assert ClaudeEngine().auth_files(config) == [".credentials.json"]
    # codex reads its own dir + its own candidate name.
    assert CodexEngine().auth_files(config) == []
    (config.codex_config_dir / "auth.json").write_text("{}")
    assert CodexEngine().auth_files(config) == ["auth.json"]


def test_skills_dir_claude_present_and_absent(config: DevelopConfig) -> None:
    assert ClaudeEngine().skills_dir(config) is None  # no skills/ dir
    (config.claude_config_dir / "skills").mkdir()
    assert ClaudeEngine().skills_dir(config) == config.claude_config_dir / "skills"


def test_skills_dir_codex_is_always_none(config: DevelopConfig) -> None:
    # even if a skills dir exists, codex has no skill concept.
    (config.codex_config_dir / "skills").mkdir()
    assert CodexEngine().skills_dir(config) is None


# ── session-transcript layout — FIRST-EVER codex coverage ──────────────────


def test_claude_transcript_exists_matches_projects_layout(tmp_path: Path) -> None:
    # claude: projects/<cwd-hash>/<uuid>.jsonl under CLAUDE_CONFIG_DIR.
    sid = "11111111-2222-3333-4444-555555555555"
    proj = tmp_path / "projects" / "-work-branch"
    proj.mkdir(parents=True)
    (proj / f"{sid}.jsonl").write_text("{}")
    assert ClaudeEngine().session_transcript_exists(tmp_path, sid) is True


def test_claude_transcript_absent_when_no_projects_dir(tmp_path: Path) -> None:
    assert ClaudeEngine().session_transcript_exists(tmp_path, "sid") is False


def test_claude_transcript_absent_for_wrong_id_or_extension(tmp_path: Path) -> None:
    proj = tmp_path / "projects" / "p"
    proj.mkdir(parents=True)
    (proj / "other-uuid.jsonl").write_text("{}")
    (proj / "sid.json").write_text("{}")  # not .jsonl
    assert ClaudeEngine().session_transcript_exists(tmp_path, "sid") is False


def test_codex_transcript_exists_matches_dated_rollout_layout(tmp_path: Path) -> None:
    # codex: sessions/YYYY/MM/DD/rollout-…-<thread_id>.jsonl under CODEX_HOME.
    tid = "0199a213-81c0-7800-8aa1-bbab2a035a53"
    day = tmp_path / "sessions" / "2026" / "07" / "06"
    day.mkdir(parents=True)
    (day / f"rollout-2026-07-06T12-00-00-{tid}.jsonl").write_text("{}")
    assert CodexEngine().session_transcript_exists(tmp_path, tid) is True


def test_codex_transcript_absent_when_no_sessions_dir(tmp_path: Path) -> None:
    assert CodexEngine().session_transcript_exists(tmp_path, "tid") is False


def test_codex_transcript_absent_for_wrong_id_or_extension(tmp_path: Path) -> None:
    day = tmp_path / "sessions" / "2026" / "07" / "06"
    day.mkdir(parents=True)
    (day / "rollout-2026-07-06T12-00-00-other-thread.jsonl").write_text("{}")
    (day / "rollout-2026-07-06T12-00-00-tid.txt").write_text("{}")  # not .jsonl
    assert CodexEngine().session_transcript_exists(tmp_path, "tid") is False


def test_engines_satisfy_the_engine_protocol() -> None:
    # structural conformance is compile-time (pyright); assert at runtime too so a
    # dropped method is caught even if a caller stops annotating against Engine.
    engines_under_test: list[Engine] = [ClaudeEngine(), CodexEngine()]
    for e in engines_under_test:
        assert hasattr(e, "cli_argv") and hasattr(e, "parse_turn")
        assert hasattr(e, "session_transcript_exists")
