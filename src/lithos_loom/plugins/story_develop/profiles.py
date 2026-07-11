"""Review Profiles — the selectable review-strength dial (#139, ADR 0003 §1/§2/§3).

A **Review Profile** is a named bundle of {panel personas, deterministic check-set
(each check with a state + stage), blocking policy}. This module is the pure-data
+ resolution layer: the profile model, the three canonical profiles, the load-time
``strength_rank`` monotonicity invariant, and the precedence/fail-closed resolver.

This slice is **resolved-but-inert**: a profile is selected + validated, but it is
not yet *applied* to the run — wiring a resolved profile into the panel + check-set
(and the per-check staging filter) is #140. The one live behaviour is the
fail-closed halt (:func:`resolve_profile` returns ``halt=True`` for an
explicit-but-unknown name).

Blocking is implicit-from-state here (a ``required`` check/persona blocks, an
``informational`` one does not — matching :meth:`check_set.CheckResult.passed`);
an explicit blocking-policy field is reserved for #140+.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from ...errors import ConfigError
from .check_set import CheckState, Stage

__all__ = [
    "Stage",
    "ProfileCheck",
    "ReviewProfile",
    "CANONICAL_PROFILES",
    "DEFAULT_PROFILE_NAME",
    "UNKNOWN_PROFILE_POLICIES",
    "MonotonicityError",
    "validate_monotonic",
    "ProfileResolution",
    "resolve_profile",
    "get_profile",
    "UnknownProfileError",
]

# ``Stage`` (when a check runs — ``fast``/``candidate``) is owned by :mod:`check_set`
# and re-exported here, since the concrete :class:`check_set.Check` now carries it;
# the round-loop stage-filter that acts on it is #140 (ADR §4).

# Host policy for an explicit-but-unknown profile name (ADR §2).
UNKNOWN_PROFILE_POLICIES: frozenset[str] = frozenset({"halt", "strongest"})


class MonotonicityError(ConfigError):
    """A profile chain violates the ``strength_rank`` monotonicity invariant (ADR §2).

    A higher-ranked profile's required check-set AND required personas must each be
    a superset of every lower-ranked profile's. Raised at load — module import for
    the built-ins; host-config parse for operator-defined profiles.
    """


@dataclass(frozen=True)
class ProfileCheck:
    """One deterministic check a profile asks the gate to run.

    A thin superset of :class:`check_catalog.DesiredCheck` that adds ``stage``.
    ``state`` governs the floor (``required`` blocks; ``informational`` does not);
    ``stage`` governs *when* it runs, not whether it blocks.
    """

    name: str
    state: CheckState
    stage: Stage = "fast"


@dataclass(frozen=True)
class ReviewProfile:
    """A named {panel, check-set, blocking policy} bundle (ADR §1)."""

    name: str
    strength_rank: int
    personas: tuple[str, ...]
    checks: tuple[ProfileCheck, ...]

    @property
    def required_check_names(self) -> frozenset[str]:
        """Canonical names of the profile's required checks (the floor).

        Bare canonical names (``lint``, not ``lint.python``) — ecosystem
        qualification is a per-repo resolution artifact, not a profile property.
        """
        return frozenset(c.name for c in self.checks if c.state == "required")

    @property
    def required_personas(self) -> frozenset[str]:
        """The profile's required panel personas (every listed persona is required)."""
        return frozenset(self.personas)


DEFAULT_PROFILE_NAME = "standard"

