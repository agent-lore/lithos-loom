"""Per-ecosystem check mappings + applicability resolution (#133, ADR 0003 §4).

The deterministic gate (:mod:`check_set`) is an ordered set of named **checks**.
This module makes that set *ecosystem-aware*: each canonical check (``format`` /
``lint`` / ``typecheck`` / ``test`` / ``sast`` / ``dep-audit`` / ``coverage`` /
``semgrep``) declares a command **per ecosystem**, and :func:`resolve_check_set`
turns a *desired* check-set (what
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
    "ENV_DEPENDENT_CHECKS",
    "FORMATTER_COMMANDS",
    "applies",
    "formatter_commands",
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


# The canonical catalog. ``sast`` / ``dep-audit`` / ``coverage`` / ``semgrep`` are
# declared here as pure data for a Review Profile (#139) to reference; they only
# *run* once #140 resolves a profile's check-set against a repo (their tools ship
# in the sandbox as of #135). ``coverage`` is required only in ``thorough``;
# ``semgrep`` is always informational (ADR §3).
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
            # Exclude `.venv` so bandit scans the project, not its vendored deps:
            # on a uv repo the gate materialises the project venv in the tree, and a
            # bare `bandit -r .` would recurse into `.venv/**` and flag third-party
            # code as project findings (#170 follow-up). `.venv` is NOT in bandit's
            # default excludes; the `./` prefix is required for the match to fire.
            "python": "bandit -r . -x ./.venv",
            "node": "semgrep --error",
        },
    ),
    CheckMapping(
        "dep-audit",
        {
            # Audit the project's RESOLVED deps (the lock), not the container's ambient
            # env (#167): `uv export` the locked deps (the project package itself
            # excluded) and pipe to image-global pip-audit. `command_tool` resolves the
            # consumer (pip-audit) past the pipe as the adapter tool; the pipe runs in
            # the gate's `sh`. pip-audit stays bare/image-global — an external auditor,
            # not a project dep (#166 review), so it is NOT uv-run-wrapped.
            "python": (
                "uv export --no-emit-project --format requirements-txt "
                "| pip-audit -r /dev/stdin"
            ),
            "node": "npm audit",
        },
    ),
    CheckMapping(
        "coverage",
        {
            # A bare `coverage report` has no data to report (nothing ran under
            # coverage), so it always errors out — the run-then-report pair is the
            # runnable form. Both steps are env-dependent (they execute the project
            # + its test deps), so on a uv repo each `&&` step is `uv run`-wrapped
            # (see ENV_DEPENDENT_CHECKS / _uv_run); bare, they run image-global.
            "python": "coverage run -m pytest && coverage report",
        },
    ),
    CheckMapping(
        "semgrep",
        {
            # semgrep is its own informational check (distinct from python `sast`
            # = bandit); node's `sast` already runs semgrep, so it is python-only.
            "python": "semgrep --error",
        },
    ),
)

# The checks whose tool must run **inside the project venv** — it imports/executes
# the project or its deps: ``pyright`` resolves third-party imports, ``pytest`` /
# ``coverage`` run the code. On a uv-managed repo these run via ``uv run`` so the
# project venv (dev group included) is materialised in the gate container; bare, they
# see the container's empty ambient environment and false-positive (#165). ``test`` is
# included for completeness (it is the precedent — its command is resolved via
# ``detect_test_commands``, not :func:`resolve_check_set`).
#
# Deliberately EXCLUDED — they need no project venv, so they stay bare and image-global:
#   - static-analysis checks (ruff / bandit / semgrep): AST/source only;
#   - ``dep-audit`` (pip-audit): an *external auditor* that reads the lock / queries a
#     vuln DB — it is NOT a project dependency, so it is never ``uv run``-wrapped. It
#     audits the project's *resolved* deps by piping ``uv export``'s locked
#     requirements into image-global pip-audit (#167), not the container's ambient
#     env; ``command_tool`` resolves pip-audit (the pipe consumer) as the adapter tool
#     so the floor still reads its severity ledger (and a failed run blocks via the
#     floor-liveness rule, #167, rather than silently passing on an empty ledger).
ENV_DEPENDENT_CHECKS: frozenset[str] = frozenset({"typecheck", "coverage", "test"})

# The auto-format pass (#134, ADR §4) runs each detected ecosystem's formatter in
# **write** mode immediately after the coder's commit, so the ``format`` check (the
# read-only ``--check`` form in CANONICAL_CHECKS) is always already clean by the time
# it would run. These are the write-mode analogues of that mapping: ``ruff format``
# (drop ``--check``), ``prettier --write`` (vs ``--check``), ``cargo fmt`` (drop
# ``--check``), ``gofmt -w`` (vs ``-l``). Like the static-analysis checks, a formatter
# is image-global and never ``uv run``-wrapped (it rewrites source, not project code).
FORMATTER_COMMANDS: dict[Ecosystem, str] = {
    "python": "ruff format",
    "node": "prettier --write .",
    "rust": "cargo fmt",
    "go": "gofmt -w .",
}


def formatter_commands(
    ecosystems: Sequence[Ecosystem],
) -> list[tuple[Ecosystem, str]]:
    """The write-mode formatter command for each detected ecosystem, in order.

    Empty when no detected ecosystem has a formatter (e.g. a markerless repo) — the
    auto-format pass is then a no-op. Mirrors :func:`_applicable_commands`'s shape.
    """
    return [
        (eco, FORMATTER_COMMANDS[eco])
        for eco in ecosystems
        if eco in FORMATTER_COMMANDS
    ]


_BY_NAME: dict[str, CheckMapping] = {m.name: m for m in CANONICAL_CHECKS}


def _uv_run(command: str) -> str:
    """Prefix each ``&&``-joined step of *command* with ``uv run`` so a compound
    env-dependent command (``coverage run -m pytest && coverage report``) resolves
    *every* step against the project venv. A single leading ``uv run`` would wrap
    only the first step, leaving the rest to the container's empty environment. A
    plain single command (``pyright``) becomes ``uv run pyright`` unchanged."""
    return " && ".join(f"uv run {step.strip()}" for step in command.split("&&"))


def _applicable_commands(
    name: str, ecosystems: Sequence[Ecosystem]
) -> list[tuple[Ecosystem, str]]:
    """The ``(ecosystem, command)`` pairs for *name* across **every** detected
    ecosystem that maps it, in detection order. Empty when no detected ecosystem
    maps the check (declared N/A). A polyglot repo therefore sees one entry per
    applicable side (e.g. ``lint`` -> ruff *and* eslint), not just the first."""
    mapping = _BY_NAME.get(name)
    if mapping is None:
        return []
    return [
        (eco, mapping.commands[eco]) for eco in ecosystems if eco in mapping.commands
    ]


def applies(name: str, ecosystems: Sequence[Ecosystem]) -> bool:
    """Whether the canonical check *name* applies to at least one detected
    ecosystem. ``applies(name, ())`` is always ``False`` — a markerless repo
    declares every check N/A."""
    return bool(_applicable_commands(name, ecosystems))


def resolve_check_set(
    desired: Sequence[DesiredCheck],
    ecosystems: Sequence[Ecosystem],
    *,
    tool_available: Callable[[str], bool],
    uv_managed: bool = False,
) -> tuple[Check, ...]:
    """Resolve a *desired* check-set into concrete checks for *ecosystems*.

    ``tool_available(tool)`` reports whether a command's tool is runnable in the
    image (injected — probe once at the call site, never here). A check that
    applies to **multiple** detected ecosystems is emitted **once per ecosystem**
    (so a polyglot repo checks every side), with the name qualified
    ``<check>.<ecosystem>``; a single applicable ecosystem keeps the bare name.
    Raises :class:`CheckApplicabilityError` for a required check unsupported by
    **any** detected ecosystem. See the module docstring for the full
    classification.

    When ``uv_managed`` is true, an :data:`ENV_DEPENDENT_CHECKS` **python** command
    is prefixed ``uv run`` so it resolves against the project venv in the gate
    container (#165) — exactly as the ``test`` check already does. The prefix is
    applied *before* the availability probe, so ``tool_available`` is asked about the
    ``uv`` entrypoint (like ``uv run pytest``); ``False`` for ``uv`` still yields a
    required check's expected-but-absent placeholder. Non-python sides and
    static-analysis checks (ruff/bandit/semgrep) are never wrapped.
    """
    if not ecosystems:
        # Markerless / docs-only repo: every check is declared N/A — never an
        # error, never a block (a required check here is not "expected").
        return tuple(Check(d.name, "", "not_applicable") for d in desired)

    resolved: list[Check] = []
    for d in desired:
        applicable = _applicable_commands(d.name, ecosystems)
        if not applicable:
            if d.state == "required":
                raise CheckApplicabilityError(
                    f"check {d.name!r} is required but has no command for "
                    f"ecosystem(s) {', '.join(ecosystems)}; add a mapping in "
                    f"CANONICAL_CHECKS or declare it not_applicable"
                )
            resolved.append(Check(d.name, "", "not_applicable"))
            continue
        # Qualify the name only when the check spans >1 ecosystem, so the common
        # single-ecosystem case stays the bare ``test`` / ``lint``. ``.`` (not
        # ``:``) keeps the name safe in container names + output filenames.
        qualify = len(applicable) > 1
        for eco, command in applicable:
            check_name = f"{d.name}.{eco}" if qualify else d.name
            if uv_managed and eco == "python" and d.name in ENV_DEPENDENT_CHECKS:
                # Run the env-dependent tool inside the project venv; probe the `uv`
                # entrypoint, like the `test` check's `uv run pytest`. _uv_run wraps
                # each `&&` step so coverage's run-then-report both resolve in the venv.
                command = _uv_run(command)
            if tool_available(command.split()[0]):
                resolved.append(Check(check_name, command, d.state))
            elif d.state == "required":
                # Expected-but-absent: applies, but its tool is missing — a
                # blocking placeholder (the runner records it `absent`).
                resolved.append(Check(check_name, "", "required"))
            # else: optional/informational tool-absent -> dropped (the tool is
            # simply not installed; not a declared N/A).
    return tuple(resolved)
