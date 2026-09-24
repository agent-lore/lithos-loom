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

from lithos_loom.cli import _converge_push_facts as cli_facts
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
from lithos_loom.github_client import GitHubError
from lithos_loom.github_models import PullRequest
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
    (wt / "README.md").write_text("base\n", encoding="utf-8")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-m", "the base this PR branched from")
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


@pytest.fixture(autouse=True)
def gh(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Stub the two live reads the command makes before its verdict: the
    worktree's ``origin`` and the PR itself.

    Autouse, because every invocation makes them — the command fails closed
    when the PR cannot be re-read, so an un-stubbed test would only ever see
    the refusal.
    """
    state: dict = {
        "origin": _REPO_NAME,
        "pr": _pull_request(),
        "raises": None,
    }

    def _fetch(repo: str, number: int):
        state.setdefault("fetched", []).append((repo, number))
        if state["raises"] is not None:
            raise state["raises"]
        return state["pr"]

    monkeypatch.setattr(cli_facts, "origin_repo_name", lambda worktree: state["origin"])
    monkeypatch.setattr(cli_facts, "fetch_pull_request", _fetch)
    return state


def _pull_request(
    *,
    state: str = "open",
    merged: bool = False,
    head_ref: str = _PR_BRANCH,
    head_repo: str = _REPO_NAME,
) -> PullRequest:
    return PullRequest(
        repo=_REPO_NAME,
        number=425,
        state=state,
        merged=merged,
        merged_at=None,
        merge_commit_sha=None,
        head_ref=head_ref,
        head_repo=head_repo,
        base_repo=_REPO_NAME,
        base_ref="main",
    )


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


# ── round 2: the reviewers' findings ───────────────────────────────────


def test_reports_the_whole_commands_spend_not_just_the_loops(
    host, run_dir: Path, worktree: Path, lithos: FakeLithosClient, tmp_path: Path
) -> None:
    """correctness/f-001: converge's intake / triage turn is spend too, and it
    is the number the operator's push decision rests on."""
    run_outcome.write_state(run_dir, {"cost_usd": 12.34, "total_cost_usd": 20.5})
    out = tmp_path / "record.json"

    result = _invoke(_RUN, "--json", str(out))

    assert result.exit_code == 0, result.output
    assert "$20.50" in result.output and "$12.34" not in result.output
    assert json.loads(out.read_text(encoding="utf-8"))["total_cost_usd"] == 20.5


def test_falls_back_to_the_loop_cost_for_a_run_that_recorded_no_total(
    host, run_dir: Path, worktree: Path, lithos: FakeLithosClient
) -> None:
    # a run from before converge merged the whole-command figure in
    assert _invoke(_RUN).exit_code == 0
    assert "$12.34" in _invoke(_RUN).output


def test_a_run_killed_during_intake_is_still_resolvable(
    host, worktree: Path, lithos: FakeLithosClient, tmp_path: Path
) -> None:
    """correctness/f-002: the intake record must make the run DISCOVERABLE —
    every lookup goes through `is_run_dir`, which recognises a run by its
    handoff dir, and converge's own intake pass seeds only `<run>-intake`'s."""
    killed = tmp_path / "work" / run_outcome.CONVERGE_DIR / "killed01"
    run_outcome.record_converge_intake(
        killed,
        pr_url=_PR_URL,
        pr_number=425,
        pr_head_branch=_PR_BRANCH,
        intake_head_sha=_remote_head(worktree),
        base_sha="b" * 40,
        repo=_REPO_NAME,
        story_id=_STORY,
    )

    assert run_outcome.is_run_dir(killed)
    assert run_outcome.resolve_run_dir(tmp_path / "work", "killed01") == killed
    # …and the command reaches the run rather than reporting it nonexistent:
    # a run with no outcome is refused as in-flight (exit 1), not exit 2
    result = _invoke("killed01")
    assert result.exit_code == 1, result.output
    assert "recorded no outcome" in result.output


def test_replies_only_to_the_threads_the_push_newly_answers(
    host,
    run_dir: Path,
    worktree: Path,
    lithos: FakeLithosClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """correctness/f-003: the run's own epilogue already answered the triage
    rejection when it exited — re-posting it would duplicate on a public
    thread. Only the FIXED ack, which needed the push to be assertable, is
    newly owed."""
    record_external_intake(
        run_dir,
        id_map={"f-001": _external_finding(7), "f-002": _external_finding(8)},
        rejections={"f-002": "the guard is already there, see line 40"},
        nothing_to_remediate={},
        surviving_ids=["f-001"],
    )
    (run_dir / "handoff" / "round_05_coder_done.md").write_text(
        "## Status: LGTM\n\n## Summary\nfixed it\n\n"
        "## External findings\n- f-001: FIXED — inverted the guard\n",
        encoding="utf-8",
    )
    replies: list[tuple[int, str]] = []
    monkeypatch.setattr(
        converge_cli,
        "post_thread_reply",
        lambda repo, pr, activity_id, body: replies.append((activity_id, body)) or True,
    )

    assert _invoke(_RUN, "--yes").exit_code == 0

    assert [activity_id for activity_id, _ in replies] == [7]
    assert replies[0][1].startswith("Fixed in ")


def test_an_infra_failed_run_still_owes_every_thread(
    host,
    run_dir: Path,
    worktree: Path,
    lithos: FakeLithosClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # converge skips its reply epilogue entirely on an infra death (#377), so
    # nothing was answered and the whole batch is owed
    run_outcome.write_state(run_dir, {"status": "infra_failed"})
    record_external_intake(
        run_dir,
        id_map={"f-002": _external_finding(8)},
        rejections={"f-002": "the guard is already there"},
        nothing_to_remediate={},
        surviving_ids=[],
    )
    replies: list[int] = []
    monkeypatch.setattr(
        converge_cli,
        "post_thread_reply",
        lambda repo, pr, activity_id, body: replies.append(activity_id) or True,
    )

    assert _invoke(_RUN, "--yes").exit_code == 0

    assert replies == [8]


def test_a_push_that_landed_but_reported_failure_runs_the_epilogue(
    host,
    run_dir: Path,
    worktree: Path,
    lithos: FakeLithosClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """correctness/f-004: the lease proves atomicity, not observability — a
    dropped connection after the server applied the update must not be
    reported as a non-write."""
    real = cli.push_to_pr_ref

    def lands_then_dies(*args, **kwargs):
        real(*args, **kwargs)
        raise RuntimeError("git push to the PR failed: connection reset by peer")

    monkeypatch.setattr(cli, "push_to_pr_ref", lands_then_dies)
    tip = _git(worktree, "rev-parse", "HEAD")

    result = _invoke(_RUN, "--yes")

    assert result.exit_code == 0, result.output
    assert _remote_head(worktree) == tip
    assert "LANDED" in result.output
    # the audit the PR would otherwise be missing
    assert [f["task_id"] for f in lithos.findings] == [_STORY]
    assert run_outcome.converge_pushed_sha(run_dir) == tip


def test_a_push_that_truly_failed_is_a_refusal_with_nothing_written(
    host,
    run_dir: Path,
    worktree: Path,
    lithos: FakeLithosClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        cli,
        "push_to_pr_ref",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("auth failed")),
    )
    before = _remote_head(worktree)

    result = _invoke(_RUN, "--yes")

    assert result.exit_code == 1, result.output
    assert _remote_head(worktree) == before
    assert lithos.calls == []
    assert run_outcome.converge_pushed_sha(run_dir) is None


def test_an_unreadable_remote_after_a_failed_push_is_uncertain_not_refused(
    host,
    run_dir: Path,
    worktree: Path,
    lithos: FakeLithosClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        cli,
        "push_to_pr_ref",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("connection reset")),
    )
    # the plan's own read lives in the facts module and still answers; the
    # read-BACK after the failed push is the one that cannot
    monkeypatch.setattr(
        cli,
        "remote_head_sha",
        lambda wt, ref: (_ for _ in ()).throw(RuntimeError("origin unreachable")),
    )

    result = _invoke(_RUN, "--yes")

    assert result.exit_code == 2, result.output
    assert "not known whether" in result.output


def test_terminal_escapes_in_agent_text_never_reach_stdout(
    host, run_dir: Path, worktree: Path, lithos: FakeLithosClient
) -> None:
    """security/f-001: the report IS the surface the push is authorised from,
    and `failure_reason` is the agent CLI's own error text."""
    run_outcome.write_state(
        run_dir,
        {"failure_reason": "round 5: \x1b[2J\x1b[H all clear, nothing open"},
    )
    (worktree / "evil.py").write_text("z = 3\n", encoding="utf-8")
    _git(worktree, "add", "-A")
    _git(worktree, "commit", "-m", "round 3: \x1b[2K\x1b[A forged subject")

    # color=True: click strips ANSI itself when the sink is not a tty, so a
    # runner in the default mode cannot see this bug at all — the operator's
    # real terminal IS a tty, which is where the forgery would land.
    result = runner.invoke(develop_app, ["converge-push", _RUN], color=True)

    assert result.exit_code == 0, result.output
    assert "\x1b" not in result.output
    # the text itself still reads, minus the escapes
    assert "all clear, nothing open" in result.output
    assert "forged subject" in result.output


def test_the_report_measures_from_the_head_the_push_is_leased_against(
    host, run_dir: Path, worktree: Path, lithos: FakeLithosClient, tmp_path: Path
) -> None:
    """security/f-002: a rewind of the PR head (how a leaked secret is taken
    back off a branch) is still an ancestor of the run's tip, so the push
    RESTORES it — the report must list what it would land, not what the run
    added to the intake head."""
    # rewind the PR branch past its own commit — what taking an accidentally
    # committed secret back off a branch looks like
    root = _git(worktree, "rev-list", "--max-parents=0", "HEAD")
    other = tmp_path / "rewinder"
    subprocess.run(
        ["git", "clone", str(tmp_path / "origin.git"), str(other)],
        check=True,
        capture_output=True,
    )
    _git(other, "push", "--force", "origin", f"{root}:refs/heads/{_PR_BRANCH}")
    out = tmp_path / "record.json"

    result = _invoke(_RUN, "--json", str(out))

    assert result.exit_code == 0, result.output
    assert "fast-forward" in result.output
    # the restored commit is named, not silently re-landed
    assert "the PR's own work" in result.output
    assert len(json.loads(out.read_text(encoding="utf-8"))["fixer_commits"]) == 3


@pytest.mark.parametrize(
    ("pr", "expected"),
    [
        (_pull_request(state="closed", merged=True), "MERGED"),
        (_pull_request(state="closed"), "closed"),
        (_pull_request(head_ref="someone-elses-branch"), "now heads"),
        (_pull_request(head_repo="a-fork/lithos-loom"), "fork"),
        (None, "no longer exists"),
    ],
)
def test_refuses_when_the_pr_is_no_longer_the_one_recorded(
    host,
    run_dir: Path,
    worktree: Path,
    lithos: FakeLithosClient,
    gh: dict,
    pr,
    expected: str,
) -> None:
    """security/f-003: the recorded PR facts are days old; `ls-remote` answers
    about a branch NAME, not about the PR."""
    gh["pr"] = pr
    before = _remote_head(worktree)

    for args in ((_RUN,), (_RUN, "--yes")):
        result = _invoke(*args)
        assert result.exit_code == 1, result.output
        assert "refused" in result.output and expected in result.output
        assert _remote_head(worktree) == before
        assert lithos.calls == []


def test_refuses_when_the_pr_cannot_be_re_read(
    host, run_dir: Path, worktree: Path, lithos: FakeLithosClient, gh: dict
) -> None:
    # fails closed: the same stale record addresses the replies and the audit
    gh["raises"] = GitHubError("gh is not authenticated")
    before = _remote_head(worktree)

    result = _invoke(_RUN, "--yes")

    assert result.exit_code == 1, result.output
    assert "could not read" in result.output
    assert _remote_head(worktree) == before


def test_refuses_when_the_worktrees_origin_is_not_the_recorded_repo(
    host, run_dir: Path, worktree: Path, lithos: FakeLithosClient, gh: dict
) -> None:
    gh["origin"] = "someone-else/lithos-loom"
    before = _remote_head(worktree)

    result = _invoke(_RUN, "--yes")

    assert result.exit_code == 1, result.output
    assert "not the" in result.output and "recorded" in result.output
    assert _remote_head(worktree) == before
