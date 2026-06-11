"""Orchestration tests for ``develop()`` (T2: coder + one reviewer pass).

Real git/worktree against a temp repo; the containers + turns are monkeypatched
so no Docker or agent is needed. A single fake ``run_turn`` plays both roles,
branching on the container name, and writes the appropriate handoff files.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from lithos_loom.plugins.story_develop import containers, handoff
from lithos_loom.plugins.story_develop import develop as develop_mod
from lithos_loom.plugins.story_develop.config import DevelopConfig
from lithos_loom.plugins.story_develop.turns import TurnResult

_LGTM = "## Status: LGTM\n## Summary\nLooks correct and complete.\n"
_FINDINGS_MAJOR = (
    "## Status: FINDINGS\n## Summary\nOne issue.\n## Findings\n"
    "- finding_id: f-001\n  severity: major\n  status: open\n"
    '  files: ["greeting.txt:1"]\n  rationale: needs work\n  coder_response:\n'
)
_FINDINGS_MINOR = _FINDINGS_MAJOR.replace("severity: major", "severity: minor")
_GARBAGE = "this is not a valid handoff at all\n"


def _worktree_from_run_cmd(run_cmd) -> Path:
    # the worktree mount is "<src>:/workspace" (coder) or "<src>:/workspace:ro"
    # (reviewer); the handoff mount "<src>:/workspace/.handoff" must not match.
    for i, arg in enumerate(run_cmd):
        if arg == "-v":
            parts = run_cmd[i + 1].split(":")
            if len(parts) >= 2 and parts[1] == "/workspace":
                return Path(parts[0])
    raise AssertionError("no /workspace mount in run cmd")


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


def _install_fakes(
    monkeypatch: pytest.MonkeyPatch,
    config: DevelopConfig,
    *,
    coder_ok: bool = True,
    write_handoff: bool = True,
    write_source: bool = True,
    review_first: str | None = _LGTM,
    review_retry: str | None = None,
) -> dict:
    state: dict = {"stopped": []}

    def fake_start(run_cmd) -> str:
        state["worktree"] = _worktree_from_run_cmd(run_cmd)
        return "cid"

    review_path = config.handoff_dir / handoff.reviewer_handoff_name(1, config.reviewer)

    def fake_run_turn(*, container, prompt, session_id, resume=False, timeout):
        wt = state["worktree"]
        if "-coder" in container:
            if write_source:
                (wt / "greeting.txt").write_text("hello\n")
            if write_handoff:
                (config.handoff_dir / handoff.coder_handoff_name(1)).write_text(
                    "## Status: LGTM\n## Summary\nWrote greeting.txt; tests pass.\n"
                )
            return TurnResult(
                exit_code=0 if coder_ok else 1,
                succeeded=coder_ok,
                session_id=session_id,
                result_text="",
                cost_usd=0.01,
                raw={"is_error": not coder_ok},
                stderr="",
            )
        # reviewer turn
        text = review_retry if resume else review_first
        if text is not None:
            review_path.write_text(text)
        return TurnResult(
            exit_code=0,
            succeeded=True,
            session_id=session_id,
            result_text="",
            cost_usd=0.02,
            raw={"is_error": False},
            stderr="",
        )

    monkeypatch.setattr(containers, "start_container", fake_start)
    monkeypatch.setattr(
        containers, "stop_container", lambda name: state["stopped"].append(name)
    )
    monkeypatch.setattr(develop_mod, "run_turn", fake_run_turn)
    return state


def _commit_count_since_base(result) -> int:
    out = subprocess.run(
        ["git", "rev-list", "--count", f"{result.base_sha}..HEAD"],
        cwd=result.worktree,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return int(out or 0)


def test_success_with_lgtm_review(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig
) -> None:
    state = _install_fakes(monkeypatch, config, review_first=_LGTM)
    result = develop_mod.develop(config)

    assert result.status == "succeeded"
    assert len(result.commits) == 1
    assert result.review is not None
    assert result.review.status == "LGTM"
    assert result.review.passed is True
    assert result.review.max_severity is None
    # both containers (coder + reviewer) were torn down
    assert any("-coder" in n for n in state["stopped"])
    assert any("-review-" in n for n in state["stopped"])
    # worktree clean; committed file present
    assert (
        subprocess.run(
            ["git", "show", "HEAD:greeting.txt"],
            cwd=result.worktree,
            capture_output=True,
            text=True,
        ).stdout
        == "hello\n"
    )


def test_findings_major_blocks(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig
) -> None:
    _install_fakes(monkeypatch, config, review_first=_FINDINGS_MAJOR)
    result = develop_mod.develop(config)
    assert result.status == "succeeded"  # T2 verdict is informational, not gating
    assert result.review is not None
    assert result.review.status == "FINDINGS"
    assert result.review.max_severity == "major"
    assert result.review.passed is False  # major >= block_threshold (major)
    assert result.review.findings_count == 1


def test_findings_below_threshold_passes(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig
) -> None:
    _install_fakes(monkeypatch, config, review_first=_FINDINGS_MINOR)
    result = develop_mod.develop(config)
    assert result.review is not None
    assert result.review.status == "FINDINGS"
    assert result.review.max_severity == "minor"
    assert result.review.passed is True  # minor < major threshold


def test_malformed_review_is_reprompted_and_recovers(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig
) -> None:
    _install_fakes(monkeypatch, config, review_first=_GARBAGE, review_retry=_LGTM)
    result = develop_mod.develop(config)
    assert result.review is not None
    assert result.review.status == "LGTM"  # the re-prompt fixed it


def test_review_invalid_when_never_well_formed(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig
) -> None:
    _install_fakes(monkeypatch, config, review_first=_GARBAGE, review_retry=_GARBAGE)
    result = develop_mod.develop(config)
    assert result.review is not None
    assert result.review.status == "invalid"
    assert result.review.passed is False


def test_no_review_when_coder_fails(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig
) -> None:
    _install_fakes(monkeypatch, config, coder_ok=False)
    result = develop_mod.develop(config)
    assert result.status == "failed"
    assert result.review is None
    assert result.commits == []
    assert _commit_count_since_base(result) == 0


def test_no_review_when_coder_makes_no_commit(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig
) -> None:
    _install_fakes(monkeypatch, config, write_source=False)
    result = develop_mod.develop(config)
    assert result.status == "failed"
    assert result.review is None
    assert _commit_count_since_base(result) == 0


def test_develop_rejects_unsupported_coder(tmp_git_repo: Path, tmp_path: Path) -> None:
    cfg = DevelopConfig(
        repo=tmp_git_repo,
        description="x",
        work_dir=tmp_path / "work",
        coder="codex",
        claude_config_dir=tmp_path / "fake-claude",
    )
    with pytest.raises(ValueError, match="unsupported tool"):
        develop_mod.develop(cfg)
