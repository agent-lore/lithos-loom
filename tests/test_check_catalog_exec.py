"""Execute the check catalog for real — R5b (task 79b256cf), opt-in, host only.

``test_story_develop_check_catalog`` asserts command STRINGS. That validates
intent, not outcome: in #173 an edited ``coverage`` command (``uv run coverage``)
could not even spawn and still shipped green, because nothing executes a catalog
command until a real gate runs it — and the daemon gates a loom story with its
own INSTALLED catalog, not the candidate's edited one.

This module runs every Python catalog check through the real gate path —
``build_check_set`` (image probe, catalog resolution, ``uv run`` wrapping, JSON
flags) → ``run_check_set`` (tree export, ``docker run`` in the sandbox image,
adapter parse) → ``check_result_blocks`` (the floor's own verdict) — against two
tiny generated fixtures, each in a uv-managed and a bare variant:

- **clean**: every check must run and pass. Catches a command that cannot spawn,
  bad flags, a tool missing from the image, output the adapter cannot parse.
- **defective**: one planted defect per check; every check must block. Catches a
  command that exits 0 without checking anything (a no-op).

Two negative controls patch a broken and a no-op command into the catalog and
assert this module reports each.

It is NEVER part of ``make check`` or CI (it needs docker, the sandbox image and
the network): ``make gate-exec`` sets ``LOOM_GATE_EXEC=1``. AGENTS.md makes it a
rule for any change to the gate definitions. The verdict helpers below are pure
and their unit tests always run.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Iterable, Mapping
from pathlib import Path

import pytest

from lithos_loom.plugins.story_develop import check_catalog, check_runner
from lithos_loom.plugins.story_develop.check_set import Check, CheckResult
from lithos_loom.plugins.story_develop.config import DEFAULT_IMAGE, DevelopConfig
from lithos_loom.plugins.story_develop.gate_findings import GateFinding, GateLedger
from lithos_loom.plugins.story_develop.test_gate import GateResult

OPT_IN_ENV = "LOOM_GATE_EXEC"
IMAGE_ENV = "LOOM_GATE_EXEC_IMAGE"

# What the `thorough` profile resolves to on a Python repo. `format` is absent on
# purpose: it is not a gate check — its live pass is the autoformat write pass.
EXPECTED_PYTHON_CHECKS = frozenset(
    {"lint", "typecheck", "sast", "test", "dep-audit", "coverage", "semgrep"}
)

needs_opt_in = pytest.mark.skipif(
    os.environ.get(OPT_IN_ENV) != "1",
    reason=f"executes the catalog in docker; run `make gate-exec` ({OPT_IN_ENV}=1)",
)


# --- verdict helpers (pure) ---------------------------------------------------


def check_problem(
    r: CheckResult, ledger: GateLedger, *, expect_block: bool
) -> str | None:
    """Why *r* is wrong for the fixture it ran on, or ``None`` when it is right.

    *expect_block* is the fixture's contract: ``False`` on the clean fixture (the
    check must run and pass), ``True`` on the defective one (it must block). The
    verdict is :func:`check_runner.check_result_blocks` — the floor's own reading,
    so an adapter check is judged by its findings, a raw one by its exit code.
    """
    if r.execution_outcome == "absent":
        return "tool missing from the image (expected-but-absent placeholder)"
    if r.execution_outcome != "ran":
        return f"did not run (outcome {r.execution_outcome!r})"
    blocks = check_runner.check_result_blocks(r, ledger)
    if expect_block and not blocks:
        return "stayed green on the defective fixture — a no-op command?"
    if not expect_block and blocks:
        return (
            "blocked on the clean fixture — a command that cannot spawn, "
            "bad flags, or output the adapter cannot parse?"
        )
    return None


def check_set_problem(names: Iterable[str]) -> str | None:
    """Why the resolved check names are not the expected Python set, or ``None``."""
    got = {n.split(".")[0] for n in names}
    missing = EXPECTED_PYTHON_CHECKS - got
    extra = got - EXPECTED_PYTHON_CHECKS
    if not missing and not extra:
        return None
    return (
        f"resolved check set differs: missing {sorted(missing)}, extra {sorted(extra)}"
    )


# Known catalog defects on the CLEAN fixture, keyed (variant, check) -> why + task.
# Strict: each entry must STILL fail, and a stale one is reported, so an entry is
# removed the moment its defect is fixed rather than masking a regression later.
KNOWN_BROKEN: Mapping[tuple[str, str], str] = {
    ("bare", "coverage"): (
        "ralph-sandbox installs coverage as an isolated `uv tool` env with no "
        "pytest, so `coverage run -m pytest` cannot import it (task b9822c09)"
    ),
}


def clean_problems(
    results: Iterable[CheckResult],
    ledger: GateLedger,
    *,
    variant: str,
    known: Mapping[tuple[str, str], str] = KNOWN_BROKEN,
) -> list[str]:
    """Problems on the clean fixture, with *known* defects expected (and enforced)."""
    out = []
    for r in results:
        problem = check_problem(r, ledger, expect_block=False)
        reason = known.get((variant, r.check.name.split(".")[0]))
        if reason is None:
            if problem is not None:
                out.append(describe(r, problem))
        elif problem is None:
            out.append(
                f"{r.check.name}: listed in KNOWN_BROKEN ({reason}) but now "
                "passes — remove the entry"
            )
    return out


def describe(r: CheckResult, problem: str) -> str:
    """One failure line block naming the check, its command and its output tail."""
    gate = r.gate
    exit_line = "" if gate is None else f"\n    exit {gate.exit_code}"
    tail = "" if gate is None else f"\n    {gate.output_tail.strip()[-1500:]}"
    return f"{r.check.name}: {problem}\n    $ {r.check.command}{exit_line}{tail}"


# --- verdict helper unit tests (hermetic: always run) -------------------------


def _ran(command: str, exit_code: int, *, name: str = "typecheck") -> CheckResult:
    return CheckResult(
        check=Check(name, command, "required"),
        execution_outcome="ran",
        gate=GateResult(command, exit_code, exit_code == 0, "tail"),
    )


def test_a_spawn_failure_on_the_clean_fixture_is_a_problem() -> None:
    r = _ran("uv run not-a-dep", 2)
    problem = check_problem(r, GateLedger(), expect_block=False)
    assert problem is not None and "blocked on the clean fixture" in problem


def test_a_green_run_on_the_defective_fixture_is_a_problem() -> None:
    problem = check_problem(_ran("true", 0), GateLedger(), expect_block=True)
    assert problem is not None and "no-op" in problem


def test_a_correct_verdict_is_no_problem() -> None:
    assert check_problem(_ran("pyright", 0), GateLedger(), expect_block=False) is None
    assert check_problem(_ran("pyright", 1), GateLedger(), expect_block=True) is None


def test_an_absent_tool_is_a_problem_either_way() -> None:
    r = CheckResult(
        check=Check("sast", "", "required"), execution_outcome="absent", gate=None
    )
    for expect_block in (False, True):
        problem = check_problem(r, GateLedger(), expect_block=expect_block)
        assert problem is not None and "missing from the image" in problem


def test_an_adapter_check_is_judged_by_its_findings_not_its_exit() -> None:
    # ruff runs with --exit-zero, so exit 0 says nothing; a major finding blocks.
    command = "ruff check --output-format=json --exit-zero"
    r = _ran(command, 0, name="lint")
    ledger = GateLedger()
    ledger.apply_round(
        "lint",
        [GateFinding("lint", "ruff", "F401", "major", "unused import", "a.py", 1)],
        1,
    )
    assert check_problem(r, ledger, expect_block=True) is None
    assert check_problem(r, GateLedger(), expect_block=True) is not None


def test_the_expected_set_reports_missing_and_extra_checks() -> None:
    assert check_set_problem(EXPECTED_PYTHON_CHECKS) is None
    assert (
        check_set_problem({"lint.python", *EXPECTED_PYTHON_CHECKS - {"lint"}}) is None
    )
    problem = check_set_problem((EXPECTED_PYTHON_CHECKS - {"coverage"}) | {"bogus"})
    assert problem is not None and "coverage" in problem and "bogus" in problem


def test_a_known_broken_check_is_expected_to_fail_and_a_stale_entry_is_reported() -> (
    None
):
    known = {("bare", "typecheck"): "reason"}
    failing, passing = _ran("pyright", 1), _ran("pyright", 0)
    assert clean_problems([failing], GateLedger(), variant="bare", known=known) == []
    stale = clean_problems([passing], GateLedger(), variant="bare", known=known)
    assert len(stale) == 1 and "remove the entry" in stale[0]
    # The entry is per variant: the same failure elsewhere is still a problem.
    other = clean_problems([failing], GateLedger(), variant="uv-managed", known=known)
    assert len(other) == 1 and "blocked on the clean fixture" in other[0]


# --- fixtures -----------------------------------------------------------------

# Dev deps are deliberately UNPINNED: `uv lock` resolves the latest at run time,
# so dep-audit's clean verdict is not hostage to a CVE published against an old
# pin (a pinned pytest 8.3.4 went red on PYSEC-2026-1845 the first time this ran).
_PYPROJECT = """\
[project]
name = "gatefixture"
version = "0.1.0"
requires-python = ">=3.10"
dependencies = [{deps}]

