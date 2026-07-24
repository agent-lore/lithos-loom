"""Tests for PR delivery + the Copilot review round (T9).

Pure builders are tested directly; ``deliver()`` is exercised with every
gh/git wrapper and the container/turn machinery monkeypatched — no network,
no Docker.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from lithos_loom.github_client import GitHubError, GitHubTransportError
from lithos_loom.plugins.story_develop import containers, pr_delivery
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


# --- request_operator_review (#113) --------------------------------------------


class _RecordingGitHubClient:
    """A recording GitHubClient double for the request_operator_review branch
    tests: records request_reviewers / add_assignees calls and raises a preset
    typed error to drive the self-author-422 fallback logic. ``github_call(op)``
    runs ``op`` against it (see :func:`_patch_github_call`)."""

    def __init__(
        self,
        *,
        request_error: GitHubError | None = None,
        assign_error: GitHubError | None = None,
    ) -> None:
        self.calls: list[tuple[str, str, int, tuple[str, ...]]] = []
        self._request_error = request_error
        self._assign_error = assign_error

    async def request_reviewers(
        self, repo: str, number: int, reviewers: list[str]
    ) -> None:
        self.calls.append(("request_reviewers", repo, number, tuple(reviewers)))
        if self._request_error is not None:
            raise self._request_error

    async def add_assignees(self, repo: str, number: int, assignees: list[str]) -> None:
        self.calls.append(("add_assignees", repo, number, tuple(assignees)))
        if self._assign_error is not None:
            raise self._assign_error


def _patch_github_call(
    monkeypatch: pytest.MonkeyPatch, fake: _RecordingGitHubClient
) -> None:
    """Route ``pr_delivery.github_call(op)`` through the recording fake client."""
    monkeypatch.setattr(pr_delivery, "github_call", lambda op: asyncio.run(op(fake)))


def test_request_operator_review_requests_reviewer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _RecordingGitHubClient()
    _patch_github_call(monkeypatch, fake)
    assert pr_delivery.request_operator_review("o/r", 7, "dave") == "review_requested"
    assert fake.calls == [("request_reviewers", "o/r", 7, ("dave",))]


def test_request_operator_review_assigns_when_author_422(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _RecordingGitHubClient(
        request_error=GitHubError(
            "GitHub 422 for o/r: Review cannot be requested from pull request author."
        )
    )
    _patch_github_call(monkeypatch, fake)
    assert pr_delivery.request_operator_review("o/r", 7, "dave") == "assigned"
    assert ("add_assignees", "o/r", 7, ("dave",)) in fake.calls


def test_request_operator_review_non_author_failure_does_not_assign(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _RecordingGitHubClient(
        request_error=GitHubError("GitHub 404 for o/r: Not Found")
    )
    _patch_github_call(monkeypatch, fake)
    assert pr_delivery.request_operator_review("o/r", 7, "dave") == "failed"
    assert not any(c[0] == "add_assignees" for c in fake.calls)


def test_request_operator_review_non_author_422_does_not_assign(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A 422 that is NOT the self-author case (e.g. a non-collaborator / bad
    # login) must surface as a real failure, not a silent assignee downgrade.
    fake = _RecordingGitHubClient(
        request_error=GitHubError(
            "GitHub 422 for o/r: Reviews may only be requested from collaborators."
        )
    )
    _patch_github_call(monkeypatch, fake)
    assert pr_delivery.request_operator_review("o/r", 7, "ghost") == "failed"
    assert not any(c[0] == "add_assignees" for c in fake.calls)


def test_request_operator_review_failed_assign_returns_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _RecordingGitHubClient(
        request_error=GitHubError(
            "GitHub 422 for o/r: cannot be requested from pull request author"
        ),
        assign_error=GitHubError("GitHub 500 for o/r: assign exploded"),
    )
    _patch_github_call(monkeypatch, fake)
    assert pr_delivery.request_operator_review("o/r", 7, "dave") == "failed"


# --- best-effort wrappers stay best-effort on a TRANSPORT failure --------------
# GitHubTransportError is a GitHubError, so a connect/read/reset error at the
# HTTP layer degrades to the same fallback the old `gh api` non-zero exit did —
# it must NOT escape and abort the post-PR delivery flow.


_TRANSPORT_ERR = GitHubTransportError(
    "https://api.github.com/repos/o/r/pulls/7", OSError("connection reset")
)


def _github_call_raises(exc: Exception) -> object:
    def _raise(op: object) -> object:
        raise exc

    return _raise


def test_request_copilot_returns_false_on_transport_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pr_delivery, "github_call", _github_call_raises(_TRANSPORT_ERR))
    assert pr_delivery.request_copilot("o/r", 7) is False


def test_request_operator_review_returns_failed_on_transport_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A transport error is not the self-author 422 → "failed", never aborts.
    monkeypatch.setattr(pr_delivery, "github_call", _github_call_raises(_TRANSPORT_ERR))
    assert pr_delivery.request_operator_review("o/r", 7, "dave") == "failed"


def test_copilot_expected_comments_returns_none_on_transport_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pr_delivery, "github_call", _github_call_raises(_TRANSPORT_ERR))
    assert pr_delivery.copilot_expected_comments("o/r", 7) is None


def test_fetch_copilot_comments_returns_empty_on_transport_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pr_delivery, "github_call", _github_call_raises(_TRANSPORT_ERR))
    assert pr_delivery.fetch_copilot_comments("o/r", 7) == []


def test_post_pr_comment_returns_false_on_transport_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pr_delivery, "github_call", _github_call_raises(_TRANSPORT_ERR))
    assert pr_delivery.post_pr_comment("o/r", 7, "body") is False


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
    expected: int | None = None,
    settled: bool | None = None,
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
        "start_cmds": [],
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
    expected_count = expected if expected is not None else len(comments or [])
    monkeypatch.setattr(
        pr_delivery,
        "wait_for_copilot",
        lambda *a, **kw: expected_count if copilot_arrives else None,
    )
    settled_flag = (
        settled
        if settled is not None
        else (expected_count <= 0 or len(comments or []) >= expected_count)
    )
    monkeypatch.setattr(
        pr_delivery,
        "fetch_copilot_comments_settled",
        lambda *a, **kw: (list(comments or []), settled_flag),
    )
    monkeypatch.setattr(
        pr_delivery,
        "post_thread_reply",
        lambda repo, pr, cid, body: state["replies"].append((cid, body)) or True,
    )
    monkeypatch.setattr(
        pr_delivery,
        "post_pr_comment",
        lambda repo, pr, body: state["pr_comments"].append(body) or True,
    )

    def fake_start(run_cmd):
        state["containers"].append("start")
        state["start_cmds"].append(list(run_cmd))
        return "cid"

    def fake_run_turn(
        *,
        container,
        prompt,
        engine,
        session_id,
        resume=False,
        timeout,
        model=None,
        effort=None,
    ):
        state["turn_prompts"].append(prompt)
        state["turn_engine"] = engine.name
        state["resume"] = resume
        state["session"] = session_id
        state["model"] = model
        state["effort"] = effort
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

    # deliver drives the coder fix turn through turns.run_turn (module attribute).
    monkeypatch.setattr(turns_mod, "run_turn", fake_run_turn)
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


def test_deliver_notifies_operator_when_configured(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig
) -> None:
    cfg = replace(config, notify_github_login="dave")
    _install(monkeypatch, cfg)
    calls: list[tuple[str, int, str]] = []
    monkeypatch.setattr(
        pr_delivery,
        "request_operator_review",
        lambda repo, pr_number, login: (
            calls.append((repo, pr_number, login)) or "review_requested"
        ),
    )
    wt = _make_wt(cfg)
    out = deliver(cfg, _result(cfg, wt), no_copilot=True)
    assert calls == [("o/r", 82, "dave")]
    assert any("requested review from @dave" in n for n in out.notes)


def test_deliver_preserves_pr_url_when_post_open_step_raises(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig
) -> None:
    # #192 review: once create_pr() returns, the PR exists. A later failure (the
    # fix push, a reply, the Copilot wait — here the Copilot request) must NOT
    # lose the url: deliver() degrades to a delivered-with-notes outcome carrying
    # it, so build_result_payload still records pr_url and `attach` can render it
    # instead of stranding the operator with an approved run and no PR.
    state = _install(monkeypatch, config)
    wt = _make_wt(config)
    state["wt"] = wt

    def boom(*a: Any, **kw: Any) -> bool:
        raise RuntimeError("github flaked right after the PR was opened")

    monkeypatch.setattr(pr_delivery, "request_copilot", boom)

    out = deliver(config, _result(config, wt), no_copilot=False)
    assert out.pr_url.endswith("/pull/82") and out.pr_number == 82  # url preserved
    assert any("did not finish after opening the PR" in n for n in out.notes)


def test_deliver_skips_operator_notify_when_unset(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig
) -> None:
    _install(monkeypatch, config)  # config.notify_github_login is None
    called = False

    def boom(*a, **k):
        nonlocal called
        called = True
        return "review_requested"

    monkeypatch.setattr(pr_delivery, "request_operator_review", boom)
    wt = _make_wt(config)
    deliver(config, _result(config, wt), no_copilot=True)
    assert called is False


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
    assert out.copilot_settled is True  # expected 0 → genuinely clean, settled
    assert state["turn_prompts"] == []  # no coder round for a clean review


def test_deliver_copilot_nonsettle_zero_defers(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig
) -> None:
    """Copilot's summary claimed comments but none materialised in the window
    (#91). The round must be flagged INCOMPLETE — copilot_settled=False + a
    'did not settle' note — NOT silently treated as a clean review, and no fix
    round runs against zero comments."""
    state = _install(monkeypatch, config, comments=[], expected=2)
    wt = _make_wt(config)
    state["wt"] = wt
    out = deliver(config, _result(config, wt))
    assert out.copilot_reviewed is True
    assert out.copilot_settled is False
    assert any("did not settle" in n for n in out.notes)
    assert state["turn_prompts"] == []  # nothing materialised → no fix round


def test_deliver_copilot_unknown_count_unsettled_flags_incomplete(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig
) -> None:
    """A Copilot review whose body states no count (expected=-1) and whose
    stream never stabilises must be flagged INCOMPLETE at the deliver() level,
    not reported as a clean/settled review (#96 review finding 2). This is the
    case most prone to silently reintroducing the missed-comments failure."""
    state = _install(monkeypatch, config, comments=[], expected=-1, settled=False)
    wt = _make_wt(config)
    state["wt"] = wt
    out = deliver(config, _result(config, wt))
    assert out.copilot_reviewed is True
    assert out.copilot_settled is False
    assert any("did not stabilise" in n for n in out.notes)
    assert state["turn_prompts"] == []  # nothing materialised → no fix round


def test_deliver_copilot_nonsettle_partial_fixes_and_flags(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig
) -> None:
    """Some-but-not-all comments materialised: address the ones we have, but
    still flag the round INCOMPLETE so the missing comments aren't lost."""
    comments = [CopilotComment(comment_id=11, path="a.py", line=5, body="fix this")]
    state = _install(monkeypatch, config, comments=comments, expected=3)
    wt = _make_wt(config)
    state["wt"] = wt
    out = deliver(config, _result(config, wt))
    assert out.copilot_settled is False
    assert any("did not settle" in n for n in out.notes)
    assert state["turn_prompts"]  # fix round still runs on the comment we have


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
    assert out.copilot_settled is True  # all expected comments materialised
    assert out.fix_committed and out.fix_pushed
    assert out.fix_sha is not None
    assert out.extra_cost_usd == pytest.approx(0.1)  # the fix turn's spend
    assert state["pushes"] == 2  # initial + fix
    # the coder round resumed the ORIGINAL session
    assert state["resume"] is True and state["session"] == "sess-1"
    # prompt carried the ledger-assigned finding + the PR url
    assert "f-001" in state["turn_prompts"][0]
    assert "pull/82" in state["turn_prompts"][0]
    # synthetic copilot review handoff persisted (round = rounds+1)
    assert (config.handoff_dir / "round_03_review_copilot.md").is_file()
    # audit parity: the conversation log now includes the Copilot exchange
    log = (config.run_dir / "conversation.md").read_text()
    assert "## Copilot round" in log
    assert "tighten this" in log  # copilot's comment
    assert "tightened the annotation" in log  # the coder's response
    # one reply, on the right thread, with the coder's public one-liner
    ((cid, body),) = state["replies"]
    assert cid == 11
    assert body.startswith("Fixed in ")
    assert "tightened the annotation" in body
    assert AUTOMATED_MARKER in body


def test_deliver_copilot_fix_turn_builds_codex_container(
    monkeypatch: pytest.MonkeyPatch, tmp_git_repo: Path, tmp_path: Path
) -> None:
    """ARCH-2.E3: a codex-coder run's Copilot fix turn rebuilds its container with
    CODEX provisioning (CODEX_HOME + auth.json), not claude's.

    The fix turn resumes ``result.coder_session`` — the codex thread_id — so the
    container must be the codex tool/env, else the handle would resume in the
    wrong place. Regression guard for the E3 ``engine=engines.get_engine(config.coder)``
    wiring at the delivery call site.
    """
    codex_dir = tmp_path / "fake-codex"
    codex_dir.mkdir()
    (codex_dir / "auth.json").write_text("{}")  # so the codex auth mount appears
    config = DevelopConfig(
        repo=tmp_git_repo,
        description="Add a flag",
        work_dir=tmp_path / "work",
        coder="codex",
        codex_config_dir=codex_dir,
        test_gate=False,
    )
    comments = [CopilotComment(comment_id=11, path="a.py", line=5, body="tighten this")]
    state = _install(monkeypatch, config, comments=comments)
    wt = _make_wt(config)
    state["wt"] = wt
    out = deliver(config, _result(config, wt))

    assert out.fix_pushed  # a coder fix round actually ran
    assert state["turn_engine"] == "codex"  # run_turn received the codex engine
    fix_cmd = state["start_cmds"][-1]  # the fix-turn container's docker-run argv
    assert "CODEX_HOME=/codex_home" in fix_cmd
    assert "CLAUDE_CONFIG_DIR=/claude_config" not in fix_cmd
    # the codex auth file is mounted from the operator's codex config dir
    assert any(a.endswith(":/codex_home/auth.json") for a in fix_cmd)


def test_deliver_copilot_round_uses_configured_model_and_effort(
    monkeypatch: pytest.MonkeyPatch, tmp_git_repo: Path, tmp_path: Path
) -> None:
    """#93: the Copilot fix round inherits the run's coder model + effort."""
    cfg_dir = tmp_path / "fake-claude"
    cfg_dir.mkdir()
    config = DevelopConfig(
        repo=tmp_git_repo,
        description="Add a flag",
        work_dir=tmp_path / "work",
        claude_config_dir=cfg_dir,
        test_gate=False,
        coder_model="opus",
        coder_effort="xhigh",
    )
    comments = [CopilotComment(comment_id=11, path="a.py", line=5, body="tighten this")]
    state = _install(monkeypatch, config, comments=comments)
    wt = _make_wt(config)
    state["wt"] = wt
    out = deliver(config, _result(config, wt))

    assert out.fix_pushed  # a coder fix round actually ran
    assert state["model"] == "opus"  # --model threaded to the fix turn
    assert state["effort"] == "xhigh"  # --effort threaded to the fix turn


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


# --- comment-lag settling (the first-dogfood race + recurrence) -----------------


def test_settled_fetch_waits_for_lagging_comments(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # First poll returns [], the comments materialise on the third — the
    # settle loop must keep going until the EXPECTED count is visible AND
    # the count has been stable for settle_seconds.
    calls = {"n": 0}
    arrived = [CopilotComment(comment_id=1, path="a.py", line=1, body="x")]
    clock = {"t": 0.0}

    def fake_fetch(repo, pr):
        calls["n"] += 1
        return arrived if calls["n"] >= 3 else []

    monkeypatch.setattr(pr_delivery, "fetch_copilot_comments", fake_fetch)
    monkeypatch.setattr(pr_delivery.time, "monotonic", lambda: clock["t"])

    def fake_sleep(s):
        clock["t"] += s

    monkeypatch.setattr(pr_delivery.time, "sleep", fake_sleep)
    out, settled = pr_delivery.fetch_copilot_comments_settled(
        "o/r", 1, expected=1, grace_seconds=120, settle_seconds=15
    )
    assert out == arrived
    assert settled is True  # comments arrived + stabilised before the deadline
    # call 1-2: empty; call 3: arrives (settle clock starts); then more
    # polls until settle_seconds elapses without a count change
    assert calls["n"] >= 3


def test_settled_fetch_zero_expected_returns_immediately(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = {"n": 0}

    def fake_fetch(repo, pr):
        calls["n"] += 1
        return []

    monkeypatch.setattr(pr_delivery, "fetch_copilot_comments", fake_fetch)
    out, settled = pr_delivery.fetch_copilot_comments_settled(
        "o/r", 1, expected=0, grace_seconds=120
    )
    assert out == [] and calls["n"] == 1  # no pointless polling
    assert settled is True  # expected 0 → genuinely nothing to wait for


def test_settled_fetch_grace_bounds_the_wait(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(pr_delivery, "fetch_copilot_comments", lambda *a: [])
    clock = {"t": 0.0}
    monkeypatch.setattr(pr_delivery.time, "monotonic", lambda: clock["t"])

    def fake_sleep(s):
        clock["t"] += s

    monkeypatch.setattr(pr_delivery.time, "sleep", fake_sleep)
    out, settled = pr_delivery.fetch_copilot_comments_settled(
        "o/r", 1, expected=2, grace_seconds=30
    )
    assert out == []  # gave up after the grace window
    assert settled is False  # deadline hit before the comments arrived


def test_settled_fetch_catches_late_arriving_comments(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The settle window re-polls after the threshold is met, catching
    comments that trickle in after the expected count is reached — the
    exact race that the 90 s grace missed."""
    c1 = CopilotComment(comment_id=1, path="a.py", line=1, body="x")
    c2 = CopilotComment(comment_id=2, path="b.py", line=2, body="y")
    clock = {"t": 0.0}

    # c1 arrives at t=5 (poll 2), c2 arrives at t=15 (poll 4)
    def fake_fetch(repo, pr):
        if clock["t"] < 5:
            return []
        if clock["t"] < 15:
            return [c1]
        return [c1, c2]

    monkeypatch.setattr(pr_delivery, "fetch_copilot_comments", fake_fetch)
    monkeypatch.setattr(pr_delivery.time, "monotonic", lambda: clock["t"])

    def fake_sleep(s):
        clock["t"] += s

    monkeypatch.setattr(pr_delivery.time, "sleep", fake_sleep)

    # expected=1 — old code would have returned [c1] immediately at t=5
    out, settled = pr_delivery.fetch_copilot_comments_settled(
        "o/r", 1, expected=1, grace_seconds=120, settle_seconds=15
    )
    assert len(out) == 2  # settle window caught c2
    assert settled is True


def test_settled_fetch_unknown_expected_waits_for_stability(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When expected=-1 (unknown count), the old code returned on the first
    comment; the settle window now waits for the count to stop changing."""
    c1 = CopilotComment(comment_id=1, path="a.py", line=1, body="x")
    c2 = CopilotComment(comment_id=2, path="b.py", line=2, body="y")
    clock = {"t": 0.0}

    def fake_fetch(repo, pr):
        if clock["t"] < 5:
            return []
        if clock["t"] < 10:
            return [c1]
        return [c1, c2]

    monkeypatch.setattr(pr_delivery, "fetch_copilot_comments", fake_fetch)
    monkeypatch.setattr(pr_delivery.time, "monotonic", lambda: clock["t"])

    def fake_sleep(s):
        clock["t"] += s

    monkeypatch.setattr(pr_delivery.time, "sleep", fake_sleep)

    out, settled = pr_delivery.fetch_copilot_comments_settled(
        "o/r", 1, expected=-1, grace_seconds=120, settle_seconds=15
    )
    # c2 arrived at t=10, settle window of 15 s starts THEN, so returns at ~t=25
    assert len(out) == 2
    assert settled is True  # unknown count stabilised before the deadline


def test_settled_fetch_unknown_expected_unstable_at_deadline_not_settled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """expected=-1 but the stream is still changing at the deadline → the fetch
    must report settled=False, so deliver() flags the round incomplete rather
    than reporting an unknown-count review as settled (#96 review finding 2)."""
    c1 = CopilotComment(comment_id=1, path="a.py", line=1, body="x")
    c2 = CopilotComment(comment_id=2, path="b.py", line=2, body="y")
    clock = {"t": 0.0}

    def fake_fetch(repo, pr):
        return [c1] if clock["t"] < 10 else [c1, c2]  # changes at t=10

    monkeypatch.setattr(pr_delivery, "fetch_copilot_comments", fake_fetch)
    monkeypatch.setattr(pr_delivery.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(
        pr_delivery.time, "sleep", lambda s: clock.update(t=clock["t"] + s)
    )
    # deadline t=20; count last changed at t=10, so only 10 s stable (< 15)
    out, settled = pr_delivery.fetch_copilot_comments_settled(
        "o/r", 1, expected=-1, grace_seconds=20, settle_seconds=15
    )
    assert len(out) == 2
    assert settled is False  # never stabilised within the window


def test_generated_count_parsing() -> None:
    def token(text: str) -> str:
        m = pr_delivery._GENERATED_RE.search(text)
        assert m is not None
        return m.group(1)

    assert token("generated 3 comments") == "3"
    assert token("generated 1 comment") == "1"
    assert token("generated no comments") == "no"
    assert pr_delivery._GENERATED_RE.search("reviewed all files") is None


def test_delivery_budget_covers_copilot_coder_and_gate() -> None:
    # #189: the recorded delivery budget must cover EVERY bounded phase deliver()
    # can run — the Copilot round, the fix coder turn, AND the regression gate on
    # the fix commit — so `develop attach` never times out a healthy slow delivery.
    from types import SimpleNamespace

    cfg = SimpleNamespace(test_timeout=900)
    budget = pr_delivery.delivery_budget_seconds(
        cfg, copilot_timeout=600, coder_timeout=3600
    )
    assert budget >= 600 + 3600 + 900  # all three phase timeouts are summed in
    # the gate timeout is summed, not ignored: a wider gate widens the budget.
    wider = pr_delivery.delivery_budget_seconds(
        SimpleNamespace(test_timeout=1800), copilot_timeout=600, coder_timeout=3600
    )
    assert wider == budget + 900


def test_delivery_fallback_exceeds_the_full_default_delivery_budget() -> None:
    # #189 cross-module invariant: run_outcome's flat fallback — used by `develop
    # attach` when a run recorded no delivery deadline (predates the marker, or its
    # write failed) — must comfortably exceed the LARGEST default delivery budget,
    # or it could false-fire on a healthy default-config run. This executes the
    # derivation that was previously only prose in cli/develop.py's
    # DELIVERY_FALLBACK_SECONDS comment (9000 > 6900): if a future phase widens the
    # budget past the fallback, this fails instead of silently under-bounding attach.
    from types import SimpleNamespace

    from lithos_loom.plugins.story_develop import run_outcome
    from lithos_loom.plugins.story_develop.config import (
        DEFAULT_CODER_TIMEOUT,
        DEFAULT_TEST_TIMEOUT,
    )

    # Every input is the daemon's real default, single-sourced from the same
    # constants the parser uses — so the invariant tracks a default that changes,
    # rather than a hard-coded copy that could silently pass against a stale value.
    default_budget = pr_delivery.delivery_budget_seconds(
        SimpleNamespace(test_timeout=DEFAULT_TEST_TIMEOUT),
        copilot_timeout=pr_delivery.DEFAULT_COPILOT_TIMEOUT,
        coder_timeout=DEFAULT_CODER_TIMEOUT,
    )
    assert default_budget < run_outcome.DELIVERY_FALLBACK_SECONDS


# --- deliver_guarded (ARCH-1.S3): the shared develop→deliver seam -----------------


def test_deliver_guarded_skips_when_nothing_to_deliver(
    config: DevelopConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # (None, None) — and deliver() is never called — when open_pr is off OR the
    # run wasn't approved. No deadline marker is recorded in the skip case.
    approved = _result(config, tmp_path)
    not_approved = replace(approved, status="max_rounds")
    called = {"n": 0}
    monkeypatch.setattr(
        pr_delivery,
        "deliver",
        lambda *a, **k: called.__setitem__("n", called["n"] + 1),
    )
    assert pr_delivery.deliver_guarded(
        config,
        not_approved,
        open_pr=True,  # approved gate fails → skip
        no_copilot=True,
        copilot_timeout=1,
        coder_timeout=1,
        github_issue_url=None,
        task_id=None,
    ) == (None, None)
    assert pr_delivery.deliver_guarded(
        config,
        approved,
        open_pr=False,  # open_pr off → skip
        no_copilot=True,
        copilot_timeout=1,
        coder_timeout=1,
        github_issue_url=None,
        task_id=None,
    ) == (None, None)
    assert called["n"] == 0  # deliver() untouched in either skip case
    assert not (config.run_dir / "delivery.json").exists()  # no deadline recorded


def test_deliver_guarded_returns_outcome_and_records_deadline_on_success(
    config: DevelopConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lithos_loom.plugins.story_develop.pr_delivery import DeliveryOutcome

    config.run_dir.mkdir(parents=True, exist_ok=True)  # develop() creates this first
    approved = _result(config, tmp_path)
    outcome = DeliveryOutcome(pr_url="https://github.com/o/r/pull/1", pr_number=1)
    seen: dict[str, bool] = {}

    def fake_deliver(cfg, result, **kw):
        # the #189 deadline must already be on disk when delivery starts (ordering)
        seen["marker_at_delivery"] = (cfg.run_dir / "delivery.json").is_file()
        return outcome

    monkeypatch.setattr(pr_delivery, "deliver", fake_deliver)
    delivery, error = pr_delivery.deliver_guarded(
        config,
        approved,
        open_pr=True,
        no_copilot=True,
        copilot_timeout=600,
        coder_timeout=3600,
        github_issue_url=None,
        task_id=None,
    )
    assert delivery is outcome and error is None
    assert seen["marker_at_delivery"] is True  # deadline recorded BEFORE delivery ran


def test_deliver_guarded_records_failure_and_returns_reason(
    config: DevelopConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # #194: deliver() raising before a PR opens → (None, reason), and the private
    # delivery.json failure marker is written so attach can report it terminally.
    config.run_dir.mkdir(parents=True, exist_ok=True)
    approved = _result(config, tmp_path)

    def boom(*a, **k):
        raise RuntimeError("gh pr create failed: HTTP 422")

    monkeypatch.setattr(pr_delivery, "deliver", boom)
    delivery, error = pr_delivery.deliver_guarded(
        config,
        approved,
        open_pr=True,
        no_copilot=True,
        copilot_timeout=600,
        coder_timeout=3600,
        github_issue_url=None,
        task_id=None,
    )
    assert delivery is None
    assert error is not None and "gh pr create failed" in error
    marker = json.loads((config.run_dir / "delivery.json").read_text(encoding="utf-8"))
    assert marker["failed"] is True


# --- push_to_pr_ref: guarded fast-forward push to a PR head ref (converge) -----


def _fake_run(
    ls_stdout: str,
    *,
    push_rc: int = 0,
    head_sha: str = "l" * 40,
    branch_sha: str | None = None,
    push_stderr: str = "",
    ancestor_rc: int = 0,
) -> Any:
    """A fake ``pr_delivery._run`` dispatching by git subcommand + a call log.

    ``rev-parse HEAD`` resolves to *head_sha*; ``rev-parse --verify <branch>``
    resolves to *branch_sha* (default == *head_sha*, i.e. the caller's branch IS
    the reviewed HEAD). ``ancestor_rc`` is the ``merge-base --is-ancestor`` exit
    code (0 = HEAD descends from the expected head, the normal case).
    """
    branch_sha = head_sha if branch_sha is None else branch_sha
    calls: list[list[str]] = []

    def run(args: list[str], *, cwd: Path, timeout: int = 120) -> Any:
        calls.append(args)
        if args[:2] == ["git", "ls-remote"]:
            return subprocess.CompletedProcess(args, 0, stdout=ls_stdout, stderr="")
        if args[:2] == ["git", "rev-parse"]:
            sha = head_sha if args[-1] == "HEAD" else branch_sha
            return subprocess.CompletedProcess(args, 0, stdout=sha + "\n", stderr="")
        if args[:2] == ["git", "merge-base"]:
            return subprocess.CompletedProcess(args, ancestor_rc, stdout="", stderr="")
        if args[:2] == ["git", "push"]:
            return subprocess.CompletedProcess(
                args, push_rc, stdout="", stderr=push_stderr
            )
        raise AssertionError(f"unexpected git call: {args}")

    run.calls = calls  # type: ignore[attr-defined]
    return run


def test_push_to_pr_ref_fast_forwards_when_remote_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _fake_run("e" * 40 + "\trefs/heads/feature\n")
    monkeypatch.setattr(pr_delivery, "_run", run)
    pushed = pr_delivery.push_to_pr_ref(
        Path("/wt"), "converge-abc", "feature", expected_remote_sha="e" * 40
    )
    assert pushed == "l" * 40  # the exact reviewed HEAD sha
    push = next(c for c in run.calls if c[:2] == ["git", "push"])
    # push the exact HEAD sha to the fully-qualified ref, leased to the expected
    # head — a guarded fast-forward, NOT a blind --force / -f.
    assert push == [
        "git",
        "push",
        "--force-with-lease=refs/heads/feature:" + "e" * 40,
        "origin",
        "l" * 40 + ":refs/heads/feature",
    ]
    assert "--force" not in push and "-f" not in push


def test_push_to_pr_ref_refuses_when_local_branch_is_not_head(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # the source-ref safety boundary: local_branch must BE the reviewed HEAD.
    # A divergent branch (points elsewhere) is refused BEFORE pushing, so a
    # non-descendant local_branch can never force the PR branch backward even
    # though HEAD descends from expected and the lease matches (finding: checked
    # vs pushed object mismatch). Not a merge race — a caller/contract error.
    run = _fake_run(
        "e" * 40 + "\trefs/heads/feature\n",
        head_sha="l" * 40,
        branch_sha="d" * 40,  # local_branch resolves to a DIFFERENT commit
    )
    monkeypatch.setattr(pr_delivery, "_run", run)
    with pytest.raises(RuntimeError) as excinfo:
        pr_delivery.push_to_pr_ref(
            Path("/wt"), "converge-abc", "feature", expected_remote_sha="e" * 40
        )
    assert not isinstance(excinfo.value, pr_delivery.MergeRaceDetected)
    assert not any(c[:2] == ["git", "push"] for c in run.calls)  # never pushed


def test_push_to_pr_ref_raises_fork_when_ref_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _fake_run("")  # ls-remote finds nothing → head lives on a fork
    monkeypatch.setattr(pr_delivery, "_run", run)
    with pytest.raises(pr_delivery.ForkPushUnsupported):
        pr_delivery.push_to_pr_ref(
            Path("/wt"), "converge-abc", "feature", expected_remote_sha="e" * 40
        )
    assert not any(c[:2] == ["git", "push"] for c in run.calls)  # never pushed


def test_push_to_pr_ref_reads_exact_ref_among_suffix_collisions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # git ls-remote treats a bare name as a tail pattern: `feature` also matches
    # refs/heads/a/feature, and output is ref-name-sorted so the collision comes
    # FIRST. The pre-fix code took the first line's sha → a permanent false
    # merge_race on a valid PR. The lookup must key on the exact fully-qualified
    # ref name, not line order.
    run = _fake_run(
        "z" * 40 + "\trefs/heads/a/feature\n" + "e" * 40 + "\trefs/heads/feature\n"
    )
    monkeypatch.setattr(pr_delivery, "_run", run)
    pushed = pr_delivery.push_to_pr_ref(
        Path("/wt"), "converge-abc", "feature", expected_remote_sha="e" * 40
    )
    assert pushed == "l" * 40
    # and the preflight queries the fully-qualified ref, same as the lease/push
    ls = next(c for c in run.calls if c[:2] == ["git", "ls-remote"])
    assert ls[-1] == "refs/heads/feature"


def test_push_to_pr_ref_fork_when_only_a_suffix_collision_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # the PR ref itself is absent; only a suffix-colliding branch matched the
    # pattern. That is a fork (ref not on origin), NOT a merge race against the
    # wrong branch's sha.
    run = _fake_run("z" * 40 + "\trefs/heads/a/feature\n")
    monkeypatch.setattr(pr_delivery, "_run", run)
    with pytest.raises(pr_delivery.ForkPushUnsupported):
        pr_delivery.push_to_pr_ref(
            Path("/wt"), "converge-abc", "feature", expected_remote_sha="e" * 40
        )
    assert not any(c[:2] == ["git", "push"] for c in run.calls)  # never pushed


def test_push_to_pr_ref_raises_merge_race_when_remote_advanced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _fake_run("z" * 40 + "\trefs/heads/feature\n")  # remote moved
    monkeypatch.setattr(pr_delivery, "_run", run)
    with pytest.raises(pr_delivery.MergeRaceDetected):
        pr_delivery.push_to_pr_ref(
            Path("/wt"), "converge-abc", "feature", expected_remote_sha="e" * 40
        )
    # never pushed, and never with --force (would clobber the concurrent commit)
    assert not any(c[:2] == ["git", "push"] for c in run.calls)
    assert not any("--force" in c or "-f" in c for c in run.calls)


def test_push_to_pr_ref_raises_on_push_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # a non-race failure (auth) stays a generic RuntimeError, NOT a merge race
    run = _fake_run(
        "e" * 40 + "\trefs/heads/feature\n",
        push_rc=1,
        push_stderr="fatal: Authentication failed for 'https://github.com/o/r'",
    )
    monkeypatch.setattr(pr_delivery, "_run", run)
    with pytest.raises(RuntimeError) as excinfo:
        pr_delivery.push_to_pr_ref(
            Path("/wt"), "converge-abc", "feature", expected_remote_sha="e" * 40
        )
    assert not isinstance(excinfo.value, pr_delivery.MergeRaceDetected)


def test_push_to_pr_ref_maps_non_fast_forward_push_to_merge_race(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # TOCTOU: ls-remote matched, but the remote advanced before the push, which
    # git rejects as non-fast-forward. That must surface as merge_race (the same
    # outcome as the pre-check), never a force-push or a generic crash (finding #4).
    run = _fake_run(
        "e" * 40 + "\trefs/heads/feature\n",
        push_rc=1,
        push_stderr=(
            " ! [rejected]        converge-abc -> feature (non-fast-forward)\n"
            "error: failed to push some refs to 'origin'"
        ),
    )
    monkeypatch.setattr(pr_delivery, "_run", run)
    with pytest.raises(pr_delivery.MergeRaceDetected):
        pr_delivery.push_to_pr_ref(
            Path("/wt"), "converge-abc", "feature", expected_remote_sha="e" * 40
        )
    assert not any("--force" in c or "-f" in c for c in run.calls)


def test_push_to_pr_ref_hook_rejection_is_not_a_merge_race(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # a branch-protection / pre-receive hook rejection also says "rejected" but
    # is NOT a non-fast-forward race — re-running converge won't help, so it must
    # stay a generic RuntimeError, not a merge_race (Copilot #272).
    run = _fake_run(
        "e" * 40 + "\trefs/heads/feature\n",
        push_rc=1,
        push_stderr=(
            " ! [remote rejected] converge-abc -> feature "
            "(protected branch hook declined)\n"
            "error: failed to push some refs to 'origin'"
        ),
    )
    monkeypatch.setattr(pr_delivery, "_run", run)
    with pytest.raises(RuntimeError) as excinfo:
        pr_delivery.push_to_pr_ref(
            Path("/wt"), "converge-abc", "feature", expected_remote_sha="e" * 40
        )
    assert not isinstance(excinfo.value, pr_delivery.MergeRaceDetected)


def test_push_to_pr_ref_lease_rejection_is_merge_race(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The ls-remote pre-check passed (ref at expected), but the remote then changed
    # (deleted / advanced / force-rewound) before the leased push, which git
    # rejects with "stale info". That atomic-CAS failure is the race the lease
    # exists to catch → merge_race, never a silent recreate/overwrite (finding #1).
    run = _fake_run(
        "e" * 40 + "\trefs/heads/feature\n",
        push_rc=1,
        push_stderr=(
            " ! [rejected] converge-abc -> feature (stale info)\n"
            "error: failed to push some refs to 'origin'"
        ),
    )
    monkeypatch.setattr(pr_delivery, "_run", run)
    with pytest.raises(pr_delivery.MergeRaceDetected):
        pr_delivery.push_to_pr_ref(
            Path("/wt"), "converge-abc", "feature", expected_remote_sha="e" * 40
        )
    # the lease is pinned to the expected head, and never a blind --force
    push = next(c for c in run.calls if c[:2] == ["git", "push"])
    assert "--force-with-lease=refs/heads/feature:" + "e" * 40 in push
    assert "--force" not in push and "-f" not in push


def test_push_to_pr_ref_non_descendant_local_is_rejected_without_pushing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # append-only guard: if the local branch does NOT descend from the expected
    # head (a rewrite, not an append), refuse BEFORE pushing so the leased update
    # can only ever fast-forward — never rewind the contributor's history.
    run = _fake_run("e" * 40 + "\trefs/heads/feature\n", ancestor_rc=1)
    monkeypatch.setattr(pr_delivery, "_run", run)
    with pytest.raises(pr_delivery.MergeRaceDetected):
        pr_delivery.push_to_pr_ref(
            Path("/wt"), "converge-abc", "feature", expected_remote_sha="e" * 40
        )
    assert not any(c[:2] == ["git", "push"] for c in run.calls)  # never pushed


# --- push_to_pr_ref against a REAL local bare remote (finding: checked vs -----
# --- pushed object). Mocked _run can't exercise the actual git push semantics. -


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _sha(cwd: Path, ref: str = "HEAD") -> str:
    return subprocess.run(
        ["git", "rev-parse", ref], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()


def _remote_sha(bare: Path, ref: str) -> str:
    out = subprocess.run(
        ["git", "ls-remote", str(bare), f"refs/heads/{ref}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return out.split()[0] if out else ""


def _seed_pr_repo(tmp_path: Path) -> tuple[Path, Path, str, str, str]:
    """A bare 'origin' with `feature` at H (parent G), and a work repo whose HEAD
    is a descendant of H on branch `converge-x`. Returns (wt, bare, G, H, fixed)."""
    bare = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(bare)],
        check=True,
        capture_output=True,
    )
    wt = tmp_path / "wt"
    subprocess.run(
        ["git", "init", "-b", "main", str(wt)], check=True, capture_output=True
    )
    _git(wt, "config", "user.email", "t@t")
    _git(wt, "config", "user.name", "t")
    _git(wt, "remote", "add", "origin", str(bare))
    (wt / "a.txt").write_text("G\n")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-m", "G")
    g = _sha(wt)
    (wt / "a.txt").write_text("H\n")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-m", "H")
    h = _sha(wt)
    _git(wt, "push", "origin", "HEAD:refs/heads/feature")  # feature at H
    _git(wt, "checkout", "-b", "converge-x")
    (wt / "b.txt").write_text("fix\n")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-m", "fix")
    return wt, bare, g, h, _sha(wt)


def test_push_to_pr_ref_pushes_reviewed_head_real_git(tmp_path: Path) -> None:
    wt, bare, _g, h, fixed = _seed_pr_repo(tmp_path)
    pushed = pr_delivery.push_to_pr_ref(
        wt, "converge-x", "feature", expected_remote_sha=h
    )
    assert pushed == fixed  # returns the EXACT pushed sha (the reviewed HEAD)
    assert _remote_sha(bare, "feature") == fixed  # remote fast-forwarded to HEAD


def test_push_to_pr_ref_ignores_suffix_colliding_remote_branch_real_git(
    tmp_path: Path,
) -> None:
    # ls-remote pattern semantics: `feature` also matches refs/heads/a/feature,
    # which SORTS FIRST. The pre-fix code read the first returned sha → a false,
    # unresolvable merge_race for a valid PR whenever such a branch exists. Only
    # refs/heads/feature may govern the push; a/feature must be left untouched.
    wt, bare, g, h, fixed = _seed_pr_repo(tmp_path)
    _git(wt, "push", "origin", f"{g}:refs/heads/a/feature")  # collider at G ≠ H
    pushed = pr_delivery.push_to_pr_ref(
        wt, "converge-x", "feature", expected_remote_sha=h
    )
    assert pushed == fixed
    assert _remote_sha(bare, "feature") == fixed  # the real PR branch advanced
    assert _remote_sha(bare, "a/feature") == g  # the collider is untouched


def test_push_to_pr_ref_refuses_non_head_branch_real_git(tmp_path: Path) -> None:
    # a divergent local_branch pointing at an OLDER commit (G, an ancestor of the
    # remote's H) must NOT force the PR branch backward. The pre-fix code pushed
    # local_branch, so `stale`->feature would have rewound feature from H to G;
    # the fix pushes the reviewed HEAD and refuses a branch that isn't HEAD.
    wt, bare, g, h, _fixed = _seed_pr_repo(tmp_path)
    _git(wt, "branch", "stale", g)  # 'stale' at G, older than HEAD (and than H)
    with pytest.raises(RuntimeError):
        pr_delivery.push_to_pr_ref(wt, "stale", "feature", expected_remote_sha=h)
    assert _remote_sha(bare, "feature") == h  # remote UNCHANGED — never rewound
