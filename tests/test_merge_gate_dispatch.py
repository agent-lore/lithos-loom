"""Tests for ``lithos_loom.subscriptions.merge_gate_dispatch`` (PRD S3, the
watcher half).

On every still-open ``pr`` gate the sweep may spawn ``develop merge-gate
--story`` — zero tokens, minutes of checks — to answer *"will the base break
if this merges now?"* against the project's CURRENT config. Pinned hardest:

- **The re-run key is ``(head_sha, base_sha, settings fingerprint)``**: a base
  move or a head push re-gates; an unchanged pair re-gates only when the
  story's resolved settings changed (probed with ``--resolve-only``, no
  fetch, no merge); a conflict / fork / closed record never probes.
- **One in-flight run per project**; a second gate in the same project
  defers, another project's proceeds beside it.
- **Mutual hold with remediation** on the same PR (either may push).
- **Every outcome is a one-shot record**: green records (and a push is
  recorded as loom's own on the S5b budget); red / errored posts
  ``[MergeGateFailed]``; a conflict widens ``[PRConflicted]`` with the paths;
  an unresolvable config or a crash posts ``[Friction]`` on the story.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from lithos_loom.gates import PrGateSpec, create_pr_gate, parse_pr_gate
from lithos_loom.subscriptions import SubscriptionContext
from lithos_loom.subscriptions.merge_gate_dispatch import (
    MERGE_GATE_FAILED,
    MERGE_GATE_KEY,
    MERGE_GATE_SETTING,
    MergeGateDispatch,
    MergeGateSettings,
    read_record,
)
from lithos_loom.subscriptions.pr_landability import PR_CONFLICTED
from lithos_loom.subscriptions.remediation_budget import (
    REMEDIATION_KEY,
    RemediationBudget,
    read_budget,
)
from tests.support import FakeLithosClient, make_note

_PR_URL = "https://github.com/agent-lore/lithos-lens/pull/62"
_HEAD = "h" * 40
_BASE = "b" * 40
_MERGE = "m" * 40
_FP = "0123456789abcdef"

import logging  # noqa: E402


def _ctx(lithos: Any) -> SubscriptionContext:
    return SubscriptionContext(
        lithos=lithos,
        logger=logging.getLogger("test-merge-gate-dispatch"),
        agent_id="lithos-loom-agent",
    )


async def _gate_with_story(
    client: FakeLithosClient,
    *,
    project: str | None = "p",
    story_project: str | None = "p",
) -> tuple[str, Any]:
    story = await client.task_create(
        title="US7",
        metadata={"project": story_project} if story_project else {},
    )
    gate_id = await create_pr_gate(
        client,
        story_id=story,
        story_title="US7",
        pr_url=_PR_URL,
        project=project,
        agent="a",
    )
    gate = await client.task_get(task_id=gate_id)
    assert gate is not None
    return story, gate


async def _refresh(client: FakeLithosClient, gate_id: str) -> Any:
    gate = await client.task_get(task_id=gate_id)
    assert gate is not None
    return gate


def _settings(tmp_path: Path, **overrides: Any) -> MergeGateSettings:
    defaults: dict[str, Any] = {
        "enabled": True,
        "projects": {"p": tmp_path / "repo", "q": tmp_path / "repo-q"},
        "work_dir": tmp_path / "work",
        "config_path": tmp_path / "host.toml",
    }
    defaults.update(overrides)
    return MergeGateSettings(**defaults)


def _pr(
    head: str = _HEAD,
    base: str = _BASE,
    *,
    head_repo: str = "agent-lore/lithos-lens",
    base_repo: str = "agent-lore/lithos-lens",
) -> SimpleNamespace:
    return SimpleNamespace(
        head_sha=head,
        base_sha=base,
        base_ref="main",
        head_repo=head_repo,
        base_repo=base_repo,
    )


def _record(
    status: str = "green",
    *,
    fp: str = _FP,
    pushed: bool | None = None,
    verdict: str | None = None,
    checks: list[dict[str, Any]] | None = None,
    paths: list[str] | None = None,
    push_error: str = "",
    behind: bool = True,
) -> dict[str, Any]:
    if verdict is None:
        verdict = {"green": "GREEN", "red": "RED"}.get(status)
    if pushed is None:
        # a green run on a behind PR pushes its merge commit; anything else
        # pushes nothing (an unpushed green-and-behind is a push FAILURE)
        pushed = status == "green" and behind
    return {
        "status": status,
        "head_ref": "#62",
        "head_branch": "feature",
        "base_ref": "origin/main",
        "base_sha": _BASE,
        "head_sha": _HEAD,
        "merge_sha": _MERGE if status not in ("conflict",) else "",
        "behind": behind,
        "conflicting_paths": paths or [],
        "checks": checks
        or (
            [
                {
                    "name": "test",
                    "command": "make test",
                    "state": "required",
                    "stage": "fast",
                    "outcome": "ran",
                    "passed": status != "red",
                    "exit_code": 0 if status != "red" else 1,
                    "timed_out": False,
                    "output_tail": "",
                }
            ]
            if status in ("green", "red")
            else []
        ),
        "verdict": verdict,
        "config_fingerprint": "c" * 16,
        "settings_fingerprint": fp,
        "pushed": pushed,
        "pushed_sha": _MERGE if pushed else "",
        "push_error": push_error,
        "message": f"stubbed {status}",
    }


def _probe(fp: str = _FP) -> dict[str, Any]:
    return {
        "status": "resolved",
        "settings_fingerprint": fp,
        "image": "img",
        "review_profile": "standard",
    }


def _spawner(
    run: dict[str, Any] | None,
    *,
    probe: dict[str, Any] | None = None,
    rc: int = 0,
    probe_rc: int = 0,
) -> tuple[Any, list[list[str]]]:
    """A fake spawn: records every argv, writes the --json payload the CLI
    would — the probe's for a --resolve-only argv, the run's otherwise."""
    calls: list[list[str]] = []

    async def spawn(cmd: list[str]) -> tuple[int, str]:
        calls.append(cmd)
        path = Path(cmd[cmd.index("--json") + 1])
        path.parent.mkdir(parents=True, exist_ok=True)
        if "--resolve-only" in cmd:
            if probe is not None:
                path.write_text(json.dumps(probe), encoding="utf-8")
            return probe_rc, "probe output"
        if run is not None:
            path.write_text(json.dumps(run), encoding="utf-8")
        return rc, "merge-gate output"

    return spawn, calls


def _probes(calls: list[list[str]]) -> list[list[str]]:
    return [c for c in calls if "--resolve-only" in c]


def _runs(calls: list[list[str]]) -> list[list[str]]:
    return [c for c in calls if "--resolve-only" not in c]


def _findings(client: FakeLithosClient) -> list[str]:
    return [f["summary"] for f in client.findings]


async def _consider(
    client: FakeLithosClient,
    gate: Any,
    story: str | None,
    dispatch: MergeGateDispatch,
    *,
    pr: Any = None,
    hold: bool = False,
) -> str:
    spec = parse_pr_gate(gate)
    assert spec is not None
    return await dispatch.consider(
        gate, spec, story, pr if pr is not None else _pr(), _ctx(client), hold=hold
    )


async def _settle(dispatch: MergeGateDispatch) -> None:
    await dispatch.drain()


# ── refusals that spawn nothing ────────────────────────────────────────


async def test_disabled_never_spawns(tmp_path: Path) -> None:
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    spawn, calls = _spawner(_record())
    dispatch = MergeGateDispatch(_settings(tmp_path, enabled=False), spawn=spawn)
    assert await _consider(client, gate, story, dispatch) == "disabled"
    assert calls == []
    assert read_record(gate, _PR_URL) is None


async def test_no_story_never_spawns(tmp_path: Path) -> None:
    # `--story` is how the run resolves the project's current config; a
    # gate with no waiter has nothing to resolve from and nowhere to report
    client = FakeLithosClient()
    _story, gate = await _gate_with_story(client)
    spawn, calls = _spawner(_record())
    dispatch = MergeGateDispatch(_settings(tmp_path), spawn=spawn)
    assert await _consider(client, gate, None, dispatch) == "no_story"
    assert calls == []


async def test_unknown_shas_wait_for_github(tmp_path: Path) -> None:
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    spawn, calls = _spawner(_record())
    dispatch = MergeGateDispatch(_settings(tmp_path), spawn=spawn)
    assert (
        await _consider(client, gate, story, dispatch, pr=_pr(head="", base=""))
        == "unknown_shas"
    )
    assert calls == []


async def test_fork_is_recorded_once_and_never_fetched(tmp_path: Path) -> None:
    # PRD S3: a third-party head is never pulled into the operator's checkout
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    spawn, calls = _spawner(_record())
    dispatch = MergeGateDispatch(_settings(tmp_path), spawn=spawn)
    fork = _pr(head_repo="someone/lithos-lens")

    assert await _consider(client, gate, story, dispatch, pr=fork) == "fork_unsupported"
    assert calls == []
    (finding,) = _findings(client)  # on the story, not just the host log
    assert finding.startswith("[Friction] merge-gate") and "fork" in finding
    gate = await _refresh(client, gate.id)
    record = read_record(gate, _PR_URL)
    assert record is not None
    assert record.status == "fork_unsupported"
    assert (record.head_sha, record.base_sha) == (_HEAD, _BASE)

    # same shas next sweep: nothing to say again
    assert await _consider(client, gate, story, dispatch, pr=fork) == "unchanged"
    assert calls == [] and len(_findings(client)) == 1


async def test_no_project_posts_friction_on_the_story_once(tmp_path: Path) -> None:
    # PRD S3 + PR #362 review F3: an unmapped project is a one-shot [Friction]
    # ON THE STORY (the operator action lives in Lithos / Lens, not the host
    # log), and the record keeps it one-shot.
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client, project=None, story_project=None)
    spawn, calls = _spawner(_record())
    dispatch = MergeGateDispatch(_settings(tmp_path), spawn=spawn)

    assert await _consider(client, gate, story, dispatch) == "no_project"
    assert calls == []
    (finding,) = _findings(client)
    assert finding.startswith("[Friction] merge-gate")
    assert "[projects]" in finding and "metadata.project" in finding
    assert story in finding and _PR_URL in finding
    gate = await _refresh(client, gate.id)
    record = read_record(gate, _PR_URL)
    assert record is not None and record.status == "no_project"
    assert await _consider(client, gate, story, dispatch) == "unchanged"
    assert len(_findings(client)) == 1


async def test_a_project_mapped_later_re_gates_on_the_same_key(tmp_path: Path) -> None:
    # the no_project record is a one-shot friction, not a verdict: once the
    # operator maps the project (and restarts), the same shas must gate
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client, project="unmapped", story_project=None)
    spawn, calls = _spawner(_record("green"))
    dispatch = MergeGateDispatch(_settings(tmp_path), spawn=spawn)
    assert await _consider(client, gate, story, dispatch) == "no_project"
    gate = await _refresh(client, gate.id)
    assert await _consider(client, gate, story, dispatch) == "unchanged"

    mapped = MergeGateDispatch(
        _settings(tmp_path, projects={"unmapped": tmp_path / "repo"}), spawn=spawn
    )
    assert await _consider(client, gate, story, mapped) == "dispatched"
    await _settle(mapped)
    assert len(_runs(calls)) == 1
    assert _probes(calls) == []
    record = read_record(await _refresh(client, gate.id), _PR_URL)
    assert record is not None and record.status == "green" and record.attempts == 1


async def test_project_falls_back_to_the_story_when_the_gate_lacks_it(
    tmp_path: Path,
) -> None:
    # gate creation records `project` only when the payload carried it
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client, project=None, story_project="p")
    spawn, calls = _spawner(_record())
    dispatch = MergeGateDispatch(_settings(tmp_path), spawn=spawn)
    assert await _consider(client, gate, story, dispatch) == "dispatched"
    await _settle(dispatch)
    (cmd,) = _runs(calls)
    assert str(tmp_path / "repo") in cmd


@pytest.mark.parametrize(
    ("value", "label"),
    [(False, "project_disabled"), ("false", "project_disabled"), (True, "dispatched")],
)
async def test_per_project_dial(tmp_path: Path, value: Any, label: str) -> None:
    client = FakeLithosClient(
        notes=(
            make_note(
                "projects/p/p-project-context.md",
                path="projects/p/p-project-context.md",
                tags=("project-context",),
                metadata={MERGE_GATE_SETTING: value},
            ),
        )
    )
    story, gate = await _gate_with_story(client)
    spawn, calls = _spawner(_record())
    dispatch = MergeGateDispatch(_settings(tmp_path), spawn=spawn)
    assert await _consider(client, gate, story, dispatch) == label
    await _settle(dispatch)
    assert bool(_runs(calls)) is (label == "dispatched")


async def test_unreadable_project_doc_fails_closed_and_retries(tmp_path: Path) -> None:
    from lithos_loom.errors import LithosClientError

    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    client.raise_on["note_read"] = LithosClientError("internal", "boom")
    spawn, calls = _spawner(_record())
    dispatch = MergeGateDispatch(_settings(tmp_path), spawn=spawn)
    assert (
        await _consider(client, gate, story, dispatch) == "project_settings_unavailable"
    )
    assert calls == []
    assert read_record(await _refresh(client, gate.id), _PR_URL) is None


# ── the run itself ─────────────────────────────────────────────────────


async def test_first_sighting_dispatches_with_the_story_and_records_green(
    tmp_path: Path,
) -> None:
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    spawn, calls = _spawner(_record("green"))
    dispatch = MergeGateDispatch(_settings(tmp_path), spawn=spawn)

    assert await _consider(client, gate, story, dispatch) == "dispatched"
    assert dispatch.busy_on(_PR_URL) is True
    await _settle(dispatch)
    assert dispatch.busy_on(_PR_URL) is False

    assert _probes(calls) == []  # a first sighting needs no probe
    (cmd,) = _runs(calls)
    assert cmd[:3] == [sys.executable, "-m", "lithos_loom"]
    assert cmd[3:5] == ["develop", "merge-gate"]
    assert "62" in cmd
    assert cmd[cmd.index("--story") + 1] == story
    assert cmd[cmd.index("--repo") + 1] == str(tmp_path / "repo")
    assert cmd[cmd.index("--config") + 1] == str(tmp_path / "host.toml")
    assert "--no-push" not in cmd  # additive is automatic (ADR 0011 decision 2)

    gate = await _refresh(client, gate.id)
    record = read_record(gate, _PR_URL)
    assert record is not None
    assert record.status == "green"
    assert record.verdict == "GREEN"
    assert (record.head_sha, record.base_sha) == (_HEAD, _BASE)
    assert record.settings_fingerprint == _FP
    assert record.merge_sha == _MERGE
    assert _findings(client) == []  # green is a record, not chatter


async def test_a_loom_push_is_recorded_on_the_remediation_budget(
    tmp_path: Path,
) -> None:
    # observe_head reads any head that isn't loom's recorded push as a HUMAN
    # push and resets the S5b budget — the merge commit must be loom's own.
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    await client.task_update(
        task_id=gate.id,
        metadata={
            REMEDIATION_KEY: RemediationBudget(
                pr_url=_PR_URL, rounds_used=1, last_seen_head_sha=_HEAD
            ).as_marker()
        },
    )
    gate = await _refresh(client, gate.id)
    spawn, _calls = _spawner(_record("green", pushed=True))
    dispatch = MergeGateDispatch(_settings(tmp_path), spawn=spawn)
    assert await _consider(client, gate, story, dispatch) == "dispatched"
    await _settle(dispatch)

    gate = await _refresh(client, gate.id)
    budget = read_budget(gate, _PR_URL)
    assert budget.last_loom_pushed_sha == _MERGE
    assert budget.rounds_used == 1  # the existing state is merged into, not replaced
    record = read_record(gate, _PR_URL)
    assert record is not None and record.pushed_sha == _MERGE


async def test_a_loom_push_is_recorded_even_when_the_fresh_gate_read_fails(
    tmp_path: Path,
) -> None:
    # PRD S3 review: the push has HAPPENED by the time the record is written;
    # a transient Lithos error on the re-read must not turn a pushed green
    # into a bare "crashed" record with no loom sha — the next sweep would
    # read the merge commit as a human push and reset the S5b budget.
    from lithos_loom.errors import LithosClientError

    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    await client.task_update(
        task_id=gate.id,
        metadata={
            REMEDIATION_KEY: RemediationBudget(
                pr_url=_PR_URL, rounds_used=1, last_seen_head_sha=_HEAD
            ).as_marker()
        },
    )
    gate = await _refresh(client, gate.id)
    spawn, _calls = _spawner(_record("green", pushed=True))
    dispatch = MergeGateDispatch(_settings(tmp_path), spawn=spawn)
    assert await _consider(client, gate, story, dispatch) == "dispatched"
    client.raise_on["task_get"] = LithosClientError("internal", "blip")
    await _settle(dispatch)
    del client.raise_on["task_get"]

    gate = await _refresh(client, gate.id)
    record = read_record(gate, _PR_URL)
    assert record is not None
    assert record.status == "green" and record.pushed_sha == _MERGE
    budget = read_budget(gate, _PR_URL)
    assert budget.last_loom_pushed_sha == _MERGE
    assert budget.rounds_used == 1
    assert _findings(client) == []


async def test_unchanged_shas_probe_the_settings_and_skip_when_equal(
    tmp_path: Path,
) -> None:
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    spawn, calls = _spawner(_record("green"), probe=_probe(_FP))
    dispatch = MergeGateDispatch(_settings(tmp_path), spawn=spawn)
    assert await _consider(client, gate, story, dispatch) == "dispatched"
    await _settle(dispatch)
    gate = await _refresh(client, gate.id)

    # PR #362 review F5: the probe never blocks the sweep — it runs in the
    # background and only a changed fingerprint starts a run
    assert await _consider(client, gate, story, dispatch) == "probing"
    await _settle(dispatch)
    assert len(_runs(calls)) == 1
    (probe,) = _probes(calls)
    assert "--resolve-only" in probe and "--story" in probe
    assert cmd_has_no_push_or_fetch(probe)
    assert not dispatch.busy_on(_PR_URL)


def cmd_has_no_push_or_fetch(cmd: list[str]) -> bool:
    return "--resolve-only" in cmd and "--no-push" not in cmd


async def test_a_changed_settings_fingerprint_re_gates(tmp_path: Path) -> None:
    # PRD S3: "changing the project's check-set config must invalidate a
    # result" — the case re-resolving the current config exists to catch
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    spawn, calls = _spawner(
        _record("green", fp="new-fingerprint"), probe=_probe("new-fingerprint")
    )
    dispatch = MergeGateDispatch(_settings(tmp_path), spawn=spawn)
    await client.task_update(
        task_id=gate.id,
        metadata={
            MERGE_GATE_KEY: {
                "pr_url": _PR_URL,
                "head_sha": _HEAD,
                "base_sha": _BASE,
                "settings_fingerprint": _FP,
                "status": "green",
                "attempts": 1,
            }
        },
    )
    gate = await _refresh(client, gate.id)

    assert await _consider(client, gate, story, dispatch) == "probing"
    await _settle(dispatch)
    assert len(_probes(calls)) == 1 and len(_runs(calls)) == 1
    record = read_record(await _refresh(client, gate.id), _PR_URL)
    assert record is not None and record.settings_fingerprint == "new-fingerprint"


async def test_a_base_move_re_gates_without_a_probe(tmp_path: Path) -> None:
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    spawn, calls = _spawner(_record("green"), probe=_probe(_FP))
    dispatch = MergeGateDispatch(_settings(tmp_path), spawn=spawn)
    assert await _consider(client, gate, story, dispatch) == "dispatched"
    await _settle(dispatch)
    gate = await _refresh(client, gate.id)

    moved = _pr(base="c" * 40)
    assert await _consider(client, gate, story, dispatch, pr=moved) == "dispatched"
    await _settle(dispatch)
    assert _probes(calls) == []
    assert len(_runs(calls)) == 2
    record = read_record(await _refresh(client, gate.id), _PR_URL)
    assert record is not None
    # the record carries the shas the sweep SAW, so the key compares next pass
    assert record.base_sha == "c" * 40


async def test_a_replacement_pr_re_evaluates_from_scratch(tmp_path: Path) -> None:
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    await client.task_update(
        task_id=gate.id,
        metadata={
            MERGE_GATE_KEY: {
                "pr_url": "https://github.com/agent-lore/lithos-lens/pull/1",
                "head_sha": _HEAD,
                "base_sha": _BASE,
                "settings_fingerprint": _FP,
                "status": "conflict",
                "attempts": 1,
            }
        },
    )
    gate = await _refresh(client, gate.id)
    spawn, calls = _spawner(_record("green"))
    dispatch = MergeGateDispatch(_settings(tmp_path), spawn=spawn)
    assert await _consider(client, gate, story, dispatch) == "dispatched"
    await _settle(dispatch)
    assert len(_runs(calls)) == 1


# ── outcomes on the story ──────────────────────────────────────────────


async def test_red_posts_merge_gate_failed_naming_the_check_once(
    tmp_path: Path,
) -> None:
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    spawn, calls = _spawner(_record("red"), probe=_probe(_FP))
    dispatch = MergeGateDispatch(_settings(tmp_path), spawn=spawn)
    assert await _consider(client, gate, story, dispatch) == "dispatched"
    await _settle(dispatch)

    (finding,) = _findings(client)
    assert finding.startswith(MERGE_GATE_FAILED)
    assert "test" in finding and "make test" in finding
    assert _PR_URL in finding and _MERGE[:12] in finding
    assert "RED" in finding
    gate = await _refresh(client, gate.id)
    record = read_record(gate, _PR_URL)
    assert record is not None and record.status == "red" and record.verdict == "RED"

    # same key next sweep: probed, unchanged, nothing re-posted
    assert await _consider(client, gate, story, dispatch) == "probing"
    await _settle(dispatch)
    assert len(_runs(calls)) == 1
    assert len(_findings(client)) == 1


async def test_errored_posts_merge_gate_failed_naming_the_unverified_check(
    tmp_path: Path,
) -> None:
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    checks = [
        {
            "name": "typecheck",
            "command": "uv run pyright",
            "state": "required",
            "stage": "fast",
            "outcome": "errored",
            "passed": False,
            "exit_code": None,
            "timed_out": False,
            "output_tail": "docker: image not found",
        }
    ]
    spawn, _calls = _spawner(_record("errored", verdict=None, checks=checks))
    dispatch = MergeGateDispatch(_settings(tmp_path), spawn=spawn)
    assert await _consider(client, gate, story, dispatch) == "dispatched"
    await _settle(dispatch)
    (finding,) = _findings(client)
    assert finding.startswith(MERGE_GATE_FAILED)
    assert "typecheck" in finding and "errored" in finding
    assert "not verified" in finding or "unverified" in finding


async def test_conflict_widens_pr_conflicted_with_the_paths_once(
    tmp_path: Path,
) -> None:
    # PRD S3: the trial merge is the ONLY source of the conflicting paths
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    spawn, calls = _spawner(
        _record("conflict", paths=["src/a.py", "docs/b.md"]), probe=_probe(_FP)
    )
    dispatch = MergeGateDispatch(_settings(tmp_path), spawn=spawn)
    assert await _consider(client, gate, story, dispatch) == "dispatched"
    await _settle(dispatch)

    (finding,) = _findings(client)
    assert finding.startswith(PR_CONFLICTED)
    assert "merge-gate" in finding
    assert "src/a.py" in finding and "docs/b.md" in finding
    assert "never rebase" in finding
    gate = await _refresh(client, gate.id)
    record = read_record(gate, _PR_URL)
    assert record is not None and record.status == "conflict"

    # a conflict does not depend on the settings: no probe, no re-run
    assert await _consider(client, gate, story, dispatch) == "unchanged"
    assert _probes(calls) == []
    assert len(_findings(client)) == 1


async def test_no_checks_records_without_chatter(tmp_path: Path) -> None:
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    spawn, _calls = _spawner(_record("no_checks", verdict=None))
    dispatch = MergeGateDispatch(_settings(tmp_path), spawn=spawn)
    assert await _consider(client, gate, story, dispatch) == "dispatched"
    await _settle(dispatch)
    assert _findings(client) == []
    record = read_record(await _refresh(client, gate.id), _PR_URL)
    assert record is not None and record.status == "no_checks"


async def test_config_unresolved_posts_friction_once(tmp_path: Path) -> None:
    # PRD S3: an unresolvable project is skipped with a one-shot [Friction],
    # never silently — and never gated with built-ins
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    spawn, calls = _spawner(None, rc=4, probe=None, probe_rc=4)
    dispatch = MergeGateDispatch(_settings(tmp_path), spawn=spawn)
    assert await _consider(client, gate, story, dispatch) == "dispatched"
    await _settle(dispatch)

    (finding,) = _findings(client)
    assert finding.startswith("[Friction] merge-gate")
    assert "config" in finding and story in finding
    gate = await _refresh(client, gate.id)
    record = read_record(gate, _PR_URL)
    assert record is not None and record.status == "config_unresolved"

    # still unresolved next sweep (probe exits 4): nothing re-posted
    assert await _consider(client, gate, story, dispatch) == "probing"
    await _settle(dispatch)
    assert len(_findings(client)) == 1
    assert len(_probes(calls)) == 1 and len(_runs(calls)) == 1


async def test_config_resolving_again_re_gates(tmp_path: Path) -> None:
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    await client.task_update(
        task_id=gate.id,
        metadata={
            MERGE_GATE_KEY: {
                "pr_url": _PR_URL,
                "head_sha": _HEAD,
                "base_sha": _BASE,
                "settings_fingerprint": "",
                "status": "config_unresolved",
                "attempts": 1,
            }
        },
    )
    gate = await _refresh(client, gate.id)
    spawn, calls = _spawner(_record("green"), probe=_probe(_FP))
    dispatch = MergeGateDispatch(_settings(tmp_path), spawn=spawn)
    assert await _consider(client, gate, story, dispatch) == "probing"
    await _settle(dispatch)
    assert len(_runs(calls)) == 1


async def test_a_crash_posts_friction_and_retries_once_then_waits(
    tmp_path: Path,
) -> None:
    # no JSON and a non-zero exit: the CLI died before recording anything
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    spawn, calls = _spawner(None, rc=1, probe=_probe(_FP))
    dispatch = MergeGateDispatch(_settings(tmp_path), spawn=spawn)

    assert await _consider(client, gate, story, dispatch) == "dispatched"
    await _settle(dispatch)
    (finding,) = _findings(client)
    assert finding.startswith("[Friction] merge-gate")
    assert "exit 1" in finding
    gate = await _refresh(client, gate.id)
    record = read_record(gate, _PR_URL)
    assert record is not None
    assert record.status == "crashed" and record.attempts == 1

    # one retry on the same key (a transient host problem heals itself)...
    assert await _consider(client, gate, story, dispatch) == "dispatched"
    await _settle(dispatch)
    gate = await _refresh(client, gate.id)
    record = read_record(gate, _PR_URL)
    assert record is not None and record.attempts == 2
    assert len(_findings(client)) == 2

    # ...then it waits for the key to move (no hourly crash loop)
    assert await _consider(client, gate, story, dispatch) == "unchanged"
    assert len(_runs(calls)) == 2
    assert await _consider(client, gate, story, dispatch, pr=_pr(head="n" * 40)) == (
        "dispatched"
    )
    await _settle(dispatch)
    record = read_record(await _refresh(client, gate.id), _PR_URL)
    assert record is not None and record.attempts == 1  # a fresh key


async def test_a_zero_exit_without_a_record_is_a_crash_too(tmp_path: Path) -> None:
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    spawn, _calls = _spawner(None, rc=0)
    dispatch = MergeGateDispatch(_settings(tmp_path), spawn=spawn)
    assert await _consider(client, gate, story, dispatch) == "dispatched"
    await _settle(dispatch)
    (finding,) = _findings(client)
    assert finding.startswith("[Friction] merge-gate")
    record = read_record(await _refresh(client, gate.id), _PR_URL)
    assert record is not None and record.status == "crashed"


async def test_pr_closed_from_the_run_writes_nothing(tmp_path: Path) -> None:
    # the sweep's own merge poll owns the closed / merged states; the run
    # saying so is a race, not an event — and a record keyed on unchanged
    # shas would freeze a PR reopened without a push (self-review)
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    spawn, calls = _spawner(_record("pr_closed", verdict=None), rc=2)
    dispatch = MergeGateDispatch(_settings(tmp_path), spawn=spawn)
    assert await _consider(client, gate, story, dispatch) == "dispatched"
    await _settle(dispatch)
    assert _findings(client) == []
    assert read_record(await _refresh(client, gate.id), _PR_URL) is None
    # the sweep only asks again while GitHub says the PR is open, and then
    # the run decides afresh
    assert await _consider(client, gate, story, dispatch) == "dispatched"
    await _settle(dispatch)
    assert len(_runs(calls)) == 2


async def test_a_failed_probe_retries_next_sweep(tmp_path: Path) -> None:
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    await client.task_update(
        task_id=gate.id,
        metadata={
            MERGE_GATE_KEY: {
                "pr_url": _PR_URL,
                "head_sha": _HEAD,
                "base_sha": _BASE,
                "settings_fingerprint": _FP,
                "status": "green",
                "attempts": 1,
            }
        },
    )
    gate = await _refresh(client, gate.id)
    spawn, calls = _spawner(_record("green"), probe=None, probe_rc=1)
    dispatch = MergeGateDispatch(_settings(tmp_path), spawn=spawn)
    assert await _consider(client, gate, story, dispatch) == "probing"
    await _settle(dispatch)
    assert _runs(calls) == []
    assert len(_findings(client)) == 0
    # a failed probe writes nothing; the next sweep simply probes again
    assert await _consider(client, gate, story, dispatch) == "probing"
    await _settle(dispatch)
    assert len(_probes(calls)) == 2 and _runs(calls) == []


async def test_a_failed_push_is_a_friction_and_a_bounded_retry(tmp_path: Path) -> None:
    # PR #362 review F1: `run_merge_gate` reports a push failure BESIDE a
    # green verdict (behind + pushed=false + push_error). Recording that as
    # a settled green left the PR behind forever with nothing on the story.
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    spawn, calls = _spawner(
        _record("green", pushed=False, push_error="remote rejected: lease"),
        probe=_probe(_FP),
    )
    dispatch = MergeGateDispatch(_settings(tmp_path), spawn=spawn)

    assert await _consider(client, gate, story, dispatch) == "dispatched"
    await _settle(dispatch)
    (finding,) = _findings(client)
    assert finding.startswith("[Friction] merge-gate")
    assert "remote rejected: lease" in finding and "green" in finding.lower()
    gate = await _refresh(client, gate.id)
    record = read_record(gate, _PR_URL)
    assert record is not None
    assert record.status == "push_failed" and record.verdict == "GREEN"
    assert record.behind is True and record.pushed_sha == ""
    assert record.push_error == "remote rejected: lease" and record.attempts == 1
    assert read_budget(gate, _PR_URL).last_loom_pushed_sha == ""  # nothing pushed

    # one bounded retry on the same key...
    assert await _consider(client, gate, story, dispatch) == "dispatched"
    await _settle(dispatch)
    gate = await _refresh(client, gate.id)
    record = read_record(gate, _PR_URL)
    assert record is not None and record.attempts == 2
    assert len(_findings(client)) == 2

    # ...then the verdict stands and only a settings change or a move re-gates
    assert await _consider(client, gate, story, dispatch) == "probing"
    await _settle(dispatch)
    assert len(_runs(calls)) == 2 and len(_probes(calls)) == 1
    assert await _consider(client, gate, story, dispatch, pr=_pr(base="c" * 40)) == (
        "dispatched"
    )
    await _settle(dispatch)
    assert len(_runs(calls)) == 3


async def test_a_retried_push_that_lands_records_green(tmp_path: Path) -> None:
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    await client.task_update(
        task_id=gate.id,
        metadata={
            MERGE_GATE_KEY: {
                "pr_url": _PR_URL,
                "head_sha": _HEAD,
                "base_sha": _BASE,
                "settings_fingerprint": _FP,
                "status": "push_failed",
                "verdict": "GREEN",
                "behind": True,
                "push_error": "network",
                "attempts": 1,
            }
        },
    )
    gate = await _refresh(client, gate.id)
    spawn, _calls = _spawner(_record("green", pushed=True))
    dispatch = MergeGateDispatch(_settings(tmp_path), spawn=spawn)
    assert await _consider(client, gate, story, dispatch) == "dispatched"
    await _settle(dispatch)
    gate = await _refresh(client, gate.id)
    record = read_record(gate, _PR_URL)
    assert record is not None and record.status == "green"
    assert record.pushed_sha == _MERGE and record.push_error == ""
    assert read_budget(gate, _PR_URL).last_loom_pushed_sha == _MERGE


async def test_an_up_to_date_green_is_not_a_push_failure(tmp_path: Path) -> None:
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    spawn, _calls = _spawner(_record("green", pushed=False, behind=False))
    dispatch = MergeGateDispatch(_settings(tmp_path), spawn=spawn)
    assert await _consider(client, gate, story, dispatch) == "dispatched"
    await _settle(dispatch)
    record = read_record(await _refresh(client, gate.id), _PR_URL)
    assert record is not None and record.status == "green"
    assert _findings(client) == []


async def test_repo_mismatch_posts_friction_and_is_bounded(tmp_path: Path) -> None:
    # PR #362 review F2: the CLI refused to act on a checkout whose origin is
    # not the gate's repo; the operator must hear it, and the sweep must not
    # spawn it hourly forever.
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    spawn, calls = _spawner(
        {
            "status": "repo_mismatch",
            "expected_repo": "agent-lore/lithos-lens",
            "actual_repo": "agent-lore/other",
            "message": "checkout is agent-lore/other",
        },
        rc=2,
    )
    dispatch = MergeGateDispatch(_settings(tmp_path), spawn=spawn)
    assert await _consider(client, gate, story, dispatch) == "dispatched"
    await _settle(dispatch)
    (finding,) = _findings(client)
    assert finding.startswith("[Friction] merge-gate")
    assert "agent-lore/lithos-lens" in finding and "agent-lore/other" in finding
    assert "[projects." in finding
    record = read_record(await _refresh(client, gate.id), _PR_URL)
    assert record is not None and record.status == "repo_mismatch"

    gate = await _refresh(client, gate.id)
    assert await _consider(client, gate, story, dispatch) == "dispatched"  # once more
    await _settle(dispatch)
    gate = await _refresh(client, gate.id)
    assert await _consider(client, gate, story, dispatch) == "unchanged"
    assert len(_runs(calls)) == 2 and _probes(calls) == []


async def test_a_mismatched_checkout_is_refused_before_any_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # PR #362 re-review 2: the cheap origin read runs in the sweep, so a
    # stale mapping costs no subprocess — one [Friction], then quiet until
    # the mapping (path or origin) changes, at which point the same shas gate.
    from lithos_loom.subscriptions import merge_gate_dispatch as mod

    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    origin = {"repo": "agent-lore/other"}

    async def fake_origin(path: Path) -> str | None:
        return origin["repo"]

    monkeypatch.setattr(mod, "origin_repo", fake_origin)
    spawn, calls = _spawner(_record("green"))
    dispatch = MergeGateDispatch(_settings(tmp_path), spawn=spawn)

    assert await _consider(client, gate, story, dispatch) == "repo_mismatch"
    assert calls == []
    (finding,) = _findings(client)
    assert finding.startswith("[Friction] merge-gate")
    assert "agent-lore/other" in finding and "[projects." in finding
    gate = await _refresh(client, gate.id)
    record = read_record(gate, _PR_URL)
    assert record is not None and record.status == "repo_mismatch"
    assert record.actual_repo == "agent-lore/other"
    assert record.repo_path == str(tmp_path / "repo")

    assert await _consider(client, gate, story, dispatch) == "unchanged"
    assert len(_findings(client)) == 1

    # the origin is fixed in place: the same shas gate
    origin["repo"] = "Agent-Lore/Lithos-Lens"
    assert await _consider(client, gate, story, dispatch) == "dispatched"
    await _settle(dispatch)
    assert len(_runs(calls)) == 1
    record = read_record(await _refresh(client, gate.id), _PR_URL)
    assert record is not None and record.status == "green" and record.attempts == 1


async def test_a_settled_cli_mismatch_re_gates_once_the_mapping_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # PR #362 re-review 2 F1: after the bounded retries a `repo_mismatch`
    # record waited for a sha move — fixing [projects.<slug>].repo and
    # restarting did nothing for the unchanged PR. The mapped checkout is
    # part of the record: a different path, or an origin that now matches,
    # is a fresh key.
    from lithos_loom.subscriptions import merge_gate_dispatch as mod

    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    await client.task_update(
        task_id=gate.id,
        metadata={
            MERGE_GATE_KEY: {
                "pr_url": _PR_URL,
                "head_sha": _HEAD,
                "base_sha": _BASE,
                "settings_fingerprint": "",
                "status": "repo_mismatch",
                "attempts": 2,
                "repo_path": str(tmp_path / "repo"),
                "actual_repo": "agent-lore/other",
            }
        },
    )
    gate = await _refresh(client, gate.id)

    async def unknown_origin(path: Path) -> str | None:
        return None  # the cheap read cannot answer (as in every other test)

    monkeypatch.setattr(mod, "origin_repo", unknown_origin)
    spawn, calls = _spawner(_record("green"))
    dispatch = MergeGateDispatch(_settings(tmp_path), spawn=spawn)
    assert await _consider(client, gate, story, dispatch) == "unchanged"
    assert calls == []

    # the operator remapped the project to another checkout and restarted
    remapped = MergeGateDispatch(
        _settings(tmp_path, projects={"p": tmp_path / "repo-fixed"}), spawn=spawn
    )
    assert await _consider(client, gate, story, remapped) == "dispatched"
    await _settle(remapped)
    assert len(_runs(calls)) == 1
    record = read_record(await _refresh(client, gate.id), _PR_URL)
    assert record is not None and record.status == "green" and record.attempts == 1


async def test_a_cli_refusal_settles_even_when_the_cheap_read_matches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # self-review: the sweep's origin read and the CLI's gh-based check can
    # disagree (a rename gh follows; a shape the read cannot parse). With
    # "cheap read matches" treated as a fresh key, that looped a spawn and a
    # [Friction] every sweep. The settle key is what the SWEEP observes —
    # the mapped path and its own origin read — never the CLI's verdict.
    from lithos_loom.subscriptions import merge_gate_dispatch as mod

    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    origin = {"repo": "agent-lore/lithos-lens"}

    async def fake_origin(path: Path) -> str | None:
        return origin["repo"]

    monkeypatch.setattr(mod, "origin_repo", fake_origin)
    spawn, calls = _spawner(
        {
            "status": "repo_mismatch",
            "expected_repo": "agent-lore/lithos-lens",
            "actual_repo": "agent-lore/renamed",
            "message": "gh says renamed",
        },
        rc=2,
    )
    dispatch = MergeGateDispatch(_settings(tmp_path), spawn=spawn)
    for expected_attempt in (1, 2):
        gate = await _refresh(client, gate.id)
        assert await _consider(client, gate, story, dispatch) == "dispatched"
        await _settle(dispatch)
        record = read_record(await _refresh(client, gate.id), _PR_URL)
        assert record is not None
        assert record.status == "repo_mismatch" and record.attempts == expected_attempt
        assert record.origin_seen == "agent-lore/lithos-lens"
    gate = await _refresh(client, gate.id)
    assert await _consider(client, gate, story, dispatch) == "unchanged"
    assert await _consider(client, gate, story, dispatch) == "unchanged"
    assert len(_runs(calls)) == 2 and len(_findings(client)) == 2

    # the operator changed the remote url: the sweep's read moves → one
    # fresh, bounded attempt
    origin["repo"] = "agent-lore/renamed"
    gate = await _refresh(client, gate.id)
    assert (
        await _consider(client, gate, story, dispatch) == "repo_mismatch"
    )  # pre-check
    assert len(_runs(calls)) == 2


async def test_a_stale_probe_never_starts_a_run_for_a_superseded_key(
    tmp_path: Path,
) -> None:
    # PR #362 re-review 2 F3: a probe captured (head, base) when scheduled;
    # if the key moved and a newer run finished meanwhile, the old probe
    # must not start a run that overwrites the newer record.
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    await client.task_update(
        task_id=gate.id,
        metadata={
            MERGE_GATE_KEY: {
                "pr_url": _PR_URL,
                "head_sha": _HEAD,
                "base_sha": _BASE,
                "settings_fingerprint": "stale",
                "status": "green",
                "attempts": 1,
            }
        },
    )
    gate = await _refresh(client, gate.id)
    release = asyncio.Event()
    runs: list[str] = []

    async def spawn(cmd: list[str]) -> tuple[int, str]:
        path = Path(cmd[cmd.index("--json") + 1])
        path.parent.mkdir(parents=True, exist_ok=True)
        if "--resolve-only" in cmd:
            await release.wait()
            path.write_text(json.dumps(_probe(_FP)), encoding="utf-8")
            return 0, ""
        runs.append(cmd[cmd.index("--story") + 1])
        path.write_text(json.dumps(_record("green")), encoding="utf-8")
        return 0, ""

    dispatch = MergeGateDispatch(_settings(tmp_path), spawn=spawn)
    assert await _consider(client, gate, story, dispatch) == "probing"
    await asyncio.sleep(0)
    # the head moved: a new-key run dispatches and finishes while the old
    # probe is still out
    moved = _pr(head="n" * 40)
    assert await _consider(client, gate, story, dispatch, pr=moved) == "dispatched"
    await asyncio.sleep(0.01)
    assert len(runs) == 1
    release.set()
    await _settle(dispatch)
    assert len(runs) == 1  # the stale probe started nothing
    record = read_record(await _refresh(client, gate.id), _PR_URL)
    assert record is not None and record.head_sha == "n" * 40


async def test_a_probe_that_outlives_its_record_starts_nothing(tmp_path: Path) -> None:
    # the same guard without the cancel: the record was replaced by someone
    # else (a newer key written) while the probe was out
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    await client.task_update(
        task_id=gate.id,
        metadata={
            MERGE_GATE_KEY: {
                "pr_url": _PR_URL,
                "head_sha": _HEAD,
                "base_sha": _BASE,
                "settings_fingerprint": "stale",
                "status": "green",
                "attempts": 1,
            }
        },
    )
    gate = await _refresh(client, gate.id)
    release = asyncio.Event()
    spawn_calls: list[list[str]] = []

    async def spawn(cmd: list[str]) -> tuple[int, str]:
        spawn_calls.append(cmd)
        path = Path(cmd[cmd.index("--json") + 1])
        path.parent.mkdir(parents=True, exist_ok=True)
        await release.wait()
        path.write_text(json.dumps(_probe(_FP)), encoding="utf-8")
        return 0, ""

    dispatch = MergeGateDispatch(_settings(tmp_path), spawn=spawn)
    assert await _consider(client, gate, story, dispatch) == "probing"
    await asyncio.sleep(0)
    await client.task_update(
        task_id=gate.id,
        metadata={
            MERGE_GATE_KEY: {
                "pr_url": _PR_URL,
                "head_sha": "n" * 40,
                "base_sha": _BASE,
                "settings_fingerprint": _FP,
                "status": "green",
                "attempts": 1,
            }
        },
    )
    release.set()
    await _settle(dispatch)
    assert len(_runs(spawn_calls)) == 0


async def test_the_run_argv_pins_the_gates_repo(tmp_path: Path) -> None:
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    spawn, calls = _spawner(_record("green"), probe=_probe(_FP))
    dispatch = MergeGateDispatch(_settings(tmp_path), spawn=spawn)
    assert await _consider(client, gate, story, dispatch) == "dispatched"
    await _settle(dispatch)
    gate = await _refresh(client, gate.id)
    assert await _consider(client, gate, story, dispatch) == "probing"
    await _settle(dispatch)
    for cmd in calls:  # the run AND the probe
        assert cmd[cmd.index("--expect-repo") + 1] == "agent-lore/lithos-lens"


async def test_a_slow_probe_never_blocks_the_sweep(tmp_path: Path) -> None:
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    await client.task_update(
        task_id=gate.id,
        metadata={
            MERGE_GATE_KEY: {
                "pr_url": _PR_URL,
                "head_sha": _HEAD,
                "base_sha": _BASE,
                "settings_fingerprint": _FP,
                "status": "green",
                "attempts": 1,
            }
        },
    )
    gate = await _refresh(client, gate.id)
    release = asyncio.Event()
    probes = 0

    async def spawn(cmd: list[str]) -> tuple[int, str]:
        nonlocal probes
        assert "--resolve-only" in cmd
        probes += 1
        await release.wait()
        path = Path(cmd[cmd.index("--json") + 1])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(_probe(_FP)), encoding="utf-8")
        return 0, ""

    dispatch = MergeGateDispatch(_settings(tmp_path), spawn=spawn)
    assert await _consider(client, gate, story, dispatch) == "probing"
    await asyncio.sleep(0)
    # a second sweep while the probe is still out: no second probe
    assert await _consider(client, gate, story, dispatch) == "probing"
    await asyncio.sleep(0)
    assert probes == 1
    release.set()
    await _settle(dispatch)


async def test_a_probe_that_finds_a_change_waits_for_a_busy_project_slot(
    tmp_path: Path,
) -> None:
    client = FakeLithosClient()
    story_a, gate_a = await _gate_with_story(client)
    story_b = await client.task_create(title="US8", metadata={"project": "p"})
    gate_b_id = await create_pr_gate(
        client,
        story_id=story_b,
        story_title="US8",
        pr_url="https://github.com/agent-lore/lithos-lens/pull/63",
        project="p",
        agent="a",
    )
    await client.task_update(
        task_id=gate_b_id,
        metadata={
            MERGE_GATE_KEY: {
                "pr_url": "https://github.com/agent-lore/lithos-lens/pull/63",
                "head_sha": _HEAD,
                "base_sha": _BASE,
                "settings_fingerprint": "stale",
                "status": "green",
                "attempts": 1,
            }
        },
    )
    gate_b = await _refresh(client, gate_b_id)
    release = asyncio.Event()
    runs: list[str] = []

    async def spawn(cmd: list[str]) -> tuple[int, str]:
        path = Path(cmd[cmd.index("--json") + 1])
        path.parent.mkdir(parents=True, exist_ok=True)
        if "--resolve-only" in cmd:
            path.write_text(json.dumps(_probe(_FP)), encoding="utf-8")
            return 0, ""
        runs.append(cmd[cmd.index("--story") + 1])
        await release.wait()
        path.write_text(json.dumps(_record("green")), encoding="utf-8")
        return 0, ""

    dispatch = MergeGateDispatch(_settings(tmp_path), spawn=spawn)
    assert await _consider(client, gate_a, story_a, dispatch) == "dispatched"
    # a busy slot defers BEFORE probing (a probe whose answer could not be
    # acted on is a wasted subprocess); the next sweep probes
    assert await _consider(client, gate_b, story_b, dispatch) == "deferred_busy"
    await asyncio.sleep(0.01)
    assert runs == [story_a]
    release.set()
    await _settle(dispatch)
    assert runs == [story_a]
    # the next sweep probes and, with the slot free, runs
    assert await _consider(client, gate_b, story_b, dispatch) == "probing"
    await _settle(dispatch)
    assert runs == [story_a, story_b]


async def test_a_probe_honours_the_hold_when_it_completes(tmp_path: Path) -> None:
    # self-review: the hold was checked when the probe was SCHEDULED; a
    # remediation dispatched on the same PR while the probe was out must
    # still hold the run the probe would start (either may push).
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    await client.task_update(
        task_id=gate.id,
        metadata={
            MERGE_GATE_KEY: {
                "pr_url": _PR_URL,
                "head_sha": _HEAD,
                "base_sha": _BASE,
                "settings_fingerprint": "stale",
                "status": "green",
                "attempts": 1,
            }
        },
    )
    gate = await _refresh(client, gate.id)
    release = asyncio.Event()
    runs = 0

    async def spawn(cmd: list[str]) -> tuple[int, str]:
        nonlocal runs
        path = Path(cmd[cmd.index("--json") + 1])
        path.parent.mkdir(parents=True, exist_ok=True)
        if "--resolve-only" in cmd:
            await release.wait()
            path.write_text(json.dumps(_probe(_FP)), encoding="utf-8")
            return 0, ""
        runs += 1
        path.write_text(json.dumps(_record("green")), encoding="utf-8")
        return 0, ""

    held = {"on": False}
    dispatch = MergeGateDispatch(
        _settings(tmp_path), spawn=spawn, hold=lambda url: held["on"]
    )
    assert await _consider(client, gate, story, dispatch) == "probing"
    await asyncio.sleep(0)
    held["on"] = True  # a converge started on this PR meanwhile
    release.set()
    await _settle(dispatch)
    assert runs == 0
    assert dispatch.pending_probes() == 0  # finished probes are pruned

    held["on"] = False
    assert await _consider(client, gate, story, dispatch) == "probing"
    await _settle(dispatch)
    assert runs == 1
    assert dispatch.pending_probes() == 0


async def test_shutdown_cancels_an_in_flight_probe(tmp_path: Path) -> None:
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    await client.task_update(
        task_id=gate.id,
        metadata={
            MERGE_GATE_KEY: {
                "pr_url": _PR_URL,
                "head_sha": _HEAD,
                "base_sha": _BASE,
                "settings_fingerprint": _FP,
                "status": "green",
                "attempts": 1,
            }
        },
    )
    gate = await _refresh(client, gate.id)
    cancelled = asyncio.Event()

    async def spawn(cmd: list[str]) -> tuple[int, str]:
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return 0, ""

    dispatch = MergeGateDispatch(_settings(tmp_path), spawn=spawn)
    assert await _consider(client, gate, story, dispatch) == "probing"
    await asyncio.sleep(0)
    await dispatch.shutdown()
    assert cancelled.is_set()


# ── concurrency ────────────────────────────────────────────────────────


async def test_one_in_flight_run_per_project(tmp_path: Path) -> None:
    client = FakeLithosClient()
    story_a, gate_a = await _gate_with_story(client)
    story_b = await client.task_create(title="US8", metadata={"project": "p"})
    gate_b_id = await create_pr_gate(
        client,
        story_id=story_b,
        story_title="US8",
        pr_url="https://github.com/agent-lore/lithos-lens/pull/63",
        project="p",
        agent="a",
    )
    gate_b = await _refresh(client, gate_b_id)
    story_q = await client.task_create(title="Q1", metadata={"project": "q"})
    gate_q_id = await create_pr_gate(
        client,
        story_id=story_q,
        story_title="Q1",
        pr_url="https://github.com/agent-lore/other/pull/5",
        project="q",
        agent="a",
    )
    gate_q = await _refresh(client, gate_q_id)

    release = asyncio.Event()
    started: list[str] = []

    async def spawn(cmd: list[str]) -> tuple[int, str]:
        started.append(cmd[cmd.index("--story") + 1])
        await release.wait()
        path = Path(cmd[cmd.index("--json") + 1])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(_record("green")), encoding="utf-8")
        return 0, ""

    dispatch = MergeGateDispatch(_settings(tmp_path), spawn=spawn)
    assert await _consider(client, gate_a, story_a, dispatch) == "dispatched"
    assert await _consider(client, gate_b, story_b, dispatch) == "deferred_busy"
    spec_q = parse_pr_gate(gate_q)
    assert spec_q is not None
    assert (
        await dispatch.consider(
            gate_q, spec_q, story_q, _pr(), _ctx(client), hold=False
        )
        == "dispatched"
    )
    await asyncio.sleep(0)
    assert sorted(started) == sorted([story_a, story_q])
    assert dispatch.busy_on(_PR_URL) is True
    release.set()
    await _settle(dispatch)

    # the deferred gate simply dispatches on a later sweep (no marker needed)
    assert await _consider(client, gate_b, story_b, dispatch) == "dispatched"
    await _settle(dispatch)


async def test_held_pr_defers_without_state(tmp_path: Path) -> None:
    # a remediation run in flight on this PR may push at any moment
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    spawn, calls = _spawner(_record("green"))
    dispatch = MergeGateDispatch(_settings(tmp_path), spawn=spawn)
    assert await _consider(client, gate, story, dispatch, hold=True) == (
        "deferred_remediation"
    )
    assert calls == []
    assert read_record(await _refresh(client, gate.id), _PR_URL) is None
    assert await _consider(client, gate, story, dispatch) == "dispatched"
    await _settle(dispatch)


async def test_shutdown_cancels_the_in_flight_runs(tmp_path: Path) -> None:
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    cancelled = asyncio.Event()

    async def spawn(cmd: list[str]) -> tuple[int, str]:
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return 0, ""

    dispatch = MergeGateDispatch(_settings(tmp_path), spawn=spawn)
    assert await _consider(client, gate, story, dispatch) == "dispatched"
    await asyncio.sleep(0)
    await dispatch.shutdown()
    assert cancelled.is_set()
    assert dispatch.busy_on(_PR_URL) is False


# ── the argv must be accepted by the REAL merge-gate parser ────────────────
#
# Every spawn above is faked (lens#78's first live dispatch died on a flag the
# CLI did not have — a usage error is the class this pins, for BOTH argv
# shapes the dispatcher builds).


@pytest.mark.parametrize("resolve_only", [False, True])
def test_dispatch_argv_is_accepted_by_the_real_merge_gate_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, resolve_only: bool
) -> None:
    from typer.testing import CliRunner

    from lithos_loom.cli import merge_gate as merge_gate_cli
    from lithos_loom.main import app

    class _Stop(Exception):
        pass

    seen: list[Path | None] = []

    def fake_load_config(path: Path | None) -> None:
        seen.append(path)
        raise _Stop

    monkeypatch.setattr(merge_gate_cli, "load_config", fake_load_config)
    dispatch = MergeGateDispatch(_settings(tmp_path), spawn=_spawner(None)[0])
    spec = PrGateSpec(repo="agent-lore/lithos-lens", pr_number=78, pr_url=_PR_URL)
    cmd = dispatch.command(
        spec,
        tmp_path / "repo",
        tmp_path / "out.json",
        "story-1",
        resolve_only=resolve_only,
    )
    assert cmd[:3] == [sys.executable, "-m", "lithos_loom"]

    result = CliRunner().invoke(app, cmd[3:])

    assert result.exit_code != 2, result.output
    assert "No such option" not in result.output
    assert isinstance(result.exception, _Stop), result.output
    assert seen == [tmp_path / "host.toml"]