[dependency-groups]
dev = ["pytest", "coverage"]

[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["."]
"""

_CLEAN_MODULE = '''\
"""A tiny package the gate checks run against."""


def add(a: int, b: int) -> int:
    return a + b
'''

# One planted defect per check: F401 (lint), a type error (typecheck),
# shell=True (sast B602, semgrep), and the failing test below (test, coverage).
# dep-audit's defect is the vulnerable pin in pyproject.
_DEFECTIVE_MODULE = '''\
"""A tiny package with one planted defect per gate check."""

import os
import subprocess


def add(a: int, b: int) -> int:
    return a + b


def run(cmd: str) -> int:
    return subprocess.call(cmd, shell=True)


total: int = "not an int"
'''

_CLEAN_TEST = """\
from gatefixture import add


def test_add() -> None:
    assert add(1, 2) == 3
"""

_DEFECTIVE_TEST = (
    _CLEAN_TEST
    + """

def test_planted_failure() -> None:
    assert add(1, 1) == 3
"""
)

# A long-published, never-withdrawn advisory set (CVE-2021-33503 and later).
_VULNERABLE_PIN = '"urllib3==1.26.4"'


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        [
            "git",
            "-c",
            "user.name=gate-exec",
            "-c",
            "user.email=gate-exec@invalid",
            *args,
        ],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def make_fixture(root: Path, *, defective: bool, uv_managed: bool) -> tuple[Path, str]:
    """Write a fixture project under *root*, commit it, and return (repo, sha)."""
    repo = (
        root
        / f"{'defective' if defective else 'clean'}-{'uv' if uv_managed else 'bare'}"
    )
    (repo / "gatefixture").mkdir(parents=True)
    (repo / "tests").mkdir()
    deps = _VULNERABLE_PIN if defective else ""
    (repo / "pyproject.toml").write_text(_PYPROJECT.format(deps=deps))
    (repo / "gatefixture" / "__init__.py").write_text(
        _DEFECTIVE_MODULE if defective else _CLEAN_MODULE
    )
    (repo / "tests" / "test_add.py").write_text(
        _DEFECTIVE_TEST if defective else _CLEAN_TEST
    )
    if uv_managed:
        subprocess.run(["uv", "lock", "-q"], cwd=repo, check=True)
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "fixture")
    return repo, _git(repo, "rev-parse", "HEAD")


@pytest.fixture(scope="module")
def gate_image() -> str:
    """The sandbox image. Opted in, a missing docker or image FAILS — an explicit
    run must never pass by skipping its own subject."""
    image = os.environ.get(IMAGE_ENV, DEFAULT_IMAGE)
    if shutil.which("docker") is None:
        pytest.fail("docker is not on PATH; `make gate-exec` runs on the host")
    probe = subprocess.run(
        ["docker", "image", "inspect", image], capture_output=True, text=True
    )
    if probe.returncode != 0:
        pytest.fail(f"sandbox image {image!r} not found (set {IMAGE_ENV} to override)")
    if shutil.which("uv") is None:
        pytest.fail("uv is not on PATH (needed to lock the uv-managed fixture)")
    return image


def run_catalog(
    tmp_path: Path,
    image: str,
    *,
    defective: bool,
    uv_managed: bool,
    only: str | None = None,
) -> tuple[tuple[CheckResult, ...], GateLedger]:
    """Run the `thorough` check set, every check forced required, on one fixture.

    *only* turns every other check ``off`` — the negative controls exercise one.
    """
    repo, sha = make_fixture(tmp_path, defective=defective, uv_managed=uv_managed)
    names = EXPECTED_PYTHON_CHECKS if only is None else {only}
    states = {n: ("required" if n in names else "off") for n in EXPECTED_PYTHON_CHECKS}
    config = DevelopConfig(
        repo=repo,
        description="gate-exec",
        work_dir=tmp_path / "work",
        image=image,
        review_profile="thorough",
        check_states=states,
    )
    checks = check_runner.build_check_set(config, repo)
    ledger = GateLedger()
    result = check_runner.run_check_set(config, repo, sha, 1, checks, ledger)
    assert result is not None, "the gate cache dir could not be created"
    return result.results, ledger


def problems(
    results: Iterable[CheckResult], ledger: GateLedger, *, expect_block: bool
) -> list[str]:
    out = []
    for r in results:
        problem = check_problem(r, ledger, expect_block=expect_block)
        if problem is not None:
            out.append(describe(r, problem))
    return out


def _patch_catalog(
    monkeypatch: pytest.MonkeyPatch, mapping: check_catalog.CheckMapping
) -> None:
    """Swap one catalog entry for the negative controls. The resolver reads the
    private ``_BY_NAME`` index; patching it puts the broken command through the
    REAL resolution path, which an override (``check_commands``) would bypass."""
    monkeypatch.setitem(check_catalog._BY_NAME, mapping.name, mapping)


# --- execution (opt-in) -------------------------------------------------------

VARIANTS = [pytest.param(True, id="uv-managed"), pytest.param(False, id="bare")]


@needs_opt_in
@pytest.mark.parametrize("uv_managed", VARIANTS)
def test_every_catalog_check_runs_clean(
    tmp_path: Path, gate_image: str, uv_managed: bool
) -> None:
    results, ledger = run_catalog(
        tmp_path, gate_image, defective=False, uv_managed=uv_managed
    )
    variant = "uv-managed" if uv_managed else "bare"
    found = [p for p in [check_set_problem(r.check.name for r in results)] if p]
    found += clean_problems(results, ledger, variant=variant)
    assert not found, "\n".join(found)


@needs_opt_in
@pytest.mark.parametrize("uv_managed", VARIANTS)
def test_every_catalog_check_catches_its_defect(
    tmp_path: Path, gate_image: str, uv_managed: bool
) -> None:
    results, ledger = run_catalog(
        tmp_path, gate_image, defective=True, uv_managed=uv_managed
    )
    found = [p for p in [check_set_problem(r.check.name for r in results)] if p]
    found += problems(results, ledger, expect_block=True)
    assert not found, "\n".join(found)


@needs_opt_in
def test_negative_control_a_command_that_cannot_spawn_is_caught(
    tmp_path: Path, gate_image: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#173's shape: `uv run <not-a-dep>` — the module must report it."""
    broken = check_catalog.CheckMapping("typecheck", {"python": "not-a-dep-r5b"})
    _patch_catalog(monkeypatch, broken)
    results, ledger = run_catalog(
        tmp_path, gate_image, defective=False, uv_managed=True, only="typecheck"
    )
    assert [r.check.command for r in results] == ["uv run not-a-dep-r5b"]
    found = problems(results, ledger, expect_block=False)
    assert len(found) == 1 and "blocked on the clean fixture" in found[0], found


@needs_opt_in
def test_negative_control_a_no_op_command_is_caught(
    tmp_path: Path, gate_image: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    noop = check_catalog.CheckMapping("lint", {"python": "true"})
    _patch_catalog(monkeypatch, noop)
    results, ledger = run_catalog(
        tmp_path, gate_image, defective=True, uv_managed=True, only="lint"
    )
    assert [r.check.command for r in results] == ["true"]
    found = problems(results, ledger, expect_block=True)
    assert len(found) == 1 and "no-op" in found[0], found
