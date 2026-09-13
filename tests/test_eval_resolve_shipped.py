"""Preflight for the shipped resolve fixtures (evals/resolve/cases).

Every shipped case loads (hermetic). Where the — possibly sibling — checkout
is present: the three trees build; the base is not already in the head; the
merge S5 would receive conflicts in at least one path, every one a text
conflict carrying markers (the only shape the mode resolves); and the
oracle discriminates its own controls — every probe passes the known-good
tree, at least one fails the known-bad (this half needs the project's
toolchain on the host, like a live run). Skips with a reason otherwise (CI
has no sibling lens). Case-specific pins follow.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from lithos_loom.evals.resolve.case import ResolveCase, load_resolve_case
from lithos_loom.evals.resolve.harness import Trees, materialise_trees, run_probe
from lithos_loom.runner import git, worktree

_SHIPPED = Path(__file__).resolve().parents[1] / "evals" / "resolve" / "cases"


def _shipped_dirs() -> list[Path]:
    if not _SHIPPED.is_dir():
        return []
    return sorted(d for d in _SHIPPED.iterdir() if (d / "case.toml").is_file())


def _commit_exists(repo: Path, sha: str) -> bool:
    return (
        subprocess.run(
            ["git", "-C", str(repo), "cat-file", "-e", f"{sha}^{{commit}}"],
            capture_output=True,
        ).returncode
        == 0
    )


def _tree(repo: Path, sha: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", f"{sha}^{{tree}}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


@pytest.fixture
def built(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[ResolveCase, Path, Trees]]:
    """``(case, repo, trees)`` for a shipped case, or skip where it can't run."""
    case = load_resolve_case(request.param)
    repo = Path(case.repo).resolve()
    if not (repo / ".git").exists():
        pytest.skip(f"repo {case.repo!r} is not a git checkout here")
    for sha in (case.merge_base, case.base):
        if not _commit_exists(repo, sha):
            pytest.skip(f"{sha[:12]} not present in {case.repo} (shallow clone?)")
    monkeypatch.setenv("GIT_AUTHOR_NAME", "loom-eval-preflight")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "loom-eval-preflight@localhost")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "loom-eval-preflight")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "loom-eval-preflight@localhost")
    trees, cleanup = materialise_trees(case)
    try:
        yield case, repo, trees
    finally:
        cleanup()


def test_every_shipped_case_loads() -> None:
    dirs = _shipped_dirs()
    assert dirs, f"no shipped cases under {_SHIPPED}"
    for d in dirs:
        case = load_resolve_case(d)
        assert case.id == d.name


@pytest.mark.parametrize("built", _shipped_dirs(), ids=lambda d: d.name, indirect=True)
def test_the_merge_conflicts_in_text_paths_only(
    built: tuple[ResolveCase, Path, Trees],
) -> None:
    case, repo, trees = built
    assert not git.is_ancestor(repo, case.base, trees.head), (
        "the base is already in the head"
    )
    parent = Path(tempfile.mkdtemp(prefix="loom-eval-resolve-preflight-"))
    wt = worktree.create_at(repo, trees.head, "preflight", parent=parent)
    try:
        paths = git.merge_no_commit(wt, case.base)
        assert paths, "the merge is clean — nothing for S5 to resolve"
        assert git.unmerged_paths(wt) == paths
        assert sorted(git.conflict_markers(wt, paths)) == sorted(paths), (
            "a conflicted path without markers is a shape the mode cannot resolve"
        )
        git.abort_merge(wt)
    finally:
        worktree.remove(wt, force=True)
        shutil.rmtree(parent, ignore_errors=True)


@pytest.mark.parametrize("built", _shipped_dirs(), ids=lambda d: d.name, indirect=True)
def test_the_oracle_discriminates_its_controls(
    built: tuple[ResolveCase, Path, Trees],
) -> None:
    case, _repo, trees = built
    if shutil.which("uv") is None:
        pytest.skip("the shipped probes run under uv")
    good = [run_probe(case, trees.known_good, p) for p in case.probes]
    assert all(r.passed for r in good), [
        (r.name, r.exit_code, r.output[-400:], r.error) for r in good
    ]
    bad = [run_probe(case, trees.known_bad, p) for p in case.probes]
    assert not all(r.passed for r in bad), [
        (r.name, r.exit_code, r.output[-400:]) for r in bad
    ]


# --- lens43-projects-merge ----------------------------------------------------

_LENS43_DELIVERED = "d53126ae2132f56dc3cf9354afa1925b6518456a"
_LENS43_OPERATOR_MERGE = "e1965aab1aa985f029616c32f8ae6cb245609441"
_LENS43_HELPER = "def filters_narrow_the_board"
_LENS43_TERM = "or bool(filters.projects)"


@pytest.mark.parametrize(
    "built", [_SHIPPED / "lens43-projects-merge"], ids=["lens43"], indirect=True
)
def test_lens43_fixture_pins_the_real_conflict(
    built: tuple[ResolveCase, Path, Trees],
) -> None:
    # The S8 conflict-resolution seed: the delivered #43 head vs lens main
    # after #44/#45; the operator's merge is the known-good, the composed-
    # projects defect head the known-bad. Pinned: the rebuilt trees ARE the
    # real commits' (where the checkout has them); the helper merges cleanly
    # (it sits in no conflicted hunk — the defect is in the RELATION to the
    # base's new filter, not in the markers); the term separates the controls.
    case, repo, trees = built
    if _commit_exists(repo, _LENS43_DELIVERED):
        assert _tree(repo, trees.head) == _tree(repo, _LENS43_DELIVERED)
    if _commit_exists(repo, _LENS43_OPERATOR_MERGE):
        assert _tree(repo, trees.known_good) == _tree(repo, _LENS43_OPERATOR_MERGE)
    parent = Path(tempfile.mkdtemp(prefix="loom-eval-resolve-preflight-"))
    wt = worktree.create_at(repo, trees.head, "preflight-lens43", parent=parent)
    try:
        paths = git.merge_no_commit(wt, case.base)
        assert len(paths) == 10
        assert "src/lithos_lens/task_filtering.py" not in paths
        merged_tasks = (wt / "src/lithos_lens/tasks.py").read_text(encoding="utf-8")
        helper_at = merged_tasks.index(_LENS43_HELPER)
        helper = merged_tasks[helper_at : helper_at + 900]
        assert "<<<<<<<" not in helper and _LENS43_TERM not in helper
        git.abort_merge(wt)
    finally:
        worktree.remove(wt, force=True)
        shutil.rmtree(parent, ignore_errors=True)
    for sha, present in ((trees.known_good, True), (trees.known_bad, False)):
        blob = subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "show",
                f"{sha}:src/lithos_lens/task_filtering.py",
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        assert _LENS43_HELPER in blob
        assert (_LENS43_TERM in blob) is present