# The three canonical profiles (ADR §3). `format` is required-but-auto-satisfied
# (so it never blocks a round on whitespace — the auto-format slice is separate);
# `coverage`'s threshold is informational input to the test-quality reviewer.
# `thorough`'s expensive checks are staged to the approval candidate (#140 acts on
# the stage; the field is set + validated here).
CANONICAL_PROFILES: tuple[ReviewProfile, ...] = (
    ReviewProfile(
        name="minimal",
        strength_rank=10,
        personas=(),
        checks=(
            ProfileCheck("format", "required"),
            ProfileCheck("lint", "required"),
            ProfileCheck("test", "required"),
        ),
    ),
    ReviewProfile(
        name="standard",
        strength_rank=20,
        personas=("correctness", "security"),
        checks=(
            ProfileCheck("format", "required"),
            ProfileCheck("lint", "required"),
            ProfileCheck("typecheck", "required"),
            # #140 floor slice (Option A): `sast` (bandit) is surfaced but does NOT
            # block the default — its repo baseline is untriaged, so blocking it on
            # `standard` would strand PRs on legacy findings. It is required (blocking)
            # only on `thorough`, an explicit opt-in. lint/typecheck/test are exactly
            # what `make check` already enforces, so blocking them adds no new
            # false-positive surface. Monotonicity holds: standard.required
            # {format,lint,typecheck,test} stays a subset of thorough's.
            ProfileCheck("sast", "informational"),
            ProfileCheck("test", "required"),
        ),
    ),
    ReviewProfile(
        name="thorough",
        strength_rank=30,
        personas=(
            "correctness",
            "security",
            "architecture",
            "test-quality",
            "dependency-hygiene",
        ),
        checks=(
            ProfileCheck("format", "required"),
            ProfileCheck("lint", "required"),
            ProfileCheck("typecheck", "required"),
            ProfileCheck("sast", "required"),
            ProfileCheck("test", "required"),
            ProfileCheck("dep-audit", "required", stage="candidate"),
            ProfileCheck("coverage", "required", stage="candidate"),
            ProfileCheck("semgrep", "informational", stage="candidate"),
        ),
    ),
)


def validate_monotonic(profiles: Sequence[ReviewProfile]) -> None:
    """Enforce the ADR §2 monotonicity invariant or raise :class:`MonotonicityError`.

    For every pair where ``a.strength_rank < b.strength_rank``, ``b``'s required
    check-set and required personas must each be a **superset** of ``a``'s — a
    higher rank may only *add* to the floor, never drop, so ``strength_rank`` truly
    tracks strictness and "strongest" is well-defined.

    Comparison is by canonical check + persona name only; ``stage`` is ignored (a
    check moved to ``candidate`` is still required, so it still counts to the floor).
    """
    for lower in profiles:
        for higher in profiles:
            if lower.strength_rank >= higher.strength_rank:
                continue
            missing_checks = lower.required_check_names - higher.required_check_names
            if missing_checks:
                raise MonotonicityError(
                    f"profile {higher.name!r} (rank {higher.strength_rank}) is not a "
                    f"superset of {lower.name!r} (rank {lower.strength_rank}): missing "
                    f"required check(s) {', '.join(sorted(missing_checks))}"
                )
            missing_personas = lower.required_personas - higher.required_personas
            if missing_personas:
                raise MonotonicityError(
                    f"profile {higher.name!r} (rank {higher.strength_rank}) is not a "
                    f"superset of {lower.name!r} (rank {lower.strength_rank}): missing "
                    f"required persona(s) {', '.join(sorted(missing_personas))}"
                )


# Validate the built-ins at import — a non-monotonic edit to CANONICAL_PROFILES is a
# load-time error (caught by any import, incl. pytest collection + pyright runs).
validate_monotonic(CANONICAL_PROFILES)

_BY_NAME: dict[str, ReviewProfile] = {p.name: p for p in CANONICAL_PROFILES}


class UnknownProfileError(ValueError):
    """A profile name that is not one of the canonical profiles (fail-closed, ADR §2).

    Raised by :func:`get_profile` — the single known-name seam. The ``develop
    review`` CLI and the eval case loader validate their explicit profile name
    through it and re-wrap this into their own error surface (a ``typer.BadParameter``
    / a case-prefixed ``ValueError``). ``resolve_profile`` applies the host
    ``halt``/``strongest`` *policy* to an unknown name and so does not raise.
    """

    def __init__(self, name: str, known: tuple[str, ...]) -> None:
        self.name = name
        self.known = known
        super().__init__(f"unknown profile {name!r}; known: {', '.join(known)}")


