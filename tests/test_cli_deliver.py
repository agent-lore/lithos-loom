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
import os
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from lithos_loom.cli import _deliver_facts as cli_facts
from lithos_loom.cli import _deliver_lithos as cli_lithos
from lithos_loom.cli import _deliver_repo as cli_repo
from lithos_loom.cli import _deliver_session as cli_session
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
from lithos_loom.runner import pidfile
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
    # every Lithos phase runs through one short-lived session there
    monkeypatch.setattr(cli_session, "LithosClient", lambda *a, **k: client)
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
        # the allowlist a delivery retires gates under: this host's own routes
        routes=(SimpleNamespace(name="story-develop"),),
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
    # the delivery's own leases (its `deliver` claim and the dispatch hold)
    # and nothing else — no push, no gate, no write on the story
    assert set(lithos.mutating_calls) == {"task_claim", "task_release"}


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
    real_status = lithos.task_status
    injected = {"done": False}

    async def _inject_after_the_first_story_read(**kwargs: Any) -> Any:
        task = await real_status(**kwargs)
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

    lithos.task_status = _inject_after_the_first_story_read  # type: ignore[method-assign]

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


def test_a_pr_failure_after_the_push_is_a_partial_not_nothing_written(
    host,
    lithos: FakeLithosClient,
    run_dir: Path,
    repo: Path,
    gh: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """correctness/f-002: the push is externally committed, so a gh failure
    after it must not be reported as a refusal that wrote nothing."""

    def _boom(*a: Any, **k: Any):
        raise RuntimeError("gh pr list failed: network is down")

    monkeypatch.setattr(cli_repo, "list_open_prs_for_branch", _boom)

    result = _invoke(_RUN)

    assert result.exit_code == 2, result.output
    assert "no PR was opened" in result.output
    assert "nothing written" not in result.output
    # …and the push really did land, so a re-run inherits it
    assert _git(repo, "ls-remote", "origin", f"refs/heads/{_BRANCH}") != ""
    assert (_get(lithos, "gate-human")).status == "open"


def test_a_gh_failure_with_nothing_pushed_is_still_a_plain_refusal(
    host,
    lithos: FakeLithosClient,
    run_dir: Path,
    repo: Path,
    gh: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """correctness/f-002, the other side: when the remote ref was already
    equal this run wrote nothing, so exit 1 is the honest answer."""
    _git(repo, "push", "origin", _BRANCH)

    def _boom(*a: Any, **k: Any):
        raise RuntimeError("gh pr list failed: network is down")

    monkeypatch.setattr(cli_repo, "list_open_prs_for_branch", _boom)

    result = _invoke(_RUN)

    assert result.exit_code == 1, result.output
    # the delivery's own leases (its `deliver` claim and the dispatch hold)
    # and nothing else — no push, no gate, no write on the story
    assert set(lithos.mutating_calls) == {"task_claim", "task_release"}


def test_an_unwritable_json_record_is_a_partial_delivery(
    host, lithos: FakeLithosClient, run_dir: Path, repo: Path, gh: dict
) -> None:
    """correctness/f-002: the operator asked for a record and did not get it;
    the delivery stands but the run is not complete."""
    result = _invoke(_RUN, "--json", str(run_dir / "handoff"))  # a directory

    assert result.exit_code == 2, result.output
    assert "could not write the JSON record" in result.output
    assert _PR_URL in result.output  # the delivery itself stands


def test_no_gate_re_posts_a_lost_finding_and_then_stays_silent(
    host, lithos: FakeLithosClient, run_dir: Path, repo: Path, gh: dict
) -> None:
    """correctness/f-003: --no-gate raises no gate, so the durable marker lives
    on the STORY — otherwise a lost finding could never be recovered."""
    lithos.raise_on["finding_post"] = LithosClientError("boom", "post failed")
    assert _invoke(_RUN, "--no-gate").exit_code == 2
    assert lithos.findings == []

    lithos.raise_on.pop("finding_post")
    gh["existing"] = [_open_pr(head_sha=_head(repo))]
    assert _invoke(_RUN, "--no-gate").exit_code == 0
    assert len(lithos.findings) == 1

    assert _invoke(_RUN, "--no-gate").exit_code == 0
    assert len(lithos.findings) == 1  # marked: nothing more to say


def test_the_delivery_marker_survives_the_gate_being_completed(
    host, lithos: FakeLithosClient, run_dir: Path, repo: Path, gh: dict
) -> None:
    """correctness/f-003: the marker is on the story, so a gate the merge
    sweep completes cannot take the provenance record with it."""
    assert _invoke(_RUN).exit_code == 0
    gate_id = _get(lithos, _STORY).metadata[STORY_GATE_ID_KEY]
    asyncio.run(lithos.task_complete(task_id=gate_id, agent="watcher"))

    gh["existing"] = [_open_pr(head_sha=_head(repo))]
    assert _invoke(_RUN, "--no-gate").exit_code == 0
    assert len(lithos.findings) == 1  # still one — the story remembers


def test_a_foreign_gate_beside_a_matching_one_still_refuses(
    host, lithos: FakeLithosClient, run_dir: Path, repo: Path, gh: dict
) -> None:
    """correctness/f-005: a MIXED state is not an idempotent re-run — the
    story's merge semantics would stay hostage to the other PR."""
    for gate_id, number, url in (
        ("gate-pr-ours", 99, _PR_URL),
        ("gate-pr-other", 42, "https://github.com/agent-lore/lithos-loom/pull/42"),
    ):
        lithos.add_task(
            make_task(
                gate_id,
                title=f"Awaiting merge: {number}",
                task_type="gate",
                metadata={
                    "gate_type": GATE_TYPE_PR,
                    "repo": "agent-lore/lithos-loom",
                    "pr_number": number,
                    "pr_url": url,
                    "required_state": "merged",
                },
            )
        )
        lithos.add_edge(from_task_id=gate_id, to_task_id=_STORY, type=WAITS_ON_GATE)

    result = _invoke(_RUN)

    assert result.exit_code == 2, result.output
    assert "gate-pr-other" in result.output
    assert (_get(lithos, "gate-human")).status == "open"  # escalation kept
    assert not lithos.calls_to("task_create")


def test_the_claim_is_renewed_before_the_gate_work(
    host, lithos: FakeLithosClient, run_dir: Path, repo: Path, gh: dict
) -> None:
    """correctness/f-006: the git + gh phases can legitimately spend minutes,
    so the gate decision starts on a fresh lease."""
    assert _invoke(_RUN).exit_code == 0

    order = [
        c.method
        for c in lithos.calls
        if c.method in {"task_claim", "task_renew", "task_create", "task_release"}
    ]
    assert order.index("task_renew") < order.index("task_create")
    claim = lithos.calls_to("task_claim")[0]
    assert claim["ttl_minutes"] == cli_lithos.DELIVER_CLAIM_TTL_MINUTES >= 60


def test_the_push_sends_the_commit_it_classified(
    host,
    lithos: FakeLithosClient,
    run_dir: Path,
    repo: Path,
    gh: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """correctness/f-009: a local process that moves the branch between the
    classification and the push must not change what is delivered."""
    classified = _head(repo)
    real_push = cli_repo.push_branch

    def _move_the_branch_then_push(repo_path: Path, branch: str, state: Any) -> None:
        (repo_path / "raced.py").write_text("late = 1\n", encoding="utf-8")
        _git(repo_path, "add", "-A")
        _git(repo_path, "commit", "-m", "a concurrent local commit")
        real_push(repo_path, branch, state)

    # `deliver` imports push_branch by name, so its own binding is the seam
    monkeypatch.setattr(cli, "push_branch", _move_the_branch_then_push)

    result = _invoke(_RUN)

    assert result.exit_code == 0, result.output
    remote = _git(repo, "ls-remote", "origin", f"refs/heads/{_BRANCH}").split()[0]
    assert remote == classified  # the measured object, not the moved ref
    assert classified[:12] in lithos.findings[-1]["summary"]


def test_the_handoff_summary_is_fenced_and_defanged(
    host, lithos: FakeLithosClient, run_dir: Path, repo: Path, gh: dict
) -> None:
    """security/f-006: the handoff is agent-written into a RW mount and a PR
    description is live markup — closing keywords close issues, @names ping."""
    (run_dir / "handoff" / "round_04_coder_done.md").write_text(
        "## Status: LGTM\n\n## Summary\nDone. Closes #1337 cc @evil-org/sec\n",
        encoding="utf-8",
    )

    assert _invoke(_RUN).exit_code == 0
    body = gh["created"][0]["body"]

    assert "```text" in body  # quoted, not spliced into live markup
    assert "Closes #1337" not in body  # the keyword no longer binds
    assert "#1337" in body  # …but the operator still sees what was said
    assert "@evil-org/sec" not in body  # the mention notifies nobody
    assert "&#64;evil-org/sec" in body


def test_a_symlinked_or_fifo_handoff_is_never_read(
    host,
    lithos: FakeLithosClient,
    run_dir: Path,
    repo: Path,
    gh: dict,
    tmp_path: Path,
) -> None:
    """security/f-007: the handoff dir is agent-writable, so it must not decide
    what a host-privileged process opens — and a FIFO must not hang it."""
    secret = tmp_path / "another-run.md"
    secret.write_text("## Summary\nSECRET-abc123\n", encoding="utf-8")
    (run_dir / "handoff" / "round_07_coder_done.md").symlink_to(secret)
    os.mkfifo(run_dir / "handoff" / "round_08_coder_done.md")

    result = _invoke(_RUN)  # must not hang, must not read the link target

    assert result.exit_code == 0, result.output
    assert "SECRET-abc123" not in gh["created"][0]["body"]


# ── round-4 regressions ────────────────────────────────────────────────


def test_a_push_that_committed_but_reported_failure_is_not_a_refusal(
    host,
    lithos: FakeLithosClient,
    run_dir: Path,
    repo: Path,
    gh: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """correctness/f-002: the remote applied the update and the response was
    lost. Asking the remote what it holds is the only honest classifier."""
    real_git = cli_repo.run_git

    def _push_then_lose_the_response(repo_path: Path, args: list[str], **kw: Any):
        proc = real_git(repo_path, args, **kw)
        if args[:1] == ["push"]:  # it landed; only the answer was lost
            return subprocess.CompletedProcess(args, 1, "", "fatal: the remote hung up")
        return proc

    monkeypatch.setattr(cli_repo, "run_git", _push_then_lose_the_response)

    result = _invoke(_RUN)

    assert result.exit_code == 0, result.output
    assert _git(repo, "ls-remote", "origin", f"refs/heads/{_BRANCH}") != ""
    assert len(gh["created"]) == 1  # the delivery carried on


def test_a_push_that_did_not_land_is_still_a_refusal(
    host,
    lithos: FakeLithosClient,
    run_dir: Path,
    repo: Path,
    gh: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """correctness/f-002, the other side: the remote does NOT hold our sha, so
    "nothing was written" is true and exit 1 is right."""

    real_git = cli_repo.run_git

    def _refuse_to_push(repo_path: Path, args: list[str], **kw: Any):
        if args[:1] == ["push"]:
            return subprocess.CompletedProcess(args, 1, "", "fatal: permission denied")
        return real_git(repo_path, args, **kw)

    monkeypatch.setattr(cli_repo, "run_git", _refuse_to_push)

    result = _invoke(_RUN)

    assert result.exit_code == 1, result.output
    assert "permission denied" in result.output
    assert gh["created"] == []


def test_a_pushed_delivery_with_no_pr_never_claims_one(
    host,
    lithos: FakeLithosClient,
    run_dir: Path,
    repo: Path,
    gh: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """correctness/f-002: the render must not say "opened the PR" over a
    record whose own note says no PR was opened."""

    def _boom(*a: Any, **k: Any):
        raise RuntimeError("gh pr list failed: network is down")

    monkeypatch.setattr(cli_repo, "list_open_prs_for_branch", _boom)

    result = _invoke(_RUN)

    assert result.exit_code == 2, result.output
    headline = result.output.splitlines()[0]
    assert "PUSHED, NO PR" in headline
    # …and no line asserting a PR that does not exist
    assert not [ln for ln in result.output.splitlines() if ln.startswith("  opened")]
    assert not [ln for ln in result.output.splitlines() if ln.startswith("  adopted")]


def test_the_create_path_sets_the_branch_upstream(
    host, lithos: FakeLithosClient, run_dir: Path, repo: Path, gh: dict
) -> None:
    """correctness/f-013: the pinned-object refspec cannot carry `push -u`'s
    meaning, so the tracking config is written explicitly."""
    assert _invoke(_RUN).exit_code == 0
    assert _git(repo, "rev-parse", "--abbrev-ref", f"{_BRANCH}@{{upstream}}") == (
        f"origin/{_BRANCH}"
    )


def test_a_repair_pass_does_not_duplicate_a_recorded_finding(
    host, lithos: FakeLithosClient, run_dir: Path, repo: Path, gh: dict
) -> None:
    """correctness/f-003: the marker is the SOLE gate on the finding — a pass
    that repairs story state must not manufacture a second copy of a record
    the story already carries."""
    assert _invoke(_RUN).exit_code == 0
    assert len(lithos.findings) == 1

    # a lost story write: the repair pass will re-record it (changed=True)…
    asyncio.run(
        lithos.task_update(
            task_id=_STORY, agent="op", metadata={STORY_GATE_ID_KEY: None}
        )
    )
    gh["existing"] = [_open_pr(head_sha=_head(repo))]

    assert _invoke(_RUN).exit_code == 0
    story = _get(lithos, _STORY)
    assert story.metadata[STORY_GATE_ID_KEY]  # …repaired…
    assert len(lithos.findings) == 1  # …and silent


def test_a_partial_pass_stays_re_postable_until_it_completes(
    host, lithos: FakeLithosClient, run_dir: Path, repo: Path, gh: dict
) -> None:
    """correctness/f-003: the marker is written only for a delivery that
    FINISHED, so the corrected record lands exactly once."""
    lithos.raise_on["task_create"] = LithosClientError("boom", "gate create failed")
    assert _invoke(_RUN).exit_code == 2
    assert len(lithos.findings) == 1
    assert "[Friction]" in lithos.findings[0]["summary"]
    assert "merge-tracking gate" in lithos.findings[0]["summary"]

    lithos.raise_on.pop("task_create")
    gh["existing"] = [_open_pr(head_sha=_head(repo))]
    assert _invoke(_RUN).exit_code == 0
    assert len(lithos.findings) == 2  # the corrected record

    assert _invoke(_RUN).exit_code == 0
    assert len(lithos.findings) == 2  # …and nothing more, ever


def test_a_failed_renewal_skips_every_gate_mutation(
    host,
    lithos: FakeLithosClient,
    run_dir: Path,
    repo: Path,
    gh: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """correctness/f-006: a lease that will not renew may already belong to
    another delivery — so nothing is mutated and nothing is released."""
    monkeypatch.setattr(cli, "renew_story", lambda *a, **k: False)

    result = _invoke(_RUN)

    assert result.exit_code == 2, result.output
    assert "SKIPPED" in result.output
    assert not lithos.calls_to("task_create")  # no gate
    assert (_get(lithos, "gate-human")).status == "open"  # escalation kept
    # the `deliver` lease is not ours to hand back (the dispatch hold, which
    # this process demonstrably still owns, is released as usual)
    assert not [
        call
        for call in lithos.calls_to("task_release")
        if call["aspect"] == cli_lithos.DELIVER_ASPECT
    ]
    assert _PR_URL in result.output  # the PR stands


def test_a_story_that_goes_terminal_mid_delivery_gets_no_gate(
    host,
    lithos: FakeLithosClient,
    run_dir: Path,
    repo: Path,
    gh: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """correctness/f-012: the #372 shape holds on the LIVE read too — the
    initial one predates minutes of push and GitHub work."""
    real_push = cli.push_branch

    def _complete_the_story_then_push(repo_path: Path, branch: str, state: Any) -> None:
        asyncio.run(lithos.task_complete(task_id=_STORY, agent="operator"))
        real_push(repo_path, branch, state)

    monkeypatch.setattr(cli, "push_branch", _complete_the_story_then_push)

    result = _invoke(_RUN)

    assert result.exit_code == 2, result.output
    assert "became completed" in result.output
    assert not lithos.calls_to("task_create")
    assert (_get(lithos, "gate-human")).status == "open"


def test_an_approved_run_mid_delivery_is_refused(
    host, lithos: FakeLithosClient, run_dir: Path, repo: Path, gh: dict
) -> None:
    """correctness/f-011: `state.json` says approved before the daemon pushes
    and opens its PR. Delivering by hand inside that window races it."""
    state = json.loads((run_dir / "state.json").read_text())
    state["status"] = "approved"
    (run_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")
    (run_dir / "delivery.json").write_text(
        json.dumps({"deadline": "2999-01-01T00:00:00+00:00"}), encoding="utf-8"
    )

    result = _invoke(_RUN)

    assert result.exit_code == 1, result.output
    assert "APPROVED" in result.output and "develop attach" in result.output
    assert gh["created"] == []
    assert _git(repo, "ls-remote", "origin", f"refs/heads/{_BRANCH}") == ""


def test_an_approved_run_whose_delivery_failed_is_salvageable(
    host, lithos: FakeLithosClient, run_dir: Path, repo: Path, gh: dict
) -> None:
    """correctness/f-011: a positively recorded delivery failure (#194) is
    exactly the salvage this command exists for."""
    state = json.loads((run_dir / "state.json").read_text())
    state["status"] = "approved"
    (run_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")
    (run_dir / "delivery.json").write_text(
        json.dumps({"failed": True, "reason": "gh pr create failed"}), encoding="utf-8"
    )

    assert _invoke(_RUN).exit_code == 0
    assert len(gh["created"]) == 1


def test_an_approved_run_past_its_delivery_budget_is_salvageable(
    host, lithos: FakeLithosClient, run_dir: Path, repo: Path, gh: dict
) -> None:
    """correctness/f-011: an expired #189 deadline means the daemon's delivery
    is not coming back."""
    state = json.loads((run_dir / "state.json").read_text())
    state["status"] = "approved"
    (run_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")
    (run_dir / "delivery.json").write_text(
        json.dumps({"deadline": "2020-01-01T00:00:00+00:00"}), encoding="utf-8"
    )

    assert _invoke(_RUN).exit_code == 0
    assert len(gh["created"]) == 1


def _approved_salvage(run_dir: Path, *, approved_head: str | None) -> None:
    """Rewrite the fixture run as the #194 salvage: approved, delivery failed.

    *approved_head* is the revision ``result.json`` records the run as having
    ended on — the one the panel approved. ``None`` records none at all.
    """
    state = json.loads((run_dir / "state.json").read_text())
    state["status"] = "approved"
    # only the reason-bearing statuses set one: an approved dialogue did not fail
    state["failure_reason"] = None
    (run_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")
    (run_dir / "delivery.json").write_text(
        json.dumps({"failed": True, "reason": "gh pr create failed: api said 502"}),
        encoding="utf-8",
    )
    result = json.loads((run_dir.parent / "result.json").read_text())
    if approved_head is not None:
        result["commits"] = ["0" * 40, approved_head]
    (run_dir.parent / "result.json").write_text(json.dumps(result), encoding="utf-8")


def test_an_approved_salvage_records_its_approval_and_delivery_failure(
    host, lithos: FakeLithosClient, run_dir: Path, repo: Path, gh: dict
) -> None:
    """[davesnowdon] f-001: on the approved salvage path (#194 / #189) the panel
    DID approve, and what stopped is the run's own PR delivery — whose reason
    `state.json` never carries. "stopped `approved`", a verdict of "not
    recorded" and a promise of a full reason nothing carried over describe none
    of it."""
    _approved_salvage(run_dir, approved_head=_head(repo))

    plan = _invoke(_RUN, "--dry-run")
    assert plan.exit_code == 0, plan.output
    # the operator's own copy names the delivery failure, not an empty dash
    assert "approved — gh pr create failed: api said 502" in plan.output

    assert _invoke(_RUN).exit_code == 0
    body = gh["created"][0]["body"]

    # the recorded approval, rendered as the approval it was — bound to the
    # revision it was given on ([davesnowdon] f-004)
    assert "verdicts: approved — the review panel agreed on this exact revision" in body
    assert f"`{_head(repo)[:12]}`" in body
    assert "not recorded" not in body and "not approved" not in body
    assert "NOT confirmed" not in body
    # …and the delivery failure as the stop reason, not "stopped `approved`"
    assert "stopped `approved`" not in body
    assert "was approved by the review panel" in body
    assert "gh pr create failed: api said 502" in body
    assert "the story carries the full, unredacted reason" in body
    # the story's audit copy says the same
    findings = [f["summary"] for f in lithos.findings if f["task_id"] == _STORY]
    assert "the run was approved and its own PR delivery never completed" in findings[0]
    assert "had stopped approved" not in findings[0]


def test_an_approval_is_never_published_for_a_revision_the_panel_never_saw(
    host, lithos: FakeLithosClient, run_dir: Path, repo: Path, gh: dict
) -> None:
    """[davesnowdon] f-004: a branch is a MUTABLE ref, and this command pushes
    whatever it points at now. An approval recorded for one revision must not
    be published over a head the panel never saw — the PR would carry a review
    that never happened."""
    _approved_salvage(run_dir, approved_head=_head(repo))
    reviewed = _head(repo)
    # a commit lands on the branch after the panel approved it
    (repo / "sneaked.py").write_text("x = 2\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "after the review")

    # the screen the operator decides to publish on says it first
    plan = _invoke(_RUN, "--dry-run")
    assert plan.exit_code == 0, plan.output
    assert "approval NOT confirmed for this revision" in plan.output

    assert _invoke(_RUN).exit_code == 0
    body = gh["created"][0]["body"]

    assert "approved, but NOT confirmed for this revision" in body
    assert f"the branch has moved since the panel approved `{reviewed[:12]}`" in body
    assert f"`{_head(repo)[:12]}`" in body
    assert "the review panel agreed on this exact revision" not in body

    # …and the story's own audit copy does not read as a review of this head
    summary = [f["summary"] for f in lithos.findings if f["task_id"] == _STORY][0]
    assert "NOT confirmed for this revision" in summary


def test_an_approval_is_never_published_against_another_storys_criteria(
    host, lithos: FakeLithosClient, run_dir: Path, repo: Path, gh: dict
) -> None:
    """[davesnowdon] f-004, the other half: `--story` replaces the acceptance
    criteria the PR publishes, and a verdict is only about the criteria it was
    given on."""
    lithos.add_task(
        make_task(
            "story-other",
            title="A different story",
            description="Different work entirely.",
            metadata={
                "project": _SLUG,
                "acceptance_criteria": "Done when something else is true.",
            },
        )
    )
    _approved_salvage(run_dir, approved_head=_head(repo))

    assert _invoke(_RUN, "--story", "story-other").exit_code == 0
    body = gh["created"][0]["body"]

    assert "approved, but NOT confirmed for this revision" in body
    assert "come from a --story other than the one the run was reviewed" in body
    # the head itself still matches — only the criteria moved
    assert "the branch has moved" not in body


def test_an_approval_with_no_recorded_revision_is_downgraded(repo: Path) -> None:
    """[davesnowdon] f-004, the pure rule: nothing to compare against is not
    the same as a match. A run whose commits nothing recorded (a crash before
    `result.json`, an older daemon) binds its approval to no revision."""
    unrecorded = cli_facts.RunFacts(
        story_id=_STORY, branch=_BRANCH, run_id=_RUN, status="approved"
    )
    assert "no approved revision" in cli_facts.approval_unbound(
        unrecorded, delivered_head=_head(repo)
    )
    bound = cli_facts.RunFacts(
        story_id=_STORY,
        branch=_BRANCH,
        run_id=_RUN,
        status="approved",
        approved_head=_head(repo),
    )
    assert cli_facts.approval_unbound(bound, delivered_head=_head(repo)) == ""
    # an unreadable delivered head is not a match either
    assert "could not be read" in cli_facts.approval_unbound(bound, delivered_head="")
    assert "exact revision" in cli_facts.reviews_summary(
        bound, delivered_head=_head(repo).upper()
    )


def test_a_recorded_head_must_be_a_full_object_name(tmp_path: Path) -> None:
    """[davesnowdon] f-004: the delivered head is 40 hex characters read from
    git, so a short or malformed record must fail the comparison rather than
    half-match it."""
    d = tmp_path / "t-1" / "r-1"
    (d / "handoff").mkdir(parents=True)
    (d / "state.json").write_text(
        json.dumps({"status": "approved", "branch": "b"}), encoding="utf-8"
    )

    def _with_commits(commits: object) -> str:
        (d.parent / "result.json").write_text(
            json.dumps({"run_id": "r-1", "status": "failed", "commits": commits}),
            encoding="utf-8",
        )
        return cli_facts.run_facts(d).approved_head

    assert _with_commits(["a" * 40, "b" * 40]) == "b" * 40
    assert _with_commits(["b" * 12]) == ""  # abbreviated
    assert _with_commits([]) == ""
    assert _with_commits("deadbeef") == ""
    assert _with_commits([None]) == ""


def test_the_story_finding_carries_the_reason_the_pr_body_promises(
    host, lithos: FakeLithosClient, run_dir: Path, repo: Path, gh: dict
) -> None:
    """[davesnowdon] f-004, the Low half: the PR body tells its reader the
    story carries the full, unredacted reason — and the path this command
    exists for is the one where a daemon died before posting any of it. So
    [ManualDelivery] carries the reason itself."""
    state = json.loads((run_dir / "state.json").read_text())
    state["failure_reason"] = (
        "the reviewer died under /home/dave/loom/run.log with exit 137"
    )
    (run_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")
    # the daemon died before posting any escalation of its own: the story
    # carries no failed-attempt marker and no [NeedsHuman] finding
    story = _get(lithos, _STORY)
    metadata = {
        k: v
        for k, v in story.metadata.items()
        if k != "loom_last_attempt:story-develop"
    }
    lithos.add_task(
        make_task(
            _STORY,
            title=story.title,
            description=story.description,
            metadata=metadata,
        )
    )

    assert _invoke(_RUN).exit_code == 0

    body = gh["created"][0]["body"]
    assert "the story carries the full, unredacted reason" in body
    assert "(path redacted)" in body  # the PUBLISHED copy is redacted

    summary = [f["summary"] for f in lithos.findings if f["task_id"] == _STORY][0]
    assert summary.startswith("[ManualDelivery]")
    assert "the run had stopped disputed: " in summary
    # the story's copy is whole — paths and all
    assert "/home/dave/loom/run.log" in summary


def test_an_expired_delivery_budget_is_the_stop_reason(
    host, lithos: FakeLithosClient, run_dir: Path, repo: Path, gh: dict
) -> None:
    """[davesnowdon] f-001, the #189 half: nothing recorded a failure, so the
    budget the daemon never came back inside IS the reason."""
    state = json.loads((run_dir / "state.json").read_text())
    state["status"] = "approved"
    state["failure_reason"] = None
    (run_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")
    (run_dir / "delivery.json").write_text(
        json.dumps({"deadline": "2020-01-01T00:00:00+00:00"}), encoding="utf-8"
    )

    assert _invoke(_RUN).exit_code == 0
    body = gh["created"][0]["body"]
    assert "was approved by the review panel" in body
    assert "the automated delivery never reported inside its budget" in body


def test_a_reasonless_stop_never_promises_a_reason_on_the_story(
    tmp_path: Path,
) -> None:
    """[davesnowdon] f-001, the pure half: the parenthetical is a claim about
    the STORY, so a run whose reason nothing recorded must not make it."""
    facts = cli_facts.RunFacts(
        story_id=_STORY, branch=_BRANCH, run_id=_RUN, status="failed"
    )
    lines = cli_facts.provenance_lines(facts)
    assert any("stopped `failed`" in line for line in lines)
    assert not any("full, unredacted reason" in line for line in lines)
    # …and it still makes it when there IS a reason to redact
    with_reason = cli_facts.provenance_lines(
        cli_facts.RunFacts(
            story_id=_STORY,
            branch=_BRANCH,
            run_id=_RUN,
            status="failed",
            failure_reason="the coder died",
        )
    )
    assert any("full, unredacted reason" in line for line in with_reason)


def test_nonsense_rounds_and_cost_read_as_unknown(tmp_path: Path) -> None:
    """correctness/f-014: type-correct is not truthful. A negative round count
    or a NaN / negative spend cannot describe a run."""
    d = tmp_path / "t-1" / "r-1"
    (d / "handoff").mkdir(parents=True)
    (d / "state.json").write_text(
        json.dumps({"status": "failed", "branch": "b", "rounds": -3}), encoding="utf-8"
    )
    (d.parent / "result.json").write_text(
        '{"run_id": "r-1", "status": "failed", '
        '"escalation": {"brief": {"cost_usd": NaN}}}',
        encoding="utf-8",
    )
    facts = cli_facts.run_facts(d)
    assert facts.rounds is None and facts.cost_usd is None

    (d.parent / "result.json").write_text(
        '{"run_id": "r-1", "status": "failed", '
        '"escalation": {"brief": {"cost_usd": -4.0}}}',
        encoding="utf-8",
    )
    assert cli_facts.run_facts(d).cost_usd is None

    # an arbitrary-precision int: valid JSON, outside the float domain, and
    # `float()` RAISES on it rather than saturating (correctness/f-004)
    (d.parent / "result.json").write_text(
        '{"run_id": "r-1", "status": "failed", '
        '"escalation": {"brief": {"cost_usd": 1' + "0" * 400 + "}}}",
        encoding="utf-8",
    )
    assert cli_facts.run_facts(d).cost_usd is None


def test_protocol_relative_markup_never_renders_live(
    host, lithos: FakeLithosClient, run_dir: Path, repo: Path, gh: dict
) -> None:
    """security/f-009: on the unfenced provenance bullet `defang_markup` is the
    whole defence — inline HTML and scheme-less links must not survive it."""
    state = json.loads((run_dir / "state.json").read_text())
    state["failure_reason"] = (
        "died <img src=//evil.example/p.png> see [more](//evil.example/x) "
        "at proxy.internal:8443"
    )
    (run_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")

    assert _invoke(_RUN).exit_code == 0
    body = gh["created"][0]["body"]

    provenance = body.split("## Provenance")[1]
    assert "<img" not in provenance  # the tag no longer opens
    assert "&lt;img" in provenance  # …it reads as text
    assert "[more](" not in provenance  # the link no longer binds
    assert "&#91;more]" in provenance
    assert "evil.example" not in body  # the target redacted either way
    assert "proxy.internal" not in body and "(host redacted)" in body


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

    # the push already landed, so this is a PARTIAL delivery (exit 2), never a
    # "nothing written" refusal — but the PR is still not adopted
    assert result.exit_code == 2, result.output
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

    assert result.exit_code == 2, result.output  # the push landed first
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


def test_the_pr_body_carries_the_stop_reason_redacted(
    host, lithos: FakeLithosClient, run_dir: Path, repo: Path, gh: dict
) -> None:
    """correctness/f-010 + security/f-003 together: the provenance contract
    wants WHY the run stopped, and the raw line is agent/infra text (host
    paths, endpoints, credential-shaped runs). So the reason travels,
    redacted and bounded."""
    state = json.loads((run_dir / "state.json").read_text())
    state["failure_reason"] = (
        "coder died: auth 401 from https://proxy.internal/v1 reading "
        "/home/dns/.config/gh/hosts.yml with ghp_AbCdEfGhIjKlMnOpQrStUvWxYz012345"
    )
    (run_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")

    assert _invoke(_RUN).exit_code == 0
    body = gh["created"][0]["body"]

    assert "coder died: auth 401" in body  # the reason itself travels
    assert "`disputed`" in body  # and its classification
    assert "proxy.internal" not in body  # …but not the endpoint
    assert "/home/dns" not in body  # …nor the host path
    assert "ghp_AbCdEfGhIjKlMnOpQrStUvWxYz012345" not in body  # …nor the token
    assert "(url redacted)" in body and "(path redacted)" in body
    assert "(redacted)" in body
    # …and never as `<url>`: GitHub drops unknown tags, so the one control
    # that proves text was removed would leave no trace on the rendered PR
    assert "<url>" not in body and "<path>" not in body
    # the operator's own copy is untouched
    assert "proxy.internal" in _invoke(_RUN, "--dry-run").output


def test_redaction_is_bounded_and_markup_inert() -> None:
    """The pure half of correctness/f-010."""
    assert cli_facts.redact_for_publication("") == ""
    assert cli_facts.redact_for_publication("plain words") == "plain words"
    assert "(path redacted)" in cli_facts.redact_for_publication(
        "wrote ~/loom/work/run.json"
    )
    long = cli_facts.redact_for_publication("stalled after " + "word " * 200)
    assert len(long) <= 200 and long.endswith("…")
    # closing keywords and mentions never travel LIVE: the keyword no longer
    # binds to the issue ref, the mention is quoted
    out = cli_facts.redact_for_publication("gave up. Closes #12 cc @agent-lore/sec")
    assert "Closes #12" not in out and "#12" in out
    assert "@agent-lore/sec" not in out and "&#64;agent-lore/sec" in out


def test_live_github_constructs_survive_no_backtick_trick() -> None:
    """security/f-002: `GH-<n>` closes an issue exactly like `#<n>`, and a
    single stray backtick opens no code span — so neither may be the thing a
    defence depends on."""
    for keyword in ("Closes GH-1337", "fixes gh-1337"):
        out = cli_facts.defang_markup(keyword)
        assert out != keyword and "1337" in out
        assert not cli_facts._CLOSES_RE.search(out)
    # a backtick before the mention used to exempt it entirely
    quoted = cli_facts.defang_markup("cc `@evil-user please approve")
    assert "@evil-user" not in quoted and "&#64;evil-user" in quoted


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


# ── round-5 regressions ────────────────────────────────────────────────


def test_a_run_that_recorded_no_outcome_is_refused(
    host, lithos: FakeLithosClient, run_dir: Path, repo: Path, gh: dict
) -> None:
    """correctness/f-001: `state.json` lands only at run END, while the run dir
    exists from the first round — so a state-less run dir may be a run that is
    live right now, and `--branch` would otherwise supply the branch its state
    does not and deliver a mid-run commit alongside the run's own delivery."""
    (run_dir / "state.json").unlink()

    result = _invoke(_RUN, "--branch", _BRANCH)

    assert result.exit_code == 1, result.output
    assert "recorded no outcome" in result.output
    assert "develop attach" in result.output
    assert gh["created"] == []
    assert _git(repo, "ls-remote", "origin", f"refs/heads/{_BRANCH}") == ""
    assert lithos.mutating_calls == []

    # …and the run-dir-less form stays the operator's explicit assertion
    assert _invoke("--branch", _BRANCH, "--story", _STORY).exit_code == 0


def test_a_malformed_state_is_refused_like_an_absent_one(
    host, lithos: FakeLithosClient, run_dir: Path, repo: Path, gh: dict
) -> None:
    """correctness/f-001: unreadable state is unknown state, not a stop."""
    (run_dir / "state.json").write_text("{ not json", encoding="utf-8")

    result = _invoke(_RUN, "--branch", _BRANCH)

    assert result.exit_code == 1, result.output
    assert "recorded no outcome" in result.output
    assert gh["created"] == []


def test_a_pr_create_whose_response_was_lost_is_adopted_not_abandoned(
    host,
    lithos: FakeLithosClient,
    run_dir: Path,
    repo: Path,
    gh: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """correctness/f-002: `gh pr create` can COMMIT and still report failure —
    the same ambiguity the push resolves by re-reading the remote. An open,
    ungated PR must never be left behind under a "no PR was opened" report."""

    def _create_then_lose_the_response(repo_path: Path, **kwargs: Any) -> str:
        gh["existing"] = [_open_pr(head_sha=_head(repo))]  # GitHub opened it
        raise RuntimeError("gh pr create failed: connection reset by peer")

    monkeypatch.setattr(cli_repo, "create_pr", _create_then_lose_the_response)

    result = _invoke(_RUN)

    assert result.exit_code == 0, result.output
    assert _PR_URL in result.output and "adopted PR #99" in result.output
    # …and the delivery carried on: the PR is gated and the stop retired
    assert (_get(lithos, "gate-human")).status == "completed"
    assert _get(lithos, _STORY).metadata[STORY_GATE_ID_KEY]


def test_a_pr_create_that_really_failed_is_still_a_partial(
    host,
    lithos: FakeLithosClient,
    run_dir: Path,
    repo: Path,
    gh: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """correctness/f-002, the other side: the re-ask finds nothing, so the
    original failure stands and the push is reported as unfinished."""

    def _boom(repo_path: Path, **kwargs: Any) -> str:
        raise RuntimeError("gh pr create failed: permission denied")

    monkeypatch.setattr(cli_repo, "create_pr", _boom)

    result = _invoke(_RUN)

    assert result.exit_code == 2, result.output
    assert "PUSHED, NO PR" in result.output.splitlines()[0]
    assert not lithos.calls_to("task_create")


@pytest.mark.parametrize("mode", ["nonzero", "raises"])
def test_a_failed_upstream_write_never_unwinds_the_push(
    host,
    lithos: FakeLithosClient,
    run_dir: Path,
    repo: Path,
    gh: dict,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    """correctness/f-003: the tracking config is written AFTER the push, so a
    read-only / locked `.git/config` must neither raise past the pushed state
    (exit 1 would claim nothing was written) nor pass silently as though
    `push -u` had been honoured."""
    real_git = cli_repo.run_git

    def _fail_the_config_writes(repo_path: Path, args: list[str], **kw: Any):
        if args[:1] == ["config"]:
            if mode == "raises":
                raise subprocess.TimeoutExpired(cmd="git config", timeout=120)
            return subprocess.CompletedProcess(args, 1, "", "error: could not lock")
        return real_git(repo_path, args, **kw)

    monkeypatch.setattr(cli_repo, "run_git", _fail_the_config_writes)

    result = _invoke(_RUN)

    assert result.exit_code == 0, result.output
    assert _git(repo, "ls-remote", "origin", f"refs/heads/{_BRANCH}") != ""
    assert len(gh["created"]) == 1
    assert "[Friction]" in result.output and "upstream" in result.output
    # the delivery itself stands: gated, retired, recorded
    assert (_get(lithos, "gate-human")).status == "completed"


def test_a_gate_problem_is_named_once_in_the_finding(
    host, lithos: FakeLithosClient, run_dir: Path, repo: Path, gh: dict
) -> None:
    """correctness/f-005: `notes` already carries every gate-phase problem, so
    reading `outcome.problems` again printed each one twice."""
    lithos.raise_on["task_complete"] = LithosClientError("boom", "gate stuck")

    result = _invoke(_RUN)

    assert result.exit_code == 2, result.output
    summary = lithos.findings[-1]["summary"]
    assert summary.count("could not complete the needs-human gate") == 1
    assert summary.count("[Friction]") == 1


def test_only_the_stops_own_escalation_is_retired(
    host, lithos: FakeLithosClient, run_dir: Path, repo: Path, gh: dict, tmp_path: Path
) -> None:
    """security/f-001: loom raises `human` gates from several subsystems, and
    completing an `external-remediation` decision gate is the operator's
    CONSENT to spend another remediation budget on the delivered PR. A
    delivery supersedes the stopped RUN's gate and nothing else."""
    for gate_id, route in (
        ("gate-remediation", "external-remediation"),
        ("gate-conflict", "conflict-resolve"),
        ("gate-routeless", None),
    ):
        lithos.add_task(
            make_task(
                gate_id,
                title=f"Needs human: {_STORY}",
                task_type="gate",
                metadata={
                    "gate_type": GATE_TYPE_HUMAN,
                    "raised_by": RAISED_BY_LOOM,
                    "story_id": _STORY,
                    "escalation_reason": "disputed",
                    **({"route": route} if route else {}),
                },
            )
        )
        lithos.add_edge(from_task_id=gate_id, to_task_id=_STORY, type=WAITS_ON_GATE)

    out = tmp_path / "r.json"
    result = _invoke(_RUN, "--json", str(out))

    assert result.exit_code == 0, result.output  # kept gates are not friction
    assert (_get(lithos, "gate-human")).status == "completed"  # the stop's own
    for kept in ("gate-remediation", "gate-conflict", "gate-routeless"):
        assert (_get(lithos, kept)).status == "open", kept
    record = json.loads(out.read_text())
    assert record["human_gates_completed"] == ["gate-human"]
    assert len(record["human_gates_retained"]) == 3
    summary = lithos.findings[-1]["summary"]
    assert "gate-remediation (route external-remediation" in summary
    assert "left OPEN" in summary
    assert "[Friction]" not in summary


def test_the_dry_run_plan_names_the_gates_it_would_keep(
    host, lithos: FakeLithosClient, run_dir: Path, repo: Path, gh: dict
) -> None:
    """security/f-001: the plan is the screen the operator decides on."""
    lithos.add_task(
        make_task(
            "gate-remediation",
            title=f"Needs human: {_STORY}",
            task_type="gate",
            metadata={
                "gate_type": GATE_TYPE_HUMAN,
                "raised_by": RAISED_BY_LOOM,
                "route": "external-remediation",
                "escalation_reason": "remediation_exhausted",
            },
        )
    )
    lithos.add_edge(
        from_task_id="gate-remediation", to_task_id=_STORY, type=WAITS_ON_GATE
    )

    result = _invoke(_RUN, "--dry-run")

    assert result.exit_code == 0, result.output
    plan = [ln for ln in result.output.splitlines() if "human gates" in ln]
    assert plan and "gate-human" in plan[0] and "gate-remediation" not in plan[0]
    assert "leaving gate-remediation (route external-remediation" in result.output


def test_the_plan_never_echoes_agent_written_control_bytes(
    host, lithos: FakeLithosClient, run_dir: Path, repo: Path, gh: dict
) -> None:
    """security/f-003: `failure_reason` is built from agent stdout / stderr, and
    `--dry-run` is the screen the operator reads to decide whether to publish
    the branch — an ANSI escape there can forge or erase any line on it."""
    state = json.loads((run_dir / "state.json").read_text())
    # a bare BEL beside the CSI runs: click strips the ANSI *sequences* from a
    # non-tty capture on its own, but neither it nor a real terminal saves the
    # operator from the rest — the strip has to happen before the echo
    state["failure_reason"] = "died\x07\x1b[2K\x1b[A  1 push: OK — nothing here"
    (run_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")

    result = _invoke(_RUN, "--dry-run")

    assert result.exit_code == 0, result.output
    assert "\x07" not in result.output and "\x1b" not in result.output
    assert "died[2K[A" in result.output  # the text survives, the escapes do not


# ── round-5 (review round 2) regressions ───────────────────────────────


def _claimed(client: FakeLithosClient, *, aspect: str = "story-develop") -> None:
    """Put a live route claim on the story, as a running dispatch does."""
    story = asyncio.run(client.task_get(task_id=_STORY))
    assert story is not None
    client.add_task(
        make_task(
            _STORY,
            title=story.title,
            description=story.description,
            metadata=dict(story.metadata),
            claims=(
                {
                    "aspect": aspect,
                    "agent": "loom-daemon",
                    "expires_at": "2999-01-01T00:00:00+00:00",
                },
            ),
        )
    )


def test_a_live_route_claim_refuses_before_anything_is_written(
    host, lithos: FakeLithosClient, run_dir: Path, repo: Path, gh: dict
) -> None:
    """correctness/f-001: the run writes its terminal `state.json` well before
    the daemon applies the result and raises the needs-human gate, and the
    route holds its claim across that whole window. Delivering inside it gates
    a story whose escalation does not exist yet — the runner then raises it
    afterwards and the story sits behind BOTH gates."""
    _claimed(lithos)

    result = _invoke(_RUN)

    assert result.exit_code == 1, result.output
    assert "claimed by a live dispatch" in result.output
    assert "story-develop" in result.output
    assert gh["created"] == []
    assert _git(repo, "ls-remote", "origin", f"refs/heads/{_BRANCH}") == ""
    assert lithos.mutating_calls == []


def test_an_expired_claim_does_not_block_a_delivery(
    host, lithos: FakeLithosClient, run_dir: Path, repo: Path, gh: dict
) -> None:
    """correctness/f-001: a claim whose TTL has run out is not a live
    dispatch — only an unexpired (or unparseable) one holds the command."""
    story = _get(lithos, _STORY)
    lithos.add_task(
        make_task(
            _STORY,
            title=story.title,
            description=story.description,
            metadata=dict(story.metadata),
            claims=(
                {
                    "aspect": "story-develop",
                    "agent": "loom-daemon",
                    "expires_at": "2020-01-01T00:00:00+00:00",
                },
            ),
        )
    )

    assert _invoke(_RUN).exit_code == 0
    assert len(gh["created"]) == 1


def test_a_dispatch_that_claims_mid_delivery_keeps_the_human_gate(
    host,
    lithos: FakeLithosClient,
    run_dir: Path,
    repo: Path,
    gh: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """correctness/f-001, the interleaving: the claim appears only after this
    delivery has started, so its escalation may still be on its way. The pr
    gate is raised (that half is always safe) but NO human gate is completed —
    completing one now could retire a gate the run has not finished raising."""
    real_push = cli.push_branch

    def _claim_while_we_push(repo_path: Path, branch: str, state: Any):
        out = real_push(repo_path, branch, state)
        _claimed(lithos)
        return out

    monkeypatch.setattr(cli, "push_branch", _claim_while_we_push)

    result = _invoke(_RUN)

    assert result.exit_code == 2, result.output
    assert _PR_URL in result.output  # the PR stands and is gated
    assert _get(lithos, _STORY).metadata[STORY_GATE_ID_KEY]
    assert (_get(lithos, "gate-human")).status == "open"
    assert "claimed this story while the delivery ran" in result.output


def test_another_runs_gate_is_never_retired(
    host, lithos: FakeLithosClient, run_dir: Path, repo: Path, gh: dict, tmp_path: Path
) -> None:
    """correctness/f-002: a story can match two dispatch routes, each with its
    own stopped run and its own open escalation. Delivering run A's branch
    must not retire run B's gate — nor delete route B's failure record."""
    lithos.add_task(
        make_task(
            "gate-human-b",
            title=f"Needs human: {_STORY}",
            task_type="gate",
            metadata={
                "gate_type": GATE_TYPE_HUMAN,
                "raised_by": RAISED_BY_LOOM,
                "route": "docs-develop",
                "story_id": _STORY,
                "run_id": "other-run",
                "escalation_reason": "stalled",
            },
        )
    )
    lithos.add_edge(from_task_id="gate-human-b", to_task_id=_STORY, type=WAITS_ON_GATE)
    story = _get(lithos, _STORY)
    metadata = dict(story.metadata)
    metadata["loom_last_attempt:docs-develop"] = {"status": "failed"}
    metadata["gate_human_run"] = None
    asyncio.run(lithos.task_update(task_id=_STORY, agent="op", metadata=metadata))
    # both routes are configured on this host
    host.routes = (
        SimpleNamespace(name="story-develop"),
        SimpleNamespace(name="docs-develop"),
    )
    # the delivered run's own gate names it, so it is unambiguous
    gate_a = _get(lithos, "gate-human")
    lithos.add_task(
        make_task(
            "gate-human",
            title=gate_a.title,
            task_type="gate",
            metadata={**gate_a.metadata, "run_id": _RUN},
        )
    )

    out = tmp_path / "r.json"
    assert _invoke(_RUN, "--json", str(out)).exit_code == 0

    assert (_get(lithos, "gate-human")).status == "completed"
    assert (_get(lithos, "gate-human-b")).status == "open"
    metadata = _get(lithos, _STORY).metadata
    assert "loom_last_attempt:story-develop" not in metadata
    assert metadata["loom_last_attempt:docs-develop"]  # route B's record stands
    record = json.loads(out.read_text())
    assert record["human_gates_completed"] == ["gate-human"]
    assert any("other-run" in kept for kept in record["human_gates_retained"])


def test_two_unattributed_dispatch_gates_retire_neither(
    host, lithos: FakeLithosClient, run_dir: Path, repo: Path, gh: dict
) -> None:
    """correctness/f-002: with no run recorded on either gate there is nothing
    to match on, so the conservative rule keeps both and says why."""
    lithos.add_task(
        make_task(
            "gate-human-b",
            title=f"Needs human: {_STORY}",
            task_type="gate",
            metadata={
                "gate_type": GATE_TYPE_HUMAN,
                "raised_by": RAISED_BY_LOOM,
                "route": "story-develop",
                "escalation_reason": "stalled",
            },
        )
    )
    lithos.add_edge(from_task_id="gate-human-b", to_task_id=_STORY, type=WAITS_ON_GATE)

    result = _invoke(_RUN)

    assert result.exit_code == 0, result.output
    assert (_get(lithos, "gate-human")).status == "open"
    assert (_get(lithos, "gate-human-b")).status == "open"
    assert "cannot tell which run raised it" in result.output
    # route A's failure record is protected too while its gate is open
    assert "loom_last_attempt:story-develop" in _get(lithos, _STORY).metadata


def test_an_unconfigured_route_is_never_treated_as_a_dispatch(
    host, lithos: FakeLithosClient, run_dir: Path, repo: Path, gh: dict
) -> None:
    """security/f-003: the rule is an ALLOWLIST of this host's configured
    routes. A route nobody configured — the next subsystem to raise a gate —
    is never a stopped run's, so it is never completed on a denylist miss."""
    host.routes = (SimpleNamespace(name="docs-develop"),)

    result = _invoke(_RUN)

    assert result.exit_code == 0, result.output
    assert (_get(lithos, "gate-human")).status == "open"
    assert "not a dispatch route on this host" in result.output


def test_a_failed_gate_attempt_is_never_recorded_as_no_gate(
    host,
    lithos: FakeLithosClient,
    run_dir: Path,
    repo: Path,
    gh: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """correctness/f-003: `outcome is None` is not "the operator asked for an
    ungated PR" — it is also every way the gate phase can fail."""

    def _unreachable(*a: Any, **k: Any):
        raise cli_lithos.DeliverRefused("Lithos call failed: connection refused")

    monkeypatch.setattr(cli, "run_gate_delivery", _unreachable)

    assert _invoke(_RUN).exit_code == 2
    summary = lithos.findings[-1]["summary"]
    assert "--no-gate" not in summary
    assert "the pr gate was NOT raised" in summary


def test_gating_a_previously_no_gate_delivery_posts_the_correction(
    host, lithos: FakeLithosClient, run_dir: Path, repo: Path, gh: dict
) -> None:
    """correctness/f-003: the marker records the state the delivery REACHED,
    so the run that gates a PR delivered `--no-gate` must correct the story's
    only durable provenance instead of reading it as already said."""
    assert _invoke(_RUN, "--no-gate").exit_code == 0
    assert "UNMONITORED" in lithos.findings[0]["summary"]
    gh["existing"] = [_open_pr(head_sha=_head(repo))]

    assert _invoke(_RUN).exit_code == 0

    assert len(lithos.findings) == 2
    corrected = lithos.findings[-1]["summary"]
    assert "UNMONITORED" not in corrected
    assert "now blocks the story" in corrected
    assert "needs-human gate gate-human completed" in corrected
    # …and a third run, now that the record matches, says nothing more
    assert _invoke(_RUN).exit_code == 0
    assert len(lithos.findings) == 2


def test_an_unreadable_remote_after_a_push_is_never_nothing_written(
    host,
    lithos: FakeLithosClient,
    run_dir: Path,
    repo: Path,
    gh: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """correctness/f-004: the push reported failure AND the read that would
    settle it failed too. The one thing that cannot be asserted here is that
    nothing was written."""
    real_git = cli_repo.run_git
    pushed = {"done": False}

    def _lose_the_response_then_the_remote(repo_path: Path, args: list[str], **kw: Any):
        if args[:1] == ["push"]:
            real_git(repo_path, args, **kw)  # it lands…
            pushed["done"] = True
            return subprocess.CompletedProcess(args, 1, "", "fatal: the remote hung up")
        if pushed["done"] and args[:1] == ["ls-remote"]:
            return subprocess.CompletedProcess(args, 1, "", "fatal: could not read")
        return real_git(repo_path, args, **kw)

    monkeypatch.setattr(cli_repo, "run_git", _lose_the_response_then_the_remote)

    result = _invoke(_RUN)

    assert result.exit_code == 2, result.output
    assert "PUSH UNCERTAIN" in result.output
    assert "not known whether" in result.output
    # the remote really does hold it — the command just could not prove it
    assert _git(repo, "ls-remote", "origin", f"refs/heads/{_BRANCH}") != ""


def test_an_unreadable_pr_list_after_a_create_is_never_nothing_written(
    host,
    lithos: FakeLithosClient,
    run_dir: Path,
    repo: Path,
    gh: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """correctness/f-004, the PR half: `gh pr create` may have opened the PR
    and the recovery read failed too. With the branch already on origin this
    used to exit 1 saying nothing was written."""
    _git(repo, "push", "origin", _BRANCH)  # nothing for this run to push
    listed = {"count": 0}

    def _list_then_fail(*a: Any, **k: Any):
        listed["count"] += 1
        if listed["count"] > 1:
            raise RuntimeError("gh pr list failed: network is down")
        return []

    def _create_then_lose_the_response(*a: Any, **k: Any):
        raise RuntimeError("gh pr create failed: connection reset by peer")

    monkeypatch.setattr(cli_repo, "list_open_prs_for_branch", _list_then_fail)
    monkeypatch.setattr(cli_repo, "create_pr", _create_then_lose_the_response)

    result = _invoke(_RUN)

    assert result.exit_code == 2, result.output
    assert "not known whether a PR was opened" in result.output
    assert "nothing written" not in result.output


def test_the_handoff_summary_is_redacted_like_the_stop_reason(
    host, lithos: FakeLithosClient, run_dir: Path, repo: Path, gh: dict
) -> None:
    """security/f-001: the handoff is agent-chosen text on its way to a
    world-readable PR body — no less host-derived than the stop reason beside
    it, and the fence around it neutralises markup, not content."""
    (run_dir / "handoff" / "round_04_coder_done.md").write_text(
        "## Status: LGTM\n\n## Summary\nRan /home/dave/.config/gh/hosts.yml against "
        "https://proxy.internal/v1 with ghp_AbCdEfGhIjKlMnOpQrStUvWxYz012345\n",
        encoding="utf-8",
    )

    assert _invoke(_RUN).exit_code == 0
    body = gh["created"][0]["body"]

    assert "/home/dave" not in body
    assert "proxy.internal" not in body
    assert "ghp_AbCdEfGhIjKlMnOpQrStUvWxYz012345" not in body
    assert "(path redacted)" in body and "(url redacted)" in body


def test_redaction_placeholders_are_visible_in_the_rendered_pr() -> None:
    """security/f-002: `url`, `host`, `path` and `redacted` are all valid HTML
    tag names, so an angle-bracketed placeholder is parsed as raw inline HTML
    and dropped by GitHub's sanitizer — the redaction would leave no trace on
    the one surface people read."""
    out = cli_facts.redact_for_publication("auth 401 from https://proxy.internal/v1")
    assert "<" not in out and ">" not in out
    assert "(url redacted)" in out


def test_bare_hosts_and_autolinked_www_never_reach_the_pr() -> None:
    """security/f-004: an RFC1918 address or a bare `host:port` is pure host
    topology, and GFM autolinks a `www.` host — the live link the defang pass
    exists to prevent."""
    assert "10.1.2.3" not in cli_facts.redact_for_publication(
        "connection refused to 10.1.2.3:8443"
    )
    out = cli_facts.redact_for_publication("talking to 192.168.7.11 (gh-proxy-3:8080)")
    assert "192.168.7.11" not in out and "gh-proxy-3:8080" not in out
    assert "www.evil.example" not in cli_facts.redact_for_publication(
        "see www.evil.example/beacon.png"
    )
    # …and ordinary text with a colon is left alone
    assert cli_facts.redact_for_publication("Error:404 at line 12:34") == (
        "Error:404 at line 12:34"
    )


def test_bidi_and_zero_width_characters_are_stripped(
    host, lithos: FakeLithosClient, run_dir: Path, repo: Path, gh: dict
) -> None:
    """security/f-005: a bidi override reorders the rendered line — the trojan
    source technique — so the plan the operator decides on, and the PR body,
    can read differently from what was delivered. Neutralised in the one
    shared pass, like the ANSI escapes beside it."""
    state = json.loads((run_dir / "state.json").read_text())
    state["failure_reason"] = "died \u202egnihton\u202c \u200bhidden"
    (run_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")

    plan = _invoke(_RUN, "--dry-run")
    assert plan.exit_code == 0, plan.output
    assert "\u202e" not in plan.output and "\u200b" not in plan.output

    assert _invoke(_RUN).exit_code == 0
    body = gh["created"][0]["body"]
    assert "\u202e" not in body and "\u200b" not in body
    assert "gnihton" in body  # the text survives; only the reordering goes
    assert cli_facts.defang_markup("a\u202eb") == "ab"


# ── round-6 (review round 3) regressions ───────────────────────────────


def _daemon(monkeypatch: pytest.MonkeyPatch, *, alive: bool) -> None:
    """Report a loom daemon running (or not) on this host's work dir.

    The command asks the pidfile — the PROCESS, not its claim — so that is the
    seam, patched on the public module both it and `drain` read.
    """
    identity = SimpleNamespace(pid=4242, boot_id="b", start_ticks=1)
    monkeypatch.setattr(
        pidfile, "read_pidfile", lambda path: identity if alive else None
    )
    monkeypatch.setattr(pidfile, "daemon_alive", lambda path, ident: alive)


def _drop_the_human_gate(client: FakeLithosClient) -> None:
    """The story as it looks BEFORE the daemon has raised the run's gate."""
    asyncio.run(client.task_cancel(task_id="gate-human", agent="op"))
    story = asyncio.run(client.task_get(task_id=_STORY))
    assert story is not None
    metadata = {
        key: None
        for key in story.metadata
        if key.startswith("loom_last_attempt:") or key == STORY_HUMAN_GATE_ID_KEY
    }
    asyncio.run(client.task_update(task_id=_STORY, agent="op", metadata=metadata))


def test_a_stop_whose_escalation_has_not_landed_is_refused_under_a_live_daemon(
    host,
    lithos: FakeLithosClient,
    run_dir: Path,
    repo: Path,
    gh: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """correctness/f-001: an absent CLAIM proves nothing — the runner's renew
    loop swallows every failure, so a Lithos outage longer than the TTL leaves
    the claim expired (and invisible) while the plugin writes its terminal
    state and the runner waits to apply the result. The durable handoff is the
    escalation itself; until it lands, a running daemon still owns this run."""
    _drop_the_human_gate(lithos)
    _daemon(monkeypatch, alive=True)
    before = len(lithos.mutating_calls)

    result = _invoke(_RUN)

    assert result.exit_code == 1, result.output
    assert "records its escalation yet" in result.output
    assert "drain" in result.output and "--branch" in result.output
    assert gh["created"] == []
    assert _git(repo, "ls-remote", "origin", f"refs/heads/{_BRANCH}") == ""
    assert lithos.mutating_calls[before:] == []  # not even a claim


def test_a_failed_attempt_marker_is_handoff_enough(
    host,
    lithos: FakeLithosClient,
    run_dir: Path,
    repo: Path,
    gh: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """correctness/f-001: the marker-only `[BlockerFailed]` fallback raises no
    gate, but the runner wrote the marker on the same path — and a marker that
    names NO gate is the one shape that suppresses dispatch by itself
    (`declines_bootstrap_replay`), so the run's result has been applied and
    nothing can be re-dispatching it."""
    _drop_the_human_gate(lithos)
    asyncio.run(
        lithos.task_update(
            task_id=_STORY,
            agent="op",
            metadata={"loom_last_attempt:story-develop": {"run_id": _RUN}},
        )
    )
    _daemon(monkeypatch, alive=True)

    assert _invoke(_RUN).exit_code == 0
    assert len(gh["created"]) == 1


def test_a_completed_gates_marker_is_not_a_handoff(
    host,
    lithos: FakeLithosClient,
    run_dir: Path,
    repo: Path,
    gh: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """correctness/f-001: a failed-attempt marker that NAMES a gate stops
    deciding anything the moment that gate is completed — the gate is the
    guard, and completing it is the operator's authorisation to re-dispatch
    (`declines_bootstrap_replay` returns False whenever `gate_id` is set). So
    between the tick and the route's claim the story is on the frontier with
    the marker still on it: reading that as the handoff would deliver the old
    run while the route walks from ready to claimed, and the run it dispatches
    would raise its own gate over a story this command just called delivered.
    """
    # the runner's real shape: the marker names the gate it raised…
    asyncio.run(
        lithos.task_update(
            task_id=_STORY,
            agent="op",
            metadata={
                "loom_last_attempt:story-develop": {
                    "run_id": _RUN,
                    "gate_id": "gate-human",
                }
            },
        )
    )
    # …and the operator has just ticked that gate (re-dispatch authorised)
    asyncio.run(lithos.task_complete(task_id="gate-human", agent="op"))
    _daemon(monkeypatch, alive=True)
    before = len(lithos.mutating_calls)

    result = _invoke(_RUN)

    assert result.exit_code == 1, result.output
    assert "records its escalation yet" in result.output
    assert gh["created"] == []
    assert _git(repo, "ls-remote", "origin", f"refs/heads/{_BRANCH}") == ""
    assert lithos.mutating_calls[before:] == []


def test_a_landed_escalation_delivers_under_a_live_daemon_and_re_runs_cleanly(
    host,
    lithos: FakeLithosClient,
    run_dir: Path,
    repo: Path,
    gh: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """correctness/f-001: the guard must not cost the ordinary case. The gate
    the daemon raised IS the handoff — and once this delivery has retired it,
    the story's own open `pr` gate lets the idempotent re-run through."""
    _daemon(monkeypatch, alive=True)

    assert _invoke(_RUN).exit_code == 0
    gh["existing"] = [_open_pr(head_sha=_head(repo))]

    result = _invoke(_RUN)  # the gate is completed now — the pr gate answers

    assert result.exit_code == 0, result.output
    assert len(gh["created"]) == 1
    assert len(lithos.findings) == 1


def test_a_dead_daemon_never_blocks_the_salvage(
    host,
    lithos: FakeLithosClient,
    run_dir: Path,
    repo: Path,
    gh: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """correctness/f-001: no producer on this host, so nothing can be racing —
    which is exactly the crash this command exists to clean up after."""
    _drop_the_human_gate(lithos)
    _daemon(monkeypatch, alive=False)

    assert _invoke(_RUN).exit_code == 0
    assert len(gh["created"]) == 1


def test_the_explicit_branch_form_is_the_operators_own_assertion(
    host,
    lithos: FakeLithosClient,
    repo: Path,
    gh: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """correctness/f-001: with no run dir there is no run to be mid-flight and
    no state to read — the operator is the one saying it is over."""
    _drop_the_human_gate(lithos)
    _daemon(monkeypatch, alive=True)

    assert _invoke("--branch", _BRANCH, "--story", _STORY).exit_code == 0
    assert len(gh["created"]) == 1


def test_no_gate_over_an_already_gated_pr_never_calls_it_unmonitored(
    host, lithos: FakeLithosClient, run_dir: Path, repo: Path, gh: dict, tmp_path: Path
) -> None:
    """correctness/f-003: achieved state is not "what this invocation did". A
    `--no-gate` pass over a PR that is already behind its own `pr` gate must
    not durably record that PR as UNMONITORED, nor mark the delivery as an
    ungated one — the story would contradict its own open gate."""
    lithos.raise_on["task_complete"] = LithosClientError("boom", "gate stuck")
    assert _invoke(_RUN).exit_code == 2  # pr gate raised, human gate not, no marker
    gate_id = _get(lithos, _STORY).metadata[STORY_GATE_ID_KEY]
    lithos.raise_on.pop("task_complete")
    gh["existing"] = [_open_pr(head_sha=_head(repo))]

    out = tmp_path / "r.json"
    result = _invoke(_RUN, "--no-gate", "--json", str(out))

    assert result.exit_code == 0, result.output
    summary = lithos.findings[-1]["summary"]
    assert "UNMONITORED" not in summary
    assert gate_id in summary and "already blocks the story" in summary
    assert json.loads(out.read_text())["pr_gate_id"] == gate_id
    story = _get(lithos, _STORY)
    assert story.metadata["manual_delivery"]["gated"] is True


def test_the_completed_gate_swap_is_always_recorded_somewhere(
    host, lithos: FakeLithosClient, run_dir: Path, repo: Path, gh: dict
) -> None:
    """correctness/f-003: the marker records what the delivery ACHIEVED, and
    the gate swap is half of that. A pass that gated the PR but could not
    complete the human gate, then a `--no-gate` pass over that state, must not
    between them silence the run that finally completes the swap — step 5
    promises the finding names the gates retired, so the run that retires them
    is the one that speaks."""
    lithos.raise_on["task_complete"] = LithosClientError("boom", "gate stuck")
    assert _invoke(_RUN).exit_code == 2  # pr gate raised, human gate not
    lithos.raise_on.pop("task_complete")
    gh["existing"] = [_open_pr(head_sha=_head(repo))]

    # …an intermediate `--no-gate` pass sees the pr gate and records THAT
    assert _invoke(_RUN, "--no-gate").exit_code == 0
    assert (_get(lithos, "gate-human")).status == "open"
    findings_before = len(lithos.findings)

    result = _invoke(_RUN)  # …and now the swap actually completes

    assert result.exit_code == 0, result.output
    assert (_get(lithos, "gate-human")).status == "completed"
    assert len(lithos.findings) == findings_before + 1
    corrected = lithos.findings[-1]["summary"]
    assert "needs-human gate gate-human completed" in corrected
    marker = _get(lithos, _STORY).metadata["manual_delivery"]
    assert marker["gated"] is True and marker["swapped"] is True
    # …and with nothing left to achieve, a fourth pass says nothing more
    assert _invoke(_RUN).exit_code == 0
    assert len(lithos.findings) == findings_before + 1


def test_an_unverifiable_pr_create_never_asserts_there_is_no_pr(
    host,
    lithos: FakeLithosClient,
    run_dir: Path,
    repo: Path,
    gh: dict,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """correctness/f-004: the PR may exist. Every line must stay in the
    subjunctive — an operator told "NO PR" opens a second one by hand — and
    the headline must not claim a push this invocation did not make."""
    _git(repo, "push", "origin", _BRANCH)  # nothing for this run to push
    listed = {"count": 0}

    def _list_then_fail(*a: Any, **k: Any):
        listed["count"] += 1
        if listed["count"] > 1:
            raise RuntimeError("gh pr list failed: network is down")
        return []

    monkeypatch.setattr(cli_repo, "list_open_prs_for_branch", _list_then_fail)
    monkeypatch.setattr(
        cli_repo,
        "create_pr",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("gh pr create failed")),
    )

    out = tmp_path / "r.json"
    result = _invoke(_RUN, "--json", str(out))

    assert result.exit_code == 2, result.output
    headline = result.output.splitlines()[0]
    assert "PR UNCERTAIN" in headline
    assert "NO PR" not in result.output
    assert "no PR was opened" not in result.output
    assert "PUSHED" not in headline  # this invocation pushed nothing
    assert json.loads(out.read_text())["pr_uncertain"] is True


def test_a_huge_handoff_summary_cannot_hang_the_command() -> None:
    """security/f-006: the redaction pass runs patterns that cost O(n²) on a
    long dotted run, and the handoff feeding it is agent-written and bounded
    only by 1 MiB — so the INPUT is bounded, not just the output."""
    started = time.monotonic()
    out = cli_facts.redact_for_publication("a." * 100_000, limit=600)
    assert time.monotonic() - started < 5.0
    assert len(out) <= 600


def test_a_huge_handoff_is_still_read_bounded_and_redacted(tmp_path: Path) -> None:
    """security/f-006, end to end through the real reader: a 200 KB summary
    with a host path in it comes back capped, redacted and fast."""
    handoff = tmp_path / "handoff"
    handoff.mkdir()
    (handoff / "round_03_coder_done.md").write_text(
        "## Status: LGTM\n\n## Summary\nRan /home/dave/.config/gh/hosts.yml "
        + "a." * 100_000
        + "\n",
        encoding="utf-8",
    )

    started = time.monotonic()
    summary = cli_facts.coder_summary(handoff)

    assert time.monotonic() - started < 5.0
    assert len(summary) <= 600
    assert "/home/dave" not in summary and "(path redacted)" in summary


# ── round-7 (review round 4) regressions ───────────────────────────────


def _claim_spy(client: FakeLithosClient, order: list[str]) -> None:
    """Record every claim as it happens, so the ordering can be asserted."""
    real = client.task_claim

    async def _spy(**kwargs: Any) -> Any:
        order.append(f"claim:{kwargs['aspect']}")
        return await real(**kwargs)

    client.task_claim = _spy  # type: ignore[method-assign]


def test_the_dispatch_hold_excludes_a_route_for_the_whole_delivery(
    host,
    lithos: FakeLithosClient,
    run_dir: Path,
    repo: Path,
    gh: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """correctness/f-001: the preflight reads are point-in-time — a daemon can
    boot, bootstrap the story and pass its readiness check a moment later, and
    the runner's own gap between "ready" and "claimed" spans several awaits.
    So the delivery CLAIMS every configured route before it pushes and holds
    it until the gate exists: the route-runner claims the same aspect before
    dispatching, and the server decides which of us got there first."""
    order: list[str] = []
    _claim_spy(lithos, order)
    real_create = gh["created"]

    def _note_the_push(repo_path: Path, branch: str, state: Any):
        order.append("push")
        return cli_repo.push_branch(repo_path, branch, state)

    monkeypatch.setattr(cli, "push_branch", _note_the_push)

    assert _invoke(_RUN).exit_code == 0
    assert len(real_create) == 1

    # taken under an identity of our OWN: a claim only excludes another agent,
    # so a hold under the daemon's id would exclude nothing
    held = [c for c in lithos.calls_to("task_claim") if c["aspect"] == "story-develop"]
    assert held and held[0]["agent"] == cli_lithos.dispatch_hold_agent("loom")
    assert held[0]["agent"] != "loom"
    # …before the push, and handed back afterwards (the release is what
    # re-triggers the runner's readiness check, which now defers)
    assert order.index("claim:story-develop") < order.index("push")
    assert [
        c for c in lithos.calls_to("task_release") if c["aspect"] == "story-develop"
    ]


def test_a_route_that_claims_first_stops_the_delivery_dead(
    host,
    lithos: FakeLithosClient,
    run_dir: Path,
    repo: Path,
    gh: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """correctness/f-001, the interleaving: a daemon boots after the preflight
    reads and its route claims the story first. The hold is the atomic test —
    it fails, and the delivery stops before anything is written rather than
    gating a story a fresh run is about to develop from scratch."""
    _drop_the_human_gate(lithos)
    _daemon(monkeypatch, alive=False)  # nothing to see at preflight…
    real = lithos.task_claim

    async def _route_got_there_first(**kwargs: Any) -> Any:
        if kwargs["aspect"] == "story-develop":  # …the daemon booted since
            raise LithosClientError("claim_failed", "held by the route")
        return await real(**kwargs)

    lithos.task_claim = _route_got_there_first  # type: ignore[method-assign]

    result = _invoke(_RUN)

    assert result.exit_code == 1, result.output
    assert "is dispatching" in result.output
    assert gh["created"] == []
    assert _git(repo, "ls-remote", "origin", f"refs/heads/{_BRANCH}") == ""
    assert not lithos.calls_to("task_create")


def test_a_pidfile_being_written_counts_as_a_live_daemon(
    host,
    lithos: FakeLithosClient,
    run_dir: Path,
    repo: Path,
    gh: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """correctness/f-001: `claim_pidfile` truncates and rewrites the file under
    its lock, so a daemon booting right now shows a pidfile with no well-formed
    identity in it. Reading that as "nobody here" is the wrong answer at the
    worst moment — the lock answers instead."""
    _drop_the_human_gate(lithos)
    monkeypatch.setattr(pidfile, "read_pidfile", lambda path: None)
    monkeypatch.setattr(pidfile, "holder_alive", lambda path: True)

    result = _invoke(_RUN)

    assert result.exit_code == 1, result.output
    assert "records its escalation yet" in result.output
    assert gh["created"] == []


def test_the_swap_is_never_called_done_over_a_gate_this_host_cannot_retire(
    host, lithos: FakeLithosClient, run_dir: Path, repo: Path, gh: dict
) -> None:
    """correctness/f-003: "the swap is finished" must not mean "this config
    gave me nothing to retire". A gate whose route this host does not
    configure is left OPEN — so the record must stay re-postable, and the
    invocation that can retire it (a config that names the route) must still
    be able to say so."""
    gate = _get(lithos, "gate-human")
    lithos.add_task(
        make_task(
            "gate-human",
            title=gate.title,
            task_type="gate",
            metadata={**gate.metadata, "route": "docs-develop", "run_id": _RUN},
        )
    )

    assert _invoke(_RUN).exit_code == 0  # host.routes names story-develop only
    assert (_get(lithos, "gate-human")).status == "open"
    findings_before = len(lithos.findings)

    # the eligibility changes: the route is configured now, so the gate is
    # this delivery's to retire — and the corrected record must land
    host.routes = (
        SimpleNamespace(name="story-develop"),
        SimpleNamespace(name="docs-develop"),
    )
    gh["existing"] = [_open_pr(head_sha=_head(repo))]

    assert _invoke(_RUN).exit_code == 0
    assert (_get(lithos, "gate-human")).status == "completed"
    assert len(lithos.findings) == findings_before + 1
    assert "needs-human gate gate-human completed" in lithos.findings[-1]["summary"]
    assert _get(lithos, _STORY).metadata["manual_delivery"]["swapped"] is True
    # …and a third pass, with nothing left to retire, says nothing more
    assert _invoke(_RUN).exit_code == 0
    assert len(lithos.findings) == findings_before + 1


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


def test_adoptable_takes_only_our_own_head() -> None:
    ours = _open_pr(head_sha="a" * 40)
    fork = _open_pr(number=2, head_sha="a" * 40, cross_repository=True)
    stale = _open_pr(number=3, head_sha="b" * 40)
    assert cli_repo.adoptable([ours], head_sha="a" * 40, base="main") == (ours, "")
    assert cli_repo.adoptable([fork, ours], head_sha="a" * 40, base="main") == (
        ours,
        "",
    )
    assert cli_repo.adoptable([], head_sha="a" * 40, base="main") == (None, "")
    pr, reason = cli_repo.adoptable([fork, stale], head_sha="a" * 40, base="main")
    assert pr is None and "#2" in reason and "#3" in reason


def test_adoptable_fails_closed_on_fields_github_did_not_report() -> None:
    """correctness/f-008 + security/f-008: a check that silently does not run
    is the failure this function exists to prevent. Unknown provenance is
    never ours."""
    no_head = _open_pr(number=4, head_sha="")
    pr, reason = cli_repo.adoptable([no_head], head_sha="a" * 40, base="main")
    assert pr is None and "#4" in reason and "not reported" in reason

    no_base = _open_pr(number=5, head_sha="a" * 40, base_ref="")
    pr, reason = cli_repo.adoptable([no_base], head_sha="a" * 40, base="main")
    assert pr is None and "#5" in reason


def test_adoptable_checks_the_base_even_when_the_operator_named_none() -> None:
    """security/f-008: the comparison is against the RESOLVED base, so an
    adopted PR can never target a branch `deliver` would not have opened onto."""
    other_base = _open_pr(head_sha="a" * 40, base_ref="release")
    pr, reason = cli_repo.adoptable([other_base], head_sha="a" * 40, base="main")
    assert pr is None and "release" in reason and "main" in reason


def test_coder_summary_is_bounded_and_control_stripped(tmp_path: Path) -> None:
    handoff = tmp_path / "handoff"
    handoff.mkdir()
    (handoff / "round_02_coder_done.md").write_text(
        "## Status: LGTM\n\n## Summary\n" + "word " * 1000 + "\n", encoding="utf-8"
    )
    summary = cli_facts.coder_summary(handoff)
    assert len(summary) <= 600 and summary.endswith("…")
    assert cli_facts.coder_summary(tmp_path / "nope") == ""


def test_run_facts_reads_state_and_the_escalation_brief(run_dir: Path) -> None:
    facts = cli_facts.run_facts(run_dir)
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
    facts = cli_facts.run_facts(d)
    assert facts == cli_facts.RunFacts(
        story_id="t-1", branch="", run_id="r-1", run_dir=str(d)
    )


def test_provenance_names_the_run_and_why_it_stopped(run_dir: Path) -> None:
    lines = cli_facts.provenance_lines(cli_facts.run_facts(run_dir))
    assert any("develop deliver" in line for line in lines)
    assert any(_RUN in line and "disputed" in line for line in lines)
    assert any(_BRANCH in line for line in lines)


def test_the_delivery_does_not_mistake_its_own_dispatch_hold_for_a_live_run(
    lithos: FakeLithosClient,
) -> None:
    """converge f6babfd8, correctness/f-001. The dispatch hold claims every
    configured route's aspect — exactly what a real dispatch claims — under the
    hold identity, and the story is read back while it is held. Told apart by
    AGENT, not aspect: named as ours it is invisible; a claim on the same aspect
    under any other agent is the live run it looks like. (The end-to-end test
    covers the integration since the fake started recording claims; this pins
    the rule itself, through the public read.)"""
    hold = cli_lithos.dispatch_hold_agent("loom")
    asyncio.run(lithos.task_claim(task_id=_STORY, aspect="story-develop", agent=hold))
    asyncio.run(lithos.task_claim(task_id=_STORY, aspect="deliver", agent="loom"))

    ours = asyncio.run(cli_lithos.read_story(lithos, _STORY, own_agents=(hold,)))
    assert ours.route_claims == ()
    unnamed = asyncio.run(cli_lithos.read_story(lithos, _STORY))
    assert unnamed.route_claims == (f"story-develop (agent {hold})",)

    asyncio.run(lithos.task_claim(task_id=_STORY, aspect="story-develop", agent="loom"))
    foreign = asyncio.run(cli_lithos.read_story(lithos, _STORY, own_agents=(hold,)))
    assert foreign.route_claims == ("story-develop (agent loom)",)
