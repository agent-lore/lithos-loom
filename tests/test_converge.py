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
import subprocess
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


def _change(
    *,
    is_fork: bool = False,
    is_merged: bool = False,
    head_branch: str = "feature",
) -> ResolvedChange:
    return ResolvedChange(
        base_sha=_BASE,
        head_sha=_HEAD,
        head_ref="#142 (feature)",
        base_ref="origin/main",
        title="A PR",
        body="do the thing",
        head_branch=head_branch,
        is_fork=is_fork,
        is_merged=is_merged,
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
        host_action=_HOST_ACTION if status == "infra_failed" else "",
    )


_HOST_ACTION = "re-authenticate the agent CLI on the host, then complete the gate"


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
        return dataclasses.replace(
            _dev_result(wt, status=captured.get("develop_status", "approved")),
            rounds=captured.get("develop_rounds", 2),
            failure_reason=captured.get("develop_failure_reason", ""),
        )

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
        if captured.get("no_fixer_commits"):
            return []  # the loop committed nothing (#380)
        return ["fix1"] if base == _HEAD else ["orig1", "orig2", "fix1"]

    monkeypatch.setattr(converge_mod.git, "commits_since", fake_commits_since)

    # #387: the objective "did the tree move" read behind a `fixed` claim —
    # the fake worktree is no git repo, so answer from the script (default:
    # it moved).
    def fake_tree_differs(wt, a, b, *, exclude=()):
        captured["tree_differs"] = (a, b, tuple(exclude))
        return captured.get("tree_changed", True)

    monkeypatch.setattr(converge_mod.git, "tree_differs", fake_tree_differs)
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
    # PR merge-base (not the worktree HEAD) + the live base ref, so a base merge
    # during the run moves the fork point (S5c)
    assert entry.base_override == converge_mod.git.RangeBase(_BASE, "origin/main")
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


def test_merged_pr_refused_before_spending_containers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A merged PR has nothing to converge and any fix is unlandable.

    Observed 2026-08-27: converge ran 5 rounds and 6 fixer commits against an
    already-merged PR for $29.78 and pushed nothing, because nothing checked.
    Refuse BEFORE the intake, which is where the spend starts.
    """
    captured = _install(monkeypatch, blocking=True)
    result = converge_pr(_config(tmp_path), _change(is_merged=True))
    assert result.status == "merged"
    assert not result.succeeded
    assert "already merged" in result.message
    assert "intake_ran" not in captured  # refused pre-intake, no review spend
    assert "entry" not in captured


def test_merged_check_precedes_the_fork_check(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A merged fork PR reports `merged`, the more fundamental refusal —
    "push it from your fork" is useless advice for a PR that already landed."""
    _install(monkeypatch, blocking=True)
    result = converge_pr(_config(tmp_path), _change(is_merged=True, is_fork=True))
    assert result.status == "merged"


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


def _deferring_panel(cost: float) -> SimpleNamespace:
    """An intake panel whose one review deferred a finding out-of-scope."""
    finding = SimpleNamespace(
        finding_id="f-001",
        severity="major",
        status="out-of-scope",
        rationale="Button text overlaps the icon",
        files=["ui.py:10"],
        deferral_reason="pre-existing on the base",
    )
    outcome = SimpleNamespace(reviewer="correctness", findings=[finding])
    return SimpleNamespace(round_reviews=[outcome], cost=cost)


