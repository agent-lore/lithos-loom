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

import os
import subprocess
from pathlib import Path

import pytest

from lithos_loom.plugins.story_develop import check_runner
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


# --- the post-commit pass (resolve mode regenerates deterministically) --------


def _committed_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    (repo / "docs" / "generated").mkdir(parents=True)
    (repo / "docs" / "generated" / "metrics.json").write_text('{"lines": 10}\n')
    (repo / "src.py").write_text("x = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "round commit")
    return repo


def test_post_commit_regenerate_commits_what_the_generator_moved(
    tmp_path: Path,
) -> None:
    repo = _committed_repo(tmp_path)
    before = _git(repo, "rev-parse", "HEAD")
    calls: list[dict] = []

    def mutate(tree: Path) -> None:
        (tree / "docs/generated/metrics.json").write_text('{"lines": 99}\n')

    passer = gen.post_commit_regenerate(
        _config(repo, tmp_path), run_container=_fake_runner(calls, mutate=mutate)
    )
    assert passer is not None
    outcome = passer(repo, 2)
    sha = outcome.sha
    assert sha is not None and sha != before
    assert outcome.infra_error == ""
    # the generator's verdict rides with the commit as a GREEN required row
    assert outcome.row is not None and outcome.row.passed
    assert outcome.row.check.name == gen.REGENERATE_CHECK_NAME
    assert _git(repo, "rev-parse", "HEAD") == sha
    assert _git(repo, "log", "-1", "--format=%s") == "story-develop r2: regenerate"
    assert _git(repo, "show", "HEAD:docs/generated/metrics.json") == '{"lines": 99}'
    assert calls[0]["name"].endswith("-regenerate-r2")
    assert _git(repo, "status", "--porcelain") == ""


def test_post_commit_regenerate_is_a_no_op_when_nothing_moves(tmp_path: Path) -> None:
    repo = _committed_repo(tmp_path)
    before = _git(repo, "rev-parse", "HEAD")
    passer = gen.post_commit_regenerate(
        _config(repo, tmp_path), run_container=_fake_runner([])
    )
    assert passer is not None
    outcome = passer(repo, 1)
    assert outcome.sha is None and outcome.infra_error == ""
    assert outcome.row is not None and outcome.row.passed
    assert _git(repo, "rev-parse", "HEAD") == before


def test_post_commit_regenerate_reports_a_failed_generator_as_a_blocking_row(
    tmp_path: Path,
) -> None:
    """PR #388 review (High): a generator that says no is a REQUIRED raw-exit
    check row — it blocks approval through the floor, the coder reads its
    output next round, the epilogue names it — and nothing is committed."""
    repo = _committed_repo(tmp_path)
    before = _git(repo, "rev-parse", "HEAD")
    config = _config(repo, tmp_path)

    def mutate(tree: Path) -> None:  # a generator that wrote, then failed
        (tree / "docs/generated/metrics.json").write_text('{"lines": 99}\n')

    passer = gen.post_commit_regenerate(
        config, run_container=_fake_runner([], passed=False, mutate=mutate)
    )
    assert passer is not None

    outcome = passer(repo, 1)

    assert outcome.sha is None and outcome.infra_error == ""
    row = outcome.row
    assert row is not None
    assert row.check.name == "regenerate" and row.check.command == "make diagrams"
    assert row.check.state == "required" and row.check.raw_exit
    assert row.execution_outcome == "ran" and not row.passed
    assert row.gate is not None
    assert row.gate.exit_code == 2 and row.gate.output_tail == "gen"
    assert check_runner.check_result_blocks(row, None)
    assert _git(repo, "rev-parse", "HEAD") == before
    assert _git(repo, "status", "--porcelain") == ""
    # the output lands beside the round's gate output for the operator
    written = (config.gate_dir / "round_01" / "output_regenerate.txt").read_text()
    assert written.startswith("$ make diagrams\nexit: 2 (RED)")


def test_post_commit_regenerate_reports_a_timed_out_generator_as_blocking(
    tmp_path: Path,
) -> None:
    repo = _committed_repo(tmp_path)

    def timed_out(cmd, *, name, command, timeout):
        return GateResult(
            command=command, exit_code=124, passed=False, output_tail="killed"
        )

    passer = gen.post_commit_regenerate(
        _config(repo, tmp_path), run_container=timed_out
    )
    assert passer is not None
    outcome = passer(repo, 1)
    row = outcome.row
    assert outcome.sha is None and row is not None
    assert row.execution_outcome == "timed_out" and not row.passed
    assert row.gate is not None and row.gate.timed_out
    assert check_runner.check_result_blocks(row, None)


def test_post_commit_regenerate_that_cannot_run_is_an_infra_error_not_a_verdict(
    tmp_path: Path,
) -> None:
    """PR #388 review (High), the other half: an export / container /
    copy-back failure is no verdict on the tree — the pass reports it as the
    round's infra failure (terminal, host action named), never as a row the
    floor could read as "skipped" and never as silence."""
    repo = _committed_repo(tmp_path)
    before = _git(repo, "rev-parse", "HEAD")

    def boom(cmd, *, name, command, timeout):
        raise OSError("docker: not found")

    passer = gen.post_commit_regenerate(_config(repo, tmp_path), run_container=boom)
    assert passer is not None
    outcome = passer(repo, 1)
    assert outcome.sha is None and outcome.row is None
    assert "docker: not found" in outcome.infra_error
    assert "make diagrams" in outcome.infra_error
    assert "complete the gate" in outcome.host_action
    assert _git(repo, "rev-parse", "HEAD") == before


def test_post_commit_regenerate_is_absent_without_a_policy(tmp_path: Path) -> None:
    repo = _committed_repo(tmp_path)
    config = DevelopConfig(repo=repo, description="t", work_dir=tmp_path / "w")
    assert gen.post_commit_regenerate(config) is None


# --- a green generator whose output cannot be applied (review round 2) --------


def test_output_that_cannot_be_applied_after_a_green_run_is_the_generators_red(
    tmp_path: Path,
) -> None:
    """PR #388 review round 2 (Medium): the generator exited 0 but its output
    cannot land — a file became a directory (here), a path git refuses (next
    test). That is a defect of the project's generator or policy, never the
    host's: the result is RED with the generator's exit and the reason in
    its tail, so the pass reports a blocking row the coder reads and the run
    walks to the human gate — not ``infra_failed`` with "check docker"."""
    repo = _committed_repo(tmp_path)

    def mutate(tree: Path) -> None:
        target = tree / "docs/generated/metrics.json"
        target.unlink()
        target.mkdir()
        (target / "part.json").write_text("{}\n")

    result = gen.regenerate(
        _config(repo, tmp_path),
        repo,
        label="r1",
        run_container=_fake_runner([], mutate=mutate),
    )

    assert not result.ok
    assert result.exit_code == 0  # the generator's own verdict, kept honest
    assert "could not be applied" in result.error
    assert "metrics.json" in result.error


def test_a_gitignored_generated_path_is_the_generators_red_not_host_infra(
    tmp_path: Path,
) -> None:
    repo = _committed_repo(tmp_path)
    (repo / ".gitignore").write_text("*.log\n")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-q", "-m", "ignore logs")

    def mutate(tree: Path) -> None:
        (tree / "docs/generated/build.log").write_text("built\n")

    config = _config(repo, tmp_path)
    passer = gen.post_commit_regenerate(
        config, run_container=_fake_runner([], mutate=mutate)
    )
    assert passer is not None

    outcome = passer(repo, 1)

    assert outcome.infra_error == "" and outcome.sha is None
    row = outcome.row
    assert row is not None and not row.passed and row.gate is not None
    assert row.gate.exit_code == 0 and row.gate.verdict == "RED"
    assert "could not be applied" in row.gate.output_tail
    assert "build.log" in row.gate.output_tail
    assert check_runner.check_result_blocks(row, None)


# --- executable bits (review round 2) ----------------------------------------


def _scripted_repo(tmp_path: Path, *, executable: bool) -> Path:
    """A committed repo whose generated dir holds a script, committed as
    ``100755`` when *executable* else ``100644``."""
    repo = _committed_repo(tmp_path)
    script = repo / "docs" / "generated" / "run.sh"
    script.write_text("#!/bin/sh\necho hi\n")
    if executable:
        script.chmod(script.stat().st_mode | 0o111)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "script")
    return repo


