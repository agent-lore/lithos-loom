"""PRD S5, the watcher half: autonomous conflict resolution.

On a still-open ``pr`` gate whose merge-gate record says ``conflict`` for the
CURRENT ``(head, base)`` the sweep dispatches ``develop converge <pr>
--resolve-conflicts --story <id>`` once per sha pair — one in flight at a
time, holding and held by the other two dispatchers on that PR. Converged +
pushed records loom's own push on the S5b budget and posts
``[ConflictResolved]``; a run that could not resolve it raises the needs-human
gate (``conflict_unresolved``); a clean merge or a moved head just records.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from lithos_loom.gates import (
    STORY_HUMAN_GATE_ID_KEY,
    create_pr_gate,
    is_loom_human_gate,
    parse_pr_gate,
)
from lithos_loom.notifications import NeedsHumanNotice
from lithos_loom.subscriptions import SubscriptionContext
from lithos_loom.subscriptions.conflict_resolve_dispatch import (
    CONFLICT_RESOLVE_KEY,
    CONFLICT_RESOLVE_SETTING,
    CONFLICT_RESOLVED,
    PUSHED_BREADCRUMB_KEY,
    ConflictResolveDispatch,
    ConflictResolveSettings,
    OriginRead,
    read_record,
)
from lithos_loom.subscriptions.merge_gate_record import (
    MERGE_GATE_KEY,
    MergeGateRecord,
)
from lithos_loom.subscriptions.remediation_budget import REMEDIATION_KEY, read_budget
from tests.support import FakeLithosClient

_PR_URL = "https://github.com/agent-lore/lithos-lens/pull/62"
_HEAD = "h" * 40
_BASE = "b" * 40
_PUSHED = "p" * 40


@pytest.fixture(autouse=True)
def _resolvable_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    from lithos_loom.subscriptions import conflict_resolve_dispatch as mod

    async def resolvable(path: Path) -> OriginRead:
        return OriginRead("agent-lore/lithos-lens", "ok")

    monkeypatch.setattr(mod, "origin_read", resolvable)


def _ctx(lithos: Any) -> SubscriptionContext:
    return SubscriptionContext(
        lithos=lithos,
        logger=logging.getLogger("test-conflict-resolve"),
        agent_id="lithos-loom-agent",
    )


class _Notifier:
    def __init__(self) -> None:
        self.notices: list[NeedsHumanNotice] = []

    async def needs_human(self, notice: NeedsHumanNotice) -> list[str]:
        self.notices.append(notice)
        return []


async def _gate_with_story(client: FakeLithosClient) -> tuple[str, Any]:
    story = await client.task_create(title="US7", metadata={"project": "p"})
    gate_id = await create_pr_gate(
        client,
        story_id=story,
        story_title="US7",
        pr_url=_PR_URL,
        project="p",
        agent="a",
    )
    return story, await _refresh(client, gate_id)


async def _refresh(client: FakeLithosClient, gate_id: str) -> Any:
    gate = await client.task_get(task_id=gate_id)
    assert gate is not None
    return gate


async def _with_conflict(
    client: FakeLithosClient, gate: Any, *, head: str = _HEAD, base: str = _BASE
) -> Any:
    """The merge-gate record the S3 watcher half leaves on a conflict."""
    record = MergeGateRecord(
        _PR_URL, head, base, status="conflict", behind=True, repo_path="/r"
    )
    await client.task_update(
        task_id=gate.id, metadata={MERGE_GATE_KEY: record.as_marker()}
    )
    return await _refresh(client, gate.id)


def _settings(tmp_path: Path, **overrides: Any) -> ConflictResolveSettings:
    defaults: dict[str, Any] = {
        "enabled": True,
        "projects": {"p": tmp_path / "repo"},
        "work_dir": tmp_path / "work",
        "config_path": tmp_path / "host.toml",
    }
    defaults.update(overrides)
    return ConflictResolveSettings(**defaults)


def _pr(head: str = _HEAD, base: str = _BASE) -> SimpleNamespace:
    return SimpleNamespace(
        head_sha=head,
        base_sha=base,
        base_ref="main",
        head_repo="agent-lore/lithos-lens",
        base_repo="agent-lore/lithos-lens",
    )


def _result(status: str, **overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "status": status,
        "succeeded": status == "converged",
        "head_sha": _HEAD,
        "base_sha": _BASE,
        "rounds": 2,
        "develop_status": "approved" if status == "converged" else "max_rounds",
        "fixer_commits": 3,
        "pushed": status == "converged",
        "pushed_sha": _PUSHED if status == "converged" else None,
        "total_cost_usd": 12.5,
        "message": f"run ended {status}",
        "conflict": {
            "paths": ["AGENTS.md", "docs/x.md"],
            "base_ref": "origin/main",
            "base_sha": _BASE,
        },
    }
    data.update(overrides)
    return data


def _spawner(run: dict[str, Any] | None, *, rc: int = 0) -> tuple[Any, list[list[str]]]:
    calls: list[list[str]] = []

    async def spawn(cmd: list[str]) -> tuple[int, str]:
        calls.append(cmd)
        path = Path(cmd[cmd.index("--json") + 1])
        path.parent.mkdir(parents=True, exist_ok=True)
        if run is not None:
            path.write_text(json.dumps(run), encoding="utf-8")
        return rc, "converge output"

    return spawn, calls


def _findings(client: FakeLithosClient) -> list[str]:
    return [f["summary"] for f in client.findings]


async def _consider(
    client: FakeLithosClient,
    gate: Any,
    story: str | None,
    dispatch: ConflictResolveDispatch,
    *,
    pr: Any = None,
    hold: bool = False,
) -> str:
    spec = parse_pr_gate(gate)
    assert spec is not None
    return await dispatch.consider(
        gate, spec, story, pr if pr is not None else _pr(), _ctx(client), hold=hold
    )


async def _human_gates(client: FakeLithosClient) -> list[Any]:
    return [t for t in await client.task_list(status="open") if is_loom_human_gate(t)]


# ── when it does NOT spawn ────────────────────────────────────────────


async def test_nothing_without_a_current_conflict_record(tmp_path: Path) -> None:
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    spawn, calls = _spawner(_result("converged"))
    dispatch = ConflictResolveDispatch(_settings(tmp_path), spawn=spawn)

    assert await _consider(client, gate, story, dispatch) == "no_conflict"
    # a conflict record for OTHER shas is stale — the merge-gate re-runs first
    stale = await _with_conflict(client, gate, head="x" * 40)
    assert await _consider(client, stale, story, dispatch) == "no_conflict"
    await dispatch.drain()
    assert calls == []


async def test_disabled_held_busy_and_dials(tmp_path: Path) -> None:
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    gate = await _with_conflict(client, gate)
    spawn, calls = _spawner(_result("converged"))

    off = ConflictResolveDispatch(_settings(tmp_path, enabled=False), spawn=spawn)
    assert await _consider(client, gate, story, off) == "disabled"
    dispatch = ConflictResolveDispatch(_settings(tmp_path), spawn=spawn)
    assert await _consider(client, gate, None, dispatch) == "no_story"
    assert await _consider(client, gate, story, dispatch, hold=True) == "held"
    # the per-project dial, fail-closed when unreadable
    await client.note_write(
        title="p project context",
        content="ctx",
        path="projects/p/p-project-context.md",
        metadata={CONFLICT_RESOLVE_SETTING: False},
    )
    assert await _consider(client, gate, story, dispatch) == "opted_out"
    await dispatch.drain()
    assert calls == []


async def test_dispatches_once_per_sha_pair_with_the_pinned_argv(
    tmp_path: Path,
) -> None:
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    gate = await _with_conflict(client, gate)
    spawn, calls = _spawner(_result("converged"))
    dispatch = ConflictResolveDispatch(_settings(tmp_path), spawn=spawn)

    assert await _consider(client, gate, story, dispatch) == "dispatched"
    assert dispatch.busy_on(_PR_URL)
    # the pair is spent the moment it is reserved — a duplicate sweep while
    # the run is in flight is "unchanged", never a second spawn
    assert await _consider(client, gate, story, dispatch) == "unchanged"
    await dispatch.drain()
    (cmd,) = calls
    assert cmd[3:6] == ["develop", "converge", "62"]
    assert "--resolve-conflicts" in cmd and "--from-github" not in cmd
    assert cmd[cmd.index("--story") + 1] == story
    assert cmd[cmd.index("--repo") + 1] == str(tmp_path / "repo")
    assert cmd[cmd.index("--expect-repo") + 1] == "agent-lore/lithos-lens"
    assert cmd[cmd.index("--config") + 1] == str(tmp_path / "host.toml")

    gate = await _refresh(client, gate.id)
    record = read_record(gate, _PR_URL)
    assert record is not None and record.status == "converged"
    # one attempt per (head, base): the same key is settled…
    assert await _consider(client, gate, story, dispatch) == "unchanged"
    # …a moved head (the pushed merge commit) is the next key, for merge-gate first
    assert await _consider(client, gate, story, dispatch, pr=_pr(head=_PUSHED)) == (
        "no_conflict"
    )
    assert len(calls) == 1


def test_dispatch_argv_is_accepted_by_the_real_converge_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A BUILT argv through the real parser (lens#78: a fake spawn hid `No
    such option`)."""
    import sys

    from typer.testing import CliRunner

    from lithos_loom.cli import converge as converge_cli
    from lithos_loom.gates import PrGateSpec
    from lithos_loom.main import app

    class _Stop(Exception):
        pass

    seen: list[Path | None] = []

    def fake_load_config(path: Path | None) -> None:
        seen.append(path)
        raise _Stop

    monkeypatch.setattr(converge_cli, "load_config", fake_load_config)
    host_cfg = tmp_path / "host.toml"
    dispatch = ConflictResolveDispatch(_settings(tmp_path, config_path=host_cfg))
    spec = PrGateSpec(repo="agent-lore/lithos-lens", pr_number=62, pr_url=_PR_URL)
    cmd = dispatch.command(spec, tmp_path / "repo", tmp_path / "out.json", "story-1")
    assert cmd[:3] == [sys.executable, "-m", "lithos_loom"]

    result = CliRunner().invoke(app, cmd[3:])

    assert result.exit_code != 2, result.output  # Typer's usage error
    assert "No such option" not in result.output
    assert isinstance(result.exception, _Stop), result.output
    assert seen == [host_cfg]


