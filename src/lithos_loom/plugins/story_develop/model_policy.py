"""Explicit per-agent model policy (#304).

An agent whose ``model`` is ``None`` runs whatever the sandbox image's CLI
happens to default to — a value recorded nowhere that drifts with image
rebuilds (the RH-8 investigation found every eval arm silently pinned to the
image's builtin, #303). Policy: every agent invocation loom makes (coder +
each panel reviewer, develop and eval alike) must resolve to an explicit
model. The lowest-priority explicit layer is ``[story_develop.default_models]``
in the loom TOML (tool → model); anything still unset after that fails closed
before a container starts.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace

from .config import ReviewerSpec


def apply_panel_default_models(
    panel: Sequence[ReviewerSpec], default_models: Mapping[str, str]
) -> tuple[ReviewerSpec, ...]:
    """Fill each reviewer's model from the per-tool default where still unset.

    Keyed by each reviewer's RESOLVED tool so a heterogeneous panel (#94) has
    every reviewer pick up the default for *their* own tool; an explicitly set
    model always wins. A tool with no configured default leaves that reviewer's
    model ``None`` — callers enforce explicitness via
    :func:`require_agent_models`.
    """
    return tuple(
        spec
        if spec.model is not None
        else replace(spec, model=default_models.get(spec.tool))
        for spec in panel
    )


def missing_agent_models(
    *,
    panel: Sequence[ReviewerSpec],
    coder: str | None = None,
    coder_model: str | None = None,
) -> tuple[str, ...]:
    """The agents that would run WITHOUT an explicit model, as operator phrases.

    Pass *coder* only on surfaces that run one (the develop path); review-only
    surfaces (eval, ``develop review`` / ``converge``) check the panel alone.
    """
    missing: list[str] = []
    if coder is not None and coder_model is None:
        missing.append(f"coder (tool {coder!r})")
    missing.extend(
        f"reviewer {spec.name!r} (tool {spec.tool!r})"
        for spec in panel
        if spec.model is None
    )
    return tuple(missing)


def require_agent_models(
    *,
    panel: Sequence[ReviewerSpec],
    coder: str | None = None,
    coder_model: str | None = None,
    where: str,
) -> None:
    """Fail closed when any agent lacks an explicit model (#304).

    Raises :class:`ValueError` naming every offending agent and the fix — the
    same pre-paid posture as the RH-7 override validation: an implicit model
    makes runs incomparable, so it is rejected before anything is spent.
    """
    missing = missing_agent_models(panel=panel, coder=coder, coder_model=coder_model)
    if missing:
        raise ValueError(
            f"{where}: agent(s) resolve to no explicit model: "
            + ", ".join(missing)
            + ' — set [story_develop.default_models] <tool> = "<model-id>" in the'
            " loom config, or pin the model per agent (project metadata / task"
            " override / --reviewer-override)"
        )
