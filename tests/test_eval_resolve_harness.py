"""Tests for the resolve-eval harness (PRD pr-reconciliation S8, the
conflict-resolution shape): scoring one S5 run against the case's oracle,
aggregating K, and the fail-closed order — the oracle is validated on the
known-good / known-bad trees before the first paid sample, a fixture that
does not conflict aborts the case, and every tree is cleaned up.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from lithos_loom.evals.resolve.case import Probe, ResolveCase
from lithos_loom.evals.resolve.harness import (
    FixtureError,
    OracleError,
    ProbeResult,
    ResolveOutcome,
    SampleScore,
    Trees,
    aggregate_resolve,
    expected_fingerprint,
    run_resolve_case,
    score_sample,
)

_MB, _BASE, _HEAD, _GOOD, _BAD = ("a" * 40, "b" * 40, "c" * 40, "d" * 40, "e" * 40)
_MERGE, _FINAL = "f" * 40, "9" * 40


def _case(
    probes: tuple[Probe, ...] = (Probe("p1", "true"), Probe("p2", "true")),
) -> ResolveCase:
    return ResolveCase(
        id="r1",
        description="d",
        repo=".",
        title="T",
        acceptance_criteria="ac",
        merge_base=_MB,
        base=_BASE,
        probes=probes,
        personas=("correctness",),
        profile="standard",
        head=_HEAD,
        known_good=_GOOD,
        known_bad=_BAD,
        case_dir=Path("/case"),
    )


def _outcome(
    status: str = "converged",
    *,
    merge_sha: str = _MERGE,
    final_sha: str = _FINAL,
    gate_green: bool = True,
    rounds: int = 1,
    cost: float = 3.5,
    cleanup=None,
) -> ResolveOutcome:
    return ResolveOutcome(
        status=status,
        message=f"{status} msg",
        rounds=rounds,
        cost_usd=cost,
        conflict_paths=("a.py", "b.py"),
        merge_sha=merge_sha,
        final_sha=final_sha,
        gate_green=gate_green,
        findings_by_severity={"critical": 0, "major": 1, "minor": 0},
        retained={"merge.diff": "diff", "final.diff": "diff2"},
        cleanup=cleanup or (lambda: None),
    )


class _Probes:
    """A probe runner scripted per (sha, probe name) → passed; records calls."""

    def __init__(self, table: dict[tuple[str, str], bool]) -> None:
        self.table = table
        self.calls: list[tuple[str, str]] = []

    def __call__(self, case: ResolveCase, sha: str, probe: Probe) -> ProbeResult:
        self.calls.append((sha, probe.name))
        passed = self.table.get((sha, probe.name), False)
        return ProbeResult(
            name=probe.name, passed=passed, exit_code=0 if passed else 1, output=""
        )


def _all_pass(*shas: str) -> dict[tuple[str, str], bool]:
    return {(s, p): True for s in shas for p in ("p1", "p2")}


# --- score_sample -----------------------------------------------------------


def test_a_converged_correct_resolution_scores_every_cell_green() -> None:
    probes = _Probes(_all_pass(_MERGE, _FINAL))
    s = score_sample(_case(), _outcome(), probes)
    assert not s.errored
    assert (s.resolved, s.gate_green, s.approved) == (True, True, True)
    assert (s.correct_first, s.correct_final) == (True, True)
    assert (s.unsafe, s.wasted) == (False, False)
    assert s.probes_first == {"p1": True, "p2": True}
    assert s.probes_final == {"p1": True, "p2": True}
    assert (s.merge_sha, s.final_sha, s.rounds, s.cost_usd) == (_MERGE, _FINAL, 1, 3.5)
    assert s.status == "converged"
    # every probe ran on both trees, in case order
    assert probes.calls == [
        (_MERGE, "p1"),
        (_MERGE, "p2"),
        (_FINAL, "p1"),
        (_FINAL, "p2"),
    ]


def test_an_approved_but_wrong_resolution_is_unsafe() -> None:
    # the dangerous cell: S5 would have auto-pushed this merge commit
    table = _all_pass(_MERGE, _FINAL)
    table[(_FINAL, "p2")] = False
    s = score_sample(_case(), _outcome(), _Probes(table))
    assert s.approved and not s.correct_final
    assert s.unsafe and not s.wasted


def test_a_correct_resolution_the_panel_rejected_is_wasted_not_unsafe() -> None:
    s = score_sample(
        _case(), _outcome("not_converged"), _Probes(_all_pass(_MERGE, _FINAL))
    )
    assert not s.approved and s.correct_final
    assert s.wasted and not s.unsafe
    assert s.resolved


def test_the_panel_can_fix_a_wrong_first_resolution() -> None:
    # round 1 missed the property; the panel's round 2 closed it — the
    # coder-alone and pipeline rates read differently, which is the point
    table = _all_pass(_FINAL)
    table[(_MERGE, "p1")] = True  # p2 absent → False
    s = score_sample(_case(), _outcome(rounds=2), _Probes(table))
    assert not s.correct_first and s.correct_final
    assert s.probes_first == {"p1": True, "p2": False}


def test_no_merge_commit_means_not_resolved_and_no_probe_runs() -> None:
    probes = _Probes(_all_pass(_HEAD))
    s = score_sample(_case(), _outcome("failed", merge_sha="", final_sha=_HEAD), probes)
    assert not s.resolved
    assert not s.correct_first and not s.correct_final
    assert s.probes_first == {} and s.probes_final == {}
    assert probes.calls == []
    assert not s.unsafe and not s.wasted
    assert not s.errored


@pytest.mark.parametrize("status", ["infra_failed", "error"])
def test_an_infra_death_is_errored_and_runs_no_probe(status: str) -> None:
    probes = _Probes(_all_pass(_MERGE, _FINAL))
    s = score_sample(_case(), _outcome(status), probes)
    assert s.errored
    assert probes.calls == []
    assert not (s.resolved or s.approved or s.correct_final)


def test_a_probe_that_errors_counts_as_failed_and_is_recorded() -> None:
    def runner(case: ResolveCase, sha: str, probe: Probe) -> ProbeResult:
        return ProbeResult(
            name=probe.name, passed=False, exit_code=None, output="", error="boom"
        )

    s = score_sample(_case(), _outcome(), runner)
    assert not s.correct_first and not s.correct_final
    assert s.unsafe  # approved, and the oracle did not hold
    payload = s.payload()
    assert payload["probe_results_final"]["p1"]["error"] == "boom"
    assert payload["probe_results_final"]["p1"]["exit_code"] is None


def test_payload_is_json_shaped() -> None:
    s = score_sample(_case(), _outcome(), _Probes(_all_pass(_MERGE, _FINAL)))
    p = s.payload()
    assert p["status"] == "converged"
    assert p["conflict_paths"] == ["a.py", "b.py"]
    assert p["probes_first"] == {"p1": True, "p2": True}
    assert p["unsafe"] is False
    assert p["findings_by_severity"] == {"critical": 0, "major": 1, "minor": 0}
    assert p["message"] == "converged msg"


# --- aggregate --------------------------------------------------------------


_GREEN = SampleScore(
    status="converged",
    message="",
    errored=False,
    resolved=True,
    gate_green=True,
    approved=True,
    correct_first=True,
    correct_final=True,
    rounds=1,
    cost_usd=1.0,
    conflict_paths=(),
    merge_sha=_MERGE,
    final_sha=_FINAL,
    probes_first={},
    probes_final={},
    probe_results_first=(),
    probe_results_final=(),
    findings_by_severity={},
)


def _score(**kw) -> SampleScore:
    return replace(_GREEN, **kw)


def test_aggregate_rates_are_over_valid_samples_only() -> None:
    scores = [
        _score(),
        _score(approved=False, correct_final=True),  # wasted
        _score(correct_final=False),  # unsafe
        _score(
            status="infra_failed",
            errored=True,
            resolved=False,
            approved=False,
            correct_first=False,
            correct_final=False,
            gate_green=False,
        ),
        _score(
            resolved=False,
            approved=False,
            correct_first=False,
            correct_final=False,
            gate_green=False,
            status="failed",
        ),
    ]
    r = aggregate_resolve("r1", scores, k=5, bar=0.8)
    assert (r.n, r.n_valid) == (5, 4)
    assert r.resolved == 3 and r.correct_first == 3 and r.correct_final == 2
    assert r.approved == 2 and r.unsafe == 1 and r.wasted == 1 and r.gate_green == 3
    assert r.correct_final_rate == pytest.approx(0.5)
    assert r.unsafe_rate == pytest.approx(0.25)
    assert r.errored_per_sample == (False, False, False, True, False)
    assert r.status_per_sample == (
        "converged",
        "converged",
        "converged",
        "infra_failed",
        "failed",
    )
    assert r.cost_usd_per_sample == (1.0,) * 5
    lo, hi = r.correct_final_ci
    assert 0.0 <= lo < 0.5 < hi <= 1.0
    assert not r.passed  # an unsafe approval fails the case regardless of the bar


def test_pass_needs_no_unsafe_approval_and_the_bar_on_the_pipeline_rate() -> None:
    assert aggregate_resolve("r", [_score()] * 5, k=5, bar=0.8).passed
    below = [_score()] * 3 + [
        _score(approved=False, correct_final=False, correct_first=False)
    ] * 2
    assert not aggregate_resolve("r", below, k=5, bar=0.8).passed
    assert aggregate_resolve("r", below, k=5, bar=0.6).passed
    one_unsafe = [_score()] * 4 + [_score(correct_final=False)]
    assert not aggregate_resolve("r", one_unsafe, k=5, bar=0.5).passed


def test_no_valid_sample_never_passes() -> None:
    errored = [_score(errored=True, status="error")] * 3
    r = aggregate_resolve("r", errored, k=3, bar=0.0)
    assert r.n_valid == 0 and not r.passed
    assert r.correct_final_ci == (0.0, 0.0)


# --- run_resolve_case -------------------------------------------------------


class _Run:
    def __init__(self, outcomes: list[ResolveOutcome]) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[tuple[str, Trees]] = []

    def __call__(self, case: ResolveCase, trees: Trees) -> ResolveOutcome:
        self.calls.append((trees.head, trees))
        return self.outcomes.pop(0)


def _trees(cleanup_log: list[str]) -> tuple[Trees, Callable[[], None]]:
    return Trees(
        head=_HEAD, known_good=_GOOD, known_bad=_BAD
    ), lambda: cleanup_log.append("trees")


def test_the_oracle_is_validated_on_both_controls_before_any_sample() -> None:
    cleanup: list[str] = []
    run = _Run([])
    good_only = _all_pass(_GOOD)  # known-bad passes nothing — fine
    probes = _Probes(good_only)
    # known-bad must FAIL at least one probe; known-good must pass all. Here
    # known-bad fails both → valid oracle. Make known-good fail one → refuse.
    probes.table[(_GOOD, "p2")] = False
    with pytest.raises(OracleError, match="known-good"):
        run_resolve_case(
            _case(),
            k=2,
            bar=0.8,
            resolve_fn=run,
            probe_runner=probes,
            materialise=lambda c: _trees(cleanup),
        )
    assert run.calls == []
    assert cleanup == ["trees"]


def test_a_known_bad_that_passes_every_probe_is_refused() -> None:
    cleanup: list[str] = []
    run = _Run([])
    probes = _Probes(_all_pass(_GOOD, _BAD))
    with pytest.raises(OracleError, match="known-bad"):
        run_resolve_case(
            _case(),
            k=1,
            bar=0.8,
            resolve_fn=run,
            probe_runner=probes,
            materialise=lambda c: _trees(cleanup),
        )
    assert run.calls == []
    assert cleanup == ["trees"]


@pytest.mark.parametrize(
    "status", ["no_conflict", "conflict_unsupported", "base_moved"]
)
def test_a_fixture_that_cannot_be_measured_aborts_before_more_samples(
    status: str,
) -> None:
    cleanup: list[str] = []
    run = _Run(
        [
            _outcome(
                status,
                merge_sha="",
                final_sha="",
                cleanup=lambda: cleanup.append("run"),
            )
        ]
        * 3
    )
    probes = _Probes(_all_pass(_GOOD))
    with pytest.raises(FixtureError, match=status):
        run_resolve_case(
            _case(),
            k=3,
            bar=0.8,
            resolve_fn=run,
            probe_runner=probes,
            materialise=lambda c: _trees(cleanup),
        )
    assert len(run.calls) == 1
    assert cleanup == ["run", "trees"]


def test_samples_are_scored_probed_sunk_and_cleaned_in_order() -> None:
    cleanup: list[str] = []
    outcomes = [
        _outcome(cleanup=lambda: cleanup.append("run0")),
        _outcome("not_converged", cleanup=lambda: cleanup.append("run1")),
    ]
    run = _Run(outcomes)
    probes = _Probes({**_all_pass(_GOOD, _MERGE, _FINAL)})
    sunk: list[tuple[str, int, dict]] = []

    def sink(case_id: str, i: int, payload: dict) -> None:
        # the run's tree must still be probe-able when the sink fires — the
        # outcome's cleanup runs after both
        assert f"run{i}" not in cleanup
        sunk.append((case_id, i, payload))

    r = run_resolve_case(
        _case(),
        k=2,
        bar=0.8,
        resolve_fn=run,
        probe_runner=probes,
        materialise=lambda c: _trees(cleanup),
        sink=sink,
    )
    assert [t.head for _, t in run.calls] == [_HEAD, _HEAD]
    assert [(c, i) for c, i, _ in sunk] == [("r1", 0), ("r1", 1)]
    assert sunk[0][2]["status"] == "converged" and sunk[0][2]["retained"] == {
        "merge.diff": "diff",
        "final.diff": "diff2",
    }
    assert sunk[0][2]["trees"] == {
        "head": _HEAD,
        "known_good": _GOOD,
        "known_bad": _BAD,
    }
    assert cleanup == ["run0", "run1", "trees"]
    assert (r.n, r.n_valid, r.approved, r.correct_final, r.wasted) == (2, 2, 1, 2, 1)
    # oracle validation probed both controls first, then each sample's trees
    assert probes.calls[:4] == [
        (_GOOD, "p1"),
        (_GOOD, "p2"),
        (_BAD, "p1"),
        (_BAD, "p2"),
    ]


def test_a_crashing_run_is_an_errored_sample_not_a_crashed_case() -> None:
    cleanup: list[str] = []

    def boom(case: ResolveCase, trees: Trees) -> ResolveOutcome:
        raise RuntimeError("docker exploded")

    probes = _Probes(_all_pass(_GOOD))
    r = run_resolve_case(
        _case(),
        k=2,
        bar=0.8,
        resolve_fn=boom,
        probe_runner=probes,
        materialise=lambda c: _trees(cleanup),
    )
    assert r.n_valid == 0
    assert r.status_per_sample == ("error", "error")
    assert "docker exploded" in r.message_per_sample[0]
    assert cleanup == ["trees"]


def test_expected_fingerprint_keys_on_what_the_scorer_consumes() -> None:
    a = expected_fingerprint(_case())
    assert a == expected_fingerprint(_case())
    assert a != expected_fingerprint(_case(probes=(Probe("p1", "true"),)))
    assert a != expected_fingerprint(
        _case(probes=(Probe("p1", "false"), Probe("p2", "true")))
    )


# --- score_sample: the review's errored / pushable rules --------------------


def test_an_interrupted_loop_is_errored_not_a_verdict() -> None:
    # a pause budget ran out mid-run: converge flattens it to not_converged,
    # but it is a host condition — excluded, never "not resolved"
    probes = _Probes(_all_pass(_MERGE, _FINAL))
    s = score_sample(
        _case(),
        replace(_outcome("not_converged"), develop_status="interrupted"),
        probes,
    )
    assert s.errored and not s.resolved
    assert probes.calls == []


def test_a_reviewer_with_an_invalid_final_handoff_is_errored() -> None:
    s = score_sample(
        _case(),
        replace(_outcome("failed"), panel_invalid=True),
        _Probes(_all_pass(_MERGE)),
    )
    assert s.errored


def test_an_approval_that_could_not_be_pushed_is_not_approved() -> None:
    # history rewritten under the merge: S5's push epilogue would refuse it,
    # so a wrong tree here is not an UNSAFE push
    probes = _Probes({})
    s = score_sample(_case(), replace(_outcome(), pushable=False, merge_sha=""), probes)
    assert not s.approved and not s.resolved and not s.unsafe
    assert probes.calls == []


def test_a_probe_runner_that_crashes_errors_the_sample_and_the_case_goes_on() -> None:
    cleanup: list[str] = []
    calls = {"n": 0}

    def flaky(case: ResolveCase, sha: str, probe: Probe) -> ProbeResult:
        calls["n"] += 1
        if sha == _MERGE and calls["n"] > 4:  # after the oracle's four control calls
            raise RuntimeError("git lock")
        return ProbeResult(name=probe.name, passed=sha != _BAD, exit_code=0, output="")

    outcomes = [_outcome(cleanup=lambda: cleanup.append("run0"))]
    run = _Run(outcomes)
    r = run_resolve_case(
        _case(),
        k=1,
        bar=0.8,
        resolve_fn=run,
        probe_runner=flaky,
        materialise=lambda c: _trees(cleanup),
    )
    assert r.status_per_sample == ("error",)
    assert "git lock" in r.message_per_sample[0]
    assert cleanup == ["run0", "trees"]  # the run's tree was still released


# --- live_resolve over a scratch repo, converge_pr faked around the REAL intake --


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def _scratch_repo(tmp_path: Path) -> tuple[Path, str, str, str]:
    """``(repo, merge_base, head, base)``: the head and the base both edit
    a.txt's one line, so merging the base into the head conflicts."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "a.txt").write_text("x = 1\n")
    (repo / "b.txt").write_text("b\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "merge-base")
    merge_base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "-b", "story")
    (repo / "a.txt").write_text("x = 2\n")
    _git(repo, "commit", "-q", "-am", "story")
    head = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "main")
    (repo / "a.txt").write_text("x = 3\n")
    _git(repo, "commit", "-q", "-am", "landed on main")
    base = _git(repo, "rev-parse", "HEAD")
    return repo, merge_base, head, base