# ── outcomes ───────────────────────────────────────────────────────────


async def test_converged_and_pushed_is_looms_own_push(tmp_path: Path) -> None:
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    gate = await _with_conflict(client, gate)
    spawn, _calls = _spawner(_result("converged"))
    dispatch = ConflictResolveDispatch(_settings(tmp_path), spawn=spawn)

    assert await _consider(client, gate, story, dispatch) == "dispatched"
    await dispatch.drain()

    gate = await _refresh(client, gate.id)
    record = read_record(gate, _PR_URL)
    assert record is not None
    assert record.status == "converged" and record.pushed_sha == _PUSHED
    # the S5b budget reads the merge commit as loom's own, never a human push
    assert read_budget(gate, _PR_URL).last_loom_pushed_sha == _PUSHED
    (finding,) = _findings(client)
    assert finding.startswith(CONFLICT_RESOLVED)
    assert "AGENTS.md" in finding and _PUSHED[:12] in finding and "2 round" in finding
    assert await _human_gates(client) == []


@pytest.mark.parametrize("status", ["not_converged", "failed", "conflict_unsupported"])
async def test_an_unresolved_conflict_raises_the_needs_human_gate(
    tmp_path: Path, status: str
) -> None:
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    gate = await _with_conflict(client, gate)
    notifier = _Notifier()
    spawn, calls = _spawner(_result(status))
    dispatch = ConflictResolveDispatch(
        _settings(tmp_path, notifier=notifier), spawn=spawn
    )

    assert await _consider(client, gate, story, dispatch) == "dispatched"
    await dispatch.drain()

    (human,) = await _human_gates(client)
    assert human.metadata["escalation_reason"] == "conflict_unresolved"
    brief = human.metadata["run_brief"]
    assert brief["pr_url"] == _PR_URL and brief["status"] == status
    assert brief["paths"] == ["AGENTS.md", "docs/x.md"]
    story_task = await client.task_get(task_id=story)
    assert story_task is not None
    assert story_task.metadata[STORY_HUMAN_GATE_ID_KEY] == human.id
    needs = [f for f in _findings(client) if f.startswith("[NeedsHuman]")]
    assert len(needs) == 1 and "conflict_unresolved" in needs[0]
    assert [n.reason for n in notifier.notices] == ["conflict_unresolved"]
    gate = await _refresh(client, gate.id)
    record = read_record(gate, _PR_URL)
    assert record is not None and record.needs_human_gate_id == human.id
    # once per key: the next sweep raises no second gate and spawns nothing
    assert await _consider(client, gate, story, dispatch) == "unchanged"
    await dispatch.drain()
    assert len(calls) == 1 and len(await _human_gates(client)) == 1


