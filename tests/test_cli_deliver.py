"""Tests for ``lithos-loom develop deliver`` (the stopped-run delivery).

Three layers:

1. **Real git** for the four push cases — a bare ``origin`` plus a clone in
   ``tmp_path``, so "absent / equal / fast-forward / diverged-refused" is
   decided by git itself rather than by a stub agreeing with the code.
2. **A stubbed ``gh`` seam** (the ``pr_delivery`` wrappers ``deliver``
   imports) for open-vs-adopt.
3. **A ``FakeLithosClient``** for the gate swap, with a call-order spy pinning
   the load-bearing ordering: the ``pr`` gate must hold the story BEFORE the
   needs-human gate is completed.

Hermetic: no live Lithos, no network, no ``gh`` binary.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from lithos_loom.cli import _deliver_lithos as cli_lithos
from lithos_loom.cli import _deliver_repo as cli_repo
from lithos_loom.cli import deliver as cli
from lithos_loom.cli.develop import develop_app
from lithos_loom.errors import LithosClientError
from lithos_loom.gates import (
    GATE_TYPE_HUMAN,
    GATE_TYPE_PR,
    RAISED_BY_LOOM,
    STORY_GATE_ID_KEY,
    STORY_HUMAN_GATE_ID_KEY,
    WAITS_ON_GATE,
)
from lithos_loom.plugins.story_develop.github_access import OpenPullRequest
from tests.support import FakeLithosClient, make_task

runner = CliRunner()

_PR_URL = "https://github.com/agent-lore/lithos-loom/pull/99"
_STORY = "story-ac1380c1"
_RUN = "de459d10"
_BRANCH = "loom/story-ac1380c1-4f2a"
_SLUG = "lithos-loom"
_REPO_NAME = "agent-lore/lithos-loom"


# ── fixtures ───────────────────────────────────────────────────────────


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True
    )
    return proc.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A clone with a stopped run's branch on it, plus a bare ``origin``."""
    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(origin)],
        check=True,
        capture_output=True,
    )
    seed = tmp_path / "seed"
    subprocess.run(
        ["git", "clone", str(origin), str(seed)], check=True, capture_output=True
    )
    _git(seed, "config", "user.email", "t@example.com")
    _git(seed, "config", "user.name", "T")
    (seed / "README.md").write_text("base\n", encoding="utf-8")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-m", "base")
    _git(seed, "push", "-u", "origin", "main")

    work = tmp_path / "repo"
    subprocess.run(
        ["git", "clone", str(origin), str(work)], check=True, capture_output=True
    )
    _git(work, "config", "user.email", "t@example.com")
    _git(work, "config", "user.name", "T")
    _git(work, "checkout", "-b", _BRANCH)
    (work / "feature.py").write_text("x = 1\n", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "round 1")
    return work


@pytest.fixture
def run_dir(tmp_path: Path) -> Path:
    """A retained run dir for a ``disputed`` stop, as the plugin leaves it."""
    d = tmp_path / "work" / _STORY / _RUN
    (d / "handoff").mkdir(parents=True)
    (d / "state.json").write_text(
        json.dumps(
            {
                "status": "disputed",
                "run_id": _RUN,
                "branch": _BRANCH,
                "worktree": str(tmp_path / "gone"),
                "rounds": 4,
                "failure_reason": "reviewer and coder deadlocked on the AC",
            }
        ),
        encoding="utf-8",
    )
    (d.parent / "result.json").write_text(
        json.dumps(
            {
                "run_id": _RUN,
                "status": "failed",
                "escalation": {
                    "reason": "disputed",
                    "summary": "deadlock",
                    "brief": {"cost_usd": 33.57, "test_gate_verdict": "GREEN"},
                },
            }
        ),
        encoding="utf-8",
    )
    return d


@pytest.fixture
def lithos(monkeypatch: pytest.MonkeyPatch) -> FakeLithosClient:
    """A fake Lithos holding the story + the human gate its stop raised."""
    client = FakeLithosClient(agent_id="loom")
    client.add_task(
        make_task(
            _STORY,
            title="Deliver a stopped run's branch",
            description="The gap: a stopped run leaves a branch with no PR.",
            metadata={
                "project": _SLUG,
                "acceptance_criteria": "Done when the PR is open and gated.",
                "loom_last_attempt:story-develop": {"status": "failed"},
                STORY_HUMAN_GATE_ID_KEY: "gate-human",
            },
        )
    )
    client.add_task(
        make_task(
            "gate-human",
            title=f"Needs human: {_STORY}",
            task_type="gate",
            metadata={
                "gate_type": GATE_TYPE_HUMAN,
                "raised_by": RAISED_BY_LOOM,
                "route": "story-develop",
                "story_id": _STORY,
                "escalation_reason": "disputed",
                "escalation_summary": "deadlock",
            },
        )
    )
    client.add_edge(from_task_id="gate-human", to_task_id=_STORY, type=WAITS_ON_GATE)
    monkeypatch.setattr(cli_lithos, "LithosClient", lambda *a, **k: client)
    return client


