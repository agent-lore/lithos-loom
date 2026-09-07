"""Tests for ``lithos_loom.plugins.story_develop.merge_gate`` (PRD S3, CLI half).

Real git throughout — a bare ``origin`` with ``main`` and the PR branch
``feature`` on it, a work repo tracking both — because the whole point is
merge topology and a leased push. Only the check-set run is stubbed (it
needs containers).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from lithos_loom.plugins.story_develop import merge_gate as mg
from lithos_loom.plugins.story_develop.check_set import (
    Check,
    CheckResult,
    CheckSetResult,
)
from lithos_loom.plugins.story_develop.config import DevelopConfig
from lithos_loom.plugins.story_develop.review_resolve import ResolvedChange
from lithos_loom.plugins.story_develop.test_gate import GateResult
from lithos_loom.runner import git


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


def _commit(repo: Path, name: str, content: str, message: str | None = None) -> str:
    (repo / name).write_text(content)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message or f"add {name}")
    return _git(repo, "rev-parse", "HEAD")


def _remote_sha(bare: Path, ref: str) -> str:
    out = _git(bare, "rev-parse", f"refs/heads/{ref}")
    return out


class Fixture:
    """``origin`` (bare) holds ``main`` at *base_start* and ``feature`` at
    *head*; the work repo has both fetched. ``advance_base`` / ``conflict``
    move ``origin/main`` on."""

    def __init__(self, tmp_path: Path) -> None:
        self.bare = tmp_path / "origin.git"
        subprocess.run(
            ["git", "init", "--bare", "-q", "-b", "main", str(self.bare)], check=True
        )
        self.repo = tmp_path / "repo"
        self.repo.mkdir()
        _git(self.repo, "init", "-q", "-b", "main")
        _git(self.repo, "config", "user.email", "t@example.com")
        _git(self.repo, "config", "user.name", "T")
        _git(self.repo, "remote", "add", "origin", str(self.bare))
        (self.repo / "shared.txt").write_text("v0\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", "init")
        self.base_start = _git(self.repo, "rev-parse", "HEAD")
        _git(self.repo, "switch", "-q", "-c", "feature")
        (self.repo / "shared.txt").write_text("story\n")
        self.head = _commit(self.repo, "own.txt", "story work\n", "story: own change")
        _git(self.repo, "switch", "-q", "main")
        _git(self.repo, "push", "-q", "origin", "main", "feature")
        self.work_dir = tmp_path / "work"

    def advance_base(self, *, conflict: bool = False) -> str:
        _git(self.repo, "switch", "-q", "main")
        if conflict:
            (self.repo / "shared.txt").write_text("base\n")
        tip = _commit(self.repo, "base.txt", "landed PR\n", "base: other PR landed")
        _git(self.repo, "push", "-q", "origin", "main")
        _git(self.repo, "fetch", "-q", "origin")
        return tip

    def change(self, *, is_fork: bool = False) -> ResolvedChange:
        return ResolvedChange(
            base_sha=self.base_start,
            head_sha=self.head,
            head_ref="#7 (feature)",
            title="A PR",
            body="do the thing",
            head_branch="feature",
            base_ref="origin/main",
            is_fork=is_fork,
        )

    def config(self, **overrides) -> DevelopConfig:
        return DevelopConfig(
            repo=self.repo,
            description="merge-gate #7",
            work_dir=self.work_dir,
            **overrides,
        )


@pytest.fixture
def fx(tmp_path: Path) -> Fixture:
    return Fixture(tmp_path)


_TEST = Check(name="test", command="make test", state="required")
_LINT = Check(name="lint", command="ruff check", state="informational")


def _stub_checks(
    monkeypatch: pytest.MonkeyPatch,
    *,
    checks: tuple[Check, ...] = (_TEST, _LINT),
    passed: bool = True,
    infra_error: bool = False,
) -> dict:
    captured: dict = {}

    def fake_build(config, wt):
        captured["build_wt"] = wt
        return checks

    def fake_run(config, wt, sha, round_no, chks, ledger=None):
        captured["run"] = {"wt": wt, "sha": sha, "round": round_no, "checks": chks}
        if infra_error:
            return None
        results = []
        for c in chks:
            ok = passed or c.name != "test"
            results.append(
                CheckResult(
                    check=c,
                    execution_outcome="ran",
                    gate=GateResult(
                        command=c.command,
                        exit_code=0 if ok else 1,
                        passed=ok,
                        output_tail="2 failed" if not ok else "ok",
                    ),
                )
            )
        return CheckSetResult(results=tuple(results))

    monkeypatch.setattr(mg, "build_check_set", fake_build)
    monkeypatch.setattr(mg, "run_check_set", fake_run)
    return captured


def _worktrees(repo: Path) -> list[str]:
    return [line.split()[0] for line in _git(repo, "worktree", "list").splitlines()[1:]]


def _branches(repo: Path) -> set[str]:
    return {
        b.strip().lstrip("* ").strip()
        for b in _git(repo, "branch", "--list").splitlines()
    }


# ── up to date ───────────────────────────────────────────────────────────────


def test_up_to_date_pr_gates_its_own_head_and_never_pushes(
    fx: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    cap = _stub_checks(monkeypatch)
    result = mg.run_merge_gate(fx.config(), fx.change())

    assert result.status == "green"
    assert result.behind is False
    assert result.merge_sha == fx.head  # nothing to merge: the gated tree IS the head
    assert result.base_sha == fx.base_start
    assert result.head_sha == fx.head
    assert cap["run"]["sha"] == fx.head
    assert result.pushed is False and result.pushed_sha == ""
    assert _remote_sha(fx.bare, "feature") == fx.head
    assert [c.name for c in result.checks] == ["test", "lint"]
    assert result.verdict == "GREEN"


# ── behind, clean ────────────────────────────────────────────────────────────


def test_behind_and_green_pushes_the_merge_commit_to_the_pr_branch(
    fx: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    tip = fx.advance_base()
    cap = _stub_checks(monkeypatch)

    result = mg.run_merge_gate(fx.config(), fx.change())

    assert result.status == "green" and result.behind is True
    assert result.base_sha == tip
    # the gated tree is the merge result, not the PR head
    assert result.merge_sha != fx.head and cap["run"]["sha"] == result.merge_sha
    parents = _git(fx.repo, "log", "-1", "--format=%P", result.merge_sha).split()
    assert parents == [fx.head, tip]  # append-only: head first, base second
    # ...and it was pushed onto the PR branch, append-only
    assert result.pushed is True and result.pushed_sha == result.merge_sha
    assert _remote_sha(fx.bare, "feature") == result.merge_sha
    assert git.is_ancestor(fx.repo, fx.head, result.merge_sha)
    assert result.conflicting_paths == ()


def test_no_push_gates_but_leaves_the_pr_branch_alone(
    fx: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    fx.advance_base()
    _stub_checks(monkeypatch)
    result = mg.run_merge_gate(fx.config(), fx.change(), push=False)
    assert result.status == "green" and result.behind is True
    assert result.pushed is False
    assert _remote_sha(fx.bare, "feature") == fx.head


def test_behind_and_red_reports_the_failing_check_and_does_not_push(
    fx: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    fx.advance_base()
    _stub_checks(monkeypatch, passed=False)
    result = mg.run_merge_gate(fx.config(), fx.change())
    assert result.status == "red"
    assert result.verdict == "RED"
    failing = [c for c in result.checks if not c.passed]
    assert [c.name for c in failing] == ["test"]
    assert failing[0].output_tail == "2 failed"
    assert result.pushed is False
    assert _remote_sha(fx.bare, "feature") == fx.head
    assert "test" in result.message


def test_check_set_infra_error_is_errored_not_green(
    fx: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    fx.advance_base()
    _stub_checks(monkeypatch, infra_error=True)
    result = mg.run_merge_gate(fx.config(), fx.change())
    assert result.status == "errored"
    assert result.verdict is None
    assert result.pushed is False  # an unverified merge is never pushed


def test_empty_check_set_is_reported_as_no_checks_and_not_pushed(
    fx: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    fx.advance_base()
    cap = _stub_checks(monkeypatch, checks=())
    result = mg.run_merge_gate(fx.config(), fx.change())
    assert result.status == "no_checks"
    assert "run" not in cap  # nothing to run
    assert result.checks == ()
    # a vacuous gate proves nothing — the update is not pushed on its strength
    assert result.pushed is False


# ── conflict ─────────────────────────────────────────────────────────────────


def test_conflict_names_the_paths_and_skips_the_check_set(
    fx: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    tip = fx.advance_base(conflict=True)
    cap = _stub_checks(monkeypatch)
    result = mg.run_merge_gate(fx.config(), fx.change())
    assert result.status == "conflict"
    assert result.behind is True
    assert result.conflicting_paths == ("shared.txt",)
    assert result.base_sha == tip and result.head_sha == fx.head
    assert "run" not in cap  # no gate run on a tree that does not exist
    assert result.merge_sha == ""
    assert result.pushed is False
    assert _remote_sha(fx.bare, "feature") == fx.head


# ── refusals / push failures ─────────────────────────────────────────────────


def test_fork_pr_is_refused_before_any_git_work(
    fx: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    cap = _stub_checks(monkeypatch)
    result = mg.run_merge_gate(fx.config(), fx.change(is_fork=True))
    assert result.status == "fork_unsupported"
    assert "build_wt" not in cap
    assert _worktrees(fx.repo) == []


def test_remote_head_moved_since_resolve_is_a_push_race_not_a_gate_failure(
    fx: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    fx.advance_base()
    _stub_checks(monkeypatch)
    # someone pushes to the PR branch between the resolve and our push
    _git(fx.repo, "switch", "-q", "feature")
    moved = _commit(fx.repo, "late.txt", "human push\n", "human: late fix")
    _git(fx.repo, "push", "-q", "origin", "feature")
    _git(fx.repo, "switch", "-q", "main")

    result = mg.run_merge_gate(fx.config(), fx.change())  # change still names fx.head
    assert result.status == "green"  # the gate verdict stands
    assert result.pushed is False
    assert "advanced remotely" in result.push_error
    assert _remote_sha(fx.bare, "feature") == moved  # untouched


# ── hygiene ──────────────────────────────────────────────────────────────────


def test_throwaway_worktree_and_branch_are_removed_after_the_run(
    fx: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    fx.advance_base()
    _stub_checks(monkeypatch)
    before = _branches(fx.repo)
    result = mg.run_merge_gate(fx.config(), fx.change())
    assert result.worktree is None
    assert _worktrees(fx.repo) == []
    assert _branches(fx.repo) == before
    # the merge commit object survives for the pushed ref to point at
    assert _git(fx.repo, "cat-file", "-t", result.merge_sha) == "commit"


def test_conflict_also_cleans_up(fx: Fixture, monkeypatch: pytest.MonkeyPatch) -> None:
    fx.advance_base(conflict=True)
    _stub_checks(monkeypatch)
    before = _branches(fx.repo)
    mg.run_merge_gate(fx.config(), fx.change())
    assert _worktrees(fx.repo) == []
    assert _branches(fx.repo) == before


def test_keep_worktree_retains_the_merged_tree_for_inspection(
    fx: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    fx.advance_base()
    _stub_checks(monkeypatch)
    result = mg.run_merge_gate(fx.config(), fx.change(), push=False, keep_worktree=True)
    assert result.worktree is not None and result.worktree.is_dir()
    assert _git(result.worktree, "rev-parse", "HEAD") == result.merge_sha
    assert (result.worktree / "base.txt").exists()


# ── fingerprint + JSON ───────────────────────────────────────────────────────


def test_config_fingerprint_tracks_the_resolved_check_set_and_image(
    fx: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_checks(monkeypatch)
    a = mg.run_merge_gate(fx.config(), fx.change(), push=False)
    b = mg.run_merge_gate(fx.config(), fx.change(), push=False)
    assert a.config_fingerprint == b.config_fingerprint
    assert len(a.config_fingerprint) == 16
    c = mg.run_merge_gate(fx.config(image="other:img"), fx.change(), push=False)
    assert c.config_fingerprint != a.config_fingerprint
    _stub_checks(monkeypatch, checks=(_TEST,))
    d = mg.run_merge_gate(fx.config(), fx.change(), push=False)
    assert d.config_fingerprint != a.config_fingerprint


def test_to_json_is_a_stable_flat_record(
    fx: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    tip = fx.advance_base()
    _stub_checks(monkeypatch)
    result = mg.run_merge_gate(fx.config(), fx.change())
    data = json.loads(json.dumps(result.to_json()))
    assert data["status"] == "green"
    assert data["head_ref"] == "#7 (feature)"
    assert data["head_branch"] == "feature"
    assert data["base_ref"] == "origin/main"
    assert data["base_sha"] == tip and data["head_sha"] == fx.head
    assert data["merge_sha"] == result.merge_sha
    assert data["behind"] is True
    assert data["conflicting_paths"] == []
    assert data["verdict"] == "GREEN"
    assert data["pushed"] is True and data["pushed_sha"] == result.merge_sha
    assert data["push_error"] == ""
    assert data["config_fingerprint"] == result.config_fingerprint
    assert data["checks"][0] == {
        "name": "test",
        "command": "make test",
        "state": "required",
        "stage": "fast",
        "outcome": "ran",
        "passed": True,
        "exit_code": 0,
        "timed_out": False,
        "output_tail": "ok",
    }