async def test_a_clean_merge_or_a_race_just_records(tmp_path: Path) -> None:
    for status in ("no_conflict", "merge_race", "merged"):
        client = FakeLithosClient()
        story, gate = await _gate_with_story(client)
        gate = await _with_conflict(client, gate)
        spawn, _calls = _spawner(_result(status, pushed=False, pushed_sha=None))
        dispatch = ConflictResolveDispatch(_settings(tmp_path), spawn=spawn)
        assert await _consider(client, gate, story, dispatch) == "dispatched"
        await dispatch.drain()
        gate = await _refresh(client, gate.id)
        record = read_record(gate, _PR_URL)
        assert record is not None and record.status == status
        assert _findings(client) == [] and await _human_gates(client) == []
        assert await _consider(client, gate, story, dispatch) == "unchanged"


async def test_a_crash_posts_friction_and_re_arms_on_a_restart(tmp_path: Path) -> None:
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    gate = await _with_conflict(client, gate)
    spawn, calls = _spawner(None, rc=1)
    first = ConflictResolveDispatch(_settings(tmp_path), spawn=spawn)

    assert await _consider(client, gate, story, first) == "dispatched"
    await first.drain()
    (finding,) = _findings(client)
    assert finding.startswith("[Friction] conflict-resolve") and "exit 1" in finding
    gate = await _refresh(client, gate.id)
    record = read_record(gate, _PR_URL)
    assert record is not None and record.status == "crashed"
    assert await _consider(client, gate, story, first) == "unchanged"  # not a loop

    second = ConflictResolveDispatch(_settings(tmp_path), spawn=spawn)  # a restart
    assert await _consider(client, gate, story, second) == "dispatched"
    await second.drain()
    assert len(calls) == 2


