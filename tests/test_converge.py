"""Tests for the converge orchestrator (`converge_pr`) — converge PR 3/3.

`converge_pr` wires three already-tested pieces together: the review intake
(`review_only._review_head` + `_build_report`), the parameterized develop loop
(`develop(entry=LoopEntry(...))`, PR 2), and the guarded fast-forward push
(`push_to_pr_ref`, PR 1). These tests stub all three at the converge boundary
and assert the WIRING: the already-clean short-circuit (no coder, no push), the
LoopEntry seeded from the intake, the push-only-on-approval epilogue, the
fork/merge-race refusals, and — the PR-3 reporting gotcha — that the fixer's
commit count is measured against the PR head, not the merge-base.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from lithos_loom.plugins.story_develop import converge as converge_mod
from lithos_loom.plugins.story_develop import review_only
from lithos_loom.plugins.story_develop.config import DevelopConfig
from lithos_loom.plugins.story_develop.converge import ConvergeResult, converge_pr
from lithos_loom.plugins.story_develop.develop import DevelopResult
from lithos_loom.plugins.story_develop.pr_delivery import (
    ForkPushUnsupported,
    MergeRaceDetected,
)
from lithos_loom.plugins.story_develop.review_resolve import ResolvedChange

_BASE = "b" * 40
_HEAD = "h" * 40


def _change(*, is_fork: bool = False, head_branch: str = "feature") -> ResolvedChange:
    return ResolvedChange(
        base_sha=_BASE,
        head_sha=_HEAD,
        head_ref="#142 (feature)",
        title="A PR",
        body="do the thing",
        head_branch=head_branch,
        is_fork=is_fork,
    )


def _config(tmp_path: Path) -> DevelopConfig:
    return DevelopConfig(
        repo=tmp_path / "repo",
        description="A PR",
        work_dir=tmp_path / "work",
        acceptance_criteria="do the thing",
    )


def _dev_result(
    worktree: Path, *, status: str, branch: str = "converge-x"
) -> DevelopResult:
    return DevelopResult(
        status=status,
        run_id="run1",
        worktree=worktree,
        branch=branch,
        base_sha=_BASE,
        commits=["c1", "c2", "c3"],  # spans the PR's ORIGINAL + fixer commits
        rounds=2,
        handoff_present=True,
        coder_cost_usd=0.0,
        review_cost_usd=0.0,
        message=f"loop ended {status}",
    )


def _install(
    monkeypatch: pytest.MonkeyPatch,
    *,
    blocking: bool,
    panel: object | None = SimpleNamespace(round_reviews=["outcome"]),
    check_set: object | None = "check-set-sentinel",
) -> dict:
    """Stub the collaborators converge wires together; capture their calls."""
    captured: dict = {}

    # IntakeResult.blocking is a property on the real thing; the stub carries it
    # as a plain attribute so the converge branch under test is what we control.
    intake = SimpleNamespace(
        reviewers=["reviewer-state"],
        panel=panel,
        check_set=check_set,
        gate_ledger="ledger",
        blocking=blocking,
    )

    def fake_review_head(config, change, *, reviewer_timeout=3600, keep_worktree=False):
        captured["intake_ran"] = True
        return intake

    monkeypatch.setattr(review_only, "review_head", fake_review_head)

    def fake_develop(config, *, coder_timeout=3600, reviewer_timeout=3600, entry=None):
        captured["entry"] = entry
        wt = config.work_dir / "wt"
        wt.mkdir(parents=True, exist_ok=True)
        return _dev_result(wt, status=captured.get("develop_status", "approved"))

    monkeypatch.setattr(converge_mod, "develop", fake_develop)

    def fake_push(wt, local_branch, remote_ref, *, expected_remote_sha):
        captured["push"] = {
            "wt": wt,
            "local_branch": local_branch,
            "remote_ref": remote_ref,
            "expected_remote_sha": expected_remote_sha,
        }
        if captured.get("push_raises") is not None:
            raise captured["push_raises"]
        return "p" * 40

    monkeypatch.setattr(converge_mod, "push_to_pr_ref", fake_push)

    # deterministic fixer-commit count: only head_sha..HEAD (the fixer's), not
    # the whole merge-base..HEAD span develop() would report.
    def fake_commits_since(wt, base):
        captured["commits_since_base"] = base
        return ["fix1"] if base == _HEAD else ["orig1", "orig2", "fix1"]

    monkeypatch.setattr(converge_mod.git, "commits_since", fake_commits_since)
    return captured


# --- already-clean short-circuit ---------------------------------------------


def test_already_clean_intake_short_circuits(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A non-blocking intake returns already_clean WITHOUT building a coder or
    pushing — the cheapest path for the common re-check."""
    captured = _install(monkeypatch, blocking=False)
    result = converge_pr(_config(tmp_path), _change())
    assert result.status == "already_clean"
    assert result.succeeded
    assert "entry" not in captured  # develop() never called
    assert "push" not in captured  # nothing pushed