def get_profile(name: str) -> ReviewProfile:
    """The canonical :class:`ReviewProfile` for *name* — the single known-name seam.

    Fail-closed (ADR §2): an unknown name raises :class:`UnknownProfileError` rather
    than silently returning ``standard``. Every path that reaches here has already
    validated the name — :func:`resolve_profile` (daemon + standalone) resolves
    known-or-halt, and the ``develop review`` CLI + the eval case loader validate the
    explicit name through this function — so an unknown name here is an unvalidated
    path or a bug, and downgrading it to ``standard`` would run a *weaker* review
    than the operator asked for.
    """
    profile = _BY_NAME.get(name)
    if profile is None:
        raise UnknownProfileError(name, tuple(_BY_NAME))
    return profile


@dataclass(frozen=True)
class ProfileResolution:
    """The outcome of :func:`resolve_profile`.

    ``profile`` is always populated — on a fail-closed halt it carries the strongest
    available profile purely as a non-null placeholder the caller never runs.
    ``frictions`` are operator-facing ``[Friction]`` lines. ``halt`` is True iff the
    caller must **halt before any agent runs** (an explicit-but-unknown name under
    the default ``unknown_profile="halt"``).
    """

    profile: ReviewProfile
    frictions: tuple[str, ...] = ()
    halt: bool = False


def _first_set(*values: str | None) -> str | None:
    """The first non-None, non-blank value (the highest-precedence layer that is
    set), stripped; ``None`` when every layer is unset."""
    for value in values:
        if value is not None and value.strip():
            return value.strip()
    return None


def _strongest(available: Mapping[str, ReviewProfile]) -> ReviewProfile:
    """The highest-``strength_rank`` profile. Well-defined as "strongest" only over a
    validated monotonic chain (so max-rank genuinely is a superset of all below); the
    canonical set is validated at import."""
    return max(available.values(), key=lambda p: p.strength_rank)


def resolve_profile(
    *,
    task_value: str | None,
    project_value: str | None,
    host_value: str | None,
    unknown_profile: str = "halt",
    available: Mapping[str, ReviewProfile] | None = None,
) -> ProfileResolution:
    """Resolve the selected Review Profile (ADR §2).

    Precedence: ``task_value`` > ``project_value`` > ``host_value`` > built-in
    ``standard``. The first layer that is **set** (non-None, non-blank) is the
    *requested* name; an unset/blank layer inherits the layer below, silently.

    A **known** requested name resolves to it. An **explicit-but-unknown** name fails
    closed: ``unknown_profile="halt"`` (default) returns ``halt=True`` + a blocking
    friction (the run must not proceed at a lower strength than asked);
    ``unknown_profile="strongest"`` falls back to the strongest configured profile +
    a friction, **never a weaker one**.
    """
    profiles = dict(_BY_NAME if available is None else available)
    requested = _first_set(task_value, project_value, host_value)

    if requested is None:
        return ProfileResolution(profile=profiles[DEFAULT_PROFILE_NAME])

    known = profiles.get(requested)
    if known is not None:
        return ProfileResolution(profile=known)

    # Explicit-but-unknown -> fail closed.
    if unknown_profile == "strongest":
        strongest = _strongest(profiles)
        return ProfileResolution(
            profile=strongest,
            frictions=(
                f"review profile {requested!r} is not defined; falling back to the "
                f"strongest configured profile {strongest.name!r} "
                "(unknown_profile=strongest)",
            ),
        )
    # Default: halt. ``profile`` is the strongest only as a non-null placeholder.
    return ProfileResolution(
        profile=_strongest(profiles),
        frictions=(
            f"review profile {requested!r} is not defined; halting before any agent "
            "runs (fail-closed). Define the profile or fix the name.",
        ),
        halt=True,
    )
