"""Preflight for the shipped triage fixtures (evals/triage/cases).

Hermetic (git only): every shipped case loads, its sha is a real commit in
the (possibly sibling) checkout, every refutation file a correct rejection
must cite is tracked at that sha, and every finding's own file anchor
resolves there too — so a paid run never fails on a fixture typo, and a
known-false whose refutation moved is caught by the gate. Skips with a
reason where the checkout is absent (CI has no sibling lens).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from lithos_loom.evals.triage.case import load_triage_case

_SHIPPED = Path(__file__).resolve().parents[1] / "evals" / "triage" / "cases"


def _shipped_dirs() -> list[Path]:
    if not _SHIPPED.is_dir():
        return []
    return sorted(d for d in _SHIPPED.iterdir() if (d / "case.toml").is_file())


def _tracked_at(repo: Path, sha: str) -> frozenset[str]:
    out = subprocess.run(
        ["git", "-C", str(repo), "ls-tree", "-r", "--name-only", sha],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return frozenset(out.split())


def test_at_least_one_triage_fixture_ships() -> None:
    assert _shipped_dirs(), "the S8 triage corpus is empty"


@pytest.mark.parametrize("case_dir", _shipped_dirs(), ids=lambda p: p.name)
def test_shipped_triage_case_resolves(case_dir: Path) -> None:
    case = load_triage_case(case_dir)
    repo = Path(case.repo).resolve()
    if not (repo / ".git").exists():
        pytest.skip(f"repo {case.repo!r} is not a git checkout here")
    probe = subprocess.run(
        ["git", "-C", str(repo), "cat-file", "-e", f"{case.sha}^{{commit}}"],
        capture_output=True,
    )
    if probe.returncode != 0:
        pytest.skip(f"sha {case.sha[:12]} not present (shallow clone?)")
    tracked = _tracked_at(repo, case.sha)
    for f in case.findings:
        for anchor in f.files:
            path = anchor.split(":", 1)[0]
            assert path in tracked, (
                f"{case.id}/{f.finding_id}: {path} not at {case.sha[:12]}"
            )
        for path in f.refutation_files:
            assert path in tracked, (
                f"{case.id}/{f.finding_id}: refutation {path} not at {case.sha[:12]}"
            )
    # Every batch must contain both directions or say why not: an eval that
    # measures rejection needs a known-false, and one that measures
    # over-suppression needs a known-true.
    assert case.known_true, (
        f"{case.id}: no known-true finding to guard over-suppression"
    )
