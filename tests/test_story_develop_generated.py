"""Tests for ``lithos_loom.plugins.story_develop.generated`` — PRD
pr-reconciliation S4, the loom half: generated artifacts are REGENERATED on
a merge, never merged.

A project declares which paths are generated and the command that
regenerates them; loom's own merges (the S3 trial merge, the S5 resolve
intake) then take either side of a conflict in those paths and run the
generator on the composed tree instead of asking anyone to merge text no
generator would emit. Real git for the merge topology; the container run
is stubbed through the same seam the gate uses.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from lithos_loom.plugins.story_develop import generated as gen
from lithos_loom.plugins.story_develop.config import DevelopConfig
from lithos_loom.plugins.story_develop.test_gate import GateResult
from lithos_loom.runner import git


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


# --- the policy ---------------------------------------------------------------


def test_parse_generated_paths_normalises_prefixes() -> None:
    assert gen.parse_generated_paths(None, where="x") == ()
    assert gen.parse_generated_paths(
        ["docs/generated/", "./api/client.py", "docs/generated"], where="x"
    ) == ("docs/generated", "api/client.py")


@pytest.mark.parametrize(
    "value",
    [
        "docs/generated",  # a bare string is not a list
        ["/abs/path"],
        ["../up"],
        ["docs/../x"],
        [""],
        ["  "],
        [".", "docs"],
        [42],
        ["docs/generated", "docs/generated/sub"],  # nested prefixes: declare the outer
    ],
)
def test_parse_generated_paths_rejects_garbage(value: object) -> None:
    with pytest.raises(ValueError, match="generated_paths"):
        gen.parse_generated_paths(value, where="x")


def test_parse_regenerate_command_mirrors_parity() -> None:
    assert gen.parse_regenerate_command(None, where="x") is None
    assert (
        gen.parse_regenerate_command("  make diagrams ", where="x") == "make diagrams"
    )
    for bad in ("", "   ", 3):
        with pytest.raises(ValueError, match="regenerate_command"):
            gen.parse_regenerate_command(bad, where="x")


def test_is_generated_matches_on_path_segments() -> None:
    prefixes = ("docs/generated", "api/client.py")
    assert gen.is_generated("docs/generated/metrics.json", prefixes)
    assert gen.is_generated("docs/generated", prefixes)
    assert gen.is_generated("api/client.py", prefixes)
    assert not gen.is_generated("docs/generated2/x.md", prefixes)
    assert not gen.is_generated("docs/generate/x.md", prefixes)
    assert not gen.is_generated("api/client.pyi", prefixes)
    assert not gen.is_generated("src/app.py", ())


def test_partition_conflicts_keeps_order() -> None:
    generated, real = gen.partition_conflicts(
        ["src/a.py", "docs/generated/m.json", "docs/generated/m.md", "tests/t.py"],
        ("docs/generated",),
    )
    assert generated == ("docs/generated/m.json", "docs/generated/m.md")
    assert real == ("src/a.py", "tests/t.py")


# --- taking a side, on a real in-progress merge ------------------------------


def _conflicting_repo(tmp_path: Path) -> tuple[Path, str, str]:
    """A repo whose ``feature`` and ``main`` both rewrote the generated
    metrics AND a source file; returns ``(repo, head, base)``."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    (repo / "docs" / "generated").mkdir(parents=True)
    (repo / "docs" / "generated" / "metrics.json").write_text('{"lines": 10}\n')
    (repo / "docs" / "generated" / "old.md").write_text("old\n")
    (repo / "src.py").write_text("x = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "switch", "-q", "-c", "feature")
    (repo / "docs" / "generated" / "metrics.json").write_text('{"lines": 12}\n')
    (repo / "src.py").write_text("x = 2\n")
    _git(repo, "commit", "-q", "-am", "feature")
    head = _git(repo, "rev-parse", "HEAD")
    _git(repo, "switch", "-q", "main")
    (repo / "docs" / "generated" / "metrics.json").write_text('{"lines": 11}\n')
    (repo / "src.py").write_text("x = 3\n")
    _git(repo, "commit", "-q", "-am", "main moved")
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "switch", "-q", "feature")
    return repo, head, base


def test_take_base_side_resolves_the_generated_conflict_only(tmp_path: Path) -> None:
    repo, _head, base = _conflicting_repo(tmp_path)
    conflicts = git.merge_no_commit(repo, base)
    assert sorted(conflicts) == ["docs/generated/metrics.json", "src.py"]
    generated, real = gen.partition_conflicts(conflicts, ("docs/generated",))
    gen.take_base_side(repo, generated)
    # the generated file is the base's copy, staged, marker-free; the real
    # conflict is untouched and still unmerged
    assert (repo / "docs/generated/metrics.json").read_text() == '{"lines": 11}\n'
    assert git.unmerged_paths(repo) == ["src.py"]
    assert git.conflict_markers(repo, list(conflicts)) == ["src.py"]
    assert git.merge_head(repo) == base


# --- regenerate(): export → container → copy back ---------------------------


def _config(repo: Path, tmp_path: Path, **kw) -> DevelopConfig:
    return DevelopConfig(
        repo=repo,
        description="t",
        work_dir=tmp_path / "work",
        generated_paths=("docs/generated",),
        regenerate_command="make diagrams",
        **kw,
    )


