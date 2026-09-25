"""Unit tests for the per-round checkpoint (5dbeb0c8 slice C).

Pure read / write over a manually-built run dir — no docker, no loop. The
end-to-end "develop() writes one per round and a resume continues from it" tests
live in ``test_story_develop_resume.py`` (real git, faked turns).
"""

from __future__ import annotations

import json
from pathlib import Path

from lithos_loom.plugins.story_develop import checkpoint, run_outcome


def _run_dir(tmp_path: Path) -> Path:
    rd = tmp_path / "task-1" / "run-1"
    (rd / "handoff").mkdir(parents=True)
    return rd


def test_records_the_round_boundary_as_a_nested_block(tmp_path: Path) -> None:
    rd = _run_dir(tmp_path)
    checkpoint.record_round_checkpoint(
        rd,
        round_no=4,
        branch="add-a-thing-1a2b",
        head_sha="h" * 40,
        base_sha="b" * 40,
        base_ref="origin/main",
        commit="c" * 40,
        repo="/repos/foo",
        worktree="/wt/add-a-thing-1a2b",
        cost_usd=12.3456789,
    )

    data = json.loads((rd / run_outcome.STATE_FILE).read_text())
    # the run is RUNNING, and that must never read as a terminal verdict:
    # `status` stays out of the top level (run_phase's contract).
    assert "status" not in data
    assert data[checkpoint.CHECKPOINT_KEY]["status"] == "running"
    cp = checkpoint.round_checkpoint(rd)
    assert cp is not None
    assert (cp.round, cp.branch, cp.head_sha) == (4, "add-a-thing-1a2b", "h" * 40)
    assert (cp.base_sha, cp.base_ref, cp.commit) == ("b" * 40, "origin/main", "c" * 40)
    assert (cp.repo, cp.worktree) == ("/repos/foo", "/wt/add-a-thing-1a2b")
    assert cp.cost_usd == 12.3457  # rounded to cents-of-a-cent on the way out
    # with nothing carried, the branch's totals ARE this run's
    assert (cp.branch_rounds, cp.branch_cost_usd) == (4, 12.3457)
    assert cp.has_committed_round is True


def test_a_checkpointed_run_is_still_classified_as_running(tmp_path: Path) -> None:
    """The regression the nesting exists to prevent.

    A top-level ``status: running`` would make ``run_phase`` — which reads any
    top-level status as the run's terminal verdict — report a live run as
    finished the moment its first round landed, and ``develop attach`` would
    exit mid-run.
    """
    rd = _run_dir(tmp_path)
    checkpoint.record_round_checkpoint(
        rd, round_no=1, branch="b", head_sha="h" * 40, base_sha="a" * 40
    )
    state = run_outcome.read_state(rd)
    assert (
        run_outcome.run_phase(rd, state, containers_running=True, seen_container=True)
        == "running"
    )


def test_the_loops_terminal_write_keeps_the_checkpoint(tmp_path: Path) -> None:
    rd = _run_dir(tmp_path)
    checkpoint.record_round_checkpoint(
        rd, round_no=2, branch="b", head_sha="h" * 40, base_sha="a" * 40
    )
    run_outcome.write_state(rd, {"status": "infra_failed", "rounds": 2})

    state = run_outcome.read_state(rd)
    assert state is not None and state["status"] == "infra_failed"
    cp = checkpoint.from_state(state)
    assert cp is not None and cp.round == 2


def test_carried_figures_span_the_whole_branch(tmp_path: Path) -> None:
    """A resumed run's checkpoint records the BRANCH's rounds and spend.

    Without this a second infra death would resume on a budget that silently
    reset: the remainder is computed from the recorded branch totals.
    """
    rd = _run_dir(tmp_path)
    checkpoint.record_round_checkpoint(
        rd,
        round_no=2,  # this run's own second round…
        branch="b",
        head_sha="h" * 40,
        base_sha="a" * 40,
        cost_usd=5.0,
        branch_rounds=6,  # …the branch's sixth
        branch_cost_usd=21.5,
    )
    cp = checkpoint.round_checkpoint(rd)
    assert cp is not None
    assert (cp.round, cp.cost_usd) == (2, 5.0)
    assert (cp.branch_rounds, cp.branch_cost_usd) == (6, 21.5)


def test_no_checkpoint_for_an_absent_or_partial_block(tmp_path: Path) -> None:
    rd = _run_dir(tmp_path)
    assert checkpoint.round_checkpoint(rd) is None  # no state.json at all
    run_outcome.write_state(rd, {"status": "approved"})
    assert checkpoint.round_checkpoint(rd) is None  # a run that predates this
    for partial in (
        {"round": 0, "branch": "b", "head_sha": "h", "base_sha": "a"},  # none done
        {"round": True, "branch": "b", "head_sha": "h", "base_sha": "a"},  # not a round
        {
            "round": 2,
            "branch": "",
            "head_sha": "h",
            "base_sha": "a",
        },  # nothing to enter
        {"round": 2, "branch": "b", "base_sha": "a"},  # no head: it would guess
        {"round": 2, "branch": "b", "head_sha": "h"},  # no fork point: empty range
        "not a block",
    ):
        run_outcome.write_state(rd, {checkpoint.CHECKPOINT_KEY: partial})
        assert checkpoint.round_checkpoint(rd) is None


def test_a_round_that_committed_nothing_is_not_resumable(tmp_path: Path) -> None:
    """``head_sha == base_sha`` means the branch is still at its fork point."""
    rd = _run_dir(tmp_path)
    checkpoint.record_round_checkpoint(
        rd, round_no=1, branch="b", head_sha="a" * 40, base_sha="a" * 40
    )
    cp = checkpoint.round_checkpoint(rd)
    assert cp is not None and cp.has_committed_round is False
    assert checkpoint.resumable_checkpoint(rd) is None


