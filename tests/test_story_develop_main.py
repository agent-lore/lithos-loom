"""CLI-boundary validation tests for the story-develop entry point.

These exercise ``main()``'s fail-fast guards, which return before any Docker /
agent work happens.
"""

from __future__ import annotations

from pathlib import Path

from lithos_loom.plugins.story_develop.__main__ import main


def test_main_rejects_empty_description(tmp_git_repo: Path, capsys) -> None:
    rc = main(["--repo", str(tmp_git_repo), "--description", "   "])
    assert rc == 2
    assert "description must not be empty" in capsys.readouterr().err


def test_main_rejects_non_git_repo(tmp_path: Path, capsys) -> None:
    rc = main(["--repo", str(tmp_path), "--description", "do a thing"])
    assert rc == 2
    assert "not a git repository" in capsys.readouterr().err


def test_main_rejects_invalid_reviewer_name(tmp_git_repo: Path, capsys) -> None:
    rc = main(
        [
            "--repo",
            str(tmp_git_repo),
            "--description",
            "x",
            "--reviewer",
            "code quality",
        ]
    )
    assert rc == 2
    assert "invalid --reviewer" in capsys.readouterr().err


def test_main_rejects_bad_max_rounds(tmp_git_repo: Path, capsys) -> None:
    rc = main(["--repo", str(tmp_git_repo), "--description", "x", "--max-rounds", "0"])
    assert rc == 2
    assert "--max-rounds must be >= 1" in capsys.readouterr().err


def test_main_rejects_duplicate_reviewers(tmp_git_repo: Path, capsys) -> None:
    rc = main(
        [
            "--repo",
            str(tmp_git_repo),
            "--description",
            "x",
            "--reviewer",
            "cq",
            "--reviewer",
            "cq",
        ]
    )
    assert rc == 2
    assert "duplicate --reviewer" in capsys.readouterr().err


def test_main_rejects_reviewer_with_develop_config(
    tmp_git_repo: Path, tmp_path: Path, capsys
) -> None:
    cfg = tmp_path / "develop.toml"
    cfg.write_text("[[reviewers]]\nname = 'cq'\n")
    rc = main(
        [
            "--repo",
            str(tmp_git_repo),
            "--description",
            "x",
            "--reviewer",
            "other",
            "--develop-config",
            str(cfg),
        ]
    )
    assert rc == 2
    assert "mutually exclusive" in capsys.readouterr().err


def test_main_rejects_bad_develop_config(
    tmp_git_repo: Path, tmp_path: Path, capsys
) -> None:
    cfg = tmp_path / "develop.toml"
    cfg.write_text("[[reviewers]]\nname = 'Bad Name'\n")
    rc = main(
        [
            "--repo",
            str(tmp_git_repo),
            "--description",
            "x",
            "--develop-config",
            str(cfg),
        ]
    )
    assert rc == 2
    assert "must be a lowercase" in capsys.readouterr().err


def test_main_rejects_zero_pause_poll(tmp_git_repo: Path, capsys) -> None:
    # 0 would spin forever on zero-second pauses; negative would crash sleep()
    rc = main(
        [
            "--repo",
            str(tmp_git_repo),
            "--description",
            "x",
            "--pause-poll-minutes",
            "0",
        ]
    )
    assert rc == 2
    assert "--pause-poll-minutes must be >= 1" in capsys.readouterr().err


def test_main_rejects_negative_max_pause(tmp_git_repo: Path, capsys) -> None:
    rc = main(
        [
            "--repo",
            str(tmp_git_repo),
            "--description",
            "x",
            "--max-pause-minutes",
            "-1",
        ]
    )
    assert rc == 2
    assert "--max-pause-minutes must be >= 0" in capsys.readouterr().err
