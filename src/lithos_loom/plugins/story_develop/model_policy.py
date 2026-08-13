"""Explicit per-agent model policy (#304).

An agent whose ``model`` is ``None`` runs whatever the sandbox image's CLI
happens to default to — a value recorded nowhere that drifts with image
rebuilds (the RH-8 investigation found every eval arm silently pinned to the
image's builtin, #303). Policy: every agent invocation loom makes (coder +
each panel reviewer, develop and eval alike) must resolve to an explicit
model. The lowest-priority explicit layer is ``[story_develop.default_models]``
in the loom TOML (tool → model); anything still unset after that fails closed
before a container starts.

A reviewer's ``model`` field belongs to its PRIMARY tool only: after a
usage-limit engine switch the fallback engine draws from the per-tool
defaults via :func:`active_model` (a claude model string must never reach
``codex -m``), so callers validate reachable fallback tools up front too.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace

from . import engines
from .config import DevelopConfig, ReviewerSpec


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


def active_model(
    spec: ReviewerSpec, tool: str, default_models: Mapping[str, str]
) -> str | None:
    """The model for *spec* running on the currently-active *tool*.

    ``spec.model`` is pinned against the spec's PRIMARY tool; once a
    usage-limit fallback switches engines, that string would be another
    provider's model id — so any other tool draws from the per-tool defaults
    instead (#305 review finding 1).
    """
    if tool == spec.tool:
        return spec.model
    return default_models.get(tool)


def missing_agent_models(
    *,
    panel: Sequence[ReviewerSpec],
    coder: str | None = None,
    coder_model: str | None = None,
) -> tuple[str, ...]:
    """The agents that would run WITHOUT an explicit model, as operator phrases.

    Pass *coder* only on surfaces that run one (daemon/standalone develop and
    ``develop converge``); the coder-less surfaces (eval, ``develop review``)
    check the panel alone.
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


def missing_fallback_models(
    panel: Sequence[ReviewerSpec], default_models: Mapping[str, str]
) -> tuple[str, ...]:
    """Reachable fallback tools with no per-tool default model, as phrases.

    A usage-limited reviewer switches engines mid-run (its ``fallback_chain``),
    where :func:`active_model` has ONLY the per-tool defaults to draw from — so
    every reachable chain tool needs one up front, not at switch time. The
    reviewer's own tool is exempt (covered by ``spec.model``), and so are
    tools the engine registry doesn't support: the runtime skips those with a
    warning, so no model of theirs can ever be used.
    """
    missing: list[str] = []
    for spec in panel:
        for tool in dict.fromkeys(spec.fallback_chain):
            if tool == spec.tool or not engines.is_supported(tool):
                continue
            if default_models.get(tool) is None:
                missing.append(
                    f"reviewer {spec.name!r} fallback tool {tool!r}"
                    " (needed if its primary engine is usage-limited)"
                )
    return tuple(missing)


def require_agent_models(
    *,
    panel: Sequence[ReviewerSpec],
    coder: str | None = None,
    coder_model: str | None = None,
    default_models: Mapping[str, str] | None = None,
    where: str,
) -> None:
    """Fail closed when any agent lacks an explicit model (#304).

    Raises :class:`ValueError` naming every offending agent and the fix — the
    same pre-paid posture as the RH-7 override validation: an implicit model
    makes runs incomparable, so it is rejected before anything is spent. Pass
    *default_models* to additionally require coverage for every reachable
    fallback-chain tool (the switch-time model source — #305 review finding 1).
    """
    missing = missing_agent_models(panel=panel, coder=coder, coder_model=coder_model)
    if default_models is not None:
        missing += missing_fallback_models(panel, default_models)
    if missing:
        raise ValueError(
            f"{where}: agent(s) resolve to no explicit model: "
            + ", ".join(missing)
            + ' — set [story_develop.default_models] <tool> = "<model-id>" in the'
            " loom config, or pin the model per agent (project metadata, task"
            " override, or a per-agent CLI flag where this surface has one)"
        )


def resolve_config_models(
    config: DevelopConfig,
    default_models: Mapping[str, str],
    *,
    where: str,
    include_coder: bool,
) -> DevelopConfig:
    """Resolve + validate every agent model on a built config (#304).

    The one shared implementation behind every entry point (daemon,
    standalone, ``develop review`` / ``converge`` — the eval harness resolves
    its panel pre-paid instead): fill the EFFECTIVE panel (so the folded-in
    built-in reviewer is covered) and — when *include_coder* — the coder from
    *default_models*, validate (including reachable fallback-chain tools), and
    store the mapping on the config for the panel's switch-time resolution.
    Raises :class:`ValueError`; each entry point maps that onto its own
    fail-closed surface.
    """
    panel = apply_panel_default_models(config.effective_reviewers, default_models)
    coder_model = config.coder_model
    if include_coder and coder_model is None:
        coder_model = default_models.get(config.coder)
    require_agent_models(
        panel=panel,
        coder=config.coder if include_coder else None,
        coder_model=coder_model,
        default_models=default_models,
        where=where,
    )
    return replace(
        config,
        reviewers=panel,
        coder_model=coder_model,
        default_models=dict(default_models),
    )