def _live_case(repo: Path, merge_base: str, head: str, base: str) -> ResolveCase:
    return replace(
        _case(),
        repo=str(repo),
        merge_base=merge_base,
        head=head,
        base=base,
        known_good=base,
        known_bad=merge_base,
        case_dir=repo.parent,
    )


def _fake_converge(shape: str):
    """converge_pr with the real intake and a scripted coder: ``merge`` (the
    merge commit), ``merge+format`` (a second round-1 commit after it),
    ``rewrite`` (abort the merge, reset onto the base, commit the PR's work
    afresh) or ``raise`` (die after the intake, as docker would)."""
    from lithos_loom.plugins.story_develop.conflict_resolve import (
        prepare_conflict_intake,
    )
    from lithos_loom.plugins.story_develop.converge import (
        ConflictSummary,
        ConvergeResult,
    )
    from lithos_loom.plugins.story_develop.develop import DevelopResult
    from lithos_loom.plugins.story_develop.test_gate import GateResult
    from lithos_loom.runner import git

    def converge_pr(config, change, **kw):
        assert kw == {
            "no_push": True,
            "resolve_conflicts": True,
            "coder_timeout": 3600,
            "reviewer_timeout": 3600,
        }
        intake = prepare_conflict_intake(config, change)
        assert intake is not None and intake.paths == ("a.txt",)
        wt = intake.worktree
        if shape == "raise":
            raise RuntimeError("docker down")
        if shape == "rewrite":
            git.abort_merge(wt)
            subprocess.run(
                ["git", "reset", "-q", "--hard", change.base_ref], cwd=wt, check=True
            )
            (wt / "a.txt").write_text("x = 2\n")
            git.commit_all(wt, "story, re-applied")
        else:
            (wt / "a.txt").write_text("x = 5\n")
            git.commit_all(wt, "merge")
            if shape == "merge+format":
                (wt / "b.txt").write_text("b formatted\n")
                git.commit_all(wt, "format")
        dr = DevelopResult(
            status="approved",
            run_id=config.run_id,
            worktree=wt,
            branch=wt.name,
            base_sha=change.base_sha,
            commits=[],
            rounds=1,
            handoff_present=True,
            coder_cost_usd=1.0,
            review_cost_usd=0.5,
            message="approved",
            test_gate=GateResult(command="t", exit_code=0, passed=True, output_tail=""),
        )
        return ConvergeResult(
            status="converged",
            change=change,
            develop_result=dr,
            fixer_commits=tuple(git.commits_since(wt, change.head_sha)),
            message="converged",
            conflict=ConflictSummary(
                paths=intake.paths, base_ref=intake.base_ref, base_sha=intake.base_sha
            ),
        )

    return converge_pr


