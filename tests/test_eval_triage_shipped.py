"""Preflight for the shipped triage fixtures (evals/triage/cases).

Hermetic (git only): every shipped case loads, its tree can be built (the
sha exists, or base + head_patch applies) in the — possibly sibling —
checkout, every finding's anchor and every refutation path is tracked at
that tree and every refutation range lies inside its file, and a batch
carries at least one must-proceed finding. Skips with a reason where the
checkout is absent (CI has no sibling lens). Case-specific pins follow.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from lithos_loom.evals.triage.case import TriageCase, load_triage_case
from lithos_loom.evals.triage.harness import materialise_tree

_SHIPPED = Path(__file__).resolve().parents[1] / "evals" / "triage" / "cases"


def _shipped_dirs() -> list[Path]:
    if not _SHIPPED.is_dir():
        return []
    return sorted(d for d in _SHIPPED.iterdir() if (d / "case.toml").is_file())


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout


def _commit_exists(repo: Path, sha: str) -> bool:
    return (
        subprocess.run(
            ["git", "-C", str(repo), "cat-file", "-e", f"{sha}^{{commit}}"],
            capture_output=True,
        ).returncode
        == 0
    )


def _built(
    case: TriageCase, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, str, object]:
    """``(repo, sha, cleanup)`` for a shipped case, or skip where it can't run."""
    repo = Path(case.repo).resolve()
    if not (repo / ".git").exists():
        pytest.skip(f"repo {case.repo!r} is not a git checkout here")
    anchor = case.sha or case.base
    if not _commit_exists(repo, anchor):
        pytest.skip(f"{anchor[:12]} not present in {case.repo} (shallow clone?)")
    monkeypatch.setenv("GIT_AUTHOR_NAME", "loom-eval-preflight")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "loom-eval-preflight@localhost")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "loom-eval-preflight")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "loom-eval-preflight@localhost")
    sha, cleanup = materialise_tree(case)
    return repo, sha, cleanup


def test_at_least_one_triage_fixture_ships() -> None:
    assert _shipped_dirs(), "the S8 triage corpus is empty"


@pytest.mark.parametrize("case_dir", _shipped_dirs(), ids=lambda p: p.name)
def test_shipped_triage_case_resolves(
    case_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = load_triage_case(case_dir)
    # Gate-enforced: an eval that measures rejection alone trains the wrong
    # reflex, so every batch guards over-suppression with a must-proceed.
    assert case.known_true, f"{case.id}: no must-proceed finding"
    repo, sha, cleanup = _built(case, monkeypatch)
    try:
        tracked = frozenset(_git(repo, "ls-tree", "-r", "--name-only", sha).split())
        for f in case.findings:
            if f.path:
                assert f.path in tracked, f"{case.id}/{f.finding_id}: anchor {f.path}"
            for r in f.refutation:
                assert r.path in tracked, (
                    f"{case.id}/{f.finding_id}: refutation {r.path}"
                )
                if r.start is not None:
                    n_lines = len(_git(repo, "show", f"{sha}:{r.path}").splitlines())
                    end = r.end if r.end is not None else r.start
                    assert end <= n_lines, (
                        f"{case.id}/{f.finding_id}: {r.spec} past EOF"
                    )
    finally:
        cleanup()  # type: ignore[operator]


# ── lens43-known-good-batch ─────────────────────────────────────────────────

_LENS43_TIP = "41a43c8"  # the pre-squash tip the patch rebuilds (local ref only)


def test_lens43_batch_rebuilds_the_pre_squash_tip_and_its_refutations_hold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = load_triage_case(_SHIPPED / "lens43-known-good-batch")
    repo, sha, cleanup = _built(case, monkeypatch)
    try:
        if _commit_exists(repo, _LENS43_TIP):
            # The rebuilt tree IS the tip the findings were filed against.
            assert _git(repo, "rev-parse", f"{sha}^{{tree}}") == _git(
                repo, "rev-parse", f"{_LENS43_TIP}^{{tree}}"
            )
        by_id = {f.finding_id: f for f in case.findings}
        # f-005: the range holds the return expression that counts the agent.
        r5 = by_id["f-005"].refutation[0]
        lines = _git(repo, "show", f"{sha}:{r5.path}").splitlines()
        assert r5.start is not None and r5.end is not None
        assert any("bool(filters.agent)" in ln for ln in lines[r5.start - 1 : r5.end])
        # ...and the claim's own anchor (the def line) is outside it.
        assert not r5.covers(r5.path, by_id["f-005"].line or 0)
        # f-006: the range holds the errors guard.
        r6 = by_id["f-006"].refutation[0]
        lines = _git(repo, "show", f"{sha}:{r6.path}").splitlines()
        assert r6.start is not None and r6.end is not None
        assert any(
            "if errors or open_snapshot or filters_narrowed" in ln
            for ln in lines[r6.start - 1 : r6.end]
        )
        assert not r6.covers(r6.path, by_id["f-006"].line or 0)
        # The two judgements are declared as such; the two true ones are not.
        assert [f.ambiguous for f in case.findings] == [
            False,
            False,
            True,
            True,
            False,
            False,
        ]
    finally:
        cleanup()  # type: ignore[operator]
