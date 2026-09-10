"""Canonical reviewer personas (#137, ADR 0003 §8).

Each persona is a reusable :class:`ReviewerSpec` with its engine, severity floor,
and reasoning effort baked in, and a *one-dimension* ``system_prompt`` loaded from
``prompts/personas/<name>.md``. Personas are **opt-in**: a project selects them by
name (``develop_default_reviewers`` / task ``metadata.reviewers``) — the
zero-config default stays the single generalist ``code-quality`` reviewer (see
``daemon_io.BUILTIN_REVIEWERS``). The selectable *bundle/dial* is #139.

Engines are heterogeneous on purpose — different tools have different blind spots
(#94). Both engines honour ``effort`` (claude via ``--effort``, codex via the
``model_reasoning_effort`` config override — see
:mod:`~lithos_loom.plugins.story_develop.engines`), and the registry pins a level
on every reasoning-bound persona — ``xhigh`` for security, ``high`` for the codex
trio — so a run gets the same depth in the sandbox as the operator's hand review.
Per-reviewer project config and ``--reviewer-override`` still override it.
``model`` is left ``None`` (inherits the route / project default) rather than
hard-pinning a possibly-stale model id; operators may pin a cheaper model per
persona (e.g. ``dependency-hygiene``) via project config.
"""

from __future__ import annotations

from functools import lru_cache

from .config import ReviewerSpec
from .handoff import load_prompt

# (name, tool, effort, block_threshold) — the §8 table with the operator's
# correctness=codex override. The codex personas pin `high` — the depth the
# operator reviews at by hand (2026-09-10); before the engine honoured effort
# they ran at the sandbox CLI's builtin default. dependency-hygiene stays on the
# claude default: a cheap vetting pass, not a reasoning-bound one.
_PERSONA_SPECS: tuple[tuple[str, str, str | None, str], ...] = (
    ("correctness", "codex", "high", "major"),
    ("security", "claude", "xhigh", "minor"),
    ("architecture", "codex", "high", "major"),
    ("test-quality", "codex", "high", "minor"),
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
