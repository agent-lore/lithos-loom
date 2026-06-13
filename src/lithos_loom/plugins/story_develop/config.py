"""Resolved configuration + paths for a single ``story-develop`` run.

:class:`DevelopConfig` carries the coder, the reviewer panel, per-reviewer
severity thresholds, and usage-limit fallback chains — see
``docs/prd/archive/story-develop.md`` and SPECIFICATION.md §5.5.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass, field
from pathlib import Path

# A reviewer name becomes a Docker container name, a host dir, and a handoff
# filename, so it must be a safe slug (lowercase alphanumerics + hyphens,
# starting alphanumeric). This rejects spaces ("code quality") and path
# separators ("security/appsec") before they create invalid names / nested dirs.
_REVIEWER_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")


def is_valid_reviewer_name(name: str) -> bool:
    """True if *name* is a safe slug for container / path / filename use."""
    return bool(_REVIEWER_NAME_RE.fullmatch(name))


# Image + container constants (ralph-sandbox; see ADR 0002 / feasibility gate).
DEFAULT_CODER_TOOL = "claude"
DEFAULT_REVIEWER_TOOL = "claude"
DEFAULT_REVIEWER_NAME = "code-quality"
DEFAULT_BLOCK_THRESHOLD = "major"  # findings below this don't block (see handoff.py)
DEFAULT_MAX_ROUNDS = 5  # T3 loop bound; stall/dispute/cost guards arrive with T7
DEFAULT_TEST_TIMEOUT = 900  # seconds for one test-gate container run (T4)
DEFAULT_MAX_PAUSE_MINUTES = 120  # T5: total usage-limit pause budget per run
DEFAULT_PAUSE_POLL_MINUTES = 5  # T5: retry cadence when the reset time is unknown
DEFAULT_IMAGE = "ralph-sandbox:latest"
WORKSPACE_MOUNT = "/workspace"
CLAUDE_CONFIG_MOUNT = "/claude_config"
# The single auth file bind-mounted from the operator's real config (RW, so the
# OAuth token refresh propagates) — never the whole ~/.claude, and NOT
# ``.claude.json`` (that is mutable user state, not auth; mounting the real one
# RW would let the container pollute the operator's live config). See the PRD
# "Run-state & session durability" section.
CLAUDE_AUTH_FILES = (".credentials.json",)
HANDOFF_DIRNAME = ".handoff"


def _short_run_id() -> str:
    """8 hex chars; unique enough to namespace a run's tmux/containers/state."""
    return secrets.token_hex(4)


@dataclass(frozen=True)
class ReviewerSpec:
    """One named reviewer: its persona, strictness, and tooling (T6).

    ``block_threshold`` is per-reviewer — security typically blocks at
    ``minor`` while code-quality blocks at ``major`` (PRD decision #7).
    ``system_prompt`` is an optional focus brief injected into the reviewer's
    prompts. ``fallback_chain`` lists alternate tools tried when this
    reviewer's tool is usage-limited (T5).

    ``model`` / ``thinking`` are per-reviewer (#93): a strong reviewer can run
    a more capable model + bigger thinking budget than a lenient one. ``None``
    means "inherit the agent CLI's default" — see :class:`DevelopConfig` for
    why we do not hard-pin a model string.
    """

    name: str
    tool: str = DEFAULT_REVIEWER_TOOL
    block_threshold: str = DEFAULT_BLOCK_THRESHOLD
    system_prompt: str | None = None
    fallback_chain: tuple[str, ...] = ()
    model: str | None = None
    thinking: int | None = None


_VALID_THRESHOLDS = ("critical", "major", "minor")

_REVIEWER_ENTRY_KEYS = {
    "name",
    "tool",
    "block_threshold",
    "system_prompt",
    "fallback_chain",
    "model",
    "thinking",
}


def parse_model(value: object, *, where: str) -> str | None:
    """Validate a ``model`` value: a non-empty string, or ``None``.

    Shared by the reviewer-entry parser and the daemon-mode coder lookup so
    both surfaces reject the same garbage (empty / non-string) identically.
    Raises :class:`ValueError`.
    """
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{where}: model must be a non-empty string (got {value!r})")
    return value