def test_incomplete_intake_still_carries_deferrals(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """PR #342 re-review P2: one reviewer can defer a finding before another
    fails the intake — the deferral already happened, and the `failed` exit
    must surface it or it is lost (converge spawns no follow-up tasks)."""
    _install(
        monkeypatch,
        blocking=True,
        incomplete=True,
        panel=_deferring_panel(0.4),
        intake_cost=0.4,
    )
    result = converge_pr(_config(tmp_path), _change())
    assert result.status == "failed"
    (d,) = result.deferred_findings
    assert d.reviewer == "correctness"
    assert d.rationale == "Button text overlaps the icon"
    assert d.deferral_reason == "pre-existing on the base"


def test_budget_exhausted_intake_still_carries_deferrals(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """PR #342 re-review P2: a COMPLETED intake that exhausts --max-cost exits
    `failed` — but its review happened in full, deferrals included."""
    _install(monkeypatch, blocking=True, panel=_deferring_panel(6.0), intake_cost=6.0)
    config = dataclasses.replace(_config(tmp_path), max_cost_usd=5.0)
    result = converge_pr(config, _change())
    assert result.status == "failed"
    (d,) = result.deferred_findings
    assert d.finding_id == "f-001"
    assert d.deferral_reason == "pre-existing on the base"


def test_already_clean_intake_carries_deferrals(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A deferral is exactly what can make a blocking finding non-blocking —
    the `already_clean` short-circuit must still report it."""
    _install(monkeypatch, blocking=False, panel=_deferring_panel(0.4), intake_cost=0.4)
    result = converge_pr(_config(tmp_path), _change())
    assert result.status == "already_clean"
    (d,) = result.deferred_findings
    assert d.rationale == "Button text overlaps the icon"
    assert d.deferral_reason == "pre-existing on the base"


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
    # NaN compares False against everything (`nan <= 0` AND every later budget
    # comparison), so it would silently behave as an unlimited ceiling rendered
    # as $nan; inf is equally nonsensical. Both must fail fast (Copilot #272).
    for bad in (float("nan"), float("inf")):
        with pytest.raises(ValueError, match="max_cost_usd"):
            converge_pr(
                dataclasses.replace(_config(tmp_path), max_cost_usd=bad), _change()
            )


def test_converge_pr_rejects_change_without_a_pushable_branch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Same fail-fast boundary for the change itself: a range/branch-resolved
    change (no head branch to push to) must be refused BEFORE spending on intake,
    not die post-loop with a misleading fork error on an empty ref."""
    stubs = _install(monkeypatch, blocking=True)
    with pytest.raises(ValueError, match="pushable head branch"):
        converge_pr(_config(tmp_path), _change(head_branch=""))
    assert "intake_config" not in stubs  # refused before any review spend


def test_max_cost_is_soft_an_approved_result_over_ceiling_is_delivered(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """--max-cost is a SOFT phase-boundary ceiling, not a hard cap: an approved
    result whose total spend exceeds it is still converged + pushed (approval wins
    over a cost overrun, as in the develop loop) — finding #2. Here intake $4.5 +
    loop $1.0 = $5.5 > the $5 ceiling, yet the run is delivered."""
    captured = _install(monkeypatch, blocking=True, intake_cost=4.5)
    config = dataclasses.replace(_config(tmp_path), max_cost_usd=5.0)
    result = converge_pr(config, _change())
    assert result.status == "converged"
    assert result.pushed is True
    assert result.total_cost_usd > 5.0  # exceeded the ceiling but still delivered
    assert "push" in captured


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
    # #412: the RUN's own branch + worktree (where a coder's commits live on
    # an infra_failed exit) ride beside the PR's head_branch (where they do
    # not, until a push)
    assert result.develop_result is not None
    assert data.pop("branch") == result.develop_result.branch
    assert data.pop("worktree") == str(result.develop_result.worktree)
    assert data == {
        "deferred_findings": [],  # 819370e5: out-of-scope deferrals (none here)
        "status": "converged",
        "conflict": None,  # resolve mode only (PRD S5)
        "succeeded": True,
        "head_ref": "#142 (feature)",
        "head_branch": "feature",
        "base_sha": _BASE,
        "head_sha": _HEAD,
        "rounds": 2,
        "develop_status": "approved",
        "host_action": "",
        "fixer_commits": 1,
        "pushed": True,
        "external_outcomes": [],
        "pushed_sha": "p" * 40,
        "intake_cost_usd": 1.5,
        "total_cost_usd": 2.5,  # 1.5 intake + 1.0 loop (0.6 coder + 0.4 review)
        "message": "converged and pushed to feature",
    }


# --- external mode (PRD S2 slice B: triage-then-inject, no local intake) -----


def _ext_finding(comment_id: int = 7, body: str = "leaks a handle"):
    from lithos_loom.github_review_activity import ReviewStream
    from lithos_loom.github_review_streams import ReplyMode
    from lithos_loom.plugins.story_develop.external_reviews import ExternalFinding

    return ExternalFinding(
        author="dave",
        source="human",
        trusted=True,
        stream=ReviewStream.INLINE,
        activity_id=comment_id,
        reply_mode=ReplyMode.THREAD,
        thread_url=f"https://example/thread/{comment_id}",
        head_sha=_HEAD,
        path="src/x.py",
        line=12,
        body=body,
    )


def _install_triage(
    monkeypatch: pytest.MonkeyPatch,
    captured: dict,
    *,
    proceed: tuple[str, ...],
    rejections: dict[str, str] | None = None,
    nothing_to_remediate: dict[str, str] | None = None,
    cost: float = 0.1,
):
    from lithos_loom.plugins.story_develop.external_triage import TriageVerdicts

    def fake_triage(config, change, outcome, *, approval_eligible=None, timeout=1800):
        captured["triage_findings"] = [f.finding_id for f in outcome.findings]
        captured["approval_eligible"] = approval_eligible
        return TriageVerdicts(
            proceed=proceed,
            rejections=rejections or {},
            nothing_to_remediate=nothing_to_remediate or {},
            cost_usd=cost,
        )

    monkeypatch.setattr(converge_mod, "triage_external_findings", fake_triage)


def test_external_mode_skips_intake_and_seeds_surviving_findings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured = _install(monkeypatch, blocking=True)
    _install_triage(
        monkeypatch,
        captured,
        proceed=("f-002",),
        rejections={"f-001": "src/x.py:12 refutes it"},
    )
    config = _config(tmp_path)

    # A CONFORMING external-mode coder handoff (PR #345 re-review 1): the
    # injected prompt mandates a `## External findings` section with one
    # FIXED/DISPUTED/REVERTED line per injected id — the per-id half of the
    # fixed evidence (the loop's approval is the other half). The FINAL
    # round's handoff is the one read (#387; the fake loop reports rounds=2).
    config.handoff_dir.mkdir(parents=True, exist_ok=True)
    from lithos_loom.plugins.story_develop import handoff as handoff_mod

    (config.handoff_dir / handoff_mod.coder_handoff_name(2)).write_text(
        "## Status: LGTM\n## Summary\nf-002: guarded the handle.\n"
        "## External findings\n- f-002: FIXED — guarded the handle\n",
        encoding="utf-8",
    )

    result = converge_pr(
        config,
        _change(),
        external_findings=(
            _ext_finding(7, body="claim one"),
            _ext_finding(8, body="claim two"),
        ),
    )

    assert "intake_ran" not in captured  # the local panel never runs
    assert captured["triage_findings"] == ["f-001", "f-002"]
    entry = captured["entry"]
    (outcome,) = entry.intake_reviews
    assert [f.finding_id for f in outcome.findings] == ["f-002"]  # survivor only
    assert entry.intake_check_set is None
    # The acknowledgement contract rides into the round-1 coder prompt via the
    # entry, naming exactly the surviving ids.
    assert "## External findings" in entry.external_ack
    assert "f-002" in entry.external_ack
    assert result.status == "converged" and result.pushed
    assert result.intake_cost_usd == 0.1  # the triage spend

    by_id = {o.finding_id: o for o in result.external_outcomes}
    assert by_id["f-001"].disposition == "rejected"
    assert "refutes" in by_id["f-001"].detail
    assert by_id["f-002"].disposition == "fixed"  # acked FIXED + approved loop
    assert by_id["f-001"].finding.activity_id == 7


def test_external_mode_all_rejected_builds_no_coder(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured = _install(monkeypatch, blocking=True)
    _install_triage(
        monkeypatch,
        captured,
        proceed=(),
        rejections={"f-001": "evidence one", "f-002": "evidence two"},
    )

    result = converge_pr(
        _config(tmp_path),
        _change(),
        external_findings=(_ext_finding(7), _ext_finding(8)),
    )

    assert result.status == "triage_rejected"
    assert result.succeeded  # nothing left for the operator to do
    assert "entry" not in captured  # develop() never ran
    assert "push" not in captured
    assert {o.disposition for o in result.external_outcomes} == {"rejected"}


def test_external_mode_an_approval_only_batch_is_already_clean_at_round_zero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """827cedf8 / lens #100: the batch's only content was "No findings. Ready
    to merge." Triage says NOTHING_TO_REMEDIATE and the run ends at round 0
    — `already_clean` (the watcher refunds the round), never a coder turn
    and a panel pass to rediscover the approval."""
    captured = _install(monkeypatch, blocking=True)
    _install_triage(
        monkeypatch,
        captured,
        proceed=(),
        nothing_to_remediate={"f-001": "the comment is an approval"},
    )

    result = converge_pr(
        _config(tmp_path), _change(), external_findings=(_ext_finding(7),)
    )

    assert result.status == "already_clean"
    assert result.succeeded
    assert "entry" not in captured  # develop() never ran
    assert "push" not in captured
    (outcome,) = result.external_outcomes
    assert outcome.disposition == "nothing_to_remediate"
    assert outcome.detail == "the comment is an approval"


def test_external_mode_an_approval_beside_a_rejection_stays_triage_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A refuted claim still owes the reviewer a rejection reply, so the
    mixed batch keeps the `triage_rejected` status — the approval just reads
    as what it is."""
    captured = _install(monkeypatch, blocking=True)
    _install_triage(
        monkeypatch,
        captured,
        proceed=(),
        rejections={"f-001": "src/x.py:12 refutes it"},
        nothing_to_remediate={"f-002": "an approval"},
    )

    result = converge_pr(
        _config(tmp_path),
        _change(),
        external_findings=(_ext_finding(7), _ext_finding(8)),
    )

    assert result.status == "triage_rejected"
    assert "entry" not in captured
    by_id = {o.finding_id: o.disposition for o in result.external_outcomes}
    assert by_id == {"f-001": "rejected", "f-002": "nothing_to_remediate"}


def test_external_mode_an_approval_beside_a_real_claim_only_injects_the_claim(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The guard, end to end: the mixed batch dispatches on its actionable
    part only — the approval never reaches the coder and is not reported as
    unaddressed."""
    captured = _install(monkeypatch, blocking=True)
    _install_triage(
        monkeypatch,
        captured,
        proceed=("f-002",),
        nothing_to_remediate={"f-001": "an approval, no ask"},
    )
    config = _config(tmp_path)
    config.handoff_dir.mkdir(parents=True, exist_ok=True)
    from lithos_loom.plugins.story_develop import handoff as handoff_mod

    (config.handoff_dir / handoff_mod.coder_handoff_name(2)).write_text(
        "## Status: LGTM\n## Summary\nfixed.\n"
        "## External findings\n- f-002: FIXED — guarded the handle\n",
        encoding="utf-8",
    )

    result = converge_pr(
        config,
        _change(),
        external_findings=(_ext_finding(7), _ext_finding(8, body="claim two")),
    )

    entry = captured["entry"]
    (outcome,) = entry.intake_reviews
    assert [f.finding_id for f in outcome.findings] == ["f-002"]  # the claim only
    by_id = {o.finding_id: o.disposition for o in result.external_outcomes}
    assert by_id == {"f-001": "nothing_to_remediate", "f-002": "fixed"}


def test_external_mode_triage_spend_meets_budget_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured = _install(monkeypatch, blocking=True)
    _install_triage(monkeypatch, captured, proceed=("f-001",), cost=5.0)
    config = dataclasses.replace(_config(tmp_path), max_cost_usd=5.0)

    result = converge_pr(config, _change(), external_findings=(_ext_finding(),))

    assert result.status == "failed" and "entry" not in captured


def test_external_mode_rejects_empty_findings(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="non-empty"):
        converge_pr(_config(tmp_path), _change(), external_findings=())


def test_external_mode_dispute_and_unapproved_dispositions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A coder dispute survives as `disputed` — from the DISPUTED
    acknowledgement line in a later round (round 1's `## Findings` block is
    the other channel; a later round's block belongs to the panel, #387);
    an unapproved loop yields `unaddressed`, never a false `fixed`."""
    from lithos_loom.plugins.story_develop import handoff as handoff_mod

    captured = _install(monkeypatch, blocking=True)
    _install_triage(monkeypatch, captured, proceed=("f-001", "f-002"))
    config = _config(tmp_path)
    config.handoff_dir.mkdir(parents=True, exist_ok=True)
    (config.handoff_dir / handoff_mod.coder_handoff_name(2)).write_text(
        "## Status: LGTM\n## Summary\nf-001 disputed; f-002 addressed.\n"
        "## External findings\n"
        "- f-001: DISPUTED — deliberate decision\n"
        "- f-002: FIXED — closed the handle\n",
        encoding="utf-8",
    )

    result = converge_pr(
        config,
        _change(),
        external_findings=(_ext_finding(7), _ext_finding(8)),
    )
    by_id = {o.finding_id: o for o in result.external_outcomes}
    assert by_id["f-001"].disposition == "disputed"
    assert by_id["f-001"].detail == "deliberate decision"
    assert by_id["f-002"].disposition == "fixed"

    # Unapproved loop: same handoff, but the loop stops without approval.
    captured2 = _install(monkeypatch, blocking=True)
    captured2["develop_status"] = "stalled"
    _install_triage(monkeypatch, captured2, proceed=("f-001", "f-002"))
    result = converge_pr(
        config,
        _change(),
        external_findings=(_ext_finding(7), _ext_finding(8)),
    )
    assert result.status == "not_converged"
    assert {o.disposition for o in result.external_outcomes} == {
        "disputed",
        "unaddressed",
    }


def test_external_mode_unacked_finding_stays_unaddressed_when_approved(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """PR #345 re-review 1, end-to-end: two findings survive triage, the coder
    acknowledges only one, and the panel (which never saw the original
    external text) approves the tree. The omitted finding must stay
    `unaddressed` — its thread gets no "Fixed in" reply — instead of riding
    the blanket approval to a false `fixed`."""
    from lithos_loom.plugins.story_develop import handoff as handoff_mod

    captured = _install(monkeypatch, blocking=True)
    _install_triage(monkeypatch, captured, proceed=("f-001", "f-002"))
    config = _config(tmp_path)
    config.handoff_dir.mkdir(parents=True, exist_ok=True)
    (config.handoff_dir / handoff_mod.coder_handoff_name(2)).write_text(
        "## Status: LGTM\n## Summary\nf-001: guarded the handle.\n"
        "## External findings\n- f-001: FIXED — guarded the handle\n",
        encoding="utf-8",
    )

    result = converge_pr(
        config,
        _change(),
        external_findings=(_ext_finding(7), _ext_finding(8)),
    )

    assert result.status == "converged"  # the loop itself approved + pushed
    by_id = {o.finding_id: o for o in result.external_outcomes}
    assert by_id["f-001"].disposition == "fixed"
    assert by_id["f-002"].disposition == "unaddressed"


def test_external_mode_reads_the_final_rounds_acks_a_reverted_fix_is_not_fixed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """lens #84 (#387): round 1 acked FIXED, the panel held the change
    contradicted the acceptance criteria, round 2 reverted it in full and
    said so. The thread must be answered from the FINAL handoff — the run
    is not a success (an operator decision is outstanding), and the message
    says why."""
    from lithos_loom.plugins.story_develop import handoff as handoff_mod

    captured = _install(monkeypatch, blocking=True)
    _install_triage(monkeypatch, captured, proceed=("f-001",))
    config = _config(tmp_path)
    config.handoff_dir.mkdir(parents=True, exist_ok=True)
    (config.handoff_dir / handoff_mod.coder_handoff_name(1)).write_text(
        "## Status: LGTM\n## Summary\nf-001: withheld the stripe.\n"
        "## External findings\n- f-001: FIXED — withheld the healthy stripe\n",
        encoding="utf-8",
    )
    (config.handoff_dir / handoff_mod.coder_handoff_name(2)).write_text(
        "## Status: LGTM\n## Summary\nreverted in full.\n"
        "## External findings\n- f-001: REVERTED — the correctness reviewer "
        "holds it contradicts the acceptance criteria; operator decision needed\n",
        encoding="utf-8",
    )

    result = converge_pr(config, _change(), external_findings=(_ext_finding(7),))

    (o,) = result.external_outcomes
    assert o.disposition == "reverted"
    assert "contradicts the acceptance criteria" in o.detail
    assert result.status == "converged"  # the loop's own verdict on the tree
    assert not result.succeeded  # ...but the external finding is undecided
    assert "f-001 REVERTED" in result.message
    assert "operator decision" in result.message
    assert result.to_json()["succeeded"] is False


def test_a_later_rounds_findings_block_never_speaks_for_an_external_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """opus round 1: round N's `## Findings` block is the T7 dispute contract
    for the PANEL's findings, whose ids are minted independently — a panel
    `f-001` disputed there must not mask the external `f-001`'s FIXED (or
    REVERTED) acknowledgement. Only round 1's block can name injected ids."""
    from lithos_loom.plugins.story_develop import handoff as handoff_mod

    captured = _install(monkeypatch, blocking=True)
    _install_triage(monkeypatch, captured, proceed=("f-001",))
    config = _config(tmp_path)
    config.handoff_dir.mkdir(parents=True, exist_ok=True)
    (config.handoff_dir / handoff_mod.coder_handoff_name(2)).write_text(
        "## Status: LGTM\n## Summary\npanel f-001 disputed; external f-001 stands.\n"
        "## Findings\n"
        "- finding_id: f-001\n  severity: minor\n  status: disputed\n"
        "  rationale: the panel's own nit\n  coder_response: deliberate\n"
        "## External findings\n- f-001: FIXED — guarded the handle\n",
        encoding="utf-8",
    )

    result = converge_pr(config, _change(), external_findings=(_ext_finding(7),))

    (o,) = result.external_outcomes
    assert o.disposition == "fixed" and o.detail == "guarded the handle"


def _no_commit_round_one(captured: dict) -> None:
    """The lens #83 shape (#380): the round-1 coder found nothing to change,
    wrote its handoff and made no commit — the loop's exit C."""
    captured["develop_status"] = "failed"
    captured["develop_rounds"] = 1
    captured["develop_failure_reason"] = "round 1: coder produced no commit"
    captured["no_fixer_commits"] = True


def _no_change_round_one_approved(captured: dict) -> None:
    """The lens #83 shape (#380) as the loop judges it since PR #396's review:
    the round-1 coder found nothing to change and said so per id, the loop
    admitted the empty round and its gate + panel APPROVED the unchanged
    head — no commit, no exit C."""
    captured["develop_status"] = "approved"
    captured["develop_rounds"] = 1
    captured["no_fixer_commits"] = True
    captured["tree_changed"] = False


def test_external_mode_every_id_no_change_needed_is_already_clean(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """lens #83 (#380): the reviewer's "good to merge" was ingested as a
    finding; triage rightly proceeded; the coder rightly changed nothing and
    said so per id. That is `already_clean` — reported, not remediated —
    never `not_converged` (which spent the last budget round and raised a
    needs-human gate on a mergeable PR)."""
    from lithos_loom.plugins.story_develop import handoff as handoff_mod

    captured = _install(monkeypatch, blocking=True)
    _no_change_round_one_approved(captured)
    _install_triage(monkeypatch, captured, proceed=("f-001", "f-002"))
    config = _config(tmp_path)
    config.handoff_dir.mkdir(parents=True, exist_ok=True)
    (config.handoff_dir / handoff_mod.coder_handoff_name(1)).write_text(
        "## Status: LGTM\n## Summary\nnothing to change.\n"
        "## External findings\n"
        "- f-001: NO CHANGE NEEDED — an approval verdict, not a defect\n"
        "- f-002: NO CHANGE NEEDED — verified: the guard already exists\n",
        encoding="utf-8",
    )

    result = converge_pr(
        config, _change(), external_findings=(_ext_finding(7), _ext_finding(8))
    )

    assert result.status == "already_clean"
    assert result.succeeded and not result.pushed
    assert "push" not in captured
    assert {o.disposition for o in result.external_outcomes} == {"no_change_needed"}
    assert "no change" in result.message and "f-001" in result.message
    assert result.to_json()["succeeded"] is True
    # PR #396 review (High): the claim was REVIEWED — the entry admits the
    # empty round only on an all-no-change handoff, and the panel was told
    # what the coder claimed about
    entry = captured["entry"]
    assert entry.no_change_claim is not None and entry.no_change_claim(1) is True
    assert "External review findings" in entry.review_context
    assert "leaks a handle" in entry.review_context
    assert "the gate and panel approved" in result.message


def test_external_mode_an_infra_death_after_no_change_acks_stays_infra_failed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """opus round 1: a coder that wrote its no-change acks and then died on
    the host (auth expiry mid-stream) committed nothing too — but the loop's
    verdict is `infra_failed`, and that must win: the host action reaches
    the operator and the watcher refunds + re-parks, never a success."""
    from lithos_loom.plugins.story_develop import handoff as handoff_mod

    captured = _install(monkeypatch, blocking=True)
    _no_commit_round_one(captured)
    captured["develop_status"] = "infra_failed"
    captured["develop_failure_reason"] = "round 1: coder auth_failed persisted"
    _install_triage(monkeypatch, captured, proceed=("f-001",))
    config = _config(tmp_path)
    config.handoff_dir.mkdir(parents=True, exist_ok=True)
    (config.handoff_dir / handoff_mod.coder_handoff_name(1)).write_text(
        "## Status: LGTM\n## Summary\nx\n"
        "## External findings\n- f-001: NO CHANGE NEEDED — an approval\n",
        encoding="utf-8",
    )

    result = converge_pr(config, _change(), external_findings=(_ext_finding(7),))

    assert result.status == "infra_failed" and not result.succeeded
    assert result.host_action == _HOST_ACTION


def test_external_mode_a_dispute_beside_no_change_is_not_already_clean(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # a dispute is a disagreement the reviewer may answer — reported as
    # before, the round spent; only refuted + not-a-defect is "nothing to do"
    from lithos_loom.plugins.story_develop import handoff as handoff_mod

    captured = _install(monkeypatch, blocking=True)
    _no_commit_round_one(captured)
    _install_triage(monkeypatch, captured, proceed=("f-001", "f-002"))
    config = _config(tmp_path)
    config.handoff_dir.mkdir(parents=True, exist_ok=True)
    (config.handoff_dir / handoff_mod.coder_handoff_name(1)).write_text(
        "## Status: LGTM\n## Summary\nx\n"
        "## External findings\n"
        "- f-001: DISPUTED — the claim is wrong\n"
        "- f-002: NO CHANGE NEEDED — an approval\n",
        encoding="utf-8",
    )

    result = converge_pr(
        config, _change(), external_findings=(_ext_finding(7), _ext_finding(8))
    )

    assert result.status == "not_converged"


def test_external_mode_a_fixed_claim_with_no_commit_is_not_already_clean(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # a FIXED ack over a tree that never changed is a contradiction, not a
    # clean run — the old outcome stands
    from lithos_loom.plugins.story_develop import handoff as handoff_mod

    captured = _install(monkeypatch, blocking=True)
    _no_commit_round_one(captured)
    _install_triage(monkeypatch, captured, proceed=("f-001", "f-002"))
    config = _config(tmp_path)
    config.handoff_dir.mkdir(parents=True, exist_ok=True)
    (config.handoff_dir / handoff_mod.coder_handoff_name(1)).write_text(
        "## Status: LGTM\n## Summary\nx\n"
        "## External findings\n"
        "- f-001: FIXED — guarded it\n"
        "- f-002: NO CHANGE NEEDED — an approval verdict\n",
        encoding="utf-8",
    )

    result = converge_pr(
        config, _change(), external_findings=(_ext_finding(7), _ext_finding(8))
    )

    assert result.status == "not_converged" and not result.succeeded
    by_id = {o.finding_id: o for o in result.external_outcomes}
    assert by_id["f-001"].disposition == "unaddressed"
    # the loop never judged the head (exit C): the no-change claim is not
    # answered on its thread either (PR #396 review)
    assert by_id["f-002"].disposition == "unaddressed"


def test_external_mode_rejected_plus_no_change_needed_is_already_clean(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # triage refuted one, the coder found nothing in the other: nothing to do
    from lithos_loom.plugins.story_develop import handoff as handoff_mod

    captured = _install(monkeypatch, blocking=True)
    _no_change_round_one_approved(captured)
    _install_triage(
        monkeypatch, captured, proceed=("f-002",), rejections={"f-001": "x.py:12"}
    )
    config = _config(tmp_path)
    config.handoff_dir.mkdir(parents=True, exist_ok=True)
    (config.handoff_dir / handoff_mod.coder_handoff_name(1)).write_text(
        "## Status: LGTM\n## Summary\nx\n"
        "## External findings\n- f-002: NO CHANGE NEEDED — intended\n",
        encoding="utf-8",
    )

    result = converge_pr(
        config, _change(), external_findings=(_ext_finding(7), _ext_finding(8))
    )

    assert result.status == "already_clean"
    assert "f-002" in result.message and "refuted" in result.message


def test_external_mode_a_no_change_claim_the_loop_rejects_is_not_already_clean(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """PR #396 review (High): the regression — the coder labelled a real
    defect NO CHANGE NEEDED, the loop admitted the empty round for review,
    the panel disagreed and the validation pass ended the run with its
    rationale, nothing committed. That is `not_converged` (the round spent,
    the exhaustion rule applies), never a refunded `already_clean` with an
    authoritative "Not changed" reply: the claim is reported unaddressed, so
    no thread is answered."""
    from lithos_loom.plugins.story_develop import handoff as handoff_mod

    captured = _install(monkeypatch, blocking=True)
    # the validation pass ended the run (opus round 2): round 1, the loop's
    # own exit with the panel's rationale, nothing committed
    captured["develop_status"] = "failed"
    captured["develop_rounds"] = 1
    captured["develop_failure_reason"] = (
        "round 1: the panel rejected the coder's no-change claim — "
        "correctness: the guard is missing"
    )
    captured["no_fixer_commits"] = True
    captured["tree_changed"] = False
    _install_triage(monkeypatch, captured, proceed=("f-001",))
    config = _config(tmp_path)
    config.handoff_dir.mkdir(parents=True, exist_ok=True)
    (config.handoff_dir / handoff_mod.coder_handoff_name(1)).write_text(
        "## Status: LGTM\n## Summary\nx\n"
        "## External findings\n- f-001: NO CHANGE NEEDED — intended\n",
        encoding="utf-8",
    )

    result = converge_pr(config, _change(), external_findings=(_ext_finding(7),))

    assert result.status == "not_converged" and not result.succeeded
    assert "push" not in captured
    (o,) = result.external_outcomes
    assert o.disposition == "unaddressed" and "not approve" in o.detail


def test_external_mode_an_approved_loop_with_nothing_committed_never_pushes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # defensive: the loop approved a tree it never changed, but the final
    # handoff claims a FIX — nothing was committed, so this is a claim with
    # no fix behind it, NOT #387's "fixed, then reverted" (opus round 2: the
    # watcher would raise a `disputed` gate over a revert that never
    # happened): nothing to push, never a "converged"
    from lithos_loom.plugins.story_develop import handoff as handoff_mod

    captured = _install(monkeypatch, blocking=True)
    _no_change_round_one_approved(captured)
    _install_triage(monkeypatch, captured, proceed=("f-001",))
    config = _config(tmp_path)
    config.handoff_dir.mkdir(parents=True, exist_ok=True)
    (config.handoff_dir / handoff_mod.coder_handoff_name(1)).write_text(
        "## Status: LGTM\n## Summary\nx\n"
        "## External findings\n- f-001: FIXED — guarded it\n",
        encoding="utf-8",
    )

    result = converge_pr(config, _change(), external_findings=(_ext_finding(7),))

    assert result.status == "failed" and not result.succeeded
    assert "push" not in captured
    assert "nothing to push" in result.message and "f-001" in result.message
    (o,) = result.external_outcomes
    assert o.disposition == "unaddressed" and "never committed" in o.detail


def test_external_mode_a_final_round_without_acks_claims_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The safe direction (#387): a round-1 FIXED that the final round's
    handoff does not restate is a stale claim — no "Fixed in" reply, the
    detail names the missing acknowledgement."""
    from lithos_loom.plugins.story_develop import handoff as handoff_mod

    captured = _install(monkeypatch, blocking=True)
    _install_triage(monkeypatch, captured, proceed=("f-001",))
    config = _config(tmp_path)
    config.handoff_dir.mkdir(parents=True, exist_ok=True)
    (config.handoff_dir / handoff_mod.coder_handoff_name(1)).write_text(
        "## Status: LGTM\n## Summary\nf-001: guarded it.\n"
        "## External findings\n- f-001: FIXED — guarded it\n",
        encoding="utf-8",
    )
    (config.handoff_dir / handoff_mod.coder_handoff_name(2)).write_text(
        "## Status: LGTM\n## Summary\nfixed the reviewer's nit.\n",
        encoding="utf-8",
    )

    result = converge_pr(config, _change(), external_findings=(_ext_finding(7),))

    (o,) = result.external_outcomes
    assert o.disposition == "unaddressed"
    assert "round 2" in o.detail and "acknowledg" in o.detail


def test_external_mode_a_round_one_fix_survives_a_final_no_change_needed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """lens #87 (#399): round 1 acked FIXED, the loop went on to converge
    the panel's own findings, and the final handoff said NO CHANGE NEEDED
    ("nothing further this round"). The fix is in the pushed tree: the run
    succeeds, the thread is answered `fixed` from round 1's acknowledgement,
    and the drift rides the outcome as a note."""
    from lithos_loom.plugins.story_develop import handoff as handoff_mod

    captured = _install(monkeypatch, blocking=True)
    _install_triage(monkeypatch, captured, proceed=("f-001",))
    config = _config(tmp_path)
    config.handoff_dir.mkdir(parents=True, exist_ok=True)
    (config.handoff_dir / handoff_mod.coder_handoff_name(1)).write_text(
        "## Status: LGTM\n## Summary\nf-001: guarded it.\n"
        "## External findings\n- f-001: FIXED — guarded the handle\n",
        encoding="utf-8",
    )
    (config.handoff_dir / handoff_mod.coder_handoff_name(2)).write_text(
        "## Status: LGTM\n## Summary\nthe panel's nit.\n"
        "## External findings\n- f-001: NO CHANGE NEEDED — the reviewer closed "
        "it with LGTM this round\n",
        encoding="utf-8",
    )

    result = converge_pr(config, _change(), external_findings=(_ext_finding(7),))

    (o,) = result.external_outcomes
    assert o.disposition == "fixed" and o.detail == "guarded the handle"
    assert "round 1" in o.note and "round 2" in o.note
    assert result.status == "converged" and result.succeeded
    (row,) = result.to_json()["external_outcomes"]
    assert row["disposition"] == "fixed" and row["note"] == o.note


def test_external_mode_a_fixed_ack_over_an_unmoved_tree_is_not_fixed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The objective backstop (#387): the final tree equals the PR head
    outside the project's generated paths — nothing can have been fixed,
    and an approved loop that ends there undid its own fix: the decision
    shape, even when the coder never said REVERTED."""
    from lithos_loom.plugins.story_develop import handoff as handoff_mod

    captured = _install(monkeypatch, blocking=True)
    captured["tree_changed"] = False
    _install_triage(monkeypatch, captured, proceed=("f-001",))
    config = dataclasses.replace(
        _config(tmp_path),
        generated_paths=("docs/generated",),
        regenerate_command="make gen",
    )
    config.handoff_dir.mkdir(parents=True, exist_ok=True)
    (config.handoff_dir / handoff_mod.coder_handoff_name(2)).write_text(
        "## Status: LGTM\n## Summary\nf-001: guarded it.\n"
        "## External findings\n- f-001: FIXED — guarded it\n",
        encoding="utf-8",
    )

    result = converge_pr(config, _change(), external_findings=(_ext_finding(7),))

    (o,) = result.external_outcomes
    assert o.disposition == "reverted"
    assert "identical to the PR head" in o.detail
    assert not result.succeeded and "f-001 REVERTED" in result.message
    # measured from the PR head to the final tree, generated paths excluded
    assert captured["tree_differs"] == (_HEAD, "HEAD", ("docs/generated",))


# ── S5 conflict convergence: resolve mode ─────────────────────────────────────


def _seed_conflict(repo: Path, *, conflicting: bool = True) -> tuple[str, str, str]:
    """main + feature both edit shared.txt (or not); returns
    (merge-base, feature head, main tip)."""

    def run(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=repo, capture_output=True, text=True, check=True
        ).stdout.strip()

    (repo / "shared.txt").write_text("v0\n")
    run("add", "-A")
    run("commit", "-q", "-m", "seed")
    merge_base = run("rev-parse", "HEAD")
    run("switch", "-q", "-c", "feature")
    (repo / ("shared.txt" if conflicting else "own.txt")).write_text("feature\n")
    run("add", "-A")
    run("commit", "-q", "-m", "feature work")
    head = run("rev-parse", "HEAD")
    run("switch", "-q", "main")
    (repo / "shared.txt").write_text("base\n")
    run("add", "-A")
    run("commit", "-q", "-m", "feat: landed thing on main")
    base_tip = run("rev-parse", "HEAD")
    return merge_base, head, base_tip


def _resolve_change(merge_base: str, head: str) -> ResolvedChange:
    return ResolvedChange(
        base_sha=merge_base,
        head_sha=head,
        head_ref="#142 (feature)",
        base_ref="main",
        title="A PR",
        body="do the thing",
        head_branch="feature",
    )


def test_resolve_mode_seeds_the_loop_from_a_merge_in_progress(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tmp_git_repo: Path
) -> None:
    merge_base, head, base_tip = _seed_conflict(tmp_git_repo)
    captured = _install(monkeypatch, blocking=False)
    seen: dict = {}

    def fake_develop(config, *, coder_timeout=3600, reviewer_timeout=3600, entry=None):
        assert entry is not None and entry.pre_commit_guard is not None
        wt = entry.worktree_factory(config)
        seen["entry"] = entry
        seen["merge_head"] = (wt / ".git").is_file()
        seen["marked"] = "<<<<<<<" in (wt / "shared.txt").read_text()
        seen["guard_before"] = entry.pre_commit_guard(wt)
        (wt / "shared.txt").write_text("feature+base\n")  # the coder resolves it
        seen["guard_after"] = entry.pre_commit_guard(wt)
        # the round commit (what develop()'s commit phase does on the host)
        subprocess.run(["git", "add", "-A"], cwd=wt, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "resolve"], cwd=wt, check=True)
        return _dev_result(wt, status="approved")

    monkeypatch.setattr(converge_mod, "develop", fake_develop)

    result = converge_pr(
        _config(tmp_path), _resolve_change(merge_base, head), resolve_conflicts=True
    )

    assert result.status == "converged" and result.pushed
    assert "intake_ran" not in captured  # no panel intake: the merge IS the intake
    entry = seen["entry"]
    assert entry.coder_init_template == "resolve_coder_init.md"
    brief = entry.coder_init_extra["conflict_brief"]
    assert "shared.txt" in brief and "landed thing on main" in brief and "A PR" in brief
    assert entry.intake_reviews == [] and entry.intake_check_set is None
    assert entry.base_override.start_sha == merge_base
    assert entry.base_override.ref == "main"
    assert seen["marked"] and seen["merge_head"]
    assert seen["guard_before"] and "shared.txt" in seen["guard_before"]
    assert seen["guard_after"] is None
    assert result.conflict is not None
    assert result.conflict.paths == ("shared.txt",)
    assert result.conflict.base_sha == base_tip
    assert result.to_json()["conflict"] == {
        "paths": ["shared.txt"],
        "base_ref": "main",
        "base_sha": base_tip,
    }
    assert captured["push"]["expected_remote_sha"] == head


def test_resolve_mode_wires_the_regenerate_pass_and_names_the_set_aside_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tmp_git_repo: Path
) -> None:
    # PRD S4: under a policy the loop entry carries the post-commit regenerate
    # pass, and the panel's merge context names the generated paths that were
    # set aside (a hand-merged one is a defect, a stale one too)
    merge_base, head, _tip = _seed_conflict(tmp_git_repo)
    # add a generated conflict beside the real one
    subprocess.run(["git", "switch", "-q", "main"], cwd=tmp_git_repo, check=True)
    (tmp_git_repo / "docs" / "generated").mkdir(parents=True, exist_ok=True)
    (tmp_git_repo / "docs" / "generated" / "m.json").write_text("main\n")
    subprocess.run(["git", "add", "-A"], cwd=tmp_git_repo, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "main: gen"], cwd=tmp_git_repo, check=True
    )
    subprocess.run(["git", "switch", "-q", "feature"], cwd=tmp_git_repo, check=True)
    (tmp_git_repo / "docs" / "generated").mkdir(parents=True, exist_ok=True)
    (tmp_git_repo / "docs" / "generated" / "m.json").write_text("feature\n")
    subprocess.run(["git", "add", "-A"], cwd=tmp_git_repo, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "feature: gen"], cwd=tmp_git_repo, check=True
    )
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_git_repo, capture_output=True, text=True
    ).stdout.strip()
    _install(monkeypatch, blocking=False)
    seen: dict = {}

    def fake_develop(config, *, coder_timeout=3600, reviewer_timeout=3600, entry=None):
        assert entry is not None
        wt = entry.worktree_factory(config)
        seen["entry"] = entry
        (wt / "shared.txt").write_text("feature+base\n")
        subprocess.run(["git", "add", "-A"], cwd=wt, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "resolve"], cwd=wt, check=True)
        return _dev_result(wt, status="approved")

    monkeypatch.setattr(converge_mod, "develop", fake_develop)
    config = dataclasses.replace(
        _config(tmp_path),
        generated_paths=("docs/generated",),
        regenerate_command="make gen",
    )
    result = converge_pr(
        config, _resolve_change(merge_base, head), resolve_conflicts=True, no_push=True
    )
    assert result.status == "converged"
    entry = seen["entry"]
    assert entry.post_commit_pass is not None
    assert "docs/generated/m.json" in entry.review_context
    assert "not hand-merged" in entry.review_context
    assert "docs/generated/m.json" in entry.coder_init_extra["conflict_brief"]
    assert "make gen" in entry.coder_init_extra["conflict_brief"]
    assert result.conflict is not None and result.conflict.paths == ("shared.txt",)

    # without a policy: no pass, no mention
    seen.clear()
    converge_pr(
        _config(tmp_path),
        _resolve_change(merge_base, head),
        resolve_conflicts=True,
        no_push=True,
    )
    assert seen["entry"].post_commit_pass is None
    assert "not hand-merged" not in seen["entry"].review_context


def test_resolve_mode_with_nothing_to_resolve_spends_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tmp_git_repo: Path
) -> None:
    merge_base, head, _tip = _seed_conflict(tmp_git_repo, conflicting=False)
    captured = _install(monkeypatch, blocking=False)

    result = converge_pr(
        _config(tmp_path), _resolve_change(merge_base, head), resolve_conflicts=True
    )

    assert result.status == "no_conflict" and not result.succeeded
    assert "entry" not in captured and "push" not in captured
    assert result.to_json()["conflict"] is None
    # the throwaway worktree is gone: only the repo and the work dir remain
    leftover = sorted(p.name for p in tmp_path.iterdir() if p.is_dir())
    assert leftover == [n for n in leftover if n in ("repo", "work")]


def test_resolve_mode_is_exclusive_with_external_findings(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="resolve_conflicts"):
        converge_pr(
            _config(tmp_path),
            _change(),
            resolve_conflicts=True,
            external_findings=("finding",),  # type: ignore[arg-type]
        )


def test_resolve_mode_refuses_a_fork_before_touching_git(tmp_path: Path) -> None:
    result = converge_pr(
        _config(tmp_path), _change(is_fork=True), resolve_conflicts=True
    )
    assert result.status == "fork_unsupported"


def test_markers_guard_refuses_an_abandoned_merge(
    tmp_path: Path, tmp_git_repo: Path
) -> None:
    """The prompt forbids `git merge --abort` but nothing else enforced it: a
    coder that abandons the merge leaves a clean, marker-free tree at the PR
    head — and a plain commit there would converge WITHOUT the base. The
    guard refuses while HEAD is still the PR head and no merge is in progress."""
    from lithos_loom.plugins.story_develop.conflict_resolve import markers_guard
    from lithos_loom.runner import git

    merge_base, head, base_tip = _seed_conflict(tmp_git_repo)
    subprocess.run(["git", "switch", "-q", "feature"], cwd=tmp_git_repo, check=True)
    assert git.merge_no_commit(tmp_git_repo, base_tip) == ["shared.txt"]
    guard = markers_guard(("shared.txt",), head_sha=head, base_sha=base_tip)
    marked = guard(tmp_git_repo)
    assert marked is not None and "shared.txt" in marked

    git.abort_merge(tmp_git_repo)
    refused = guard(tmp_git_repo)
    assert refused is not None and "abandoned" in refused

    # a resolved, still-in-progress merge passes; so does any later round
    # (HEAD moved past the PR head, MERGE_HEAD gone)
    assert git.merge_no_commit(tmp_git_repo, base_tip) == ["shared.txt"]
    (tmp_git_repo / "shared.txt").write_text("both\n")
    assert guard(tmp_git_repo) is None
    assert git.commit_all(tmp_git_repo, "resolve") is not None
    assert guard(tmp_git_repo) is None


def test_resolve_mode_gives_the_panel_the_merge_context(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tmp_git_repo: Path
) -> None:
    """PR #364 review F1: the panel must see the conflicted paths and both
    parents — a resolution taking the base version is invisible in the
    ordinary fork-point diff."""
    merge_base, head, base_tip = _seed_conflict(tmp_git_repo)
    _install(monkeypatch, blocking=False)
    seen: dict = {}

    def fake_develop(config, *, coder_timeout=3600, reviewer_timeout=3600, entry=None):
        assert entry is not None
        wt = entry.worktree_factory(config)
        seen["context"] = entry.review_context
        (wt / "shared.txt").write_text("feature+base\n")
        subprocess.run(["git", "add", "-A"], cwd=wt, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "resolve"], cwd=wt, check=True)
        return _dev_result(wt, status="approved")

    monkeypatch.setattr(converge_mod, "develop", fake_develop)
    result = converge_pr(
        _config(tmp_path), _resolve_change(merge_base, head), resolve_conflicts=True
    )
    assert result.status == "converged"
    assert "shared.txt" in seen["context"].splitlines()
    assert head[:12] in seen["context"] and base_tip[:12] in seen["context"]


def test_resolve_mode_never_pushes_a_tree_without_the_base(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tmp_git_repo: Path
) -> None:
    """PR #364 review F2, the belt to the guard's braces: whatever the loop
    did, the push epilogue refuses a HEAD the intended base is not an
    ancestor of — `push_to_pr_ref` only proves descent from the PR head."""
    merge_base, head, base_tip = _seed_conflict(tmp_git_repo)
    captured = _install(monkeypatch, blocking=False)

    def fake_develop(config, *, coder_timeout=3600, reviewer_timeout=3600, entry=None):
        assert entry is not None
        wt = entry.worktree_factory(config)
        # the loop "approved" a tree that abandoned the merge
        from lithos_loom.runner import git as _git

        _git.abort_merge(wt)
        (wt / "extra.txt").write_text("x\n")
        subprocess.run(["git", "add", "-A"], cwd=wt, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "not a merge"], cwd=wt, check=True)
        return _dev_result(wt, status="approved")

    monkeypatch.setattr(converge_mod, "develop", fake_develop)
    result = converge_pr(
        _config(tmp_path), _resolve_change(merge_base, head), resolve_conflicts=True
    )
    assert result.status == "failed" and not result.pushed
    assert "push" not in captured
    assert base_tip[:12] in result.message and "ancestor" in result.message


def test_resolve_mode_reports_a_moved_base_without_spending(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tmp_git_repo: Path
) -> None:
    merge_base, head, _tip = _seed_conflict(tmp_git_repo)
    captured = _install(monkeypatch, blocking=False)
    result = converge_pr(
        _config(tmp_path),
        _resolve_change(merge_base, head),
        resolve_conflicts=True,
        expect_base="0" * 40,
    )
    assert result.status == "base_moved" and not result.succeeded
    assert "entry" not in captured and "push" not in captured


def test_infra_failed_loop_is_its_own_verdict_with_the_host_action(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """#377: a loop that died on infrastructure is not "reviewed and did not
    converge" — the watcher's budget and reservation accounting key on the
    status, so it must be distinguishable, and the host action must ride the
    JSON for the watcher's breadcrumb."""
    captured = _install(monkeypatch, blocking=True)
    captured["develop_status"] = "infra_failed"
    result = converge_pr(_config(tmp_path), _change())
    assert result.status == "infra_failed"
    assert not result.succeeded
    assert "push" not in captured
    data = result.to_json()
    assert data["status"] == "infra_failed" and data["succeeded"] is False
    assert data["develop_status"] == "infra_failed"
    assert data["host_action"] == _HOST_ACTION


def test_other_unapproved_loops_carry_no_host_action(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured = _install(monkeypatch, blocking=True)
    captured["develop_status"] = "stalled"
    result = converge_pr(_config(tmp_path), _change())
    assert result.status == "not_converged"
    assert result.to_json()["host_action"] == ""


def test_an_intake_panel_that_died_on_infra_is_infra_failed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """#377: the same class one phase earlier — the operator is told what to
    fix on the host, not that the panel was "interrupted / invalid"."""
    from types import SimpleNamespace

    panel = SimpleNamespace(
        round_reviews=[],
        cost=0.3,
        infra_failure="reviewer [correctness] auth_failed persisted after 2 attempts",
        infra_host_action=_HOST_ACTION,
    )
    captured = _install(monkeypatch, blocking=True, incomplete=True, panel=panel)
    result = converge_pr(_config(tmp_path), _change())
    assert result.status == "infra_failed"
    assert result.host_action == _HOST_ACTION
    assert "INFRA FAILURE during the intake review" in result.message
    assert result.to_json()["host_action"] == _HOST_ACTION
    assert "develop_ran" not in captured
