"""Tests for ``lithos-loom develop resume`` (5dbeb0c8 slice C, the on-demand half).

The planning and the refusal texts are the shared resume path's
(``test_story_develop_resume.py``); what is pinned here is the command around
them: resolving the run, taking the repo / task text off the run's own records,
handing the loop the remainder config plus the entry, and refusing without
starting a container.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import typer

from lithos_loom.cli import resume as resume_mod
from lithos_loom.plugins.story_develop import checkpoint


def _repo(tmp_path: Path) -> tuple[Path, str, str]:
    repo = tmp_path / "repo"
    repo.mkdir()

    def run(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=repo, check=True, capture_output=True, text=True
        ).stdout.strip()

    run("init", "-b", "main")
    run("config", "user.email", "t@example.com")
    run("config", "user.name", "T")
    (repo / "f.txt").write_text("base\n")
    run("add", "-A")
    run("commit", "-m", "base")
    base = run("rev-parse", "HEAD")
    (repo / "f.txt").write_text("round 1\n")
    run("commit", "-am", "round 1")
    return repo, base, run("rev-parse", "HEAD")


@pytest.fixture
def host(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """``load_config`` → a fake host rooted at ``tmp_path/work``."""
    work_dir = tmp_path / "work"
    monkeypatch.setattr(
        resume_mod,
        "load_config",
        lambda config=None: SimpleNamespace(
            orchestrator=SimpleNamespace(work_dir=work_dir),
            story_develop=SimpleNamespace(
                default_models={"claude": "test-claude-model"}
            ),
        ),
    )
    return work_dir


def _dead_run(
    work_dir: Path,
    *,
    repo: Path,
    base: str,
    head: str,
    rounds: int = 2,
    cost: float = 3.0,
    task: dict[str, Any] | None = None,
) -> Path:
    run_dir = work_dir / "t-1" / "dead"
    (run_dir / "handoff").mkdir(parents=True)
    (run_dir / "task.json").write_text(
        json.dumps(
            {
                "task": task
                or {
                    "id": "t-1",
                    "title": "Add a flag",
                    "description": "Body.",
                    "metadata": {},
                }
            }
        )
    )
    checkpoint.record_round_checkpoint(
        run_dir,
        round_no=rounds,
        branch="story-dead",
        head_sha=head,
        base_sha=base,
        repo=str(repo),
        cost_usd=cost,
    )
    return run_dir


def test_resume_hands_the_loop_the_branch_and_the_remainder(
    host: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    repo, base, head = _repo(tmp_path)
    _dead_run(host, repo=repo, base=base, head=head)
    seen: dict[str, Any] = {}

    def fake_develop(config, **kw):
        seen["config"] = config
        seen["entry"] = kw.get("entry")
        return SimpleNamespace(
            run_id=config.run_id,
            status="approved",
            approved=True,
            message="approved in 1 round(s)",
        )

    monkeypatch.setattr(resume_mod, "develop", fake_develop)

    resume_mod.resume_command(
        run="dead",
        repo=None,
        story=None,
        no_story_settings=True,
        description=None,
        profile=None,
        max_rounds=6,
        max_cost=None,
        image=None,
        base=None,
        dry_run=False,
        config=None,
    )

    config = seen["config"]
    assert config.repo == repo  # from the checkpoint, no --repo needed
    assert config.description == "Add a flag\n\nBody."  # the run's own snapshot
    assert config.work_dir == host / "t-1"  # beside the run it continues
    assert config.max_rounds == 4  # 6 - the 2 already landed
    assert config.coder_model == "test-claude-model"  # the host model policy ran
    assert seen["entry"].carried_rounds == 2
    out = capsys.readouterr().out
    assert "resuming run dead at round 2" in out
    assert "develop deliver" in out  # the branch is local only


def test_dry_run_resolves_everything_and_starts_nothing(
    host: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    repo, base, head = _repo(tmp_path)
    _dead_run(host, repo=repo, base=base, head=head)

    def boom(*a, **k):  # pragma: no cover - must not be reached
        raise AssertionError("--dry-run must not start the loop")

    monkeypatch.setattr(resume_mod, "develop", boom)

    with pytest.raises(typer.Exit) as exc:
        resume_mod.resume_command(
            run="dead",
            repo=None,
            story=None,
            no_story_settings=True,
            description=None,
            profile=None,
            max_rounds=None,
            max_cost=None,
            image=None,
            base=None,
            dry_run=True,
            config=None,
        )
    assert exc.value.exit_code == 0
    assert "nothing started" in capsys.readouterr().out


def _expect_refusal(**overrides: Any) -> None:
    kwargs: dict[str, Any] = {
        "run": "dead",
        "repo": None,
        "story": None,
        "no_story_settings": True,
        "description": None,
        "profile": None,
        "max_rounds": None,
        "max_cost": None,
        "image": None,
        "base": None,
        "dry_run": False,
        "config": None,
    }
    kwargs.update(overrides)
    with pytest.raises(typer.Exit) as exc:
        resume_mod.resume_command(**kwargs)
    assert exc.value.exit_code == 2


def test_refuses_an_unknown_run(host: Path, capsys) -> None:
    _expect_refusal(run="nope")
    assert "no run dir for 'nope'" in capsys.readouterr().err


def test_refuses_a_run_with_no_checkpoint(host: Path, tmp_path: Path, capsys) -> None:
    run_dir = host / "t-1" / "dead"
    (run_dir / "handoff").mkdir(parents=True)

    _expect_refusal()

    assert "recorded no round boundary" in capsys.readouterr().err


def test_refuses_a_converge_run(host: Path, tmp_path: Path, capsys) -> None:
    """A converge run's rounds belong on the PR it was converging."""
    run_dir = host / "converge" / "dead"
    (run_dir / "handoff").mkdir(parents=True)

    _expect_refusal()

    assert "converge-push" in capsys.readouterr().err


def test_refuses_without_a_repo_to_work_in(host: Path, tmp_path: Path, capsys) -> None:
    run_dir = host / "t-1" / "dead"
    (run_dir / "handoff").mkdir(parents=True)
    checkpoint.record_round_checkpoint(
        run_dir,
        round_no=1,
        branch="story-dead",
        head_sha="ba" * 20,
        base_sha="b" * 40,  # no repo recorded (an older checkpoint)
    )

    _expect_refusal(description="Add a flag")  # the task text is not the gap here

    assert "records no repo" in capsys.readouterr().err