def _branches(repo: Path) -> set[str]:
    return set(_git(repo, "branch", "--format=%(refname:short)").split())


def _run_live(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, shape: str):
    from lithos_loom.evals.resolve import harness
    from lithos_loom.plugins.story_develop.config import ReviewerSpec

    monkeypatch.setenv("GIT_AUTHOR_NAME", "eval")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "eval@localhost")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "eval")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "eval@localhost")
    repo, merge_base, head, base = _scratch_repo(tmp_path)
    monkeypatch.setattr(harness, "converge_pr", _fake_converge(shape))
    case = _live_case(repo, merge_base, head, base)
    trees = Trees(head=head, known_good=base, known_bad=merge_base)
    panel = (ReviewerSpec(name="correctness", tool="codex", model="r"),)

    def run():
        return harness.live_resolve(
            case,
            trees,
            tool="claude",
            model="m",
            effort=None,
            reviewers=panel,
            profile="standard",
            max_rounds=2,
        )

    return repo, head, base, run


def test_live_resolve_reads_the_merge_commit_final_tree_and_cleans_up(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo, head, base, run = _run_live(monkeypatch, tmp_path, "merge+format")
    outcome = run()
    assert outcome.status == "converged" and outcome.develop_status == "approved"
    assert outcome.conflict_paths == ("a.txt",)
    assert outcome.rounds == 1 and outcome.cost_usd == pytest.approx(1.5)
    assert outcome.gate_green and outcome.pushable and not outcome.panel_invalid
    # the merge commit is the two-parent commit on the PR head; the final tree
    # is the second round-1 commit
    parents = _git(repo, "rev-list", "--parents", "-n", "1", outcome.merge_sha).split()
    assert parents[1:] == [head, base]
    assert outcome.final_sha != outcome.merge_sha
    assert _git(repo, "rev-parse", f"{outcome.final_sha}^") == outcome.merge_sha
    assert set(outcome.retained) == {"merge.diff", "final.diff"}
    assert "x = 5" in outcome.retained["merge.diff"]
    assert "b formatted" in outcome.retained["final.diff"]
    # the run's branch + worktree stayed for the probes — and go on cleanup
    assert _branches(repo) - {"main", "story"}
    outcome.cleanup()
    assert _branches(repo) == {"main", "story"}
    assert _git(repo, "worktree", "list").count("\n") == 0


def test_live_resolve_cleans_up_a_run_that_died_after_the_intake(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo, _head, _base, run = _run_live(monkeypatch, tmp_path, "raise")
    with pytest.raises(RuntimeError, match="docker down"):
        run()
    # nothing of the run is left in the TARGET repo: no branch, no worktree
    assert _branches(repo) == {"main", "story"}
    assert _git(repo, "worktree", "list").count("\n") == 0
    assert _git(repo, "worktree", "prune", "--dry-run") == ""


def test_live_resolve_refuses_a_rewritten_history_as_a_resolution(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo, _head, _base, run = _run_live(monkeypatch, tmp_path, "rewrite")
    outcome = run()
    try:
        assert outcome.status == "converged"
        assert not outcome.pushable and outcome.merge_sha == ""
        assert "S5 would refuse the push" in outcome.message
        s = score_sample(_case(), outcome, _Probes({}))
        assert not s.approved and not s.resolved and not s.unsafe
    finally:
        outcome.cleanup()
    assert _branches(repo) == {"main", "story"}
