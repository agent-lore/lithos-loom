"""Tests for PR delivery + the Copilot review round (T9).

Pure builders are tested directly; ``deliver()`` is exercised with every
gh/git wrapper and the container/turn machinery monkeypatched — no network,
no Docker.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from lithos_loom.plugins.story_develop import containers, pr_delivery
from lithos_loom.plugins.story_develop import develop as develop_mod
from lithos_loom.plugins.story_develop.config import DevelopConfig
from lithos_loom.plugins.story_develop.develop import DevelopResult, ReviewOutcome
from lithos_loom.plugins.story_develop.pr_delivery import (
    AUTOMATED_MARKER,
    CopilotComment,
    build_pr_body,
    closes_line,
    comments_to_handoff_text,
    deliver,
    parse_issue_ref,
    pr_number_from_url,
    reply_body,
)
from lithos_loom.plugins.story_develop.turns import TurnResult

# --- pure builders --------------------------------------------------------------


def test_parse_issue_ref() -> None:
    assert parse_issue_ref("https://github.com/o/r/issues/42") == ("o/r", 42)
    assert parse_issue_ref("https://github.com/o/r/issues/42/") == ("o/r", 42)
    assert parse_issue_ref("https://github.com/o/r/pull/42") is None
    assert parse_issue_ref("not a url") is None


def test_closes_line_same_and_cross_repo() -> None:
    assert closes_line("https://github.com/o/r/issues/7", "o/r") == "Closes #7"
    assert closes_line("https://github.com/O/R/issues/7", "o/r") == "Closes #7"
    assert (
        closes_line("https://github.com/other/repo/issues/7", "o/r")
        == "Closes other/repo#7"
    )
    assert closes_line(None, "o/r") == ""
    assert closes_line("garbage", "o/r") == ""


def test_build_pr_body_contents() -> None:
    body = build_pr_body(
        description="Add a flag\n\nDetails.",
        acceptance_criteria="1. works",
        reviews_summary="[cq]=LGTM",
        rounds=2,
        gate_verdict="GREEN",
        cost_usd=1.234,
        task_id="task-9",
        issue_closes="Closes #7",
    )
    assert "Closes #7" in body
    assert "## Acceptance criteria" in body and "1. works" in body
    assert "[cq]=LGTM" in body and "rounds: 2" in body
    assert "test gate: GREEN" in body
    assert "$1.23" in body
    assert "Lithos task: `task-9`" in body
    assert "squash-merge" in body


def test_build_pr_body_minimal() -> None:
    body = build_pr_body(
        description="x",
        acceptance_criteria=None,
        reviews_summary="[cq]=LGTM",
        rounds=1,
        gate_verdict=None,
        cost_usd=0.5,
        task_id=None,
        issue_closes="",
    )
    assert "Acceptance criteria" not in body
    assert "Closes" not in body
    assert "Lithos task" not in body


def test_comments_become_parseable_findings() -> None:
    from lithos_loom.plugins.story_develop.handoff import parse_review_handoff

    comments = [
        CopilotComment(comment_id=1, path="a.py", line=3, body="Multi\nline\ncomment"),
        CopilotComment(comment_id=2, path="b.py", line=None, body="No line"),
    ]
    parsed = parse_review_handoff(comments_to_handoff_text(comments))
    assert parsed.status == "FINDINGS"
    assert len(parsed.findings) == 2
    assert parsed.findings[0].finding_id == ""  # ledger assigns
    assert parsed.findings[0].severity == "minor"
    assert parsed.findings[0].files == ["a.py:3"]
    assert "Multi line comment" in parsed.findings[0].rationale
    assert parsed.findings[1].files == ["b.py"]


def test_reply_body_variants() -> None:
    fixed = reply_body(fixed=True, sha="abcdef12345", coder_response="tightened it")
    assert fixed.startswith("Fixed in abcdef1234 — tightened it")
    assert AUTOMATED_MARKER in fixed
    disputed = reply_body(fixed=False, sha=None, coder_response="intentional")
    assert disputed.startswith("Not changed — intentional")
    nodetail = reply_body(fixed=True, sha=None, coder_response="")
    assert "Addressed — (no further detail given)" in nodetail
    held = reply_body(
        fixed=False, sha=None, coder_response="adds a guard", held_back_verdict="RED"
    )
    assert "NOT pushed" in held and "RED" in held and "adds a guard" in held
    assert AUTOMATED_MARKER in held


def test_pr_number_from_url() -> None:
    assert pr_number_from_url("https://github.com/o/r/pull/82") == 82
    with pytest.raises(RuntimeError):
        pr_number_from_url("https://github.com/o/r")


# --- deliver() orchestration ------------------------------------------------------


@pytest.fixture
def config(tmp_git_repo: Path, tmp_path: Path) -> DevelopConfig:
    cfg_dir = tmp_path / "fake-claude"
    cfg_dir.mkdir()
    return DevelopConfig(
        repo=tmp_git_repo,
        description="Add a flag",
        work_dir=tmp_path / "work",
        claude_config_dir=cfg_dir,
        test_gate=False,  # gate scenarios toggle this on explicitly
    )


def _result(config: DevelopConfig, wt: Path) -> DevelopResult:
    return DevelopResult(
        status="approved",
        run_id=config.run_id,
        worktree=wt,
        branch="my-branch",
        base_sha="0" * 40,
        commits=["c1"],
        rounds=2,
        handoff_present=True,
        coder_cost_usd=0.5,
        review_cost_usd=0.5,
        message="approved",
        reviews=(
            ReviewOutcome(
                reviewer="cq",
                status="LGTM",
                passed=True,
                max_severity=None,
            ),
        ),
        coder_session="sess-1",
    )


def _install(
    monkeypatch: pytest.MonkeyPatch,
    config: DevelopConfig,
    *,
    comments: list[CopilotComment] | None = None,
    copilot_arrives: bool = True,
    request_ok: bool = True,
    coder_ok: bool = True,
    coder_handoff: str | None = None,
    coder_writes_source: bool = True,
) -> dict:
    """Fake every side-effecting seam deliver() touches."""
    state: dict[str, Any] = {
        "pushes": 0,
        "replies": [],
        "pr_comments": [],
        "turn_prompts": [],
        "containers": [],
    }
    config.handoff_dir.mkdir(parents=True, exist_ok=True)
    config.coder_config_dir.mkdir(parents=True, exist_ok=True)

    def fake_push(wt, b):
        state["pushes"] += 1

    monkeypatch.setattr(pr_delivery, "push_branch", fake_push)
    monkeypatch.setattr(pr_delivery, "repo_name_with_owner", lambda wt: "o/r")
    monkeypatch.setattr(
        pr_delivery,
        "create_pr",
        lambda wt, **kw: state.update(pr_kwargs=kw) or "https://github.com/o/r/pull/82",
    )
    monkeypatch.setattr(pr_delivery, "request_copilot", lambda *a: request_ok)
    monkeypatch.setattr(
        pr_delivery, "wait_for_copilot", lambda *a, **kw: copilot_arrives
    )
    monkeypatch.setattr(
        pr_delivery, "fetch_copilot_comments", lambda *a: list(comments or [])
    )
    monkeypatch.setattr(
        pr_delivery,
        "post_thread_reply",
        lambda wt, repo, pr, cid, body: state["replies"].append((cid, body)) or True,
    )
    monkeypatch.setattr(
        pr_delivery,
        "post_pr_comment",
        lambda wt, pr, body: state["pr_comments"].append(body) or True,
    )

    def fake_start(run_cmd):
        state["containers"].append("start")
        return "cid"

    def fake_run_turn(
        *, container, prompt, session_id, resume=False, timeout, tool="claude"
    ):
        state["turn_prompts"].append(prompt)
        state["resume"] = resume
        state["session"] = session_id
        wt = state["wt"]
        if coder_writes_source:
            (wt / "copilot_fix.txt").write_text("fixed\n")
        text = coder_handoff
        if text is None:
            text = (
                "## Status: LGTM\n## Summary\nDone.\n## Findings\n"
                "- finding_id: f-001\n  severity: minor\n  status: fixed\n"
                "  coder_response: tightened the annotation\n"
            )
        import re as _re

        m = _re.search(r"round_(\d+)_coder_done\.md", prompt)
        assert m is not None
        (config.handoff_dir / f"round_{int(m.group(1)):02d}_coder_done.md").write_text(
            text
        )
        return TurnResult(
            exit_code=0 if coder_ok else 1,
            succeeded=coder_ok,
            session_id=session_id,
            result_text="",
            cost_usd=0.1,
            raw={},
            stderr="",
        )

    monkeypatch.setattr(containers, "start_container", fake_start)
    monkeypatch.setattr(containers, "stop_container", lambda n: None)
    import lithos_loom.plugins.story_develop.turns as turns_mod

    monkeypatch.setattr(
        develop_mod, "run_turn", fake_run_turn
    )  # not used by deliver, but harmless
    monkeypatch.setattr(turns_mod, "run_turn", fake_run_turn)
    # deliver imports run_turn from .turns inside the function body
    return state


def _make_wt(config: DevelopConfig) -> Path:
    from lithos_loom.runner import worktree

    config.worktree_parent.mkdir(parents=True, exist_ok=True)
    return worktree.create(
        config.repo, "main", "delivery test", parent=config.worktree_parent
    )


def test_deliver_no_copilot(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig
) -> None:
    state = _install(monkeypatch, config)
    wt = _make_wt(config)
    state["wt"] = wt
    out = deliver(config, _result(config, wt), no_copilot=True)
    assert out.pr_url.endswith("/pull/82") and out.pr_number == 82
    assert state["pushes"] == 1
    assert out.copilot_requested is False
    assert state["pr_kwargs"]["base"] == "main"


def test_deliver_copilot_timeout_degrades(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig
) -> None:
    state = _install(monkeypatch, config, copilot_arrives=False)
    wt = _make_wt(config)
    state["wt"] = wt
    out = deliver(config, _result(config, wt), copilot_timeout=1)
    assert out.copilot_requested is True and out.copilot_reviewed is False
    assert any("not received" in n for n in out.notes)
    assert state["replies"] == []


def test_deliver_copilot_clean_review_no_fix_round(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig
) -> None:
    state = _install(monkeypatch, config, comments=[])
    wt = _make_wt(config)
    state["wt"] = wt
    out = deliver(config, _result(config, wt))
    assert out.copilot_reviewed is True and out.comments_count == 0
    assert state["turn_prompts"] == []  # no coder round for a clean review


def test_deliver_full_copilot_round(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig
) -> None:
    comments = [
        CopilotComment(comment_id=11, path="a.py", line=5, body="tighten this"),
    ]
    state = _install(monkeypatch, config, comments=comments)
    wt = _make_wt(config)
    state["wt"] = wt
    result = _result(config, wt)
    out = deliver(config, result)

    assert out.comments_count == 1
    assert out.fix_committed and out.fix_pushed
    assert state["pushes"] == 2  # initial + fix
    # the coder round resumed the ORIGINAL session
    assert state["resume"] is True and state["session"] == "sess-1"
    # prompt carried the ledger-assigned finding + the PR url
    assert "f-001" in state["turn_prompts"][0]
    assert "pull/82" in state["turn_prompts"][0]
    # synthetic copilot review handoff persisted (round = rounds+1)
    assert (config.handoff_dir / "round_03_review_copilot.md").is_file()
    # one reply, on the right thread, with the coder's public one-liner
    ((cid, body),) = state["replies"]
    assert cid == 11
    assert body.startswith("Fixed in ")
    assert "tightened the annotation" in body
    assert AUTOMATED_MARKER in body


def test_deliver_dispute_reply(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig
) -> None:
    comments = [CopilotComment(comment_id=7, path="a.py", line=1, body="wrong idea")]
    dispute_handoff = (
        "## Status: LGTM\n## Summary\nDisputed.\n## Findings\n"
        "- finding_id: f-001\n  severity: minor\n  status: disputed\n"
        "  coder_response: the behaviour is intentional and documented\n"
    )
    state = _install(
        monkeypatch,
        config,
        comments=comments,
        coder_handoff=dispute_handoff,
        coder_writes_source=False,
    )
    wt = _make_wt(config)
    state["wt"] = wt
    out = deliver(config, _result(config, wt))
    assert out.fix_committed is False  # nothing changed
    ((_, body),) = state["replies"]
    assert body.startswith("Not changed — the behaviour is intentional")


def test_deliver_red_gate_holds_fix_back(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig
) -> None:
    from lithos_loom.plugins.story_develop.test_gate import GateResult

    comments = [CopilotComment(comment_id=3, path="a.py", line=2, body="bug here")]
    cfg = replace(config, test_gate=True, test_command="fake-tests")
    state = _install(monkeypatch, cfg, comments=comments)
    import lithos_loom.plugins.story_develop.test_gate as tg

    monkeypatch.setattr(
        tg,
        "run_gate_container",
        lambda cmd, *, name, command, timeout: GateResult(
            command=command, exit_code=1, passed=False, output_tail="boom"
        ),
    )
    wt = _make_wt(cfg)
    state["wt"] = wt
    out = deliver(cfg, _result(cfg, wt))

    assert out.fix_committed is True
    assert out.fix_pushed is False  # RED gate held it back
    assert out.fix_gate_verdict == "RED"
    assert state["pushes"] == 1  # only the initial push
    assert any("NOT pushed" in c for c in state["pr_comments"])
    # replies say what actually happened: a fix exists but was held back —
    # neither "Fixed in <sha>" (not on the PR) nor "Not changed" (it WAS)
    ((_, body),) = state["replies"]
    assert not body.startswith("Fixed in ")
    assert not body.startswith("Not changed")
    assert "NOT pushed" in body and "RED" in body
    assert "tightened the annotation" in body  # intended change still shown


def test_deliver_coder_failure_comments_and_degrades(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig
) -> None:
    comments = [CopilotComment(comment_id=5, path="a.py", line=1, body="x")]
    state = _install(monkeypatch, config, comments=comments, coder_ok=False)
    wt = _make_wt(config)
    state["wt"] = wt
    out = deliver(config, _result(config, wt))
    assert out.fix_committed is False and out.replies_posted == 0
    assert any("coder turn failed" in c for c in state["pr_comments"])
    assert any("respond manually" in n for n in out.notes)