async def test_a_repo_mismatch_refusal_spawns_nothing_more(tmp_path: Path) -> None:
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    gate = await _with_conflict(client, gate)
    spawn, calls = _spawner(
        {
            "status": "repo_mismatch",
            "expected_repo": "agent-lore/lithos-lens",
            "actual_repo": "agent-lore/other",
            "message": "refused",
        },
        rc=2,
    )
    dispatch = ConflictResolveDispatch(_settings(tmp_path), spawn=spawn)
    assert await _consider(client, gate, story, dispatch) == "dispatched"
    await dispatch.drain()
    (finding,) = _findings(client)
    assert (
        finding.startswith("[Friction] conflict-resolve")
        and "agent-lore/other" in finding
    )
    gate = await _refresh(client, gate.id)
    assert await _consider(client, gate, story, dispatch) == "unchanged"
    assert len(calls) == 1


# ── self-review: writes that must land regardless of the breadcrumb ────────


async def test_a_pushed_resolution_records_the_budget_before_any_finding(
    tmp_path: Path,
) -> None:
    """The push has HAPPENED; the record and the S5b `last_loom_pushed_sha`
    must land even when the [ConflictResolved] finding cannot post — else
    the next sweep reads the merge commit as a human push and resets the
    remediation budget, and the resolver's trigger (the old key) is gone,
    so nothing ever re-derives it."""
    from lithos_loom.errors import LithosClientError

    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    gate = await _with_conflict(client, gate)

    async def boom(**kwargs):
        raise LithosClientError("server_error", "findings down")

    client.finding_post = boom  # type: ignore[method-assign]
    spawn, _calls = _spawner(_result("converged"))
    dispatch = ConflictResolveDispatch(_settings(tmp_path), spawn=spawn)
    assert await _consider(client, gate, story, dispatch) == "dispatched"
    await dispatch.drain()

    gate = await _refresh(client, gate.id)
    record = read_record(gate, _PR_URL)
    assert record is not None and record.status == "converged"
    assert read_budget(gate, _PR_URL).last_loom_pushed_sha == _PUSHED


