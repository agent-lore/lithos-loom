"""Per-run panel overrides for ``eval review`` (RH-7).

Widens the eval from a prompt-comparison instrument into a lever-matrix
instrument: a run can vary the panel — profile replacement, explicit
``--reviewer`` enumeration, per-reviewer model/effort/tool overrides — without
editing case files or persona definitions. Everything here validates **before
any paid run** (fail closed on a typo, not hours into a K-sample sweep), and the
resolved panel is recorded per case in ``summary.json`` so two report dirs are
comparable.

This is exposure, not capability: ``ReviewerSpec.model`` / ``.effort`` (#93) and
``.tool`` (#94) already reach the agent end-to-end in review-only mode; this
module only builds the spec tuple the harness hands to ``DevelopConfig``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from ...plugins.story_develop import engines
from ...plugins.story_develop.config import ReviewerSpec, parse_effort, parse_model
from ...plugins.story_develop.personas import canonical_personas
from ...plugins.story_develop.profiles import get_profile
from .case import Case

# The per-reviewer axes a run may vary. Deliberately NOT block_threshold /
# system_prompt / fallback_chain: those change what a persona IS, not which
# lever is being measured — vary them by editing the persona, benchmark-gated.
_OVERRIDE_FIELDS = ("model", "effort", "tool")

# {persona: {field: validated value}} — the shape parse_reviewer_overrides
# returns and resolve_panel consumes.
ReviewerOverrides = dict[str, dict[str, str]]


def parse_reviewer_overrides(items: Sequence[str]) -> ReviewerOverrides:
    """Parse ``PERSONA.FIELD=VALUE`` override strings, fail-closed.

    Persona names are validated against the canonical registry, fields against
    :data:`_OVERRIDE_FIELDS`, and values through the same validators the config
    surfaces use (``parse_model`` / ``parse_effort`` / ``engines.is_supported``)
    so every surface rejects identical garbage. A later duplicate for the same
    ``persona.field`` wins. Raises :class:`ValueError`.
    """
    registry = canonical_personas()
    overrides: ReviewerOverrides = {}
    for item in items:
        where = f"--reviewer-override {item!r}"
        target, sep, value = item.partition("=")
        persona, dot, field = target.partition(".")
        if not sep or not dot or not persona or not field:
            raise ValueError(f"{where}: must be PERSONA.FIELD=VALUE")
        if persona not in registry:
            raise ValueError(
                f"{where}: unknown persona {persona!r}; "
                f"known: {', '.join(sorted(registry))}"
            )
        if field not in _OVERRIDE_FIELDS:
            raise ValueError(
                f"{where}: field must be one of {_OVERRIDE_FIELDS} (got {field!r})"
            )
        if field == "model":
            parsed = parse_model(value, where=where)
        elif field == "effort":
            parsed = parse_effort(value, where=where)
        else:
            parsed = value.strip()
            if not engines.is_supported(parsed):
                raise ValueError(
                    f"{where}: unsupported tool {parsed!r} "
                    f"(expected {engines.supported_tools_phrase()})"
                )
        # parse_model/parse_effort return None only for a None input; the CLI
        # hands us strings, and empty strings raise above.
        assert parsed is not None
        overrides.setdefault(persona, {})[field] = parsed
    return overrides


def resolve_panel(
    case: Case,
    *,
    profile: str | None = None,
    reviewers: Sequence[str] | None = None,
    overrides: ReviewerOverrides | None = None,
) -> tuple[str, tuple[ReviewerSpec, ...]]:
    """The effective ``(profile, panel)`` for *case* under the run's overrides.

    Precedence: an explicit ``reviewers`` enumeration wins the panel (dedup
    preserving order, unknown names fail closed); else a ``profile`` override
    replaces the panel with the profile's personas — what a live
    ``develop_review_profile`` run would field — and a gate-only profile is
    rejected because there would be nothing to measure; else the case's own
    personas. Per-reviewer *overrides* then apply where present: a case whose
    panel lacks an overridden persona runs unmodified (a full-benchmark sweep
    mixes panels), the typo protection being the registry check in
    :func:`parse_reviewer_overrides`. Specs are rebuilt with
    ``dataclasses.replace`` — the shared cached registry is never mutated.
    """
    registry = canonical_personas()
    effective_profile = profile or case.profile
    if reviewers:
        specs: list[ReviewerSpec] = []
        for name in reviewers:
            spec = registry.get(name)
            if spec is None:
                raise ValueError(
                    f"--reviewer: unknown persona {name!r}; "
                    f"known: {', '.join(sorted(registry))}"
                )
            if spec not in specs:
                specs.append(spec)
        panel = tuple(specs)
    elif profile is not None:
        prof = get_profile(profile)
        if not prof.personas:
            raise ValueError(
                f"--profile {profile!r} is gate-only (no personas) — nothing to "
                "measure; add --reviewer to name a panel"
            )
        panel = tuple(registry[p] for p in prof.personas)
    else:
        panel = tuple(registry[p] for p in case.personas)
    if overrides:
        panel = tuple(
            replace(spec, **overrides[spec.name]) if spec.name in overrides else spec
            for spec in panel
        )
    return effective_profile, panel