def _fake_runner(calls: list[dict], *, passed: bool = True, mutate=None):
    def run(cmd, *, name, command, timeout):
        # the export dir is the tree mount in the argv
        tree = Path(next(a for a in cmd if ":/workspace" in a).split(":")[0])
        calls.append(
            {
                "cmd": cmd,
                "name": name,
                "command": command,
                "tree": tree,
                "timeout": timeout,
                # what the generator SAW — the export is cleaned up afterwards
                "src_seen": (tree / "src.py").read_text(),
                "metrics_seen": (tree / "docs/generated/metrics.json").read_text(),
            }
        )
        if mutate is not None:
            mutate(tree)
        return GateResult(
            command=command,
            exit_code=0 if passed else 2,
            passed=passed,
            output_tail="gen",
        )

    return run


def _regen_mutation(tree: Path) -> None:
    # what a generator does on the composed tree: rewrite, add, delete — and
    # touch something OUTSIDE the declared paths, which must never come back
    (tree / "docs/generated/metrics.json").write_text('{"lines": 23}\n')
    (tree / "docs/generated/new.md").write_text("new page\n")
    (tree / "docs/generated/old.md").unlink()
    (tree / "src.py").write_text("x = 999\n")
    (tree / "docs/generated/leak").symlink_to("/etc/hostname")


def test_regenerate_runs_the_command_on_the_composed_tree_and_syncs_back(
    tmp_path: Path,
) -> None:
    repo, _head, base = _conflicting_repo(tmp_path)
    conflicts = git.merge_no_commit(repo, base)
    gen.take_base_side(repo, gen.partition_conflicts(conflicts, ("docs/generated",))[0])
    # resolve the real conflict too, so the composed tree is whole
    (repo / "src.py").write_text("x = 5\n")
    _git(repo, "add", "src.py")
    calls: list[dict] = []
    config = _config(repo, tmp_path, image="img:1")

    result = gen.regenerate(
        config,
        repo,
        label="merge",
        run_container=_fake_runner(calls, mutate=_regen_mutation),
    )

    assert result.ok and result.exit_code == 0
    assert len(calls) == 1
    call = calls[0]
    assert call["command"] == "make diagrams" and "img:1" in call["cmd"]
    assert (
        call["name"].endswith("-regenerate-merge")
        and call["timeout"] == config.test_timeout
    )
    # the export the generator ran on is the COMPOSED tree (the index): the
    # resolved source and the base's copy of the generated file, not HEAD's
    assert call["src_seen"] == "x = 5\n" and call["metrics_seen"] == '{"lines": 11}\n'
    # synced back under the declared prefix only: changed, added, deleted
    assert (repo / "docs/generated/metrics.json").read_text() == '{"lines": 23}\n'
    assert (repo / "docs/generated/new.md").read_text() == "new page\n"
    assert not (repo / "docs/generated/old.md").exists()
    assert not (repo / "docs/generated/leak").exists()  # symlinks never copied
    assert (repo / "src.py").read_text() == "x = 5\n"  # outside the policy: untouched
    assert sorted(result.changed) == [
        "docs/generated/metrics.json",
        "docs/generated/new.md",
        "docs/generated/old.md",
    ]
    # ...and staged, so the merge commit carries them
    staged = _git(repo, "diff", "--cached", "--name-only").splitlines()
    assert set(result.changed) <= set(staged)
    assert git.merge_head(repo) == base  # the merge is still in progress
    assert not call["tree"].exists()  # the export is cleaned up


def test_regenerate_failure_leaves_the_tree_alone(tmp_path: Path) -> None:
    repo, _head, base = _conflicting_repo(tmp_path)
    git.merge_no_commit(repo, base)
    gen.take_base_side(repo, ("docs/generated/metrics.json",))
    (repo / "src.py").write_text("x = 5\n")
    _git(repo, "add", "src.py")
    calls: list[dict] = []
    result = gen.regenerate(
        config=_config(repo, tmp_path),
        wt=repo,
        label="merge",
        run_container=_fake_runner(calls, passed=False, mutate=_regen_mutation),
    )
    assert not result.ok and result.exit_code == 2 and result.output_tail == "gen"
    assert result.changed == ()
    assert (repo / "docs/generated/metrics.json").read_text() == '{"lines": 11}\n'
    assert (repo / "docs/generated/old.md").exists()
    assert not (repo / "docs/generated/new.md").exists()


def test_regenerate_reports_an_unchanged_tree(tmp_path: Path) -> None:
    repo, _head, base = _conflicting_repo(tmp_path)
    git.merge_no_commit(repo, base)
    gen.take_base_side(repo, ("docs/generated/metrics.json",))
    (repo / "src.py").write_text("x = 5\n")
    _git(repo, "add", "src.py")
    result = gen.regenerate(
        _config(repo, tmp_path), repo, label="merge", run_container=_fake_runner([])
    )
    assert result.ok and result.changed == ()


def test_regenerate_refuses_without_a_policy(tmp_path: Path) -> None:
    repo, _head, _base = _conflicting_repo(tmp_path)
    config = DevelopConfig(repo=repo, description="t", work_dir=tmp_path / "w")
    with pytest.raises(ValueError, match="regenerate"):
        gen.regenerate(config, repo, label="merge", run_container=_fake_runner([]))


def test_export_or_container_errors_are_a_failed_result_not_an_exception(
    tmp_path: Path,
) -> None:
    repo, _head, base = _conflicting_repo(tmp_path)
    git.merge_no_commit(repo, base)
    gen.take_base_side(repo, ("docs/generated/metrics.json",))
    (repo / "src.py").write_text("x = 5\n")
    _git(repo, "add", "src.py")

    def boom(cmd, *, name, command, timeout):
        raise OSError("docker: not found")

    result = gen.regenerate(
        _config(repo, tmp_path), repo, label="merge", run_container=boom
    )
    assert not result.ok and result.exit_code is None
    assert "docker: not found" in result.error
