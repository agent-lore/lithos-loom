"""Tests for the per-ecosystem check catalog + applicability resolver (#133).

ADR 0003 §4: the check-set is ecosystem-aware. Each canonical check declares the
ecosystem(s) it applies to (a command per ecosystem); a desired check-set resolves
against the repo's detected ecosystem(s). Applicability is **declared, not inferred
from absence**:

- a *non-required* check with no command for the ecosystem -> recorded N/A;
- a *required* check with no command for any detected ecosystem -> a validation
  error (the operator asked for something the ecosystem can't satisfy);
- a *required* check that applies but whose tool is absent in the image ->
  "expected-but-absent": a non-running placeholder (empty command) that blocks.

The resolver is pure and hermetic — tool availability is injected, never probed.
"""

from __future__ import annotations

import pytest

from lithos_loom.plugins.story_develop.check_catalog import (
    CANONICAL_CHECKS,
    ENV_DEPENDENT_CHECKS,
    CheckApplicabilityError,
    CheckMapping,
    DesiredCheck,
    applies,
    resolve_check_set,
)


def _always(_tool: str) -> bool:
    return True


def _never(_tool: str) -> bool:
    return False


def _mapping(name: str) -> CheckMapping:
    return next(m for m in CANONICAL_CHECKS if m.name == name)


# --- uv-aware resolution for env-dependent checks (#165) ---------------------
# Checks that must run INSIDE the project venv — they import/execute the project or
# its deps (pyright resolves imports; pytest/coverage run the code) — get `uv run`
# on a uv-managed repo, exactly like the `test` check already does; bare, they run
# against the gate container's empty environment and false-positive. Static-analysis
# checks (ruff/bandit/semgrep — AST/source only) AND external auditors (pip-audit
# reads the lock, doesn't run in the venv) need no project venv and stay bare.


def test_env_dependent_checks_are_the_project_env_consumers() -> None:
    # The single data-driven source of truth for checks that run inside the project
    # venv. Static-analysis tools AND external auditors (pip-audit) are excluded.
    assert frozenset({"typecheck", "coverage", "test"}) == ENV_DEPENDENT_CHECKS


def test_resolve_uv_managed_wraps_env_dependent_python_checks() -> None:
    checks = resolve_check_set(
        [
            DesiredCheck("typecheck", "required"),
            DesiredCheck("lint", "required"),
        ],
        ("python",),
        tool_available=_always,
        uv_managed=True,
    )
    by = {c.name: c for c in checks}
    assert by["typecheck"].command == "uv run pyright"
    # lint (ruff) is static-analysis only — NOT wrapped.
    assert by["lint"].command == "ruff check"


def test_resolve_uv_managed_wraps_each_step_of_compound_coverage() -> None:
    # coverage is a run-then-report compound (#170 follow-up): a single leading
    # `uv run` would wrap only the `coverage run` step, leaving `coverage report`
    # to the container's empty environment. Each `&&` step must resolve in the venv.
    checks = resolve_check_set(
        [DesiredCheck("coverage", "required")],
        ("python",),
        tool_available=_always,
        uv_managed=True,
    )
    assert checks[0].command == (
        "uv run coverage run -m pytest && uv run coverage report"
    )


def test_resolve_uv_managed_does_not_wrap_dep_audit_external_auditor() -> None:
    # Regression guard (#166 review): pip-audit is an *external auditor* (reads the
    # lock, queries a vuln DB) — NOT a project dependency, so it is never `uv run`-
    # wrapped even on a uv repo (dep-audit is excluded from ENV_DEPENDENT_CHECKS); the
    # `uv export` producer is image-global uv, not a venv-resident tool.
    checks = resolve_check_set(
        [DesiredCheck("dep-audit", "required")],
        ("python",),
        tool_available=_always,
        uv_managed=True,
    )
    assert checks[0].command == (
        "uv export --no-emit-project --format requirements-txt "
        "| pip-audit -r /dev/stdin"
    )


def test_dep_audit_python_audits_project_lock_not_ambient_env() -> None:
    # #167: bare `pip-audit` audits the container's AMBIENT env (the image's global
    # site-packages), not the project. The catalog must export the project's locked
    # deps and pipe them to pip-audit so the auditor sees the project's RESOLVED set.
    checks = resolve_check_set(
        [DesiredCheck("dep-audit", "required")], ("python",), tool_available=_always
    )
    assert checks[0].command == (
        "uv export --no-emit-project --format requirements-txt "
        "| pip-audit -r /dev/stdin"
    )


def test_resolve_uv_managed_false_leaves_env_checks_bare() -> None:
    checks = resolve_check_set(
        [DesiredCheck("typecheck", "required")],
        ("python",),
        tool_available=_always,
        uv_managed=False,
    )
    assert checks[0].command == "pyright"