async def test_a_failed_escalation_record_never_re_runs_or_double_gates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the gate's resolve record fails to land after the human gate was
    raised, the story still names the gate — and that is the guard: the next
    sweep sees an open loom human gate on the story and dispatches nothing
    (no second paid run, no second gate)."""
    from lithos_loom.subscriptions import conflict_resolve_outcome as out

    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    gate = await _with_conflict(client, gate)
    real_write = out.write_marker
    fail = {"on": True}

    async def flaky(ctx, *, task_id, marker, subsystem):
        # the reservation lands; the escalation record (gate id) does not
        record = marker.get(CONFLICT_RESOLVE_KEY) or {}
        if fail["on"] and record.get("needs_human_gate_id"):
            return False
        return await real_write(
            ctx, task_id=task_id, marker=marker, subsystem=subsystem
        )

    monkeypatch.setattr(out, "write_marker", flaky)
    monkeypatch.setattr(out, "STRICT_WRITE_DELAYS", ())
    spawn, calls = _spawner(_result("not_converged"))
    dispatch = ConflictResolveDispatch(_settings(tmp_path), spawn=spawn)
    assert await _consider(client, gate, story, dispatch) == "dispatched"
    await dispatch.drain()
    (human,) = await _human_gates(client)
    stale = read_record(await _refresh(client, gate.id), _PR_URL)
    assert stale is not None and stale.status == "running"  # the reservation only
    assert any("could not record" in f for f in _findings(client))  # said out loud

    fail["on"] = False
    gate = await _refresh(client, gate.id)
    # this boot remembers the spent pair…
    assert await _consider(client, gate, story, dispatch) == "unchanged"
    # …and a RESTART (which would re-arm a stale `running` reservation) is
    # stopped by the story-side belt: an open loom human gate already waits
    rebooted = ConflictResolveDispatch(_settings(tmp_path), spawn=spawn)
    assert await _consider(client, gate, story, rebooted) == "escalated"
    await dispatch.drain()
    await rebooted.drain()
    assert len(calls) == 1 and [g.id for g in await _human_gates(client)] == [human.id]


async def test_unreadable_dial_and_checkout_spawn_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lithos_loom.errors import LithosClientError
    from lithos_loom.subscriptions import conflict_resolve_dispatch as mod

    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    gate = await _with_conflict(client, gate)
    spawn, calls = _spawner(_result("converged"))
    dispatch = ConflictResolveDispatch(_settings(tmp_path), spawn=spawn)

    async def unreadable(**kwargs):
        raise LithosClientError("server_error", "notes down")

    monkeypatch.setattr(client, "note_read", unreadable)
    monkeypatch.setattr(client, "note_list", unreadable)
    assert await _consider(client, gate, story, dispatch) == "dial_unreadable"
    monkeypatch.undo()

    async def missing(path: Path) -> OriginRead:
        return OriginRead(None, "missing")

    monkeypatch.setattr(mod, "origin_read", missing)
    assert await _consider(client, gate, story, dispatch) == "checkout_unresolved"

    async def other(path: Path) -> OriginRead:
        return OriginRead("agent-lore/other", "ok")

    monkeypatch.setattr(mod, "origin_read", other)
    assert await _consider(client, gate, story, dispatch) == "repo_mismatch"
    await dispatch.drain()
    assert calls == []


# ── PR #366 review round 1 ─────────────────────────────────────────────────


async def test_the_run_is_pinned_to_the_triggering_sha_pair(tmp_path: Path) -> None:
    """F3: the child re-fetches the PR; it must refuse before any agent when
    the head or the base tip no longer equals the pair the sweep authorized.
    The argv carries both; a `head_moved` / `base_moved` answer just records
    (the next sweep re-keys)."""
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    gate = await _with_conflict(client, gate)
    spawn, calls = _spawner(
        {
            "status": "head_moved",
            "expected_head": _HEAD,
            "actual_head": "n" * 40,
            "message": "the PR head moved",
        }
    )
    dispatch = ConflictResolveDispatch(_settings(tmp_path), spawn=spawn)
    assert await _consider(client, gate, story, dispatch) == "dispatched"
    await dispatch.drain()
    (cmd,) = calls
    assert cmd[cmd.index("--expect-head") + 1] == _HEAD
    assert cmd[cmd.index("--expect-base") + 1] == _BASE
    gate = await _refresh(client, gate.id)
    record = read_record(gate, _PR_URL)
    assert record is not None and record.status == "head_moved"
    assert _findings(client) == [] and await _human_gates(client) == []


async def test_the_attempt_is_reserved_before_the_spawn(tmp_path: Path) -> None:
    """F2: the once-per-pair bound lived only in a record written AFTER the
    run, through the finding helper — a failed breadcrumb left the next sweep
    free to spend again. The attempt is reserved on the gate strictly BEFORE
    the spawn, and a reservation that does not land spawns nothing."""
    from lithos_loom.subscriptions import conflict_resolve_outcome as out

    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    gate = await _with_conflict(client, gate)
    seen: list[str] = []

    async def peeking(cmd: list[str]) -> tuple[int, str]:
        g = await client.task_get(task_id=gate.id)
        assert g is not None
        rec = read_record(g, _PR_URL)
        seen.append(rec.status if rec else "absent")
        return 1, "boom"  # and then it crashes, producing no result

    dispatch = ConflictResolveDispatch(_settings(tmp_path), spawn=peeking)
    assert await _consider(client, gate, story, dispatch) == "dispatched"
    await dispatch.drain()
    assert seen == ["running"]  # reserved while the child ran

    # a reservation that cannot be written spawns nothing
    monkeypatch_write = {"fail": True}
    real = out.write_marker

    async def flaky(ctx, *, task_id, marker, subsystem):
        if monkeypatch_write["fail"] and CONFLICT_RESOLVE_KEY in marker:
            return False
        return await real(ctx, task_id=task_id, marker=marker, subsystem=subsystem)

    out.write_marker = flaky  # type: ignore[assignment]
    try:
        client2 = FakeLithosClient()
        story2, gate2 = await _gate_with_story(client2)
        gate2 = await _with_conflict(client2, gate2)
        spawn2, calls2 = _spawner(_result("converged"))
        d2 = ConflictResolveDispatch(_settings(tmp_path), spawn=spawn2)
        assert await _consider(client2, gate2, story2, d2) == "reserve_failed"
        await d2.drain()
        assert calls2 == []
    finally:
        out.write_marker = real  # type: ignore[assignment]


async def test_a_crash_whose_breadcrumb_fails_cannot_spend_again_this_boot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F2: finding AND marker writes both fail after a crash — the in-boot
    memory of the attempt (and the pre-spawn reservation) still stop a second
    paid run in the same daemon boot."""
    from lithos_loom.errors import LithosClientError
    from lithos_loom.subscriptions import conflict_resolve_outcome as out

    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    gate = await _with_conflict(client, gate)
    spawn, calls = _spawner(None, rc=1)
    dispatch = ConflictResolveDispatch(_settings(tmp_path), spawn=spawn)
    assert await _consider(client, gate, story, dispatch) == "dispatched"
    await dispatch.drain()  # the reservation landed; the crash record follows

    async def boom(**kwargs):
        raise LithosClientError("server_error", "down")

    real = out.write_marker

    async def never(ctx, *, task_id, marker, subsystem):
        return False

    monkeypatch.setattr(client, "finding_post", boom)
    monkeypatch.setattr(out, "write_marker", never)
    # simulate the worst case: even the reservation is wiped from the gate
    await client.task_update(task_id=gate.id, metadata={CONFLICT_RESOLVE_KEY: None})
    gate = await _refresh(client, gate.id)
    assert read_record(gate, _PR_URL) is None
    assert await _consider(client, gate, story, dispatch) == "unchanged"
    await dispatch.drain()
    assert len(calls) == 1
    monkeypatch.setattr(out, "write_marker", real)


