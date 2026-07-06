"""Per-tool Engine adapter — one home for the claude/codex differences.

The claude-vs-codex conditionals used to be scattered across four modules: the
``docker exec`` argv (``containers.build_exec_command``), the turn-result parsing
(``turns.parse_claude_result`` / ``parse_codex_result``), the transcript-existence
probe (``develop._session_transcript_exists``), and the container mount / auth
constants (``config``). This module concentrates them behind one interface so a
new tool is added in one place and a caller stops branching on a ``tool`` string.

Each old public name stays as a **one-line delegate** to the matching Engine
until its last caller migrates (E2/E3/E5) — this slice (ARCH-2.E1) moves the
*logic*, not the call sites, so behaviour is pinned twice (old tests + new).

The capability flags (``meters_cost_usd`` / ``mints_session_handle`` /
``supports_effort``) **express** decisions ADR 0002 + #94 already made; they are
not re-decided here. ``meters_cost_usd = False`` for codex surfaces the #102
boundary (codex reports tokens, not USD) as a fact a caller can read, instead of
a silent ``cost_usd = 0.0``.

Imports stay one-directional: ``engines`` imports ``config``; ``config`` must NOT
import ``engines`` (the import-linter contract enforces it). ``turns`` /
``containers`` / ``develop`` import ``engines``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .config import (
    CLAUDE_AUTH_FILES,
    CLAUDE_CONFIG_MOUNT,
    CODEX_AUTH_FILES,
    CODEX_CONFIG_MOUNT,
    WORKSPACE_MOUNT,
    DevelopConfig,
)

_TIMEOUT_EXIT = 124  # conventional timeout exit; we set it ourselves on timeout


@dataclass(frozen=True)
class TurnResult:
    """Outcome of one agent turn (tool-independent).

    Lives here (not in ``turns``) because the per-tool parsers that build it now
    live here; ``turns`` re-exports it so ``from .turns import TurnResult`` keeps
    working.
    """

    exit_code: int
    succeeded: bool
    session_id: str
    result_text: str
    cost_usd: float
    raw: dict | None
    stderr: str

    @property
    def timed_out(self) -> bool:
        return self.exit_code == _TIMEOUT_EXIT


class Engine(Protocol):
    """What a coder/reviewer tool must provide to run under story-develop.

    Structural (satisfied by :class:`ClaudeEngine` / :class:`CodexEngine`); used
    to type the registry + :func:`get_engine`.
    """

    # identity / capabilities
    name: str  # registry key, state.json value, default_models key
    meters_cost_usd: bool  # claude True; codex reports tokens, not USD (#102)
    mints_session_handle: bool  # codex mints thread_id turn-1; claude echoes uuid
    supports_effort: bool  # codex depth is model-driven — no effort knob

    # container provisioning
    config_mount: str  # in-container config/transcript mountpoint
    config_env_var: str  # env var pointing the tool at config_mount
    auth_file_candidates: tuple[str, ...]  # auth filenames to bind-mount if present

    def auth_source_dir(self, config: DevelopConfig) -> Path: ...
    def auth_files(self, config: DevelopConfig) -> list[str]: ...
    def skills_dir(self, config: DevelopConfig) -> Path | None: ...

    # turn execution
    def cli_argv(
        self,
        *,
        prompt: str,
        session_id: str | None = None,
        resume: bool = False,
        model: str | None = None,
        effort: str | None = None,
    ) -> list[str]: ...
    def build_exec_argv(
        self,
        *,
        name: str,
        prompt: str,
        session_id: str,
        resume: bool = False,
        workdir: str = WORKSPACE_MOUNT,
        model: str | None = None,
        effort: str | None = None,
    ) -> list[str]: ...
    def parse_turn(
        self,
        stdout: str,
        *,
        exit_code: int,
        stderr: str,
        session_id: str = "",
        resume: bool = False,
    ) -> TurnResult: ...

    # session durability
    def session_transcript_exists(self, config_dir: Path, session_id: str) -> bool: ...


class _BaseEngine:
    """Shared implementation for the parts that are engine-independent.

    ``build_exec_argv`` (the ``docker exec`` wrapper) and ``auth_files`` (filter
    the candidates that exist) are identical across tools — they are template
    methods over the per-tool ``cli_argv`` / ``auth_source_dir`` / attributes.
    """

    # set / implemented by subclasses (declared for the shared methods below):
    auth_file_candidates: tuple[str, ...]

    def auth_source_dir(self, config: DevelopConfig) -> Path:
        raise NotImplementedError

    def cli_argv(
        self,
        *,
        prompt: str,
        session_id: str | None = None,
        resume: bool = False,
        model: str | None = None,
        effort: str | None = None,
    ) -> list[str]:
        raise NotImplementedError

    def auth_files(self, config: DevelopConfig) -> list[str]:
        """The subset of :attr:`auth_file_candidates` present in the operator dir.

        Bind-mounted RW (token refresh) — never the whole config dir. Absorbs the
        former ``containers.resolve_auth_files``.
        """
        source = self.auth_source_dir(config)
        return [f for f in self.auth_file_candidates if (source / f).is_file()]

    def build_exec_argv(
        self,
        *,
        name: str,
        prompt: str,
        session_id: str,
        resume: bool = False,
        workdir: str = WORKSPACE_MOUNT,
        model: str | None = None,
        effort: str | None = None,
    ) -> list[str]:
        """The ``docker exec`` argv for one turn = the wrapper + the bare CLI argv.

        ``build_exec_argv`` supplies the container; :meth:`cli_argv` is the tool
        invocation, reusable host-side (no docker) by passing ``session_id=None``
        (the eval judge does this in E5).
        """
        return [
            "docker",
            "exec",
            "-w",
            workdir,
            name,
            *self.cli_argv(
                prompt=prompt,
                session_id=session_id,
                resume=resume,
                model=model,
                effort=effort,
            ),
        ]


class ClaudeEngine(_BaseEngine):
    name = "claude"
    meters_cost_usd = True
    mints_session_handle = False  # echoes the caller-supplied uuid (ADR 0002)
    supports_effort = True  # canonical --effort levels (low…max)

    config_mount = CLAUDE_CONFIG_MOUNT
    config_env_var = "CLAUDE_CONFIG_DIR"
    auth_file_candidates: tuple[str, ...] = CLAUDE_AUTH_FILES

    def auth_source_dir(self, config: DevelopConfig) -> Path:
        return config.claude_config_dir

    def skills_dir(self, config: DevelopConfig) -> Path | None:
        return config.operator_skills_dir  # operator ~/.claude/skills, if present

    def cli_argv(
        self,
        *,
        prompt: str,
        session_id: str | None = None,
        resume: bool = False,
        model: str | None = None,
        effort: str | None = None,
    ) -> list[str]:
        argv = ["claude"]
        # session_id=None → omit session flags (bare host-side invocation, E5).
        if session_id is not None:
            argv += ["--resume", session_id] if resume else ["--session-id", session_id]
        if model:
            argv += ["--model", model]
        if effort:
            argv += ["--effort", effort]
        argv += [
            "-p",
            "--dangerously-skip-permissions",
            "--output-format",
            "json",
            prompt,
        ]
        return argv

    def parse_turn(
        self,
        stdout: str,
        *,
        exit_code: int,
        stderr: str,
        session_id: str = "",
        resume: bool = False,
    ) -> TurnResult:
        """Parse ``claude --output-format json`` stdout (session_id/resume unused).

        The payload is a single JSON object (``type: "result"``) carrying
        ``is_error``, ``result``, ``session_id`` and ``total_cost_usd``. A
        non-zero exit *or* ``is_error: true`` *or* unparseable output is a failure.
        """
        raw: dict | None = None
        try:
            parsed = json.loads(stdout) if stdout.strip() else None
            if isinstance(parsed, dict):
                raw = parsed
        except json.JSONDecodeError:
            raw = None

        is_error = bool(raw.get("is_error")) if raw else True
        # ``or ""`` normalises an explicit JSON ``null`` to "" (not "None").
        parsed_session = str(raw.get("session_id") or "") if raw else ""
        result_text = str(raw.get("result") or "") if raw else ""
        cost_usd = float(raw.get("total_cost_usd") or 0.0) if raw else 0.0
        # A non-empty session_id is required for success so later resume turns
        # (T3) always have a handle to resume.
        succeeded = (
            exit_code == 0 and raw is not None and not is_error and bool(parsed_session)
        )

        return TurnResult(
            exit_code=exit_code,
            succeeded=succeeded,
            session_id=parsed_session,
            result_text=result_text,
            cost_usd=cost_usd,
            raw=raw,
            stderr=stderr,
        )

    def session_transcript_exists(self, config_dir: Path, session_id: str) -> bool:
        projects = config_dir / "projects"
        if not projects.is_dir():
            return False
        return any(projects.glob(f"*/{session_id}.jsonl"))


class CodexEngine(_BaseEngine):
    name = "codex"
    meters_cost_usd = False  # reports tokens, not USD — the #102 boundary
    mints_session_handle = True  # thread_id from turn-1 thread.started
    supports_effort = False  # depth is model-driven — no effort knob

    config_mount = CODEX_CONFIG_MOUNT
    config_env_var = "CODEX_HOME"
    auth_file_candidates: tuple[str, ...] = CODEX_AUTH_FILES

    def auth_source_dir(self, config: DevelopConfig) -> Path:
        return config.codex_config_dir

    def skills_dir(self, config: DevelopConfig) -> Path | None:
        return None  # codex has no skill concept (honours the worktree AGENTS.md)

    def cli_argv(
        self,
        *,
        prompt: str,
        session_id: str | None = None,
        resume: bool = False,
        model: str | None = None,
        effort: str | None = None,
    ) -> list[str]:
        # Verified against codex-cli 0.139.0:
        #   first:  codex exec [OPTIONS] [PROMPT]
        #   resume: codex exec resume [OPTIONS] [SESSION_ID] [PROMPT]
        # The thread_id is minted on turn 1 (thread.started) and passed
        # positionally to resume; the working dir is set by `docker exec -w`, so
        # the -C/--cd flag `resume` lacks is not needed. `effort` is ignored
        # (codex depth is model-driven). A bare host-side invocation
        # (session_id=None) is never a resume, so it degrades to plain `exec`.
        if resume and session_id is not None:
            subcommand = ["exec", "resume", session_id]
        else:
            subcommand = ["exec"]
        argv = [
            "codex",
            *subcommand,
            "--json",
            "--dangerously-bypass-approvals-and-sandbox",
        ]
        if model:
            argv += ["-m", model]
        argv += [prompt]
        return argv

    def parse_turn(
        self,
        stdout: str,
        *,
        exit_code: int,
        stderr: str,
        session_id: str = "",
        resume: bool = False,
    ) -> TurnResult:
        """Parse ``codex exec --json`` JSONL stdout into a TurnResult (#94).

        The stream is one JSON object per line. We read:

        * ``{"type": "thread.started", "thread_id": ...}`` → the session handle
          (codex *mints* it on turn 1; we capture it for ``codex exec resume``);
        * the last ``{"type": "item.completed", "item": {"type": "agent_message",
          "text": ...}}`` → the final message text;
        * ``{"type": "turn.completed", "usage": {...}}`` → success signal + token
          usage (stashed in ``raw``);
        * ``{"type": "turn.failed" | "error", ...}`` → failure; the event is
          retained **verbatim** in ``raw["failure_events"]`` (#103 Part A). The
          exact codex limit signal is not yet known, so we capture the events
          without interpreting them.

        The returned ``session_id`` is the captured ``thread_id``, or — on a
        **resume** turn where the stream may not re-announce ``thread.started`` —
        the inbound *session_id*. Success requires a zero exit, a
        ``turn.completed``, no failure event, AND a usable handle. ``cost_usd`` is
        ``0.0``: codex reports tokens, not USD (:attr:`meters_cost_usd` False; the
        cost-measure design is #102). Token usage is preserved in ``raw``.
        """
        thread_id = ""
        result_text = ""
        usage: dict | None = None
        saw_completed = False
        failure_events: list[dict] = []

        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            etype = event.get("type")
            if etype == "thread.started":
                thread_id = str(event.get("thread_id") or "")
            elif etype == "item.completed":
                item = event.get("item")
                if isinstance(item, dict) and item.get("type") == "agent_message":
                    result_text = str(item.get("text") or "")
            elif etype == "turn.completed":
                saw_completed = True
                u = event.get("usage")
                if isinstance(u, dict):
                    usage = u
            elif etype in ("turn.failed", "error"):
                failure_events.append(event)

        # On resume, keep the handle we resumed even if the stream didn't re-emit
        # thread.started; on the first turn the handle MUST be captured fresh.
        handle = thread_id or (session_id if resume else "")
        succeeded = (
            exit_code == 0 and saw_completed and not failure_events and bool(handle)
        )
        raw_parts: dict = {}
        if usage is not None:
            raw_parts["usage"] = usage
        if failure_events:
            raw_parts["failure_events"] = failure_events
        raw = raw_parts or None

        return TurnResult(
            exit_code=exit_code,
            succeeded=succeeded,
            session_id=handle,
            result_text=result_text,
            cost_usd=0.0,
            raw=raw,
            stderr=stderr,
        )

    def session_transcript_exists(self, config_dir: Path, session_id: str) -> bool:
        # codex writes sessions/YYYY/MM/DD/rollout-…-<thread_id>.jsonl under CODEX_HOME
        sessions = config_dir / "sessions"
        if not sessions.is_dir():
            return False
        return any(sessions.glob(f"**/*{session_id}*.jsonl"))


ENGINES: dict[str, Engine] = {"claude": ClaudeEngine(), "codex": CodexEngine()}


def get_engine(tool: str) -> Engine:
    """The :class:`Engine` for *tool*, or ``ValueError`` naming the supported set.

    The message shape matches the former ``build_exec_command`` raise so operator-
    facing errors are unchanged.
    """
    try:
        return ENGINES[tool]
    except KeyError:
        expected = " or ".join(repr(t) for t in ENGINES)
        raise ValueError(f"unsupported tool: {tool!r} (expected {expected})") from None


def is_supported(tool: str) -> bool:
    """Whether the container/exec layer can run *tool* (claude + codex, #94)."""
    return tool in ENGINES


def supported_tools() -> tuple[str, ...]:
    """The registered tool names, in registry order."""
    return tuple(ENGINES)