def _index_mode(repo: Path, rel: str) -> str:
    return _git(repo, "ls-files", "--stage", "--", rel).split()[0]


def test_sync_copies_the_executable_bit_the_generator_set(tmp_path: Path) -> None:
    """PR #388 review (Medium): git tracks the executable bit, so a generator
    that only flips it has moved the file — and a fresh executable must land
    executable, not with the host's default mode."""
    repo = _scripted_repo(tmp_path, executable=False)

    def mutate(tree: Path) -> None:
        same = tree / "docs/generated/run.sh"  # content unchanged, mode only
        same.chmod(same.stat().st_mode | 0o111)
        new = tree / "docs/generated/new.sh"
        new.write_text("#!/bin/sh\n")
        new.chmod(0o755)

    result = gen.regenerate(
        _config(repo, tmp_path),
        repo,
        label="r1",
        run_container=_fake_runner([], mutate=mutate),
    )

    assert result.ok
    assert sorted(result.changed) == ["docs/generated/new.sh", "docs/generated/run.sh"]
    assert os.access(repo / "docs/generated/run.sh", os.X_OK)
    assert os.access(repo / "docs/generated/new.sh", os.X_OK)
    assert _index_mode(repo, "docs/generated/run.sh") == "100755"
    assert _index_mode(repo, "docs/generated/new.sh") == "100755"


