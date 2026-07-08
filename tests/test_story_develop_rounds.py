"""Unit tests for the shared round primitives in ``rounds.py``.

The full ``develop()`` round pipeline (``CycleExit`` / ``RoundContext`` / the phase
functions / ``run_round``) is characterised end-to-end by
``test_story_develop_core.py``; this file pins the small shared primitives other
modules drive directly — today ``commit_round`` (ARCH-1.S7), which both
``develop()``'s ``commit_phase`` and ``pr_delivery``'s Copilot fix round call so
the handoff-dir exclusion is single-sourced on ``HANDOFF_DIRNAME``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from lithos_loom.plugins.story_develop.config import HANDOFF_DIRNAME
from lithos_loom.plugins.story_develop.rounds import commit_round


def _init_repo(path: Path) -> None:
    def g(*args: str) -> None:
        subprocess.run(["git", *args], cwd=path, check=True, capture_output=True)

    path.mkdir(parents=True, exist_ok=True)
    g("init", "-q")
    g("config", "user.email", "t@t")
    g("config", "user.name", "t")
    g("commit", "--allow-empty", "-q", "-m", "root")


def _tracked_at_head(repo: Path) -> list[str]:
    out = subprocess.run(
        ["git", "show", "--name-only", "--format=", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return out.stdout.split()


def test_commit_round_commits_work_but_excludes_the_handoff_dir(tmp_path: Path) -> None:
    repo = tmp_path / "wt"
    _init_repo(repo)
    (repo / "src.py").write_text("print('work')\n", encoding="utf-8")
    handoff_dir = repo / HANDOFF_DIRNAME
    handoff_dir.mkdir()
    (handoff_dir / "round_01_coder_done.md").write_text(
        "## Status: LGTM\n", encoding="utf-8"
    )

    sha = commit_round(repo, "story-develop r1: do the thing")

    assert sha is not None
    tracked = _tracked_at_head(repo)
    assert "src.py" in tracked
    # the handoff scaffolding must never reach the deliverable commit
    assert not any(HANDOFF_DIRNAME in t for t in tracked)


def test_commit_round_returns_none_when_only_excluded_work_is_present(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "wt"
    _init_repo(repo)
    # only a handoff dir present -> excluded -> nothing staged -> no commit
    handoff_dir = repo / HANDOFF_DIRNAME
    handoff_dir.mkdir()
    (handoff_dir / "x.md").write_text("x\n", encoding="utf-8")

    assert commit_round(repo, "empty round") is None


def test_handoff_dirname_matches_the_legacy_delivery_literal() -> None:
    # ARCH-1.S7: pr_delivery's Copilot-round commit hardcoded exclude=[".handoff"]
    # while develop used HANDOFF_DIRNAME (accidental drift). Both now route through
    # commit_round(exclude=[HANDOFF_DIRNAME]); pin the constant to the value the
    # literal carried so the drift-fix stays behaviour-preserving.
    assert HANDOFF_DIRNAME == ".handoff"
