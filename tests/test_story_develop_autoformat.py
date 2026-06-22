"""Tests for the auto-format-before-review pass (#134, ADR 0003 §4).

The formatter runs in the sandbox against an isolated ``git archive`` export of the
coder's commit; only a **successful** run's changes are applied back to the worktree
as a SEPARATE commit, so the gate + reviewers see that exact formatted tree. The pass
is best-effort — an absent / erroring / nonzero formatter is skipped, never fatal. The
container run is monkeypatched (no Docker): the fake formatter mutates the **export**
files in place (as a real formatter would), and real ``git`` captures the diff.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from lithos_loom.plugins.story_develop import autoformat, test_gate
from lithos_loom.plugins.story_develop.config import DevelopConfig
from lithos_loom.plugins.story_develop.test_gate import GateResult


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


def _export_dir_from_cmd(gate_cmd: list[str]) -> Path:
    """The host path bind-mounted at /workspace — the isolated export the formatter
    rewrites (NOT the live worktree)."""
    for i, arg in enumerate(gate_cmd):
        if arg == "-v":
            host, _, mount = gate_cmd[i + 1].rpartition(":")
            if mount == "/workspace":
                return Path(host)
    raise AssertionError("no /workspace mount in format cmd")


@pytest.fixture
def config(tmp_git_repo: Path, tmp_path: Path) -> DevelopConfig:
    cfg_dir = tmp_path / "fake-claude"
    cfg_dir.mkdir()
    return DevelopConfig(
        repo=tmp_git_repo,
        description="Add a greeting file",
        work_dir=tmp_path / "work",
        claude_config_dir=cfg_dir,
    )


# --- build_format_command: hardened, isolated, separate cache (#134 review) --


def test_build_format_command_is_network_isolated_and_uses_the_export(
    tmp_path: Path,
) -> None:
    export = tmp_path / "export"
    cache = tmp_path / "format_cache"
    cmd = autoformat.build_format_command(
        name="loom-format",
        image="img:1",
        tree=export,
        cache_dir=cache,
        command="ruff format",
    )
    # Formatters need no network — egress is denied (security/f-002).
    assert "--network" in cmd and cmd[cmd.index("--network") + 1] == "none"
    # The hardened profile is preserved.
    assert "--cap-drop" in cmd and "ALL" in cmd
    assert "no-new-privileges:true" in cmd
    # It mounts the ISOLATED export at /workspace, never the live worktree.
    assert f"{export}:/workspace" in cmd
    assert cmd[-1] == "ruff format" and cmd[-3] == "img:1"


def test_build_format_command_cache_is_separate_from_the_gate(
    tmp_path: Path,
) -> None:
    # The format-pass cache must not be the gate's cache dir, or a malicious formatter
    # could poison the package cache the "independent" gate later trusts (sec/f-002).
    cfg = DevelopConfig(
        repo=tmp_path / "repo",
        description="x",
        work_dir=tmp_path / "work",
        claude_config_dir=tmp_path / "cc",
    )
    gate_cache = cfg.gate_dir / "cache"
    format_cache = cfg.gate_dir / "format_cache"
    assert gate_cache != format_cache


# --- resolve_formatters: detection + image probe ----------------------------


def test_resolve_formatters_python_present(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig, tmp_git_repo: Path
) -> None:
    (tmp_git_repo / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    monkeypatch.setattr(test_gate, "probe_tools", lambda image, tools: ["ruff"])
    assert autoformat.resolve_formatters(config, tmp_git_repo) == ["ruff format"]


def test_resolve_formatters_markerless_repo_skips_probe(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig, tmp_git_repo: Path
) -> None:
    # No ecosystem marker -> no formatter, and the image is never probed.
    def _no_probe(image: str, tools: list[str]) -> list[str]:
        raise AssertionError("must not probe when there is nothing to format")

    monkeypatch.setattr(test_gate, "probe_tools", _no_probe)
    assert autoformat.resolve_formatters(config, tmp_git_repo) == []


def test_resolve_formatters_drops_absent_tool(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig, tmp_git_repo: Path
) -> None:
    # ruff applies but is absent from the image -> dropped (the pass is a no-op,
    # not a blocking placeholder — formatting is best-effort).
    (tmp_git_repo / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    monkeypatch.setattr(test_gate, "probe_tools", lambda image, tools: [])
    assert autoformat.resolve_formatters(config, tmp_git_repo) == []


# --- run_format_pass: success-gated, isolated, separate commit ---------------


def _commit_round(wt: Path, content: str) -> str:
    (wt / "greeting.txt").write_text(content)
    _git(wt, "add", "-A")
    _git(wt, "commit", "-m", "coder round commit")
    return _git(wt, "rev-parse", "HEAD")


def test_run_format_pass_commits_reformatted_tree(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig, tmp_git_repo: Path
) -> None:
    wt = tmp_git_repo
    head_before = _commit_round(wt, "unformatted\n")

    def _fake_run(gate_cmd, *, name, command, timeout) -> GateResult:
        # A real formatter rewrites source in the EXPORT (the mounted tree), not the
        # live worktree; the host applies its result back on success.
        (_export_dir_from_cmd(gate_cmd) / "greeting.txt").write_text("formatted\n")
        return GateResult(command=command, exit_code=0, passed=True, output_tail="")

    monkeypatch.setattr(test_gate, "run_gate_container", _fake_run)

    sha = autoformat.run_format_pass(config, wt, round_no=1, formatters=["ruff format"])

    assert sha is not None and sha != head_before
    assert _git(wt, "rev-parse", "HEAD") == sha
    # The formatting is its OWN commit on top of the coder's, not a rewrite of it.
    assert _git(wt, "rev-parse", "HEAD~1") == head_before
    assert "auto-format" in _git(wt, "log", "-1", "--format=%s")
    assert (wt / "greeting.txt").read_text() == "formatted\n"


def test_run_format_pass_noop_when_already_clean(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig, tmp_git_repo: Path
) -> None:
    wt = tmp_git_repo
    head_before = _commit_round(wt, "already clean\n")

    # Formatter runs but changes nothing in the export -> no commit, no SHA.
    monkeypatch.setattr(
        test_gate,
        "run_gate_container",
        lambda *a, command, **k: GateResult(
            command=command, exit_code=0, passed=True, output_tail=""
        ),
    )

    sha = autoformat.run_format_pass(config, wt, round_no=1, formatters=["ruff format"])

    assert sha is None
    assert _git(wt, "rev-parse", "HEAD") == head_before


def test_run_format_pass_discards_partial_edits_of_a_failed_formatter(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig, tmp_git_repo: Path
) -> None:
    # correctness/f-001: a formatter that rewrites some files and then exits nonzero
    # (e.g. another file is invalid, or it is killed mid-run) must NOT have its partial
    # edits committed — the gate + panel would otherwise review a half-formatted tree.
    wt = tmp_git_repo
    head_before = _commit_round(wt, "unformatted\n")

    def _partial_then_fail(gate_cmd, *, name, command, timeout) -> GateResult:
        (_export_dir_from_cmd(gate_cmd) / "greeting.txt").write_text("PARTIAL\n")
        return GateResult(
            command=command, exit_code=1, passed=False, output_tail="boom"
        )

    monkeypatch.setattr(test_gate, "run_gate_container", _partial_then_fail)

    sha = autoformat.run_format_pass(config, wt, round_no=1, formatters=["ruff format"])

    assert sha is None
    # The worktree is untouched — the partial edit stayed in the discarded export.
    assert _git(wt, "rev-parse", "HEAD") == head_before
    assert (wt / "greeting.txt").read_text() == "unformatted\n"


def test_run_format_pass_does_not_mutate_handoff_on_disk(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig, tmp_git_repo: Path
) -> None:
    # security/f-001: the formatter must not rewrite the on-disk orchestration trust
    # channel. `.handoff` lives only in the worktree (untracked), so it is absent from
    # the export and can never be touched — even by a formatter that tries.
    wt = tmp_git_repo
    _commit_round(wt, "unformatted\n")
    handoff_dir = wt / ".handoff"
    handoff_dir.mkdir()
    (handoff_dir / "round_01_review.md").write_text("## Status: LGTM\n")

    def _fake_run(gate_cmd, *, name, command, timeout) -> GateResult:
        export = _export_dir_from_cmd(gate_cmd)
        # The export never contains .handoff (untracked), so a real formatter can't see
        # it; assert that and reformat a tracked file.
        assert not (export / ".handoff").exists()
        (export / "greeting.txt").write_text("formatted\n")
        return GateResult(command=command, exit_code=0, passed=True, output_tail="")

    monkeypatch.setattr(test_gate, "run_gate_container", _fake_run)

    autoformat.run_format_pass(config, wt, round_no=1, formatters=["ruff format"])

    assert (handoff_dir / "round_01_review.md").read_text() == "## Status: LGTM\n"
    # And `.handoff` stayed out of the deliverable commit.
    tracked = _git(wt, "ls-tree", "-r", "--name-only", "HEAD")
    assert ".handoff" not in tracked


def test_run_format_pass_no_formatters_runs_no_container(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig, tmp_git_repo: Path
) -> None:
    def _boom(*a, **k):
        raise AssertionError("must not run a container with no formatters")

    monkeypatch.setattr(test_gate, "run_gate_container", _boom)
    assert autoformat.run_format_pass(config, tmp_git_repo, 1, []) is None


def test_run_format_pass_container_error_is_skipped(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig, tmp_git_repo: Path
) -> None:
    # An infra failure (e.g. Docker down) must not crash the run — formatting is
    # best-effort; the pass returns None and the round proceeds unformatted.
    wt = tmp_git_repo
    head_before = _commit_round(wt, "unformatted\n")

    def _raise(*a, **k) -> GateResult:
        raise RuntimeError("simulated docker failure")

    monkeypatch.setattr(test_gate, "run_gate_container", _raise)

    sha = autoformat.run_format_pass(config, wt, 1, ["ruff format"])

    assert sha is None
    assert _git(wt, "rev-parse", "HEAD") == head_before
