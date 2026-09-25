"""Tests for the resume entry (5dbeb0c8 slice C): planning, refusals, intake.

The end-to-end "run 1 dies, run 2 continues its branch" test runs the real loop
against a real git repo in ``test_story_develop_core.py``, and the runner's half
of the wiring (which stops point a dispatch at a dead run) is in
``test_route_runner.py``; here the checkpoint is written by hand so each refusal
and the intake selection can be pinned on their own.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from lithos_loom.plugins.story_develop import checkpoint
from lithos_loom.plugins.story_develop.config import DevelopConfig
from lithos_loom.plugins.story_develop.daemon_io import read_resume_run_dir
from lithos_loom.plugins.story_develop.handoff import (
    coder_handoff_name,
    reviewer_handoff_name,
)
from lithos_loom.plugins.story_develop.resume import prepare_resume

_FINDINGS = (
    "## Status: FINDINGS\n## Summary\nOne issue.\n## Findings\n"
    "- finding_id: f-001\n  severity: major\n  status: open\n"
    '  files: ["greeting.txt:1"]\n  rationale: {rationale}\n'
)


def _repo(tmp_path: Path) -> tuple[Path, str, str]:
    """A repo with two commits: ``(repo, base_sha, head_sha)``."""
    repo = tmp_path / "repo"
    repo.mkdir()
    run = lambda *args: subprocess.run(  # noqa: E731 - test-local shorthand
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    )
    run("init", "-b", "main")
    run("config", "user.email", "t@example.com")
    run("config", "user.name", "T")
    (repo / "greeting.txt").write_text("base\n")
    run("add", "-A")
    run("commit", "-m", "base")
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True
    ).stdout.strip()
    (repo / "greeting.txt").write_text("round 1\n")
    run("commit", "-am", "round 1")
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True
    ).stdout.strip()
    return repo, base, head


def _dead_run(
    tmp_path: Path,
    *,
    base: str,
    head: str,
    repo: Path,
    rounds: int = 2,
    cost: float = 4.0,
    reviews: dict[int, str] | None = None,
) -> Path:
    """A run dir shaped like one the host killed after *rounds* rounds."""
    run_dir = tmp_path / "work" / "task-1" / "dead"
    handoff_dir = run_dir / "handoff"
    handoff_dir.mkdir(parents=True)
    for rnd, text in (reviews or {}).items():
        (handoff_dir / reviewer_handoff_name(rnd, "code-quality")).write_text(text)
        (handoff_dir / coder_handoff_name(rnd)).write_text(
            "## Status: LGTM\n## Summary\nwork\n"
        )
    checkpoint.record_round_checkpoint(
        run_dir,
        round_no=rounds,
        branch="add-a-greeting-dead",
        head_sha=head,
        base_sha=base,
        base_ref="main",
        repo=str(repo),
        worktree=str(tmp_path / "wt" / "add-a-greeting-dead"),
        cost_usd=cost,
    )
    return run_dir


def _config(repo: Path, tmp_path: Path, **overrides) -> DevelopConfig:
    return replace(
        DevelopConfig(
            repo=repo,
            description="Add a greeting file",
            work_dir=tmp_path / "work" / "task-1",
        ),
        **overrides,
    )


def test_plans_the_entry_from_the_checkpoint(tmp_path: Path) -> None:
    repo, base, head = _repo(tmp_path)
    run_dir = _dead_run(
        tmp_path,
        base=base,
        head=head,
        repo=repo,
        reviews={
            1: _FINDINGS.format(rationale="first round finding"),
            2: _FINDINGS.format(rationale="the live finding"),
        },
    )

    resumption, refused = prepare_resume(
        _config(repo, tmp_path, max_rounds=8, max_cost_usd=20.0), run_dir
    )

    assert refused == "" and resumption is not None
    # the REMAINDER of the branch's budgets
    assert resumption.config.max_rounds == 6
    assert resumption.config.max_cost_usd == 16.0
    entry = resumption.entry
    # the review range is the dead run's fork point, not its head (else the
    # resumed run would review an empty diff)
    assert entry.base_override.start_sha == base
    assert entry.base_override.ref == "main"
    # the LAST review round is the intake, and it carries that round's findings
    assert resumption.plan.intake_round == 2
    assert [f.rationale.strip() for f in entry.intake_reviews[0].findings] == [
        "the live finding"
    ]
    assert entry.coder_init_template == "resume_coder_init.md"
    assert entry.carried_rounds == 2 and entry.carried_cost_usd == 4.0
    # the worktree factory positions a FRESH branch at the dead run's head: the
    # dead worktree still has its own checked out, and git allows one checkout
    # per branch.
    wt = entry.worktree_factory(resumption.config)
    assert (
        subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=wt, capture_output=True, text=True
        ).stdout.strip()
        == head
    )
    assert wt.name != "add-a-greeting-dead"
    assert "resuming run dead at round 2" in resumption.note


def test_intake_falls_back_when_the_last_round_was_never_reviewed(
    tmp_path: Path,
) -> None:
    """A run that died in its coder turn has no review for the round it was in."""
    repo, base, head = _repo(tmp_path)
    run_dir = _dead_run(
        tmp_path,
        base=base,
        head=head,
        repo=repo,
        rounds=3,
        reviews={1: _FINDINGS.format(rationale="round one finding")},
    )

    resumption, _ = prepare_resume(_config(repo, tmp_path), run_dir)

    assert resumption is not None
    assert resumption.plan.intake_round == 1
    assert resumption.entry.intake_reviews[0].findings[0].rationale.strip() == (
        "round one finding"
    )


def test_intake_is_empty_but_present_when_nothing_was_reviewed(
    tmp_path: Path,
) -> None:
    repo, base, head = _repo(tmp_path)
    run_dir = _dead_run(tmp_path, base=base, head=head, repo=repo, rounds=1)

    resumption, _ = prepare_resume(_config(repo, tmp_path), run_dir)

    assert resumption is not None
    assert resumption.plan.intake_round == 0
    # ONE empty outcome, so the round-1 prompt renders "no structured findings"
    # rather than a blank section (and `intake_reviews is not None` still
    # selects the cold-start entry).
    assert len(resumption.entry.intake_reviews) == 1
    assert resumption.entry.intake_reviews[0].findings == []


def test_an_unparseable_handoff_does_not_fail_the_resume(tmp_path: Path) -> None:
    repo, base, head = _repo(tmp_path)
    run_dir = _dead_run(
        tmp_path,
        base=base,
        head=head,
        repo=repo,
        reviews={
            1: _FINDINGS.format(rationale="round one finding"),
            2: "this is not a handoff at all\n",
        },
    )

    resumption, _ = prepare_resume(_config(repo, tmp_path), run_dir)

    # the branch is the work; a handoff is a breadcrumb — fall back a round
    assert resumption is not None and resumption.plan.intake_round == 1


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"max_rounds": 2}, "meeting the max_rounds ceiling of 2"),
        ({"max_rounds": 8, "max_cost_usd": 4.0}, "already spent $4.00"),
    ],
)
def test_refuses_when_the_branch_already_meets_a_ceiling(
    tmp_path: Path, kwargs: dict, expected: str
) -> None:
    """Continuing buys nothing — so the caller does what it did before."""
    repo, base, head = _repo(tmp_path)
    run_dir = _dead_run(tmp_path, base=base, head=head, repo=repo, cost=4.0)

    resumption, refused = prepare_resume(_config(repo, tmp_path, **kwargs), run_dir)

    assert resumption is None and expected in refused


def test_refuses_a_run_with_no_committed_round(tmp_path: Path) -> None:
    repo, base, _head = _repo(tmp_path)
    run_dir = _dead_run(tmp_path, base=base, head=base, repo=repo, rounds=1)

    resumption, refused = prepare_resume(_config(repo, tmp_path), run_dir)

    assert resumption is None
    assert "no round boundary with a commit" in refused


def test_refuses_a_head_the_repo_no_longer_has(tmp_path: Path) -> None:
    repo, base, _head = _repo(tmp_path)
    run_dir = _dead_run(tmp_path, base=base, head="f" * 40, repo=repo)

    resumption, refused = prepare_resume(_config(repo, tmp_path), run_dir)

    assert resumption is None
    assert "is no longer in" in refused


# --- the plugin's half of the wiring ----------------------------------------


def test_the_plugin_reads_the_pointer_tolerantly(tmp_path: Path) -> None:
    """A resume pointer is an optimisation; it must never fail a dispatch."""
    envelope = tmp_path / "task.json"
    task = {"task": {"id": "t-1", "title": "A story"}}
    envelope.write_text(json.dumps({**task, "resume": {"run_dir": "/runs/dead"}}))
    assert read_resume_run_dir(envelope) == Path("/runs/dead")
    for bad in (
        task,  # no resume block: the ordinary dispatch
        {**task, "resume": {}},
        {**task, "resume": {"run_dir": "   "}},
        {**task, "resume": "dead"},
        [],
    ):
        envelope.write_text(json.dumps(bad))
        assert read_resume_run_dir(envelope) is None
    envelope.write_text("{not json")
    assert read_resume_run_dir(envelope) is None
    assert read_resume_run_dir(tmp_path / "nope.json") is None