# --- blocking intake → loop → push -------------------------------------------


def test_blocking_intake_seeds_loop_and_pushes_on_approval(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A blocking intake enters develop() via a LoopEntry seeded from the intake
    (base = merge-base, reviews = panel.round_reviews, the intake check-set), and
    on approval fast-forward-pushes the fixed branch to the PR head ref."""
    panel = SimpleNamespace(round_reviews=["seed-outcome"])
    captured = _install(monkeypatch, blocking=True, panel=panel, check_set="cs")
    result = converge_pr(_config(tmp_path), _change())

    entry = captured["entry"]
    assert entry is not None
    assert entry.base_override == _BASE  # PR merge-base, not the worktree HEAD
    assert entry.intake_reviews is panel.round_reviews  # seeded from the intake panel
    assert entry.intake_check_set == "cs"
    assert callable(entry.worktree_factory)

    # pushed to the PR head ref, anchored on the PR head sha (never --force)
    assert captured["push"]["remote_ref"] == "feature"
    assert captured["push"]["expected_remote_sha"] == _HEAD
    assert result.status == "converged"
    assert result.pushed is True
    assert result.pushed_sha == "p" * 40


def test_fixer_commit_count_measured_against_pr_head_not_merge_base(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """PR-3 reporting gotcha: converge enters at the PR head with base = the
    merge-base, so develop()'s own `commits` span the PR's ORIGINAL commits too.
    The converge summary must count only the fixer's commits (head_sha..HEAD)."""
    captured = _install(monkeypatch, blocking=True)
    result = converge_pr(_config(tmp_path), _change())
    assert captured["commits_since_base"] == _HEAD  # measured from the PR head
    assert result.fixer_commits == ("fix1",)  # not the 3-commit develop() span


# --- push guards -------------------------------------------------------------


def test_no_push_skips_the_push(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured = _install(monkeypatch, blocking=True)
    result = converge_pr(_config(tmp_path), _change(), no_push=True)
    assert result.status == "converged"
    assert result.pushed is False
    assert "push" not in captured


def test_unapproved_loop_does_not_push(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A loop that stops without approval (max_rounds / disputed / …) leaves the
    fixes in the local worktree and does NOT push un-green code to the PR."""
    captured = _install(monkeypatch, blocking=True)
    captured["develop_status"] = "max_rounds"
    result = converge_pr(_config(tmp_path), _change())
    assert result.status == "not_converged"
    assert not result.succeeded
    assert "push" not in captured
    assert result.fixer_commits == ("fix1",)  # progress is still reported


def test_fork_pr_refused_before_spending_containers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured = _install(monkeypatch, blocking=True)
    result = converge_pr(_config(tmp_path), _change(is_fork=True))
    assert result.status == "fork_unsupported"
    assert "intake_ran" not in captured  # refused pre-loop, no review spend
    assert "entry" not in captured


def test_merge_race_caught_not_force_pushed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured = _install(monkeypatch, blocking=True)
    captured["push_raises"] = MergeRaceDetected("PR head advanced remotely")
    result = converge_pr(_config(tmp_path), _change())
    assert result.status == "merge_race"
    assert not result.succeeded
    assert "advanced remotely" in result.message


def test_fork_push_raised_post_loop_is_surfaced(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # defensive: the pre-loop fork guard should catch forks, but if push still
    # raises ForkPushUnsupported it is surfaced, not crashed on.
    captured = _install(monkeypatch, blocking=True)
    captured["push_raises"] = ForkPushUnsupported("head ref not on origin")
    result = converge_pr(_config(tmp_path), _change())
    assert result.status == "fork_unsupported"
    assert not result.succeeded


def test_intake_panel_missing_is_a_failure_not_a_crash(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A blocking intake whose panel never ran (crash / interrupted before any
    reviewer) can't seed the loop — surface it rather than crash on
    panel.round_reviews."""
    captured = _install(monkeypatch, blocking=True, panel=None)
    result = converge_pr(_config(tmp_path), _change())
    assert result.status == "failed"
    assert "entry" not in captured  # never entered the loop


def test_converge_result_json_is_serialisable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install(monkeypatch, blocking=True)
    result = converge_pr(_config(tmp_path), _change())
    data = result.to_json()
    assert data["status"] == "converged"
    assert data["head_branch"] == "feature"
    assert data["fixer_commits"] == 1
    assert data["pushed"] is True
    assert isinstance(ConvergeResult, type)
