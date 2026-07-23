"""Tests for the converge orchestrator (`converge_pr`) — converge PR 3/3.

`converge_pr` wires three already-tested pieces together: the review intake
(`review_only.review_head` + `IntakeResult.blocking`), the parameterized develop
loop (`develop(entry=LoopEntry(...))`, PR 2), and the guarded fast-forward push
(`push_to_pr_ref`, PR 1). These tests stub all three at the converge boundary
and assert the WIRING: the already-clean short-circuit (no coder, no push), the
incomplete-intake failure, the LoopEntry seeded from the intake, the intake
run_id isolation, the whole-command budget, the push-only-on-approval epilogue,
the fork/merge-race refusals, and — the PR-3 reporting gotcha — that the fixer's
commit count is measured against the PR head, not the merge-base.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from lithos_loom.plugins.story_develop import converge as converge_mod
from lithos_loom.plugins.story_develop import review_only
from lithos_loom.plugins.story_develop.config import DevelopConfig
from lithos_loom.plugins.story_develop.converge import converge_pr
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
        coder_cost_usd=0.6,  # nonzero loop spend so total_cost = intake + loop
        review_cost_usd=0.4,
        message=f"loop ended {status}",
    )


_UNSET = object()


def _install(
    monkeypatch: pytest.MonkeyPatch,
    *,
    blocking: bool,
    incomplete: bool = False,
    panel: object = _UNSET,
    check_set: object | None = "check-set-sentinel",
    intake_cost: float = 0.0,
) -> dict:
    """Stub the collaborators converge wires together; capture their calls."""
    captured: dict = {}

    if panel is _UNSET:
        panel = SimpleNamespace(round_reviews=["outcome"], cost=intake_cost)
    # IntakeResult.blocking / .incomplete are properties on the real thing; the
    # stub carries them as plain attributes so the converge branch under test is
    # what we control (the properties themselves are tested in test_review_only).
    intake = SimpleNamespace(
        reviewers=["reviewer-state"],
        panel=panel,
        check_set=check_set,
        gate_ledger="ledger",
        blocking=blocking,
        incomplete=incomplete,
    )

    def fake_review_head(config, change, *, reviewer_timeout=3600, keep_worktree=False):
        captured["intake_ran"] = True
        captured["intake_config"] = config
        return intake

    monkeypatch.setattr(review_only, "review_head", fake_review_head)

    def fake_develop(config, *, coder_timeout=3600, reviewer_timeout=3600, entry=None):
        captured["entry"] = entry
        captured["loop_config"] = config
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
    panel = SimpleNamespace(round_reviews=["seed-outcome"], cost=0.0)
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


def test_incomplete_intake_is_failed_not_seeded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An incomplete intake panel (interrupted / invalid / absent) has no
    trustworthy review to seed the loop — converge stops with `failed` rather
    than fixing against a partial/absent review (finding #2)."""
    captured = _install(monkeypatch, blocking=True, incomplete=True, intake_cost=0.4)
    result = converge_pr(_config(tmp_path), _change())
    assert result.status == "failed"
    assert not result.succeeded
    assert "entry" not in captured  # never entered the loop
    assert result.intake_cost_usd == 0.4  # the intake spend is still reported


# --- artifact isolation (finding #1) -----------------------------------------


def test_intake_runs_under_a_distinct_run_id_from_the_loop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Intake and the fix loop must NOT share a run_id — their round-1 handoff /
    gate-export dirs (all run_id-derived) would otherwise collide, letting the
    PR-head export / stale reviewer handoff bleed into the fixed-tree gate + panel
    (finding #1)."""
    captured = _install(monkeypatch, blocking=True)
    config = _config(tmp_path)
    converge_pr(config, _change())
    intake_run_id = captured["intake_config"].run_id
    loop_run_id = captured["loop_config"].run_id
    assert intake_run_id != loop_run_id
    assert intake_run_id == f"{config.run_id}-intake"
    assert loop_run_id == config.run_id  # the loop keeps the caller's run_id


# --- --max-cost covers the whole command (finding #3) ------------------------


def test_intake_cost_is_carried_into_the_loop_budget(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """--max-cost bounds the WHOLE command: the loop's ceiling is reduced by the
    intake spend, so total spend can't exceed the operator's declared budget."""
    captured = _install(monkeypatch, blocking=True, intake_cost=2.0)
    config = dataclasses.replace(_config(tmp_path), max_cost_usd=10.0)
    converge_pr(config, _change())
    assert captured["loop_config"].max_cost_usd == 8.0  # 10 - 2 intake


def test_intake_exhausting_the_budget_stops_before_the_loop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured = _install(monkeypatch, blocking=True, intake_cost=6.0)
    config = dataclasses.replace(_config(tmp_path), max_cost_usd=5.0)
    result = converge_pr(config, _change())
    assert result.status == "failed"
    assert "entry" not in captured  # never built a coder
    assert result.intake_cost_usd == 6.0


def test_already_clean_intake_exhausting_budget_is_failed_not_clean(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A CLEAN intake that alone meets --max-cost is `failed`, not `already_clean`
    — the budget is checked before the clean/blocking split so a clean intake
    can't bypass the whole-command budget contract (finding #2)."""
    captured = _install(monkeypatch, blocking=False, intake_cost=6.0)
    config = dataclasses.replace(_config(tmp_path), max_cost_usd=5.0)
    result = converge_pr(config, _change())
    assert result.status == "failed"
    assert not result.succeeded
    assert "entry" not in captured
    assert result.intake_cost_usd == 6.0


def test_converge_pr_rejects_invalid_numeric_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """converge_pr validates its numeric bounds at the API boundary (not only the
    CLI), so a future daemon caller fails fast instead of spending on intake."""
    _install(monkeypatch, blocking=True)
    with pytest.raises(ValueError, match="max_cost_usd"):
        converge_pr(dataclasses.replace(_config(tmp_path), max_cost_usd=0.0), _change())
    with pytest.raises(ValueError, match="max_rounds"):
        converge_pr(dataclasses.replace(_config(tmp_path), max_rounds=0), _change())


def test_unlimited_budget_leaves_the_loop_ceiling_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured = _install(monkeypatch, blocking=True, intake_cost=3.0)
    converge_pr(_config(tmp_path), _change())  # max_cost_usd defaults to None
    assert captured["loop_config"].max_cost_usd is None


def test_converge_result_json_round_trips_the_documented_shape(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install(monkeypatch, blocking=True, intake_cost=1.5)
    result = converge_pr(_config(tmp_path), _change())
    # actually serialise (the old test never called json.dumps) and pin the shape
    data = json.loads(json.dumps(result.to_json()))
    assert data == {
        "status": "converged",
        "head_ref": "#142 (feature)",
        "head_branch": "feature",
        "base_sha": _BASE,
        "head_sha": _HEAD,
        "rounds": 2,
        "develop_status": "approved",
        "fixer_commits": 1,
        "pushed": True,
        "pushed_sha": "p" * 40,
        "intake_cost_usd": 1.5,
        "total_cost_usd": 2.5,  # 1.5 intake + 1.0 loop (0.6 coder + 0.4 review)
        "message": "converged and pushed to feature",
    }