def parse_thinking(value: object, *, where: str) -> int | None:
    """Validate a ``thinking`` token budget: a positive int, or ``None``.

    ``bool`` is rejected explicitly (it is an ``int`` subclass, so ``True``
    would otherwise sneak through as ``1``). Raises :class:`ValueError`.
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(
            f"{where}: thinking must be a positive integer (got {value!r})"
        )
    return value


def parse_reviewer_entry(entry: object, *, where: str) -> ReviewerSpec:
    """Validate one reviewer mapping into a :class:`ReviewerSpec`.

    Shared by the ``--develop-config`` TOML loader and the daemon-mode
    project-context metadata loader (T10) so both surfaces enforce the
    identical schema. *where* labels the entry in error messages
    (e.g. ``"config.toml: reviewers[2]"``). Raises :class:`ValueError`.
    """
    if not isinstance(entry, dict):
        raise ValueError(f"{where} is not a table/object")
    unknown = set(entry) - _REVIEWER_ENTRY_KEYS
    if unknown:
        raise ValueError(f"{where} has unknown keys {sorted(unknown)}")
    name = entry.get("name", "")
    if not isinstance(name, str) or not is_valid_reviewer_name(name):
        raise ValueError(
            f"{where}: name {name!r} must be a lowercase alphanumeric-and-hyphens slug"
        )
    threshold = entry.get("block_threshold", DEFAULT_BLOCK_THRESHOLD)
    if threshold not in _VALID_THRESHOLDS:
        raise ValueError(
            f"{where}: block_threshold must be one of "
            f"{_VALID_THRESHOLDS} (got {threshold!r})"
        )
    chain = entry.get("fallback_chain", [])
    if not isinstance(chain, list) or not all(isinstance(t, str) for t in chain):
        raise ValueError(f"{where}: fallback_chain must be a list of strings")
    system_prompt = entry.get("system_prompt")
    if system_prompt is not None and not isinstance(system_prompt, str):
        raise ValueError(f"{where}: system_prompt must be a string")
    tool = entry.get("tool", DEFAULT_REVIEWER_TOOL)
    if not isinstance(tool, str):
        raise ValueError(f"{where}: tool must be a string")
    model = parse_model(entry.get("model"), where=where)
    thinking = parse_thinking(entry.get("thinking"), where=where)
    return ReviewerSpec(
        name=name,
        tool=tool,
        block_threshold=threshold,
        system_prompt=system_prompt,
        fallback_chain=tuple(chain),
        model=model,
        thinking=thinking,
    )


def load_develop_config(path: Path) -> tuple[ReviewerSpec, ...]:
    """Parse a ``--develop-config`` TOML file into reviewer specs.

    Schema::

        [[reviewers]]
        name = "code-quality"          # required, safe slug
        block_threshold = "major"      # optional
        tool = "claude"                # optional
        system_prompt = "Focus on..."  # optional
        fallback_chain = ["codex"]     # optional

    Raises :class:`ValueError` with an operator-actionable message on any
    schema problem — never half-loads.
    """
    import tomllib

    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"cannot read develop config {path}: {exc}") from exc

    raw = data.get("reviewers")
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"{path}: expected at least one [[reviewers]] table")
    specs: list[ReviewerSpec] = []
    seen: set[str] = set()
    for i, entry in enumerate(raw, start=1):
        spec = parse_reviewer_entry(entry, where=f"{path}: reviewers[{i}]")
        if spec.name in seen:
            raise ValueError(f"{path}: duplicate reviewer name {spec.name!r}")
        seen.add(spec.name)
        specs.append(spec)
    return tuple(specs)


@dataclass(frozen=True)
class DevelopConfig:
    """Everything ``develop()`` needs for one run.

    Paths under ``work_dir`` are derived lazily so the dataclass stays a plain
    value object: ``run_dir``/``coder_config_dir``/``worktree_parent``.
    """

    repo: Path
    description: str
    work_dir: Path
    coder: str = DEFAULT_CODER_TOOL
    # Coder model + thinking budget (#93). ``None`` = inherit the agent CLI's
    # default. We deliberately do NOT hard-pin a model string here: a pin
    # chosen today goes stale and couples the plugin to a model's lifecycle
    # (an upgrade would need a code release). Reproducibility is instead served
    # by letting the operator pin via project metadata / CLI and by recording
    # the resolved choice with the run. Per-reviewer model/thinking live on
    # ``ReviewerSpec``; this pair is the coder's.
    coder_model: str | None = None
    coder_thinking: int | None = None
    image: str = DEFAULT_IMAGE
    base_branch: str = "main"
    # Single-reviewer convenience fields (the T2-era surface; still the
    # default path). T6: `reviewers` holds full multi-reviewer specs and,
    # when non-empty, takes precedence — see `effective_reviewers`.
    reviewer: str = DEFAULT_REVIEWER_NAME
    reviewer_tool: str = DEFAULT_REVIEWER_TOOL
    block_threshold: str = DEFAULT_BLOCK_THRESHOLD
    reviewers: tuple[ReviewerSpec, ...] = ()
    # T3: how many implement→review→fix rounds before we stop unapproved.
    max_rounds: int = DEFAULT_MAX_ROUNDS
    # T4: objective test gate per round commit (throwaway container).
    test_gate: bool = True  # auto-skips when no test command is detected
    test_command: str | None = None  # explicit override beats detection
    block_on_red: bool = False  # red gate prevents approval + feeds the coder
    test_timeout: int = DEFAULT_TEST_TIMEOUT
    # T5: usage-limit reaction. The pause budget is shared across the run;
    # the fallback chain lists ALTERNATE reviewer tools tried in order when
    # the current one is usage-limited (empty = no alternate -> pause).
    max_pause_minutes: int = DEFAULT_MAX_PAUSE_MINUTES
    pause_poll_minutes: int = DEFAULT_PAUSE_POLL_MINUTES
    reviewer_fallback_chain: tuple[str, ...] = ()
    # T7: total agent-spend ceiling for the run (None = unlimited).
    max_cost_usd: float | None = None
    acceptance_criteria: str | None = None
    run_id: str = field(default_factory=_short_run_id)
    # Host path to the operator's claude config dir (source of the auth file).
    claude_config_dir: Path = field(default_factory=lambda: Path.home() / ".claude")

    @property
    def effective_reviewers(self) -> tuple[ReviewerSpec, ...]:
        """The run's reviewer panel.

        Explicit ``reviewers`` specs win; otherwise the single-reviewer
        convenience fields are folded into one spec (the T2-era behaviour).
        """
        if self.reviewers:
            return self.reviewers
        return (
            ReviewerSpec(
                name=self.reviewer,
                tool=self.reviewer_tool,
                block_threshold=self.block_threshold,
                fallback_chain=self.reviewer_fallback_chain,
            ),
        )

    @property
    def effective_acceptance_criteria(self) -> str:
        """The "definition of done" shown to the reviewer.

        T2 falls back to the task description; an explicit ``--acceptance-criteria``
        surface is wired in T8/T12.
        """
        return self.acceptance_criteria or self.description

    @property
    def run_dir(self) -> Path:
        """Per-run state root: ``<work_dir>/<run_id>``."""
        return self.work_dir / self.run_id

    @property
    def coder_config_dir(self) -> Path:
        """Per-run coder config dir (CLAUDE_CONFIG_DIR target; holds transcript)."""
        return self.run_dir / "agents" / "coder" / "claude_config"

    def reviewer_config_dir(self, name: str) -> Path:
        """Per-run, per-reviewer config dir (its own CLAUDE_CONFIG_DIR / transcript)."""
        return self.run_dir / "agents" / f"review-{name}" / "claude_config"

    @property
    def worktree_parent(self) -> Path:
        """Where the run's worktree directory is created."""
        return self.run_dir / "worktree"

    @property
    def handoff_dir(self) -> Path:
        """Per-run handoff dir, mounted into the container at ``/workspace/.handoff``.

        Lives *outside* the git worktree so the worktree stays clean (the
        handoff is a separate artifact, not part of the deliverable branch).
        """
        return self.run_dir / "handoff"

    @property
    def gate_dir(self) -> Path:
        """Per-run root for test-gate state (exported trees, output, cache)."""
        return self.run_dir / "test_gate"

    @property
    def failures_dir(self) -> Path:
        """Per-run dir of failed-turn fixtures (the G4 capture harness)."""
        return self.run_dir / "failures"

    @property
    def operator_skills_dir(self) -> Path | None:
        """Operator's ``~/.claude/skills`` if present (mounted read-only).

        Restores the feasibility-gate G2 behaviour: operator-installed skills
        are available to the agent inside the per-run ``CLAUDE_CONFIG_DIR``.
        """
        skills = self.claude_config_dir / "skills"
        return skills if skills.is_dir() else None