@pytest.fixture
def host(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, repo: Path
) -> SimpleNamespace:
    cfg = SimpleNamespace(
        orchestrator=SimpleNamespace(
            work_dir=tmp_path / "work",
            agent_id="loom",
            lithos_url="http://lithos.invalid",
        ),
        projects={_SLUG: SimpleNamespace(name=_SLUG, repo=repo)},
        story_develop=SimpleNamespace(operator_github_login=None),
    )
    monkeypatch.setattr(cli, "load_config", lambda config=None: cfg)
    return cfg


def _open_pr(
    *,
    number: int = 99,
    url: str = _PR_URL,
    head_sha: str = "",
    cross_repository: bool = False,
    base_ref: str = "main",
    head_owner: str = "agent-lore",
) -> OpenPullRequest:
    return OpenPullRequest(
        number=number,
        url=url,
        head_sha=head_sha,
        cross_repository=cross_repository,
        base_ref=base_ref,
        head_owner=head_owner,
    )


@pytest.fixture
def gh(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Stub the ``gh``-shaped seams ``deliver`` uses.

    ``existing`` is the list ``gh pr list`` would return; tests that want the
    adopt path set it (``_open_pr(head_sha=<the pushed sha>)`` is ours).
    """
    calls: dict[str, Any] = {"existing": [], "created": [], "listed": []}

    def _list(repo_path: Path, branch: str, *, repo_name: str | None = None):
        calls["listed"].append({"branch": branch, "repo_name": repo_name})
        return list(calls["existing"])

    def _create(
        repo_path: Path,
        *,
        branch: str,
        base: str,
        title: str,
        body: str,
        repo_name: str | None = None,
    ):
        calls["created"].append(
            {
                "branch": branch,
                "base": base,
                "title": title,
                "body": body,
                "repo_name": repo_name,
            }
        )
        return _PR_URL

    # The fixture's `origin` is a bare repo in tmp_path (so the push cases are
    # real git); the owner/name pin gets its own unit tests below.
    monkeypatch.setattr(cli, "origin_repo_name", lambda repo_path: _REPO_NAME)
    monkeypatch.setattr(cli_repo, "list_open_prs_for_branch", _list)
    monkeypatch.setattr(cli_repo, "create_pr", _create)
    monkeypatch.setattr(
        cli_repo, "default_base_branch", lambda repo_path, repo_name=None: "main"
    )
    return calls


def _head(repo: Path) -> str:
    """The branch's current sha — what an adoptable PR's head must be."""
    return _git(repo, "rev-parse", _BRANCH)


def _invoke(*args: str):
    return runner.invoke(develop_app, ["deliver", *args])


def _get(client: FakeLithosClient, task_id: str):
    """The CLI is sync (it owns ``asyncio.run``), so these tests are too: the
    fake's async reads are driven through their own loop."""
    task = asyncio.run(client.task_get(task_id=task_id))
    assert task is not None
    return task


def _edges_into(client: FakeLithosClient, task_id: str):
    return asyncio.run(
        client.task_edge_list(
            task_id=task_id, direction="incoming", types=[WAITS_ON_GATE]
        )
    )


# ── the happy path ─────────────────────────────────────────────────────


def test_delivers_a_stopped_run_end_to_end(
    host, lithos: FakeLithosClient, run_dir: Path, repo: Path, gh: dict, tmp_path: Path
) -> None:
    out = tmp_path / "record.json"
    result = _invoke(_RUN, "--json", str(out))

    assert result.exit_code == 0, result.output
    # 1 — the branch is on origin
    assert (
        subprocess.run(
            ["git", "-C", str(repo), "ls-remote", "--heads", "origin", _BRANCH],
            capture_output=True,
            text=True,
        ).stdout.strip()
        != ""
    )
    # 2 — one PR, opened onto the default branch, carrying the provenance
    assert len(gh["created"]) == 1
    body = gh["created"][0]["body"]
    assert gh["created"][0]["base"] == "main"
    assert "## Provenance" in body
    assert _RUN in body and "disputed" in body
    assert f"`{_STORY}`" in body  # the story id, for the issue mirror
    assert "Done when the PR is open and gated." in body

    # 3 — the pr gate holds the story, and its id is recorded on it
    record = json.loads(out.read_text())
    gate_id = record["pr_gate_id"]
    assert gate_id
    gate = _get(lithos, gate_id)
    assert gate.task_type == "gate"
    assert gate.metadata["gate_type"] == GATE_TYPE_PR
    assert gate.metadata["pr_url"] == _PR_URL
    assert gate.metadata["project"] == _SLUG
    edges = _edges_into(lithos, _STORY)
    assert gate_id in {e.from_task_id for e in edges}
    story = _get(lithos, _STORY)
    assert story.metadata[STORY_GATE_ID_KEY] == gate_id
    # the stop's markers are retired on that same write
    assert STORY_HUMAN_GATE_ID_KEY not in story.metadata
    assert "loom_last_attempt:story-develop" not in story.metadata

    # 4 — the human gate is completed
    assert (_get(lithos, "gate-human")).status == "completed"
    assert record["human_gates_completed"] == ["gate-human"]

    # 5 — exactly one provenance finding, naming the PR and the run
    findings = [f["summary"] for f in lithos.findings if f["task_id"] == _STORY]
    assert len(findings) == 1
    assert findings[0].startswith(cli.MANUAL_DELIVERY)
    assert _PR_URL in findings[0] and _RUN in findings[0] and gate_id in findings[0]

    assert record["pushed"] is True
    assert record["adopted"] is False
    assert record["pr_number"] == 99
    assert record["notes"] == []


def test_pr_gate_is_created_before_the_human_gate_is_completed(
    host, lithos: FakeLithosClient, run_dir: Path, repo: Path, gh: dict
) -> None:
    """Gate-first ordering: completing the human gate while nothing else blocks
    the story would put it back on the ready frontier — a live runner could
    claim it into a duplicate run."""
    order: list[str] = []
    create, complete = lithos.task_create, lithos.task_complete

    async def _create_spy(**kwargs: Any) -> Any:
        order.append(f"create:{(kwargs.get('metadata') or {}).get('gate_type')}")
        return await create(**kwargs)

    async def _complete_spy(**kwargs: Any) -> Any:
        order.append(f"complete:{kwargs['task_id']}")
        return await complete(**kwargs)

    lithos.task_create = _create_spy  # type: ignore[method-assign]
    lithos.task_complete = _complete_spy  # type: ignore[method-assign]

    assert _invoke(_RUN).exit_code == 0
    assert order == [f"create:{GATE_TYPE_PR}", "complete:gate-human"]


def test_second_invocation_adopts_the_pr_and_changes_nothing(
    host, lithos: FakeLithosClient, run_dir: Path, repo: Path, gh: dict
) -> None:
    assert _invoke(_RUN).exit_code == 0
    gh["existing"] = [_open_pr(head_sha=_head(repo))]
    before = len(lithos.calls)
    findings_before = len(lithos.findings)

    result = _invoke(_RUN)

    assert result.exit_code == 0, result.output
    assert len(gh["created"]) == 1  # no second PR
    assert "adopted PR #99" in result.output
    # no second pr gate, no second finding, no write at all
    after = [c.method for c in lithos.calls[before:]]
    assert not [m for m in after if m in {"task_create", "task_update", "finding_post"}]
    assert len(lithos.findings) == findings_before


# ── step 1: the four push cases (real git) ─────────────────────────────


def test_push_is_a_no_op_when_origin_already_has_the_branch(
    host, lithos: FakeLithosClient, run_dir: Path, repo: Path, gh: dict
) -> None:
    _git(repo, "push", "origin", _BRANCH)
    sha = _git(repo, "rev-parse", _BRANCH)

    result = _invoke(_RUN)

    assert result.exit_code == 0, result.output
    assert "already up to date" in result.output
    assert _git(repo, "ls-remote", "origin", f"refs/heads/{_BRANCH}").split()[0] == sha


def test_push_fast_forwards_a_behind_remote(
    host, lithos: FakeLithosClient, run_dir: Path, repo: Path, gh: dict
) -> None:
    _git(repo, "push", "origin", _BRANCH)
    (repo / "feature.py").write_text("x = 2\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "round 2")
    head = _git(repo, "rev-parse", _BRANCH)

    result = _invoke(_RUN)

    assert result.exit_code == 0, result.output
    assert _git(repo, "ls-remote", "origin", f"refs/heads/{_BRANCH}").split()[0] == head


def test_a_diverged_remote_is_refused_and_nothing_is_written(
    host, lithos: FakeLithosClient, run_dir: Path, repo: Path, gh: dict
) -> None:
    """Append-only: the remote carries a commit the local branch does not, so
    pushing would need a force. Refuse, naming both shas."""
    _git(repo, "push", "origin", _BRANCH)
    other = _git(repo, "rev-parse", _BRANCH)
    (repo / "theirs.py").write_text("y = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "someone else")
    _git(repo, "push", "origin", _BRANCH)
    remote_head = _git(repo, "rev-parse", _BRANCH)
    _git(repo, "reset", "--hard", other)  # local is now behind-and-different

    (repo / "ours.py").write_text("z = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "our round 2")
    local_head = _git(repo, "rev-parse", _BRANCH)

    result = _invoke(_RUN)

    assert result.exit_code == 1, result.output
    assert "diverged" in result.output
    assert remote_head[:12] in result.output and local_head[:12] in result.output
    # nothing written: the remote is untouched, no gh call, no Lithos write
    assert _git(repo, "ls-remote", "origin", f"refs/heads/{_BRANCH}").split()[0] == (
        remote_head
    )
    assert gh["created"] == []
    # nothing but the claim it took and gave straight back
    assert lithos.mutating_calls == ["task_claim", "task_release"]


# ── refusals + flags ───────────────────────────────────────────────────


def test_dry_run_prints_the_plan_and_writes_nothing(
    host, lithos: FakeLithosClient, run_dir: Path, repo: Path, gh: dict
) -> None:
    result = _invoke(_RUN, "--dry-run")

    assert result.exit_code == 0, result.output
    assert "dry run, nothing written" in result.output
    assert "gate-human" in result.output  # the gate it would complete
    assert _git(repo, "ls-remote", "origin", f"refs/heads/{_BRANCH}") == ""
    assert lithos.mutating_calls == []
    assert gh["created"] == []
    assert gh["listed"] == []  # no gh call at all


def test_no_gate_opens_the_pr_and_leaves_the_human_gate_alone(
    host, lithos: FakeLithosClient, run_dir: Path, repo: Path, gh: dict, tmp_path: Path
) -> None:
    out = tmp_path / "r.json"
    result = _invoke(_RUN, "--no-gate", "--json", str(out))

    assert result.exit_code == 0, result.output
    assert len(gh["created"]) == 1
    record = json.loads(out.read_text())
    assert record["pr_gate_id"] is None
    assert (_get(lithos, "gate-human")).status == "open"
    assert not lithos.calls_to("task_create")
    assert "UNMONITORED" in lithos.findings[0]["summary"]


def test_no_gate_is_a_choice_not_friction_and_repeats_silently(
    host, lithos: FakeLithosClient, run_dir: Path, repo: Path, gh: dict, tmp_path: Path
) -> None:
    out = tmp_path / "r.json"
    assert _invoke(_RUN, "--no-gate").exit_code == 0
    gh["existing"] = [_open_pr(head_sha=_head(repo))]
    findings_before = len(lithos.findings)

    result = _invoke(_RUN, "--no-gate", "--json", str(out))

    assert result.exit_code == 0, result.output
    record = json.loads(out.read_text())
    assert record["notes"] == []  # the hand-off is deliberate, not degraded
    assert record["changed"] is False
    assert len(lithos.findings) == findings_before


def test_a_terminal_story_is_refused_unless_no_gate(
    host, lithos: FakeLithosClient, run_dir: Path, repo: Path, gh: dict
) -> None:
    """A completed story takes no pr gate (the #372 shape): gating a done story
    would strand it behind a blocker nothing will resolve."""
    asyncio.run(lithos.task_complete(task_id=_STORY, agent="op"))

    refused = _invoke(_RUN)
    assert refused.exit_code == 1
    assert "--no-gate" in refused.output
    assert gh["created"] == []

    allowed = _invoke(_RUN, "--no-gate")
    assert allowed.exit_code == 0, allowed.output
    assert len(gh["created"]) == 1


def test_a_failed_pr_gate_exits_2_keeps_the_human_gate_and_names_the_pr(
    host, lithos: FakeLithosClient, run_dir: Path, repo: Path, gh: dict
) -> None:
    """The PR is open, so its url must survive: the gate failure degrades to a
    [Friction] and a partial-delivery exit code, never a lost PR."""
    lithos.raise_on["task_create"] = LithosClientError("boom", "gate create failed")

    result = _invoke(_RUN)

    assert result.exit_code == 2, result.output
    assert _PR_URL in result.output
    assert "[Friction]" in result.output
    # the human gate stays: nothing else blocks the story
    assert (_get(lithos, "gate-human")).status == "open"
    summary = lithos.findings[-1]["summary"]
    assert summary.startswith(cli.MANUAL_DELIVERY) and "[Friction]" in summary


# ── round-2 regressions: partial deliveries, wrong PRs, races ──────────


def test_a_retry_repairs_the_story_write_the_first_pass_lost(
    host, lithos: FakeLithosClient, run_dir: Path, repo: Path, gh: dict
) -> None:
    """correctness/f-001: gate created, metadata write lost. The retry adopts
    the gate — and must still write `pr_gate_id` + the retirements, or the
    story stays half-delivered for ever."""
    lithos.raise_on["task_update"] = LithosClientError("boom", "write lost")
    assert _invoke(_RUN).exit_code == 2
    story = _get(lithos, _STORY)
    assert STORY_GATE_ID_KEY not in story.metadata  # the write really was lost

    lithos.raise_on.pop("task_update")
    gh["existing"] = [_open_pr(head_sha=_head(repo))]
    result = _invoke(_RUN)

    assert result.exit_code == 0, result.output
    story = _get(lithos, _STORY)
    gates = [e.from_task_id for e in _edges_into(lithos, _STORY)]
    assert story.metadata[STORY_GATE_ID_KEY] in gates
    assert STORY_HUMAN_GATE_ID_KEY not in story.metadata
    assert "loom_last_attempt:story-develop" not in story.metadata
    # and still exactly ONE pr gate
    pr_gates = [g for g in gates if _get(lithos, g).metadata.get("gate_type") == "pr"]
    assert len(pr_gates) == 1


def test_a_gate_failure_after_the_pr_opens_exits_2_and_keeps_the_pr_url(
    host,
    lithos: FakeLithosClient,
    run_dir: Path,
    repo: Path,
    gh: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """correctness/f-002: an unreachable Lithos AFTER the PR is open is a
    partial delivery, not a refusal — the url and the [Friction] must survive."""

    def _unreachable(*a: Any, **k: Any):
        raise cli_lithos.DeliverRefused("Lithos call failed: connection refused")

    monkeypatch.setattr(cli, "run_gate_delivery", _unreachable)

    result = _invoke(_RUN)

    assert result.exit_code == 2, result.output
    assert _PR_URL in result.output
    assert "[Friction]" in result.output
    assert "NOT gated" in result.output
    assert len(gh["created"]) == 1
    # the story still learns about the PR, with the friction folded in
    summary = lithos.findings[-1]["summary"]
    assert summary.startswith(cli.MANUAL_DELIVERY) and _PR_URL in summary
    assert "[Friction]" in summary
    assert (_get(lithos, "gate-human")).status == "open"


def test_a_missing_finding_is_re_posted_on_the_next_run(
    host, lithos: FakeLithosClient, run_dir: Path, repo: Path, gh: dict
) -> None:
    """correctness/f-003: the finding is one-shot via a marker on the GATE, so
    a delivery whose post failed re-posts instead of computing 'unchanged'."""
    lithos.raise_on["finding_post"] = LithosClientError("boom", "post failed")
    assert _invoke(_RUN).exit_code == 2
    assert lithos.findings == []

    lithos.raise_on.pop("finding_post")
    gh["existing"] = [_open_pr(head_sha=_head(repo))]
    assert _invoke(_RUN).exit_code == 0

    summaries = [f["summary"] for f in lithos.findings]
    assert len(summaries) == 1 and summaries[0].startswith(cli.MANUAL_DELIVERY)

    # …and a third run, with the marker in place, posts nothing more
    assert _invoke(_RUN).exit_code == 0
    assert len(lithos.findings) == 1


def test_a_pr_gate_watching_another_pr_is_never_adopted(
    host, lithos: FakeLithosClient, run_dir: Path, repo: Path, gh: dict
) -> None:
    """correctness/f-005: adopting a gate just because it blocks this story
    would point merge tracking at someone else's PR and retire the escalation
    anyway. Refuse, keep the human gate."""
    other = "https://github.com/agent-lore/lithos-loom/pull/42"
    lithos.add_task(
        make_task(
            "gate-pr-other",
            title="Awaiting merge: something else",
            task_type="gate",
            metadata={
                "gate_type": GATE_TYPE_PR,
                "repo": "agent-lore/lithos-loom",
                "pr_number": 42,
                "pr_url": other,
                "required_state": "merged",
            },
        )
    )
    lithos.add_edge(from_task_id="gate-pr-other", to_task_id=_STORY, type=WAITS_ON_GATE)

    result = _invoke(_RUN)

    assert result.exit_code == 2, result.output
    assert other in result.output and _PR_URL in result.output
    assert (_get(lithos, "gate-human")).status == "open"  # escalation kept
    assert (_get(lithos, "gate-pr-other")).status == "open"
    assert not lithos.calls_to("task_create")  # no second pr gate


def test_a_gate_raised_between_the_read_and_the_pr_is_adopted_not_duplicated(
    host, lithos: FakeLithosClient, run_dir: Path, repo: Path, gh: dict
) -> None:
    """correctness/f-006: the gate decision is made on a FRESH read, so a gate
    that landed while this command was pushing is adopted."""
    real_get = lithos.task_get
    injected = {"done": False}

    async def _inject_after_the_first_story_read(**kwargs: Any) -> Any:
        task = await real_get(**kwargs)
        if kwargs.get("task_id") == _STORY and not injected["done"]:
            injected["done"] = True
            lithos.add_task(
                make_task(
                    "gate-pr-racer",
                    title="Awaiting merge: raced",
                    task_type="gate",
                    metadata={
                        "gate_type": GATE_TYPE_PR,
                        "repo": "agent-lore/lithos-loom",
                        "pr_number": 99,
                        "pr_url": _PR_URL,
                        "required_state": "merged",
                    },
                )
            )
            lithos.add_edge(
                from_task_id="gate-pr-racer", to_task_id=_STORY, type=WAITS_ON_GATE
            )
        return task

    lithos.task_get = _inject_after_the_first_story_read  # type: ignore[method-assign]

    result = _invoke(_RUN)

    assert result.exit_code == 0, result.output
    assert not lithos.calls_to("task_create")  # the raced gate was adopted
    assert _get(lithos, _STORY).metadata[STORY_GATE_ID_KEY] == "gate-pr-racer"


def test_a_concurrent_delivery_holding_the_claim_is_refused(
    host, lithos: FakeLithosClient, run_dir: Path, repo: Path, gh: dict
) -> None:
    """correctness/f-006: the cross-process guard. A claim held by another
    process stops this one before it pushes."""
    lithos.raise_on["task_claim"] = LithosClientError("claim_failed", "held")

    result = _invoke(_RUN)

    assert result.exit_code == 1, result.output
    assert "claim" in result.output
    assert gh["created"] == []
    assert _git(repo, "ls-remote", "origin", f"refs/heads/{_BRANCH}") == ""


def test_a_branch_named_like_a_git_option_is_pushed_not_parsed(
    host, lithos: FakeLithosClient, repo: Path, gh: dict
) -> None:
    """security/f-004: git accepts refs beginning with `-`, and a bare
    positional `--receive-pack=<path>` would be read as an OPTION naming a
    program to execute. The push travels as a fully-qualified refspec."""
    hostile = "--receive-pack=/tmp/pwn"
    _git(repo, "update-ref", f"refs/heads/{hostile}", "HEAD")

    result = _invoke(f"--branch={hostile}", "--story", _STORY)

    assert result.exit_code == 0, result.output
    assert _git(repo, "ls-remote", "origin", f"refs/heads/{hostile}") != ""


def test_a_fork_pr_with_the_same_branch_name_is_never_adopted(
    host, lithos: FakeLithosClient, run_dir: Path, repo: Path, gh: dict
) -> None:
    """security/f-001: `gh pr list --head` matches on the branch NAME, so a
    fork PR named for our branch looks identical. Adopting it would gate the
    story on a third party's work."""
    gh["existing"] = [
        _open_pr(
            number=1234,
            url="https://github.com/agent-lore/lithos-loom/pull/1234",
            head_sha=_head(repo),
            cross_repository=True,
            head_owner="attacker",
        )
    ]

    result = _invoke(_RUN)

    assert result.exit_code == 1, result.output
    assert "1234" in result.output and "attacker" in result.output
    assert gh["created"] == []  # nothing opened either — the state is ambiguous
    assert (_get(lithos, "gate-human")).status == "open"
    assert not lithos.calls_to("task_create")


def test_a_same_name_pr_on_another_head_is_never_adopted(
    host, lithos: FakeLithosClient, run_dir: Path, repo: Path, gh: dict
) -> None:
    """security/f-001, the head-sha half: same repo, but not our commit."""
    gh["existing"] = [_open_pr(number=7, head_sha="f" * 40)]

    result = _invoke(_RUN)

    assert result.exit_code == 1, result.output
    assert "#7" in result.output
    assert gh["created"] == []


def test_every_gh_call_is_pinned_to_the_origin_repository(
    host, lithos: FakeLithosClient, run_dir: Path, repo: Path, gh: dict
) -> None:
    """security/f-002: gh must not infer the target from the checkout (for a
    fork checkout its default is the parent)."""
    assert _invoke(_RUN).exit_code == 0
    assert gh["listed"] == [{"branch": _BRANCH, "repo_name": _REPO_NAME}]
    assert gh["created"][0]["repo_name"] == _REPO_NAME


def test_the_pr_body_never_carries_the_raw_failure_reason(
    host, lithos: FakeLithosClient, run_dir: Path, repo: Path, gh: dict
) -> None:
    """security/f-003: `failure_reason` is raw agent/infra text (host paths,
    endpoints, auth payloads). The PR is world-readable; the classification
    goes in, the raw line stays on the story."""
    assert _invoke(_RUN).exit_code == 0
    body = gh["created"][0]["body"]
    assert "deadlocked on the AC" not in body
    assert "`disputed`" in body  # the classification still travels


def test_the_pr_body_carries_the_coders_final_handoff_summary(
    host, lithos: FakeLithosClient, run_dir: Path, repo: Path, gh: dict
) -> None:
    """correctness/f-004: the author's own account of the branch."""
    (run_dir / "handoff" / "round_01_coder_done.md").write_text(
        "## Status: LGTM\n\n## Summary\nFirst round.\n", encoding="utf-8"
    )
    (run_dir / "handoff" / "round_04_coder_done.md").write_text(
        "## Status: LGTM\n\n## Summary\nWired the \x1b[31mgate\x1b[0m end to "
        "end.\n\n## Findings\n- none\n",
        encoding="utf-8",
    )

    assert _invoke(_RUN).exit_code == 0
    body = gh["created"][0]["body"]
    assert "Wired the" in body and "end to end." in body  # the last round's
    assert "\x1b" not in body  # terminal control bytes stripped
    assert "First round." not in body
    assert "- none" not in body  # only the Summary section


def test_unknown_rounds_and_cost_are_not_asserted_as_zero(
    host, lithos: FakeLithosClient, repo: Path, gh: dict
) -> None:
    """correctness/f-004: a run dir that was reaped records neither."""
    assert _invoke("--branch", _BRANCH, "--story", _STORY).exit_code == 0
    body = gh["created"][0]["body"]
    assert "- rounds: unknown" in body
    assert "- agent cost: unknown" in body
    assert "$0.00" not in body


def test_the_finding_names_the_delivered_sha_even_when_nothing_was_pushed(
    host, lithos: FakeLithosClient, run_dir: Path, repo: Path, gh: dict
) -> None:
    """correctness/f-007: an audit that cannot be checked later is no audit."""
    _git(repo, "push", "origin", _BRANCH)
    head = _head(repo)

    assert _invoke(_RUN).exit_code == 0

    summary = lithos.findings[-1]["summary"]
    assert head[:12] in summary
    assert "pushed" not in summary.split("[Friction]")[0]


def test_an_unmapped_project_is_refused_before_any_git_work(
    host, lithos: FakeLithosClient, run_dir: Path, gh: dict
) -> None:
    host.projects.clear()
    result = _invoke(_RUN)
    assert result.exit_code == 1
    assert "not mapped" in result.output
    assert lithos.mutating_calls == []


def test_an_unknown_run_is_refused(host, lithos: FakeLithosClient, gh: dict) -> None:
    result = _invoke("nope")
    assert result.exit_code == 1
    assert "no run state" in result.output


def test_a_bare_branch_and_story_deliver_without_a_run_dir(
    host, lithos: FakeLithosClient, repo: Path, gh: dict
) -> None:
    """`retain_failed_workdirs = false`: the operator names the branch itself."""
    result = _invoke("--branch", _BRANCH, "--story", _STORY)

    assert result.exit_code == 0, result.output
    assert len(gh["created"]) == 1
    assert (_get(lithos, "gate-human")).status == "completed"


def test_naming_neither_a_run_nor_a_branch_is_refused(
    host, lithos: FakeLithosClient, gh: dict
) -> None:
    result = _invoke()
    assert result.exit_code == 1
    assert "--branch" in result.output


def test_an_already_delivered_run_is_refused_with_its_pr(
    host, lithos: FakeLithosClient, run_dir: Path, repo: Path, gh: dict
) -> None:
    (run_dir.parent / "result.json").write_text(
        json.dumps({"run_id": _RUN, "status": "succeeded", "pr_url": _PR_URL}),
        encoding="utf-8",
    )
    result = _invoke(_RUN)
    assert result.exit_code == 1
    assert _PR_URL in result.output
    assert gh["created"] == []


# ── pure helpers ───────────────────────────────────────────────────────


def test_origin_repo_name_reads_the_checkouts_origin(repo: Path) -> None:
    """security/f-002: the pin comes from `origin` — the remote step 1 pushes
    to — never from gh's inference."""
    _git(
        repo, "remote", "set-url", "origin", "git@github.com:agent-lore/lithos-loom.git"
    )
    assert cli_repo.origin_repo_name(repo) == "agent-lore/lithos-loom"


def test_origin_repo_name_refuses_a_non_github_origin(repo: Path) -> None:
    with pytest.raises(cli_lithos.DeliverRefused, match="not a GitHub"):
        cli_repo.origin_repo_name(repo)  # the fixture's origin is a local path


def test_adoptable_takes_only_our_own_head(tmp_path: Path) -> None:
    ours = _open_pr(head_sha="a" * 40)
    fork = _open_pr(number=2, head_sha="a" * 40, cross_repository=True)
    stale = _open_pr(number=3, head_sha="b" * 40)
    assert cli_repo.adoptable([ours], head_sha="a" * 40, base=None) == (ours, "")
    assert cli_repo.adoptable([fork, ours], head_sha="a" * 40, base=None) == (ours, "")
    assert cli_repo.adoptable([], head_sha="a" * 40, base=None) == (None, "")
    pr, reason = cli_repo.adoptable([fork, stale], head_sha="a" * 40, base=None)
    assert pr is None and "#2" in reason and "#3" in reason
    # a PR onto another base is not this delivery either
    other_base = _open_pr(head_sha="a" * 40, base_ref="release")
    pr, reason = cli_repo.adoptable([other_base], head_sha="a" * 40, base="main")
    assert pr is None and "release" in reason


def test_coder_summary_is_bounded_and_control_stripped(tmp_path: Path) -> None:
    handoff = tmp_path / "handoff"
    handoff.mkdir()
    (handoff / "round_02_coder_done.md").write_text(
        "## Status: LGTM\n\n## Summary\n" + "x" * 5000 + "\n", encoding="utf-8"
    )
    summary = cli.coder_summary(handoff)
    assert len(summary) <= 600 and summary.endswith("…")
    assert cli.coder_summary(tmp_path / "nope") == ""


def test_run_facts_reads_state_and_the_escalation_brief(run_dir: Path) -> None:
    facts = cli.run_facts(run_dir)
    assert facts.story_id == _STORY
    assert facts.run_id == _RUN
    assert facts.branch == _BRANCH
    assert facts.status == "disputed"
    assert facts.rounds == 4
    assert facts.cost_usd == 33.57
    assert facts.test_gate_verdict == "GREEN"
    assert facts.delivered_pr_url is None


def test_run_facts_tolerates_an_empty_run_dir(tmp_path: Path) -> None:
    d = tmp_path / "t-1" / "r-1"
    (d / "handoff").mkdir(parents=True)
    facts = cli.run_facts(d)
    assert facts == cli.RunFacts(
        story_id="t-1", branch="", run_id="r-1", run_dir=str(d)
    )


def test_provenance_names_the_run_and_why_it_stopped(run_dir: Path) -> None:
    lines = cli.provenance_lines(cli.run_facts(run_dir))
    assert any("develop deliver" in line for line in lines)
    assert any(_RUN in line and "disputed" in line for line in lines)
    assert any(_BRANCH in line for line in lines)