def test_resolve_uv_managed_defaults_to_false() -> None:
    # Back-compat: callers that don't pass uv_managed get the bare command (a non-uv
    # project, and every existing #133 call site, is unchanged).
    checks = resolve_check_set(
        [DesiredCheck("typecheck", "required")], ("python",), tool_available=_always
    )
    assert checks[0].command == "pyright"


def test_resolve_uv_managed_only_wraps_the_python_side_in_polyglot() -> None:
    checks = resolve_check_set(
        [DesiredCheck("typecheck", "required")],
        ("python", "node"),
        tool_available=_always,
        uv_managed=True,
    )
    by = {c.name: c for c in checks}
    assert by["typecheck.python"].command == "uv run pyright"
    # `uv run` is python-only — the node side keeps its native command.
    assert by["typecheck.node"].command == "tsc --noEmit"


def test_resolve_uv_managed_probes_the_uv_entrypoint() -> None:
    # Availability is keyed on the `uv` entrypoint (like the `test` check's
    # `uv run pytest`), since `uv run` materialises and invokes the real tool.
    seen: list[str] = []

    def _probe(tool: str) -> bool:
        seen.append(tool)
        return True

    checks = resolve_check_set(
        [DesiredCheck("typecheck", "required")],
        ("python",),
        tool_available=_probe,
        uv_managed=True,
    )
    assert "uv" in seen
    assert checks[0].command == "uv run pyright"


# --- the catalog: per-ecosystem command mappings (AC1) -----------------------


def test_catalog_covers_the_canonical_checks() -> None:
    names = {m.name for m in CANONICAL_CHECKS}
    assert {"format", "lint", "typecheck", "test"} <= names


def test_catalog_includes_coverage_and_semgrep_for_profiles() -> None:
    # #139: `thorough` references coverage (required) + semgrep (informational);
    # both must resolve for python so a profile doesn't raise when #140 wires it.
    cov = resolve_check_set(
        [DesiredCheck("coverage", "required")], ("python",), tool_available=_always
    )
    assert cov[0].name == "coverage"
    assert cov[0].command == "coverage run -m pytest && coverage report"
    sg = resolve_check_set(
        [DesiredCheck("semgrep", "informational")], ("python",), tool_available=_always
    )
    assert sg[0].name == "semgrep" and sg[0].command == "semgrep --error"


def test_sast_python_excludes_the_project_venv() -> None:
    # On a uv repo the gate materialises `.venv` in the tree; bandit must not recurse
    # into it and flag vendored deps as project findings (#170 follow-up). `.venv` is
    # not a bandit default exclude, so the mapping declares it (with the `./` prefix
    # bandit's path-match requires).
    checks = resolve_check_set(
        [DesiredCheck("sast", "required")], ("python",), tool_available=_always
    )
    assert checks[0].command == "bandit -r . -x ./.venv"


def test_coverage_python_is_runnable_run_then_report() -> None:
    # A bare `coverage report` has no data to report and always errors; the catalog
    # must declare the run-then-report compound so the check is actually runnable.
    checks = resolve_check_set(
        [DesiredCheck("coverage", "required")], ("python",), tool_available=_always
    )
    assert checks[0].command == "coverage run -m pytest && coverage report"


def test_test_check_maps_python_and_at_least_one_other_ecosystem() -> None:
    # AC1: "at least Python + one other". We ship all four.
    cmds = _mapping("test").commands
    assert cmds["python"] == "pytest"
    assert cmds["node"] == "npm test"
    assert cmds["rust"] == "cargo test"
    assert cmds["go"] == "go test ./..."


def test_lint_check_has_distinct_per_ecosystem_commands() -> None:
    cmds = _mapping("lint").commands
    assert cmds["python"] == "ruff check"
    assert "eslint" in cmds["node"]


def test_typecheck_is_not_applicable_to_go() -> None:
    # A check declares the ecosystems it applies to: typecheck has no Go analogue,
    # so Go simply has no mapping (declared N/A), not a degraded Python command.
    cmds = _mapping("typecheck").commands
    assert cmds["python"] == "pyright"
    assert "go" not in cmds


def test_every_mapping_is_non_empty() -> None:
    for m in CANONICAL_CHECKS:
        assert m.commands, f"{m.name} has no per-ecosystem commands"
        assert all(cmd for cmd in m.commands.values())


# --- applies(): the live applicability predicate -----------------------------


def test_applies_true_when_mapped_for_a_detected_ecosystem() -> None:
    assert applies("test", ("python",)) is True


def test_applies_false_when_unmapped_for_the_ecosystem() -> None:
    assert applies("typecheck", ("go",)) is False


def test_applies_false_without_any_ecosystem() -> None:
    # Markerless / docs-only repo: nothing applies -> declared N/A at repo level.
    assert applies("test", ()) is False


