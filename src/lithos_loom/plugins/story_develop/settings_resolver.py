"""I/O-free metadata → scalar develop-settings resolution (ARCH-9 slice 2).

The precedence + parse + friction core for the *scalar* per-run develop settings,
lifted out of :func:`daemon_io.resolve_project_settings` so it has its own test
surface. Given two plain dicts — the project context-doc metadata and the task
metadata — plus a ``frictions`` accumulator, it returns a frozen
:class:`ScalarSettings`. **No I/O**: the caller fetches the context-doc metadata
and resolves the bespoke reviewer panel + the review-profile precedence (those need
Lithos / the host config); this module is the I/O-free part a unit test drives with
two dicts and a ``frictions`` list. It is a value resolver with an *append-only
friction sink* — not strictly pure: ``frictions`` is mutated by design, because its
append order is itself part of the preserved contract.

Precedence for the scalar fields (ADR 0003 §2 shape): per-task
``task.metadata.develop_*`` > per-project context-doc ``develop_*`` > built-in
default. A bad value at any layer degrades to a ``[Friction]`` breadcrumb (never
raises) so resolution never fails the run — the two friction suffixes encode which
layer was rejected: a bad **project** value is ``"; ignoring"`` (fall through to the
built-in default), a bad **task** override is ``"; keeping project default"``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from .config import (
    DEFAULT_CODER_TOOL,
    parse_bool_setting,
    parse_effort,
    parse_image,
    parse_model,
    parse_test_command,
)

# A config parser: ``(value, *, where) -> parsed | None``, raising ``ValueError`` on
# a malformed value (the shared contract of parse_model / parse_image / … in config).
Parser = Callable[..., Any]


@dataclass(frozen=True)
class ScalarSettings:
    """The scalar develop settings resolved from metadata.

    Mirrors the scalar subset of :class:`daemon_io.ProjectDevelopSettings` — it does
    NOT carry the reviewer panel or the *resolved* review profile (those stay bespoke
    in daemon_io). ``review_profile_project`` is the project-layer profile *name*
    only; the full task > project > host resolution runs in daemon_io (it needs the
    host policy).
    """

    coder: str = DEFAULT_CODER_TOOL
    coder_model: str | None = None
    coder_effort: str | None = None
    fallback_chain: tuple[str, ...] = ()
    max_rounds: int | None = None
    max_cost_usd: float | None = None
    image: str | None = None
    test_command: str | None = None
    test_gate: bool | None = None
    review_profile_project: str | None = None


def _parse_or_friction(
    parser: Parser,
    value: object,
    *,
    where: str,
    suffix: str,
    frictions: list[str],
    fallback: Any,
) -> Any:
    """Parse *value*; on ``ValueError`` append ``f"{exc}{suffix}"`` and return
    *fallback*. The single parse-or-friction atom every scalar layer shares."""
    try:
        return parser(value, where=where)
    except ValueError as exc:
        frictions.append(f"{exc}{suffix}")
        return fallback


@dataclass(frozen=True)
class _ProjectThenTaskField:
    """A scalar read from a top-level ``develop_*`` key at both layers: the project
    context-doc value, then a per-task override under the SAME key, parsed by the same
    parser. The byte-identical parse-or-friction block that used to repeat per field.
    """

    attr: str  # ScalarSettings attribute name
    key: str  # the metadata key at both layers (project + task)
    parser: Parser


# The three fields whose project + task keys are identical and whose value is a plain
# scalar. coder model/effort share the *shape* but read their project value from the
# nested ``develop_coder`` table under differently-named task keys, so they go through
# :func:`_resolve_coder` (which reuses the same :func:`_parse_or_friction` atom).
_PROJECT_THEN_TASK_FIELDS: tuple[_ProjectThenTaskField, ...] = (
    _ProjectThenTaskField("image", "develop_image", parse_image),
    _ProjectThenTaskField("test_command", "develop_test_command", parse_test_command),
    _ProjectThenTaskField("test_gate", "develop_test_gate", parse_bool_setting),
)


def _resolve_project_then_task(
    field: _ProjectThenTaskField,
    meta: Mapping[str, Any],
    task_metadata: Mapping[str, Any],
    frictions: list[str],
) -> Any:
    """Project ``develop_*`` (friction ``"; ignoring"`` on a bad value), then a
    per-task override under the same key (``"; keeping project default"``)."""
    value = _parse_or_friction(
        field.parser,
        meta.get(field.key),
        where=field.key,
        suffix="; ignoring",
        frictions=frictions,
        fallback=None,
    )
    if task_metadata.get(field.key) is not None:
        value = _parse_or_friction(
            field.parser,
            task_metadata[field.key],
            where=f"task metadata.{field.key}",
            suffix="; keeping project default",
            frictions=frictions,
            fallback=value,
        )
    return value


def _resolve_coder(
    meta: Mapping[str, Any], task_metadata: Mapping[str, Any], frictions: list[str]
) -> tuple[str, str | None, str | None]:
    """Resolve the coder tool + model + effort from ``develop_coder`` (project) and
    the ``develop_model`` / ``develop_effort`` task overrides (#93).

    The tool is project policy only (no task override): a blanket per-task model/effort
    pin flags "this one is cheap / needs deep reasoning" without letting a task swap
    engines. Friction order matches the original resolver: project model, project
    effort, then the task overrides.
    """
    coder = DEFAULT_CODER_TOOL
    project_model: object = None
    project_effort: object = None
    raw_coder = meta.get("develop_coder")
    if isinstance(raw_coder, dict):
        raw_tool = raw_coder.get("tool")
        if isinstance(raw_tool, str):
            coder = raw_tool
        elif raw_tool is not None:
            frictions.append("develop_coder.tool must be a string; using default")
        project_model = raw_coder.get("model")
        project_effort = raw_coder.get("effort")
    elif raw_coder is not None:
        frictions.append(
            "develop_coder must be an object with optional tool/model/effort; ignoring"
        )

    coder_model = _parse_or_friction(
        parse_model,
        project_model,
        where="develop_coder.model",
        suffix="; ignoring",
        frictions=frictions,
        fallback=None,
    )
    coder_effort = _parse_or_friction(
        parse_effort,
        project_effort,
        where="develop_coder.effort",
        suffix="; ignoring",
        frictions=frictions,
        fallback=None,
    )
    if task_metadata.get("develop_model") is not None:
        coder_model = _parse_or_friction(
            parse_model,
            task_metadata["develop_model"],
            where="task metadata.develop_model",
            suffix="; keeping project default",
            frictions=frictions,
            fallback=coder_model,
        )
    if task_metadata.get("develop_effort") is not None:
        coder_effort = _parse_or_friction(
            parse_effort,
            task_metadata["develop_effort"],
            where="task metadata.develop_effort",
            suffix="; keeping project default",
            frictions=frictions,
            fallback=coder_effort,
        )
    return coder, coder_model, coder_effort


def _warn_removed_block_on_red(
    meta: Mapping[str, Any], task_metadata: Mapping[str, Any], frictions: list[str]
) -> None:
    # #140: ``develop_block_on_red`` is removed — the ``test`` check's blocking is now
    # the resolved review profile's ``ProfileCheck("test", ...)``. A lingering key is
    # inert; surface a one-shot deprecation friction so the change is not silent.
    if (
        meta.get("develop_block_on_red") is not None
        or task_metadata.get("develop_block_on_red") is not None
    ):
        frictions.append(
            "develop_block_on_red is removed and ignored; the `test` check's blocking "
            "is now governed by the review profile (its ProfileCheck state) — use "
            "develop_review_profile / develop_test_gate instead"
        )


def _resolve_fallback_chain(
    meta: Mapping[str, Any], frictions: list[str]
) -> tuple[str, ...]:
    raw_chain = meta.get("develop_fallback_chain")
    if isinstance(raw_chain, list) and all(isinstance(t, str) for t in raw_chain):
        return tuple(raw_chain)
    if raw_chain is not None:
        frictions.append("develop_fallback_chain must be a list of strings; ignoring")
    return ()


def _resolve_max_rounds(meta: Mapping[str, Any], frictions: list[str]) -> int | None:
    max_rounds = meta.get("develop_max_rounds")
    if max_rounds is not None and (not isinstance(max_rounds, int) or max_rounds < 1):
        frictions.append(f"develop_max_rounds {max_rounds!r} invalid; ignoring")
        return None
    return max_rounds


def _resolve_max_cost(meta: Mapping[str, Any], frictions: list[str]) -> float | None:
    max_cost = meta.get("develop_max_cost_usd")
    if max_cost is not None and (
        not isinstance(max_cost, (int, float)) or max_cost <= 0
    ):
        frictions.append(f"develop_max_cost_usd {max_cost!r} invalid; ignoring")
        return None
    return float(max_cost) if max_cost is not None else None


def _resolve_review_profile_project(
    meta: Mapping[str, Any], frictions: list[str]
) -> str | None:
    # Carry the project-layer name only; the full task > project > host resolution
    # needs the host policy and runs in daemon_io.apply_review_profile.
    raw_profile = meta.get("develop_review_profile")
    if isinstance(raw_profile, str) and raw_profile.strip():
        return raw_profile.strip()
    if raw_profile is not None:
        frictions.append(
            f"develop_review_profile {raw_profile!r} invalid; ignoring "
            "(must be a non-empty string)"
        )
    return None


def resolve_scalar_settings(
    meta: Mapping[str, Any],
    task_metadata: Mapping[str, Any],
    frictions: list[str],
) -> ScalarSettings:
    """Resolve every scalar develop setting from project + task metadata.

    Appends to *frictions* in the same order the original
    :func:`daemon_io.resolve_project_settings` did (coder, image, test_command,
    test_gate, the ``block_on_red`` deprecation, fallback_chain, max_rounds,
    max_cost, review_profile). Never raises.
    """
    # Order is load-bearing: each helper appends its frictions as a side effect, and
    # the sequence must match the original resolve_project_settings (coder, image,
    # test_command, test_gate, block_on_red, fallback_chain, max_rounds, max_cost,
    # review_profile) — the daemon test net pins it.
    coder, coder_model, coder_effort = _resolve_coder(meta, task_metadata, frictions)
    scalars = {
        f.attr: _resolve_project_then_task(f, meta, task_metadata, frictions)
        for f in _PROJECT_THEN_TASK_FIELDS
    }
    _warn_removed_block_on_red(meta, task_metadata, frictions)
    fallback_chain = _resolve_fallback_chain(meta, frictions)
    max_rounds = _resolve_max_rounds(meta, frictions)
    max_cost_usd = _resolve_max_cost(meta, frictions)
    review_profile_project = _resolve_review_profile_project(meta, frictions)
    return ScalarSettings(
        coder=coder,
        coder_model=coder_model,
        coder_effort=coder_effort,
        image=scalars["image"],
        test_command=scalars["test_command"],
        test_gate=scalars["test_gate"],
        fallback_chain=fallback_chain,
        max_rounds=max_rounds,
        max_cost_usd=max_cost_usd,
        review_profile_project=review_profile_project,
    )
