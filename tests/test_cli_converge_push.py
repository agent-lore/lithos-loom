"""Tests for ``lithos-loom develop converge-push`` (#425's salvage path).

Three layers, as in ``test_cli_deliver``:

1. **Real git** for the push decision — a bare ``origin``, a "PR branch" on it
   and a converge worktree ahead of it, so ``fast-forward`` / ``already
   pushed`` / ``refused: PR head moved`` is decided by git, not by a stub
   agreeing with the code.
2. **A stubbed reply transport** (the ``pr_delivery`` seam the converge CLI
   routes thread replies through) for the epilogue.
3. **A ``FakeLithosClient``** for the ``[ConvergePushed]`` finding and the
   ``--complete-gate`` gate match.

Every invocation goes through the real Typer parser (``CliRunner``).
Hermetic: no live Lithos, no network, no ``gh`` binary.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from lithos_loom.cli import converge as converge_cli
from lithos_loom.cli import converge_push as cli
from lithos_loom.cli import deliver as deliver_cli
from lithos_loom.cli.develop import develop_app
from lithos_loom.errors import LithosClientError
from lithos_loom.gates import (
    GATE_TYPE_HUMAN,
    RAISED_BY_LOOM,
    ROUTE_EXTERNAL_REMEDIATION,
    WAITS_ON_GATE,
)
from lithos_loom.github_review_activity import ReviewStream
from lithos_loom.github_review_streams import ReplyMode
from lithos_loom.plugins.story_develop import run_outcome
from lithos_loom.plugins.story_develop.external_record import record_external_intake
from lithos_loom.plugins.story_develop.external_reviews import ExternalFinding
from lithos_loom.plugins.story_develop.pr_delivery import AUTOMATED_MARKER
from tests.support import FakeLithosClient, make_task

runner = CliRunner()

_RUN = "26f8ecc5"
_STORY = "story-284f3a40"
_PR_BRANCH = "loom/external-remediation-425"
_LOCAL_BRANCH = "loom-external-remediation-425-4f2a"
_REPO_NAME = "agent-lore/lithos-loom"
_PR_URL = f"https://github.com/{_REPO_NAME}/pull/425"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def worktree(tmp_path: Path) -> Path:
    """A converge worktree whose tip is 2 fixer commits ahead of the PR head."""
    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(origin)],
        check=True,
        capture_output=True,
    )
    wt = tmp_path / "work" / "converge" / _RUN / "worktree"
    wt.parent.mkdir(parents=True)
    subprocess.run(
        ["git", "clone", str(origin), str(wt)], check=True, capture_output=True
    )
    _git(wt, "config", "user.email", "t@example.com")
    _git(wt, "config", "user.name", "T")
    (wt / "feature.py").write_text("x = 1\n", encoding="utf-8")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-m", "the PR's own work")
    _git(wt, "push", "-u", "origin", f"HEAD:refs/heads/{_PR_BRANCH}")
    # the converge run's local branch, cut at the PR head, with its rounds on it
    _git(wt, "checkout", "-b", _LOCAL_BRANCH)
    for round_no in (1, 2):
        (wt / f"fix_{round_no}.py").write_text("y = 2\n", encoding="utf-8")
        _git(wt, "add", "-A")
        _git(wt, "commit", "-m", f"round {round_no}: fix the reviewer's finding")
    return wt


def _remote_head(worktree: Path) -> str:
    out = _git(worktree, "ls-remote", "--heads", "origin", f"refs/heads/{_PR_BRANCH}")
    return out.split()[0] if out else ""


@pytest.fixture
def run_dir(worktree: Path) -> Path:
    """The run dir a ``max_rounds`` converge run leaves behind."""
    d = worktree.parent
    (d / "handoff").mkdir()
    (d / "state.json").write_text(
        json.dumps(
            {
                "status": "max_rounds",
                "run_id": _RUN,
                "branch": _LOCAL_BRANCH,
                "worktree": str(worktree),
                "rounds": 5,
                "cost_usd": 12.34,
                "test_gate": {"verdict": "GREEN", "command": "make check"},
                "blocking_checks": [],
                "open_findings": [
                    {
                        "reviewer": "opus",
                        "finding_id": "f-002",
                        "severity": "minor",
                        "title": "the note is not rendered on the gate",
                    }
                ],
                run_outcome.CONVERGE_KEY: {
                    "pr_url": _PR_URL,
                    "pr_number": 425,
                    "pr_head_branch": _PR_BRANCH,
                    "intake_head_sha": _remote_head(worktree),
                    "base_sha": "b" * 40,
                    "repo": _REPO_NAME,
                    "story_id": _STORY,
                },
            }
        ),
        encoding="utf-8",
    )
    return d


@pytest.fixture
def host(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> SimpleNamespace:
    cfg = SimpleNamespace(
        orchestrator=SimpleNamespace(
            work_dir=tmp_path / "work",
            agent_id="loom",
            lithos_url="http://lithos.invalid",
        )
    )
    monkeypatch.setattr(cli, "load_config", lambda config=None: cfg)
    return cfg


@pytest.fixture
def lithos(monkeypatch: pytest.MonkeyPatch) -> FakeLithosClient:
    """The story plus the exhaustion gate the watcher raised for THIS run."""
    client = FakeLithosClient(agent_id="loom")
    client.add_task(make_task(_STORY, title="Converge the delivered PR"))
    client.add_task(
        make_task(
            "gate-exhausted",
            title=f"Needs human: {_STORY}",
            task_type="gate",
            metadata={
                "gate_type": GATE_TYPE_HUMAN,
                "raised_by": RAISED_BY_LOOM,
                "route": ROUTE_EXTERNAL_REMEDIATION,
                "story_id": _STORY,
                "run_id": _RUN,
                "escalation_reason": "remediation_exhausted",
                "escalation_summary": "budget spent",
            },
        )
    )
    client.add_edge(
        from_task_id="gate-exhausted", to_task_id=_STORY, type=WAITS_ON_GATE
    )
    monkeypatch.setattr(cli, "LithosClient", lambda *a, **k: client)
    return client


def _invoke(*args: str):
    return runner.invoke(develop_app, ["converge-push", *args])


def _get(client: FakeLithosClient, task_id: str):
    task = asyncio.run(client.task_get(task_id=task_id))
    assert task is not None
    return task


# ── the report ─────────────────────────────────────────────────────────


def test_reports_the_plan_and_writes_nothing(
    host, run_dir: Path, worktree: Path, lithos: FakeLithosClient, tmp_path: Path
) -> None:
    before = _remote_head(worktree)
    out = tmp_path / "record.json"

    result = _invoke(_RUN, "--json", str(out))

    assert result.exit_code == 0, result.output
    assert "max_rounds" in result.output
    assert _PR_URL in result.output and _PR_BRANCH in result.output
    assert "fast-forward" in result.output
    # the fixer commits, by their subjects
    assert "round 1: fix the reviewer's finding" in result.output
    assert "round 2: fix the reviewer's finding" in result.output
    # the last review round's open findings, from the ledger record
    assert "f-002" in result.output and "the note is not rendered" in result.output
    # nothing written: the remote is untouched, Lithos saw no call, and the
    # run dir records no push
    assert _remote_head(worktree) == before
    assert lithos.calls == []
    assert run_outcome.converge_pushed_sha(run_dir) is None

    record = json.loads(out.read_text(encoding="utf-8"))
    assert record["verdict"] == "fast-forward"
    assert record["pushed"] is False
    assert record["pushed_sha"] is None
    assert len(record["fixer_commits"]) == 2
    assert record["rounds"] == 5 and record["total_cost_usd"] == 12.34
    assert record["pr_number"] == 425 and record["story_id"] == _STORY


# ── the push ───────────────────────────────────────────────────────────


def test_yes_pushes_the_fixer_commits_and_posts_the_finding(
    host, run_dir: Path, worktree: Path, lithos: FakeLithosClient
) -> None:
    before = _remote_head(worktree)
    tip = _git(worktree, "rev-parse", "HEAD")

    result = _invoke(_RUN, "--yes")

    assert result.exit_code == 0, result.output
    # exactly the fixer commits: the remote is the worktree tip, and the
    # previous remote tip is still an ancestor of it (append-only)
    assert _remote_head(worktree) == tip
    assert (
        subprocess.run(
            ["git", "-C", str(worktree), "merge-base", "--is-ancestor", before, tip]
        ).returncode
        == 0
    )
    # the outcome finding, naming the sha, the rounds and what was left open
    posted = [f for f in lithos.findings if f["task_id"] == _STORY]
    assert len(posted) == 1
    summary = posted[0]["summary"]
    assert summary.startswith(cli.CONVERGE_PUSHED)
    assert tip[:12] in summary
    assert "rounds: 5" in summary
    assert "f-002" in summary  # pushed WITH this finding open
    # no gate was touched without --complete-gate
    assert _get(lithos, "gate-exhausted").status == "open"
    # and the run records its own push, for `develop list` and a re-run
    assert run_outcome.converge_pushed_sha(run_dir) == tip


def test_a_second_invocation_is_already_pushed_and_writes_nothing(
    host, run_dir: Path, worktree: Path, lithos: FakeLithosClient
) -> None:
    assert _invoke(_RUN, "--yes").exit_code == 0
    tip = _remote_head(worktree)
    lithos.calls.clear()

    result = _invoke(_RUN, "--yes")

    assert result.exit_code == 0, result.output
    assert "already pushed" in result.output
    assert _remote_head(worktree) == tip
    assert lithos.calls == []


def test_refuses_when_the_pr_head_moved_and_writes_nothing(
    host, run_dir: Path, worktree: Path, lithos: FakeLithosClient, tmp_path: Path
) -> None:
    # somebody else pushed onto the PR branch: the remote head is no longer an
    # ancestor of this run's tip
    other = tmp_path / "other"
    subprocess.run(
        ["git", "clone", str(tmp_path / "origin.git"), str(other)],
        check=True,
        capture_output=True,
    )
    _git(other, "config", "user.email", "o@example.com")
    _git(other, "config", "user.name", "O")
    _git(other, "checkout", _PR_BRANCH)
    (other / "theirs.py").write_text("z = 3\n", encoding="utf-8")
    _git(other, "add", "-A")
    _git(other, "commit", "-m", "someone else's commit")
    _git(other, "push", "origin", _PR_BRANCH)
    moved = _remote_head(worktree)

    for args in ((_RUN,), (_RUN, "--yes")):
        result = _invoke(*args)
        assert result.exit_code == 1, result.output
        assert "refused" in result.output and "PR head moved" in result.output
        assert _remote_head(worktree) == moved  # nothing pushed
        assert lithos.calls == []  # no finding, no gate
        assert run_outcome.converge_pushed_sha(run_dir) is None


def test_complete_gate_completes_only_this_runs_exhaustion_gate(
    host, run_dir: Path, lithos: FakeLithosClient, tmp_path: Path
) -> None:
    # a second gate on the same story: another run's, same reason
    lithos.add_task(
        make_task(
            "gate-other-run",
            task_type="gate",
            metadata={
                "gate_type": GATE_TYPE_HUMAN,
                "raised_by": RAISED_BY_LOOM,
                "route": ROUTE_EXTERNAL_REMEDIATION,
                "story_id": _STORY,
                "run_id": "other-run",
                "escalation_reason": "remediation_exhausted",
                "escalation_summary": "a different run",
            },
        )
    )
    lithos.add_edge(
        from_task_id="gate-other-run", to_task_id=_STORY, type=WAITS_ON_GATE
    )
    out = tmp_path / "record.json"

    result = _invoke(_RUN, "--yes", "--complete-gate", "--json", str(out))

    assert result.exit_code == 0, result.output
    assert _get(lithos, "gate-exhausted").status == "completed"
    assert _get(lithos, "gate-other-run").status == "open"
    # never a cancellation
    assert not lithos.called("task_cancel")
    assert json.loads(out.read_text(encoding="utf-8"))["pushed"] is True


def test_refuses_a_run_with_no_recorded_outcome(
    host, run_dir: Path, worktree: Path, lithos: FakeLithosClient
) -> None:
    (run_dir / "state.json").unlink()

    result = _invoke(_RUN)

    assert result.exit_code == 1
    assert "recorded no outcome" in result.output
    assert lithos.calls == []


def test_refuses_a_story_develop_run_and_names_deliver(
    host, tmp_path: Path, lithos: FakeLithosClient
) -> None:
    story_run = tmp_path / "work" / _STORY / "aa11bb22"
    (story_run / "handoff").mkdir(parents=True)

    result = _invoke("aa11bb22")

    assert result.exit_code == 2
    assert "develop deliver" in result.output
    assert story_run.is_dir()


def test_resolves_a_pr_number_to_its_converge_run(
    host, run_dir: Path, worktree: Path, lithos: FakeLithosClient
) -> None:
    result = _invoke("425")

    assert result.exit_code == 0, result.output
    assert _RUN in result.output


# ── the thread replies ─────────────────────────────────────────────────


def _external_finding(activity_id: int = 7) -> ExternalFinding:
    return ExternalFinding(
        author="copilot-pull-request-reviewer[bot]",
        source="bot",
        trusted=True,
        stream=ReviewStream.INLINE,
        activity_id=activity_id,
        reply_mode=ReplyMode.THREAD,
        thread_url=f"{_PR_URL}#discussion_r{activity_id}",
        head_sha="a" * 40,
        path="src/x.py",
        line=12,
        body="this guard is inverted",
    )


def test_yes_posts_the_thread_replies_the_run_never_got_to_post(
    host,
    run_dir: Path,
    worktree: Path,
    lithos: FakeLithosClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record_external_intake(
        run_dir,
        id_map={"f-001": _external_finding()},
        rejections={},
        nothing_to_remediate={},
        surviving_ids=["f-001"],
    )
    # the coder's final-round acknowledgement — the only channel that earns a
    # "Fixed in <sha>" reply
    (run_dir / "handoff" / "round_05_coder_done.md").write_text(
        "## Status: LGTM\n\n## Summary\nfixed it\n\n"
        "## External findings\n- f-001: FIXED — inverted the guard\n",
        encoding="utf-8",
    )
    replies: list[tuple[str, int, int, str]] = []
    monkeypatch.setattr(
        converge_cli,
        "post_thread_reply",
        lambda repo, pr, activity_id, body: (
            replies.append((repo, pr, activity_id, body)) or True
        ),
    )

    result = _invoke(_RUN, "--yes")

    assert result.exit_code == 0, result.output
    assert len(replies) == 1
    repo, pr_number, activity_id, body = replies[0]
    assert (repo, pr_number, activity_id) == (_REPO_NAME, 425, 7)
    assert body.startswith("Fixed in ")
    assert _remote_head(worktree)[:10] in body
    assert "inverted the guard" in body
    # every reply carries the automated marker the watcher's trust filter reads
    assert AUTOMATED_MARKER in body


def test_no_reply_is_posted_without_the_push(
    host,
    run_dir: Path,
    worktree: Path,
    lithos: FakeLithosClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record_external_intake(
        run_dir,
        id_map={"f-001": _external_finding()},
        rejections={},
        nothing_to_remediate={},
        surviving_ids=["f-001"],
    )
    replies: list[object] = []
    monkeypatch.setattr(
        converge_cli,
        "post_thread_reply",
        lambda *a: replies.append(a) or True,
    )

    assert _invoke(_RUN).exit_code == 0  # report only

    assert replies == []


# ── `develop deliver` refuses a converge run ───────────────────────────


def test_deliver_refuses_a_converge_run(
    monkeypatch: pytest.MonkeyPatch, run_dir: Path, tmp_path: Path, worktree: Path
) -> None:
    cfg = SimpleNamespace(
        orchestrator=SimpleNamespace(
            work_dir=tmp_path / "work",
            agent_id="loom",
            lithos_url="http://lithos.invalid",
        ),
        projects={},
        routes=(),
        story_develop=SimpleNamespace(operator_github_login=None),
    )
    monkeypatch.setattr(deliver_cli, "load_config", lambda config=None: cfg)
    # a Lithos client here would be a bug: the refusal precedes every read
    monkeypatch.setattr(
        deliver_cli,
        "read_story_sync",
        lambda *a, **k: pytest.fail("deliver must refuse before reading the story"),
    )
    before = _remote_head(worktree)

    result = runner.invoke(develop_app, ["deliver", _RUN])

    assert result.exit_code == 2, result.output
    assert "develop converge-push" in result.output
    assert _remote_head(worktree) == before


def test_a_landed_push_is_never_reported_as_a_failure(
    host, run_dir: Path, worktree: Path, lithos: FakeLithosClient
) -> None:
    """Everything after the push degrades into a note: the commits are on the
    PR, and no later failure may read as a failure to push."""
    lithos.raise_on["finding_post"] = LithosClientError(
        "server_error", "lithos is down"
    )
    tip = _git(worktree, "rev-parse", "HEAD")

    result = _invoke(_RUN, "--yes")

    assert result.exit_code == 0, result.output
    assert _remote_head(worktree) == tip
    assert "note:" in result.output and "lithos is down" in result.output
    # …and the push is still recorded, so a re-run is `already pushed`
    assert run_outcome.converge_pushed_sha(run_dir) == tip