# --- resolve_check_set(): declared-applicability resolution (AC2/AC3) ---------


def test_resolve_runnable_when_mapped_and_tool_present() -> None:
    checks = resolve_check_set(
        [DesiredCheck("lint", "required")], ("python",), tool_available=_always
    )
    assert len(checks) == 1
    assert checks[0].name == "lint"
    assert checks[0].command == "ruff check"
    assert checks[0].state == "required"


def test_resolve_required_unmapped_raises_actionable_error() -> None:
    # AC3: an unsupported *required* check on a (present) ecosystem is a config
    # error with an operator-actionable message — not a silent downgrade.
    with pytest.raises(CheckApplicabilityError) as exc:
        resolve_check_set(
            [DesiredCheck("typecheck", "required")], ("go",), tool_available=_always
        )
    msg = str(exc.value)
    assert "typecheck" in msg
    assert "go" in msg


def test_resolve_nonrequired_unmapped_is_recorded_na() -> None:
    checks = resolve_check_set(
        [DesiredCheck("typecheck", "informational")], ("go",), tool_available=_always
    )
    assert len(checks) == 1
    assert checks[0].state == "not_applicable"
    assert checks[0].command == ""


def test_resolve_required_tool_absent_is_expected_but_absent() -> None:
    # AC2: the check applies to the ecosystem but its tool is absent in the image.
    # That is a quality gap (a blocking placeholder), distinct from declared N/A.
    checks = resolve_check_set(
        [DesiredCheck("test", "required")], ("python",), tool_available=_never
    )
    assert len(checks) == 1
    assert checks[0].name == "test"
    assert checks[0].command == ""  # the empty-command "absent" placeholder
    assert checks[0].state == "required"


def test_resolve_optional_tool_absent_is_dropped() -> None:
    # ADR §4: an optional/informational check whose tool is absent is fine — it is
    # neither run nor recorded N/A (it is not N/A; the tool merely isn't installed).
    checks = resolve_check_set(
        [DesiredCheck("lint", "informational")], ("python",), tool_available=_never
    )
    assert checks == ()


def test_resolve_without_ecosystem_records_na_and_never_raises() -> None:
    # A markerless repo: even a *required* desired check is declared N/A, not an
    # error (docs-only repos are legitimately check-free).
    checks = resolve_check_set(
        [DesiredCheck("test", "required")], (), tool_available=_always
    )
    assert len(checks) == 1
    assert checks[0].state == "not_applicable"


def test_resolve_single_applicable_ecosystem_keeps_bare_name() -> None:
    # typecheck applies to python but not go: only one applicable ecosystem, so
    # the name stays bare and resolves to that ecosystem's command.
    checks = resolve_check_set(
        [DesiredCheck("typecheck", "required")],
        ("go", "python"),  # go has no typecheck; python does
        tool_available=_always,
    )
    assert len(checks) == 1
    assert checks[0].name == "typecheck"
    assert checks[0].command == "pyright"


def test_resolve_polyglot_emits_one_check_per_applicable_ecosystem() -> None:
    # The core #133 fix: a check that applies to >1 detected ecosystem runs once
    # per ecosystem (a polyglot repo must check every side), name-qualified.
    checks = resolve_check_set(
        [DesiredCheck("lint", "required")], ("python", "node"), tool_available=_always
    )
    by_name = {c.name: c for c in checks}
    assert set(by_name) == {"lint.python", "lint.node"}
    assert by_name["lint.python"].command == "ruff check"
    assert by_name["lint.node"].command == "eslint ."
    assert all(c.state == "required" for c in checks)


def test_resolve_polyglot_drops_only_the_absent_optional_side() -> None:
    # Per-ecosystem availability: ruff present, eslint absent + informational ->
    # only the python side survives (the absent optional side is dropped).
    def _only_ruff(tool: str) -> bool:
        return tool == "ruff"

    checks = resolve_check_set(
        [DesiredCheck("lint", "informational")],
        ("python", "node"),
        tool_available=_only_ruff,
    )
    assert [c.name for c in checks] == ["lint.python"]
    assert checks[0].command == "ruff check"


def test_resolve_polyglot_required_absent_side_blocks() -> None:
    # ruff present, eslint absent + required -> python runs; node is
    # expected-but-absent (empty-command placeholder that blocks).
    def _only_ruff(tool: str) -> bool:
        return tool == "ruff"

    checks = resolve_check_set(
        [DesiredCheck("lint", "required")],
        ("python", "node"),
        tool_available=_only_ruff,
    )
    by_name = {c.name: c for c in checks}
    assert by_name["lint.python"].command == "ruff check"
    assert by_name["lint.node"].command == ""  # expected-but-absent placeholder
    assert by_name["lint.node"].state == "required"