def test_resumable_reasons_are_the_host_verdicts_only() -> None:
    # A stop that judges the WORK must not silently continue the branch — that
    # is a separate, bigger question (see the module docstring).
    assert set(checkpoint.RESUMABLE_ESCALATION_REASONS) == {
        "infra",
        "resume_exhausted",
    }
    for work_verdict in ("failed", "timeout", "contract_violation", "delivery"):
        assert work_verdict not in checkpoint.RESUMABLE_ESCALATION_REASONS


def test_a_write_failure_never_raises_at_the_round_boundary(tmp_path: Path) -> None:
    # The run is mid-loop with committed work; a raise here would throw it away.
    unwritable = tmp_path / "state.json" / "run"  # parent is a FILE
    (tmp_path / "state.json").write_text("x")
    checkpoint.record_round_checkpoint(
        unwritable, round_no=1, branch="b", head_sha="h", base_sha="a"
    )
    assert checkpoint.round_checkpoint(unwritable) is None


def test_an_interrupted_write_leaves_the_previous_checkpoint_intact(
    tmp_path: Path, monkeypatch
) -> None:
    """correctness/f-001: the checkpoint must survive the boundary it exists for.

    An in-place rewrite truncates first, so a host death between the truncation
    and the write left an empty ``state.json`` — no resumable checkpoint, the
    previous round's destroyed too, and the re-dispatch the operator authorised
    starting from scratch. The write is atomic, so a failure mid-write leaves
    round 4's checkpoint exactly as it was.
    """
    rd = _run_dir(tmp_path)
    checkpoint.record_round_checkpoint(
        rd, round_no=4, branch="b", head_sha="h" * 40, base_sha="a" * 40, cost_usd=4.64
    )
    before = (rd / run_outcome.STATE_FILE).read_bytes()

    def die(fd: int) -> None:  # the host dies with the new bytes unflushed
        raise OSError("host died mid-write")

    monkeypatch.setattr(run_outcome.os, "fsync", die)
    checkpoint.record_round_checkpoint(  # best-effort: must not raise
        rd, round_no=5, branch="b", head_sha="i" * 40, base_sha="a" * 40, cost_usd=9.99
    )

    assert (rd / run_outcome.STATE_FILE).read_bytes() == before
    cp = checkpoint.round_checkpoint(rd)
    assert cp is not None and cp.round == 4  # round 4 is still resumable
    # and no half-written temp file is left behind for `prune` to puzzle over
    assert [p.name for p in rd.iterdir() if p.name.endswith(".tmp")] == []


def test_a_block_whose_fields_contradict_each_other_is_not_a_checkpoint(
    tmp_path: Path,
) -> None:
    """correctness/f-005: type-valid is not valid.

    These are invariants the WRITER guarantees, and the two that matter are money
    and the review range: a `branch_rounds` below the round (or a
    `branch_cost_usd` below the run's own spend) hands a resume budget the branch
    has already used, which is the whole point of the slice. A record that breaks
    one is corrupt or not loom's, and falling back to a fresh run is safe.
    """
    rd = _run_dir(tmp_path)
    sound = {
        "round": 4,
        "branch": "b",
        "head_sha": "h" * 40,
        "base_sha": "a" * 40,
        "cost_usd": 4.0,
        "branch_rounds": 4,
        "branch_cost_usd": 4.0,
        "next_round": 0,
        "reviewed_round": 3,
    }
    run_outcome.write_state(rd, {checkpoint.CHECKPOINT_KEY: sound})
    assert checkpoint.round_checkpoint(rd) is not None  # the control

    for broken in (
        {"branch_rounds": 0},  # …the branch cannot have run fewer rounds
        {"branch_rounds": 3},
        {"branch_cost_usd": 0.0},  # …nor spent less than this run did
        {"next_round": 4},  # the contract is 0 or round + 1
        {"next_round": 6},
        {"reviewed_round": 5},  # a round that has not happened
    ):
        run_outcome.write_state(rd, {checkpoint.CHECKPOINT_KEY: {**sound, **broken}})
        assert checkpoint.round_checkpoint(rd) is None, broken


def test_the_entered_round_is_recorded_by_the_round_that_entered_it(
    tmp_path: Path,
) -> None:
    """correctness/f-004: a boundary never predicts the round after it.

    ``record_round_checkpoint`` has no ``next_round`` parameter at all — the next
    round amends the boundary when it starts — so a process killed between the
    two leaves "round N reached", never a permanent claim about a round that
    never began.
    """
    rd = _run_dir(tmp_path)
    checkpoint.record_round_checkpoint(
        rd, round_no=4, branch="b", head_sha="h" * 40, base_sha="a" * 40
    )
    cp = checkpoint.round_checkpoint(rd)
    assert cp is not None and cp.round == 4 and cp.next_round == 0

    checkpoint.record_round_entered(rd, 5)
    cp = checkpoint.round_checkpoint(rd)
    assert cp is not None and cp.round == 4 and cp.next_round == 5
    # …and the rest of the boundary is untouched by that amendment
    assert cp.head_sha == "h" * 40 and cp.base_sha == "a" * 40

    # a claim about anything but the boundary's successor is not made at all
    checkpoint.record_round_entered(rd, 9)
    assert checkpoint.round_checkpoint(rd).next_round == 5  # type: ignore[union-attr]


def test_entering_the_first_round_records_nothing(tmp_path: Path) -> None:
    # Nothing has been completed, so there is no boundary to amend — and the
    # observers fall back to the handoff names, which is right for round 1.
    rd = _run_dir(tmp_path)
    checkpoint.record_round_entered(rd, 1)
    assert checkpoint.round_checkpoint(rd) is None