async def test_a_pushed_resolution_keeps_the_budget_from_the_dispatch_snapshot(
    tmp_path: Path,
) -> None:
    """F1a: the gate refresh after the push fails — the budget still lands
    from the snapshot taken at dispatch (the push has happened; a lost
    `last_loom_pushed_sha` = a human-push reset next sweep)."""
    from lithos_loom.errors import LithosClientError

    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    gate = await _with_conflict(client, gate)
    spawn, _calls = _spawner(_result("converged"))
    dispatch = ConflictResolveDispatch(_settings(tmp_path), spawn=spawn)
    real_get = client.task_get
    state = {"armed": False}

    async def flaky_get(*, task_id: str):
        if state["armed"] and task_id == gate.id:
            raise LithosClientError("server_error", "down")
        return await real_get(task_id=task_id)

    client.task_get = flaky_get  # type: ignore[method-assign]
    assert await _consider(client, gate, story, dispatch) == "dispatched"
    state["armed"] = True  # the refresh after the run fails
    await dispatch.drain()
    state["armed"] = False
    gate = await _refresh(client, gate.id)
    assert read_budget(gate, _PR_URL).last_loom_pushed_sha == _PUSHED
    record = read_record(gate, _PR_URL)
    assert record is not None and record.status == "converged"


async def test_a_pushed_resolution_whose_write_fails_holds_until_it_lands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F1b: the combined outcome+budget write is a REQUIRED post-push
    transition. While it has not landed: no success finding, an honest
    [Friction], and the resolver stays `busy_on` the PR (so remediation's
    head observation stays inert and cannot reset the budget); the debt is
    retried on later sweeps and released only when it lands."""
    from lithos_loom.subscriptions import conflict_resolve_outcome as out

    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    gate = await _with_conflict(client, gate)
    spawn, _calls = _spawner(_result("converged"))
    dispatch = ConflictResolveDispatch(_settings(tmp_path), spawn=spawn)
    real = out.write_marker
    fail = {"on": False}

    async def flaky(ctx, *, task_id, marker, subsystem):
        if fail["on"] and REMEDIATION_KEY in marker:
            return False
        return await real(ctx, task_id=task_id, marker=marker, subsystem=subsystem)

    monkeypatch.setattr(out, "write_marker", flaky)
    monkeypatch.setattr(out, "STRICT_WRITE_DELAYS", ())
    assert await _consider(client, gate, story, dispatch) == "dispatched"
    fail["on"] = True
    await dispatch.drain()

    assert not [f for f in _findings(client) if f.startswith(CONFLICT_RESOLVED)]
    assert any(f.startswith("[Friction] conflict-resolve") for f in _findings(client))
    assert dispatch.busy_on(_PR_URL)  # the debt holds the PR
    gate = await _refresh(client, gate.id)
    assert read_budget(gate, _PR_URL).last_loom_pushed_sha == ""

    fail["on"] = False  # Lithos answers again: the next sweep flushes the debt
    assert await _consider(client, gate, story, dispatch, pr=_pr(head=_PUSHED)) == (
        "debt_settled"
    )
    gate = await _refresh(client, gate.id)
    assert read_budget(gate, _PR_URL).last_loom_pushed_sha == _PUSHED
    assert any(f.startswith(CONFLICT_RESOLVED) for f in _findings(client))
    assert not dispatch.busy_on(_PR_URL)


async def test_a_repo_mismatch_refusal_re_arms_when_the_mapping_moves(
    tmp_path: Path,
) -> None:
    """F4: the child's authoritative repo-mismatch refusal settles on what the
    sweep observes — the mapped path and its origin — and re-arms for the
    same sha pair when either moves (the advertised repair)."""
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    gate = await _with_conflict(client, gate)
    spawn, calls = _spawner(
        {
            "status": "repo_mismatch",
            "expected_repo": "agent-lore/lithos-lens",
            "actual_repo": "agent-lore/other",
            "message": "refused",
        },
        rc=2,
    )
    dispatch = ConflictResolveDispatch(_settings(tmp_path), spawn=spawn)
    assert await _consider(client, gate, story, dispatch) == "dispatched"
    await dispatch.drain()
    gate = await _refresh(client, gate.id)
    assert await _consider(client, gate, story, dispatch) == "unchanged"
    # the operator re-points the mapping: a fresh attempt on the same shas
    moved = ConflictResolveDispatch(
        _settings(tmp_path, projects={"p": tmp_path / "repo-fixed"}), spawn=spawn
    )
    assert await _consider(client, gate, story, moved) == "dispatched"
    await moved.drain()
    assert len(calls) == 2


async def test_a_clean_resolution_leaves_no_hold_behind(tmp_path: Path) -> None:
    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    gate = await _with_conflict(client, gate)
    spawn, _calls = _spawner(_result("converged"))
    dispatch = ConflictResolveDispatch(_settings(tmp_path), spawn=spawn)
    assert await _consider(client, gate, story, dispatch) == "dispatched"
    await dispatch.drain()
    assert not dispatch.busy_on(_PR_URL)
    story_task = await client.task_get(task_id=story)
    assert story_task is not None
    assert story_task.metadata.get(PUSHED_BREADCRUMB_KEY) is None


async def test_a_held_debt_survives_a_restart_through_the_story_breadcrumb(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Self-review of round 1: the debt was in-memory only — a restart while
    the combined write was still failing would let the next sweep read the
    merge commit as a human push. The push is breadcrumbed on the STORY (a
    different task, so a gate write outage does not take it down) before the
    combined write; a fresh boot recovers the debt from it BEFORE
    remediation observes the head, holds the PR, flushes, and clears it."""
    from lithos_loom.subscriptions import conflict_resolve_outcome as out

    client = FakeLithosClient()
    story, gate = await _gate_with_story(client)
    gate = await _with_conflict(client, gate)
    spawn, _calls = _spawner(_result("converged"))
    dispatch = ConflictResolveDispatch(_settings(tmp_path), spawn=spawn)
    real = out.write_marker
    fail = {"on": False}

    async def flaky(ctx, *, task_id, marker, subsystem):
        if fail["on"] and REMEDIATION_KEY in marker:
            return False
        return await real(ctx, task_id=task_id, marker=marker, subsystem=subsystem)

    monkeypatch.setattr(out, "write_marker", flaky)
    monkeypatch.setattr(out, "STRICT_WRITE_DELAYS", ())
    assert await _consider(client, gate, story, dispatch) == "dispatched"
    fail["on"] = True
    await dispatch.drain()
    story_task = await client.task_get(task_id=story)
    assert story_task is not None
    crumb = story_task.metadata[PUSHED_BREADCRUMB_KEY]
    assert crumb["pr_url"] == _PR_URL and crumb["pushed_sha"] == _PUSHED

    # the daemon restarts: a fresh dispatcher, empty memory
    rebooted = ConflictResolveDispatch(_settings(tmp_path), spawn=spawn)
    assert not rebooted.busy_on(_PR_URL)
    gate = await _refresh(client, gate.id)
    spec = parse_pr_gate(gate)
    assert spec is not None
    await rebooted.recover_debt(gate, spec, story, _ctx(client))
    assert rebooted.busy_on(_PR_URL)  # held again, before anyone observes the head

    fail["on"] = False
    assert await _consider(client, gate, story, rebooted, pr=_pr(head=_PUSHED)) == (
        "debt_settled"
    )
    gate = await _refresh(client, gate.id)
    assert read_budget(gate, _PR_URL).last_loom_pushed_sha == _PUSHED
    assert not rebooted.busy_on(_PR_URL)
    story_task = await client.task_get(task_id=story)
    assert story_task is not None
    assert story_task.metadata.get(PUSHED_BREADCRUMB_KEY) is None