def test_sync_clears_the_executable_bit_the_generator_dropped(tmp_path: Path) -> None:
    repo = _scripted_repo(tmp_path, executable=True)
    assert _index_mode(repo, "docs/generated/run.sh") == "100755"

    def mutate(tree: Path) -> None:
        script = tree / "docs/generated/run.sh"
        script.chmod(script.stat().st_mode & ~0o111)

    result = gen.regenerate(
        _config(repo, tmp_path),
        repo,
        label="r1",
        run_container=_fake_runner([], mutate=mutate),
    )

    assert result.ok and result.changed == ("docs/generated/run.sh",)
    assert not os.access(repo / "docs/generated/run.sh", os.X_OK)
    assert _index_mode(repo, "docs/generated/run.sh") == "100644"


def test_sync_leaves_an_unchanged_executable_alone(tmp_path: Path) -> None:
    repo = _scripted_repo(tmp_path, executable=True)
    result = gen.regenerate(
        _config(repo, tmp_path), repo, label="r1", run_container=_fake_runner([])
    )
    assert result.ok and result.changed == ()
    assert _index_mode(repo, "docs/generated/run.sh") == "100755"


# --- modify/delete + absent prefixes (review round 1) ------------------------


def test_take_base_side_removes_a_generated_file_the_base_deleted(
    tmp_path: Path,
) -> None:
    # theirs = the deletion: no stage-3 blob to check out; the path goes and
    # the generator recreates it if the composed tree still wants it
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    (repo / "docs" / "generated").mkdir(parents=True)
    (repo / "docs" / "generated" / "old.md").write_text("v0\n")
    (repo / "docs" / "generated" / "keep.md").write_text("v0\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "switch", "-q", "-c", "feature")
    (repo / "docs" / "generated" / "old.md").write_text("feature\n")
    (repo / "docs" / "generated" / "keep.md").write_text("feature\n")
    _git(repo, "commit", "-q", "-am", "feature")
    _git(repo, "switch", "-q", "main")
    (repo / "docs" / "generated" / "old.md").unlink()
    (repo / "docs" / "generated" / "keep.md").write_text("main\n")
    _git(repo, "commit", "-q", "-am", "main removes old, edits keep")
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "switch", "-q", "feature")
    conflicts = git.merge_no_commit(repo, base)
    assert sorted(conflicts) == ["docs/generated/keep.md", "docs/generated/old.md"]
    gen.take_base_side(repo, tuple(conflicts))
    assert not (repo / "docs/generated/old.md").exists()
    assert (repo / "docs/generated/keep.md").read_text() == "main\n"
    assert git.unmerged_paths(repo) == []
    assert git.write_tree(repo)  # the index is whole again


def test_regenerate_survives_a_declared_prefix_absent_from_both_trees(
    tmp_path: Path,
) -> None:
    repo = _committed_repo(tmp_path)
    config = DevelopConfig(
        repo=repo,
        description="t",
        work_dir=tmp_path / "work",
        generated_paths=("docs/generated", "api/client.py", "never/here"),
        regenerate_command="make gen",
    )
    result = gen.regenerate(config, repo, label="r1", run_container=_fake_runner([]))
    assert result.ok and result.changed == ()

    # ...and a prefix the generator brings into being is staged as an addition
    def mutate(tree: Path) -> None:
        (tree / "api").mkdir()
        (tree / "api" / "client.py").write_text("# generated\n")

    result = gen.regenerate(
        config, repo, label="r2", run_container=_fake_runner([], mutate=mutate)
    )
    assert result.ok and result.changed == ("api/client.py",)
    assert "api/client.py" in _git(repo, "diff", "--cached", "--name-only")
