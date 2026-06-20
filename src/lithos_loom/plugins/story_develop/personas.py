"""Canonical reviewer personas (#137, ADR 0003 §8).

Each persona is a reusable :class:`ReviewerSpec` with its engine, severity floor,
and reasoning effort baked in, and a *one-dimension* ``system_prompt`` loaded from
``prompts/personas/<name>.md``. Personas are **opt-in**: a project selects them by
name (``develop_default_reviewers`` / task ``metadata.reviewers``) — the
zero-config default stays the single generalist ``code-quality`` reviewer (see
``daemon_io.BUILTIN_REVIEWERS``). The selectable *bundle/dial* is #139.

Engines are heterogeneous on purpose — different tools have different blind spots
(#94). ``effort`` is honoured by claude only (codex depth is model-driven, see
``containers.build_exec_command``), so it is set only on the claude personas.
``model`` is left ``None`` (inherits the route / project default) rather than
hard-pinning a possibly-stale model id; operators may pin a cheaper model per
persona (e.g. ``dependency-hygiene``) via project config.
"""

from __future__ import annotations

from functools import lru_cache

from .config import ReviewerSpec
from .handoff import load_prompt

# (name, tool, effort, block_threshold) — the §8 table with the operator's
# correctness=codex override. effort is None for codex personas (model-driven).
_PERSONA_SPECS: tuple[tuple[str, str, str | None, str], ...] = (
    ("correctness", "codex", None, "major"),
    ("security", "claude", "xhigh", "minor"),
    ("architecture", "codex", None, "major"),
    ("test-quality", "codex", None, "minor"),
    ("dependency-hygiene", "claude", None, "minor"),
)


def _load_brief(name: str) -> str:
    """The persona's one-dimension focus brief from ``prompts/personas/<name>.md``."""
    return load_prompt(f"personas/{name}.md").strip()


@lru_cache(maxsize=1)
def canonical_personas() -> dict[str, ReviewerSpec]:
    """The canonical reviewer personas, keyed by name (ADR 0003 §8).

    Cached: the briefs are read from package data once. Selection wiring
    (``daemon_io._select_reviewers``) falls back to this registry when a chosen
    name is not in the project's explicit ``develop_reviewers`` pool.
    """
    return {
        name: ReviewerSpec(
            name=name,
            tool=tool,
            block_threshold=threshold,
            system_prompt=_load_brief(name),
            effort=effort,
        )
        for name, tool, effort, threshold in _PERSONA_SPECS
    }
