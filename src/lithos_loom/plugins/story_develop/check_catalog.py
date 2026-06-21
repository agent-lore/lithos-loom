"""Per-ecosystem check mappings + applicability resolution (#133, ADR 0003 §4).

The deterministic gate (:mod:`check_set`) is an ordered set of named **checks**.
This module makes that set *ecosystem-aware*: each canonical check (``format`` /
``lint`` / ``typecheck`` / ``test`` / ``sast`` / ``dep-audit``) declares a command
**per ecosystem**, and :func:`resolve_check_set` turns a *desired* check-set (what
a Review Profile asks for — #139) into the concrete :class:`~.check_set.Check`
objects to run against the repo's detected ecosystem(s).

Applicability is **declared, not inferred from absence** (ADR §4):

- *non-required* check with no command for any detected ecosystem -> recorded N/A;
- *required* check with no command for any detected ecosystem -> a
  :class:`CheckApplicabilityError` (the profile asked for something the ecosystem
  cannot satisfy — an operator-actionable config error);
- *required* check that applies but whose tool is absent in the image ->
  "expected-but-absent": a non-running placeholder (empty ``command``) that the
  runner records as ``absent`` so a required check *blocks* (it is **not** a
  silent downgrade, and **not** the same as declared N/A).

The resolver is pure and hermetic: tool availability is **injected**, never
probed here, so it is unit-testable without a container. #133 ships the resolver
+ catalog; #139 wires a profile's desired set through it. The live single-``test``
default consults :func:`applies` only (its command keeps the tuned
:func:`...runner.detection.detect_test_commands` resolution).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from ...runner.detection import Ecosystem
from .check_set import Check, CheckState

__all__ = [
    "CheckMapping",
    "DesiredCheck",
    "CheckApplicabilityError",
    "CANONICAL_CHECKS",
    "applies",
    "resolve_check_set",
]


@dataclass(frozen=True)
class CheckMapping:
    """One canonical check's command per ecosystem.

    A check *applies* to an ecosystem iff that ecosystem is a key in ``commands``;
    a missing key is declared N/A (not a degraded command from another ecosystem).
    """

    name: str
    commands: dict[Ecosystem, str]


@dataclass(frozen=True)
class DesiredCheck:
    """What a profile (or the default) *asks for* — the input to resolution."""

    name: str
    state: CheckState


class CheckApplicabilityError(ValueError):
    """A *required* desired check has no command for any detected ecosystem.

    Operator-actionable: either add a per-ecosystem mapping or declare the check
    not-applicable for this repo. Distinct from "expected-but-absent" (the check
    *applies* but its tool is missing), which blocks at run time rather than here.
    """


# The canonical catalog. ``sast`` / ``dep-audit`` are declared for #139 to opt
# into once #135 provisions their tools in the sandbox; until then their tools
# are absent, so a profile must not mark them required (every repo would block).
CANONICAL_CHECKS: tuple[CheckMapping, ...] = (
    CheckMapping(
        "format",
        {
            "python": "ruff format --check",
            "node": "prettier --check .",
            "rust": "cargo fmt --check",
            "go": "gofmt -l .",
        },
    ),
    CheckMapping(
        "lint",
        {
            "python": "ruff check",
            "node": "eslint .",
            "rust": "cargo clippy",
            "go": "go vet ./...",
        },
    ),
    CheckMapping(
        "typecheck",
        {
            "python": "pyright",
            "node": "tsc --noEmit",
        },
    ),
    CheckMapping(
        "test",
        {
            "python": "pytest",
            "node": "npm test",
            "rust": "cargo test",
            "go": "go test ./...",
        },
    ),
    CheckMapping(
        "sast",
        {
            "python": "bandit -r .",
            "node": "semgrep --error",
        },
    ),
    CheckMapping(
        "dep-audit",
        {
            "python": "pip-audit",
            "node": "npm audit",
        },
    ),
)

_BY_NAME: dict[str, CheckMapping] = {m.name: m for m in CANONICAL_CHECKS}


def _mapped_command(name: str, ecosystems: Sequence[Ecosystem]) -> str | None:
    """The command for *name* against the first detected ecosystem that maps it,
    or ``None`` when no detected ecosystem maps the check (declared N/A)."""
    mapping = _BY_NAME.get(name)
    if mapping is None:
        return None
    for eco in ecosystems:
        cmd = mapping.commands.get(eco)
        if cmd is not None:
            return cmd
    return None


def applies(name: str, ecosystems: Sequence[Ecosystem]) -> bool:
    """Whether the canonical check *name* applies to at least one detected
    ecosystem. ``applies(name, ())`` is always ``False`` — a markerless repo
    declares every check N/A."""
    return _mapped_command(name, ecosystems) is not None


def resolve_check_set(
    desired: Sequence[DesiredCheck],
    ecosystems: Sequence[Ecosystem],
    *,
    tool_available: Callable[[str], bool],
) -> tuple[Check, ...]:
    """Resolve a *desired* check-set into concrete checks for *ecosystems*.

    ``tool_available(tool)`` reports whether a command's tool is runnable in the
    image (injected — probe once at the call site, never here). Raises
    :class:`CheckApplicabilityError` for a required check unsupported by the
    detected ecosystem(s). See the module docstring for the full classification.
    """
    if not ecosystems:
        # Markerless / docs-only repo: every check is declared N/A — never an
        # error, never a block (a required check here is not "expected").
        return tuple(Check(d.name, "", "not_applicable") for d in desired)

    resolved: list[Check] = []
    for d in desired:
        command = _mapped_command(d.name, ecosystems)
        if command is None:
            if d.state == "required":
                raise CheckApplicabilityError(
                    f"check {d.name!r} is required but has no command for "
                    f"ecosystem(s) {', '.join(ecosystems)}; add a mapping in "
                    f"CANONICAL_CHECKS or declare it not_applicable"
                )
            resolved.append(Check(d.name, "", "not_applicable"))
            continue
        if tool_available(command.split()[0]):
            resolved.append(Check(d.name, command, d.state))
        elif d.state == "required":
            # Expected-but-absent: applies to the ecosystem, but its tool is
            # missing — a blocking placeholder (the runner records it `absent`).
            resolved.append(Check(d.name, "", "required"))
        # else: optional/informational tool-absent -> fine, dropped (not run,
        # not recorded N/A — the tool simply isn't installed).
    return tuple(resolved)
